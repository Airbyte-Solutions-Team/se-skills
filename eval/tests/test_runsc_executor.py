"""Deterministic tests for the gVisor `runsc` runtime adapter and sandbox runner.

Tests that do not need `runsc` exercise the fake runner and `RunscSandboxRunner`
configuration generation. A single gated integration test is skipped when `runsc`
is unavailable.
"""
from __future__ import annotations

import json
import os
import shutil
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
        lease_token="lease-1",
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
    proxy = ModelProxy(proxy_config=cfg)
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
async def test_runsc_sandbox_runner_argv_no_shell_interpolation() -> None:
    """`RunscSandboxRunner` builds an explicit argv array with no shell interpolation."""
    runner = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/var/lib/runsc/rootfs",
        network="none",
        rootless=True,
    )
    argv = runner._build_argv(Path("/bundle"), "se-abc123")
    assert argv == [
        "/usr/local/bin/runsc",
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
        job_path = bundle_dir / "job.json"
        job_path.write_text("{}", encoding="utf-8")

        runner = RunscSandboxRunner(runsc_binary="/fake/runsc", rootfs=str(rootfs))
        config = runner._build_config(
            bundle_dir=bundle_dir,
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
    proxy = ModelProxy(proxy_config=cfg)
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
