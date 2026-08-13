"""Deterministic no-op/echo executor for Slice 4 job lifecycle testing.

This executor proves the worker claim, heartbeat, completion, failure, retry,
timeout, cancellation, and recovery paths without invoking a model, shell,
container, or the local skill runtime. It returns synthetic metadata only.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class ExecutorResult:
    """Synthetic result from a no-op/echo execution."""

    output_id: uuid.UUID | None
    validation_status: str
    token_usage: dict[str, Any]
    cost: float | None
    runtime_version: str
    model: str
    error_category: str | None = None
    error: str | None = None
    validation_errors: list[str] | None = None


@runtime_checkable
class Executor(Protocol):
    """Protocol for job executors."""

    async def execute(self, job: dict[str, Any]) -> ExecutorResult:
        """Execute the job and return a deterministic result."""
        ...


class EchoExecutor:
    """A deterministic executor that echoes job metadata as a synthetic result.

    This is used for Slice 4 to validate the job lifecycle without requiring a
    hosted agent runtime, model provider, or sandbox. It does not read
    transcript bodies or make outbound calls.
    """

    def __init__(self, runtime_version: str = "slice4-echo", model: str = "echo") -> None:
        self.runtime_version = runtime_version
        self.model = model

    async def execute(self, job: dict[str, Any]) -> ExecutorResult:
        token_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        return ExecutorResult(
            output_id=None,
            validation_status="unvalidated",
            token_usage=token_usage,
            cost=0.0,
            runtime_version=self.runtime_version,
            model=self.model,
        )
