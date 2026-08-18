"""gVisor `runsc` executor and the trusted worker-side runtime adapter.

`RunscSkillRuntime` is the `SkillRuntime` implementation that the worker passes to
`PostCallOrchestrator`. It prepares one ephemeral sandbox per attempt, issues a
short-lived model-proxy capability, starts a Unix-socket proxy, and invokes a
`SandboxRunner` to actually run the sandbox. `FakeSandboxRunner` allows the full
trusted boundary to be tested deterministically without `runsc`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import signal
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import ValidationError

from webapp.hosted import config
from webapp.hosted.agent_loop_harness import TypedToolRuntime
from webapp.hosted.model_proxy import ModelProxy, ProxyFinalization
from webapp.hosted.runtime_contract import (
    Allowlist,
    CancellationToken,
    ExecutionMetadata,
    NetworkDestination,
    RedactedFailure,
    RuntimeJob,
    RuntimeResult,
    SkillRuntime,
    TokenUsage,
)

logger = logging.getLogger(__name__)

MAX_RESULT_BYTES = 1_000_000
BROKER_EXIT_FAILURE_MESSAGES = {
    65: "sandbox state verification failed",
    66: "sandbox remained after delete",
}


class RuntimeExecutionError(Exception):
    """Raised when the sandbox runtime cannot be started or does not return a result."""


def _redacted_failure(category: str) -> RuntimeResult:
    """Return a redacted `RuntimeResult` failure."""
    from webapp.hosted.runtime_contract import FailureCategory

    return RuntimeResult(failure=RedactedFailure(category=category))  # type: ignore[arg-type]


def _map_runtime_execution_error(exc: Exception) -> RuntimeResult:
    """Map a sandbox runner failure to a closed `FailureCategory`.

    The messages are fixed strings emitted by the trusted worker, so this mapping
    does not propagate model-controlled or sandbox-controlled text.
    """
    if isinstance(exc, RuntimeExecutionError):
        msg = str(exc).lower()
        if "deadline" in msg or "timed out" in msg:
            return _redacted_failure("timeout")
        if "cancelled" in msg:
            return _redacted_failure("cancelled")
        if "state verification" in msg or "remained after delete" in msg:
            return _redacted_failure("cleanup_error")
        if "cleanup" in msg:
            return _redacted_failure("cleanup_error")
    return _redacted_failure("runtime_error")


def _raise_for_broker_exit(returncode: int, operation: str) -> None:
    """Map broker sentinels to fixed, closed runtime diagnostics."""
    if returncode == 65:
        raise RuntimeExecutionError(BROKER_EXIT_FAILURE_MESSAGES[65])
    if returncode == 66:
        raise RuntimeExecutionError(BROKER_EXIT_FAILURE_MESSAGES[66])
    raise RuntimeExecutionError(f"runsc {operation} exited with code {returncode}")


@runtime_checkable
class SandboxRunner(Protocol):
    """Abstract runner that executes the sandboxed runtime for one attempt."""

    async def run(
        self,
        job: RuntimeJob,
        input_dir: Path,
        output_dir: Path,
        result_path: Path,
        cancellation: CancellationToken,
        proxy_uds_path: Path | None = None,
    ) -> None:
        """Run the sandbox.

        The runner must write `result.json` to `result_path` and may read input
        from `input_dir` and write `output.md`/`sidecar.json` to `output_dir`.
        It must clean up any ephemeral state on cancellation, timeout, or error.
        """
        ...


class FakeSandboxRunner:
    """Deterministic in-process runner used for unit tests.

    It runs `TypedToolRuntime` with an `httpx.AsyncClient` whose transport is
    `httpx.MockTransport` wired to the real `ModelProxy.handle`. This exercises
    the proxy validation, request/response handling, and token binding without
    requiring `runsc` or a real Anthropic key.
    """

    def __init__(self, proxy: ModelProxy) -> None:
        self.proxy = proxy

    async def run(
        self,
        job: RuntimeJob,
        input_dir: Path,
        output_dir: Path,
        result_path: Path,
        cancellation: CancellationToken,
        proxy_uds_path: Path | None = None,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)

        if cancellation.is_cancelled():
            result = _redacted_failure("cancelled")
            result_path.write_text(result.model_dump_json(), encoding="utf-8")
            return

        # In the fake runner there is no container boundary, so the runtime uses
        # the host paths directly. The model client still talks through the proxy.
        host_job = job.model_copy(
            update={
                "input_workspace": str(input_dir),
                "output_workspace": str(output_dir),
            }
        )

        proxy_url = _proxy_base_url(host_job.allowlist.network)
        if proxy_uds_path is not None:
            transport: httpx.AsyncTransport = httpx.AsyncHTTPTransport(uds=str(proxy_uds_path))
        else:
            transport = httpx.MockTransport(self.proxy.handle)

        headers = {
            "Authorization": f"Bearer {host_job.proxy_token or ''}",
            "x-job-id": str(host_job.job_id),
            "x-attempt-number": str(host_job.attempt_number),
        }
        if host_job.attempt_id:
            headers["x-attempt-id"] = host_job.attempt_id

        client = httpx.AsyncClient(
            base_url=proxy_url,
            transport=transport,
            headers=headers,
            timeout=httpx.Timeout(60.0),
        )
        try:
            result = await TypedToolRuntime(model_client=client).execute(host_job, cancellation)
        finally:
            await client.aclose()

        result_path.write_text(result.model_dump_json(), encoding="utf-8")


class RunscSandboxRunner:
    """Production runner that invokes one `runsc` sandbox per attempt.

    This runner does not fall back to Docker, host execution, or an in-process
    runtime. If `runsc` or the rootfs is unavailable it raises `RuntimeError`.
    """

    def __init__(
        self,
        runsc_binary: str | None = None,
        runsc_helper: str | None = None,
        rootfs: str | None = None,
        network: str = "none",
        rootless: bool = False,
        extra_runsc_args: list[str] | None = None,
    ) -> None:
        self.runsc_binary = runsc_binary or config.RUNSC_BINARY
        self.runsc_helper = runsc_helper or config.RUNSC_HELPER_BINARY
        self.rootfs = rootfs or config.RUNSC_ROOTFS
        self.network = network
        self.rootless = rootless
        self.extra_runsc_args = extra_runsc_args or []

    async def run(
        self,
        job: RuntimeJob,
        input_dir: Path,
        output_dir: Path,
        result_path: Path,
        cancellation: CancellationToken,
        proxy_uds_path: Path | None = None,
    ) -> None:
        self._validate_prerequisites()
        self._validate_mount_paths(input_dir, output_dir, proxy_uds_path)

        container_id = f"se-{uuid.uuid4().hex[:12]}"
        root_dir = Path(config.RUNSC_STATE_DIR) / container_id
        try:
            request = {
                "operation": "run",
                "container_id": container_id,
                "input_dir": str(input_dir),
                "output_dir": str(output_dir),
                "proxy_uds_path": str(proxy_uds_path) if proxy_uds_path else None,
                "job": json.loads(job.model_dump_json()),
            }
            argv = self._build_argv(container_id)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if proc.stdin is None:
                raise RuntimeExecutionError("runsc broker stdin is unavailable")
            proc.stdin.write(json.dumps(request, sort_keys=True).encode() + b"\n")
            await proc.stdin.drain()
            proc.stdin.close()
            try:
                await self._wait_for_sandbox(proc, cancellation, job.execution_deadline)
            finally:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.send_signal(signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=10.0)
                await self._runsc_delete(root_dir, container_id)
        finally:
            # The broker owns bundle and state cleanup.
            pass

    def _validate_prerequisites(self) -> None:
        if not self.runsc_binary or not self.runsc_helper:
            raise RuntimeExecutionError("runsc binary is not configured")
        if not shutil.which(self.runsc_binary):
            raise RuntimeExecutionError(f"runsc binary not found: {self.runsc_binary}")
        if not shutil.which("sudo"):
            raise RuntimeExecutionError("sudo is not installed")
        if not shutil.which(self.runsc_helper):
            raise RuntimeExecutionError(f"runsc helper not found: {self.runsc_helper}")
        if not self.rootfs:
            raise RuntimeExecutionError("runsc rootfs is not configured")
        rootfs_path = Path(self.rootfs)
        if not rootfs_path.exists():
            raise RuntimeExecutionError(f"runsc rootfs does not exist: {self.rootfs}")
        if not rootfs_path.is_dir():
            raise RuntimeExecutionError(f"runsc rootfs is not a directory: {self.rootfs}")

    def _validate_mount_paths(
        self,
        input_dir: Path,
        output_dir: Path,
        proxy_uds_path: Path | None,
    ) -> None:
        for path, name, must_exist in [
            (input_dir, "input_dir", True),
            (output_dir, "output_dir", True),
        ]:
            if must_exist and not path.exists():
                raise RuntimeExecutionError(f"{name} does not exist: {path}")
            if path.is_symlink() or path.is_fifo() or path.is_block_device() or path.is_char_device():
                raise RuntimeExecutionError(f"{name} is not a regular directory: {path}")

        if proxy_uds_path is not None:
            if proxy_uds_path.is_symlink():
                raise RuntimeExecutionError("proxy socket must not be a symlink")
            # The socket file is created by the proxy server before runsc starts.
            if not proxy_uds_path.exists():
                raise RuntimeExecutionError("proxy socket does not exist")

    def _build_argv(self, container_id: str) -> list[str]:
        argv = ["sudo", "--non-interactive", self.runsc_helper]
        if self.rootless:
            raise RuntimeExecutionError("rootless runsc is not supported")
        if self.extra_runsc_args:
            raise RuntimeExecutionError("extra runsc arguments are not permitted")
        return argv

    @staticmethod
    async def _send_broker_request(
        proc: asyncio.subprocess.Process, request: dict[str, Any]
    ) -> None:
        if proc.stdin is None:
            raise RuntimeExecutionError("runsc broker stdin is unavailable")
        proc.stdin.write(json.dumps(request, sort_keys=True).encode() + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()

    def _build_config(
        self,
        bundle_dir: Path,
        root_dir: Path,
        job_path: Path,
        input_dir: Path,
        output_dir: Path,
        proxy_uds_path: Path | None,
        container_id: str,
    ) -> dict[str, Any]:
        rootfs_path = Path(self.rootfs).resolve()
        mounts: list[dict[str, Any]] = [
            {"destination": "/proc", "source": "proc", "type": "proc"},
            {
                "destination": "/tmp",
                "source": "tmpfs",
                "type": "tmpfs",
                "options": ["nosuid", "nodev", "noexec", "mode=1777", "size=128m"],
            },
            {
                "destination": "/runtime/job.json",
                "source": str(job_path.resolve()),
                "type": "bind",
                "options": ["bind", "ro"],
            },
            {
                "destination": "/runtime/input",
                "source": str(input_dir.resolve()),
                "type": "bind",
                "options": ["bind", "ro"],
            },
            {
                "destination": "/runtime/output",
                "source": str(output_dir.resolve()),
                "type": "bind",
                "options": ["bind", "rw"],
            },
        ]
        if proxy_uds_path is not None:
            mounts.append(
                {
                    "destination": "/runtime/proxy.sock",
                    "source": str(proxy_uds_path.resolve()),
                    "type": "bind",
                    "options": ["bind", "rw"],
                }
            )

        namespaces = [
            {"type": "pid"},
            {"type": "network"},
            {"type": "ipc"},
            {"type": "uts"},
            {"type": "mount"},
        ]
        # gVisor's own sandbox provides the user-namespace equivalent.  runsc
        # with `--rootless` creates a user namespace on unprivileged hosts.  We
        # keep the container process uid non-root and drop all capabilities.
        if self.rootless:
            namespaces.append({"type": "user"})

        return {
            "ociVersion": "1.1.0",
            "process": {
                "terminal": False,
                "user": {"uid": 65532, "gid": 65532, "umask": 27},
                "args": [
                    "/usr/bin/python3",
                    "/app/webapp/hosted/runsc/sandbox_entry.py",
                ],
                "env": [
                    "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "PYTHONPATH=/app/venv/lib/python3.11/site-packages:/app",
                    "SE_RUNTIME_JOB_PATH=/runtime/job.json",
                    "SE_RUNTIME_RESULT_PATH=/runtime/output/result.json",
                ],
                "cwd": "/tmp",
                "rlimits": [
                    {"type": "RLIMIT_CPU", "hard": 120, "soft": 120},
                    {"type": "RLIMIT_AS", "hard": 2_000_000_000, "soft": 2_000_000_000},
                    {"type": "RLIMIT_NOFILE", "hard": 1024, "soft": 1024},
                    {"type": "RLIMIT_FSIZE", "hard": 100_000_000, "soft": 100_000_000},
                    {"type": "RLIMIT_NPROC", "hard": 64, "soft": 64},
                ],
                "noNewPrivileges": True,
                "capabilities": {
                    "bounding": [],
                    "effective": [],
                    "permitted": [],
                    "inheritable": [],
                    "ambient": [],
                },
            },
            "root": {"path": str(rootfs_path), "readonly": True},
            "hostname": container_id,
            "mounts": mounts,
            "linux": {
                "namespaces": namespaces,
                "resources": {
                    "cpu": {"shares": 1024, "quota": 100000, "period": 100000},
                    "memory": {"limit": 2147483648, "reservation": 268435456},
                    "pids": {"limit": 64},
                },
                "maskedPaths": [
                    "/proc/acpi",
                    "/proc/asound",
                    "/proc/kcore",
                    "/proc/keys",
                    "/proc/latency_stats",
                    "/proc/timer_list",
                    "/proc/timer_stats",
                    "/proc/sched_debug",
                    "/proc/scsi",
                    "/sys/firmware",
                    "/sys/devices/virtual/powercap",
                ],
                "readonlyPaths": [
                    "/proc/bus",
                    "/proc/fs",
                    "/proc/irq",
                    "/proc/sys",
                    "/proc/sysrq-trigger",
                ],
            },
        }

    async def _wait_for_sandbox(
        self,
        proc: asyncio.subprocess.Process,
        cancellation: CancellationToken,
        deadline: datetime,
    ) -> None:
        while proc.returncode is None:
            now = datetime.now(tz=timezone.utc)
            remaining = max((deadline - now).total_seconds(), 0.0)
            if remaining <= 0:
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.send_signal(signal.SIGKILL)
                raise RuntimeExecutionError("sandbox exceeded execution deadline")
            try:
                await asyncio.wait_for(proc.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError:
                if cancellation.is_cancelled():
                    with contextlib.suppress(ProcessLookupError, OSError):
                        proc.send_signal(signal.SIGTERM)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    if proc.returncode is None:
                        with contextlib.suppress(ProcessLookupError, OSError):
                            proc.send_signal(signal.SIGKILL)
                    raise RuntimeExecutionError("sandbox cancelled")

        if proc.returncode != 0:
            # Do not capture or log raw sandbox stderr.
            logger.warning("runsc exited with code %s", proc.returncode)
            raise RuntimeExecutionError(f"runsc exited with code {proc.returncode}")

    def _build_delete_argv(
        self, root_dir: Path, container_id: str
    ) -> list[str]:
        """Build the `runsc delete` argv for the per-attempt root directory."""
        if self.rootless:
            raise RuntimeExecutionError("rootless runsc is not supported")
        return ["sudo", "--non-interactive", self.runsc_helper]

    def _build_list_argv(self, root_dir: Path) -> list[str]:
        """Build the `runsc list` argv for verifying container cleanup."""
        if self.rootless:
            raise RuntimeExecutionError("rootless runsc is not supported")
        return ["sudo", "--non-interactive", self.runsc_helper]

    async def _is_container_gone(
        self, root_dir: Path, container_id: str
    ) -> bool:
        """Return True if gVisor reports the container no longer exists.

        `runsc list` is used rather than relying on the filesystem, because the
        sandbox process can outlive its state directory if delete fails.  The
        command must exit 0; any non-zero exit or unparseable output is treated
        as "not gone" and fails closed.
        """
        argv = self._build_list_argv(root_dir)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        await self._send_broker_request(
            proc,
            {
                "operation": "list",
                "container_id": container_id,
                "state_dir": str(root_dir),
            },
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=10.0
            )
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            raise RuntimeExecutionError("runsc list timed out during cleanup")

        if proc.returncode != 0:
            _raise_for_broker_exit(proc.returncode, "list")

        lines = stdout.decode("utf-8", errors="replace").splitlines()
        if not lines or not lines[0].startswith("ID"):
            raise RuntimeExecutionError("runsc list produced unparseable output")

        for line in lines[1:]:
            parts = line.split()
            if parts and parts[0] == container_id:
                return False
        return True

    async def _runsc_delete(
        self, root_dir: Path, container_id: str
    ) -> None:
        """Delete the gVisor container and verify it is gone.

        Uses the same `--root` that was passed to `runsc run`, requires a clean
        exit, and confirms the container no longer appears in `runsc list`.
        """
        argv = self._build_delete_argv(root_dir, container_id)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        await self._send_broker_request(
            proc,
            {
                "operation": "delete",
                "container_id": container_id,
                "state_dir": str(root_dir),
            },
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            if proc.returncode is None:
                raise RuntimeExecutionError(
                    "runsc delete did not terminate after SIGKILL"
                )
            raise RuntimeExecutionError("runsc delete timed out")

        if proc.returncode != 0:
            _raise_for_broker_exit(proc.returncode, "delete")



class RunscSkillRuntime:
    """SkillRuntime adapter that runs one ephemeral gVisor `runsc` sandbox per attempt."""

    def __init__(
        self,
        runner: SandboxRunner,
        proxy: ModelProxy,
        start_proxy_server: bool = True,
        workspace_root: Path | None = None,
    ) -> None:
        self.runner = runner
        self.proxy = proxy
        self.start_proxy_server = start_proxy_server
        self.workspace_root = workspace_root

    async def _finalize_proxy_session(self, jti: str | None) -> ProxyFinalization:
        """Signal cancellation and consume trusted proxy state once."""
        self.proxy.cancel_session(jti)
        return await self.proxy.consume_attempt_finalization(jti)

    @staticmethod
    def _terminal_failure(finalization: ProxyFinalization) -> RuntimeResult | None:
        if finalization.terminal_category is None:
            return None
        return _redacted_failure(finalization.terminal_category)

    async def execute(self, job: RuntimeJob, cancellation: CancellationToken) -> RuntimeResult:
        if cancellation.is_cancelled():
            return _redacted_failure("cancelled")

        try:
            input_dir = Path(job.input_workspace)
            output_dir = Path(job.output_workspace)
        except Exception:
            return _redacted_failure("configuration_error")

        # The sandbox sees fixed container paths; the host paths are bind-mounted.
        sandbox_network = frozenset([NetworkDestination(host="worker-proxy", scheme="http")])
        sandbox_job = job.model_copy(
            update={
                "input_workspace": "/runtime/input",
                "output_workspace": "/runtime/output",
                "allowlist": Allowlist(
                    tools=job.allowlist.tools,
                    network=sandbox_network,
                ),
            }
        )

        proxy_uds_host_path: Path | None = None
        server_task: asyncio.Task | None = None
        server: Any | None = None
        jti: str | None = None
        try:
            if self.start_proxy_server:
                try:
                    proxy_uds_host_path, server_task, server = await self._start_proxy_server(sandbox_job)
                except Exception as exc:
                    logger.warning("Failed to start model proxy server: %s", type(exc).__name__)
                    return _redacted_failure("runtime_error")

            token, jti, attempt_id = self.proxy.issue_capability(
                sandbox_job,
                attempt_number=sandbox_job.attempt_number,
            )
            sandbox_job = sandbox_job.model_copy(
                update={
                    "attempt_id": attempt_id,
                    "proxy_token": token,
                    "proxy_uds_path": "/runtime/proxy.sock" if proxy_uds_host_path else None,
                }
            )

            result_path = output_dir / "result.json"
            runner_coro = self.runner.run(
                sandbox_job,
                input_dir,
                output_dir,
                result_path,
                cancellation,
                proxy_uds_path=proxy_uds_host_path,
            )

            runner_task = asyncio.create_task(runner_coro)
            cancel_task = asyncio.create_task(cancellation.wait())
            try:
                done, pending = await asyncio.wait(
                    {runner_task, cancel_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for p in pending:
                    p.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await p
            except asyncio.CancelledError:
                runner_task.cancel()
                cancel_task.cancel()
                await self._finalize_proxy_session(jti)
                raise

            if cancel_task in done:
                if not runner_task.done():
                    runner_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await runner_task
                await self._finalize_proxy_session(jti)
                return _redacted_failure("cancelled")

            try:
                await runner_task
            except RuntimeExecutionError as exc:
                logger.warning("Runner raised RuntimeExecutionError: %s", exc)
                finalization = await self._finalize_proxy_session(jti)
                terminal_failure = self._terminal_failure(finalization)
                if terminal_failure is not None:
                    return terminal_failure
                return _map_runtime_execution_error(exc)
            except Exception as exc:
                logger.warning("Runner raised: %s", type(exc).__name__)
                finalization = await self._finalize_proxy_session(jti)
                terminal_failure = self._terminal_failure(finalization)
                if terminal_failure is not None:
                    return terminal_failure
                return _redacted_failure("runtime_error")

            result = _safe_read_runtime_result(result_path)
            if result is None:
                finalization = await self._finalize_proxy_session(jti)
                terminal_failure = self._terminal_failure(finalization)
                if terminal_failure is not None:
                    return terminal_failure
                return _redacted_failure("runtime_error")

            finalization = await self._finalize_proxy_session(jti)
            terminal_failure = self._terminal_failure(finalization)
            if terminal_failure is not None:
                return terminal_failure
            authoritative = finalization.metadata
            if result.failure is None:
                result = result.model_copy(
                    update={
                        "execution_metadata": ExecutionMetadata(
                            runtime_version=sandbox_job.requested_runtime_version,
                            model=authoritative.model,
                            token_usage=authoritative.token_usage,
                            cost=authoritative.cost,
                        )
                    }
                )
            return result
        finally:
            if server is not None:
                server.should_exit = True
                if hasattr(server, "force_exit"):
                    server.force_exit = True
            if server_task is not None:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(server_task, timeout=2.0)
                if not server_task.done():
                    server_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await server_task
            if proxy_uds_host_path is not None:
                run_dir = proxy_uds_host_path.parent
                with contextlib.suppress(OSError):
                    shutil.rmtree(run_dir, ignore_errors=True)

    async def _start_proxy_server(self, job: RuntimeJob) -> tuple[Path, asyncio.Task, Any]:
        """Start the `ModelProxy` ASGI server on a Unix domain socket.

        The socket path is returned so the runner can bind-mount it into the sandbox.
        """
        import uvicorn
        from uvicorn.config import Config

        workspace_root = self.workspace_root or Path(config.RUNSC_WORKSPACE_ROOT)
        run_dir = Path(tempfile.mkdtemp(prefix="se-proxy-", dir=workspace_root))
        uds_path = run_dir / "proxy.sock"
        app = self.proxy.create_app()
        uvicorn_config = Config(
            app=app,
            uds=str(uds_path),
            loop="asyncio",
            log_level="warning",
            limit_max_requests=100,
        )
        server = uvicorn.Server(uvicorn_config)
        server_task = asyncio.create_task(server.serve())

        # Wait for the socket file to be created or the server to fail.
        for _ in range(50):
            if uds_path.exists():
                return uds_path, server_task, server
            if server_task.done():
                await server_task
                raise RuntimeExecutionError("proxy server failed to start")
            await asyncio.sleep(0.05)

        if not uds_path.exists():
            server.should_exit = True
            raise RuntimeExecutionError("proxy server did not create socket in time")

        return uds_path, server_task, server


def _safe_read_runtime_result(result_path: Path, max_bytes: int = MAX_RESULT_BYTES) -> RuntimeResult | None:
    """Read `result.json` safely from a potentially hostile filesystem.

    Uses `O_NOFOLLOW | O_NONBLOCK`, verifies a regular file via `fstat`, and
    enforces a small byte cap.  Symlinks, FIFOs, devices, and oversized files
    are rejected.
    """
    try:
        fd = os.open(str(result_path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if st.st_size > max_bytes:
            return None
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(max_bytes + 1)
        if len(data) > max_bytes:
            return None
        try:
            return RuntimeResult.model_validate_json(data.decode("utf-8", errors="replace"))
        except ValidationError:
            return None
    finally:
        os.close(fd)


def _proxy_base_url(network: frozenset[NetworkDestination]) -> str:
    """Return the single allowed model proxy destination as a base URL."""
    from webapp.hosted.agent_loop_harness import _proxy_base_url as harness_proxy_url

    return harness_proxy_url(network)


def create_runsc_runtime(
    runsc_binary: str | None = None,
    rootfs: str | None = None,
    network: str = "none",
    rootless: bool = False,
) -> SkillRuntime:
    """Factory that builds a `RunscSkillRuntime` backed by a real `runsc` runner.

    Fails closed during execution if `runsc` or the rootfs is unavailable.
    """
    runner = RunscSandboxRunner(
        runsc_binary=runsc_binary,
        rootfs=rootfs,
        network=network,
        rootless=rootless,
    )
    proxy = ModelProxy()
    return RunscSkillRuntime(runner=runner, proxy=proxy, start_proxy_server=True)
