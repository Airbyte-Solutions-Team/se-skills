"""Focused deterministic tests for the extracted job lifecycle service and routes.

These tests exercise `webapp/services/job_service.py` and `webapp/routes/jobs.py`
without running the real `claude -p` subprocess. They verify the exact behaviors
listed in the ARCH-001 acceptance criteria.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

import persistence
from webapp.services.job_service import JobService


class _FakeProc:
    """Minimal async subprocess stand-in for `_run_job` tests."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.pid = 12345
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode("utf-8"), self._stderr.encode("utf-8")


def _noop_persist(*args, **kwargs) -> None:
    pass


def _model_for(use: str) -> str:
    return "dummy-model"


def _fake_proc(returncode: int, stdout: str = "", stderr: str = ""):
    return AsyncMock(return_value=_FakeProc(returncode, stdout, stderr))


@pytest.fixture(autouse=True)
def _block_background_tasks(monkeypatch) -> None:
    """Prevent `launch` from actually starting `claude -p` in tests.

    Tests that exercise `_run_job` call it directly with a patched subprocess.
    """
    def _discard(coro, *, name=None):
        if asyncio.iscoroutine(coro):
            coro.close()
        return None

    monkeypatch.setattr("asyncio.create_task", _discard)


@pytest.fixture
def service(tmp_path, monkeypatch) -> JobService:
    """A fresh `JobService` backed by a temp workspace and no-op persist_run."""
    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    svc.jobs = {}

    async def _noop_run(*args, **kwargs) -> None:
        pass

    monkeypatch.setattr(svc, "_run_job", _noop_run)
    return svc


def test_launch_creates_running_job(service: JobService) -> None:
    """1. Job creation; 4. Running transition; 9. started_at preservation."""
    job_id, warning = asyncio.run(service.launch(
        account="Acme",
        opp_slug="op1",
        skill="prep-call",
        opportunity="Big Deal",
        sig=("Acme", "op1", "prep-call", "extra"),
        prompt="Run prep-call skill",
        meta={"account": "Acme", "opp_slug": "op1", "skill": "prep-call", "opportunity": "Big Deal"},
    ))
    assert job_id
    assert warning is None
    job = service.get_job(job_id)
    assert job is not None
    assert job["status"] == "running"
    assert job["ok"] is None
    assert job["skill"] == "prep-call"
    assert job["account"] == "Acme"
    assert job["opp_slug"] == "op1"
    assert job["opportunity"] == "Big Deal"
    assert job["sig"] == ("Acme", "op1", "prep-call", "extra")
    assert "started_at" in job and job["started_at"] > 0
    assert "finished_at" not in job


def test_launch_generates_unique_ids(service: JobService) -> None:
    """2. Unique job IDs."""
    ids = set()
    for i in range(20):
        job_id, _ = asyncio.run(service.launch(
            account="Acme",
            opp_slug=None,
            skill="prep-call",
            opportunity=None,
            sig=("Acme", None, "prep-call", str(i)),
            prompt="p",
            meta={"account": "Acme", "opp_slug": None, "skill": "prep-call", "opportunity": None},
        ))
        ids.add(job_id)
    assert len(ids) == 20


def test_find_reused_job(service: JobService) -> None:
    """Reusing a running job by signature returns the same job."""
    sig = ("Acme", "op1", "prep-call", "context")
    job_id, _ = asyncio.run(service.launch(
        account="Acme",
        opp_slug="op1",
        skill="prep-call",
        opportunity="Big Deal",
        sig=sig,
        prompt="p",
        meta={"account": "Acme", "opp_slug": "op1", "skill": "prep-call", "opportunity": "Big Deal"},
    ))
    reused = service.find_reused_job(sig)
    assert reused is not None
    assert reused[0] == job_id
    assert reused[1]["status"] == "running"


def test_run_job_success(service: JobService, monkeypatch) -> None:
    """5. Successful completion; 10. finished_at preservation."""
    job_id, _ = asyncio.run(service.launch(
        account="Acme", opp_slug="op1", skill="prep-call", opportunity="Big Deal",
        sig=("s",), prompt="p", meta={"account": "Acme", "opp_slug": "op1", "skill": "prep-call", "opportunity": "Big Deal"},
    ))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_proc(returncode=0, stdout="output", stderr=""))

    asyncio.run(JobService._run_job(service, job_id, "p", {"account": "Acme", "opp_slug": "op1", "skill": "prep-call", "opportunity": "Big Deal"}))

    job = service.get_job(job_id)
    assert job["status"] == "done"
    assert job["ok"] is True
    assert job["stdout"] == "output"
    assert job["stderr"] == ""
    assert "finished_at" in job and job["finished_at"] >= job["started_at"]


def test_run_job_failure(service: JobService, monkeypatch) -> None:
    """6. Failure transition; 7. Error detail preservation."""
    job_id, _ = asyncio.run(service.launch(
        account="Acme", opp_slug=None, skill="prep-call", opportunity=None,
        sig=("f",), prompt="p", meta={"account": "Acme", "opp_slug": None, "skill": "prep-call", "opportunity": None},
    ))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_proc(returncode=1, stdout="", stderr="claude failed"))

    asyncio.run(JobService._run_job(service, job_id, "p", {"account": "Acme", "opp_slug": None, "skill": "prep-call", "opportunity": None}))

    job = service.get_job(job_id)
    assert job["status"] == "done"
    assert job["ok"] is False
    assert job["stderr"] == "claude failed"
    assert "finished_at" in job


def test_run_job_claude_not_found(service: JobService, monkeypatch) -> None:
    """Failure when the Claude CLI is missing produces a clear UI error."""
    job_id, _ = asyncio.run(service.launch(
        account="Acme", opp_slug=None, skill="prep-call", opportunity=None,
        sig=("nf",), prompt="p", meta={"account": "Acme", "opp_slug": None, "skill": "prep-call", "opportunity": None},
    ))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=FileNotFoundError))

    asyncio.run(JobService._run_job(service, job_id, "p", {"account": "Acme", "opp_slug": None, "skill": "prep-call", "opportunity": None}))

    job = service.get_job(job_id)
    assert job["status"] == "error"
    assert job["ok"] is False
    assert "claude` CLI not found" in job["stderr"]
    assert "finished_at" in job


def test_run_job_timeout(service: JobService, monkeypatch) -> None:
    """A slow subprocess is reported as a timeout."""
    job_id, _ = asyncio.run(service.launch(
        account="Acme", opp_slug=None, skill="prep-call", opportunity=None,
        sig=("to",), prompt="p", meta={"account": "Acme", "opp_slug": None, "skill": "prep-call", "opportunity": None},
    ))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(side_effect=asyncio.TimeoutError))

    asyncio.run(JobService._run_job(service, job_id, "p", {"account": "Acme", "opp_slug": None, "skill": "prep-call", "opportunity": None}))

    job = service.get_job(job_id)
    assert job["status"] == "error"
    assert "timed out" in job["stderr"].lower()


def test_persistence_excludes_runtime_only_fields(tmp_path) -> None:
    """15. Runtime-only fields excluded from persistence; 16. task/proc retained only in memory."""
    jobs = {
        "j1": {
            "status": "running",
            "task": "<asyncio.Task>",
            "proc": "<subprocess.Popen>",
            "persistence_warning": "warn",
            "stdout": "out",
            "stderr": "err",
            "sig": ("a", "b"),
        },
    }
    persistence.save_jobs(jobs, tmp_path)
    raw = (tmp_path / ".state" / "jobs.json").read_text()
    assert "task" not in raw
    assert "proc" not in raw
    assert "persistence_warning" not in raw
    assert "stdout" in raw  # stdout/stderr are legitimate persisted state


def test_persistence_recover_interrupted_jobs(tmp_path) -> None:
    """8. Interrupted-job recovery; 19. Multiple jobs surviving restart."""
    before = {
        "done1": {"status": "done", "ok": True, "finished_at": 1.0, "skill": "prep-call", "account": "Acme"},
        "running1": {"status": "running", "skill": "poc-plan", "account": "Acme", "started_at": 2.0},
    }
    persistence.save_jobs(before, tmp_path)
    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    assert svc.get_job("done1")["status"] == "done"
    recovered = svc.get_job("running1")
    assert recovered["status"] == "error"
    assert recovered["ok"] is False
    assert "Server restarted" in recovered["stderr"]
    assert "finished_at" in recovered


def test_persistence_missing_file_returns_empty(tmp_path) -> None:
    """12. Missing persistence file."""
    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    assert svc.jobs == {}


def test_persistence_malformed_entries(tmp_path) -> None:
    """13. Malformed persisted entries; 14. valid + malformed in one file."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)
    bad_json = '{"j1": "not a dict", "j2": {"status": "done", "ok": true}}'
    (state_dir / "jobs.json").write_text(bad_json)
    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    assert "j2" in svc.jobs
    assert svc.jobs["j2"]["status"] == "done"


def test_persistence_legacy_records_missing_fields(tmp_path) -> None:
    """11. Legacy records missing newer fields."""
    state_dir = tmp_path / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)
    legacy = '{"old": {"status": "done", "ok": true, "skill": "prep-call", "account": "Acme"}}'
    (state_dir / "jobs.json").write_text(legacy)
    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    job = svc.get_job("old")
    assert job["status"] == "done"
    assert job.get("started_at") is None
    assert job.get("finished_at") is None


def test_save_and_reload_round_trip(tmp_path) -> None:
    """18. Save and reload round trip; 20/21. Account/opportunity preservation."""
    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    svc.jobs = {
        "j1": {
            "status": "done", "ok": True, "skill": "prep-call",
            "account": "Acme", "opp_slug": "op1", "opportunity": "Big Deal",
            "started_at": 1.0, "finished_at": 2.0,
            "sig": ("a",),
        },
    }
    persistence.save_jobs(svc.jobs, tmp_path)
    svc2 = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    job = svc2.get_job("j1")
    assert job["account"] == "Acme"
    assert job["opp_slug"] == "op1"
    assert job["opportunity"] == "Big Deal"


def test_save_snapshot_warns_and_clears(tmp_path, monkeypatch) -> None:
    """17. Persistence-warning behavior."""
    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    svc.jobs = {"j1": {"status": "running"}}
    monkeypatch.setattr(persistence, "save_jobs", lambda jobs, ws: False)
    warn = asyncio.run(svc.save_snapshot("j1"))
    assert warn is not None
    assert svc.jobs["j1"].get("persistence_warning") == warn

    monkeypatch.setattr(persistence, "save_jobs", lambda jobs, ws: True)
    cleared = asyncio.run(svc.save_snapshot("j1"))
    assert cleared is None
    assert "persistence_warning" not in svc.jobs["j1"]


def test_list_jobs_filters_and_excludes_sensitive_fields(service: JobService) -> None:
    """22. /api/jobs response compatibility; runtime-only fields not in list."""
    service.jobs = {
        "j1": {
            "status": "running", "skill": "prep-call", "account": "Acme",
            "opp_slug": "op1", "opportunity": "Big Deal", "stdout": "secret", "stderr": "secret",
            "sig": ("a",), "started_at": 1.0,
        },
        "j2": {
            "status": "done", "skill": "poc-plan", "account": "Other",
            "opp_slug": None, "opportunity": None, "stdout": "", "stderr": "",
            "sig": ("b",), "started_at": 2.0, "finished_at": 3.0,
        },
    }
    all_jobs = service.list_jobs()
    assert len(all_jobs) == 2
    assert all("sig" not in j and "stdout" not in j and "stderr" not in j for j in all_jobs)
    assert all("job_id" in j for j in all_jobs)

    acme = service.list_jobs(account="Acme")
    assert len(acme) == 1
    assert acme[0]["job_id"] == "j1"

    opp = service.list_jobs(account="Acme", opp_slug="op1")
    assert len(opp) == 1
    assert opp[0]["opportunity"] == "Big Deal"


def test_get_job_returns_full_record(service: JobService) -> None:
    """get_job returns the full dict including stdout/stderr (used by the poller)."""
    service.jobs = {"j1": {"status": "done", "stdout": "out", "stderr": "err", "sig": ("a",)}}
    job = service.get_job("j1")
    assert job["stdout"] == "out"
    assert job["stderr"] == "err"


def test_service_loads_at_init_and_marks_running_as_error(tmp_path) -> None:
    """Initialization triggers recovery semantics."""
    persistence.save_jobs({"j1": {"status": "running", "account": "Acme"}}, tmp_path)
    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    assert svc.get_job("j1")["status"] == "error"


def test_route_api_jobs_response_shape(tmp_path, monkeypatch) -> None:
    """22/25. Router registration and unchanged route path."""
    from types import SimpleNamespace
    from webapp import app as app_module
    from webapp.routes import jobs as routes_jobs

    svc = JobService(tmp_path, model_for=_model_for, persist_run=_noop_persist)
    monkeypatch.setattr(app_module, "job_service", svc)
    fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(job_service=svc)))
    svc.jobs = {
        "abc123": {
            "status": "running", "skill": "prep-call", "account": "Acme",
            "opp_slug": "op1", "opportunity": "Big Deal", "started_at": time.time(),
            "stdout": "out", "stderr": "err", "sig": ("a",),
        },
    }

    jobs = routes_jobs.api_jobs_for(None, None, fake_request)
    assert len(jobs) == 1
    assert jobs[0]["job_id"] == "abc123"
    assert "stdout" not in jobs[0]
    assert "stderr" not in jobs[0]
    assert "sig" not in jobs[0]
    assert jobs[0]["account"] == "Acme"
    assert jobs[0]["opp_slug"] == "op1"

    body = routes_jobs.api_job("abc123", fake_request)
    assert body["stdout"] == "out"  # detail endpoint exposes output for the poller
    assert "sig" not in body

    # Confirm the router still registered the exact URL patterns.
    assert app_module.app.url_path_for("api_jobs_for") == "/api/jobs"
    assert app_module.app.url_path_for("api_job", job_id="abc123") == "/api/jobs/abc123"


def test_compatibility_wrappers_delegate(service: JobService, monkeypatch) -> None:
    """24. Compatibility wrappers delegate to the service."""
    from webapp import app as app_module

    monkeypatch.setattr(app_module, "job_service", service)
    service.jobs = {"j1": {"status": "running", "persistence_warning": "warn", "stdout": "out", "stderr": "err", "sig": ("a",)}}

    assert app_module.api_job("j1")["persistence_warning"] == "warn"
    assert app_module.api_jobs_for() == [{"job_id": "j1", "status": "running", "persistence_warning": "warn"}]
