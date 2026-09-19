from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone

import pytest

from services.opportunity_state_executor import (
    CanonicalStateExecutionError,
    CanonicalStateExecutionRequest,
    ClaudeCanonicalStateExecutor,
    VERIFIED_CLAUDE_VERSION,
)
from services.transcription_service import ResolvedTranscriptEvidence
from eval.tests.opportunity_state_helpers import TRANSCRIPT_ID, candidate


def _request() -> CanonicalStateExecutionRequest:
    content = b"[12:00:00] Customer: SYNTHETIC_SENTINEL"
    return CanonicalStateExecutionRequest(
        account="Acme",
        opportunity_slug="synthetic-opportunity",
        opportunity_name="Synthetic Opportunity",
        opportunity_metadata={"name": "Synthetic Opportunity"},
        metadata_source_id="opportunity-metadata-v1",
        transcripts=[ResolvedTranscriptEvidence(
            evidence_id=TRANSCRIPT_ID,
            display_name="Acme-09.17.26.txt",
            content=content,
            sha256="a" * 64,
            byte_count=len(content),
            observed_at=datetime(2026, 9, 17, tzinfo=timezone.utc),
        )],
    )


def _executor(tmp_path, **kwargs) -> ClaudeCanonicalStateExecutor:
    kwargs.setdefault("executable", sys.executable)
    return ClaudeCanonicalStateExecutor(
        model="synthetic-model", forbidden_roots=[tmp_path], **kwargs
    )


def _set_version_command(monkeypatch, executor, code: str | None = None) -> None:
    script = code or f"print({(VERIFIED_CLAUDE_VERSION + ' (Claude Code)')!r})"
    monkeypatch.setattr(
        executor,
        "version_command",
        lambda executable=None: [sys.executable, "-c", script],
    )


def test_command_enforces_no_tools_no_mcp_no_prompts_and_no_sessions(tmp_path) -> None:
    executor = _executor(tmp_path)
    command = executor.command()
    for flag in (
        "-p", "--max-turns", "--restricted", "--safe-mode", "--tools", "--disallowedTools",
        "--permission-prompts", "--disable-slash-commands", "--no-session-persistence",
        "--no-chrome", "--strict-mcp-config", "--output-format", "--json-schema",
    ):
        assert flag in command
    assert command[command.index("--max-turns") + 1] == "1"
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--disallowedTools") + 1] == "mcp__*"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--permission-prompts") + 1] == "none"
    assert command[command.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "*,mcp__*" not in command
    assert all("SYNTHETIC_SENTINEL" not in token for token in command)
    assert all("Acme-09.17.26.txt" not in token for token in command)


def test_authorized_evidence_is_in_stdin_not_command_line(tmp_path) -> None:
    executor = _executor(tmp_path)
    stdin = executor._prompt(_request())
    assert b"SYNTHETIC_SENTINEL" in stdin
    assert all("SYNTHETIC_SENTINEL" not in arg for arg in executor.command())


def test_create_schema_and_prompt_require_all_overview_frameworks(tmp_path) -> None:
    executor = _executor(tmp_path)
    schema = executor.json_schema()
    assert {"business_case", "meddpicc", "stakeholders"} <= set(schema["required"])
    assert set(schema["$defs"]["BusinessCase"]["required"]) == {
        "current_state", "future_state", "negative_consequences", "positive_business_outcomes",
    }
    assert schema["$defs"]["Meddpicc"]["properties"]["dimensions"]["minItems"] == 8
    assert schema["$defs"]["Meddpicc"]["properties"]["dimensions"]["maxItems"] == 8
    assert schema["$defs"]["StakeholderMap"]["properties"]["stakeholders"]["maxItems"] == 24
    assert schema["$defs"]["Stakeholder"]["properties"]["evidence_refs"]["minItems"] == 1

    task = json.loads(executor._prompt(_request()))["task"]
    assert "all four business_case areas" in task
    assert "all eight meddpicc dimensions" in task
    assert "suggested_discovery" in task
    assert "stakeholders map" in task
    assert "never invent a name, title, category, influence" in task
    assert "champion, economic_buyer, and technical_decision_maker" in task
    assert "Generated outputs are not evidence" in task


def test_update_prompt_supplies_baseline_without_treating_it_as_evidence(tmp_path) -> None:
    executor = _executor(tmp_path)
    request = _request()
    update_request = CanonicalStateExecutionRequest(
        account=request.account,
        opportunity_slug=request.opportunity_slug,
        opportunity_name=request.opportunity_name,
        opportunity_metadata=request.opportunity_metadata,
        metadata_source_id=request.metadata_source_id,
        transcripts=request.transcripts,
        base_state=candidate(),
        base_version_id="a" * 32,
        base_revision=1,
    )
    payload = json.loads(executor._prompt(update_request))
    assert payload["base"]["prior_canonical_state"] == candidate().model_dump(mode="json")
    assert payload["base"]["version_id"] == "a" * 32
    assert "not as an evidence source" in payload["task"]
    assert [item["source_id"] for item in payload["selected_transcripts"]] == [TRANSCRIPT_ID]
    assert "generated output sentinel" not in json.dumps(payload).lower()
    assert str(tmp_path) not in json.dumps(payload)
    assert "all four business_case areas" in payload["task"]
    assert "all eight meddpicc dimensions" in payload["task"]
    assert "stakeholders map" in payload["task"]
    assert len(payload["base"]["prior_canonical_state"]["meddpicc"]["dimensions"]) == 8
    assert payload["base"]["prior_canonical_state"]["stakeholders"]["stakeholders"] == []


def test_extract_candidate_accepts_cli_string_and_fenced_json_envelopes() -> None:
    payload = json.dumps(candidate().model_dump(mode="json"))
    for outer in (
        {"structured_output": payload},
        {"result": f"```json\n{payload}\n```"},
    ):
        parsed = ClaudeCanonicalStateExecutor._extract_candidate(json.dumps(outer).encode("utf-8"))
        assert parsed == candidate()


@pytest.mark.asyncio
async def test_executor_accepts_exact_version_and_records_it_in_result(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path)
    _set_version_command(monkeypatch, executor)
    structured = json.dumps({"structured_output": candidate().model_dump(mode="json")})
    code = f"import sys; sys.stdin.buffer.read(); print({structured!r})"
    monkeypatch.setattr(executor, "command", lambda executable=None: [sys.executable, "-c", code])
    result = await executor.execute(_request())
    assert result.candidate == candidate()
    assert result.cli_version == VERIFIED_CLAUDE_VERSION


@pytest.mark.asyncio
async def test_version_check_receives_no_evidence_or_stdin(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path)
    version_code = (
        "import sys; data=sys.stdin.buffer.read(); "
        f"print({(VERIFIED_CLAUDE_VERSION + ' (Claude Code)')!r} if not data else 'EVIDENCE_LEAK')"
    )
    _set_version_command(monkeypatch, executor, version_code)
    structured = json.dumps({"structured_output": candidate().model_dump(mode="json")})
    run_code = f"import sys; sys.stdin.buffer.read(); print({structured!r})"
    monkeypatch.setattr(executor, "command", lambda executable=None: [sys.executable, "-c", run_code])
    result = await executor.execute(_request())
    assert result.cli_version == VERIFIED_CLAUDE_VERSION


@pytest.mark.asyncio
async def test_missing_executable_fails_before_prompt_construction(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path, executable="definitely-missing-claude-slice2a")
    monkeypatch.setattr(executor, "_prompt", lambda request: pytest.fail("evidence prompt was constructed"))
    with pytest.raises(CanonicalStateExecutionError) as exc:
        await executor.execute(_request())
    assert exc.value.code == "runtime_unavailable"
    assert "definitely-missing" not in exc.value.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version_code", "expected_code"),
    [
        ("print('2.1.273 (Claude Code)')", "runtime_version_unsupported"),
        ("print('Claude Code unknown')", "runtime_version_invalid"),
    ],
)
async def test_unsupported_and_malformed_versions_fail_closed_before_evidence(
    tmp_path, monkeypatch, version_code, expected_code
) -> None:
    executor = _executor(tmp_path)
    _set_version_command(monkeypatch, executor, version_code)
    monkeypatch.setattr(executor, "_prompt", lambda request: pytest.fail("evidence prompt was constructed"))
    with pytest.raises(CanonicalStateExecutionError) as exc:
        await executor.execute(_request())
    assert exc.value.code == expected_code
    assert "SYNTHETIC_SENTINEL" not in exc.value.detail


@pytest.mark.asyncio
async def test_version_check_enforces_timeout_and_output_limits(tmp_path, monkeypatch) -> None:
    timeout_executor = _executor(tmp_path, version_timeout_seconds=0.05)
    _set_version_command(monkeypatch, timeout_executor, "import time; time.sleep(1)")
    with pytest.raises(CanonicalStateExecutionError) as timeout:
        await timeout_executor.execute(_request())
    assert timeout.value.code == "runtime_version_timeout"

    output_executor = _executor(tmp_path, max_version_stdout_bytes=32)
    _set_version_command(monkeypatch, output_executor, "print('x' * 1000)")
    with pytest.raises(CanonicalStateExecutionError) as oversized:
        await output_executor.execute(_request())
    assert oversized.value.code == "runtime_version_output_too_large"


@pytest.mark.asyncio
async def test_version_check_never_exposes_stderr_or_paths(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path)
    sentinel = f"RAW_STDERR {tmp_path} SYNTHETIC_SENTINEL"
    code = f"import sys; sys.stderr.write({sentinel!r}); raise SystemExit(7)"
    _set_version_command(monkeypatch, executor, code)
    with pytest.raises(CanonicalStateExecutionError) as exc:
        await executor.execute(_request())
    assert exc.value.code == "runtime_version_failed"
    assert sentinel not in exc.value.detail
    assert str(tmp_path) not in exc.value.detail
    assert "SYNTHETIC_SENTINEL" not in str(exc.value)


@pytest.mark.asyncio
async def test_executor_enforces_timeout_and_output_limits(tmp_path, monkeypatch) -> None:
    timeout_executor = _executor(tmp_path, timeout_seconds=0.05)
    _set_version_command(monkeypatch, timeout_executor)
    monkeypatch.setattr(
        timeout_executor,
        "command",
        lambda executable=None: [sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(1)"],
    )
    with pytest.raises(CanonicalStateExecutionError) as timeout:
        await timeout_executor.execute(_request())
    assert timeout.value.code == "runtime_timeout"

    output_executor = _executor(tmp_path, max_stdout_bytes=32)
    _set_version_command(monkeypatch, output_executor)
    monkeypatch.setattr(
        output_executor,
        "command",
        lambda executable=None: [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); print('x'*1000)"],
    )
    with pytest.raises(CanonicalStateExecutionError) as oversized:
        await output_executor.execute(_request())
    assert oversized.value.code == "stdout_too_large"


@pytest.mark.asyncio
async def test_executor_rejects_oversized_input_after_safe_version_check(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path, max_stdin_bytes=10)
    _set_version_command(monkeypatch, executor)
    with pytest.raises(CanonicalStateExecutionError) as exc:
        await executor.execute(_request())
    assert exc.value.code == "input_too_large"
