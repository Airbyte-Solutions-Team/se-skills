"""Deterministic hosted-worker smoke checks with a gated live mode."""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field

from hosted.model_proxy import ModelProxy, ProxyConfig
from hosted.pins import HostedPins, load_pins
from hosted.post_call_orchestrator import (
    PostCallOrchestrator,
)
from hosted.preflight import (
    ListeningSocket,
    PathFacts,
    PreflightConfig,
    run_preflight,
)
from hosted.runsc_executor import (
    FakeSandboxRunner,
    RunscSandboxRunner,
    RunscSkillRuntime,
)
from hosted.runtime_contract import (
    InputManifest,
    RuntimeJob,
    RuntimeResult,
    SandboxOutputSidecar,
)
from hosted.supply_chain import (
    SupplyChainArtifacts,
    SupplyChainCommandResult,
    verify_supply_chain,
)
from hosted.smoke_support import SmokePool, SmokeStorage


class SmokeCheck(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    status: str
    severity: str
    detail: str


class SmokeReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[SmokeCheck, ...] = Field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(
            item.status == "fail" and item.severity == "required"
            for item in self.checks
        )


class SmokeEnvironment(Protocol):
    def getenv(self, name: str) -> str:
        ...


@dataclass
class OfflineFaults:
    """Fault switches used to prove that the smoke assertions are live."""

    drop_network_flag: bool = False
    add_mount: bool = False
    leak_sentinel: bool = False


@dataclass
class _OfflineProbe:
    pins: HostedPins

    def os_release(self) -> tuple[str, str]:
        return self.pins.host.os, self.pins.host.os_version

    def kernel_version(self) -> str:
        return self.pins.host.min_kernel

    def architecture(self) -> str:
        return self.pins.host.arch

    def path_info(self, path: str) -> PathFacts:
        if path == "/usr/local/bin/runsc":
            return PathFacts(
                True,
                "root",
                "root",
                0o755,
                True,
                False,
                self.pins.runsc_checksum(self.pins.host.arch),
            )
        if path == "/opt/se-skills/rootfs":
            return PathFacts(True, "root", "root", 0o755, False, True)
        if path == "/var/lib/se-skills":
            return PathFacts(True, "se-worker", "se-worker", 0o750, False, True)
        if path == "/var/lib/se-skills/runsc":
            return PathFacts(True, "se-worker", "se-worker", 0o700, False, True)
        if path == "/var/lib/se-skills/bundles":
            return PathFacts(True, "se-worker", "se-worker", 0o700, False, True)
        return PathFacts(False, None, None, None, False, False)

    def command(self, argv: Sequence[str]) -> SupplyChainCommandResult:
        if argv[:2] == ("nft", "list"):
            return SupplyChainCommandResult(
                returncode=0,
                stdout="""table inet se_skills {
  chain output {
    type filter hook output priority 0; policy drop;
    ip daddr 169.254.169.254 drop
    ip6 daddr fd00:ec2::254 drop
    ip daddr 10.0.0.0/8 drop
    ip daddr 172.16.0.0/12 drop
    ip daddr 192.168.0.0/16 drop
    ip6 daddr fc00::/7 drop
  }
}""",
            )
        return SupplyChainCommandResult(
            returncode=0, stdout=f"runsc {self.pins.runsc.version}"
        )

    def cgroup_version(self) -> int | None:
        return self.pins.host.cgroup_version

    def namespace_features(self) -> frozenset[str]:
        return frozenset({"pid", "net", "user"})

    def listening_sockets(self) -> tuple[ListeningSocket, ...]:
        return (ListeningSocket("127.0.0.1", 0),)

    def clock_offset_seconds(self) -> float | None:
        return 0.0

    def disk_free_bytes(self, path: str) -> int | None:
        if path in ("/", "/tmp"):
            return max(
                self.pins.limits.min_root_free_bytes,
                self.pins.limits.min_temp_free_bytes,
            )
        return None

    def file_text(self, path: str) -> str | None:
        return "policy drop\n169.254.169.254\nfd00:ec2::254"


class _OfflineCommandRunner:
    def run(self, argv: Sequence[str]) -> SupplyChainCommandResult:
        return SupplyChainCommandResult(returncode=0)


@dataclass
class _Observation:
    argv: tuple[str, ...] = ()
    authorized_mounts: frozenset[str] = frozenset()
    observed_mounts: frozenset[str] = frozenset()
    workspace_path: Path | None = None
    proxy_path: Path | None = None
    runtime_output: str = ""


class _OfflineSandboxRunner(FakeSandboxRunner):
    def __init__(
        self,
        proxy: ModelProxy,
        observation: _Observation,
        faults: OfflineFaults,
    ) -> None:
        super().__init__(proxy)
        self.observation = observation
        self.faults = faults

    async def run(
        self,
        job: RuntimeJob,
        input_dir: Path,
        output_dir: Path,
        result_path: Path,
        cancellation: object,
        proxy_uds_path: Path | None = None,
    ) -> None:
        runner = RunscSandboxRunner(
            runsc_binary="/usr/local/bin/runsc",
            rootfs="/opt/se-skills/rootfs",
            network="" if self.faults.drop_network_flag else "none",
        )
        bundle_dir = Path("/bundle")
        root_dir = bundle_dir / "root"
        job_path = bundle_dir / "job.json"
        config = runner._build_config(
            bundle_dir=bundle_dir,
            root_dir=root_dir,
            job_path=job_path,
            input_dir=input_dir,
            output_dir=output_dir,
            proxy_uds_path=proxy_uds_path,
            container_id="se-smoke",
        )
        mounts = {mount["destination"] for mount in config["mounts"]}
        if self.faults.add_mount:
            mounts.add("/runtime/unauthorized")
        self.observation.observed_mounts = frozenset(mounts)
        self.observation.argv = tuple(
            runner._build_argv(bundle_dir, root_dir, "se-smoke")
        )
        self.observation.workspace_path = output_dir
        self.observation.proxy_path = proxy_uds_path
        await super().run(
            job,
            input_dir,
            output_dir,
            result_path,
            cancellation,
            proxy_uds_path,
        )
        output = Path("eval/fixtures/outputs/post-call-full.md").read_text(
            encoding="utf-8"
        )
        output = output.replace(
            "Read Acme-06.11.26.txt in full (612 / 612 lines).",
            "Read transcript.txt in full (100 / 100 lines).",
        )
        self.observation.runtime_output = output
        sidecar = SandboxOutputSidecar(
            skill="post-call",
            skill_version="1.0",
            mode="full",
            title="Smoke output",
            date="2026-08-10",
            source_coverage="Read transcript.txt in full (100 / 100 lines).",
        )
        result = RuntimeResult(output_artifact=output, sidecar=sidecar)
        (output_dir / "output.md").write_text(output, encoding="utf-8")
        (output_dir / "sidecar.json").write_text(
            sidecar.model_dump_json(), encoding="utf-8"
        )
        result_path.write_text(result.model_dump_json(), encoding="utf-8")
        if self.faults.leak_sentinel:
            self.observation.runtime_output += _SENTINEL


_SENTINEL = "SMOKE_SECRET_SENTINEL customer-content-sentinel"


def _upstream_handler(request: httpx.Request) -> httpx.Response:
    if _SENTINEL in request.content.decode("utf-8", errors="replace"):
        raise AssertionError("secret reached upstream")
    return httpx.Response(
        200,
        json={
            "id": "smoke-message",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [
                {"type": "tool_use", "id": "smoke-tool", "name": "finish", "input": {}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    )


def run_offline_smoke(faults: OfflineFaults | None = None) -> SmokeReport:
    """Exercise the worker runtime and orchestrator against deterministic fakes."""
    faults = faults or OfflineFaults()
    pins = load_pins()
    digest = "sha256:" + "a" * 64
    rootfs_digest = "sha256:" + "b" * 64
    supply_chain = verify_supply_chain(
        SupplyChainArtifacts(
            image_digest=digest,
            approved_image_digest=digest,
            sbom_text="{}",
            sbom_image_digest=digest,
            provenance_image_digest=digest,
            rootfs_digest=rootfs_digest,
            manifest_rootfs_digest=rootfs_digest,
            signature_command=("cosign", "verify"),
        ),
        _OfflineCommandRunner(),
    )
    names = frozenset(
        {
            "DATABASE_WORKER_URL",
            "ANTHROPIC_API_KEY",
            "MODEL_PROXY_SECRET",
            "RUNSC_ROOTFS",
            "SANDBOX_IMAGE_DIGEST",
            "RUNSC_ROOTFS_DIGEST",
        }
    )
    preflight = run_preflight(
        PreflightConfig(
            runsc_path="/usr/local/bin/runsc",
            present_config_names=names,
            model_proxy_secret="OfflineSmokeSecretWithSufficientDiversity0123",
            database_url="postgresql://worker@db/app?sslmode=require",
            storage_url="https://storage.example",
            rootfs_path="/opt/se-skills/rootfs",
            supply_chain=supply_chain.model_dump(),
            hosted_env="production",
            runtime="post-call-runsc",
        ),
        _OfflineProbe(pins),
    )
    observation = _Observation()
    proxy = ModelProxy(
        proxy_config=ProxyConfig(
            secret="OfflineSmokeProxySecretWithSufficientDiversity0123",
            anthropic_api_key="offline-only",
        ),
        anthropic_client=httpx.AsyncClient(
            transport=httpx.MockTransport(_upstream_handler)
        ),
    )
    runtime = RunscSkillRuntime(
        _OfflineSandboxRunner(proxy, observation, faults),
        proxy,
        start_proxy_server=True,
    )
    bundle_dir = Path("/bundle")
    baseline = RunscSandboxRunner(
        runsc_binary="/usr/local/bin/runsc",
        rootfs="/opt/se-skills/rootfs",
        network="none",
    )
    baseline_config = baseline._build_config(
        bundle_dir=bundle_dir,
        root_dir=bundle_dir / "root",
        job_path=bundle_dir / "job.json",
        input_dir=Path("/tmp/smoke-input"),
        output_dir=Path("/tmp/smoke-output"),
        proxy_uds_path=Path("/tmp/smoke-proxy.sock"),
        container_id="se-smoke",
    )
    observation.authorized_mounts = frozenset(
        mount["destination"] for mount in baseline_config["mounts"]
    )
    transcript_path = "offline/transcript.txt"
    identifiers = {
        "org_id": str(uuid.uuid4()),
        "account_id": str(uuid.uuid4()),
        "transcript_id": str(uuid.uuid4()),
    }
    resolved = {
        **identifiers,
        "opportunity_id": None,
        "storage_path": transcript_path,
        "original_filename": "transcript.txt",
        "prior_outputs": [],
    }
    orchestrator = PostCallOrchestrator(
        runtime,
        db_pool=SmokePool(resolved),
        storage_backend=SmokeStorage(
            transcript_path,
            "Offline smoke transcript. No customer content.",
        ),
    )
    job = {
        "job_id": str(uuid.uuid4()),
        "lease_token": str(uuid.uuid4()),
        "attempt_number": 1,
        "timeout_seconds": 60,
        "org_id": identifiers["org_id"],
        "account_id": identifiers["account_id"],
        "transcript_id": identifiers["transcript_id"],
        "requester_id": str(uuid.uuid4()),
        "source_manifest": {
            **identifiers,
            "opportunity_id": None,
            "storage_path": transcript_path,
        },
        "payload": {"model": "claude-sonnet-4-6", "runtime_version": "smoke"},
        "skill_version": "1.0",
    }
    result = asyncio.run(orchestrator.execute(job))
    report = SmokeReport(
        checks=(
            _check("preflight", preflight.ok, "fixture probe contract result"),
            _check(
                "runtime",
                "--network=none" in observation.argv
                and not any(
                    item.startswith("--network=") and item != "--network=none"
                    for item in observation.argv
                ),
                "observed runsc argv contains only --network=none",
            ),
            _check(
                "mounts",
                observation.observed_mounts == observation.authorized_mounts,
                "observed production config mount destinations match its contract",
            ),
            _check(
                "artifact_validation",
                result.validation_status == "valid",
                "plain orchestrator completed worker-side artifact validation",
            ),
            _check(
                "shutdown",
                observation.workspace_path is not None
                and not observation.workspace_path.exists()
                and observation.proxy_path is not None
                and not observation.proxy_path.exists(),
                "actual per-attempt workspace and proxy socket were removed",
            ),
            _check(
                "redaction",
                _SENTINEL not in observation.runtime_output,
                "captured runtime facts contain no sensitive sentinel",
            ),
        )
    )
    asyncio.run(proxy.anthropic_client.aclose())
    return report


def run_live_smoke(environment: SmokeEnvironment | None = None) -> SmokeReport:
    """Refuse live mode unless explicitly enabled outside CI."""
    env = environment or _OsEnvironment()
    if env.getenv("ALLOW_LIVE_HOSTED_SMOKE") != "1":
        return SmokeReport(
            checks=(
                _check("live_gate", False, "live smoke requires explicit operator approval"),
            )
        )
    if env.getenv("CI") or env.getenv("GITHUB_ACTIONS"):
        return SmokeReport(
            checks=(
                _check("live_ci_gate", False, "live smoke is disabled in CI"),
            )
        )
    return SmokeReport(
        checks=(
            _check(
                "live_scope",
                False,
                "live smoke is reserved for an approved deployment handoff",
            ),
        )
    )


def _check(check_id: str, ok: bool, detail: str) -> SmokeCheck:
    return SmokeCheck(
        check_id=check_id,
        status="ok" if ok else "fail",
        severity="required",
        detail=detail,
    )


class _OsEnvironment:
    def getenv(self, name: str) -> str:
        return os.environ.get(name, "")
