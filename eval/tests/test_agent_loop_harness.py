"""Deterministic tests for the typed-tool agent loop harness.

These tests prove the runtime can start without an Anthropic API key, route
model calls through a worker proxy, execute only allowlisted typed tools, read
only manifest-authorized files, write output to the sandbox workspace, and honour
cancellation and execution deadlines.
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

from webapp.hosted.agent_loop_harness import (
    BaseContentBlock,
    MessageResponse,
    TextBlock,
    ToolResultBlock,
    TypedToolRuntime,
)
from webapp.hosted.runtime_contract import (
    Allowlist,
    CancellationToken,
    InputManifest,
    NetworkDestination,
    RuntimeJob,
    SandboxOutputSidecar,
)


TEST_MODEL = "claude-sonnet-4-6"


def _write_inputs(
    input_dir: Path,
    transcript_text: str = "Sample transcript",
    priors: dict[str, str] | None = None,
    unlisted: dict[str, str] | None = None,
) -> None:
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")
    for name, text in (priors or {}).items():
        (input_dir / name).write_text(text, encoding="utf-8")
    for name, text in (unlisted or {}).items():
        (input_dir / name).write_text(text, encoding="utf-8")


def _job(
    tmp_path: Path,
    transcript_text: str = "Sample transcript",
    priors: dict[str, str] | None = None,
    unlisted: dict[str, str] | None = None,
    tools: set[str] | None = None,
) -> RuntimeJob:
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    _write_inputs(input_dir, transcript_text, priors, unlisted)
    output_dir.mkdir(parents=True, exist_ok=True)

    tid = uuid.uuid4()
    manifest = InputManifest(
        transcript_id=tid,
        transcript_ref="transcript.txt",
        account_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        prior_context_refs=frozenset(priors or {}),
    )
    return RuntimeJob(
        job_id=uuid.uuid4(),
        org_id=manifest.org_id,
        account_id=manifest.account_id,
        transcript_id=tid,
        requester_id=uuid.uuid4(),
        requested_model=TEST_MODEL,
        skill_version="1.0",
        input_manifest=manifest,
        execution_deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        input_workspace=str(input_dir),
        output_workspace=str(output_dir),
        allowlist=Allowlist(
            tools=tools or {"read_transcript", "write_output", "finish"},
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


def _model_response(content: list[dict[str, Any]], usage: dict[str, int] | None = None, stop_reason: str = "tool_use") -> httpx.Response:
    payload = {
        "id": "msg_" + uuid.uuid4().hex[:8],
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": TEST_MODEL,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "stop_details": None,
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
        # The advertised tools must be exactly the allowlisted subset, sorted by name.
        advertised = [t["name"] for t in body.get("tools", [])]
        assert advertised == sorted(advertised)

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
async def test_typed_tool_runtime_races_execution_deadline(tmp_path: Path) -> None:
    """An in-flight model request must fail when the execution deadline expires."""

    class _BlockingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            await asyncio.Event().wait()
            return httpx.Response(200, json={})  # pragma: no cover

    client = httpx.AsyncClient(transport=_BlockingTransport(), base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)
    job = job.model_copy(update={"execution_deadline": datetime.now(timezone.utc) + timedelta(seconds=0.05)})

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert result.failure.category == "timeout"
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
            [{"type": "tool_use", "id": "toolu_report", "name": "report_failure", "input": {"category": "x"}}]
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
async def test_typed_tool_runtime_rejects_missing_transcript(tmp_path: Path) -> None:
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    job = _job(tmp_path)
    job = job.model_copy(update={"input_manifest": job.input_manifest.model_copy(update={"transcript_ref": "missing.txt"})})

    runtime = TypedToolRuntime()
    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert "transcript" in result.failure.message.lower()


def test_typed_tool_runtime_ignores_unlisted_input_files(tmp_path: Path) -> None:
    """The runtime must only read prior-context files listed in the manifest."""
    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "transcript.txt").write_text("Transcript text", encoding="utf-8")
    (input_dir / "prior-123.md").write_text("Authorized prior", encoding="utf-8")
    (input_dir / "secret.md").write_text("Should not be read", encoding="utf-8")

    manifest = InputManifest(
        transcript_id=uuid.uuid4(),
        transcript_ref="transcript.txt",
        account_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        prior_context_refs={"prior-123.md"},
    )
    job = RuntimeJob(
        job_id=uuid.uuid4(),
        org_id=manifest.org_id,
        account_id=manifest.account_id,
        transcript_id=manifest.transcript_id,
        requester_id=uuid.uuid4(),
        requested_model=TEST_MODEL,
        input_manifest=manifest,
        execution_deadline=datetime.now(timezone.utc) + timedelta(minutes=5),
        input_workspace=str(input_dir),
        output_workspace=str(tmp_path / "output"),
        allowlist=Allowlist(
            tools={"read_transcript", "read_prior_context", "list_priors", "write_output", "finish"},
            network={NetworkDestination(host="worker-proxy", scheme="http", port=8080)},
        ),
    )

    runtime = TypedToolRuntime()
    assert runtime._transcript_path(job) == input_dir / "transcript.txt"
    prior_paths = runtime._prior_paths(job)
    assert prior_paths == [input_dir / "prior-123.md"]
    assert (input_dir / "secret.md") not in prior_paths


@pytest.mark.asyncio
async def test_typed_tool_runtime_reads_prior_context_by_manifest_ref(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        assistant_messages = [m for m in body.get("messages", []) if m.get("role") == "assistant"]
        tool_inputs = {
            item.get("name"): item.get("input", {})
            for m in assistant_messages
            for item in m.get("content", [])
            if item.get("type") == "tool_use"
        }
        if "read_prior_context" in tool_inputs:
            assert tool_inputs["read_prior_context"].get("ref") == "prior-123.md"
            return _model_response(
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_write",
                        "name": "write_output",
                        "input": {
                            "markdown": "# Prior read\n\n**Date:** June 11, 2026\n\n### At a Glance\n- **Call type:** Follow-up",
                            "sidecar": {
                                "skill": "post-call",
                                "skill_version": "1.0",
                                "mode": "full",
                                "title": "Prior read",
                                "date": "June 11, 2026",
                                "source_coverage": "Read transcript.txt in full (10 / 10 lines).",
                            },
                        },
                    }
                ]
            )
        return _model_response(
            [
                {"type": "tool_use", "id": "toolu_prior", "name": "read_prior_context", "input": {"ref": "prior-123.md"}},
            ]
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(
        tmp_path,
        priors={"prior-123.md": "Authorized prior context"},
        tools={"read_transcript", "read_prior_context", "write_output", "finish"},
    )

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is None
    assert result.output_artifact is not None


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_malformed_write_output(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _model_response(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_write",
                    "name": "write_output",
                    "input": {"markdown": "# Bad"},
                }
            ]
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert result.failure.category == "tool_input_error"


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_invalid_sidecar_in_write_output(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _model_response(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_write",
                    "name": "write_output",
                    "input": {
                        "markdown": "# Bad",
                        "sidecar": {"skill": 123},
                    },
                }
            ]
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert "sidecar" in result.failure.message.lower()


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
    assert result.failure.category == "tool_input_error"


@pytest.mark.asyncio
async def test_typed_tool_runtime_handles_max_tokens_stop_reason(tmp_path: Path) -> None:
    transport = httpx.MockTransport(lambda r: _model_response([{"type": "text", "text": "truncated"}], stop_reason="max_tokens"))
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert result.failure.category == "model_error"


@pytest.mark.asyncio
async def test_typed_tool_runtime_handles_refusal_stop_reason(tmp_path: Path) -> None:
    transport = httpx.MockTransport(
        lambda r: _model_response(
            [{"type": "text", "text": "refused"}],
            stop_reason="refusal",
            usage={"input_tokens": 10, "output_tokens": 2},
        )
    )
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert result.failure.category == "model_error"


@pytest.mark.asyncio
async def test_typed_tool_runtime_parses_real_messages_api_response_shape(tmp_path: Path) -> None:
    """A worker-proxy pass-through response with cache usage and stop_sequence must parse."""
    fixture = {
        "id": "msg_01234",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "planning...", "signature": "sig"},
            {"type": "text", "text": "Okay, I will write the output."},
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
            },
        ],
        "model": TEST_MODEL,
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "stop_details": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 10,
            "cache_read_input_tokens": 200,
        },
    }

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
            return httpx.Response(200, json=fixture)
        return _model_response(
            [{"type": "tool_use", "id": "toolu_finish", "name": "finish", "input": {}}],
            usage={"input_tokens": 10, "output_tokens": 5},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is None
    assert result.output_artifact is not None
    assert result.sidecar is not None


def test_message_response_parses_anthropic_shape() -> None:
    payload = {
        "id": "msg_abc",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
        "model": TEST_MODEL,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "stop_details": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    msg = MessageResponse(**payload)
    assert msg.stop_reason == "end_turn"
    assert isinstance(msg.content[0], TextBlock | BaseContentBlock)


@pytest.mark.asyncio
async def test_typed_tool_runtime_rejects_sidecar_skill_version_mismatch(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _model_response(
            [
                {
                    "type": "tool_use",
                    "id": "toolu_write",
                    "name": "write_output",
                    "input": {
                        "markdown": "# Call Summary",
                        "sidecar": {
                            "skill": "post-call",
                            "skill_version": "2.0",
                            "mode": "full",
                            "title": "Call Summary",
                            "date": "June 11, 2026",
                            "source_coverage": "Read transcript.txt in full (612 / 612 lines).",
                        },
                    },
                }
            ]
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://worker-proxy:8080")
    runtime = TypedToolRuntime(model_client=client)
    job = _job(tmp_path)
    job = job.model_copy(update={"skill_version": "1.0"})

    result = await runtime.execute(job, _FixedToken())

    assert result.failure is not None
    assert "skill_version" in result.failure.message.lower()
