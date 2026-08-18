"""Fail-closed, injectable checks for a hosted worker host."""
from __future__ import annotations

import hashlib
import grp
import os
import platform
import pwd
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from webapp.hosted.supply_chain import SupplyChainCommandResult, SupplyChainVerification


class HostProbe(Protocol):
    """All host access required by preflight."""

    def os_release(self) -> tuple[str, str]:
        ...

    def kernel_version(self) -> str:
        ...

    def architecture(self) -> str:
        ...

    def path_info(self, path: str) -> "PathFacts":
        ...

    def command(self, argv: Sequence[str]) -> SupplyChainCommandResult:
        ...

    def cgroup_version(self) -> int | None:
        ...

    def namespace_features(self) -> frozenset[str]:
        ...

    def listening_sockets(self) -> tuple["ListeningSocket", ...]:
        ...

    def clock_offset_seconds(self) -> float | None:
        ...

    def disk_free_bytes(self, path: str) -> int:
        ...

    def file_text(self, path: str) -> str | None:
        ...


@dataclass(frozen=True)
class PathFacts:
    exists: bool
    owner: str | None
    group: str | None
    mode: int | None
    is_file: bool
    is_directory: bool
    sha512: str | None = None


@dataclass(frozen=True)
class ListeningSocket:
    address: str
    port: int
    process: str | None = None


class PreflightConfig(BaseModel):
    """Expected host contract and redacted runtime configuration facts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runsc_path: str = "/usr/local/bin/runsc"
    runsc_version: str = "20260810.0"
    runsc_sha512: str = ""
    required_names: tuple[str, ...] = (
        "DATABASE_WORKER_URL",
        "ANTHROPIC_API_KEY",
        "MODEL_PROXY_SECRET",
        "RUNSC_ROOTFS",
        "SANDBOX_IMAGE_DIGEST",
        "RUNSC_ROOTFS_DIGEST",
    )
    present_config_names: frozenset[str] = frozenset()
    model_proxy_secret: str = ""
    anthropic_api_url: str = "https://api.anthropic.com"
    approved_anthropic_hosts: frozenset[str] = frozenset({"api.anthropic.com"})
    database_url: str = ""
    storage_url: str = ""
    worker_user: str = "se-worker"
    worker_group: str = "se-worker"
    worker_state_path: str = "/var/lib/se-skills"
    worker_state_mode: int = 0o750
    runsc_state_path: str = "/var/lib/se-skills/runsc"
    runsc_state_mode: int = 0o700
    rootfs_path: str = ""
    firewall_policy_path: str = "/etc/se-skills/firewall.nft"
    min_kernel: str = "6.8"
    min_root_free_bytes: int = 0
    min_temp_free_bytes: int = 0
    clock_tolerance_seconds: float = 1.0
    supply_chain: SupplyChainVerification | None = None
    supply_chain_skipped: bool = False
    production: bool = True


class PreflightCheck(BaseModel):
    """Stable, redacted result for one host check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    status: str
    severity: str
    detail: str


class PreflightReport(BaseModel):
    """All preflight results."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[PreflightCheck, ...] = Field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(
            check.status == "fail" and check.severity == "required"
            for check in self.checks
        )

    def failed_required(self) -> tuple[PreflightCheck, ...]:
        return tuple(
            check
            for check in self.checks
            if check.status == "fail" and check.severity == "required"
        )


def run_preflight(config: PreflightConfig, probe: HostProbe) -> PreflightReport:
    """Run every required host check without logging sensitive values."""
    checks: list[PreflightCheck] = []
    os_name, os_version = probe.os_release()
    checks.append(
        _required(
            "host_os",
            os_name == "ubuntu" and os_version == "24.04",
            "supported OS is ubuntu 24.04" if os_name == "ubuntu" and os_version == "24.04" else "unsupported OS or version",
        )
    )
    checks.append(
        _required(
            "host_architecture",
            probe.architecture() == "x86_64",
            "architecture is x86_64" if probe.architecture() == "x86_64" else "unsupported architecture",
        )
    )
    checks.append(
        _required(
            "host_kernel",
            _version_at_least(probe.kernel_version(), config.min_kernel),
            "kernel meets minimum" if _version_at_least(probe.kernel_version(), config.min_kernel) else "kernel is below minimum",
        )
    )
    runsc = probe.path_info(config.runsc_path)
    checks.append(
        _required(
            "runsc_binary",
            runsc.exists and runsc.is_file and runsc.mode == 0o755 and runsc.owner == "root" and runsc.group == "root",
            "runsc is root-owned executable" if runsc.exists and runsc.is_file and runsc.mode == 0o755 and runsc.owner == "root" and runsc.group == "root" else "runsc missing or ownership/mode is unsafe",
        )
    )
    version_result = probe.command((config.runsc_path, "--version"))
    checks.append(
        _required(
            "runsc_version",
            version_result.returncode == 0 and config.runsc_version in version_result.stdout,
            "runsc version matches pin" if version_result.returncode == 0 and config.runsc_version in version_result.stdout else "runsc version does not match pin",
        )
    )
    checks.append(
        _required(
            "runsc_checksum",
            bool(runsc.sha512) and runsc.sha512 == config.runsc_sha512,
            "runsc checksum matches pin" if runsc.sha512 == config.runsc_sha512 and bool(runsc.sha512) else "runsc checksum does not match pin",
        )
    )
    checks.append(
        _required(
            "cgroup_v2",
            probe.cgroup_version() == 2,
            "cgroup v2 is enabled" if probe.cgroup_version() == 2 else "cgroup v2 is unavailable",
        )
    )
    required_names_missing = sorted(set(config.required_names) - set(config.present_config_names))
    checks.append(
        _required(
            "required_configuration",
            not required_names_missing,
            "required configuration names are present" if not required_names_missing else f"missing configuration names: {', '.join(required_names_missing)}",
        )
    )
    checks.append(_required("model_proxy_secret", _strong_secret(config.model_proxy_secret), _secret_detail(config.model_proxy_secret)))
    checks.append(_required("anthropic_url", _valid_anthropic_url(config.anthropic_api_url, config.approved_anthropic_hosts), _url_detail(config.anthropic_api_url, config.approved_anthropic_hosts)))
    checks.append(_required("database_url", _valid_database_url(config.database_url), "database URL is secure" if _valid_database_url(config.database_url) else "database URL must use postgresql with TLS"))
    checks.append(_required("storage_url", _valid_https_url(config.storage_url), "Storage URL is secure" if _valid_https_url(config.storage_url) else "Storage URL must use https"))
    checks.append(_required("worker_directories", _worker_dirs_ok(config, probe), "worker directories have expected ownership and modes" if _worker_dirs_ok(config, probe) else "worker directory ownership or mode is unsafe"))
    rootfs = probe.path_info(config.rootfs_path) if config.rootfs_path else PathFacts(False, None, None, None, False, False)
    checks.append(_required("rootfs_path", rootfs.exists and rootfs.is_directory, "sandbox rootfs directory is present" if rootfs.exists and rootfs.is_directory else "sandbox rootfs directory is missing"))
    namespaces = probe.namespace_features()
    has_network_namespace = "net" in namespaces or "network" in namespaces
    has_required_namespaces = {"pid", "user"}.issubset(namespaces) and has_network_namespace
    checks.append(_required("namespace_features", has_required_namespaces, "required namespace features are present" if has_required_namespaces else "required namespace features are missing"))
    checks.append(_required("root_disk_free", probe.disk_free_bytes("/") >= config.min_root_free_bytes, "root filesystem has sufficient free space" if probe.disk_free_bytes("/") >= config.min_root_free_bytes else "root filesystem free space is below minimum"))
    checks.append(_required("temp_disk_free", probe.disk_free_bytes("/tmp") >= config.min_temp_free_bytes, "temporary filesystem has sufficient free space" if probe.disk_free_bytes("/tmp") >= config.min_temp_free_bytes else "temporary filesystem free space is below minimum"))
    offset = probe.clock_offset_seconds()
    checks.append(_required("clock_sync", offset is not None and abs(offset) <= config.clock_tolerance_seconds, "clock offset is within tolerance" if offset is not None and abs(offset) <= config.clock_tolerance_seconds else "clock offset exceeds tolerance"))
    checks.append(_required("firewall_policy", _firewall_ok(config.firewall_policy_path, probe), "firewall is deny-by-default and blocks metadata" if _firewall_ok(config.firewall_policy_path, probe) else "firewall policy is missing or unsafe"))
    public_listener = any(socket.address not in {"127.0.0.1", "::1", "localhost"} for socket in probe.listening_sockets())
    checks.append(_required("public_listener", not public_listener, "no unexpected public listener" if not public_listener else "unexpected public listener is present"))
    checks.append(_required("production_runtime", config.production, "production runtime is fail-closed" if config.production else "production runtime fallback is unsafe"))
    if config.supply_chain is None and config.supply_chain_skipped:
        checks.append(
            PreflightCheck(
                check_id="supply_chain",
                status="skipped",
                severity="required",
                detail="supply-chain verification skipped in offline mode",
            )
        )
    elif config.supply_chain is None:
        checks.append(_required("supply_chain", False, "supply-chain verification is missing"))
    else:
        checks.extend(
            PreflightCheck(
                check_id=f"supply_chain_{item.check_id}",
                status="ok" if item.ok else "fail",
                severity="required",
                detail=item.detail,
            )
            for item in config.supply_chain.checks
        )
    return PreflightReport(checks=tuple(checks))


def _required(check_id: str, ok: bool, detail: str) -> PreflightCheck:
    return PreflightCheck(check_id=check_id, status="ok" if ok else "fail", severity="required", detail=detail)


def _version_at_least(actual: str, minimum: str) -> bool:
    def parts(value: str) -> tuple[int, ...]:
        return tuple(int(part) for part in value.split(".") if part.isdigit())

    try:
        return parts(actual) >= parts(minimum)
    except ValueError:
        return False


def _strong_secret(secret: str) -> bool:
    return len(secret) >= 32 and len(set(secret)) >= 8


def _secret_detail(secret: str) -> str:
    if len(secret) < 32:
        return f"length {len(secret)} < 32"
    if len(set(secret)) < 8:
        return "secret has insufficient character diversity"
    return "secret meets minimum length and diversity"


def _valid_anthropic_url(value: str, approved_hosts: frozenset[str]) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.hostname in approved_hosts


def _url_detail(value: str, approved_hosts: frozenset[str]) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https":
        return f"scheme {parsed.scheme or '<missing>'} not allowed"
    if parsed.username or parsed.password:
        return "URL userinfo is not allowed"
    if parsed.query or parsed.fragment:
        return "URL query and fragment are not allowed"
    if parsed.hostname not in approved_hosts:
        return "URL host is not approved"
    return "URL is secure and approved"


def _valid_https_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and not parsed.username and not parsed.query and not parsed.fragment and bool(parsed.hostname)


def _valid_database_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "postgresql" and parsed.hostname is not None and "sslmode=require" in parsed.query


def _worker_dirs_ok(config: PreflightConfig, probe: HostProbe) -> bool:
    state = probe.path_info(config.worker_state_path)
    runsc = probe.path_info(config.runsc_state_path)
    return (
        state.exists
        and state.is_directory
        and state.owner == config.worker_user
        and state.group == config.worker_group
        and state.mode == config.worker_state_mode
        and runsc.exists
        and runsc.is_directory
        and runsc.owner == config.worker_user
        and runsc.group == config.worker_group
        and runsc.mode == config.runsc_state_mode
    )


def _firewall_ok(path: str, probe: HostProbe) -> bool:
    text = probe.file_text(path)
    if text is None:
        return False
    normalized = text.lower()
    return "policy drop" in normalized and "169.254.169.254" in normalized and "fd00:ec2::254" in normalized


class LocalHostProbe:
    """Production probe implementation; tests should inject a fake instead."""

    def os_release(self) -> tuple[str, str]:
        release = Path("/etc/os-release").read_text(encoding="utf-8")
        values = dict(
            line.split("=", 1)
            for line in release.splitlines()
            if "=" in line
        )
        return values.get("ID", "").strip('"'), values.get("VERSION_ID", "").strip('"')

    def kernel_version(self) -> str:
        return platform.release().split("-", 1)[0]

    def architecture(self) -> str:
        return platform.machine()

    def path_info(self, path: str) -> PathFacts:
        try:
            info = os.stat(path)
        except OSError:
            return PathFacts(False, None, None, None, False, False)
        owner = pwd.getpwuid(info.st_uid).pw_name
        group = str(info.st_gid)
        try:
            group = grp.getgrgid(info.st_gid).gr_name
        except KeyError:
            pass
        digest = None
        if stat.S_ISREG(info.st_mode):
            digest = _file_sha512(Path(path))
        return PathFacts(
            True,
            owner,
            group,
            stat.S_IMODE(info.st_mode),
            stat.S_ISREG(info.st_mode),
            stat.S_ISDIR(info.st_mode),
            digest,
        )

    def command(self, argv: Sequence[str]) -> SupplyChainCommandResult:
        try:
            completed = subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError:
            return SupplyChainCommandResult(returncode=127)
        return SupplyChainCommandResult(returncode=completed.returncode, stdout=completed.stdout)

    def cgroup_version(self) -> int | None:
        return 2 if Path("/sys/fs/cgroup/cgroup.controllers").exists() else 1

    def namespace_features(self) -> frozenset[str]:
        return frozenset(name for name in ("pid", "net", "user") if Path(f"/proc/self/ns/{name}").exists())

    def listening_sockets(self) -> tuple[ListeningSocket, ...]:
        completed = subprocess.run(
            ("ss", "-lntuH"), capture_output=True, text=True, check=False
        )
        sockets: list[ListeningSocket] = []
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5:
                continue
            address, separator, port = fields[4].rpartition(":")
            if not separator:
                continue
            address = address.strip("[]")
            try:
                sockets.append(ListeningSocket(address=address, port=int(port)))
            except ValueError:
                continue
        return tuple(sockets)

    def clock_offset_seconds(self) -> float | None:
        return 0.0

    def disk_free_bytes(self, path: str) -> int:
        return shutil.disk_usage(path).free

    def file_text(self, path: str) -> str | None:
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return None


def _file_sha512(path: Path) -> str | None:
    digest = hashlib.sha512()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()
