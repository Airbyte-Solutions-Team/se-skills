"""Provider-neutral typed runtime contract for the Slice 5B isolated executor.

This module defines the immutable, serializable data an external sandbox runtime
must receive and the candidate result it must produce, without assuming any
specific sandbox vendor (gVisor, Firecracker, managed containers) or model provider.
No transcript bodies, bearer tokens, signed URLs, DB credentials, Storage
credentials, or browser-supplied paths travel inside the durable job payload.

All contract tests use fakes; no model or network call is required.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from ipaddress import ip_address
from typing import Any, Literal, Protocol, get_args, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RuntimeValidationError(ValueError):
    """Raised when a runtime contract invariant is violated."""


Mode = Literal["full", "brief"]
ValidationStatus = Literal["valid", "invalid", "unvalidated"]
NetworkScheme = Literal["http", "https"]

# Closed set of typed tool names a runtime may register. Generic capabilities such
# as Bash, Git, Browser, Http, McpDiscover, and BypassPermissions are deliberately
# absent from this registry.
ToolName = Literal[
    "read_transcript",
    "read_prior_context",
    "write_output",
    "list_priors",
    "search_transcript",
    "finish",
    "report_failure",
]

TRUSTED_TOOL_REGISTRY: frozenset[str] = frozenset(get_args(ToolName))


def _is_safe_path(value: str) -> bool:
    """Return True when a sandbox-relative path contains no traversal or home prefix."""
    if not value:
        return False
    if ".." in value or value.startswith("~") or value.startswith("/"):
        return False
    return True


def _is_safe_absolute_path(value: str) -> bool:
    """Return True when an absolute sandbox path is under /tmp and has no traversal."""
    if not value or not value.startswith("/tmp/"):
        return False
    if ".." in value or "~" in value:
        return False
    return True


class NetworkDestination(BaseModel):
    """An allowed network endpoint for the sandbox.

    The runtime enforces that model/API calls and any tool-mediated outbound
    traffic are limited to the per-job allowlist. Scheme is restricted to `http`
    or `https`; host must be a non-empty DNS hostname or `localhost` (not an IP
    address or wildcard); port, when present, is a positive 16-bit integer;
    path_prefix must start with `/` or be empty.
    """

    model_config = ConfigDict(frozen=True)

    host: str
    port: int | None = None
    scheme: NetworkScheme = "https"
    path_prefix: str = ""

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        if not value or value.strip() != value:
            raise RuntimeValidationError("NetworkDestination host must be non-empty")
        if "*" in value or "?" in value:
            raise RuntimeValidationError("NetworkDestination host must not contain wildcards")
        # Reject IP addresses. ip_address accepts IPv4/IPv6 strings and raises ValueError
        # for hostnames such as `api.anthropic.com`.
        try:
            ip_address(value)
        except ValueError:
            pass
        else:
            raise RuntimeValidationError("NetworkDestination host must not be an IP address")
        # Must look like a hostname: alphanum plus hyphens/dots, no leading/trailing hyphen.
        if not re.fullmatch(r"^(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]*[a-zA-Z0-9])?)*$", value):
            raise RuntimeValidationError("NetworkDestination host must be a valid hostname")
        return value

    @field_validator("port")
    @classmethod
    def _validate_port(cls, value: int | None) -> int | None:
        if value is not None and not (1 <= value <= 65535):
            raise RuntimeValidationError("NetworkDestination port must be in 1..65535")
        return value

    @field_validator("path_prefix")
    @classmethod
    def _validate_path_prefix(cls, value: str) -> str:
        if value and not value.startswith("/"):
            raise RuntimeValidationError("NetworkDestination path_prefix must start with '/' when present")
        if ".." in value:
            raise RuntimeValidationError("NetworkDestination path_prefix must not contain '..'")
        return value

    def __str__(self) -> str:
        parts = [f"{self.scheme}://{self.host}"]
        if self.port is not None:
            parts.append(f":{self.port}")
        if self.path_prefix:
            parts.append(self.path_prefix)
        return "".join(parts)


class TokenUsage(BaseModel):
    """Token usage reported by the model runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int | None = None


class ExecutionMetadata(BaseModel):
    """Non-sensitive execution metadata returned by the sandbox runtime."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_version: str | None = None
    model: str | None = None
    token_usage: TokenUsage = Field(default_factory=TokenUsage)
    cost: float | None = None


class RedactedFailure(BaseModel):
    """Categorized, non-sensitive failure information for the job ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str
    message: str


class SandboxOutputSidecar(BaseModel):
    """Candidate sidecar produced by the sandbox runtime.

    This is an untrusted artifact and contains no validation state. The worker
    validates the Markdown and sidecar with `output_schema.parse_output` outside
    the sandbox before persisting anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill: str
    skill_version: str = "1.0"
    mode: Mode = "full"
    title: str | None = None
    date: str | None = None
    source_coverage: str | None = None


class InputManifest(BaseModel):
    """Read-only references that identify the transcript and prior context.

    The manifest carries only stable, non-sensitive identifiers. The runtime is
    responsible for resolving these references through the worker-supplied,
    short-lived, read-only workspace; it never receives Storage credentials or
    a database connection string in this payload.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    transcript_id: uuid.UUID
    account_id: uuid.UUID
    org_id: uuid.UUID
    opportunity_id: uuid.UUID | None = None
    prior_context_refs: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("prior_context_refs")
    @classmethod
    def _validate_refs(cls, value: frozenset[str]) -> frozenset[str]:
        for ref in value:
            if not ref or ".." in ref or ref.startswith(("/", "\\", "~")):
                raise RuntimeValidationError(f"Invalid prior context reference: {ref!r}")
        return value


class Allowlist(BaseModel):
    """Tools and network destinations the skill is permitted to use.

    Tool names are selected from the closed `ToolName` registry. Generic tools
    such as `Bash`, `Git`, `Browser`, `Http`, `McpDiscover`, and `BypassPermissions`
    are not in the registry and therefore cannot appear in an allowlist.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tools: frozenset[ToolName] = Field(default_factory=frozenset)
    network: frozenset[NetworkDestination] = Field(default_factory=frozenset)

    @field_validator("tools")
    @classmethod
    def _validate_tools(cls, value: frozenset[str]) -> frozenset[str]:
        for tool in value:
            if tool not in TRUSTED_TOOL_REGISTRY:
                raise RuntimeValidationError(f"Tool {tool!r} is not in the trusted registry")
        return value


class RuntimeJob(BaseModel):
    """Immutable, serializable data the sandbox runtime needs to execute one attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: uuid.UUID
    org_id: uuid.UUID
    account_id: uuid.UUID
    transcript_id: uuid.UUID
    requester_id: uuid.UUID
    opportunity_id: uuid.UUID | None = None
    skill: str = "post-call"
    skill_version: str = "1.0"
    requested_model: str | None = None
    requested_runtime_version: str | None = None
    mode: Mode = "full"
    input_manifest: InputManifest
    allowlist: Allowlist = Field(default_factory=Allowlist)
    execution_deadline: datetime
    input_workspace: str = "/tmp/runtime-input"
    output_workspace: str = "/tmp/runtime-output"

    @field_validator("execution_deadline")
    @classmethod
    def _validate_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise RuntimeValidationError("execution_deadline must be timezone-aware")
        return value

    @field_validator("input_workspace")
    @classmethod
    def _validate_input_workspace(cls, value: str) -> str:
        if not _is_safe_absolute_path(value):
            raise RuntimeValidationError("input_workspace must be an absolute sandbox path under /tmp with no traversal")
        return value

    @field_validator("output_workspace")
    @classmethod
    def _validate_output_workspace(cls, value: str) -> str:
        if not _is_safe_absolute_path(value):
            raise RuntimeValidationError("output_workspace must be an absolute sandbox path under /tmp with no traversal")
        return value

    @model_validator(mode="after")
    def _identity_consistency(self) -> "RuntimeJob":
        manifest = self.input_manifest
        if manifest.transcript_id != self.transcript_id:
            raise RuntimeValidationError("input_manifest.transcript_id must match RuntimeJob.transcript_id")
        if manifest.account_id != self.account_id:
            raise RuntimeValidationError("input_manifest.account_id must match RuntimeJob.account_id")
        if manifest.org_id != self.org_id:
            raise RuntimeValidationError("input_manifest.org_id must match RuntimeJob.org_id")
        if manifest.opportunity_id != self.opportunity_id:
            raise RuntimeValidationError("input_manifest.opportunity_id must match RuntimeJob.opportunity_id")
        return self


class ValidationResult(BaseModel):
    """Deterministic validation of the produced Markdown and sidecar.

    This is produced by the trusted worker after the sandbox returns a candidate
    result, not by the sandbox itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ValidationStatus
    errors: tuple[str, ...] = ()


class RuntimeResult(BaseModel):
    """Untrusted candidate result produced by the sandbox runtime.

    Either `output_artifact` and `sidecar` are present, or `failure` is present.
    The worker validates `output_artifact` and `sidecar` outside the sandbox before
    persisting anything.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_artifact: str | None = None
    sidecar: SandboxOutputSidecar | None = None
    execution_metadata: ExecutionMetadata = Field(default_factory=ExecutionMetadata)
    failure: RedactedFailure | None = None

    @model_validator(mode="after")
    def _result_invariant(self) -> "RuntimeResult":
        has_output = self.output_artifact is not None
        has_failure = self.failure is not None
        if has_output and has_failure:
            raise RuntimeValidationError("RuntimeResult cannot contain both output_artifact and failure")
        if not has_output and not has_failure:
            raise RuntimeValidationError("RuntimeResult must contain output_artifact or failure")
        if has_output and self.sidecar is None:
            raise RuntimeValidationError("RuntimeResult with output_artifact must include a sidecar")
        return self


@runtime_checkable
class CancellationToken(Protocol):
    """Host-side handle that lets the runtime check for cancellation."""

    def is_cancelled(self) -> bool:
        """Return True when the worker has requested cancellation."""
        ...

    async def wait(self) -> None:
        """Return when cancellation has been requested.

        The runtime may race this against a blocked model call so a long-running
        request can be interrupted as soon as the worker signals cancellation.
        """
        ...


@runtime_checkable
class SkillRuntime(Protocol):
    """Protocol for an isolated sandbox runtime that executes one SE skill."""

    async def execute(self, job: RuntimeJob, cancellation: CancellationToken) -> RuntimeResult:
        """Execute the job in an isolated sandbox and return a `RuntimeResult`.

        Implementations must honour `job.execution_deadline`, `cancellation`,
        and `job.allowlist`. They must not read or write outside
        `job.output_workspace` and must not expose model credentials to sandbox
        tools or generated code.
        """
        ...
