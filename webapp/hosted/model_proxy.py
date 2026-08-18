"""Worker-side Anthropic Messages API proxy.

The proxy runs outside the gVisor sandbox and holds the platform `ANTHROPIC_API_KEY`.
The sandbox receives a short-lived, job-scoped capability token that is bound to the
attempt identity, requested model, execution deadline, and the single approved
Anthropic endpoint. The proxy validates every inbound request, strips any
sandbox-supplied provider or forwarding headers, adds the real API key and version,
and forwards only a bounded JSON body to Anthropic.

The proxy also maintains the authoritative per-attempt model/usage ledger.  The
trusted worker retrieves that ledger after the sandbox exits and uses it to overwrite
any sandbox-authored accounting in `RuntimeResult.execution_metadata`.

No prompts, transcript content, model responses, credentials, lease tokens, or raw
upstream errors are logged.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from webapp.hosted import config
from webapp.hosted.runtime_contract import (
    ExecutionMetadata,
    FailureCategory,
    RedactedFailure,
    RuntimeJob,
    RuntimeResult,
    TokenUsage,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 10 * 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
MAX_UPSTREAM_TIMEOUT = 120.0
MIN_UPSTREAM_TIMEOUT = 1.0
CAPABILITY_LEEWAY_SECONDS = 5


class _ProxySession:
    """Mutable, per-attempt proxy state kept in the trusted worker process."""

    def __init__(
        self,
        jti: str,
        attempt_id: str,
        job_id: str,
        attempt_number: int,
        model: str,
        deadline: str,
        endpoint: str,
        api_version: str,
        allowed_tools: frozenset[str],
    ) -> None:
        self.jti = jti
        self.attempt_id = attempt_id
        self.job_id = job_id
        self.attempt_number = attempt_number
        self.model = model
        self.deadline = deadline
        self.endpoint = endpoint
        self.api_version = api_version
        self.allowed_tools = allowed_tools
        self.next_seq = 1
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_creation = 0
        self.cache_read = 0
        self.request_count = 0
        self.terminal_category: FailureCategory | None = None
        self.lock = asyncio.Lock()
        self.cancel_event = asyncio.Event()


class ProxyFinalization(BaseModel):
    """Trusted, one-time finalization state for a proxy attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metadata: ExecutionMetadata
    terminal_category: FailureCategory | None = None


class ProxyCapability(BaseModel):
    """Claims in a job-scoped proxy capability token."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jti: str
    job_id: str
    attempt_number: int
    attempt_id: str
    model: str
    deadline: str  # ISO-8601 UTC timestamp
    endpoint: str
    api_version: str
    allowed_tools: tuple[str, ...] = ()
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
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_upstream_timeout: float = MAX_UPSTREAM_TIMEOUT
    min_upstream_timeout: float = MIN_UPSTREAM_TIMEOUT
    max_tokens: int = 8192
    max_concurrent_requests: int = 100
    cost_per_1k_input: float = 0.003
    cost_per_1k_output: float = 0.015

    @field_validator("max_body_bytes")
    @classmethod
    def _positive_max_body(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_body_bytes must be positive")
        return value

    @field_validator("max_response_bytes")
    @classmethod
    def _positive_max_response(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_response_bytes must be positive")
        return value

    @field_validator("max_tokens")
    @classmethod
    def _positive_max_tokens(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_tokens must be positive")
        return value

    @field_validator("max_concurrent_requests")
    @classmethod
    def _positive_concurrency(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("max_concurrent_requests must be positive")
        return value


# ---------------------------------------------------------------------------
# Strict request DTOs
# ---------------------------------------------------------------------------


class ProxyContentBlock(BaseModel):
    """One Anthropic Messages API content block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    text: str | None = None
    tool_use_id: str | None = None
    content: str | None = None


class ProxyMessage(BaseModel):
    """One message in the Anthropic Messages API conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["user", "assistant"]
    content: str | list[ProxyContentBlock]


class ProxyTool(BaseModel):
    """Anthropic tool definition with JSON schema input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class ProxyMessageRequest(BaseModel):
    """Strict request payload for the Anthropic Messages API.

    The proxy rejects unknown fields and invalid `max_tokens` / `stream` values
    before forwarding.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    model: str | None = None
    max_tokens: int = Field(default=4096, ge=1)
    system: str | None = None
    messages: list[ProxyMessage]
    tools: list[ProxyTool] | None = None
    tool_choice: Any | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    metadata: dict[str, str] | None = None
    stream: Literal[False] | None = None


# ---------------------------------------------------------------------------


class ModelProxy:
    """Auditable worker-side proxy for the Anthropic Messages API.

    The proxy is intended to be instantiated once per attempt. It can be used as an
    ASGI request handler, as an `httpx` transport callback, or driven directly by
    a test harness.  The authoritative per-attempt usage ledger is kept in memory
    and exposed through `get_attempt_metadata` for the trusted worker to retrieve
    after the sandbox exits.
    """

    def __init__(
        self,
        proxy_config: ProxyConfig | None = None,
        anthropic_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = proxy_config or _default_config()
        self.anthropic_client = anthropic_client or _default_anthropic_client(self.cfg)
        self._sessions: dict[str, _ProxySession] = {}
        self._sem: asyncio.Semaphore | None = None

    def _get_sem(self) -> asyncio.Semaphore:
        if self._sem is None:
            self._sem = asyncio.Semaphore(self.cfg.max_concurrent_requests)
        return self._sem

    def issue_capability(
        self,
        job: RuntimeJob,
        attempt_number: int,
        jti: str | None = None,
        attempt_id: str | None = None,
    ) -> tuple[str, str, str]:
        """Sign a short-lived capability token for one sandbox attempt.

        Returns `(token, jti, attempt_id)`.  The token's lifetime is the immutable
        attempt `execution_deadline`; no five-minute cap is imposed.
        """
        jti = jti or uuid.uuid4().hex
        if attempt_id is None:
            attempt_id = hashlib.sha256(jti.encode("utf-8")).hexdigest()[:32]
        allowed_tools = tuple(sorted(job.allowlist.tools))
        deadline_ts = int(job.execution_deadline.timestamp())
        claims = {
            "jti": jti,
            "job_id": str(job.job_id),
            "attempt_number": attempt_number,
            "attempt_id": attempt_id,
            "model": job.requested_model,
            "deadline": job.execution_deadline.isoformat(),
            "endpoint": f"{self.cfg.anthropic_api_url.rstrip('/')}/v1/messages",
            "api_version": self.cfg.anthropic_api_version,
            "allowed_tools": allowed_tools,
            "iat": int(time.time()),
            "exp": deadline_ts,
        }
        token = jwt.encode(claims, self.cfg.secret, algorithm="HS256")
        self._ensure_session(
            jti,
            attempt_id,
            str(job.job_id),
            attempt_number,
            job.requested_model,
            job.execution_deadline.isoformat(),
            claims["endpoint"],
            self.cfg.anthropic_api_version,
            frozenset(job.allowlist.tools),
        )
        return token, jti, attempt_id

    def _ensure_session(
        self,
        jti: str,
        attempt_id: str,
        job_id: str,
        attempt_number: int,
        model: str,
        deadline: str,
        endpoint: str,
        api_version: str,
        allowed_tools: frozenset[str],
    ) -> _ProxySession:
        session = self._sessions.get(jti)
        if session is None:
            session = _ProxySession(
                jti=jti,
                attempt_id=attempt_id,
                job_id=job_id,
                attempt_number=attempt_number,
                model=model,
                deadline=deadline,
                endpoint=endpoint,
                api_version=api_version,
                allowed_tools=allowed_tools,
            )
            self._sessions[jti] = session
        return session

    def cancel_session(self, jti: str | None) -> None:
        """Signal that an in-flight attempt has been cancelled by the worker."""
        if jti is None:
            return
        session = self._sessions.get(jti)
        if session is not None:
            session.cancel_event.set()

    def get_attempt_metadata(self, jti: str | None) -> ExecutionMetadata:
        """Return the authoritative, proxy-accumulated execution metadata.

        If the attempt is unknown, returns an empty `ExecutionMetadata`.  This is a
        non-consuming read; `consume_attempt_finalization` removes the session once.
        """
        return self._metadata_for_session(self._sessions.get(jti))

    async def consume_attempt_finalization(self, jti: str | None) -> ProxyFinalization:
        """Return trusted finalization state once and remove the session.

        The session lock is acquired so this cannot race an in-flight `handle` call;
        after returning, the same `jti` will yield empty finalization state.
        """
        if jti is None:
            return ProxyFinalization(metadata=ExecutionMetadata())
        session = self._sessions.get(jti)
        if session is None:
            return ProxyFinalization(metadata=ExecutionMetadata())
        async with session.lock:
            # Pop after acquiring the lock so we never drop a concurrent update.
            session = self._sessions.pop(jti, None)
            if session is None:
                return ProxyFinalization(metadata=ExecutionMetadata())
            return ProxyFinalization(
                metadata=self._metadata_for_session(session),
                terminal_category=session.terminal_category,
            )

    def _metadata_for_session(self, session: _ProxySession | None) -> ExecutionMetadata:
        if session is None:
            return ExecutionMetadata()
        total_input = session.input_tokens + session.cache_creation + session.cache_read
        cost = (
            total_input * self.cfg.cost_per_1k_input / 1000.0
            + session.output_tokens * self.cfg.cost_per_1k_output / 1000.0
        )
        return ExecutionMetadata(
            runtime_version=None,
            model=session.model,
            token_usage=TokenUsage(
                input_tokens=session.input_tokens,
                output_tokens=session.output_tokens,
                cache_creation_input_tokens=session.cache_creation or None,
                cache_read_input_tokens=session.cache_read or None,
            ),
            cost=cost,
        )

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

        session = self._ensure_session(
            capability.jti,
            capability.attempt_id,
            capability.job_id,
            capability.attempt_number,
            capability.model,
            capability.deadline,
            capability.endpoint,
            capability.api_version,
            frozenset(capability.allowed_tools),
        )

        async with session.lock:
            if session.cancel_event.is_set():
                return _error_response(499, "session_cancelled")

            # Bind the capability to the request identity.
            if request.headers.get("x-job-id") != capability.job_id:
                return _error_response(403, "job_mismatch")
            try:
                if int(request.headers.get("x-attempt-number") or 0) != capability.attempt_number:
                    return _error_response(403, "attempt_mismatch")
            except (TypeError, ValueError):
                return _error_response(403, "attempt_mismatch")
            if request.headers.get("x-attempt-id") != capability.attempt_id:
                return _error_response(403, "attempt_id_mismatch")

            seq_str = request.headers.get("x-request-seq")
            try:
                seq = int(seq_str) if seq_str is not None else -1
            except (TypeError, ValueError):
                return _error_response(400, "invalid_request_sequence")
            if seq != session.next_seq:
                return _error_response(403, "sequence_mismatch")
            session.next_seq += 1

            try:
                req = ProxyMessageRequest.model_validate(payload)
            except ValidationError:
                return _error_response(400, "invalid_request_shape")

            if req.max_tokens > self.cfg.max_tokens:
                return _error_response(400, "max_tokens_exceeded")

            if req.stream is True:
                return _error_response(400, "streaming_not_allowed")

            if not req.messages:
                return _error_response(400, "missing_messages")

            if req.model is not None and req.model != capability.model:
                return _error_response(403, "model_mismatch")

            if req.tools:
                for tool in req.tools:
                    if tool.name not in capability.allowed_tools:
                        return _error_response(403, "forbidden_tool")

            deadline = datetime.fromisoformat(capability.deadline)
            now = datetime.now(tz=timezone.utc)
            remaining = (deadline - now).total_seconds()
            if remaining <= 0:
                session.terminal_category = "timeout"
                return _error_response(410, "deadline_expired")

            forwarded = req.model_dump(exclude_none=True)
            forwarded["model"] = capability.model

            upstream_url = capability.endpoint
            if not upstream_url.startswith(self.cfg.anthropic_api_url.rstrip("/")):
                return _error_response(403, "endpoint_mismatch")

            upstream_headers = _build_upstream_headers(self.cfg)
            per_read_timeout = max(
                self.cfg.min_upstream_timeout,
                min(remaining, self.cfg.max_upstream_timeout),
            )

            async with self._get_sem():
                try:
                    response_body = await self._upstream_with_cancel(
                        upstream_url,
                        json.dumps(forwarded).encode("utf-8"),
                        upstream_headers,
                        per_read_timeout,
                        session,
                        deadline,
                    )
                except _ProxyError as exc:
                    logger.warning("Anthropic upstream error: %s", exc.category)
                    return _error_response(exc.status, exc.category)

            try:
                response_data = json.loads(response_body)
            except json.JSONDecodeError:
                return _error_response(502, "upstream_error")

            usage = response_data.get("usage") or {}
            session.input_tokens += _int_or_zero(usage.get("input_tokens"))
            session.output_tokens += _int_or_zero(usage.get("output_tokens"))
            session.cache_creation += _int_or_zero(usage.get("cache_creation_input_tokens"))
            session.cache_read += _int_or_zero(usage.get("cache_read_input_tokens"))
            session.model = capability.model
            session.request_count += 1

            redacted = _redact_response(response_data)
            return httpx.Response(200, json=redacted)

    async def _upstream_with_cancel(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str],
        per_read_timeout: float,
        session: _ProxySession,
        deadline: datetime,
    ) -> bytes:
        """Stream the upstream request to completion while respecting cancellation.

        An absolute wall-clock deadline is enforced independently of the per-read
        `httpx` timeout so a slow-drip response cannot outlive the attempt.  Response
        size is capped at `ProxyConfig.max_response_bytes`.
        """

        now = datetime.now(tz=timezone.utc)
        remaining = (deadline - now).total_seconds()
        if remaining <= 0:
            session.terminal_category = "timeout"
            raise _ProxyError("timeout", 504)

        async def _stream() -> bytes:
            async with self.anthropic_client.stream(
                "POST",
                url,
                content=body,
                headers=headers,
                timeout=httpx.Timeout(
                    None,
                    connect=per_read_timeout,
                    read=per_read_timeout,
                    write=per_read_timeout,
                    pool=per_read_timeout,
                ),
            ) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self.cfg.max_response_bytes:
                        raise _ProxyError("upstream_response_too_large", 502)
                    chunks.append(chunk)
                return b"".join(chunks)

        upstream_task = asyncio.create_task(_stream())
        cancel_task = asyncio.create_task(session.cancel_event.wait())
        deadline_task = asyncio.create_task(asyncio.sleep(remaining))
        tasks: set[asyncio.Task] = {upstream_task, cancel_task, deadline_task}
        try:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await p
            if cancel_task in done:
                upstream_task.cancel()
                deadline_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await upstream_task
                raise _ProxyError("session_cancelled", 499)
            if deadline_task in done:
                session.terminal_category = "timeout"
                upstream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await upstream_task
                raise _ProxyError("timeout", 504)
            try:
                return upstream_task.result()
            except httpx.HTTPStatusError as exc:
                raise _ProxyError("upstream_error", 502) from exc
            except httpx.TimeoutException as exc:
                raise _ProxyError("upstream_timeout", 504) from exc
            except httpx.RequestError as exc:
                raise _ProxyError("upstream_error", 502) from exc
        except asyncio.CancelledError:
            for t in tasks:
                t.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                for t in tasks:
                    await t
            raise

    def create_app(self) -> Any:
        """Return a minimal Starlette ASGI app bound to this proxy instance."""
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def _messages(request: Request) -> JSONResponse:
            body = b""
            async for chunk in request.stream():
                body += chunk
                if len(body) > self.cfg.max_body_bytes:
                    return JSONResponse(
                        {"error": {"type": "request_too_large", "message": "request rejected by proxy"}},
                        status_code=413,
                    )

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


def _int_or_zero(value: Any) -> int:
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


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
    thinking blocks, or streaming metadata.
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
