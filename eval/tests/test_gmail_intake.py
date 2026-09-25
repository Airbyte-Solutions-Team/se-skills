"""User-triggered Gmail evidence intake (PR E), tested against the synthetic transport only.

No Gmail credential, OAuth client, or live mailbox exists in this repository; the
`SyntheticGmailTransport` is the only transport these tests use. Live retrieval is
recorded as unavailable in `UnavailableGmailTransport` and in the PR.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from integrations.gmail import (
    GmailImportError,
    GmailSourceAdapter,
    GmailTransportError,
    SyntheticGmailTransport,
    UnavailableGmailTransport,
    strip_quoted_reply,
)
from integrations.granola import ManualGranolaImportAdapter
from integrations.granola_mcp_relay import FakeGranolaRetrievalTransport
from routes.command_center import router
from services.command_center_operations_service import (
    CommandCenterOperationsError,
    CommandCenterOperationsService,
)
from services.command_center_read_service import CommandCenterReadService
from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService
from services.gmail_intake_service import (
    CONNECTION_ID,
    MAX_SELECTION,
    GmailIntakeError,
    GmailIntakeService,
)
from services.granola_retrieval_service import GranolaRetrievalService
from services.job_service import JobService

from eval.tests.test_command_center_routes import RejectingStateService, UnusedExecutor

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "command_center" / "gmail" / "mailbox_synthetic.json"
MAILBOX = json.loads(FIXTURE.read_text(encoding="utf-8"))
SENTINEL = "SYNTHETIC-BODY"
ACME_ASK, ACME_REPLY, ACME_NO_BODY = "18a1000000000001", "18a1000000000002", "18a1000000000003"
GLOBEX, NEWSLETTER, INTERNAL = "18a1000000000004", "18a1000000000005", "18a1000000000006"
WINDOW = {"time_range": "custom", "custom_start": date(2026, 9, 1), "custom_end": date(2026, 9, 30)}


class EchoWorkspace:
    """Resolves any (account, slug) the test names, so isolation between accounts is real."""

    async def resolve_identity(self, account: str, opp_slug: str) -> dict:
        return {"safe_account": account, "safe_opp": opp_slug, "opportunity": {"sfdc_id": None, "sfdc_account_id": None}}


def _opps() -> list[dict]:
    return [
        {"account": "Acme", "opportunity_slug": "expansion", "opportunity_name": "Acme expansion"},
        {"account": "Globex", "opportunity_slug": "pilot", "opportunity_name": "Globex pilot"},
        {"account": "Globex", "opportunity_slug": "renewal", "opportunity_name": "Globex renewal"},
    ]


def _service(tmp_path: Path) -> tuple[GmailIntakeService, SyntheticGmailTransport, EvidenceLedgerService, JobService]:
    customers = tmp_path / "customers"
    customers.mkdir(exist_ok=True)
    transport = SyntheticGmailTransport.from_fixture(FIXTURE)
    ledger = EvidenceLedgerService(customers)
    jobs = JobService(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    service = GmailIntakeService(
        transport=transport, adapter=GmailSourceAdapter(), ledger=ledger, job_service=jobs, local_opportunities=_opps,
    )
    return service, transport, ledger, jobs


async def _wait(jobs: JobService, job_id: str) -> dict:
    for _ in range(300):
        job = jobs.get_job(job_id)
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("intake job did not finish")


async def _retrieve(service: GmailIntakeService, jobs: JobService, ids: list[str]) -> dict:
    started = await service.start_retrieval(ids)
    return await _wait(jobs, started["job_id"])


def _by_id(job: dict) -> dict[str, dict]:
    return {row["message_id"]: row for row in job["results"]}


# ------------------------------------------------------------- adapter


def test_strip_quoted_reply_keeps_only_the_replys_own_words() -> None:
    text, removed = strip_quoted_reply("Done.\n\nOn Mon, Sep 21, 2026 at 10:05 AM X <x@y.example> wrote:\n> earlier")
    assert (text, removed) == ("Done.", True)
    assert strip_quoted_reply("Plain\nlines") == ("Plain\nlines", False)
    assert strip_quoted_reply("> only quoted") == ("", True)


def test_adapter_rejects_attachment_content_and_unknown_fields() -> None:
    adapter = GmailSourceAdapter()
    row = dict(MAILBOX["messages"][0])
    with pytest.raises(GmailImportError) as exc:
        adapter.normalize({**row, "attachments": [{"data": "AAA="}]}, connection_id=CONNECTION_ID)
    assert exc.value.code == "malformed_payload"
    normalized = adapter.normalize(row, connection_id=CONNECTION_ID)
    assert normalized.identity.kind == "email_message" and normalized.identity.provider == "gmail"
    assert SENTINEL not in json.dumps(normalized.private_metadata())
    assert normalized.revision_metrics().body_chars > 0


def test_no_body_message_is_metadata_only_and_never_content_available() -> None:
    adapter = GmailSourceAdapter()
    row = next(m for m in MAILBOX["messages"] if m["id"] == ACME_NO_BODY)
    normalized = adapter.normalize(row, connection_id=CONNECTION_ID)
    assert normalized.availability == "metadata_only" and normalized.unavailable_reason == "no_body"
    assert normalized.attachment_count == 1 and normalized.content.is_empty()


def test_unavailable_transport_describes_no_live_route_and_fails_closed(tmp_path: Path) -> None:
    transport = UnavailableGmailTransport()
    description = transport.describe()
    assert description.live_retrieval_available is False and description.mode == "unavailable"
    service, _, ledger, jobs = _service(tmp_path)
    service = GmailIntakeService(
        transport=transport, adapter=GmailSourceAdapter(), ledger=ledger, job_service=jobs, local_opportunities=_opps,
    )
    result = asyncio.run(service.check_access())
    assert result == {**result, "connected": False, "error_code": "transport_unavailable", "retryable": False}
    with pytest.raises(GmailIntakeError) as exc:
        asyncio.run(service.list_threads(participants=["acme.example"], **WINDOW))
    assert exc.value.code == "not_checked"


# ------------------------------------------------------------- access + listing


async def test_access_check_requires_readonly_scope_only(tmp_path: Path) -> None:
    service, transport, _, _ = _service(tmp_path)
    transport.scopes.append("https://www.googleapis.com/auth/gmail.modify")
    checked = await service.check_access()
    assert checked["connected"] is False and checked["error_code"] == "scope_not_readonly"
    assert service.connection()["last_check"] is None
    transport.scopes = ["https://www.googleapis.com/auth/gmail.readonly"]
    checked = await service.check_access()
    assert checked["connected"] is True and checked["mailbox_domain"] == "example.invalid"
    assert "email_address" not in checked  # only the domain is persisted/returned


async def test_listing_is_bounded_body_free_and_excludes_unrelated_and_internal(tmp_path: Path) -> None:
    service, _, _, _ = _service(tmp_path)
    with pytest.raises(GmailIntakeError) as exc:
        await service.list_threads(participants=["acme.example"], **WINDOW)
    assert exc.value.code == "not_checked"
    await service.check_access()
    with pytest.raises(GmailIntakeError) as exc:
        await service.list_threads(participants=[], **WINDOW)
    assert exc.value.code == "unbounded_discovery"
    with pytest.raises(GmailIntakeError) as exc:
        await service.list_threads(participants=["not a term"], **WINDOW)
    assert exc.value.code == "invalid_term"

    listed = await service.list_threads(participants=["acme.example", "ops@globex.example"], **WINDOW)
    assert SENTINEL not in json.dumps(listed)
    assert listed["unattended_discovery"] is False
    ids = {t["thread_id"] for t in listed["threads"]}
    assert ids == {ACME_ASK, GLOBEX}  # newsletter + internal-only never shown
    acme = next(t for t in listed["threads"] if t["thread_id"] == ACME_ASK)
    assert acme["message_count"] == 3 and acme["matched_on"] == ["domain"]
    assert "se@example.invalid" not in acme["external_participants"]
    assert all(m["ledger"] is None for m in acme["messages"])

    # The synthetic transport only filters on the bound it is given; the service still
    # drops anything the transport returns that matches nothing.
    everything = await service.list_threads(participants=["unrelated.example"], **WINDOW)
    assert [t["thread_id"] for t in everything["threads"]] == [NEWSLETTER]
    assert everything["excluded_unrelated"] == 0


async def test_listing_excludes_internal_only_threads_even_when_transport_returns_them(tmp_path: Path) -> None:
    service, transport, _, _ = _service(tmp_path)
    await service.check_access()

    async def leaky(**_kwargs):
        return await SyntheticGmailTransport.list_threads(
            transport, after=date(2026, 9, 1), before=date(2026, 9, 30), participants=[], max_results=50
        )

    transport.list_threads = leaky  # type: ignore[method-assign]
    listed = await service.list_threads(participants=["acme.example"], **WINDOW)
    assert {t["thread_id"] for t in listed["threads"]} == {ACME_ASK}
    assert listed["excluded_internal"] == 1 and listed["excluded_unrelated"] == 2


# ------------------------------------------------------------- retrieval


async def test_retrieval_imports_only_selected_messages_and_proposes_never_associates(tmp_path: Path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_access()
    with pytest.raises(GmailIntakeError) as exc:
        await service.start_retrieval([])
    assert exc.value.code == "empty_selection"
    with pytest.raises(GmailIntakeError) as exc:
        await service.start_retrieval([f"{n:016x}" for n in range(MAX_SELECTION + 1)])
    assert exc.value.code == "selection_too_large"

    job = await _retrieve(service, jobs, [ACME_ASK, ACME_NO_BODY, GLOBEX])
    assert job["ok"], job
    assert SENTINEL not in json.dumps(job)
    rows = _by_id(job)
    assert rows[ACME_ASK]["outcome"] == "imported" and rows[ACME_ASK]["association_state"] == "proposed"
    assert rows[ACME_ASK]["proposed_candidates"] == 1
    assert rows[ACME_NO_BODY]["outcome"] == "no_body" and rows[ACME_NO_BODY]["availability"] == "metadata_only"
    # Globex has two local opportunities: ambiguous → two candidates, still only a proposal.
    assert rows[GLOBEX]["proposed_candidates"] == 2 and rows[GLOBEX]["association_state"] == "proposed"
    assert transport.calls.count("get_messages") == 1
    assert job["message_ids"] == [ACME_ASK, ACME_NO_BODY, GLOBEX]

    listed = ledger.list_sources()
    assert SENTINEL not in json.dumps(listed)
    assert {s["provider_object_id"] for s in listed["sources"]} == {ACME_ASK, ACME_NO_BODY, GLOBEX}
    globex = next(s for s in listed["sources"] if s["provider_object_id"] == GLOBEX)
    assert globex["association"]["state"] == "proposed"
    candidates = {(c["account"], c["opportunity_slug"]) for c in globex["association"]["candidates"]}
    assert candidates == {("Globex", "pilot"), ("Globex", "renewal")}
    assert globex["processing"]["status"] == "awaiting_association"
    assert globex["latest"]["metrics"]["body_chars"] > 0
    # Private content is retrievable only through the ledger's content path.
    content = ledger.read_content(globex["source_id"], revision=1)
    assert SENTINEL in json.dumps(content)


async def test_reply_chain_uses_thread_mapping_after_confirmation_and_dedups_quoted_text(tmp_path: Path) -> None:
    service, _transport, ledger, jobs = _service(tmp_path)
    await service.check_access()
    first = _by_id(await _retrieve(service, jobs, [ACME_ASK]))[ACME_ASK]
    await ledger.confirm_association(
        first["source_id"], account="Acme", opportunity_slug="expansion", reason="Confirmed by SE",
        resolve_identity=EchoWorkspace().resolve_identity,
    )
    reply = _by_id(await _retrieve(service, jobs, [ACME_REPLY]))[ACME_REPLY]
    assert reply["outcome"] == "imported" and reply["proposed_candidates"] == 1
    source = ledger.get_source(reply["source_id"])
    assert source["association"]["state"] == "proposed"
    candidate = source["association"]["candidates"][0]
    assert candidate["method"] == "thread_mapping" and candidate["opportunity_slug"] == "expansion"
    content = ledger.read_content(reply["source_id"], revision=1)
    body = content["content"]["body_text"]
    assert "SYNTHETIC-QUOTED" not in body and content["content"]["quoted_text_removed"] is True
    assert content["metadata"]["reply_depth"] == 1 and content["metadata"]["attachment_count"] == 1
    assert "attachments" not in content["content"]


async def test_repeat_retrieval_is_duplicate_and_edited_message_is_a_new_revision(tmp_path: Path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_access()
    first = _by_id(await _retrieve(service, jobs, [GLOBEX]))[GLOBEX]
    again = _by_id(await _retrieve(service, jobs, [GLOBEX]))[GLOBEX]
    assert again["outcome"] == "already_known" and again["revision"] == 1
    assert "proposed_candidates" not in again  # no second proposal is appended
    source = ledger.get_source(first["source_id"])
    assert len(source["association_history"]) == 2  # unassociated + one proposal

    transport.edit_body(GLOBEX, "SYNTHETIC-BODY-GLOBEX corrected: CDC on partitioned tables, plus a written answer by Monday.")
    edited = _by_id(await _retrieve(service, jobs, [GLOBEX]))[GLOBEX]
    assert edited["outcome"] == "edited" and edited["revision"] == 2 and edited["change"] == "content"
    assert len(ledger.get_source(first["source_id"])["association_history"]) == 2


async def test_transient_failure_before_batch_is_retryable_then_second_attempt_imports(tmp_path: Path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_access()
    transport.fail_next("rate_limited", retryable=True)
    job = await _retrieve(service, jobs, [ACME_ASK])
    assert job["ok"] is False and job["error_code"] == "rate_limited"
    assert ledger.list_sources()["total"] == 0 and service.connection()["last_check"] is not None
    job = await _retrieve(service, jobs, [ACME_ASK])
    assert job["ok"] and _by_id(job)[ACME_ASK]["outcome"] == "imported"


async def test_get_messages_retryable_failure_reports_per_row_and_nothing_is_imported(tmp_path: Path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_access()
    original = transport.get_messages

    async def flaky(ids):
        transport.get_messages = original  # type: ignore[method-assign]
        raise GmailTransportError("transient", "synthetic", retryable=True)

    transport.get_messages = flaky  # type: ignore[method-assign]
    job = await _retrieve(service, jobs, [ACME_ASK, GLOBEX])
    assert job["ok"] and job["counts"] == {"failed_retryable": 2}
    assert ledger.list_sources()["total"] == 0
    job = await _retrieve(service, jobs, [ACME_ASK, GLOBEX])
    assert job["counts"] == {"imported": 2}


async def test_revocation_marks_sources_access_lost_withholds_content_and_blocks_retrieval(tmp_path: Path) -> None:
    service, transport, ledger, jobs = _service(tmp_path)
    await service.check_access()
    row = _by_id(await _retrieve(service, jobs, [ACME_ASK]))[ACME_ASK]
    assert ledger.read_content(row["source_id"], revision=1)["content"]["body_text"]

    transport.revoke()
    job = await _retrieve(service, jobs, [GLOBEX])
    assert job["ok"] is False and job["error_code"] == "access_revoked"
    assert service.connection()["last_check"] is None
    with pytest.raises(GmailIntakeError) as exc:
        await service.start_retrieval([GLOBEX])
    assert exc.value.code == "not_checked"

    revoked = service.revoke_access()
    assert revoked["sources_marked"] == 1 and revoked["results"][0]["outcome"] == "inaccessible"
    source = ledger.get_source(row["source_id"])
    assert source["availability"] == "access_lost" and source["latest_revision"] == 2
    with pytest.raises(EvidenceLedgerError) as exc:
        ledger.read_content(row["source_id"], revision=1)
    assert exc.value.code == "content_withheld"
    assert service.revoke_access()["sources_marked"] == 0

    transport.restore()
    assert (await service.check_access())["connected"] is True
    restored = _by_id(await _retrieve(service, jobs, [ACME_ASK]))[ACME_ASK]
    assert restored["outcome"] == "edited" and restored["change"] == "availability" and restored["revision"] == 3
    assert ledger.read_content(row["source_id"], revision=1)["content"]["body_text"]


async def test_deleted_message_is_not_found_and_only_one_retrieval_runs_at_a_time(tmp_path: Path) -> None:
    service, transport, _, jobs = _service(tmp_path)
    await service.check_access()
    transport.remove(GLOBEX)
    row = _by_id(await _retrieve(service, jobs, [GLOBEX]))[GLOBEX]
    assert row["outcome"] == "not_found" and row["availability"] == "pending_unknown"

    gate = asyncio.Event()
    original = transport.get_messages

    async def slow(ids):
        await gate.wait()
        return await original(ids)

    transport.get_messages = slow  # type: ignore[method-assign]
    started = await service.start_retrieval([ACME_ASK])
    with pytest.raises(GmailIntakeError) as exc:
        await service.start_retrieval([ACME_REPLY])
    assert exc.value.code == "retrieval_in_progress"
    gate.set()
    assert (await _wait(jobs, started["job_id"]))["counts"] == {"imported": 1}


# ------------------------------------------------------------- routes


def _client(tmp_path: Path) -> tuple[TestClient, SyntheticGmailTransport, EvidenceLedgerService]:
    (tmp_path / "customers").mkdir()
    app = FastAPI()
    workspace = EchoWorkspace()
    ledger = EvidenceLedgerService(tmp_path / "customers")
    transport = SyntheticGmailTransport.from_fixture(FIXTURE)
    jobs = JobService(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    app.state.opportunity_workspace_service = workspace
    app.state.evidence_ledger_service = ledger
    app.state.job_service = jobs
    app.state.granola_adapter = ManualGranolaImportAdapter()
    app.state.granola_retrieval_service = GranolaRetrievalService(
        transport=FakeGranolaRetrievalTransport(), adapter=ManualGranolaImportAdapter(), ledger=ledger, job_service=jobs,
    )
    app.state.command_center_operations_service = CommandCenterOperationsService(
        ledger=ledger, workspace_service=workspace, state_service=RejectingStateService(), job_service=jobs,
        executor=UnusedExecutor(),
    )
    reads = CommandCenterReadService(
        customers_dir=tmp_path / "customers", ledger=ledger, operations=app.state.command_center_operations_service,
        state_service=RejectingStateService(),
    )
    app.state.command_center_read_service = reads
    app.state.gmail_intake_service = GmailIntakeService(
        transport=transport, adapter=GmailSourceAdapter(), ledger=ledger, job_service=jobs,
        local_opportunities=_opps,
    )
    app.include_router(router)
    return TestClient(app), transport, ledger


def test_routes_check_list_retrieve_review_confirm_and_revoke(tmp_path: Path) -> None:
    client, _transport, _ = _client(tmp_path)
    adapters = client.get("/api/command-center/adapters").json()["adapters"]
    gmail = next(a for a in adapters if a.get("provider") == "gmail")
    assert gmail["unattended_discovery"] is False and gmail["live_retrieval_available"] is False

    connection = client.get("/api/command-center/gmail/connection").json()
    assert connection["checked"] is False and connection["transport"]["mode"] == "synthetic_fixture"
    listed = client.post("/api/command-center/gmail/threads/list", json={"participants": ["acme.example"]})
    assert listed.status_code == 409 and listed.json()["detail"]["code"] == "not_checked"
    assert client.post("/api/command-center/gmail/connection/check", json={}).json()["connected"] is True

    body = {"time_range": "custom", "custom_start": "2026-09-01", "custom_end": "2026-09-30", "participants": ["acme.example", "globex.example"]}
    listed = client.post("/api/command-center/gmail/threads/list", json=body)
    assert listed.status_code == 200, listed.text
    assert SENTINEL not in listed.text and listed.json()["shown"] == 2
    assert client.post("/api/command-center/gmail/threads/list", json={**body, "extra": 1}).status_code == 422
    assert client.post("/api/command-center/gmail/threads/list", json={**body, "participants": []}).status_code == 400

    too_many = client.post("/api/command-center/gmail/retrievals", json={"message_ids": [f"{n:016x}" for n in range(MAX_SELECTION + 1)]})
    assert too_many.status_code == 422
    started = client.post("/api/command-center/gmail/retrievals", json={"message_ids": [GLOBEX, ACME_NO_BODY]})
    assert started.status_code == 202, started.text
    job_id = started.json()["job_id"]
    for _ in range(300):
        job = client.get(f"/api/command-center/gmail/retrievals/{job_id}").json()
        if job["status"] != "running":
            break
    assert job["ok"] and SENTINEL not in json.dumps(job), job
    assert job["counts"] == {"imported": 1, "no_body": 1}
    assert client.get("/api/command-center/gmail/retrievals/nope").status_code == 404

    source_id = next(r["source_id"] for r in job["results"] if r["message_id"] == GLOBEX)
    queue = client.get("/api/command-center/sources/unprocessed").json()
    assert SENTINEL not in json.dumps(queue)
    assert source_id in {s["source_id"] for s in queue["sources"]}
    review = client.get(f"/api/command-center/sources/{source_id}/review")
    assert review.status_code == 200 and SENTINEL not in review.text
    payload = review.json()
    assert payload["source"]["association"]["state"] == "proposed"
    assert len(payload["source"]["association"]["candidates"]) == 2
    assert payload["capabilities"]["reconcile"] is False and payload["capabilities"]["confirm_association"] is True

    # Ambiguity resolves only through explicit confirmation of one candidate.
    confirmed = client.put(
        f"/api/command-center/sources/{source_id}/association",
        json={"account": "Globex", "opportunity_slug": "pilot", "reason": "Confirmed by SE"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["association"]["state"] == "associated"
    assert confirmed.json()["association"]["method"] == "explicit"

    revoked = client.post("/api/command-center/gmail/connection/revoke", json={})
    assert revoked.status_code == 200 and revoked.json()["sources_marked"] == 2
    assert SENTINEL not in revoked.text
    assert client.get(f"/api/command-center/sources/{source_id}/review").json()["capabilities"]["reconcile"] is False
    assert client.post("/api/command-center/gmail/retrievals", json={"message_ids": [GLOBEX]}).status_code == 409


# ------------------------------------------------------------- evidence → Actions

from opportunity_state import ActionStatus

from eval.tests.test_command_center_operations_service import (
    ACCOUNT,
    OPP,
    Harness,
    _recommendation,
)


def _gmail_for(h: Harness) -> tuple[GmailIntakeService, SyntheticGmailTransport]:
    transport = SyntheticGmailTransport.from_fixture(FIXTURE)
    service = GmailIntakeService(
        transport=transport, adapter=GmailSourceAdapter(), ledger=h.ledger, job_service=h.jobs,
        local_opportunities=lambda: [{"account": ACCOUNT, "opportunity_slug": OPP, "opportunity_name": "Synthetic"}],
    )
    return service, transport


@pytest.mark.asyncio
async def test_gmail_ask_flows_through_ledger_to_actions_and_completion_is_only_suggested(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.bootstrap_overview()
    gmail, _ = _gmail_for(h)
    await gmail.check_access()
    row = _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK]))[ACME_ASK]
    source_id = row["source_id"]
    assert row["association_state"] == "proposed"

    # Proposed is not enough: reconciliation is refused until a human confirms.
    with pytest.raises(CommandCenterOperationsError) as exc:
        await h.ops.start_reconciliation(source_id, base_version_id="0" * 32, base_revision=1)
    assert exc.value.code in {"not_associated", "unassociated", "association_required"}, exc.value.code
    await h.confirm(source_id)

    h.set_recs(source_id, 1, lambda eid: [
        _recommendation(eid, key="soc2", action="Send SOC 2 report and addendum", owner="Sam Rivera (Airbyte)", due="2026-09-26"),
    ])
    job = await h.reconcile(source_id)
    assert job["ok"] is True, job
    request = h.ops._executor.requests[0]
    assert b"SYNTHETIC-BODY-ACME-ASK" in request.transcripts[0].content  # private runtime saw the body
    actions = h.actions()
    assert len(actions) == 1 and actions[0]["status"] == "open"
    assert actions[0]["origin"]["source_id"] == source_id
    action_id = actions[0]["action_id"]

    # The reply suggests completion; the Action stays open until a human transitions it.
    reply = _by_id(await _retrieve(gmail, h.jobs, [ACME_REPLY]))[ACME_REPLY]
    reply_source = h.ledger.get_source(reply["source_id"])
    assert reply_source["association"]["candidates"][0]["method"] == "thread_mapping"
    await h.confirm(reply["source_id"])
    h.set_recs(reply["source_id"], 1, lambda eid: [
        _recommendation(eid, key="soc2", action="Send SOC 2 report and addendum", owner="Sam Rivera (Airbyte)",
                        due="2026-09-26", status=ActionStatus.DONE),
    ])
    job = await h.reconcile(reply["source_id"])
    assert job["ok"] is True, job
    assert job["completion_suggestions"] == [action_id] and job["actions_created"] == []
    assert h.ops.get_action(action_id)["status"] == "open"
    done = h.ops.transition_action(action_id, to_status="completed", reason="SE confirmed the diagram was sent")
    assert done["status"] == "completed"

    h.assert_no_content()
    blob = json.dumps({k: v for k, v in h.jobs.jobs.items()}, default=str) + json.dumps(h.ledger.list_sources())
    assert SENTINEL not in blob


@pytest.mark.asyncio
async def test_gmail_evidence_confirmed_to_one_opportunity_never_reaches_another(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    await h.bootstrap_overview()
    gmail, _ = _gmail_for(h)
    await gmail.check_access()
    row = _by_id(await _retrieve(gmail, h.jobs, [GLOBEX]))[GLOBEX]
    assert row["association_state"] == "unassociated"  # nothing local matches globex.example
    await h.confirm(row["source_id"])
    h.set_recs(row["source_id"], 1, lambda eid: [
        _recommendation(eid, key="cdc-answer", action="Answer the CDC question", owner="Airbyte SE", due="2026-09-29"),
    ])
    assert (await h.reconcile(row["source_id"]))["ok"] is True
    assert h.ops.list_actions(ACCOUNT, OPP)["total"] == 1
    assert h.ops.list_actions("Globex", "pilot")["total"] == 0
    assert h.ops.list_actions(ACCOUNT, "other-opportunity")["total"] == 0
