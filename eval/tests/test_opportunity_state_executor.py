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
    return ClaudeCanonicalStateExecutor(
        model="synthetic-model", forbidden_roots=[tmp_path], **kwargs
    )


def test_command_enforces_no_tools_no_mcp_no_prompts_and_no_sessions(tmp_path) -> None:
    executor = _executor(tmp_path)
    command = executor.command()
    joined = " ".join(command)
    for flag in (
        "-p", "--restricted", "--safe-mode", "--tools", "--disallowedTools",
        "--permission-prompts", "--disable-slash-commands", "--no-session-persistence",
        "--no-chrome", "--strict-mcp-config", "--output-format", "--json-schema",
    ):
        assert flag in command
    assert "dontAsk" in command
    assert '{"mcpServers":{}}' in command
    assert "SYNTHETIC_SENTINEL" not in joined
    assert "Acme-09.17.26.txt" not in joined
    assert "--max-turns" not in command  # unsupported by verified Claude Code 2.1.272


def test_authorized_evidence_is_in_stdin_not_command_line(tmp_path) -> None:
    executor = _executor(tmp_path)
    stdin = executor._prompt(_request())
    assert b"SYNTHETIC_SENTINEL" in stdin
    assert all("SYNTHETIC_SENTINEL" not in arg for arg in executor.command())


def test_extract_candidate_accepts_cli_string_and_fenced_json_envelopes() -> None:
    payload = json.dumps(candidate().model_dump(mode="json"))
    for outer in (
        {"structured_output": payload},
        {"result": f"```json\n{payload}\n```"},
    ):
        parsed = ClaudeCanonicalStateExecutor._extract_candidate(json.dumps(outer).encode("utf-8"))
        assert parsed == candidate()


@pytest.mark.asyncio
async def test_executor_accepts_bounded_structured_output_from_isolated_cwd(tmp_path, monkeypatch) -> None:
    executor = _executor(tmp_path)
    structured = json.dumps({"structured_output": candidate().model_dump(mode="json")})
    code = f"import sys; sys.stdin.buffer.read(); print({structured!r})"
    monkeypatch.setattr(executor, "command", lambda: [sys.executable, "-c", code])
    result = await executor.execute(_request())
    assert result.candidate == candidate()


@pytest.mark.asyncio
async def test_executor_enforces_timeout_and_output_limits(tmp_path, monkeypatch) -> None:
    timeout_executor = _executor(tmp_path, timeout_seconds=0.05)
    monkeypatch.setattr(
        timeout_executor, "command", lambda: [sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(1)"]
    )
    with pytest.raises(CanonicalStateExecutionError) as timeout:
        await timeout_executor.execute(_request())
    assert timeout.value.code == "runtime_timeout"

    output_executor = _executor(tmp_path, max_stdout_bytes=32)
    monkeypatch.setattr(
        output_executor, "command", lambda: [sys.executable, "-c", "import sys; sys.stdin.buffer.read(); print('x'*1000)"]
    )
    with pytest.raises(CanonicalStateExecutionError) as oversized:
        await output_executor.execute(_request())
    assert oversized.value.code == "stdout_too_large"


@pytest.mark.asyncio
async def test_executor_rejects_oversized_input_before_launch(tmp_path) -> None:
    executor = _executor(tmp_path, max_stdin_bytes=10)
    with pytest.raises(CanonicalStateExecutionError) as exc:
        await executor.execute(_request())
    assert exc.value.code == "input_too_large"
