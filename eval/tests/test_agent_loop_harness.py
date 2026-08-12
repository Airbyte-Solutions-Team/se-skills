"""Deterministic tests for the typed-tool agent loop harness.

These tests prove the runtime can start without an Anthropic API key, route
model calls through a worker proxy, execute only allowlisted typed tools, write
output to the sandbox workspace, and honour cancellation.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from webapp.hosted.agent_loop_harness import MessageResponse, TextBlock, ToolResultBlock, TypedToolRuntime
from webapp.hosted.runtime_contract import (
    Allowlist,
    CancellationToken,
    InputManifest,
    NetworkDestination,
    RuntimeJob,
    SandboxOutputSidecar,
)


TEST_MODEL = "claude-sonnet-4-6"


def _job(tmp_path: Path, transcript_text: str = "Sample transcript") -> RuntimeJob:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")
    (input_dir / "prior-123.md").write_text("Prior context", encoding="utf-8")

    tid = uuid.uuid4()
    manifest = InputManifest(transcript_id=tid, account_id=uuid.uuid4(), org_id=uuid.uuid4())
    return RuntimeJob(
        job_id=uuid.uuid4(),
        org_id=manifest.org_id,
        account_id=manifest.account_id,
        transcript_id=tid,
        requester_id=uuid.uuid4(),
        requested_model=TEST_MODEL,
        input_manifest=manifest,
        execution_deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        input_workspace=str(input_dir),
        output_workspace=str(output_dir),
        allowlist=Allowlist(
            tools={"read_transcript", "write_output", "finish"},
            network={NetworkDestination(host="worker-proxy", scheme="http", port=8080)},
        ),
    )


class _FixedToken(CancellationToken):
    """Host-side cancellation token for tests."""

    def __init__(self, cancelled: bool = False) -> None:
        self._cancelled = cancelled
        self._event = asyncio.Event()
        if cancelled:
            self._event.set()

    def is_cancelled(self) -> bool:
        return self._cancelled

    async def wait(self) -> None:
        await self._event.wait()

    def cancel(self) -> None:
        self._cancelled = True
        self._event.set()


def _model_response(content: list[dict[str, Any]], usage: dict[str, int] | None = None) -> httpx.Response:
    payload = {
        "id": "msg_" + uuid.uuid4().hex[:8],
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": TEST_MODEL,
        "stop_reason": "tool_use",
        "usage": usage or {"input_tokens": 100, "output_tokens": 50},
    }
    return httpx.Response(200, json=payload)


def _build_mock_transport() -> httpx.MockTransport:
    """Return a transport that drives a two-turn write_output -> finish conversation."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        # Assert the sandbox is not sending a real Anthropic API key.
        assert "x-api-key" not in {k.lower() for k in request.headers.keys()}, "Sandbox must not send x-api-key"
        assert request.url.path == "/v1/messages"
        assert request.url.host == "worker-proxy"

        body = json.loads(request.content.decode("utf-8"))
        assert body.get("model") == TEST_MODEL
        # The advertised tools must be exactly the allowlisted subset.
        advertised = {t["name"] for t in body.get("tools", [])}
        assert advertised == {"read_transcript", "write_output", "finish"}

        assistant_messages = [m for m in body.get("messages", []) if m.get("role") == "assistant"]
        tool_names = {
            item.get("name")
            for m in assistant_messages
            for item in m.get("content", [])
            if item.get("type") == "tool_use"
        }
        if "write_output" not in tool_names:
            return _model_response(
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_write",
                        "name": "write_output",
                        "input": {
                            "markdown": "# Call Summary\n\n**Date:** June 11, 2026\n\n### At a Glance\n- **Call type:** Discovery",
                            "sidecar": {
                                "skill": "post-call",
                                "skill_version": "1.0",
                                "mode": "full",
                                "title": "Call Summary",
                                "date": "June 11, 2026",
                                "source_coverage": "Read transcript.txt in full (612 / 612 lines).",
                            },
                        },
                    }
                ]
            )
        return _model_response(
            [{"type": "tool_use", "id": "toolu_finish", "name": "finish", "input": {}}],
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    transport = httpx.MockTransport(handler)
    transport._calls = calls  # type: ignore[attr-defined]
    return transport


@pytest.mark.asyncio
async def test_typed_tool_runtime_routes_calls_through_proxy(tmp_path: Path) -> None:
    transport = _build_mock_transport()
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.output_artifact is not None
    assert "Call Summary" in result.output_artifact
    assert result.sidecar is not None
    assert result.sidecar.skill == "post-call"
    assert result.sidecar.title == "Call Summary"
    assert result.execution_metadata.model == TEST_MODEL
    assert result.failure is None
    assert len(transport._calls) == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_typed_tool_runtime_cancels_loop(tmp_path: Path) -> None:
    transport = _build_mock_transport()
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken(cancelled=True))

    assert result.failure is not None
    assert result.failure.category == "cancelled"
    assert result.output_artifact is None


@pytest.mark.asyncio
async def test_typed_tool_runtime_cancels_blocked_model_request(tmp_path: Path) -> None:
    """A cancellation that happens while a model request is in-flight must stop the loop."""

    class _BlockingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            # Never return: the cancellation token should interrupt this.
            await asyncio.Event().wait()
            return httpx.Response(200, json={})  # pragma: no cover

    client = httpx.AsyncClient(transport=_BlockingTransport(), base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    token = _FixedToken()

    async def cancel_later() -> None:
        await asyncio.sleep(0.05)
        token.cancel()

    task = asyncio.create_task(runtime.execute(job, token))
    await asyncio.gather(cancel_later(), task)
    result = task.result()

    assert result.failure is not None
    assert result.failure.category == "cancelled"
    assert result.output_artifact is None


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_forbidden_tool(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _model_response(
            [{"type": "tool_use", "id": "toolu_bash", "name": "bash", "input": {"command": "id"}}]
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert result.failure.category == "forbidden_tool"


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_disabled_tool_not_in_allowlist(tmp_path: Path) -> None:
    """A tool in the trusted registry but not in the job allowlist must still be rejected."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _model_response(
            [{"type": "tool_use", "id": "toolu_report", "name": "report_failure", "input": {"category": "x", "message": "y"}}]
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert result.failure.category == "forbidden_tool"
    assert "report_failure" in result.failure.message


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_multiple_network_destinations(tmp_path: Path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)
    job = job.model_copy(update={
        "allowlist": Allowlist(
            tools={"read_transcript", "write_output", "finish"},
            network={
                NetworkDestination(host="worker-proxy", scheme="http", port=8080),
                NetworkDestination(host="api.anthropic.com", scheme="https", port=443),
            },
        )
    })

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert "exactly one network destination" in result.failure.message.lower()


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_injected_client_origin_mismatch(tmp_path: Path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), base_url="http://other-proxy:9999")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert "base_url" in result.failure.message.lower() or "proxy" in result.failure.message.lower()


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_missing_requested_model(tmp_path: Path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})), base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)
    job = job.model_copy(update={"requested_model": None})

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert "requested_model" in result.failure.message.lower()


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_missing_sidecar(tmp_path: Path) -> None:
    """A write_output that does not produce a sidecar.json must fail the attempt."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        tool_names = {
            item.get("name")
            for m in body.get("messages", [])
            if m.get("role") == "assistant"
            for item in m.get("content", [])
            if item.get("type") == "tool_use"
        }
        if "write_output" not in tool_names:
            return _model_response(
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_write",
                        "name": "write_output",
                        "input": {
                            "markdown": "# Call Summary",
                        },
                    }
                ]
            )
        return _model_response(
            [{"type": "tool_use", "id": "toolu_finish", "name": "finish", "input": {}}]
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert "sidecar" in result.failure.message.lower()


def test_message_response_parses_anthropic_shape() -> None:
    payload = {
        "id": "msg_abc",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
        "model": TEST_MODEL,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    msg = MessageResponse(**payload)
    assert msg.stop_reason == "end_turn"
    assert isinstance(msg.content[0], TextBlock)
