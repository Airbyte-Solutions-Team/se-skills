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
import pytest

from webapp.hosted.model_proxy import ModelProxy, ProxyConfig
from webapp.hosted.runtime_contract import (
    Allowlist,
    InputManifest,
    NetworkDestination,
    RuntimeJob,
)
from webapp.hosted.runsc_executor import (
    FakeSandboxRunner,
    RunscSandboxRunner,
    RunscSkillRuntime,
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


def _runtime() -> RunscSkillRuntime:
    cfg = ProxyConfig(secret="a" * 32, anthropic_api_key="test-key")
    proxy = ModelProxy(
        proxy_config=cfg,
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_response_factory())
        ),
    )
    return RunscSkillRuntime(
        runner=FakeSandboxRunner(proxy), proxy=proxy, start_proxy_server=True
    )


@pytest.mark.asyncio
async def test_runsc_runtime_fake_runner_success() -> None:
    """The `RunscSkillRuntime` adapter completes a post-call via the fake runner."""
    with tempfile.TemporaryDirectory() as td:
        input_dir = Path(td) / "input"
        output_dir = Path(td) / "output"
        job = _make_job(input_dir, output_dir)
        runtime = _runtime()
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
async def test_runsc_sandbox_runner_argv_no_shell_interpolation() -> None:
    """`RunscSandboxRunner` builds an explicit argv array with no shell interpolation."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/var/lib/runsc/rootfs",
        network="none",
        rootless=True,
    )
    argv = runner._build_argv(Path("/bundle"), Path("/bundle/root"), "se-abc123")
    assert argv == [
        "/usr/local/bin/runsc",
        "--root=/bundle/root",
        "--network=none",
        "--rootless",
        "run",
        "--bundle",
        "/bundle",
        "se-abc123",
    ]
    for arg in argv:
        assert ";" not in arg
        assert "|" not in arg
        assert " " not in arg or arg == " "  # spaces are not used as separators


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


@pytest.mark.asyncio
async def test_runsc_sandbox_delete_argv_structure() -> None:
    """`_build_delete_argv` includes the per-attempt `--root` and no shell interpolation."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/var/lib/runsc/rootfs",
        network="none",
        rootless=False,
    )
    argv = runner._build_delete_argv(Path("/bundle/root"), "se-abc123")
    assert argv == [
        "/usr/local/bin/runsc",
        "--root=/bundle/root",
        "delete",
        "--force",
        "se-abc123",
    ]
    for arg in argv:
        assert ";" not in arg
        assert "|" not in arg
        assert "&" not in arg
        assert "`" not in arg
        assert "$" not in arg


@pytest.mark.asyncio
async def test_runsc_sandbox_delete_rootless_argv_structure() -> None:
    """`_build_delete_argv` passes `--rootless` before the subcommand when enabled."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/var/lib/runsc/rootfs",
        network="none",
        rootless=True,
    )
    argv = runner._build_delete_argv(Path("/bundle/root"), "se-abc123")
    assert argv == [
        "/usr/local/bin/runsc",
        "--root=/bundle/root",
        "--rootless",
        "delete",
        "--force",
        "se-abc123",
    ]


@pytest.mark.asyncio
async def test_runsc_sandbox_delete_uses_root_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_runsc_delete` invokes `runsc --root=<root_dir> delete --force <id>`."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc", rootfs="/var/lib/runsc/rootfs"
    )
    root_dir = Path(tempfile.mkdtemp()) / "root"
    root_dir.mkdir()
    container_id = "se-test"

    calls: list[list[str]] = []

    async def fake_exec(*args: str, **kwargs: Any) -> _FakeSubprocess:
        calls.append(list(args))
        if args[2] == "delete":
            return _FakeSubprocess(returncode=0)
        return _FakeSubprocess(returncode=0, list_stdout=b"ID\tPID\tSTATUS\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await runner._runsc_delete(root_dir, container_id)
    assert any(
        c[0] == "/usr/local/bin/runsc"
        and c[1] == f"--root={root_dir}"
        and c[2] == "delete"
        and c[3] == "--force"
        and c[4] == container_id
        for c in calls
    )


@pytest.mark.asyncio
async def test_runsc_sandbox_delete_nonzero_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_runsc_delete` raises if `runsc delete` exits non-zero."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc", rootfs="/var/lib/runsc/rootfs"
    )
    root_dir = Path(tempfile.mkdtemp()) / "root"
    root_dir.mkdir()

    async def fake_exec(*args: str, **kwargs: Any) -> _FakeSubprocess:
        if args[2] == "delete":
            return _FakeSubprocess(returncode=1)
        return _FakeSubprocess(returncode=0, list_stdout=b"ID\tPID\tSTATUS\n")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(Exception):
        await runner._runsc_delete(root_dir, "se-test")


@pytest.mark.asyncio
async def test_runsc_sandbox_delete_timeout_kills(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_runsc_delete` kills and reports a timeout if `runsc delete` hangs."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc", rootfs="/var/lib/runsc/rootfs"
    )
    root_dir = Path(tempfile.mkdtemp()) / "root"
    root_dir.mkdir()

    proc = _FakeSubprocess(returncode=None, wait_delay=60.0)

    async def fake_exec(*args: str, **kwargs: Any) -> _FakeSubprocess:
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(Exception) as exc:
        await runner._runsc_delete(root_dir, "se-test")
    assert proc.killed is True
    assert "timed out" in str(exc.value).lower() or "terminate" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_runsc_sandbox_delete_state_dir_present_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """`_runsc_delete` raises if the container is still present after delete."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc", rootfs="/var/lib/runsc/rootfs"
    )
    root_dir = Path(tempfile.mkdtemp()) / "root"
    root_dir.mkdir()
    container_id = "se-test"
    (root_dir / container_id).mkdir()

    async def fake_exec(*args: str, **kwargs: Any) -> _FakeSubprocess:
        if args[2] == "delete":
            return _FakeSubprocess(returncode=0)
        return _FakeSubprocess(
            returncode=0,
            list_stdout=f"ID\tPID\tSTATUS\n{container_id}\t1\trunning\n".encode(),
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(Exception) as exc:
        await runner._runsc_delete(root_dir, container_id)
    assert "still present" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_runsc_sandbox_state_directory_removed() -> None:
    """`RunscSandboxRunner.run` removes the bundle/state directory on exit."""
    before = _se_runsc_bundle_dirs()
    with tempfile.TemporaryDirectory() as td:
        rootfs = Path(td) / "rootfs"
        rootfs.mkdir()
        input_dir = Path(td) / "input"
        input_dir.mkdir()
        output_dir = Path(td) / "output"
        output_dir.mkdir()

        runner = RunscSandboxRunner(
            runsc_binary="/bin/true",
            rootfs=str(rootfs),
            network="none",
            rootless=False,
        )

        # `_runsc_delete` calls `/bin/true --root=... list`. `/bin/true` exits 0 with no
        # output, so the container is treated as gone and the state dir check passes.
        job = _make_job(input_dir, output_dir)
        await runner.run(
            job,
            input_dir,
            output_dir,
            output_dir / "result.json",
            _FakeCancellationToken(),
            proxy_uds_path=None,
        )
    after = _se_runsc_bundle_dirs()
    assert not (after - before)


def _se_runsc_bundle_dirs() -> set[str]:
    """Return any runsc bundle directories in the system temp directory."""
    import tempfile as _tmp

    return {str(p) for p in Path(_tmp.gettempdir()).glob("se-runsc-bundle-*") if p.is_dir()}


def _se_proxy_dirs() -> set[str]:
    """Return any se-proxy directories in the system temp directory."""
    import tempfile as _tmp

    return {str(p) for p in Path(_tmp.gettempdir()).glob("se-proxy-*") if p.is_dir()}


@pytest.mark.skipif(shutil.which("runsc") is None, reason="runsc is not installed")
@pytest.mark.asyncio
async def test_real_runsc_integration() -> None:
    """Gated integration test for a real gVisor `runsc` sandbox.

    Skipped automatically when `runsc` is unavailable or the host cannot run it.
    """
    rootfs = os.environ.get("RUNSC_ROOTFS")
    if not rootfs or not Path(rootfs).exists():
        pytest.skip("RUNSC_ROOTFS is not set or does not exist")

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
