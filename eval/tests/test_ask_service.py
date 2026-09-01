"""Deterministic tests for the Ask service boundary."""
from __future__ import annotations

import asyncio
import json
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
        "api_key": lambda member_id=None: "test-key",
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
# Manual escalation (force_deep / prior_answer)
# ---------------------------------------------------------------------------
def test_output_ask_force_deep_routes_to_deep_for_simple_question(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "content")
    job_mock = AsyncMock(return_value=("job-3", None))
    svc = _ask_service(
        output_svc, api_key=lambda member_id=None: "key", job_service=type("JS", (), {"launch": job_mock})()
    )

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="hello", force_deep=True))

    assert result.kind == "deep"
    assert result.job_id == "job-3"
    assert job_mock.call_args.kwargs["meta"]["escalation"] == "manual"


def test_output_ask_force_deep_prompt_includes_prior_answer(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "content")
    job_mock = AsyncMock(return_value=("job-4", None))
    svc = _ask_service(output_svc, job_service=type("JS", (), {"launch": job_mock})())

    _run(svc.output_ask(
        path="Acme/outputs/deal/deal.md",
        question="hello",
        force_deep=True,
        prior_answer="UNIQUE_PRIOR_TEXT here",
    ))

    prompt = job_mock.call_args.kwargs["prompt"]
    assert "UNIQUE_PRIOR_TEXT" in prompt
    assert "PRIOR ANSWER" in prompt


def test_deep_prompt_omits_prior_answer_block_when_none(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "content")
    job_mock = AsyncMock(return_value=("job-5", None))
    svc = _ask_service(output_svc, job_service=type("JS", (), {"launch": job_mock})())

    _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="which connector is best?"))

    prompt = job_mock.call_args.kwargs["prompt"]
    assert "PRIOR ANSWER" not in prompt


def test_transcript_ask_force_deep_routes_to_deep(tmp_path: Path) -> None:
    job_mock = AsyncMock(return_value=("job-6", None))
    svc = _ask_service(_output_service(tmp_path), job_service=type("JS", (), {"launch": job_mock})())

    result = _run(svc.transcript_ask(
        transcript="some call transcript text",
        question="hello",
        account="Acme",
        opportunity=None,
        live=False,
        session_id="sess-1",
        force_deep=True,
    ))

    assert result.kind == "deep"
    assert job_mock.call_args.kwargs["meta"]["escalation"] == "manual"


def test_transcript_ask_force_deep_prompt_includes_prior_answer(tmp_path: Path) -> None:
    job_mock = AsyncMock(return_value=("job-7", None))
    svc = _ask_service(_output_service(tmp_path), job_service=type("JS", (), {"launch": job_mock})())

    _run(svc.transcript_ask(
        transcript="some call transcript text",
        question="hello",
        account="Acme",
        opportunity=None,
        live=False,
        session_id="sess-1",
        force_deep=True,
        prior_answer="UNIQUE_PRIOR_TEXT here",
    ))

    prompt = job_mock.call_args.kwargs["prompt"]
    assert "UNIQUE_PRIOR_TEXT" in prompt
    assert "PRIOR ANSWER" in prompt


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
def test_no_key_routes_to_deep_even_for_non_deep_question(tmp_path: Path) -> None:
    # Regression: without an API key, the quick path can never work no matter
    # what the question is about. It must route to claude -p unconditionally —
    # previously a non-DEEP_HINTS question (e.g. "hello") returned a
    # "needs_deep" dead end that re-asking the same question could never
    # escape, since the keyword check ignores key availability.
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "content")
    job_mock = AsyncMock(return_value=("job-1", None))
    svc = _ask_service(
        output_svc, api_key=lambda member_id=None: None, job_service=type("JS", (), {"launch": job_mock})()
    )

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="hello"))

    assert result.kind == "deep"
    assert result.job_id == "job-1"
    job_mock.assert_awaited_once()


def test_transcript_ask_no_key_routes_to_deep_for_non_deep_question(tmp_path: Path) -> None:
    # Same regression as output_ask, for the live/saved-transcript path.
    job_mock = AsyncMock(return_value=("job-2", None))
    svc = _ask_service(
        _output_service(tmp_path), api_key=lambda member_id=None: None, job_service=type("JS", (), {"launch": job_mock})()
    )

    result = _run(svc.transcript_ask(
        transcript="some call transcript text",
        question="what should the AE do next?",
        account="Acme",
        opportunity=None,
        live=False,
        session_id="sess-1",
    ))

    assert result.kind == "deep"
    assert result.job_id == "job-2"
    job_mock.assert_awaited_once()


@pytest.mark.parametrize("tokens,expected_texts", [
    pytest.param(["Hello ", "world"], ["Hello ", "world"], id="two_tokens"),
    pytest.param(["One."], ["One."], id="single_token"),
])
def test_quick_stream_yields_tokens(tmp_path: Path, monkeypatch, tokens, expected_texts) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic_module(tokens))

    svc = _ask_service(output_svc, api_key=lambda member_id=None: "key")
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


# ---------------------------------------------------------------------------
# Auto-escalation (NEEDS_DEEP sentinel)
# ---------------------------------------------------------------------------
def test_output_ask_auto_escalates_on_needs_deep_sentinel(tmp_path: Path, monkeypatch) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        _fake_anthropic_module(["Partial answer.\n", "NEEDS_DEEP: needs live registry data"]),
    )
    job_mock = AsyncMock(return_value=("job-auto", None))
    svc = _ask_service(
        output_svc, api_key=lambda member_id=None: "key", job_service=type("JS", (), {"launch": job_mock})()
    )

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="summary"))
    assert result.kind == "quick"

    async def collect():
        return [event async for event in result.stream]

    events = _run(collect())
    escalate_events = [e for e in events if e["event"] == "escalate"]
    assert len(escalate_events) == 1
    payload = json.loads(escalate_events[0]["data"])
    assert payload["reason"] == "needs live registry data"
    assert "NEEDS_DEEP" not in payload["html"]
    assert payload["job_id"] == "job-auto"
    assert events[-1]["event"] == "done"  # done always stays last

    job_mock.assert_awaited_once()
    call = job_mock.call_args.kwargs
    assert call["skill"] == "output-ask"
    assert call["meta"]["escalation"] == "auto"
    assert "Partial answer." in call["prompt"]


def test_quick_stream_no_escalation_without_sentinel(tmp_path: Path, monkeypatch) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic_module(["Just a normal answer."]))
    job_mock = AsyncMock(return_value=("job-x", None))
    svc = _ask_service(
        output_svc, api_key=lambda member_id=None: "key", job_service=type("JS", (), {"launch": job_mock})()
    )

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="summary"))

    async def collect():
        return [event async for event in result.stream]

    events = _run(collect())
    assert not [e for e in events if e["event"] == "escalate"]
    assert events[-1]["event"] == "done"
    job_mock.assert_not_awaited()


def test_output_ask_auto_escalates_on_empty_completion(tmp_path: Path, monkeypatch) -> None:
    # Regression: an intermittent empty completion from the model (no tokens,
    # no sentinel) must never surface as a silent "no response" dead end — it
    # should auto-escalate to the deep path exactly like NEEDS_DEEP does.
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "Big deal.")
    monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic_module([]))  # zero tokens
    job_mock = AsyncMock(return_value=("job-empty", None))
    svc = _ask_service(
        output_svc, api_key=lambda member_id=None: "key", job_service=type("JS", (), {"launch": job_mock})()
    )

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="summary"))

    async def collect():
        return [event async for event in result.stream]

    events = _run(collect())
    escalate_events = [e for e in events if e["event"] == "escalate"]
    assert len(escalate_events) == 1
    payload = json.loads(escalate_events[0]["data"])
    assert payload["reason"] == "the quick path returned an empty response"
    assert payload["job_id"] == "job-empty"
    assert events[-1]["event"] == "done"
    job_mock.assert_awaited_once()
    assert job_mock.call_args.kwargs["meta"]["escalation"] == "auto"


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
        api_key=lambda member_id=None: "key",
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
    svc = _ask_service(_output_service(tmp_path), api_key=lambda member_id=None: key)
    assert svc.ai_status() is expected


# ---------------------------------------------------------------------------
# Per-member key resolution
# ---------------------------------------------------------------------------
def test_output_ask_passes_member_id_to_api_key(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "content")
    seen = []

    def spy_api_key(member_id=None):
        seen.append(member_id)
        return "key"

    svc = _ask_service(output_svc, api_key=spy_api_key)
    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="summary", member_id="gary"))
    assert result.kind == "quick"

    async def collect():
        return [event async for event in result.stream]

    _run(collect())
    assert "gary" in seen


def test_output_ask_no_key_for_this_member_routes_to_deep(tmp_path: Path) -> None:
    output_svc = _output_service(tmp_path)
    _make_file(tmp_path, "Acme/outputs/deal/deal.md", "content")
    job_mock = AsyncMock(return_value=("job-8", None))
    svc = _ask_service(
        output_svc,
        api_key=lambda member_id=None: None,  # no key configured for any member
        job_service=type("JS", (), {"launch": job_mock})(),
    )

    result = _run(svc.output_ask(path="Acme/outputs/deal/deal.md", question="hello", member_id="unowned"))

    assert result.kind == "deep"
    job_mock.assert_awaited_once()


class _FakeKeyringBackend:
    """In-memory stand-in for the `_keyring_get`/`_keyring_set`/`_keyring_delete`
    module functions — no real OS keyring involved in these tests."""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, username):
        return self.store.get(username)

    def set(self, username, value):
        self.store[username] = value


@pytest.fixture
def fake_keyring(monkeypatch):
    import services.ask_service as ask_service_module

    backend = _FakeKeyringBackend()
    monkeypatch.setattr(ask_service_module, "_keyring_get", lambda username: backend.get(username))
    monkeypatch.setattr(ask_service_module, "_keyring_set", lambda username, value: backend.set(username, value))
    monkeypatch.setattr(
        ask_service_module,
        "_keyring_delete",
        lambda username: backend.store.pop(username, None),
    )
    return backend


def test_anthropic_api_key_env_var_wins_over_keyring(fake_keyring, monkeypatch) -> None:
    import services.ask_service as ask_service_module

    fake_keyring.set("anthropic_api_key:gary", "member-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "env-key")
    assert ask_service_module.anthropic_api_key("gary") == "env-key"


def test_anthropic_api_key_per_member_wins_over_legacy_unscoped(fake_keyring, monkeypatch) -> None:
    import services.ask_service as ask_service_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_keyring.set("anthropic_api_key:gary", "member-key")
    fake_keyring.set("ANTHROPIC_API_KEY", "legacy-key")
    assert ask_service_module.anthropic_api_key("gary") == "member-key"


def test_anthropic_api_key_falls_back_to_legacy_unscoped(fake_keyring, monkeypatch) -> None:
    import services.ask_service as ask_service_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake_keyring.set("ANTHROPIC_API_KEY", "legacy-key")
    assert ask_service_module.anthropic_api_key("gary") == "legacy-key"
    assert ask_service_module.anthropic_api_key(None) == "legacy-key"


def test_anthropic_api_key_none_when_nothing_configured(fake_keyring, monkeypatch) -> None:
    import services.ask_service as ask_service_module

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ask_service_module.anthropic_api_key("gary") is None


def test_member_api_key_configured_reflects_only_per_member_entry(fake_keyring) -> None:
    import services.ask_service as ask_service_module

    fake_keyring.set("ANTHROPIC_API_KEY", "legacy-key")
    assert ask_service_module.member_api_key_configured("gary") is False
    fake_keyring.set("anthropic_api_key:gary", "member-key")
    assert ask_service_module.member_api_key_configured("gary") is True


def test_save_and_clear_member_api_key_round_trip(fake_keyring) -> None:
    import services.ask_service as ask_service_module

    ask_service_module.save_member_api_key("gary", "  sk-ant-test  ")
    assert fake_keyring.get("anthropic_api_key:gary") == "sk-ant-test"
    assert ask_service_module.member_api_key_configured("gary") is True

    ask_service_module.clear_member_api_key("gary")
    assert ask_service_module.member_api_key_configured("gary") is False


def test_save_member_api_key_rejects_empty(fake_keyring) -> None:
    import services.ask_service as ask_service_module

    with pytest.raises(AskError) as exc:
        ask_service_module.save_member_api_key("gary", "   ")
    assert exc.value.status_code == 400


def test_save_member_api_key_propagates_backend_failure(monkeypatch) -> None:
    import services.ask_service as ask_service_module

    def raising_set(username, value):
        raise AskError(503, "no backend")

    monkeypatch.setattr(ask_service_module, "_keyring_set", raising_set)
    with pytest.raises(AskError) as exc:
        ask_service_module.save_member_api_key("gary", "sk-ant-test")
    assert exc.value.status_code == 503


def test_clear_member_api_key_propagates_backend_failure(monkeypatch) -> None:
    import services.ask_service as ask_service_module

    def raising_delete(username):
        raise AskError(503, "no backend")

    monkeypatch.setattr(ask_service_module, "_keyring_delete", raising_delete)
    with pytest.raises(AskError) as exc:
        ask_service_module.clear_member_api_key("gary")
    assert exc.value.status_code == 503


# ---------------------------------------------------------------------------
# Path safety / fallback
# ---------------------------------------------------------------------------
def test_traverse_outside_customers_dir_raises_404(tmp_path: Path) -> None:
    svc = _ask_service(_output_service(tmp_path))
    with pytest.raises(AskError) as exc:
        _run(svc.output_ask(path="../outside.md", question="hello"))
    assert exc.value.status_code == 404
