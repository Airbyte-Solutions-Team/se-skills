from __future__ import annotations

import asyncio
import json

import pytest

from opportunity_state import OpportunityStateCandidate
from services.opportunity_state_executor import CanonicalStateExecutionResult, FakeCanonicalStateExecutor
from services.opportunity_state_executor import CanonicalStateExecutionError
from services.opportunity_state_update_service import OpportunityStateUpdateError, OpportunityStateUpdateService
from eval.tests.test_opportunity_state_create_service import (
    BlockingExecutor,
    _candidate_for,
    _harness,
    _wait,
)


async def _created_harness(tmp_path):
    create, state, jobs, _executor, first_path, first_id = _harness(tmp_path)
    started = await create.start_create("Acme", "synthetic-opportunity", [first_id])
    assert (await _wait(jobs, started["job_id"]))["ok"] is True
    update = OpportunityStateUpdateService(
        workspace_service=create._workspace_service,
        transcription_service=create._transcription_service,
        state_service=state,
        job_service=jobs,
        executor=FakeCanonicalStateExecutor(_candidate_for(first_id), cli_version="2.1.272"),
    )
    return create, update, state, jobs, first_path, first_id


def _add_transcript(first_path, name="Acme-09.18.26.txt", content="new synthetic update evidence"):
    path = first_path.with_name(name)
    path.write_text(content, encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_freshness_is_server_calculated_and_discloses_new_changed_missing(tmp_path) -> None:
    _create, update, state, _jobs, first_path, first_id = await _created_harness(tmp_path)
    new_path = _add_transcript(first_path)
    readiness = await update.get_readiness("Acme", "synthetic-opportunity")
    assert readiness["metadata_changed"] is False
    assert readiness["new_source_count"] == 1
    assert readiness["changed_source_count"] == 0
    assert readiness["missing_source_count"] == 0
    assert readiness["inherited_sources"][0]["id"] == first_id
    assert "content" not in json.dumps(readiness).lower()
    assert str(first_path) not in json.dumps(readiness)

    first_path.write_text("changed bytes", encoding="utf-8")
    changed = await update.get_readiness("Acme", "synthetic-opportunity")
    assert changed["changed_source_count"] == 1
    first_path.unlink()
    missing = await update.get_readiness("Acme", "synthetic-opportunity")
    assert missing["missing_source_count"] == 1
    assert state.read_current("Acme", "synthetic-opportunity").revision == 1
    assert new_path.exists()


@pytest.mark.asyncio
async def test_update_requires_explicit_delta_and_rejects_unchanged_selection(tmp_path) -> None:
    _create, update, state, _jobs, _path, first_id = await _created_harness(tmp_path)
    current = state.read_current("Acme", "synthetic-opportunity")
    with pytest.raises(OpportunityStateUpdateError) as no_op:
        await update.start_update(
            "Acme", "synthetic-opportunity",
            base_version_id=current.version_id, base_revision=current.revision, transcript_ids=[],
        )
    assert no_op.value.code == "no_op"
    with pytest.raises(OpportunityStateUpdateError) as unchanged:
        await update.start_update(
            "Acme", "synthetic-opportunity",
            base_version_id=current.version_id, base_revision=current.revision,
            transcript_ids=[first_id],
        )
    assert unchanged.value.code == "invalid_selection"
    assert state.read_current("Acme", "synthetic-opportunity").revision == 1


@pytest.mark.asyncio
async def test_metadata_change_alone_is_a_valid_delta(tmp_path) -> None:
    create, update, state, jobs, _path, _first_id = await _created_harness(tmp_path)
    create._workspace_service.opportunity["stage"] = "Negotiation"
    readiness = await update.get_readiness("Acme", "synthetic-opportunity")
    assert readiness["metadata_changed"] is True
    parent = state.read_current("Acme", "synthetic-opportunity")
    started = await update.start_update(
        "Acme", "synthetic-opportunity",
        base_version_id=parent.version_id, base_revision=1, transcript_ids=[],
    )
    assert (await _wait(jobs, started["job_id"]))["ok"] is True
    child = state.read_current("Acme", "synthetic-opportunity")
    assert child.revision == 2
    assert child.change_set.metadata_changed is True
    assert child.change_set.evidence_sources == []
    assert update._executor.requests[0].transcripts == []


@pytest.mark.asyncio
async def test_successful_update_preserves_parent_and_cumulative_manifest(tmp_path) -> None:
    _create, update, state, jobs, first_path, first_id = await _created_harness(tmp_path)
    _add_transcript(first_path)
    items = update._transcription_service.list_evidence_transcripts("Acme")
    second_id = next(item["id"] for item in items if item["id"] != first_id)
    next_candidate = _candidate_for(second_id)
    next_candidate.brief.current_status.value = "Timeline tightened and blocker discovered"
    update._executor = FakeCanonicalStateExecutor(next_candidate, cli_version="2.1.272")
    parent = state.read_current("Acme", "synthetic-opportunity")
    started = await update.start_update(
        "Acme", "synthetic-opportunity",
        base_version_id=parent.version_id, base_revision=parent.revision,
        transcript_ids=[second_id],
    )
    job = await _wait(jobs, started["job_id"])
    assert job["ok"] is True
    child = state.read_current("Acme", "synthetic-opportunity")
    assert child.revision == 2
    assert child.parent_version_id == parent.version_id
    assert child.parent_revision == 1
    assert child.change_set.child_version_id == child.version_id
    assert {item.source_id for item in child.evidence_manifest} == {
        "opportunity-metadata-v1", first_id, second_id,
    }
    history = state.read_history("Acme", "synthetic-opportunity")
    assert history == [parent, child]
    assert history[0].evidence_manifest_hash == parent.evidence_manifest_hash
    assert any(item.key == "current_status" for item in child.change_set.brief)
    request = update._executor.requests[0]
    assert request.base_state == parent.state
    assert [item.evidence_id for item in request.transcripts] == [second_id]
    assert first_id not in [item.evidence_id for item in request.transcripts]

    repeated = await update.start_update(
        "Acme", "synthetic-opportunity",
        base_version_id=parent.version_id, base_revision=parent.revision,
        transcript_ids=[second_id],
    )
    assert repeated == {"job_id": started["job_id"], "reused": True}
    assert state.read_current("Acme", "synthetic-opportunity").revision == 2


class MutatingUpdateExecutor:
    def __init__(self, candidate: OpportunityStateCandidate, path) -> None:
        self.candidate = candidate
        self.path = path

    async def execute(self, request):
        self.path.write_text("mutated during update", encoding="utf-8")
        return CanonicalStateExecutionResult(
            candidate=self.candidate, model="mutating", cli_version="2.1.272"
        )


@pytest.mark.asyncio
async def test_mutation_during_update_and_stale_base_leave_parent_current(tmp_path) -> None:
    _create, update, state, jobs, first_path, first_id = await _created_harness(tmp_path)
    second_path = _add_transcript(first_path)
    second_id = next(
        item["id"] for item in update._transcription_service.list_evidence_transcripts("Acme")
        if item["id"] != first_id
    )
    parent = state.read_current("Acme", "synthetic-opportunity")
    update._executor = MutatingUpdateExecutor(_candidate_for(second_id), second_path)
    started = await update.start_update(
        "Acme", "synthetic-opportunity",
        base_version_id=parent.version_id, base_revision=1, transcript_ids=[second_id],
    )
    job = await _wait(jobs, started["job_id"])
    assert job["error_code"] == "evidence_changed"
    assert state.read_current("Acme", "synthetic-opportunity") == parent
    assert len(state.read_history("Acme", "synthetic-opportunity")) == 1

    with pytest.raises(OpportunityStateUpdateError) as stale:
        await update.start_update(
            "Acme", "synthetic-opportunity",
            base_version_id="f" * 32, base_revision=1, transcript_ids=[second_id],
        )
    assert stale.value.code == "stale_base"


@pytest.mark.asyncio
async def test_create_and_update_share_one_active_job_gate(tmp_path) -> None:
    create, update, state, jobs, first_path, first_id = await _created_harness(tmp_path)
    _add_transcript(first_path)
    second_id = next(
        item["id"] for item in update._transcription_service.list_evidence_transcripts("Acme")
        if item["id"] != first_id
    )
    blocking = BlockingExecutor(_candidate_for(second_id))
    update._executor = blocking
    parent = state.read_current("Acme", "synthetic-opportunity")
    started = await update.start_update(
        "Acme", "synthetic-opportunity",
        base_version_id=parent.version_id, base_revision=1, transcript_ids=[second_id],
    )
    await blocking.started.wait()
    third_path = _add_transcript(first_path, "Acme-09.19.26.txt", "different authorized delta")
    third_id = next(
        item["id"] for item in update._transcription_service.list_evidence_transcripts("Acme")
        if item["id"] not in {first_id, second_id}
    )
    with pytest.raises(OpportunityStateUpdateError) as update_conflict:
        await update.start_update(
            "Acme", "synthetic-opportunity",
            base_version_id=parent.version_id, base_revision=1, transcript_ids=[third_id],
        )
    assert update_conflict.value.code == "update_in_progress"
    assert third_path.exists()
    with pytest.raises(Exception) as conflict:
        await create.start_create("Acme", "synthetic-opportunity", [first_id])
    assert getattr(conflict.value, "code", None) in {"already_created", "create_in_progress"}
    blocking.release.set()
    assert (await _wait(jobs, started["job_id"]))["ok"] is True


class FailingUpdateExecutor:
    async def execute(self, request):
        raise CanonicalStateExecutionError("invalid_model_output", "Safe synthetic model failure.")


@pytest.mark.asyncio
async def test_failed_model_output_leaves_pointer_and_history_unchanged(tmp_path) -> None:
    _create, update, state, jobs, first_path, first_id = await _created_harness(tmp_path)
    _add_transcript(first_path)
    second_id = next(
        item["id"] for item in update._transcription_service.list_evidence_transcripts("Acme")
        if item["id"] != first_id
    )
    parent = state.read_current("Acme", "synthetic-opportunity")
    update._executor = FailingUpdateExecutor()
    started = await update.start_update(
        "Acme", "synthetic-opportunity",
        base_version_id=parent.version_id, base_revision=1, transcript_ids=[second_id],
    )
    job = await _wait(jobs, started["job_id"])
    assert job["error_code"] == "invalid_model_output"
    assert state.read_current("Acme", "synthetic-opportunity") == parent
    assert state.read_history("Acme", "synthetic-opportunity") == [parent]
    persisted = (tmp_path / ".state" / "jobs.json").read_text(encoding="utf-8")
    assert "new synthetic update evidence" not in persisted
    assert "stdout" not in job and "stderr" not in job


def test_interrupted_update_recovers_without_raw_output(tmp_path) -> None:
    _create, _update, _state, jobs, _path, _id = asyncio.run(_created_harness(tmp_path))
    jobs.jobs = {
        "update1": {
            "kind": "opportunity_state_update", "status": "running", "ok": None,
            "account": "Acme", "opp_slug": "synthetic-opportunity", "started_at": 1.0,
            "base_version_id": "a" * 32, "base_revision": 1,
        }
    }
    asyncio.run(jobs.save_snapshot("update1"))
    restarted = type(jobs)(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    recovered = restarted.get_job("update1")
    assert recovered["status"] == "error"
    assert recovered["error_code"] == "interrupted"
    assert "stdout" not in recovered and "stderr" not in recovered
