"""Forget uses only synthetic Gmail fixtures and the deterministic Overview executor."""
from __future__ import annotations

import json

import pytest

from opportunity_state import GenerationProvenance, OpportunityStateCandidate, evidence_manifest_hash
from services.command_center_read_service import CommandCenterReadService
from services.evidence_ledger_service import EvidenceLedgerError
from services.gmail_forget_service import GmailForgetService
from services.command_center_operations_service import evidence_id_for
from services.opportunity_state_executor import FakeCanonicalStateExecutor

from eval.tests.test_command_center_operations_service import ACCOUNT, OPP, Harness, _recommendation
from eval.tests.test_gmail_intake import ACME_ASK, ACME_REPLY, WINDOW, _by_id, _client, _gmail_for, _retrieve


def _forget(h: Harness, gmail) -> GmailForgetService:
    return GmailForgetService(ledger=h.ledger, gmail=gmail, operations=h.ops, state=h.state)


@pytest.mark.asyncio
async def test_forget_removes_all_revisions_and_index_blocks_reimport_and_keeps_other_message(tmp_path) -> None:
    h = Harness(tmp_path)
    gmail, transport = _gmail_for(h)
    await gmail.check_access()
    first = _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK, ACME_REPLY]))
    source_id = first[ACME_ASK]["source_id"]
    other_id = first[ACME_REPLY]["source_id"]
    transport.edit_body(ACME_ASK, "SYNTHETIC-BODY-ACME-ASK edited")
    assert _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK]))[ACME_ASK]["revision"] == 2
    content_dir = h.customers / ".command-center" / "content" / source_id
    assert len(list(content_dir.glob("*.json"))) == 2

    receipt = _forget(h, gmail).forget(source_id)
    assert receipt["status"] == "complete"
    assert set(h.ledger.forget_receipt(source_id)) == {"source_id", "workspace_id", "requested_at", "status", "overview_status"}
    assert not content_dir.exists()
    assert not h.ledger._source_path(source_id).exists()
    assert source_id not in gmail._read_index()
    assert other_id in gmail._read_index()
    listed = await gmail.list_threads(participants=["acme.example"], **WINDOW)
    assert ACME_ASK not in {row["message_id"] for thread in listed["threads"] for row in thread["messages"]}
    assert ACME_REPLY in {row["message_id"] for thread in listed["threads"] for row in thread["messages"]}
    assert h.ledger.read_content(other_id, revision=1)["content"]["body_text"]
    assert source_id not in {row["source_id"] for row in h.ledger.list_sources()["sources"]}
    with pytest.raises(EvidenceLedgerError) as exc:
        h.ledger.read_content(source_id, revision=1)
    assert exc.value.code == "source_forgotten"
    with pytest.raises(EvidenceLedgerError) as exc:
        h.ledger.get_source(source_id)
    assert exc.value.code == "source_forgotten"
    with pytest.raises(Exception) as exc:
        await gmail.start_retrieval([ACME_ASK])
    assert exc.value.code == "source_forgotten"
    assert _forget(h, gmail).forget(source_id) == h.ledger.forget_receipt(source_id)
    assert "SYNTHETIC-BODY" not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_forget_failure_resumes_after_immediate_barrier(tmp_path, monkeypatch) -> None:
    h = Harness(tmp_path)
    gmail, transport = _gmail_for(h)
    await gmail.check_access()
    source_id = _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK]))[ACME_ASK]["source_id"]
    transport.edit_body(ACME_ASK, "SYNTHETIC-BODY-ACME-ASK second revision")
    assert _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK]))[ACME_ASK]["revision"] == 2
    service = _forget(h, gmail)
    real = h.ledger.purge_forgotten_source
    calls = 0

    def interrupted(source):
        nonlocal calls
        calls += 1
        if calls == 1:
            next((h.customers / ".command-center" / "content" / source).glob("*.json")).unlink()
            raise OSError("synthetic interruption")
        real(source)

    monkeypatch.setattr(h.ledger, "purge_forgotten_source", interrupted)
    with pytest.raises(OSError):
        service.forget(source_id)
    assert h.ledger.forget_receipt(source_id)["status"] == "pending"
    assert source_id not in gmail._read_index()
    with pytest.raises(EvidenceLedgerError) as exc:
        h.ledger.read_content(source_id, revision=1)
    assert exc.value.code == "source_forgotten"
    assert service.forget(source_id)["status"] == "complete"
    assert not (h.customers / ".command-center" / "content" / source_id).exists()


def test_forget_http_receipt_and_retry_surface_uses_synthetic_mail(tmp_path) -> None:
    client, _transport, ledger = _client(tmp_path)
    app = client.app
    app.state.gmail_forget_service = GmailForgetService(
        ledger=ledger, gmail=app.state.gmail_intake_service,
        operations=app.state.command_center_operations_service,
        state=app.state.command_center_operations_service._state_service,
    )
    assert client.post("/api/command-center/gmail/connection/check", json={}).status_code == 200
    started = client.post("/api/command-center/gmail/retrievals", json={"message_ids": [ACME_ASK]})
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(300):
        job = client.get(f"/api/command-center/gmail/retrievals/{job_id}").json()
        if job["status"] != "running":
            break
    source_id = job["results"][0]["source_id"]
    forgotten = client.post(f"/api/command-center/sources/{source_id}/forget")
    assert forgotten.status_code == 200 and forgotten.json()["status"] == "complete"
    assert client.get(f"/api/command-center/sources/{source_id}/review").status_code == 410
    assert client.get(f"/api/command-center/sources/{source_id}/forget").json()["status"] == "complete"
    assert client.post(f"/api/command-center/sources/{source_id}/forget").status_code == 200
    assert client.post("/api/command-center/gmail/retrievals", json={"message_ids": [ACME_ASK]}).status_code == 410


@pytest.mark.asyncio
async def test_forget_withholds_old_overviews_and_preserves_later_human_edit(tmp_path) -> None:
    h = Harness(tmp_path)
    await h.bootstrap_overview()
    gmail, _ = _gmail_for(h)
    await gmail.check_access()
    source_id = _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK]))[ACME_ASK]["source_id"]
    await h.confirm(source_id)
    h.set_recs(source_id, 1, lambda eid: [
        _recommendation(eid, key="gmail-ask", action="Send synthetic answer", owner="Airbyte SE", due="2026-09-30"),
    ])
    assert (await h.reconcile(source_id))["ok"]
    derived = h.actions()[0]["action_id"]
    assert h.changes()

    previous = h.state.read_current(ACCOUNT, OPP)
    payload = previous.state.model_dump(mode="json")
    payload["brief"]["immediate_priority"]["value"] = "Human edited priority from independent evidence"
    edited = h.state.promote_update(
        identity=previous.identity,
        expected_parent_version_id=previous.version_id, expected_parent_revision=previous.revision,
        evidence_manifest=list(previous.evidence_manifest),
        expected_manifest_hash=evidence_manifest_hash(list(previous.evidence_manifest)),
        provenance=GenerationProvenance(updater_version="human-v1", model="none", runtime="human_edit", cli_version="n/a"),
        candidate=OpportunityStateCandidate.model_validate(payload),
    )
    assert edited.revision == 3
    receipt = _forget(h, gmail).forget(source_id)
    assert receipt["overview_status"] == "repaired"
    current = h.state.read_current(ACCOUNT, OPP)
    assert current.revision == 4
    assert current.state.brief.immediate_priority.value == "Human edited priority from independent evidence"
    assert "gmail-ask" not in {item.key for item in current.state.recommended_actions}
    assert all(not entry.source_id.startswith(source_id) for entry in current.evidence_manifest)
    assert [version.revision for version in h.state.read_history(ACCOUNT, OPP)] == [1, 4]
    assert h.state._read_history_unfiltered(ACCOUNT, OPP)[2].state.brief.immediate_priority.value == payload["brief"]["immediate_priority"]["value"]
    assert h.ops.list_actions(ACCOUNT, OPP, include_retracted=True)["total"] == 0
    assert h.ops.list_changes(ACCOUNT, OPP)["total"] == 0
    reads = CommandCenterReadService(customers_dir=h.customers, ledger=h.ledger, operations=h.ops, state_service=h.state)
    assert reads.actions(include_retracted=True)["total"] == 0
    assert reads.changes()["total"] == 0
    with pytest.raises(Exception) as exc:
        h.ops.get_action(derived)
    assert exc.value.code == "source_forgotten"


@pytest.mark.asyncio
async def test_forget_keeps_another_messages_overview_action_and_changes(tmp_path) -> None:
    h = Harness(tmp_path)
    await h.bootstrap_overview()
    gmail, _ = _gmail_for(h)
    await gmail.check_access()
    rows = _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK, ACME_REPLY]))
    first, second = rows[ACME_ASK]["source_id"], rows[ACME_REPLY]["source_id"]
    await h.confirm(first)
    await h.confirm(second)
    h.set_recs(first, 1, lambda eid: [
        _recommendation(eid, key="ask-one", action="Send first synthetic answer", owner="Airbyte SE", due="2026-09-30"),
    ])
    assert (await h.reconcile(first))["ok"]
    previous = h.state.read_current(ACCOUNT, OPP)
    eid = evidence_id_for(second, 1)
    candidate = previous.state.model_copy(update={
        "recommended_actions": [
            *previous.state.recommended_actions,
            _recommendation(eid, key="ask-two", action="Send second synthetic answer", owner="Airbyte SE", due="2026-10-01"),
        ],
    })
    h.ops._executor = FakeCanonicalStateExecutor(candidate, cli_version="2.1.272")
    assert (await h.reconcile(second))["ok"]
    assert h.ops.list_actions(ACCOUNT, OPP)["total"] == 2
    assert _forget(h, gmail).forget(first)["overview_status"] == "withheld"
    # The later model saw the first message in its base. Keep its unrelated
    # work on disk, but do not assume that its output is independent.
    with pytest.raises(Exception) as exc:
        h.state.read_current(ACCOUNT, OPP)
    assert exc.value.code == "source_forgotten"
    current = h.state._read_history_unfiltered(ACCOUNT, OPP)[-1]
    assert {item.key for item in current.state.recommended_actions} >= {"ask-two"}
    assert "ask-one" in {item.key for item in current.state.recommended_actions}
    assert any(entry.source_id.startswith(second) for entry in current.evidence_manifest)
    assert any(entry.source_id.startswith(first) for entry in current.evidence_manifest)
    assert h.ops.list_actions(ACCOUNT, OPP)["total"] == 1
    assert h.ops.list_actions(ACCOUNT, OPP)["actions"][0]["origin"]["source_id"] == second
    assert any(item["source"] and item["source"]["source_id"] == second for item in h.ops.list_changes(ACCOUNT, OPP)["changes"])
    assert h.ledger.read_content(second, revision=1)


@pytest.mark.asyncio
async def test_later_model_copy_into_disjoint_overview_field_stays_withheld(tmp_path) -> None:
    h = Harness(tmp_path)
    await h.bootstrap_overview()
    gmail, _ = _gmail_for(h)
    await gmail.check_access()
    source_id = _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK]))[ACME_ASK]["source_id"]
    await h.confirm(source_id)
    marker = "SYNTHETIC-FORGOTTEN-GMAIL-MARKER-92741"
    h.set_recs(source_id, 1, lambda eid: [
        _recommendation(eid, key="gmail-ask", action=marker, owner="Airbyte SE", due="2026-09-30"),
    ])
    assert (await h.reconcile(source_id))["ok"]
    previous = h.state.read_current(ACCOUNT, OPP)
    payload = previous.state.model_dump(mode="json")
    payload["brief"]["immediate_priority"]["value"] = f"Later model repeated {marker}"
    copied = h.state.promote_update(
        identity=previous.identity,
        expected_parent_version_id=previous.version_id, expected_parent_revision=previous.revision,
        evidence_manifest=list(previous.evidence_manifest),
        expected_manifest_hash=evidence_manifest_hash(list(previous.evidence_manifest)),
        provenance=GenerationProvenance(updater_version="model-v2", model="synthetic", runtime="model", cli_version="n/a"),
        candidate=OpportunityStateCandidate.model_validate(payload),
    )
    human_payload = copied.state.model_dump(mode="json")
    human_payload["brief"]["customer_objective"]["value"] = "Human edit from independent evidence"
    edited = h.state.promote_update(
        identity=copied.identity,
        expected_parent_version_id=copied.version_id, expected_parent_revision=copied.revision,
        evidence_manifest=list(copied.evidence_manifest),
        expected_manifest_hash=evidence_manifest_hash(list(copied.evidence_manifest)),
        provenance=GenerationProvenance(updater_version="human-v1", model="none", runtime="human_edit", cli_version="n/a"),
        candidate=OpportunityStateCandidate.model_validate(human_payload),
    )
    receipt = _forget(h, gmail).forget(source_id)
    assert receipt["status"] == "complete"
    assert receipt["overview_status"] == "withheld"
    assert h.ledger.forget_receipt(source_id)["overview_status"] == "withheld"
    assert _forget(h, gmail).forget(source_id) == receipt
    with pytest.raises(Exception) as exc:
        h.state.read_current(ACCOUNT, OPP)
    assert exc.value.code == "source_forgotten"
    assert [version.revision for version in h.state.read_history(ACCOUNT, OPP)] == [1]
    assert h.state._read_history_unfiltered(ACCOUNT, OPP)[-1].version_id == edited.version_id
    assert marker in h.state._read_history_unfiltered(ACCOUNT, OPP)[-1].state.brief.immediate_priority.value
    assert h.state._read_history_unfiltered(ACCOUNT, OPP)[-1].state.brief.customer_objective.value == "Human edit from independent evidence"


@pytest.mark.asyncio
async def test_conflicting_human_edit_is_withheld_and_retained_on_disk(tmp_path) -> None:
    h = Harness(tmp_path)
    await h.bootstrap_overview()
    gmail, _ = _gmail_for(h)
    await gmail.check_access()
    source_id = _by_id(await _retrieve(gmail, h.jobs, [ACME_ASK]))[ACME_ASK]["source_id"]
    await h.confirm(source_id)
    h.set_recs(source_id, 1, lambda eid: [
        _recommendation(eid, key="gmail-ask", action="Send synthetic answer", owner="Airbyte SE", due="2026-09-30"),
    ])
    assert (await h.reconcile(source_id))["ok"]
    previous = h.state.read_current(ACCOUNT, OPP)
    payload = previous.state.model_dump(mode="json")
    payload["recommended_actions"][-1]["action"] = "Human edited the Gmail derived action"
    edited = h.state.promote_update(
        identity=previous.identity,
        expected_parent_version_id=previous.version_id, expected_parent_revision=previous.revision,
        evidence_manifest=list(previous.evidence_manifest),
        expected_manifest_hash=evidence_manifest_hash(list(previous.evidence_manifest)),
        provenance=GenerationProvenance(updater_version="human-v1", model="none", runtime="human_edit", cli_version="n/a"),
        candidate=OpportunityStateCandidate.model_validate(payload),
    )
    assert _forget(h, gmail).forget(source_id)["overview_status"] == "withheld"
    with pytest.raises(Exception) as exc:
        h.state.read_current(ACCOUNT, OPP)
    assert exc.value.code == "source_forgotten"
    assert h.state._read_history_unfiltered(ACCOUNT, OPP)[-1].version_id == edited.version_id
    assert h.state._read_history_unfiltered(ACCOUNT, OPP)[-1].state.recommended_actions[-1].action == "Human edited the Gmail derived action"
