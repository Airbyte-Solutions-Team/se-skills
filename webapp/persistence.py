"""Lightweight disk persistence for background state in `webapp/app.py`.

Stores:
- `JOBS` snapshots under `<workspace>/.state/jobs.json`
- Live-transcribe sessions under `<workspace>/.state/sessions/<session_id>.json`

All writes are atomic (temp file + rename) so a crash mid-write never leaves a
half-written JSON file. This module is intentionally small and dependency-free so
the webapp can load state before importing heavier libraries.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


def _state_dir(workspace: Path) -> Path:
    return workspace / ".state"


def _jobs_file(workspace: Path) -> Path:
    return _state_dir(workspace) / "jobs.json"


def _sessions_dir(workspace: Path) -> Path:
    return _state_dir(workspace) / "sessions"


def _ensure_dirs(workspace: Path) -> None:
    _state_dir(workspace).mkdir(parents=True, exist_ok=True)
    _sessions_dir(workspace).mkdir(parents=True, exist_ok=True)


def _safe_session_filename(session_id: str) -> str:
    """Strip any path metacharacters so a session id cannot escape the sessions dir."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", session_id)[:120]
    if not safe:
        safe = "unknown"
    return f"{safe}.json"


def _write_atomic(path: Path, data: str) -> None:
    """Write `data` to `path` atomically by writing to a temp file in the same
directory and then renaming it into place."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


def _serialize_default(obj: Any) -> Any:
    """JSON encoder fallback: convert tuples (e.g. job dedupe signatures) to lists."""
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def save_jobs(jobs: dict[str, dict], workspace: Path) -> None:
    """Persist the in-memory jobs dict to disk.

    We copy and filter out any non-serializable fields (asyncio.Task handles,
    process objects, etc.) before writing."""
    _ensure_dirs(workspace)
    payload: dict[str, dict] = {}
    for job_id, job in jobs.items():
        clean: dict[str, Any] = {}
        for key, value in job.items():
            if key in ("task", "proc"):
                continue
            clean[key] = value
        payload[job_id] = clean
    _write_atomic(_jobs_file(workspace), json.dumps(payload, default=_serialize_default, indent=2))


def load_jobs(workspace: Path) -> dict[str, dict]:
    """Load the persisted jobs snapshot and mark any still-running jobs as lost.

    Child processes cannot be reattached after a server restart, so a job that
    was `running` is converted to `error` with a clear message."""
    path = _jobs_file(workspace)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    jobs: dict[str, dict] = {}
    for job_id, rec in data.items():
        if not isinstance(rec, dict):
            continue
        if rec.get("status") == "running":
            rec["status"] = "error"
            rec["ok"] = False
            existing_stderr = rec.get("stderr") or ""
            rec["stderr"] = existing_stderr + (
                "\n[Server restarted while this job was running. Re-run if needed.]"
                if existing_stderr else "[Server restarted while this job was running. Re-run if needed.]"
            )
        sig = rec.get("sig")
        if isinstance(sig, list):
            rec["sig"] = tuple(sig)
        jobs[job_id] = rec
    return jobs


def save_session(session_data: dict[str, Any], workspace: Path) -> None:
    """Persist one live-transcribe session to disk.

    The caller (`LiveSession.to_dict`) must make `session_data` JSON-serializable
    (datetimes as timestamps, segments as plain dicts, etc.)."""
    if not session_data.get("session_id"):
        return
    _ensure_dirs(workspace)
    path = _sessions_dir(workspace) / _safe_session_filename(session_data["session_id"])
    _write_atomic(path, json.dumps(session_data, default=_serialize_default, indent=2))


def load_sessions(workspace: Path) -> list[dict[str, Any]]:
    """Return all persisted live-transcribe sessions, marked as recovered.

    Audio capture cannot survive a process restart, so every loaded session is
    treated as recovered/ended and is waiting for the SE to save or discard it."""
    out: list[dict[str, Any]] = []
    d = _sessions_dir(workspace)
    if not d.exists():
        return out
    for path in d.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        data["recovered"] = True
        data["ended"] = True
        out.append(data)
    return out


def delete_session(session_id: str, workspace: Path) -> None:
    """Remove a session file when it is explicitly stopped/saved."""
    path = _sessions_dir(workspace) / _safe_session_filename(session_id)
    if path.exists():
        path.unlink()
