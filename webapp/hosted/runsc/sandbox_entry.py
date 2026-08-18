#!/usr/bin/env -S python3
"""Sandbox entry point executed inside the gVisor `runsc` container.

This script is the only code that runs inside the sandbox. It reads the
serialized `RuntimeJob` from a worker-mounted file, runs the typed-tool loop,
and writes `output.md`, `sidecar.json`, and `result.json` to the designated
output directory. It has no network, filesystem, or credential access beyond
what the worker explicitly mounts.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from webapp.hosted.agent_loop_harness import TypedToolRuntime
from webapp.hosted.runtime_contract import RedactedFailure, RuntimeJob, RuntimeResult


class _Cancellation:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def cancel(self) -> None:
        self._event.set()


def _write_result(result: RuntimeResult, result_path: Path) -> None:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(result.model_dump_json(), encoding="utf-8")


def _read_job(job_path: Path) -> RuntimeJob | None:
    try:
        return RuntimeJob.model_validate_json(job_path.read_text(encoding="utf-8"))
    except Exception:
        return None


async def main() -> int:
    job_path = Path(os.environ.get("SE_RUNTIME_JOB_PATH", "/runtime/job.json"))
    result_path = Path(os.environ.get("SE_RUNTIME_RESULT_PATH", "/runtime/output/result.json"))

    job = _read_job(job_path)
    if job is None:
        _write_result(
            RuntimeResult(failure=RedactedFailure(category="input_error")),
            result_path,
        )
        return 1

    cancellation = _Cancellation()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, cancellation.cancel)

    try:
        result = await TypedToolRuntime().execute(job, cancellation)
    except Exception:
        result = RuntimeResult(failure=RedactedFailure(category="runtime_error"))

    _write_result(result, result_path)
    return 0 if result.failure is None else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
