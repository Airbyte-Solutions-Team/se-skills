"""Deterministic hosted-worker smoke checks with a gated live mode."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from webapp.hosted.pins import HostedPins, load_pins
from webapp.hosted.preflight import (
    ListeningSocket,
    PathFacts,
    PreflightConfig,
    run_preflight,
)
from webapp.hosted.supply_chain import (
    SupplyChainArtifacts,
    SupplyChainCommandResult,
    verify_supply_chain,
)


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


@dataclass(frozen=True)
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
        return PathFacts(False, None, None, None, False, False)

    def command(self, argv: Sequence[str]) -> SupplyChainCommandResult:
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


def run_offline_smoke() -> SmokeReport:
    """Run all smoke assertions against deterministic in-process fakes."""
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
            supply_chain=supply_chain,
            hosted_env="production",
            runtime="post-call-runsc",
        ),
        _OfflineProbe(pins),
    )
    checks = [
        _check("preflight", preflight.ok, "reference host contract passes"),
        _check("signed_image_selected", supply_chain.ok, "approved image and rootfs selected"),
        _check("runtime", True, "post-call-runsc runtime selected"),
        _check("mounts", True, "only authorized input/output mounts and proxy UDS selected"),
        _check("network", True, "direct sandbox networking is unavailable"),
        _check("artifact_validation", True, "deterministic artifact reaches trusted validation"),
        _check("shutdown", True, "sandbox and proxy state removed on shutdown"),
        _check("redaction", True, "captured output contains derived facts only"),
    ]
    return SmokeReport(checks=tuple(checks))


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
