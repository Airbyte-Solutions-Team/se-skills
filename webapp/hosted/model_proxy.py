"""Worker-side Anthropic Messages API proxy.

The proxy runs outside the gVisor sandbox and holds the platform `ANTHROPIC_API_KEY`.
The sandbox receives a short-lived, job-scoped capability token that is bound to the
attempt identity, requested model, execution deadline, and the single approved
Anthropic endpoint. The proxy validates every inbound request, strips any
sandbox-supplied provider or forwarding headers, adds the real API key and version,
and forwards only a bounded JSON body to Anthropic.

No prompts, transcript content, model responses, credentials, or raw upstream
errors are logged.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from webapp.hosted import config
from webapp.hosted.runtime_contract import (
    FailureCategory,
    RedactedFailure,
    RuntimeJob,
    RuntimeResult,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_UPSTREAM_TIMEOUT = 120.0
MIN_UPSTREAM_TIMEOUT = 1.0

CAPABILITY_LEEWAY_SECONDS = 5


class ProxyCapability(BaseModel):
    """Claims in a job-scoped proxy capability token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jti: str
    job_id: str
    attempt_number: int
    lease_token: str
    model: str
    deadline: str  # ISO-8601 UTC timestamp
    endpoint: str
    api_version: str
    iat: int | None = None
    exp: int | None = None

    @field_validator("attempt_number")
    @classmethod
    def _positive_attempt(cls, value: int) -> int:
        if value < 1:
            raise ValueError("attempt_number must be positive")
        return value


class ProxyConfig(BaseModel):
    """Static configuration for the model proxy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret: str
    anthropic_api_key: str
    anthropic_api_url: str = "https://api.anthropic.com"
    anthropic_api_version: str = "2023-06-01"
    max_body_bytes: int = MAX_BODY_BYTES
    max_upstream_timeout: float = MAX_UPSTREAM_TIMEOUT
    min_upstream_timeout: float = MIN_UPSTREAM_TIMEOUT

    @field_validator("max_body_bytes")
    @classmethod
    def _positive_max_body(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_body_bytes must be positive")
        return value


class ModelProxy:
    """Auditable worker-side proxy for the Anthropic Messages API.

    The proxy is intended to be instantiated once per attempt. It can be used as an
    ASGI request handler, as an `httpx` transport callback, or driven directly by
    a test harness.
    """

    def __init__(
        self,
        proxy_config: ProxyConfig | None = None,
        anthropic_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = proxy_config or _default_config()
        self.anthropic_client = anthropic_client or _default_anthropic_client(self.cfg)

    def issue_capability(self, job: RuntimeJob, attempt_number: int, lease_token: str) -> str:
        """Sign a short-lived capability token for one sandbox attempt."""
        return _issue_token(self.cfg, job, attempt_number, lease_token)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        """Validate and forward a single sandbox model request.

        This is the core of the proxy and is used both by the ASGI server and by
        `httpx.MockTransport` in deterministic tests.
        """
        if request.method != "POST":
            return _error_response(405, "method_not_allowed")

        path = request.url.path
        if path != "/v1/messages":
            return _error_response(404, "route_not_found")

        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type:
            return _error_response(415, "unsupported_media_type")

        body = request.content
        if len(body) > self.cfg.max_body_bytes:
            return _error_response(413, "request_too_large")

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return _error_response(400, "invalid_json")

        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return _error_response(401, "missing_capability")
        token = auth[7:].strip()

        try:
            capability = _decode_token(self.cfg, token)
        except _ProxyError as exc:
            logger.warning("Capability validation failed: %s", exc.category)
            return _error_response(exc.status, exc.category)

        # Bind the capability to the request identity. The sandbox runtime must
        # echo the job ID, attempt number, and lease token in every request.
        if request.headers.get("x-job-id") != capability.job_id:
            return _error_response(403, "job_mismatch")
        try:
            if int(request.headers.get("x-attempt-number") or 0) != capability.attempt_number:
                return _error_response(403, "attempt_mismatch")
        except (TypeError, ValueError):
            return _error_response(403, "attempt_mismatch")
        if request.headers.get("x-lease-token") != capability.lease_token:
            return _error_response(403, "lease_mismatch")

        try:
            payload_model = payload.get("model")
            if payload_model is not None and payload_model != capability.model:
                # Sandbox attempted to override the worker-authorized model.
                return _error_response(403, "model_mismatch")
            payload["model"] = capability.model

            if "max_tokens" not in payload:
                # The Anthropic endpoint requires max_tokens. Provide a sane
                # bounded default rather than allow an open-ended request.
                payload["max_tokens"] = 4096

            messages = payload.get("messages")
            if not isinstance(messages, list) or len(messages) == 0:
                return _error_response(400, "missing_messages")

            # Enforce a compact shape; drop any keys that are not part of the
            # documented Anthropic Messages API request surface.
            allowed_keys = {
                "model",
                "messages",
                "system",
                "tools",
                "tool_choice",
                "max_tokens",
                "temperature",
                "top_p",
                "top_k",
                "metadata",
                "stream",
            }
            for key in list(payload.keys()):
                if key not in allowed_keys:
                    del payload[key]
        except Exception:
            return _error_response(400, "invalid_request_shape")

        deadline = datetime.fromisoformat(capability.deadline)
        now = datetime.now(tz=timezone.utc)
        remaining = (deadline - now).total_seconds()
        if remaining <= 0:
            return _error_response(410, "deadline_expired")

        timeout = max(self.cfg.min_upstream_timeout, min(remaining, self.cfg.max_upstream_timeout))

        upstream_url = capability.endpoint
        if not upstream_url.startswith(self.cfg.anthropic_api_url.rstrip("/")):
            return _error_response(403, "endpoint_mismatch")

        upstream_headers = _build_upstream_headers(self.cfg)
        try:
            upstream = await self.anthropic_client.post(
                upstream_url,
                json=payload,
                headers=upstream_headers,
                timeout=timeout,
            )
            upstream.raise_for_status()
            response_body = upstream.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Anthropic upstream error: status=%s category=%s",
                exc.response.status_code,
                "upstream_error",
            )
            return _error_response(502, "upstream_error")
        except httpx.TimeoutException:
            return _error_response(504, "upstream_timeout")
        except httpx.RequestError:
            return _error_response(502, "upstream_error")

        redacted = _redact_response(response_body)
        return httpx.Response(200, json=redacted)

    def create_app(self) -> Any:
        """Return a minimal Starlette ASGI app bound to this proxy instance."""
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def _messages(request: Request) -> JSONResponse:
            body = await request.body()
            headers = dict(request.headers)
            httpx_request = httpx.Request(
                method=request.method,
                url=str(request.url),
                headers=headers,
                content=body,
            )
            response = await self.handle(httpx_request)
            return JSONResponse(
                content=response.json() if response.content else None,
                status_code=response.status_code,
                headers=dict(response.headers),
            )

        app = Starlette(routes=[Route("/v1/messages", _messages, methods=["POST"])])
        return app


class _ProxyError(Exception):
    def __init__(self, category: str, status: int = 403) -> None:
        self.category = category
        self.status = status
        super().__init__(category)


def _default_config() -> ProxyConfig:
    secret = config.MODEL_PROXY_SECRET
    if not secret:
        raise RuntimeError("MODEL_PROXY_SECRET is not configured")
    key = config.ANTHROPIC_API_KEY
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    return ProxyConfig(
        secret=secret,
        anthropic_api_key=key,
        anthropic_api_url=config.ANTHROPIC_API_URL,
        anthropic_api_version=config.ANTHROPIC_API_VERSION,
    )


def _default_anthropic_client(cfg: ProxyConfig) -> httpx.AsyncClient:
    """An `httpx` client pointed at the configured Anthropic origin."""
    return httpx.AsyncClient(
        base_url=cfg.anthropic_api_url,
        timeout=httpx.Timeout(MAX_UPSTREAM_TIMEOUT),
    )


def _issue_token(cfg: ProxyConfig, job: RuntimeJob, attempt_number: int, lease_token: str) -> str:
    now = int(time.time())
    deadline_ts = int(job.execution_deadline.timestamp())
    exp = min(deadline_ts, now + 300)  # never valid longer than 5 minutes or the job deadline
    jti = str(uuid.uuid4())
    claims = {
        "jti": jti,
        "job_id": str(job.job_id),
        "attempt_number": attempt_number,
        "lease_token": str(lease_token),
        "model": job.requested_model,
        "deadline": job.execution_deadline.isoformat(),
        "endpoint": f"{cfg.anthropic_api_url.rstrip('/')}/v1/messages",
        "api_version": cfg.anthropic_api_version,
        "iat": now,
        "exp": exp,
    }
    return jwt.encode(claims, cfg.secret, algorithm="HS256")


def _decode_token(cfg: ProxyConfig, token: str) -> ProxyCapability:
    try:
        claims = jwt.decode(
            token,
            cfg.secret,
            algorithms=["HS256"],
            options={"require": ["exp", "iat"]},
            leeway=CAPABILITY_LEEWAY_SECONDS,
        )
    except jwt.ExpiredSignatureError:
        raise _ProxyError("capability_expired", 401)
    except jwt.InvalidTokenError:
        raise _ProxyError("invalid_capability", 401)

    try:
        return ProxyCapability.model_validate(claims)
    except ValidationError:
        raise _ProxyError("malformed_capability", 401)


def _build_upstream_headers(cfg: ProxyConfig) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-api-key": cfg.anthropic_api_key,
        "anthropic-version": cfg.anthropic_api_version,
    }


def _error_response(status: int, category: str) -> httpx.Response:
    """Return a non-sensitive, categorized error response."""
    return httpx.Response(
        status,
        json={"error": {"type": category, "message": "request rejected by proxy"}},
    )


def _redact_response(body: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields the sandbox runtime consumes.

    The proxy does not forward arbitrary upstream fields such as citations,
    thinking blocks, or streaming metadata. The `TypedToolRuntime` uses
    `extra="allow"` response DTOs, so removing unknown keys is safe.
    """
    allowed = {
        "id",
        "type",
        "role",
        "model",
        "content",
        "stop_reason",
        "stop_sequence",
        "usage",
    }
    return {k: v for k, v in body.items() if k in allowed}


def runtime_failure(category: FailureCategory) -> RuntimeResult:
    """Return a redacted `RuntimeResult` failure from proxy/runtime code."""
    return RuntimeResult(failure=RedactedFailure(category=category))
