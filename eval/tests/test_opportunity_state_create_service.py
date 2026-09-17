from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from opportunity_state import OpportunityStateCandidate
from services.job_service import JobService
from services.opportunity_state_create_service import (
    OpportunityStateCreateError,
    OpportunityStateCreateService,
)
from services.opportunity_state_executor import (
    CanonicalStateExecutionError,
    CanonicalStateExecutionResult,
    FakeCanonicalStateExecutor,
)
from services.opportunity_state_service import OpportunityStateService
from services.transcription_service import TranscriptionService
from webapp.config import _safe
from eval.tests.opportunity_state_helpers import TRANSCRIPT_ID, candidate


class FakeWorkspace:
    def __init__(self) -> None:
        self.opportunity = {
            "name": "Synthetic Opportunity",
            "slug": "synthetic-opportunity",
            "stage": "Tech Eval",
            "stage_num": "S3",
            "amount": 50000,
            "close_date": "2026-12-31",
            "type": "New Business",
            "is_closed": False,
            "ae": "Synthetic AE",
            "sfdc_url": None,
            "metadata_source": "account_service",
            "metadata_complete": True,
        }

    async def resolve_identity(self, account: str, opp_slug: str):
        if account != "Acme" or opp_slug != "synthetic-opportunity":
            raise AssertionError("unexpected identity")
        return {
            "safe_account": account,
            "safe_opp": opp_slug,
            "account": {"name": account},
            "opportunity": dict(self.opportunity),
            "opportunity_outputs": [],
        }


def _candidate_for(evidence_id: str) -> OpportunityStateCandidate:
    payload = candidate().model_dump(mode="json")
    raw = json.dumps(payload).replace(TRANSCRIPT_ID, evidence_id)
    return OpportunityStateCandidate.model_validate_json(raw)


def _harness(tmp_path: Path, executor=None):
    customers = tmp_path / "customers"
    (customers / "Acme").mkdir(parents=True)
    transcripts = customers / "_transcripts"
    transcripts.mkdir()
    transcript = transcripts / "Acme-09.17.26.txt"
    transcript.write_text("[12:00:00] Customer: synthetic evidence sentinel", encoding="utf-8")
    transcription = TranscriptionService(
        customers_dir=customers,
        workspace=tmp_path,
        safe_name=_safe,
        titlecase=lambda value: value,
        whisper_model="tiny",
    )
    evidence_id = transcription.list_evidence_transcripts("Acme")[0]["id"]
    chosen_executor = executor or FakeCanonicalStateExecutor(
        _candidate_for(evidence_id), cli_version="2.1.272"
    )
    state = OpportunityStateService(customers, safe_name=_safe)
    jobs = JobService(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    service = OpportunityStateCreateService(
        workspace_service=FakeWorkspace(),
        transcription_service=transcription,
        state_service=state,
        job_service=jobs,
        executor=chosen_executor,
    )
    return service, state, jobs, chosen_executor, transcript, evidence_id


async def _wait(jobs: JobService, job_id: str) -> dict:
    for _ in range(100):
        job = jobs.get_job(job_id)
        if job["status"] != "running":
            return job
        await asyncio.sleep(0.01)
    raise AssertionError("job did not finish")


@pytest.mark.asyncio
async def test_create_promotes_one_valid_version_and_refuses_second_create(tmp_path) -> None:
    service, state, jobs, executor, _path, evidence_id = _harness(tmp_path)
    started = await service.start_create("Acme", "synthetic-opportunity", [evidence_id])
    job = await _wait(jobs, started["job_id"])
    assert job["kind"] == "opportunity_state_create"
    assert job["ok"] is True
    assert job["result_revision"] == 1
    current = state.read_current("Acme", "synthetic-opportunity")
    assert current is not None
    assert current.provenance.cli_version == "2.1.272"
    assert len(executor.requests) == 1
    with pytest.raises(OpportunityStateCreateError) as exc:
        await service.start_create("Acme", "synthetic-opportunity", [evidence_id])
    assert exc.value.code == "already_created"


class BlockingExecutor:
    def __init__(self, candidate_value: OpportunityStateCandidate) -> None:
        self.candidate = candidate_value
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, request):
        self.started.set()
        await self.release.wait()
        return CanonicalStateExecutionResult(
            candidate=self.candidate, model="blocking-fake", cli_version="2.1.272"
        )


@pytest.mark.asyncio
async def test_job_idempotency_and_one_active_create_per_opportunity(tmp_path) -> None:
    # Bootstrap once to discover the opaque id, then replace the executor.
    service, state, jobs, _executor, transcript, evidence_id = _harness(tmp_path)
    blocking = BlockingExecutor(_candidate_for(evidence_id))
    service._executor = blocking
    first = await service.start_create("Acme", "synthetic-opportunity", [evidence_id])
    await blocking.started.wait()
    repeated = await service.start_create("Acme", "synthetic-opportunity", [evidence_id])
    assert repeated == {"job_id": first["job_id"], "reused": True}

    other = transcript.with_name("Acme-09.18.26.txt")
    other.write_text("other synthetic evidence", encoding="utf-8")
    other_id = service._transcription_service.list_evidence_transcripts("Acme")[0]["id"]
    if other_id == evidence_id:
        other_id = service._transcription_service.list_evidence_transcripts("Acme")[1]["id"]
    with pytest.raises(OpportunityStateCreateError) as exc:
        await service.start_create("Acme", "synthetic-opportunity", [other_id])
    assert exc.value.code == "create_in_progress"
    blocking.release.set()
    assert (await _wait(jobs, first["job_id"]))["ok"] is True


@pytest.mark.asyncio
async def test_evidence_change_before_execution_prevents_executor_and_promotion(tmp_path) -> None:
    service, state, jobs, executor, transcript, evidence_id = _harness(tmp_path)
    original = service._transcription_service.resolve_evidence_transcripts
    calls = 0

    def resolve(account, ids, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            transcript.write_text("changed before execution", encoding="utf-8")
        return original(account, ids, **kwargs)

    service._transcription_service.resolve_evidence_transcripts = resolve
    started = await service.start_create("Acme", "synthetic-opportunity", [evidence_id])
    job = await _wait(jobs, started["job_id"])
    assert job["error_code"] == "evidence_changed"
    assert not executor.requests
    assert state.read_current("Acme", "synthetic-opportunity") is None


class MutatingExecutor:
    def __init__(self, candidate_value, transcript: Path) -> None:
        self.candidate = candidate_value
        self.transcript = transcript

    async def execute(self, request):
        self.transcript.write_text("changed during execution", encoding="utf-8")
        return CanonicalStateExecutionResult(
            candidate=self.candidate, model="mutating-fake", cli_version="2.1.272"
        )


@pytest.mark.asyncio
async def test_evidence_change_before_promotion_rejects_candidate(tmp_path) -> None:
    service, state, jobs, _executor, transcript, evidence_id = _harness(tmp_path)
    service._executor = MutatingExecutor(_candidate_for(evidence_id), transcript)
    started = await service.start_create("Acme", "synthetic-opportunity", [evidence_id])
    job = await _wait(jobs, started["job_id"])
    assert job["error_code"] == "evidence_changed"
    assert state.read_current("Acme", "synthetic-opportunity") is None


class SafeFailingExecutor:
    async def execute(self, request):
        raise CanonicalStateExecutionError("runtime_failed", "Safe synthetic failure; no state was saved.")


@pytest.mark.asyncio
async def test_job_metadata_and_errors_never_persist_raw_evidence(tmp_path) -> None:
    service, state, jobs, _executor, transcript, evidence_id = _harness(tmp_path)
    service._executor = SafeFailingExecutor()
    sentinel = "synthetic evidence sentinel"
    started = await service.start_create("Acme", "synthetic-opportunity", [evidence_id])
    job = await _wait(jobs, started["job_id"])
    assert job["error_code"] == "runtime_failed"
    persisted = (tmp_path / ".state" / "jobs.json").read_text(encoding="utf-8")
    assert sentinel not in persisted
    assert transcript.read_text(encoding="utf-8") not in persisted
    assert str(transcript) not in persisted
    assert "stdout" not in job and "stderr" not in job
    assert str(transcript) not in json.dumps(job)
    assert state.read_current("Acme", "synthetic-opportunity") is None


def test_interrupted_create_job_recovers_as_safe_error(tmp_path) -> None:
    service, _state, jobs, _executor, _transcript, _evidence_id = _harness(tmp_path)
    jobs.jobs = {
        "create1": {
            "kind": "opportunity_state_create",
            "status": "running",
            "ok": None,
            "account": "Acme",
            "opp_slug": "synthetic-opportunity",
            "started_at": 1.0,
            "evidence_manifest_hash": "a" * 64,
        }
    }
    asyncio.run(jobs.save_snapshot("create1"))
    restarted = JobService(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    recovered = restarted.get_job("create1")
    assert recovered["status"] == "error"
    assert recovered["error_code"] == "interrupted"
    assert "stdout" not in recovered and "stderr" not in recovered
