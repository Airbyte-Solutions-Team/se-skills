"""Provider-neutral typed runtime contract for the Slice 5B isolated executor.

This module defines what an external sandbox runtime must receive and produce,
without assuming any specific sandbox vendor (gVisor, Firecracker, managed
containers) or model provider. No transcript bodies, bearer tokens, signed
URLs, DB credentials, Storage credentials, or browser-supplied paths travel
inside the durable job payload.

All contract tests use fakes; no model or network call is required.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Protocol, runtime_checkable


class RuntimeValidationError(Exception):
    """Raised when a runtime contract invariant is violated."""


@dataclass(frozen=True)
class NetworkDestination:
    """An allowed network endpoint for the sandbox.

    The runtime enforces that model/API calls and any tool-mediated outbound
    traffic are limited to this allowlist. Scheme, host, and port are explicit;
    a trailing slash means the whole host/port is allowed when path is empty.
    """

    host: str
    port: int | None = None
    scheme: str = "https"
    path_prefix: str = ""

    def __str__(self) -> str:
        parts = [f"{self.scheme}://{self.host}"]
        if self.port is not None:
            parts.append(f":{self.port}")
        if self.path_prefix:
            parts.append(self.path_prefix)
        return "".join(parts)


@dataclass(frozen=True)
class InputManifest:
    """Read-only references that identify the transcript and prior context.

    The manifest carries only stable, non-sensitive identifiers. The runtime is
    responsible for resolving these references through the worker-supplied,
    short-lived, read-only workspace; it never receives Storage credentials or
    a database connection string in this payload.
    """

    transcript_id: uuid.UUID
    account_id: uuid.UUID
    org_id: uuid.UUID
    opportunity_id: uuid.UUID | None = None
    prior_context_refs: list[str] = field(default_factory=list)
    runtime_params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for ref in self.prior_context_refs:
            if not ref or ".." in ref or ref.startswith(("/", "\\", "~")):
                raise RuntimeValidationError(f"Invalid prior context reference: {ref!r}")


@dataclass(frozen=True)
class Allowlist:
    """Tools and network destinations the skill is permitted to use.

    Tool names are opaque strings resolved by the runtime's typed tool registry.
    Generic tools such as `Bash`, `Git`, `Browser`, `Http`, `McpDiscover`, or
    `BypassPermissions` are never allowed and are rejected at contract build time.
    """

    tools: list[str] = field(default_factory=list)
    network: list[NetworkDestination] = field(default_factory=list)

    _FORBIDDEN_TOOLS: set[str] = field(
        default_factory=lambda: {
            "bash",
            "shell",
            "exec",
            "git",
            "browser",
            "chrome",
            "http",
            "https",
            "mcp",
            "mcpdiscover",
            "bypasspermissions",
        },
        repr=False,
    )

    def __post_init__(self) -> None:
        for name in self.tools:
            normalized = name.lower().replace("_", "").replace("-", "")
            if normalized in self._FORBIDDEN_TOOLS:
                raise RuntimeValidationError(f"Forbidden tool in allowlist: {name}")


@dataclass(frozen=True)
class RuntimeJob:
    """Everything the sandbox runtime needs to execute one attempt safely.

    Identity and input references are immutable once built. The worker must
    populate the cancellation predicate and execution deadline before handing
    the job to the runtime.
    """

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
    input_manifest: InputManifest | None = None
    prior_context_files: list[str] = field(default_factory=list)
    allowlist: Allowlist = field(default_factory=Allowlist)
    execution_deadline: datetime | None = None
    is_cancelled: Callable[[], bool] = field(default=lambda: False)
    output_workspace: str = "/tmp/runtime-output"

    def __post_init__(self) -> None:
        if self.execution_deadline is not None and self.execution_deadline.tzinfo is None:
            raise RuntimeValidationError("execution_deadline must be timezone-aware")


@dataclass(frozen=True)
class ValidationResult:
    """Deterministic validation of the produced Markdown and sidecar."""

    status: str  # "valid" | "invalid" | "unvalidated"
    errors: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.status not in {"valid", "invalid", "unvalidated"}:
            raise RuntimeValidationError(f"Invalid validation status: {self.status}")


@dataclass(frozen=True)
class RedactedFailure:
    """Categorized, non-sensitive failure information for the job ledger."""

    category: str
    message: str


@dataclass(frozen=True)
class RuntimeResult:
    """Result produced by the sandbox runtime.

    Either `output_artifact` and `sidecar` are present and `validation` says
    they are usable, or `failure` is present and neither output should be
    persisted as a validated artifact.
    """

    output_artifact: str | None = None
    sidecar: dict[str, Any] = field(default_factory=dict)
    actual_runtime_version: str | None = None
    actual_model: str | None = None
    token_usage: dict[str, int | float] = field(default_factory=dict)
    cost: float | None = None
    validation: ValidationResult | None = None
    failure: RedactedFailure | None = None

    def __post_init__(self) -> None:
        if self.output_artifact and self.failure:
            raise RuntimeValidationError("RuntimeResult cannot contain both output_artifact and failure")
        if not self.output_artifact and not self.failure:
            raise RuntimeValidationError("RuntimeResult must contain output_artifact or failure")



@runtime_checkable
class SkillRuntime(Protocol):
    """Protocol for an isolated sandbox runtime that executes one SE skill."""

    async def execute(self, job: RuntimeJob) -> RuntimeResult:
        """Execute the job in an isolated sandbox and return a `RuntimeResult`.

        Implementations must honour `job.execution_deadline`, `job.is_cancelled`,
        and `job.allowlist`. They must not read or write outside
        `job.output_workspace` and must not expose model credentials to sandbox
        tools or generated code.
        """
        ...
