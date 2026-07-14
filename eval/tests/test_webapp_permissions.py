"""Tests for SEC-001: per-skill permission profiles and invoke approval gates."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import app


def _decode_json_response(resp):
    """Decode a FastAPI JSONResponse body for assertions."""
    data = resp.body
    if isinstance(data, bytes):
        return json.loads(data)
    if hasattr(data, "body"):
        return json.loads(data.body)
    return data


@pytest.mark.parametrize(
    "skill,freeform,expected_write,expected_shell,expected_git",
    [
        pytest.param("prep-call", False, True, False, False, id="prep-call-write-only"),
        pytest.param("biz-qual", False, True, False, False, id="biz-qual-write-only"),
        pytest.param("connector-feasibility", False, True, True, True, id="connector-feasibility-shell-git"),
        pytest.param("full-qual", False, True, False, False, id="full-qual-write-only"),
        pytest.param("next-move", False, True, False, False, id="next-move-write-only"),
        pytest.param(None, True, True, True, True, id="freeform-broad"),
    ],
)
def test_permission_profile_classifies_skills(skill, freeform, expected_write, expected_shell, expected_git):
    profile = app._permission_profile(skill, freeform=freeform)
    data = profile.model_dump()
    assert data["write"] is expected_write
    assert data["shell"] is expected_shell
    assert data["git"] is expected_git
    assert data["requires_approval"] is True
    assert data["summary"]


def test_permission_profile_defaults_unknown_skill_to_write():
    profile = app._permission_profile("not-a-skill")
    data = profile.model_dump()
    assert data["write"] is True
    assert data["shell"] is False
    assert data["git"] is False
    assert data["requires_approval"] is True


def test_api_permissions_returns_profile_for_known_skill():
    data = app.api_permissions(skill="connector-feasibility").model_dump()
    assert data["write"] is True
    assert data["shell"] is True
    assert data["git"] is True
    assert data["requires_approval"] is True
    assert "summary" in data


def test_api_permissions_returns_broad_profile_for_freeform():
    data = app.api_permissions(freeform=True).model_dump()
    assert data["write"] is True
    assert data["shell"] is True
    assert data["git"] is True


def test_api_permissions_rejects_unknown_skill():
    with pytest.raises(Exception):
        app.api_permissions(skill="not-a-skill")


def _patch_invoke(monkeypatch, tmp_path: Path):
    customers = tmp_path / "customers"
    monkeypatch.setattr(app, "CUSTOMERS_DIR", customers)
    monkeypatch.setattr(app, "JOBS", {})

    async def _noop(*args, **kwargs):
        pass

    monkeypatch.setattr(app, "_save_jobs_snapshot", _noop)
    monkeypatch.setattr(app, "_run_job", lambda *args, **kwargs: None)
    monkeypatch.setattr("asyncio.create_task", lambda coro, *, name=None: None)
    return customers


def test_api_invoke_blocks_without_permission_approval(monkeypatch, tmp_path: Path):
    _patch_invoke(monkeypatch, tmp_path)

    body = app.InvokeBody(account="Acme", skill="prep-call")
    resp = asyncio.run(app.api_invoke(body))
    data = _decode_json_response(resp)

    assert data["blocked"] is True
    assert "permissions" in data
    assert data["permissions"]["write"] is True
    assert "job_id" not in data


def test_api_invoke_runs_after_permission_approval(monkeypatch, tmp_path: Path):
    _patch_invoke(monkeypatch, tmp_path)

    body = app.InvokeBody(account="Acme", skill="prep-call", approve_permissions=True)
    resp = asyncio.run(app.api_invoke(body))
    data = _decode_json_response(resp)

    assert data.get("job_id")
    assert "blocked" not in data


def test_api_invoke_blocks_freeform_without_permission_approval(monkeypatch, tmp_path: Path):
    _patch_invoke(monkeypatch, tmp_path)

    body = app.InvokeBody(account="Acme", freeform="Summarize the latest call")
    resp = asyncio.run(app.api_invoke(body))
    data = _decode_json_response(resp)

    assert data["blocked"] is True
    assert data["permissions"]["write"] is True
    assert data["permissions"]["shell"] is True
    assert data["permissions"]["git"] is True
    assert "job_id" not in data


def test_api_invoke_freeform_runs_after_permission_approval(monkeypatch, tmp_path: Path):
    _patch_invoke(monkeypatch, tmp_path)

    body = app.InvokeBody(
        account="Acme",
        freeform="Summarize the latest call",
        approve_permissions=True,
    )
    resp = asyncio.run(app.api_invoke(body))
    data = _decode_json_response(resp)

    assert data.get("job_id")
    assert "blocked" not in data
