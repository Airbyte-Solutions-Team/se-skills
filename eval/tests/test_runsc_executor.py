"""Deterministic tests for the gVisor `runsc` runtime adapter and sandbox runner.

Tests that do not need `runsc` exercise the fake runner and `RunscSandboxRunner`
configuration generation. A single gated integration test is skipped when `runsc`
is unavailable.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest

from webapp.hosted import config as hosted_config
from webapp.hosted.model_proxy import ModelProxy, ProxyConfig
from webapp.hosted.runtime_contract import (
    Allowlist,
    InputManifest,
    NetworkDestination,
    RuntimeJob,
)
from webapp.hosted.runsc_executor import (
    FakeSandboxRunner,
    RuntimeExecutionError,
    RunscSandboxRunner,
    RunscSkillRuntime,
    _map_runtime_execution_error,
    _raise_for_broker_exit,
    _safe_read_runtime_result,
)


class _CancellationToken:
    """Minimal cancellation token for tests."""

    def __init__(self) -> None:
        self._cancelled = False

    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    async def wait(self) -> None:
        import asyncio

        while not self._cancelled:
            await asyncio.sleep(0.05)


class _FakeCancellationToken:
    """No-op token for successful-run tests."""

    def is_cancelled(self) -> bool:
        return False

    async def wait(self) -> None:
        import asyncio

        await asyncio.sleep(100000)


class _SlowDripStream:
    def __init__(self, closed: list[bool]) -> None:
        self.closed = closed

    async def __aenter__(self) -> "_SlowDripStream":
        return self

    async def __aexit__(self, *args: Any) -> None:
        self.closed[0] = True

    def raise_for_status(self) -> None:
        pass

    async def aiter_bytes(self) -> Any:
        while True:
            await asyncio.sleep(0.05)
            yield b'{"id":"msg","type":"message","role":"assistant","content":[]}'


class _SlowDripClient:
    def __init__(self, closed: list[bool]) -> None:
        self.closed = closed

    def stream(
        self,
        method: str,
        url: str,
        *,
        content: bytes,
        headers: dict[str, str],
        timeout: Any,
    ) -> _SlowDripStream:
        return _SlowDripStream(self.closed)

    async def aclose(self) -> None:
        pass


class _RecordingFakeSandboxRunner(FakeSandboxRunner):
    def __init__(self, proxy: ModelProxy) -> None:
        super().__init__(proxy)
        self.last_jti: str | None = None

    async def run(
        self,
        job: RuntimeJob,
        input_dir: Path,
        output_dir: Path,
        result_path: Path,
        cancellation: Any,
        proxy_uds_path: Path | None = None,
    ) -> None:
        self.last_jti = jwt.decode(
            job.proxy_token or "",
            self.proxy.cfg.secret,
            algorithms=["HS256"],
            options={"verify_exp": False},
        )["jti"]
        await super().run(
            job,
            input_dir,
            output_dir,
            result_path,
            cancellation,
            proxy_uds_path,
        )


def _make_job(
    input_dir: Path,
    output_dir: Path,
    deadline: datetime | None = None,
    tools: frozenset[str] | None = None,
    requested_model: str = "claude-sonnet-4-6",
    transcript_text: str = "hello world",
    mode: str = "full",
) -> RuntimeJob:
    if tools is None:
        tools = frozenset(
            ["read_transcript", "write_output", "finish"]
        )
    job_id = uuid4()
    org_id = uuid4()
    account_id = uuid4()
    transcript_id = uuid4()
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")
    return RuntimeJob(
        job_id=job_id,
        org_id=org_id,
        account_id=account_id,
        transcript_id=transcript_id,
        requester_id=uuid4(),
        requested_model=requested_model,
        attempt_number=1,
        input_manifest=InputManifest(
            transcript_id=transcript_id,
            transcript_ref="transcript.txt",
            account_id=account_id,
            org_id=org_id,
        ),
        execution_deadline=deadline
        or datetime.now(tz=timezone.utc) + timedelta(minutes=5),
        input_workspace=str(input_dir),
        output_workspace=str(output_dir),
        allowlist=Allowlist(
            tools=tools,
            network=frozenset([NetworkDestination(host="worker-proxy", scheme="http")]),
        ),
        mode=mode,  # type: ignore[arg-type]
    )


def _upstream_response_factory() -> Any:
    """Return a fake Anthropic upstream that completes a post-call workflow."""
    calls: list[int] = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        calls[0] += 1
        if calls[0] == 1:
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
                                "markdown": "# Test\n\n## At a Glance\n\nSummary.\n\n## Sources & Destinations\n\nSystems.\n\n## Technical Notes\n\nScope.\n\n## MEDDPICC Quick Pass\n\nDiscovery.\n",
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
        return httpx.Response(
            200,
            json={
                "id": "msg-2",
                "type": "message",
                "role": "assistant",
                "model": body.get("model", "claude-sonnet-4-6"),
                "content": [
                    {"type": "tool_use", "id": "tu2", "name": "finish", "input": {}}
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 20, "output_tokens": 5},
            },
        )

    return handler


def _runtime(workspace_root: Path | None = None) -> RunscSkillRuntime:
    if workspace_root is None:
        workspace_root = Path(tempfile.mkdtemp(prefix="runsc-test-workspace-"))
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_response_factory())
        ),
    )
    return RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy),
        proxy=proxy,
        start_proxy_server=True,
        workspace_root=workspace_root,
    )


@pytest.mark.asyncio
async def test_runsc_runtime_fake_runner_success() -> None:
    """The `RunscSkillRuntime` adapter completes a post-call via the fake runner."""
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        workspace_root = Path(td) / "workspace"
        workspace_root.mkdir()
        runtime = _runtime(workspace_root)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is None
        assert (output_dir / "output.md").exists()
        assert (output_dir / "sidecar.json").exists()
        assert "## At a Glance" in (output_dir / "output.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_runsc_runtime_fake_runner_without_server() -> None:
    """The fake runner can also use a direct `httpx.MockTransport` to the proxy."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_response_factory())
        ),
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is None


@pytest.mark.asyncio
async def test_runsc_runtime_proxy_rejection_surfaces_model_error() -> None:
    """A proxy that rejects the sandbox request returns a redacted `model_error`."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(502))
        ),
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is not None
        assert result.failure.category == "model_error"


@pytest.mark.asyncio
async def test_runsc_runtime_proxy_deadline_overrides_sandbox_model_error() -> None:
    """A proxy-owned deadline wins over the typed runner's model error."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    closed = [False]
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=_SlowDripClient(closed),
    )
    runner = _RecordingFakeSandboxRunner(proxy)
    runtime = RunscSkillRuntime(runner=runner, proxy=proxy, start_proxy_server=False)
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(
            input_dir,
            output_dir,
            deadline=datetime.now(tz=timezone.utc) + timedelta(seconds=0.3),
        )
        result = await runtime.execute(job, _FakeCancellationToken())

    assert result.failure is not None
    assert result.failure.category == "timeout"
    assert "model_error" not in result.model_dump_json()
    assert "sandbox" not in result.model_dump_json().lower()
    assert closed[0]
    assert proxy._sessions == {}
    assert runner.last_jti is not None
    second = await proxy.consume_attempt_finalization(runner.last_jti)
    assert second.metadata.token_usage.input_tokens == 0
    assert second.terminal_category is None


@pytest.mark.asyncio
async def test_runsc_runtime_cancellation_returns_cancelled() -> None:
    """Cancelling the runtime during execution stops the runner and returns cancelled."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")

    async def slow(request: httpx.Request) -> httpx.Response:
        import asyncio

        await asyncio.sleep(60)
        return httpx.Response(200, json={"id": "x"})

    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),  # placeholder
            timeout=httpx.Timeout(60.0),
        ),
    )
    # The slow transport is actually used by the proxy; replace the client.
    proxy.anthropic_client = httpx.AsyncClient(
        transport=httpx.MockTransport(slow), timeout=httpx.Timeout(60.0)
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        token = _CancellationToken()
        token.cancel()
        result = await runtime.execute(job, token)
        assert result.failure is not None
        assert result.failure.category == "cancelled"


@pytest.mark.asyncio
async def test_runsc_runtime_uses_authoritative_metadata() -> None:
    """The trusted worker overwrites sandbox-authored model/usage with the proxy ledger."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_response_factory())
        ),
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is None
        assert result.execution_metadata.model == "claude-sonnet-4-6"
        assert result.execution_metadata.token_usage.input_tokens == 30
        assert result.execution_metadata.token_usage.output_tokens == 10
        assert result.execution_metadata.cost is not None


@pytest.mark.asyncio
async def test_runsc_runtime_rejects_forged_metadata() -> None:
    """A `result.json` with forged usage is overwritten by the proxy ledger."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_response_factory())
        ),
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is None
        # The sandbox might claim 999999 tokens; the ledger should not.
        assert result.execution_metadata.token_usage.input_tokens == 30


@pytest.mark.asyncio
async def test_runsc_sandbox_runner_rejects_rootless_mode() -> None:
    """The hosted runtime uses the constrained root launcher, not rootless mode."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/var/lib/runsc/rootfs",
        network="none",
        rootless=True,
    )
    with pytest.raises(RuntimeExecutionError, match="rootless"):
        runner._build_argv("se-abc123")


@pytest.mark.asyncio
async def test_runsc_sandbox_runner_config_includes_isolation() -> None:
    """The generated OCI config enforces non-root execution, read-only root, and limits."""
    with tempfile.TemporaryDirectory() as td:
        rootfs = Path(td) / "rootfs"
        rootfs.mkdir()
        input_dir = Path(td) / "input"
        input_dir.mkdir()
        output_dir = Path(td) / "output"
        output_dir.mkdir()
        socket_path = Path(td) / "proxy.sock"
        socket_path.touch()
        bundle_dir = Path(td) / "bundle"
        bundle_dir.mkdir()
        root_dir = bundle_dir / "root"
        root_dir.mkdir()
        job_path = bundle_dir / "job.json"
        job_path.write_text("{}", encoding="utf-8")

        runner = RunscSandboxRunner(runsc_binary="/fake/runsc", rootfs=str(rootfs))
        config = runner._build_config(
            bundle_dir=bundle_dir,
            root_dir=root_dir,
            job_path=job_path,
            input_dir=input_dir,
            output_dir=output_dir,
            proxy_uds_path=socket_path,
            container_id="se-test",
        )

        assert config["process"]["user"]["uid"] != 0
        assert config["process"]["user"]["gid"] != 0
        assert config["root"]["readonly"] is True
        assert config["process"]["noNewPrivileges"] is True
        assert config["process"]["capabilities"]["bounding"] == []

        mounts = {m["destination"]: m for m in config["mounts"]}
        assert "/runtime/input" in mounts
        assert "/runtime/output" in mounts
        assert "/runtime/job.json" in mounts
        assert "/runtime/proxy.sock" in mounts
        assert mounts["/runtime/input"]["options"] == ["bind", "ro"]
        assert mounts["/runtime/output"]["options"] == ["bind", "rw"]

        rlimits = {r["type"]: r for r in config["process"]["rlimits"]}
        assert "RLIMIT_CPU" in rlimits
        assert "RLIMIT_AS" in rlimits
        assert "RLIMIT_NOFILE" in rlimits
        assert "RLIMIT_FSIZE" in rlimits
        assert "RLIMIT_NPROC" in rlimits


@pytest.mark.asyncio
async def test_runsc_sandbox_runner_rejects_symlink_mounts() -> None:
    """The runner refuses to bind-mount a symlink input or output directory."""
    with tempfile.TemporaryDirectory() as td:
        real_dir = Path(td) / "real"
        real_dir.mkdir()
        symlink_dir = Path(td) / "link"
        symlink_dir.symlink_to(real_dir, target_is_directory=True)
        runner = RunscSandboxRunner(runsc_binary="/fake/runsc", rootfs=str(real_dir))
        with pytest.raises(Exception):
            runner._validate_mount_paths(symlink_dir, real_dir, None)


@pytest.mark.asyncio
async def test_runsc_sandbox_runner_fails_closed_without_runsc() -> None:
    """`RunscSandboxRunner` raises before running if `runsc` is missing."""
    runner = RunscSandboxRunner(runsc_binary="/does/not/exist/runsc", rootfs="/tmp")
    with pytest.raises(Exception):
        runner._validate_prerequisites()


@pytest.mark.asyncio
async def test_runsc_sandbox_runner_rejects_missing_rootfs() -> None:
    """`RunscSandboxRunner` raises before running if the rootfs is missing."""
    runner = RunscSandboxRunner(runsc_binary="runsc", rootfs="/does/not/exist")
    with pytest.raises(Exception):
        runner._validate_prerequisites()


@pytest.mark.asyncio
async def test_runsc_runtime_sandbox_job_has_no_anthropic_key() -> None:
    """The serialized job file written for the sandbox never contains the API key."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(502))
        ),
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        # The proxy returned an error because the fake runner had no upstream, but
        # the proxy token is still issued from `job` and does not contain the key.
        assert "test-key" not in (output_dir / "result.json").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_safe_read_runtime_result_rejects_symlink() -> None:
    """`_safe_read_runtime_result` refuses to follow symlinks."""
    with tempfile.TemporaryDirectory() as td:
        output_dir = Path(td) / "output"
        output_dir.mkdir()
        real = output_dir / "real.json"
        real.write_text("{}", encoding="utf-8")
        link = output_dir / "result.json"
        link.symlink_to(real)
        assert _safe_read_runtime_result(link) is None


@pytest.mark.asyncio
async def test_safe_read_runtime_result_rejects_fifo() -> None:
    """`_safe_read_runtime_result` refuses to read a FIFO."""
    with tempfile.TemporaryDirectory() as td:
        output_dir = Path(td) / "output"
        output_dir.mkdir()
        fifo = output_dir / "result.json"
        os.mkfifo(str(fifo))
        assert _safe_read_runtime_result(fifo) is None


@pytest.mark.asyncio
async def test_safe_read_runtime_result_rejects_oversized() -> None:
    """`_safe_read_runtime_result` rejects files larger than the configured cap."""
    with tempfile.TemporaryDirectory() as td:
        output_dir = Path(td) / "output"
        output_dir.mkdir()
        path = output_dir / "result.json"
        path.write_bytes(b"x" * 1_000_001)
        assert _safe_read_runtime_result(path, max_bytes=1_000_000) is None


@pytest.mark.asyncio
async def test_runsc_runtime_result_symlink_returns_runtime_error() -> None:
    """A symlink `result.json` causes the runtime to return a redacted `runtime_error`."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_response_factory())
        ),
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        real = output_dir / "real.json"
        real.write_text("{}", encoding="utf-8")
        (output_dir / "result.json").symlink_to(real)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is not None
        assert result.failure.category == "runtime_error"


@pytest.mark.asyncio
async def test_runsc_runtime_overrides_forged_metadata() -> None:
    """A malicious runner cannot inflate the persisted model, token, or cost accounting."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")

    class _ForgingRunner:
        async def run(
            self,
            job: RuntimeJob,
            input_dir: Path,
            output_dir: Path,
            result_path: Path,
            cancellation: Any,
            proxy_uds_path: Path | None = None,
        ) -> None:
            # The real runtime would have made model calls through the proxy, but
            # this forged result claims impossible usage and a different model.
            forged = {
                "output_artifact": "# Forged",
                "sidecar": {
                    "skill": job.skill,
                    "skill_version": job.skill_version,
                    "mode": job.mode,
                },
                "execution_metadata": {
                    "runtime_version": "forged",
                    "model": "claude-opus-forged",
                    "token_usage": {
                        "input_tokens": 999999,
                        "output_tokens": 999999,
                        "cache_creation_input_tokens": 999999,
                        "cache_read_input_tokens": 999999,
                        "total_tokens": 3999996,
                    },
                    "cost": 9999.0,
                },
            }
            result_path.write_text(json.dumps(forged), encoding="utf-8")

    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(502))
        ),
    )
    runtime = RunscSkillRuntime(
        runner=_ForgingRunner(), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is None
        # The proxy ledger saw zero model calls and overrides the sandbox claim.
        assert result.execution_metadata.model != "claude-opus-forged"
        assert result.execution_metadata.token_usage.input_tokens == 0
        assert result.execution_metadata.token_usage.output_tokens == 0
        assert result.execution_metadata.cost != 9999.0


@pytest.mark.asyncio
async def test_runsc_runtime_removes_proxy_directory() -> None:
    """After execution the runtime removes the entire proxy run directory, not just the socket."""
    import tempfile as _tmp

    runtime = _runtime()
    before = _se_proxy_dirs()
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        await runtime.execute(job, _FakeCancellationToken())
    # The proxy server creates a run_dir under the system temp dir; it should be removed.
    after = _se_proxy_dirs()
    new_dirs = after - before
    assert not new_dirs


class _FakeSubprocess:
    """Minimal asyncio.Process stand-in for unit tests."""

    def __init__(
        self,
        returncode: int | None = 0,
        wait_delay: float | None = None,
        list_stdout: bytes = b"",
    ) -> None:
        self.returncode = returncode
        self._wait_delay = wait_delay
        self._list_stdout = list_stdout
        self.killed = False
        self.stdin = _FakeStdin()

    async def wait(self) -> int | None:
        if self._wait_delay:
            await asyncio.sleep(self._wait_delay)
        if self.killed and self.returncode is None:
            self.returncode = -9
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self._wait_delay = 0

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._wait_delay:
            await asyncio.sleep(self._wait_delay)
        return (self._list_stdout, b"")


class _FakeStdin:
    def __init__(self) -> None:
        self.data = bytearray()

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_runsc_sandbox_finalize_argv_structure() -> None:
    """Finalize uses only the root-owned broker argv; request details stay on stdin."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/var/lib/runsc/rootfs",
        network="none",
        rootless=False,
    )
    argv = runner._build_finalize_argv()
    assert argv == ["sudo", "--non-interactive", "/usr/local/sbin/se-skills-runsc"]
    for arg in argv:
        assert ";" not in arg
        assert "|" not in arg
        assert "&" not in arg
        assert "`" not in arg
        assert "$" not in arg

@pytest.mark.asyncio
async def test_runsc_sandbox_finalize_rejects_rootless_mode() -> None:
    """Finalize also rejects the unsupported rootless mode."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/var/lib/runsc/rootfs",
        network="none",
        rootless=True,
    )
    with pytest.raises(RuntimeExecutionError, match="rootless"):
        runner._build_finalize_argv()


@pytest.mark.asyncio
async def test_runsc_sandbox_list_argv_structure() -> None:
    """List uses only the root-owned broker argv; format is in the request."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/var/lib/runsc/rootfs",
        network="none",
        rootless=False,
    )
    argv = runner._build_list_argv(Path("/bundle/root"))
    assert argv == ["sudo", "--non-interactive", "/usr/local/sbin/se-skills-runsc"]



@pytest.mark.asyncio
async def test_runsc_sandbox_list_nonzero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_is_container_gone` raises if `runsc list` exits non-zero."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc", rootfs="/var/lib/runsc/rootfs"
    )
    root_dir = Path(tempfile.mkdtemp()) / "root"
    root_dir.mkdir()

    async def fake_exec(*args: str, **kwargs: Any) -> _FakeSubprocess:
        return _FakeSubprocess(returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(Exception) as exc:
        await runner._is_container_gone(root_dir, "se-test")
    assert "runsc list exited" in str(exc.value).lower()


@pytest.mark.parametrize(
    ("returncode", "message"),
    [(65, "state verification"), (66, "sandbox remained after finalize")],
)
def test_broker_exit_sentinels_map_to_closed_cleanup_failures(
    returncode: int, message: str
) -> None:
    """Broker sentinels never escape as undocumented generic failures."""
    with pytest.raises(RuntimeExecutionError, match=message):
        _raise_for_broker_exit(returncode, "finalize")
    result = _map_runtime_execution_error(
        RuntimeExecutionError(message)
    )
    assert result.failure is not None
    assert result.failure.category == "cleanup_error"


@pytest.mark.asyncio
async def test_runsc_sandbox_list_unparseable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_is_container_gone` raises if `runsc list` returns exit 0 but no parseable header."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc", rootfs="/var/lib/runsc/rootfs"
    )
    root_dir = Path(tempfile.mkdtemp()) / "root"
    root_dir.mkdir()

    async def fake_exec(*args: str, **kwargs: Any) -> _FakeSubprocess:
        return _FakeSubprocess(returncode=0, list_stdout=b"\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(Exception) as exc:
        await runner._is_container_gone(root_dir, "se-test")
    assert "unparseable" in str(exc.value).lower()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_runsc_sandbox_state_directory_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`RunscSandboxRunner.run` removes the bundle/state directory on exit."""
    with tempfile.TemporaryDirectory() as td:
        durable_root = Path(td) / "runsc"
        durable_bundles = Path(td) / "bundles"
        monkeypatch.setattr(hosted_config, "RUNSC_STATE_DIR", str(durable_root))
        monkeypatch.setattr(hosted_config, "RUNSC_BUNDLE_DIR", str(durable_bundles))
        rootfs = Path(td) / "rootfs"
        rootfs.mkdir()
        input_dir = Path(td) / "input"
        input_dir.mkdir()
        output_dir = Path(td) / "output"
        output_dir.mkdir()

        # Fake `runsc` exits 0 and prints a valid `runsc list` header when asked to list.
        fake_runsc = Path(td) / "fake-runsc"
        fake_runsc.write_text(
            '#!/bin/sh\nfor arg in "$@"; do\n  case "$arg" in\n    list) printf "ID\\tPID\\tSTATUS\\n"; exit 0 ;;\n  esac\ndone\nexit 0\n',
            encoding="utf-8",
        )
        fake_runsc.chmod(0o755)

        runner = RunscSandboxRunner(
            runsc_binary=str(fake_runsc),
            runsc_helper=str(fake_runsc),
            rootfs=str(rootfs),
            network="none",
            rootless=False,
        )

        job = _make_job(input_dir, output_dir)
        await runner.run(
            job,
            input_dir,
            output_dir,
            output_dir / "result.json",
            _FakeCancellationToken(),
            proxy_uds_path=None,
        )
        assert not durable_root.exists()
        assert not durable_bundles.exists()


@pytest.mark.asyncio
async def test_runsc_finalize_failure_preserves_state_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed broker cleanup leaves the durable state for investigation."""
    with tempfile.TemporaryDirectory() as td:
        durable_root = Path(td) / "runsc"
        durable_bundles = Path(td) / "bundles"
        monkeypatch.setattr(hosted_config, "RUNSC_STATE_DIR", str(durable_root))
        monkeypatch.setattr(hosted_config, "RUNSC_BUNDLE_DIR", str(durable_bundles))
        rootfs = Path(td) / "rootfs"
        rootfs.mkdir()
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        fake_runsc = Path(td) / "fake-runsc"
        fake_runsc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_runsc.chmod(0o755)
        runner = RunscSandboxRunner(
            runsc_binary=str(fake_runsc),
            runsc_helper=str(fake_runsc),
            rootfs=str(rootfs),
            network="none",
            rootless=False,
        )

        async def fail_finalize(container_id: str) -> None:
            raise RuntimeExecutionError("cleanup verification failed")

        monkeypatch.setattr(runner, "_runsc_finalize", fail_finalize)
        with pytest.raises(RuntimeExecutionError):
            await runner.run(
                _make_job(input_dir, output_dir),
                input_dir,
                output_dir,
                output_dir / "result.json",
                _FakeCancellationToken(),
            )

        assert not durable_root.exists()


class _DeadlineRunner:
    """Fake runner that raises a deadline-exceeded error."""

    async def run(
        self,
        job: RuntimeJob,
        input_dir: Path,
        output_dir: Path,
        result_path: Path,
        cancellation: Any,
        proxy_uds_path: Path | None = None,
    ) -> None:
        raise RuntimeExecutionError("sandbox exceeded execution deadline")


class _MissingResultRunner:
    """Fake runner that exits without writing result.json."""

    async def run(
        self,
        job: RuntimeJob,
        input_dir: Path,
        output_dir: Path,
        result_path: Path,
        cancellation: Any,
        proxy_uds_path: Path | None = None,
    ) -> None:
        output_dir.mkdir(parents=True, exist_ok=True)
        return


@pytest.mark.asyncio
async def test_runsc_runtime_deadline_returns_timeout_and_consumes_session() -> None:
    """`RunscSkillRuntime` preserves deadline expiry as `timeout` and cleans up the proxy session."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(proxy_config=cfg)
    runtime = RunscSkillRuntime(
        runner=_DeadlineRunner(), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is not None
        assert result.failure.category == "timeout"
        assert not proxy._sessions


@pytest.mark.asyncio
async def test_runsc_runtime_missing_result_consumes_session() -> None:
    """A runner that exits without result.json still finalizes the proxy session."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(proxy_config=cfg)
    runtime = RunscSkillRuntime(
        runner=_MissingResultRunner(), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is not None
        assert result.failure.category == "runtime_error"
        assert not proxy._sessions


@pytest.mark.asyncio
async def test_runsc_runtime_proxy_rejection_consumes_session() -> None:
    """A sandbox-reported failure path removes the proxy session after consumption."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(502))),
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is not None
        assert result.failure.category == "model_error"
        assert not proxy._sessions


@pytest.mark.asyncio
async def test_runsc_runtime_success_consumes_session() -> None:
    """A successful run consumes the proxy session and leaves no stale state."""
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_response_factory())
        ),
    )
    runtime = RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=False
    )
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is None
        assert not proxy._sessions


def _se_runsc_bundle_dirs() -> set[str]:
    """Return any runsc bundle directories in the system temp directory."""
    import tempfile as _tmp

    return {str(p) for p in Path(_tmp.gettempdir()).glob("se-runsc-bundle-*") if p.is_dir()}


def _se_proxy_dirs() -> set[str]:
    """Return any se-proxy directories in the system temp directory."""
    import tempfile as _tmp

    return {str(p) for p in Path(_tmp.gettempdir()).glob("se-proxy-*") if p.is_dir()}


@pytest.mark.skipif(
    shutil.which("runsc") is None or shutil.which("sudo") is None,
    reason="runsc or sudo is not installed",
)
@pytest.mark.asyncio
async def test_real_runsc_integration() -> None:
    """Gated integration test for a real gVisor `runsc` sandbox.

    Skipped automatically when `runsc` is unavailable or the host cannot run it.
    """
    rootfs = os.environ.get("RUNSC_ROOTFS")
    if not rootfs or not Path(rootfs).exists():
        pytest.skip("RUNSC_ROOTFS is not set or does not exist")
    helper = os.environ.get(
        "RUNSC_HELPER_BINARY", "/usr/local/sbin/se-skills-runsc"
    )
    if not Path(helper).is_file():
        pytest.skip("constrained runsc helper is not installed")

    cfg = ProxyConfig(
        secret="a" * 32,
        anthropic_api_key="test-key",
        anthropic_api_url="https://api.anthropic.com",
    )
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_response_factory())
        ),
    )
    runner = RunscSandboxRunner(
        runsc_binary=shutil.which("runsc") or "runsc",
        runsc_helper=helper,
        rootfs=rootfs,
        network="none",
    )
    runtime = RunscSkillRuntime(runner=runner, proxy=proxy, start_proxy_server=True)
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        result = await runtime.execute(job, _FakeCancellationToken())
        assert result.failure is None
        assert (output_dir / "output.md").exists()
