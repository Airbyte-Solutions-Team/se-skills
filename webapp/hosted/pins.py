"""Typed access to the repository's deployment pins."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel, ConfigDict


class RunscPins(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    download_base_url: str
    sha512: Mapping[str, str]
    min_version: str


class HostPins(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    os: str
    os_version: str
    arch: str
    min_kernel: str
    cgroup_version: int
    firewall_policy_path: str = "/etc/se-skills/firewall.nft"


class SandboxImagePins(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_digests: Mapping[str, str]
    approved_digest: str | None = None
    rootfs_digest: str | None = None


class BuildToolPin(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str
    filename: str
    sha256: str


class LimitPins(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    worker_cpu_quota_percent: int
    worker_memory_max_bytes: int
    worker_pid_max: int
    worker_nofile_max: int
    worker_fsize_max_bytes: int
    min_root_free_bytes: int
    min_temp_free_bytes: int


class HostedPins(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    runsc: RunscPins
    host: HostPins
    sandbox_image: SandboxImagePins
    limits: LimitPins
    build_tools: Mapping[str, BuildToolPin] = {}

    def runsc_checksum(self, architecture: str) -> str | None:
        return self.runsc.sha512.get(architecture)


def load_pins(path: Path | None = None) -> HostedPins:
    """Load and validate deploy/pins.json without network access."""
    pins_path = path or Path(__file__).resolve().parents[2] / "deploy" / "pins.json"
    return HostedPins.model_validate(
        json.loads(pins_path.read_text(encoding="utf-8"))
    )
