"""Deterministic tests for the worker-side Anthropic Messages API proxy.

These tests do not make real Anthropic calls; they use `httpx.MockTransport` to
feed the proxy a fake upstream. They also do not need `runsc` or a database.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import httpx
import pytest
import jwt

from webapp.hosted.model_proxy import ModelProxy, ProxyConfig
from webapp.hosted.runtime_contract import (
    Allowlist,
    InputManifest,
    NetworkDestination,
    RuntimeJob,
)


def _make_job(
    deadline: datetime | None = None,
    requested_model: str = "claude-sonnet-4-6",
    attempt_number: int = 1,
    tools: frozenset[str] | None = None,
) -> RuntimeJob:
    job_id = uuid4()
    org_id = uuid4()
    account_id = uuid4()
    transcript_id = uuid4()
    return RuntimeJob(
        job_id=job_id,
        org_id=org_id,
        account_id=account_id,
        transcript_id=transcript_id,
        requester_id=uuid4(),
        requested_model=requested_model,
        attempt_number=attempt_number,
        input_manifest=InputManifest(
            transcript_id=transcript_id,
            transcript_ref="transcript.txt",
            account_id=account_id,
            org_id=org_id,
        ),
        execution_deadline=deadline
        or datetime.now(tz=timezone.utc) + timedelta(minutes=5),
        allowlist=Allowlist(
            tools=tools or frozenset({"write_output", "finish"}),
            network=frozenset([NetworkDestination(host="worker-proxy", scheme="http")]),
        ),
    )


def _upstream_response(request: httpx.Request) -> httpx.Response:
    """Return a minimal Anthropic Messages API response with tool-use content."""
    body = json.loads(request.content) if request.content else {}
    return httpx.Response(
        200,
        json={
            "id": "msg-1",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-sonnet-4-6"),
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu1",
                    "name": "write_output",
                    "input": {
                        "markdown": "# Test",
                        "sidecar": {
                            "skill": "post-call",
                            "skill_version": "1.0",
                            "mode": "full",
                        },
                    },
                }
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    )


def _request_headers(job: RuntimeJob | None, token: str, attempt_id: str, seq: int = 1) -> dict[str, str]:
    """Build the identity-bound headers the sandbox runtime must echo."""
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "content-type": "application/json",
        "x-attempt-number": str(job.attempt_number) if job else "1",
        "x-attempt-id": attempt_id,
        "x-request-seq": str(seq),
    }
    if job:
        headers["x-job-id"] = str(job.job_id)
    return headers


def _proxy(
    secret: str = "a" * 32,
    key: str = "test-key",
    url: str = "https://api.anthropic.com",
    version: str = "2023-06-01",
    upstream: Any | None = None,
) -> ModelProxy:
    cfg = ProxyConfig(
        secret=secret,
        anthropic_api_key=key,
        anthropic_api_url=url,
        anthropic_api_version=version,
    )
    upstream_client = upstream or httpx.AsyncClient(
        transport=httpx.MockTransport(_upstream_response)
    )
    return ModelProxy(proxy_config=cfg, anthropic_client=upstream_client)


def _issue(proxy: ModelProxy, job: RuntimeJob) -> tuple[str, str, str]:
    """Issue a capability and update the job with the returned attempt_id."""
    return proxy.issue_capability(job, attempt_number=job.attempt_number)


@pytest.fixture
def valid_job() -> RuntimeJob:
    return _make_job()


@pytest.mark.asyncio
async def test_valid_capability_reaches_upstream(valid_job: RuntimeJob) -> None:
    """A valid, job-scoped capability is accepted and forwarded to the fake upstream."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=json.dumps({"messages": [{"role": "user", "content": "hi"}]}).encode(),
    )
    response = await proxy.handle(request)
    assert response.status_code == 200
    data = response.json()
    assert data["model"] == "claude-sonnet-4-6"
    assert data["usage"]["input_tokens"] == 10


@pytest.mark.asyncio
async def test_missing_authorization_rejected() -> None:
    """Requests without a bearer token are rejected before reaching the upstream."""
    proxy = _proxy()
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers={"content-type": "application/json"},
        content=b'{"messages": []}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_expired_capability_rejected(valid_job: RuntimeJob) -> None:
    """A capability whose `exp` claim is in the past is rejected."""
    proxy = _proxy()
    expired_job = valid_job.model_copy(
        update={
            "execution_deadline": datetime.now(tz=timezone.utc) - timedelta(minutes=10)
        }
    )
    token, _jti, attempt_id = _issue(proxy, expired_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(expired_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_model_override_rejected(valid_job: RuntimeJob) -> None:
    """The sandbox cannot override the worker-authorized model."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=json.dumps(
            {"model": "claude-opus-5", "messages": [{"role": "user", "content": "hi"}]}
        ).encode(),
    )
    response = await proxy.handle(request)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_wrong_endpoint_rejected(valid_job: RuntimeJob) -> None:
    """A capability bound to one Anthropic endpoint cannot be used on another route."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/batch",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cross_job_capability_rejected(valid_job: RuntimeJob) -> None:
    """A token signed for a different job ID is rejected when the wrong ID is echoed."""
    proxy = _proxy()
    other_job = _make_job()
    token, _jti, _attempt_id = _issue(proxy, other_job)
    # Request carries a token for other_job but echoes valid_job's identity.
    headers = _request_headers(valid_job, token, "wrong-attempt-id")
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=headers,
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_wrong_attempt_rejected(valid_job: RuntimeJob) -> None:
    """A request echoing the wrong attempt number is rejected."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    headers = _request_headers(valid_job, token, attempt_id)
    headers["x-attempt-number"] = "999"
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=headers,
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_sandbox_auth_headers_stripped(valid_job: RuntimeJob) -> None:
    """Sandbox-supplied provider headers are removed before forwarding."""
    captured: dict[str, Any] = {}

    def capture(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        return _upstream_response(request)

    proxy = _proxy(upstream=httpx.AsyncClient(transport=httpx.MockTransport(capture)))
    token, _jti, attempt_id = _issue(proxy, valid_job)
    headers = _request_headers(valid_job, token, attempt_id)
    headers["x-api-key"] = "leaked"
    headers["anthropic-version"] = "2099-01-01"
    headers["x-forwarded-for"] = "1.2.3.4"
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=headers,
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 200
    forwarded = captured["headers"]
    assert forwarded["x-api-key"] == "test-key"
    assert forwarded["anthropic-version"] == "2023-06-01"
    assert forwarded.get("x-forwarded-for") is None


@pytest.mark.asyncio
async def test_request_size_limit(valid_job: RuntimeJob) -> None:
    """Oversized request bodies are rejected without forwarding."""
    proxy = _proxy()
    proxy.cfg = proxy.cfg.model_copy(update={"max_body_bytes": 10})
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hello world"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_upstream_timeout_returns_gateway_error(valid_job: RuntimeJob) -> None:
    """A slow upstream that triggers an `httpx.TimeoutException` causes a 504."""

    def timeout_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("read timeout")

    proxy = _proxy(
        upstream=httpx.AsyncClient(transport=httpx.MockTransport(timeout_transport))
    )
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 504


@pytest.mark.asyncio
async def test_response_redaction(valid_job: RuntimeJob) -> None:
    """The proxy strips upstream fields that the sandbox runtime does not need."""

    def verbose_upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "msg-1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "ok"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "citations": [{"index": 1}],
                "thinking": "private",
            },
        )

    proxy = _proxy(upstream=httpx.AsyncClient(transport=httpx.MockTransport(verbose_upstream)))
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    data = response.json()
    assert "citations" not in data
    assert "thinking" not in data
    assert data["usage"]["input_tokens"] == 1


@pytest.mark.asyncio
async def test_capability_claims(valid_job: RuntimeJob) -> None:
    """Issued tokens contain the expected bound claims."""
    proxy = _proxy(secret="b" * 32)
    token, jti, attempt_id = proxy.issue_capability(
        valid_job,
        attempt_number=2,
    )
    claims = jwt.decode(token, "b" * 32, algorithms=["HS256"])
    assert claims["job_id"] == str(valid_job.job_id)
    assert claims["attempt_number"] == 2
    assert claims["attempt_id"] == attempt_id
    assert claims["model"] == "claude-sonnet-4-6"
    assert claims["api_version"] == "2023-06-01"
    assert claims["endpoint"] == "https://api.anthropic.com/v1/messages"
    assert claims["jti"] == jti


@pytest.mark.asyncio
async def test_attempt_id_mismatch_rejected(valid_job: RuntimeJob) -> None:
    """A request with the wrong opaque attempt identity is rejected."""
    proxy = _proxy()
    token, _jti, _attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, "wrong-attempt-id"),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_replay_rejected(valid_job: RuntimeJob) -> None:
    """Replaying the same request sequence is rejected."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    body = b'{"messages": [{"role":"user","content":"hi"}]}'
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id, seq=1),
        content=body,
    )
    response1 = await proxy.handle(request)
    assert response1.status_code == 200

    replay = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id, seq=1),
        content=body,
    )
    response2 = await proxy.handle(replay)
    assert response2.status_code == 403


@pytest.mark.asyncio
async def test_sequence_gaps_rejected(valid_job: RuntimeJob) -> None:
    """Out-of-order or skipped sequence numbers are rejected."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id, seq=2),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_long_deadline_valid(valid_job: RuntimeJob) -> None:
    """Capability lifetime covers the immutable attempt deadline, not a 5-minute cap."""
    proxy = _proxy()
    long_job = valid_job.model_copy(
        update={
            "execution_deadline": datetime.now(tz=timezone.utc) + timedelta(minutes=10)
        }
    )
    token, _jti, attempt_id = _issue(proxy, long_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(long_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_stream_rejected(valid_job: RuntimeJob) -> None:
    """The proxy rejects streaming requests before forwarding."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}], "stream": true}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_invalid_max_tokens_rejected(valid_job: RuntimeJob) -> None:
    """`max_tokens` outside the configured cap is rejected."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}], "max_tokens": 999999}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_oversized_upstream_response_rejected(valid_job: RuntimeJob) -> None:
    """Responses larger than the configured cap are rejected with a gateway error."""

    def huge_upstream(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{" + b" " * (proxy.cfg.max_response_bytes + 1) + b"}",
        )

    proxy = _proxy(upstream=httpx.AsyncClient(transport=httpx.MockTransport(huge_upstream)))
    proxy.cfg = proxy.cfg.model_copy(update={"max_response_bytes": 100})
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 502


@pytest.mark.asyncio
async def test_forbidden_tool_rejected(valid_job: RuntimeJob) -> None:
    """A request advertising a tool not in the capability allowlist is rejected."""
    proxy = _proxy()
    token, _jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=json.dumps(
            {
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "name": "bash",
                        "description": "run shell commands",
                        "input_schema": {"type": "object"},
                    }
                ],
            }
        ).encode(),
    )
    response = await proxy.handle(request)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_concurrency_limit_enforced(valid_job: RuntimeJob) -> None:
    """The proxy caps concurrent upstream requests."""

    barrier = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        barrier.set()
        await asyncio.sleep(1)
        return _upstream_response(request)

    proxy = _proxy(
        upstream=httpx.AsyncClient(transport=httpx.MockTransport(slow))
    )
    proxy.cfg = proxy.cfg.model_copy(update={"max_concurrent_requests": 1})
    token1, _jti1, attempt_id1 = _issue(proxy, valid_job)
    job2 = _make_job()
    token2, _jti2, attempt_id2 = _issue(proxy, job2)

    req1 = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token1, attempt_id1),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    req2 = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(job2, token2, attempt_id2),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )

    task1 = asyncio.create_task(proxy.handle(req1))
    # Wait until the first upstream handler starts to ensure the semaphore is held.
    await barrier.wait()
    task2 = asyncio.create_task(proxy.handle(req2))
    # Give task2 a chance to run; it should be queued, not completed.
    done, _pending = await asyncio.wait({task1, task2}, timeout=0.1)
    assert not done

    task1.cancel()
    task2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(task1, task2, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancellation_interrupts_upstream(valid_job: RuntimeJob) -> None:
    """Cancelling the attempt interrupts an in-flight upstream request."""

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(60)
        return _upstream_response(request)

    proxy = _proxy(
        upstream=httpx.AsyncClient(transport=httpx.MockTransport(slow))
    )
    token, jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )

    task = asyncio.create_task(proxy.handle(request))
    await asyncio.sleep(0.05)
    proxy.cancel_session(jti)
    response = await task
    assert response.status_code == 499


@pytest.mark.asyncio
async def test_authoritative_usage_ledger(valid_job: RuntimeJob) -> None:
    """The proxy accumulates token usage and model across multiple turns."""
    calls: list[int] = [0]

    def two_turn_upstream(request: httpx.Request) -> httpx.Response:
        calls[0] += 1
        if calls[0] == 1:
            return httpx.Response(
                200,
                json={
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu1",
                            "name": "write_output",
                            "input": {"markdown": "# Test", "sidecar": {}},
                        }
                    ],
                    "stop_reason": "tool_use",
                    "usage": {
                        "input_tokens": 10,
                        "output_tokens": 5,
                        "cache_creation_input_tokens": 2,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "msg-2",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "tool_use", "id": "tu2", "name": "finish", "input": {}}],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 5,
                    "cache_read_input_tokens": 3,
                },
            },
        )

    proxy = _proxy(upstream=httpx.AsyncClient(transport=httpx.MockTransport(two_turn_upstream)))
    token, jti, attempt_id = _issue(proxy, valid_job)
    base = b'{"messages": [{"role":"user","content":"hi"}]}'

    req1 = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id, seq=1),
        content=base,
    )
    r1 = await proxy.handle(req1)
    assert r1.status_code == 200

    req2 = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id, seq=2),
        content=base,
    )
    r2 = await proxy.handle(req2)
    assert r2.status_code == 200

    meta = proxy.get_attempt_metadata(jti)
    assert meta.model == "claude-sonnet-4-6"
    assert meta.token_usage.input_tokens == 30
    assert meta.token_usage.output_tokens == 10
    assert meta.token_usage.cache_creation_input_tokens == 2
    assert meta.token_usage.cache_read_input_tokens == 3
    assert meta.token_usage.total_tokens == 45
    assert meta.cost is not None


@pytest.mark.asyncio
async def test_create_app_streams_bounded_body(valid_job: RuntimeJob) -> None:
    """The ASGI entry point rejects oversized bodies without buffering them whole."""
    proxy = _proxy()
    proxy.cfg = proxy.cfg.model_copy(update={"max_body_bytes": 10})
    app = proxy.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/messages",
            content=b'{"messages": [{"role":"user","content":"hello world"}]}',
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_consume_attempt_finalization_removes_session(valid_job: RuntimeJob) -> None:
    """Finalization returns authoritative usage once and removes the session."""
    proxy = _proxy()
    token, jti, attempt_id = _issue(proxy, valid_job)
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 200

    meta_before = proxy.get_attempt_metadata(jti)
    assert meta_before.token_usage.input_tokens == 10

    consumed = await proxy.consume_attempt_finalization(jti)
    assert consumed.metadata.token_usage.input_tokens == 10
    assert consumed.metadata.token_usage.output_tokens == 5
    assert consumed.terminal_category is None

    assert jti not in proxy._sessions
    assert proxy.get_attempt_metadata(jti).token_usage.input_tokens == 0

    second = await proxy.consume_attempt_finalization(jti)
    assert second.metadata.token_usage.input_tokens == 0
    assert second.terminal_category is None


class _SlowDripStream:
    """Fake upstream stream that yields small chunks slowly."""

    def __init__(self, chunk_count: int, interval: float) -> None:
        self.chunk_count = chunk_count
        self.interval = interval

    async def __aenter__(self) -> "_SlowDripStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def raise_for_status(self) -> None:
        pass

    async def aiter_bytes(self) -> Any:
        for _ in range(self.chunk_count):
            await asyncio.sleep(self.interval)
            yield b'{"id":"msg-1","type":"message","role":"assistant","model":"claude-sonnet-4-6","content":[{"type":"tool_use","id":"tu1","name":"finish","input":{}}],"stop_reason":"tool_use","usage":{"input_tokens":1,"output_tokens":1}}'


class _SlowDripClient:
    """Fake httpx client whose stream yields a slow-drip response."""

    def __init__(self, chunk_count: int, interval: float) -> None:
        self.chunk_count = chunk_count
        self.interval = interval

    def stream(
        self,
        method: str,
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        timeout: Any,
    ) -> _SlowDripStream:
        return _SlowDripStream(self.chunk_count, self.interval)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_upstream_slow_drip_exceeds_deadline(valid_job: RuntimeJob) -> None:
    """A slow-drip response is cancelled by the absolute deadline, not the per-read timeout."""
    proxy = _proxy()
    # Deadline 0.2s from now; per_read_timeout from handle will be at least 1s.
    near_deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=0.2)
    job = valid_job.model_copy(update={"execution_deadline": near_deadline})
    token, jti, attempt_id = _issue(proxy, job)
    proxy.anthropic_client = _SlowDripClient(chunk_count=20, interval=0.05)

    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(job, token, attempt_id),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 504
    assert response.json()["error"]["type"] == "timeout"
    finalization = await proxy.consume_attempt_finalization(jti)
    assert finalization.terminal_category == "timeout"
