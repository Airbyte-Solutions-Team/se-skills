"""Deterministic tests for the Ask service boundary."""
from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from services.ask_service import AskError, AskService
from services.output_service import OutputService


def _run(coro):
    return asyncio.run(coro)


def _output_service(tmp_path: Path) -> OutputService:
    return OutputService(
        customers_dir=tmp_path,
        workspace=tmp_path,
        repo_root=tmp_path,
        se_config=lambda: {},
        safe_name=lambda n: n,
        slug=lambda n: n,
        run_cmd=None,
        internal_repo=None,
    )


def _make_file(tmp_path: Path, rel: str, content: str) -> None:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _ask_service(output_service: OutputService, **overrides) -> AskService:
    defaults = {
        "output_service": output_service,
        "job_service": AsyncMock(),
        "api_key": lambda: "test-key",
        "model_for": lambda use: "claude-sonnet-4-6",
    }
    defaults.update(overrides)
    return AskService(**defaults)


def _fake_anthropic_module(tokens: list[str] | None = None, raise_on_enter: Exception | None = None):
    """Return a minimal fake `anthropic` module for the streaming quick path."""
    tokens = tokens or []

    class FakeStream:
        def __init__(self):
            self._idx = 0
            self.text_stream = self

        async def __aenter__(self):
            if raise_on_enter:
                raise raise_on_enter
            return self

        async def __aexit__(self, *args):
            return False

        def __aiter__(self):
            self._idx = 0
            return self

        async def __anext__(self):
            if self._idx >= len(tokens):
                raise StopAsyncIteration
            tok = tokens[self._idx]
            self._idx += 1
            return tok

    class FakeMessages:
        def stream(self, **kwargs):
            return FakeStream()

    class FakeAsyncAnthropic:
        def __init__(self, api_key: str | None = None):
            self.messages = FakeMessages()

    return types.SimpleNamespace(AsyncAnthropic=FakeAsyncAnthropic)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_empty_question_raises(tmp_path: Path) -> None:
    svc = _ask_service(_output_service(tmp_path))
    with pytest.raises(AskError) as exc:
        _run(svc.output_ask(path="x.md", question="   "))
    assert exc.value.status_code == 400
    assert "Empty question" in exc.value.detail


def test_missing_output_raises_404(tmp_path: Path) -> None:
    svc = _ask_service(_output_service(tmp_path))
    with pytest.raises(AskError) as exc:
        _run(svc.output_ask(path="does/not/exist.md", question="hello"))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Deep path
# ---------------------------------------------------------------------------
def test_deep_uses_job_service(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    job_mock = AsyncMock(return_value=("job123", None))
    svc = _ask_service(output_svc, job_service=type("JS", (), {"launch": job_mock})())

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="which connector is best?"))

    assert result.kind == "deep"
    assert result.job_id == "job123"
    assert result.persistence_warning is None
    job_mock.assert_awaited_once()
    call = job_mock.call_args.kwargs
    assert call["account"] == "?"
    assert call["skill"] == "output-ask"
    assert call["opportunity"] is None
    assert "which connector is best?" in call["prompt"]


def test_deep_persistence_warning(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    job_mock = AsyncMock(return_value=("job456", "disk full"))
    svc = _ask_service(output_svc, job_service=type("JS", (), {"launch": job_mock})())

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="deployment options"))

    assert result.persistence_warning == "disk full"


def test_deep_prompt_includes_account_and_opportunity(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "content")
    job_mock = AsyncMock(return_value=("job", None))
    svc = _ask_service(output_svc, job_service=type("JS", (), {"launch": job_mock})())

    _run(svc.output_ask(
        path="Acme/outputs/deal/deal.md",
        question="what about cdc?",
        account="Acme",
        opportunity="Big Deal",
    ))

    prompt = job_mock.call_args.kwargs["prompt"]
    assert "'Acme'" in prompt
    assert "'Big Deal'" in prompt


# ---------------------------------------------------------------------------
# Tail and context limits
# ---------------------------------------------------------------------------
def test_output_ask_tails_document(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    prefix = "UNIQUE_START" + "A" * 17_000
    suffix = "UNIQUE_END"
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", prefix + suffix)
    job_mock = AsyncMock(return_value=("job", None))
    svc = _ask_service(output_svc, job_service=type("JS", (), {"launch": job_mock})())

    _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="codebase"))

    prompt = job_mock.call_args.kwargs["prompt"]
    assert "UNIQUE_END" in prompt
    assert "UNIQUE_START" not in prompt


# ---------------------------------------------------------------------------
# Quick path
# ---------------------------------------------------------------------------
def test_quick_no_key_returns_needs_deep(tmp_path: Path) -> None:
    svc = _ask_service(_output_service(tmp_path), api_key=lambda: None)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "content")

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="hello"))

    assert result.kind == "needs_deep"
    assert "No ANTHROPIC_API_KEY" in result.reason


@pytest.mark.parametrize("tokens,expected_texts", [
    pytest.param(["Hello ", "world"], ["Hello ", "world"], id="two_tokens"),
    pytest.param(["One."], ["One."], id="single_token"),
])
def test_quick_stream_yields_tokens(tmp_path: Path, monkeypatch, tokens, expected_texts) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic_module(tokens))

    svc = _ask_service(output_svc, api_key=lambda: "key")
    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="summary"))
    assert result.kind == "quick"

    async def collect():
        return [event async for event in result.stream]

    events = _run(collect())
    token_events = [e for e in events if e.get("event") == "token"]
    assert len(token_events) == len(expected_texts)
    for ev, text in zip(token_events, expected_texts):
        data = __import__("json").loads(ev["data"])
        assert data["text"] == text
        assert data["html"]  # markdown rendered
    assert events[-1]["event"] == "done"


def test_quick_stream_redacts_errors(tmp_path: Path, monkeypatch) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        _fake_anthropic_module(raise_on_enter=RuntimeError("boom ANTHROPIC_API_KEY=secret")),
    )

    redacted = []
    svc = _ask_service(
        output_svc,
        api_key=lambda: "key",
        redact=lambda s: redacted.append(s) or s.replace("secret", "***"),
    )
    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="summary"))

    async def collect():
        return [event async for event in result.stream]

    events = _run(collect())
    error_events = [e for e in events if e.get("event") == "error"]
    assert error_events
    data = __import__("json").loads(error_events[0]["data"])
    assert "boom" in data["error"]
    assert "secret" not in data["error"]
    assert "***" in data["error"]


# ---------------------------------------------------------------------------
# AI status
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("key,expected", [
    pytest.param("key", True, id="key_present"),
    pytest.param(None, False, id="key_missing"),
])
def test_ai_status_reflects_key(tmp_path: Path, key, expected) -> None:
    svc = _ask_service(_output_service(tmp_path), api_key=lambda: key)
    assert svc.ai_status() is expected


# ---------------------------------------------------------------------------
# Path safety / fallback
# ---------------------------------------------------------------------------
def test_traverse_outside_customers_dir_raises_404(tmp_path: Path) -> None:
    svc = _ask_service(_output_service(tmp_path))
    with pytest.raises(AskError) as exc:
        _run(svc.output_ask(path="../outside.md", question="hello"))
    assert exc.value.status_code == 404
