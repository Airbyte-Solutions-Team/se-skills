"""First meeting to Overview and durable Actions, using only synthetic local evidence."""
from __future__ import annotations

import httpx
import pytest

from opportunity_state import EvidenceSourceType
from services.command_center_operations_service import CommandCenterOperationsError, evidence_id_for
from services.command_center_operations_service import CommandCenterOperationsService
from services.opportunity_state_executor import FakeCanonicalStateExecutor
from routes.opportunity_state import router as overview_router
from eval.tests.test_command_center_operations_service import (
    ACCOUNT, OPP, DEFAULT_RECS, FailingExecutor, fixture,
)
from eval.tests.test_command_center_read_service import ReadHarness, OPP2
from eval.tests.test_opportunity_state_create_service import BlockingExecutor, _candidate_for, _wait


def _ready(h: ReadHarness, source_id: str) -> tuple[dict, FakeCanonicalStateExecutor]:
    source = h.ledger.get_source(source_id)
    eid = evidence_id_for(source_id, source["latest_revision"])
    executor = FakeCanonicalStateExecutor(_candidate_for(eid).model_copy(update={"recommended_actions": DEFAULT_RECS(eid)}))
    h.ops._executor = executor
    return source, executor


async def _start(h: ReadHarness, source_id: str, opp: str = OPP) -> dict:
    source = h.ledger.get_source(source_id)
    return await h.ops.start_first_overview(
        source_id, account=ACCOUNT, opp_slug=opp,
        revision=source["latest_revision"], association_sequence=source["association"]["sequence"],
    )


@pytest.mark.asyncio
async def test_import_confirm_create_first_overview_reaches_today_and_portfolio(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://test") as client:
        imported = await client.post("/api/command-center/imports/granola", json={"notes": [fixture("note_synthetic_v1.json")]})
        assert imported.status_code == 201, imported.text
        source_id = imported.json()["results"][0]["source_id"]
        confirmed = await client.put(f"/api/command-center/sources/{source_id}/association", json={
            "account": ACCOUNT, "opportunity_slug": OPP, "reason": "Synthetic association",
        })
        assert confirmed.status_code == 200, confirmed.text
        source, executor = _ready(h, source_id)
        review = (await client.get(f"/api/command-center/sources/{source_id}/review")).json()
        assert review["capabilities"]["create_first_overview"] is True
        assert review["capabilities"]["reconcile"] is False
        assert executor.requests == []  # reading the page never starts analysis

        body = {"account": ACCOUNT, "opportunity_slug": OPP,
                "revision": source["latest_revision"], "association_sequence": source["association"]["sequence"]}
        response = await client.post(f"/api/command-center/sources/{source_id}/create-first-overview", json=body)
        assert response.status_code == 202, response.text
        job = await _wait(h.jobs, response.json()["job_id"])
        assert job["ok"] is True and job["result_revision"] == 1
        assert len(executor.requests) == 1
        assert executor.requests[0].base_state is None
        assert [item.evidence_id for item in executor.requests[0].transcripts] == [evidence_id_for(source_id, 1)]
        version = h.state.read_current(ACCOUNT, OPP)
        assert version.revision == 1 and version.parent_version_id is None
        assert any(e.source_type == EvidenceSourceType.TRANSCRIPT and e.source_id == evidence_id_for(source_id, 1)
                   for e in version.evidence_manifest)
        assert h.ledger.get_source(source_id)["processing"]["status"] == "processed"
        actions = h.actions()
        assert len(actions) == 2 and len(job["actions_created"]) == 2
        assert all(a["origin"]["source_id"] == source_id for a in actions)
        assert any(a["status"] == "open" for a in actions)
        today = (await client.get("/api/command-center/today")).json()
        assert any(item.get("action_id") in {a["action_id"] for a in actions}
                   for item in today["attention"])
        portfolio = (await client.get("/api/command-center/portfolio")).json()
        row = next(row for row in portfolio["opportunities"] if row["opportunity_slug"] == OPP)
        assert row["overview"]["status"] == "current" and row["action_counts"]["open"] == 1
        duplicate = await client.post(f"/api/command-center/sources/{source_id}/create-first-overview", json=body)
        assert duplicate.status_code == 409
        assert len(executor.requests) == 1 and len(h.actions()) == 2
        h.assert_no_content()


@pytest.mark.asyncio
async def test_wrong_or_changed_association_is_rejected_before_analysis(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    source_id = h.import_note("note_synthetic_v1.json")
    source, executor = _ready(h, source_id)
    with pytest.raises(CommandCenterOperationsError) as error:
        await h.ops.start_first_overview(source_id, account=ACCOUNT, opp_slug=OPP,
                                         revision=1, association_sequence=source["association"]["sequence"])
    assert error.value.code == "association_changed"
    await h.confirm(source_id, ACCOUNT, OPP2)
    with pytest.raises(CommandCenterOperationsError) as error:
        await h.ops.start_first_overview(source_id, account=ACCOUNT, opp_slug=OPP,
                                         revision=1, association_sequence=source["association"]["sequence"])
    assert error.value.code == "association_changed"
    assert executor.requests == [] and h.state.read_current(ACCOUNT, OPP) is None


@pytest.mark.asyncio
async def test_association_change_during_analysis_does_not_promote(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    eid = evidence_id_for(source_id, 1)
    blocking = BlockingExecutor(_candidate_for(eid).model_copy(update={"recommended_actions": DEFAULT_RECS(eid)}))
    h.ops._executor = blocking
    started = await _start(h, source_id)
    await blocking.started.wait()
    repeated = await _start(h, source_id)
    assert repeated["reused"] is True and repeated["job_id"] == started["job_id"]
    await h.confirm(source_id, ACCOUNT, OPP2)
    blocking.release.set()
    job = await _wait(h.jobs, started["job_id"])
    assert job["ok"] is False and job["error_code"] == "association_changed"
    assert h.state.read_current(ACCOUNT, OPP) is None and h.state.read_current(ACCOUNT, OPP2) is None
    assert h.actions() == []


@pytest.mark.asyncio
async def test_missing_meeting_content_fails_without_executor_call(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    payload = fixture("note_synthetic_v1.json")
    payload.update(summary_text=None, summary_markdown=None, private_notes_text=None, transcript=[])
    meeting = h.adapter.normalize(payload, connection_id="local-manual")
    source_id = h.ledger.import_meetings([meeting])["results"][0]["source_id"]
    await h.confirm(source_id)
    _, executor = _ready(h, source_id)
    if h.ledger.get_source(source_id)["processing"]["status"] == "queued":
        job = await _wait(h.jobs, (await _start(h, source_id))["job_id"])
        assert job["ok"] is False and job["error_code"] == "content_unusable"
    else:
        assert h.ledger.get_source(source_id)["processing"]["status"] == "awaiting_content"
        assert h.reads.source_review(source_id)["capabilities"]["create_first_overview"] is False
    assert executor.requests == [] and h.state.read_current(ACCOUNT, OPP) is None


@pytest.mark.asyncio
async def test_failed_first_analysis_retries_without_duplicate_action(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    failing = FailingExecutor("timeout")
    h.ops._executor = failing
    job = await _wait(h.jobs, (await _start(h, source_id))["job_id"])
    assert job["ok"] is False and job["error_code"] == "timeout"
    assert h.state.read_current(ACCOUNT, OPP) is None and h.actions() == []
    assert h.ledger.get_source(source_id)["processing"]["retry_eligible"] is True
    _, executor = _ready(h, source_id)
    job = await _wait(h.jobs, (await _start(h, source_id))["job_id"])
    assert job["ok"] is True and len(executor.requests) == 1
    assert len(h.actions()) == 2 and h.state.read_current(ACCOUNT, OPP).revision == 1
    assert [run["status"] for run in h.ops.list_runs(source_id)] == ["failed", "succeeded"]


@pytest.mark.asyncio
async def test_first_promotion_resumes_action_application_without_second_analysis(tmp_path, monkeypatch) -> None:
    h = ReadHarness(tmp_path)
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    _, executor = _ready(h, source_id)
    original_apply = h.ops._apply_observations
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic post-promotion interruption")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(h.ops, "_apply_observations", interrupted)
    failed = await _wait(h.jobs, (await _start(h, source_id))["job_id"])
    assert failed["error_code"] == "apply_incomplete"
    assert h.state.read_current(ACCOUNT, OPP).revision == 1
    assert h.actions() == [] and len(executor.requests) == 1
    review = h.reads.source_review(source_id)
    assert review["pending_action_application"] is True
    assert review["capabilities"]["reconcile"] is True

    h.ops = CommandCenterOperationsService(
        ledger=h.ledger, workspace_service=h.workspace, state_service=h.state,
        job_service=h.jobs, executor=executor,
    )
    resumed = await _wait(h.jobs, (await _start(h, source_id))["job_id"])
    assert resumed["ok"] is True and resumed["resumed"] is True
    assert len(executor.requests) == 1 and len(h.actions()) == 2
    assert len({a["action_id"] for a in h.actions()}) == 2
    assert [change["change_type"] for change in h.changes()].count("overview_revision") == 1
    assert h.ledger.get_source(source_id)["processing"]["status"] == "processed"


@pytest.mark.asyncio
async def test_retry_after_processed_mark_finishes_without_duplicate_actions(tmp_path, monkeypatch) -> None:
    h = ReadHarness(tmp_path)
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    _, executor = _ready(h, source_id)
    original_remove = h.ops._remove_pending
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic marker removal failure")
        return original_remove(*args, **kwargs)

    monkeypatch.setattr(h.ops, "_remove_pending", fail_once)
    failed = await _wait(h.jobs, (await _start(h, source_id))["job_id"])
    assert failed["error_code"] == "apply_incomplete"
    assert h.ledger.get_source(source_id)["processing"]["status"] == "processed"
    action_ids = {a["action_id"] for a in h.actions()}
    assert len(action_ids) == 2
    review = h.reads.source_review(source_id)
    assert review["pending_action_application"] is True
    assert review["capabilities"]["reconcile"] is True
    h.ops = CommandCenterOperationsService(
        ledger=h.ledger, workspace_service=h.workspace, state_service=h.state,
        job_service=h.jobs, executor=executor,
    )
    resumed = await _wait(h.jobs, (await _start(h, source_id))["job_id"])
    assert resumed["ok"] is True and resumed["resumed"] is True
    assert len(executor.requests) == 1 and {a["action_id"] for a in h.actions()} == action_ids


@pytest.mark.asyncio
async def test_first_overview_correction_retires_wrong_head_and_reprocesses_on_right_opportunity(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    h.app.state.opportunity_state_create_service = h.create
    h.app.state.opportunity_state_service = h.state
    h.app.include_router(overview_router)
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    _, first_executor = _ready(h, source_id)
    assert (await _wait(h.jobs, (await _start(h, source_id))["job_id"]))["ok"] is True
    wrong_version = h.state.read_current(ACCOUNT, OPP)
    action_ids = {a["action_id"] for a in h.actions()}
    corrected = await h.confirm(source_id, ACCOUNT, OPP2)
    assert corrected["overview_reverted"]["status"] == "retired"
    assert set(corrected["retracted_actions"]) == action_ids
    assert h.state.inspect_current(ACCOUNT, OPP)["status"] == "retired"
    assert h.state.read_version(ACCOUNT, OPP, 1).version_id == wrong_version.version_id
    assert h.client.get(f"/api/accounts/{ACCOUNT}/opportunities/{OPP}/overview/state").status_code == 410
    assert h.ops.list_actions(ACCOUNT, OPP)["actions"] == []
    assert all(a["retracted"] for a in h.ops.list_actions(ACCOUNT, OPP, include_retracted=True)["actions"])
    assert h.ops.list_changes(ACCOUNT, OPP)["changes"][0]["change_type"] == "overview_retired"
    assert not any(c["actor"] == "analysis" for c in h.ops.list_changes(ACCOUNT, OPP)["changes"])
    assert next(row for row in h.reads.portfolio()["opportunities"]
                if row["opportunity_slug"] == OPP)["overview"]["status"] == "retired"
    assert h.reads.actions()["actions"] == []
    assert len(first_executor.requests) == 1

    _, second_executor = _ready(h, source_id)
    assert h.reads.source_review(source_id)["capabilities"]["create_first_overview"] is True
    completed = await _wait(h.jobs, (await _start(h, source_id, OPP2))["job_id"])
    assert completed["ok"] is True and completed["result_revision"] == 1
    assert len(second_executor.requests) == 1
    correct_ids = {a["action_id"] for a in h.ops.list_actions(ACCOUNT, OPP2)["actions"]}
    assert len(correct_ids) == 2 and correct_ids.isdisjoint(action_ids)
    assert {a["action_id"] for a in h.reads.actions()["actions"]} == correct_ids
    assert any(item.get("action_id") in correct_ids for item in h.reads.today()["attention"])
    rows = {row["opportunity_slug"]: row for row in h.reads.portfolio()["opportunities"]}
    assert rows[OPP]["overview"]["status"] == "retired"
    assert rows[OPP]["action_counts"]["open"] == 0
    assert rows[OPP2]["overview"]["status"] == "current"
    assert rows[OPP2]["action_counts"]["open"] == 1
    repeated = await h.confirm(source_id, ACCOUNT, OPP2)
    assert repeated["retracted_actions"] == []
    assert len(h.ops.list_actions(ACCOUNT, OPP2)["actions"]) == 2
    assert len([c for c in h.ops.list_changes(ACCOUNT, OPP)["changes"] if c["change_type"] == "overview_retired"]) == 1
    assert len(second_executor.requests) == 1
    h.assert_no_content()


@pytest.mark.asyncio
async def test_first_overview_correction_resumes_after_ledger_write(tmp_path, monkeypatch) -> None:
    h = ReadHarness(tmp_path)
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    _ready(h, source_id)
    assert (await _wait(h.jobs, (await _start(h, source_id))["job_id"]))["ok"] is True
    original = h.ops._retract_source
    calls = 0

    def interrupt(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic interruption after ledger write")
        return original(*args, **kwargs)

    monkeypatch.setattr(h.ops, "_retract_source", interrupt)
    with pytest.raises(OSError):
        await h.confirm(source_id, ACCOUNT, OPP2)
    assert h.state.inspect_current(ACCOUNT, OPP)["status"] == "retired"
    assert h.reads.actions()["actions"] == []
    old_action = h.ops.list_actions(ACCOUNT, OPP, include_retracted=True)["actions"][0]
    assert h.reads.action(old_action["action_id"])["allowed_transitions"] == []
    with pytest.raises(CommandCenterOperationsError) as error:
        h.ops.transition_action(old_action["action_id"], to_status="completed", reason="too late")
    assert error.value.code == "retracted"
    resumed = await h.confirm(source_id, ACCOUNT, OPP2)
    assert len(resumed["retracted_actions"]) == 2
    assert h.ops.list_actions(ACCOUNT, OPP)["actions"] == []
    assert len([c for c in h.ops.list_changes(ACCOUNT, OPP)["changes"] if c["change_type"] == "overview_retired"]) == 1


@pytest.mark.asyncio
async def test_later_mixed_revision_and_human_action_edit_block_first_overview_correction(tmp_path) -> None:
    for mutate in ("revision", "mixed", "action"):
        h = ReadHarness(tmp_path / mutate)
        source_id = h.import_note("note_synthetic_v1.json")
        await h.confirm(source_id)
        _ready(h, source_id)
        assert (await _wait(h.jobs, (await _start(h, source_id))["job_id"]))["ok"] is True
        before = h.ledger.get_source(source_id)["association"]
        if mutate == "action":
            action = next(a for a in h.actions() if a["status"] == "open")
            h.ops.transition_action(action["action_id"], to_status="completed", reason="Synthetic manual edit")
        elif mutate == "mixed":
            second = h.import_variant("not_SYNTH000000002")
            await h.confirm(second)
            _ready(h, second)
            current = h.state.read_current(ACCOUNT, OPP)
            job = await h.ops.start_reconciliation(
                second, base_version_id=current.version_id, base_revision=current.revision,
            )
            assert (await _wait(h.jobs, job["job_id"]))["ok"] is True
        else:
            current = h.state.read_current(ACCOUNT, OPP)
            from opportunity_state import GenerationProvenance
            h.state.promote_update(
                identity=current.identity, expected_parent_version_id=current.version_id,
                expected_parent_revision=current.revision, evidence_manifest=list(current.evidence_manifest),
                expected_manifest_hash=current.evidence_manifest_hash,
                provenance=GenerationProvenance(updater_version="test", model="none", runtime="test", cli_version="test"),
                candidate=current.state,
            )
        with pytest.raises(CommandCenterOperationsError) as error:
            await h.confirm(source_id, ACCOUNT, OPP2)
        assert error.value.code == "overview_unrecoverable"
        assert h.ledger.get_source(source_id)["association"] == before
        assert h.state.inspect_current(ACCOUNT, OPP)["status"] == "current"
        assert h.state.retirement_info(ACCOUNT, OPP) is None


@pytest.mark.asyncio
async def test_first_overviews_are_isolated_for_two_opportunities_same_account(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    first = h.import_note("note_synthetic_v1.json")
    second_payload = fixture("note_synthetic_v1.json")
    second_payload["id"] = "not_SYNTH000000002"
    second = h.ledger.import_meetings([h.adapter.normalize(second_payload, connection_id="local-manual")])["results"][0]["source_id"]
    await h.confirm(first, ACCOUNT, OPP)
    await h.confirm(second, ACCOUNT, OPP2)
    _ready(h, first)
    assert (await _wait(h.jobs, (await _start(h, first))["job_id"]))["ok"] is True
    _ready(h, second)
    assert (await _wait(h.jobs, (await _start(h, second, OPP2))["job_id"]))["ok"] is True
    rows = {row["opportunity_slug"]: row for row in h.reads.portfolio()["opportunities"]}
    assert set(rows) == {OPP, OPP2}
    assert all(row["overview"]["status"] == "current" for row in rows.values())
    assert len(h.ops.list_actions(ACCOUNT, OPP)["actions"]) == 2
    assert len(h.ops.list_actions(ACCOUNT, OPP2)["actions"]) == 2
    assert h.state.read_current(ACCOUNT, OPP).version_id != h.state.read_current(ACCOUNT, OPP2).version_id
