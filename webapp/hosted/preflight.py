"""Fail-closed, injectable checks for a hosted worker host."""
from __future__ import annotations

import grp
import hashlib
import ipaddress
import json
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

from webapp.hosted.firewall_policy import evaluate_output_policy, parse_output_policy
from webapp.hosted.pins import HostedPins, load_pins
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

    def command(
        self, argv: Sequence[str], stdin: str | None = None
    ) -> SupplyChainCommandResult:
        ...

    def cgroup_version(self) -> int | None:
        ...

    def namespace_features(self) -> frozenset[str]:
        ...

    def listening_sockets(self) -> tuple["ListeningSocket", ...] | None:
        ...

    def clock_offset_seconds(self) -> float | None:
        ...

    def disk_free_bytes(self, path: str) -> int | None:
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
    runsc_helper_path: str = "/usr/local/sbin/se-skills-runsc"
    runsc_sudoers_path: str = "/etc/sudoers.d/se-skills-runsc"
    runsc_broker_config_path: str = "/etc/se-skills/runsc-broker.json"
    broker_interpreter_path: str = "/usr/bin/python3"
    broker_script_path: str = "/usr/local/sbin/se-skills-runsc"
    broker_venv_path: str = ""
    broker_import_paths: tuple[str, ...] = ()
    runsc_rootless: bool = False
    pins: HostedPins = Field(default_factory=load_pins)
    required_names: tuple[str, ...] = (
        "DATABASE_WORKER_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_EGRESS_PROXY_URL",
        "MODEL_PROXY_SECRET",
        "RUNSC_ROOTFS",
        "SANDBOX_IMAGE_DIGEST",
        "SANDBOX_MANIFEST_PATH",
    )
    present_config_names: frozenset[str] = frozenset()
    model_proxy_secret: str = ""
    anthropic_api_url: str = "https://api.anthropic.com"
    anthropic_proxy_url: str = ""
    anthropic_proxy_host: str = ""
    anthropic_proxy_port: int = 3128
    approved_anthropic_hosts: frozenset[str] = frozenset({"api.anthropic.com"})
    database_url: str = ""
    storage_url: str = ""
    worker_user: str = "se-worker"
    worker_group: str = "se-worker"
    worker_state_path: str = "/var/lib/se-skills"
    worker_state_mode: int = 0o750
    runsc_state_path: str = "/var/lib/se-skills/runsc"
    runsc_state_mode: int = 0o700
    bundle_path: str = "/var/lib/se-skills/bundles"
    bundle_mode: int = 0o700
    workspace_path: str = "/var/lib/se-skills/workspaces"
    workspace_mode: int = 0o730
    worker_uid: int = 995
    non_worker_uid: int = 994
    sandbox_uid: int = 65532
    sandbox_gid: int = 65532
    journal_phase_pause_seconds: float = 0.0
    rootfs_path: str = ""
    clock_tolerance_seconds: float = 1.0
    supply_chain: SupplyChainVerification | None = None
    supply_chain_skipped: bool = False
    hosted_env: str = "development"
    runtime: str = "echo"
    approved_https_destinations: frozenset[str] = frozenset()
    approved_management_ssh_cidr: str = ""
    approved_management_ssh_port: int = 22
    supply_chain_manifest_status: str | None = None
    supply_chain_manifest_detail: str = ""


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
    pins = config.pins
    os_name, os_version = probe.os_release()
    os_ok = os_name == pins.host.os and os_version == pins.host.os_version
    checks.append(
        _required("host_os", os_ok, "supported OS and version" if os_ok else "unsupported OS or version")
    )
    architecture = probe.architecture()
    arch_ok = architecture == pins.host.arch
    checks.append(
        _required("host_architecture", arch_ok, "supported architecture" if arch_ok else "unsupported architecture")
    )
    kernel = probe.kernel_version()
    kernel_ok = _version_at_least(kernel, pins.host.min_kernel)
    checks.append(
        _required("host_kernel", kernel_ok, "kernel meets minimum" if kernel_ok else "kernel is below minimum")
    )
    runsc = probe.path_info(config.runsc_path)
    runsc_mode_ok = (
        runsc.exists
        and runsc.is_file
        and runsc.mode == 0o755
        and runsc.owner == "root"
        and runsc.group == "root"
    )
    checks.append(
        _required("runsc_binary", runsc_mode_ok, "runsc is root-owned executable" if runsc_mode_ok else "runsc missing or ownership/mode is unsafe")
    )
    broker_interpreter = probe.path_info(config.broker_interpreter_path)
    broker_script = probe.path_info(config.broker_script_path)
    actual_import_paths = _broker_import_paths(config, probe)
    broker_import_paths_ok = bool(actual_import_paths) and all(
        _safe_import_file(probe.path_info(path))
        or _safe_import_directory(probe.path_info(path))
        for path in actual_import_paths
    )
    broker_runtime_ok = (
        config.broker_script_path == config.runsc_helper_path
        and _safe_import_file(broker_interpreter)
        and _safe_import_file(broker_script)
        and broker_import_paths_ok
    )
    checks.append(
        _required(
            "broker_interpreter",
            broker_runtime_ok,
            "broker interpreter and import paths are root-owned and non-writable"
            if broker_runtime_ok
            else "broker interpreter or import paths are missing or unsafe",
        )
    )
    helper = probe.path_info(config.runsc_helper_path)
    broker_config = probe.path_info(config.runsc_broker_config_path)
    broker_text = probe.file_text(config.runsc_broker_config_path) or ""
    try:
        broker_document = json.loads(broker_text)
    except (TypeError, ValueError):
        broker_document = {}
    pause_value = broker_document.get("journal_phase_pause_seconds", 0.0)
    pause_ok = (
        isinstance(pause_value, (int, float))
        and not isinstance(pause_value, bool)
        and pause_value == config.journal_phase_pause_seconds
    )
    helper_roots_ok = (
        broker_config.exists
        and broker_config.is_file
        and broker_config.owner == "root"
        and broker_config.mode == 0o644
        and f'"runsc": "{config.runsc_path}"' in broker_text
        and f'"state_root": "{config.runsc_state_path}"' in broker_text
        and f'"bundle_root": "{config.bundle_path}"' in broker_text
        and f'"workspace_root": "{config.workspace_path}"' in broker_text
        and f'"sandbox_uid": {config.sandbox_uid}' in broker_text
        and f'"sandbox_gid": {config.sandbox_gid}' in broker_text
        and pause_ok
    )
    helper_ok = (
        helper.exists
        and helper.is_file
        and helper.mode == 0o755
        and helper.owner == "root"
        and helper.group == "root"
        and helper_roots_ok
    )
    checks.append(
        _required(
            "runsc_helper",
            helper_ok,
            "runsc helper is root-owned and executable"
            if helper_ok
            else "runsc helper is missing or ownership/mode is unsafe",
        )
    )
    sudoers = probe.path_info(config.runsc_sudoers_path)
    sudoers_text = probe.file_text(config.runsc_sudoers_path) or ""
    sudoers_ok = (
        sudoers.exists
        and sudoers.is_file
        and sudoers.mode == 0o440
        and sudoers.owner == "root"
        and sudoers.group == "root"
        and sudoers_text.strip()
        == f'{config.worker_user} ALL=(root) NOPASSWD: {config.runsc_helper_path} ""'
    )
    checks.append(
        _required(
            "runsc_sudoers",
            sudoers_ok,
            "runsc sudoers policy is constrained"
            if sudoers_ok
            else "runsc sudoers policy is missing or unconstrained",
        )
    )
    runtime_helper_argv = (
        "sudo",
        "--non-interactive",
        config.runsc_helper_path,
    )
    rejected_request = probe.command(
        runtime_helper_argv,
        stdin='{"operation":"run","unknown":"field"}\n',
    )
    helper_invocation = probe.command(
        runtime_helper_argv,
        stdin=(
            '{"operation":"list","container_id":"se-000000000000",'
            f'"state_dir":"{config.runsc_state_path}/se-000000000000"}}\n'
        ),
    )
    checks.append(
        _required(
            "runsc_helper_invocation",
            rejected_request.returncode != 0 and helper_invocation.returncode == 0,
            "worker can invoke the constrained runsc helper"
            if rejected_request.returncode != 0 and helper_invocation.returncode == 0
            else "worker cannot invoke the constrained runsc helper",
        )
    )
    checks.append(
        _required(
            "runsc_privilege_model",
            config.runsc_rootless is False,
            "runsc uses the privileged launcher model"
            if config.runsc_rootless is False
            else "rootless runsc is not supported by the host contract",
        )
    )
    version_result = probe.command((config.runsc_path, "--version"))
    version_ok = version_result.returncode == 0 and pins.runsc.version in version_result.stdout
    checks.append(
        _required("runsc_version", version_ok, "runsc version matches pin" if version_ok else "runsc version does not match pin")
    )
    checksum = pins.runsc_checksum(architecture)
    checksum_ok = bool(runsc.sha512) and checksum is not None and runsc.sha512 == checksum
    checks.append(
        _required("runsc_checksum", checksum_ok, "runsc checksum matches pin" if checksum_ok else "runsc checksum does not match pin")
    )
    cgroup_version = probe.cgroup_version()
    cgroup_ok = cgroup_version == pins.host.cgroup_version
    checks.append(
        _required("cgroup_v2", cgroup_ok, "required cgroup version is enabled" if cgroup_ok else "required cgroup version is unavailable")
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
    checks.append(
        _required(
            "anthropic_egress_proxy",
            _valid_proxy_url(config.anthropic_proxy_url),
            "Anthropic egress proxy URL is configured"
            if _valid_proxy_url(config.anthropic_proxy_url)
            else "Anthropic egress proxy URL is missing or unsafe",
        )
    )
    database_ok = _valid_database_url(config.database_url)
    checks.append(_required("database_url", database_ok, "database URL is secure" if database_ok else "database URL must use postgresql with TLS"))
    storage_ok = _valid_https_url(config.storage_url)
    checks.append(_required("storage_url", storage_ok, "Storage URL is secure" if storage_ok else "storage URL must use https"))
    worker_dirs_ok = _worker_dirs_ok(config, probe)
    checks.append(_required("worker_directories", worker_dirs_ok, "worker directories have expected ownership and modes" if worker_dirs_ok else "worker directory ownership or mode is unsafe"))
    rootfs = probe.path_info(config.rootfs_path) if config.rootfs_path else PathFacts(False, None, None, None, False, False)
    checks.append(_required("rootfs_path", rootfs.exists and rootfs.is_directory, "sandbox rootfs directory is present" if rootfs.exists and rootfs.is_directory else "sandbox rootfs directory is missing"))
    namespaces = probe.namespace_features()
    has_network_namespace = "net" in namespaces or "network" in namespaces
    has_required_namespaces = {"pid", "user"}.issubset(namespaces) and has_network_namespace
    checks.append(_required("namespace_features", has_required_namespaces, "required namespace features are present" if has_required_namespaces else "required namespace features are missing"))
    root_free = probe.disk_free_bytes("/")
    root_disk_ok = root_free is not None and root_free >= pins.limits.min_root_free_bytes
    checks.append(_required("root_disk_free", root_disk_ok, "root filesystem has sufficient free space" if root_disk_ok else "root filesystem free space is unavailable or below minimum"))
    temp_free = probe.disk_free_bytes("/tmp")
    temp_disk_ok = temp_free is not None and temp_free >= pins.limits.min_temp_free_bytes
    checks.append(_required("temp_disk_free", temp_disk_ok, "temporary filesystem has sufficient free space" if temp_disk_ok else "temporary filesystem free space is unavailable or below minimum"))
    offset = probe.clock_offset_seconds()
    checks.append(_required("clock_sync", offset is not None and abs(offset) <= config.clock_tolerance_seconds, "clock offset is within tolerance" if offset is not None and abs(offset) <= config.clock_tolerance_seconds else "clock offset exceeds tolerance"))
    firewall_ok = _firewall_ok(config, probe)
    checks.append(_required("firewall_policy", firewall_ok, "firewall is deny-by-default and blocks metadata" if firewall_ok else "firewall policy is missing or unsafe"))
    listeners = probe.listening_sockets()
    listener_ok = listeners is not None and _listeners_ok(config, probe, listeners)
    checks.append(_required("public_listener", listener_ok, "no unexpected public listener" if listener_ok else "listener enumeration failed or found an unexpected public listener"))
    production_runtime_ok = config.hosted_env != "production" or config.runtime == "post-call-runsc"
    checks.append(_required("production_runtime_policy", production_runtime_ok, "selected runtime is fail-closed" if production_runtime_ok else "production runtime is not fail-closed"))
    if config.supply_chain_manifest_status is not None:
        checks.append(
            _required(
                "supply_chain_manifest",
                config.supply_chain_manifest_status == "ok",
                config.supply_chain_manifest_detail,
            )
        )
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


def _safe_import_file(facts: PathFacts) -> bool:
    return (
        facts.exists
        and facts.is_file
        and facts.owner == "root"
        and facts.group == "root"
        and facts.mode is not None
        and facts.mode & 0o022 == 0
    )


def _safe_import_directory(facts: PathFacts) -> bool:
    return (
        facts.exists
        and facts.is_directory
        and facts.owner == "root"
        and facts.group == "root"
        and facts.mode is not None
        and facts.mode & 0o022 == 0
    )


def _broker_import_paths(
    config: PreflightConfig, probe: HostProbe
) -> tuple[str, ...]:
    result = probe.command(
        (
            config.broker_interpreter_path,
            "-I",
            "-c",
            "import sys; print('\\n'.join(sys.path))",
        )
    )
    if result.returncode != 0:
        return ()
    paths = tuple(path for path in result.stdout.splitlines() if path)
    if not paths or any(not Path(path).is_absolute() for path in paths):
        return ()
    return paths


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
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and bool(parsed.hostname)
    )


def _valid_proxy_url(value: str) -> bool:
    parsed = urlparse(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not parsed.query
        and not parsed.fragment
        and parsed.path in {"", "/"}
    )


def _valid_database_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "postgresql" and parsed.hostname is not None and "sslmode=require" in parsed.query


def _worker_dirs_ok(config: PreflightConfig, probe: HostProbe) -> bool:
    state = probe.path_info(config.worker_state_path)
    runsc = probe.path_info(config.runsc_state_path)
    bundles = probe.path_info(config.bundle_path)
    workspace = probe.path_info(config.workspace_path)
    return (
        state.exists
        and state.is_directory
        and state.owner == "root"
        and state.group == "root"
        and state.mode == config.worker_state_mode
        and runsc.exists
        and runsc.is_directory
        and runsc.owner == "root"
        and runsc.group == "root"
        and runsc.mode == config.runsc_state_mode
        and bundles.exists
        and bundles.is_directory
        and bundles.owner == "root"
        and bundles.group == "root"
        and bundles.mode == config.bundle_mode
        and workspace.exists
        and workspace.is_directory
        and workspace.owner == "root"
        and workspace.group == config.worker_group
        and workspace.mode == config.workspace_mode
    )


def _firewall_ok(config: PreflightConfig, probe: HostProbe) -> bool:
    result = probe.command(("nft", "list", "table", "inet", "se_skills"))
    if result.returncode != 0:
        return False
    parsed = parse_output_policy(result.stdout)
    if (
        not parsed.valid
        or parsed.input_policy != "drop"
        or parsed.output_policy != "drop"
    ):
        return False
    rules = parsed.rules
    checks = (
        ("169.254.169.254", "tcp", 443, "drop"),
        ("10.0.0.1", "tcp", 443, "drop"),
        ("fd00::1", "tcp", 443, "drop"),
        ("93.184.216.34", "tcp", 443, "drop"),
    )
    if any(
        evaluate_output_policy(rules, config.worker_uid, destination, protocol, port)
        != expected
        for destination, protocol, port, expected in checks
    ):
        return False
    if (
        not config.anthropic_proxy_host
        or _is_ipv4_address(config.anthropic_proxy_host) is False
        or evaluate_output_policy(
            rules,
            config.worker_uid,
            config.anthropic_proxy_host,
            "tcp",
            config.anthropic_proxy_port,
        )
        != "accept"
        or evaluate_output_policy(
            rules,
            config.non_worker_uid,
            config.anthropic_proxy_host,
            "tcp",
            config.anthropic_proxy_port,
        )
        != "drop"
        or evaluate_output_policy(
            rules,
            config.worker_uid,
            "10.0.0.1",
            "tcp",
            config.anthropic_proxy_port,
        )
        != "drop"
        or evaluate_output_policy(
            rules,
            config.worker_uid,
            "169.254.169.254",
            "tcp",
            config.anthropic_proxy_port,
        )
        != "drop"
    ):
        return False
    if not config.approved_https_destinations:
        return False
    normalized_destinations: set[str] = set()
    for destination in config.approved_https_destinations:
        try:
            normalized_destinations.add(
                str(ipaddress.ip_network(destination, strict=False))
            )
        except ValueError:
            return False
        if (
            evaluate_output_policy(
                rules, config.worker_uid, destination, "tcp", 443
            )
            != "accept"
            or evaluate_output_policy(
                rules, config.non_worker_uid, destination, "tcp", 443
            )
            != "drop"
        ):
            return False
    rendered_destinations = {
        str(rule.destination)
        for rule in rules
        if (
            rule.uid == config.worker_uid
            and rule.action == "accept"
            and rule.protocol == "tcp"
            and rule.port == 443
            and rule.destination is not None
        )
    }
    return rendered_destinations == normalized_destinations


def _listeners_ok(
    config: PreflightConfig,
    probe: HostProbe,
    listeners: tuple[ListeningSocket, ...],
) -> bool:
    public = tuple(
        item
        for item in listeners
        if item.address not in {"127.0.0.1", "::1", "localhost"}
    )
    if not public:
        return True
    if not config.approved_management_ssh_cidr:
        return False
    if any(item.port != config.approved_management_ssh_port for item in public):
        return False
    result = probe.command(("nft", "list", "table", "inet", "se_skills"))
    if result.returncode != 0:
        return False
    family = "ip6" if ":" in config.approved_management_ssh_cidr else "ip"
    rule = (
        f"{family} saddr {config.approved_management_ssh_cidr} "
        f"tcp dport {config.approved_management_ssh_port} accept"
    )
    return rule in result.stdout


def _is_ipv4_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).version == 4
    except ValueError:
        return False


class LocalHostProbe:
    """Production probe implementation; tests should inject a fake instead."""

    def os_release(self) -> tuple[str, str]:
        try:
            release = Path("/etc/os-release").read_text(encoding="utf-8")
        except OSError:
            return "", ""
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

    def command(
        self, argv: Sequence[str], stdin: str | None = None
    ) -> SupplyChainCommandResult:
        try:
            completed = subprocess.run(
                argv,
                input=stdin,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return SupplyChainCommandResult(returncode=127)
        return SupplyChainCommandResult(returncode=completed.returncode, stdout=completed.stdout)

    def cgroup_version(self) -> int | None:
        return 2 if Path("/sys/fs/cgroup/cgroup.controllers").exists() else 1

    def namespace_features(self) -> frozenset[str]:
        return frozenset(name for name in ("pid", "net", "user") if Path(f"/proc/self/ns/{name}").exists())

    def listening_sockets(self) -> tuple[ListeningSocket, ...] | None:
        try:
            completed = subprocess.run(
                ("ss", "-lntuH"), capture_output=True, text=True, check=False
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
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
        try:
            timedate = subprocess.run(
                (
                    "timedatectl",
                    "show",
                    "-p",
                    "NTPSynchronized",
                    "-p",
                    "TimeUSec",
                    "--value",
                ),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            timedate = None
        if timedate is not None and timedate.returncode == 0:
            values = [line.strip() for line in timedate.stdout.splitlines() if line.strip()]
            if values and values[0].lower() == "yes":
                return 0.0
        try:
            chrony = subprocess.run(
                ("chronyc", "tracking"),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if chrony.returncode != 0:
            return None
        for line in chrony.stdout.splitlines():
            if line.lower().startswith("system time"):
                try:
                    return abs(float(line.split(":", 1)[1].split()[0]))
                except (IndexError, ValueError):
                    return None
        return None

    def disk_free_bytes(self, path: str) -> int | None:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

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
