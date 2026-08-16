"""Deterministic tests for the worker-side Anthropic Messages API proxy.

These tests do not make real Anthropic calls; they use `httpx.MockTransport` to
feed the proxy a fake upstream. They also do not need `runsc` or a database.
"""
from __future__ import annotations

import asyncio
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
    lease_token: str = "lease-1",
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
        lease_token=lease_token,
        input_manifest=InputManifest(
            transcript_id=transcript_id,
            transcript_ref="transcript.txt",
            account_id=account_id,
            org_id=org_id,
        ),
        execution_deadline=deadline
        or datetime.now(tz=timezone.utc) + timedelta(minutes=5),
        allowlist=Allowlist(
            tools=frozenset(),
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


def _request_headers(job: RuntimeJob | None, token: str) -> dict[str, str]:
    """Build the identity-bound headers the sandbox runtime must echo."""
    headers: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "content-type": "application/json",
        "x-attempt-number": str(job.attempt_number) if job else "1",
    }
    if job:
        headers["x-job-id"] = str(job.job_id)
        if job.lease_token:
            headers["x-lease-token"] = job.lease_token
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


@pytest.fixture
def valid_job() -> RuntimeJob:
    return _make_job()


@pytest.mark.asyncio
async def test_valid_capability_reaches_upstream(valid_job: RuntimeJob) -> None:
    """A valid, job-scoped capability is accepted and forwarded to the fake upstream."""
    proxy = _proxy()
    token = proxy.issue_capability(
        valid_job,
        attempt_number=valid_job.attempt_number,
        lease_token=valid_job.lease_token,
    )
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token),
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
    token = proxy.issue_capability(
        expired_job,
        attempt_number=expired_job.attempt_number,
        lease_token=expired_job.lease_token,
    )
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(expired_job, token),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_wrong_model_override_rejected(valid_job: RuntimeJob) -> None:
    """The sandbox cannot override the worker-authorized model."""
    proxy = _proxy()
    token = proxy.issue_capability(
        valid_job,
        attempt_number=valid_job.attempt_number,
        lease_token=valid_job.lease_token,
    )
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token),
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
    token = proxy.issue_capability(
        valid_job,
        attempt_number=valid_job.attempt_number,
        lease_token=valid_job.lease_token,
    )
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/batch",
        headers=_request_headers(valid_job, token),
        content=b'{"messages": [{"role":"user","content":"hi"}]}',
    )
    response = await proxy.handle(request)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_cross_job_capability_rejected(valid_job: RuntimeJob) -> None:
    """A token signed for a different job ID is rejected when the wrong ID is echoed."""
    proxy = _proxy()
    other_job = _make_job()
    token = proxy.issue_capability(
        other_job,
        attempt_number=other_job.attempt_number,
        lease_token=other_job.lease_token,
    )
    # Request carries a token for other_job but echoes valid_job's identity.
    headers = _request_headers(valid_job, token)
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
    token = proxy.issue_capability(
        valid_job,
        attempt_number=valid_job.attempt_number,
        lease_token=valid_job.lease_token,
    )
    headers = _request_headers(valid_job, token)
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
    token = proxy.issue_capability(
        valid_job,
        attempt_number=valid_job.attempt_number,
        lease_token=valid_job.lease_token,
    )
    headers = _request_headers(valid_job, token)
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
    token = proxy.issue_capability(
        valid_job,
        attempt_number=valid_job.attempt_number,
        lease_token=valid_job.lease_token,
    )
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token),
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
    token = proxy.issue_capability(
        valid_job,
        attempt_number=valid_job.attempt_number,
        lease_token=valid_job.lease_token,
    )
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token),
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
    token = proxy.issue_capability(
        valid_job,
        attempt_number=valid_job.attempt_number,
        lease_token=valid_job.lease_token,
    )
    request = httpx.Request(
        method="POST",
        url="https://api.anthropic.com/v1/messages",
        headers=_request_headers(valid_job, token),
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
    token = proxy.issue_capability(
        valid_job,
        attempt_number=2,
        lease_token="lease-xyz",
    )
    claims = jwt.decode(token, "b" * 32, algorithms=["HS256"])
    assert claims["job_id"] == str(valid_job.job_id)
    assert claims["attempt_number"] == 2
    assert claims["lease_token"] == "lease-xyz"
    assert claims["model"] == "claude-sonnet-4-6"
    assert claims["api_version"] == "2023-06-01"
    assert claims["endpoint"] == "https://api.anthropic.com/v1/messages"
