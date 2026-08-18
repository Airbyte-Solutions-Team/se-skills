#!/opt/se-skills/venv/bin/python -I
"""Root-owned broker for the hosted worker's gVisor OCI contract.

The worker supplies only a typed request on stdin. This broker owns the OCI
configuration, durable runsc state, and bundle creation so a worker compromise
cannot author a privileged runsc document.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

CONFIG_PATH = Path("/etc/se-skills/runsc-broker.json")
CONTAINER_ID_PREFIX = "se-"
CONTAINER_ID_LENGTH = 15
REQUEST_OPERATIONS = frozenset({"run", "list", "delete", "cleanup"})
SANDBOX_UID = 65532
SANDBOX_GID = 65532


class BrokerError(Exception):
    """Raised for a rejected request or unsafe host path."""


@dataclass(frozen=True)
class BrokerConfig:
    """Root-owned broker configuration."""

    runsc: Path
    rootfs: Path
    state_root: Path
    bundle_root: Path
    staging_root: Path
    workspace_root: Path
    worker_uid: int
    worker_gid: int

    @classmethod
    def load(cls, path: Path | None = None) -> "BrokerConfig":
        """Load and strictly validate the root-owned configuration."""
        path = path or CONFIG_PATH
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BrokerError("broker configuration unavailable") from exc
        required = {
            "runsc",
            "rootfs",
            "state_root",
            "bundle_root",
            "staging_root",
            "workspace_root",
            "worker_uid",
            "worker_gid",
        }
        if set(raw) != required:
            raise BrokerError("broker configuration has unexpected fields")
        if not all(isinstance(raw[key], str) for key in required - {"worker_uid", "worker_gid"}):
            raise BrokerError("broker configuration has invalid paths")
        if not all(isinstance(raw[key], int) for key in ("worker_uid", "worker_gid")):
            raise BrokerError("broker configuration has invalid ownership")
        return cls(
            runsc=Path(raw["runsc"]),
            rootfs=Path(raw["rootfs"]),
            state_root=Path(raw["state_root"]),
            bundle_root=Path(raw["bundle_root"]),
            staging_root=Path(raw["staging_root"]),
            workspace_root=Path(raw["workspace_root"]),
            worker_uid=raw["worker_uid"],
            worker_gid=raw["worker_gid"],
        )


class BrokerModel(BaseModel):
    """Strict immutable model for the root-owned broker boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class BrokerManifest(BrokerModel):
    transcript_id: StrictStr
    transcript_ref: StrictStr
    account_id: StrictStr
    org_id: StrictStr
    opportunity_id: StrictStr | None
    prior_context_refs: list[StrictStr]


class BrokerNetworkDestination(BrokerModel):
    host: StrictStr
    port: StrictInt | None
    scheme: Literal["http", "https"]
    path_prefix: StrictStr


class BrokerAllowlist(BrokerModel):
    tools: list[StrictStr]
    network: list[BrokerNetworkDestination]


class BrokerJob(BrokerModel):
    job_id: StrictStr
    org_id: StrictStr
    account_id: StrictStr
    transcript_id: StrictStr
    requester_id: StrictStr
    opportunity_id: StrictStr | None
    skill: StrictStr
    skill_version: StrictStr
    requested_model: StrictStr
    requested_runtime_version: StrictStr | None
    mode: Literal["full", "brief"]
    attempt_number: StrictInt
    input_manifest: BrokerManifest
    allowlist: BrokerAllowlist
    execution_deadline: StrictStr
    input_workspace: StrictStr
    output_workspace: StrictStr
    attempt_id: StrictStr
    proxy_token: StrictStr | None
    proxy_uds_path: StrictStr | None


class RunRequest(BrokerModel):
    operation: Literal["run"]
    container_id: StrictStr
    input_dir: StrictStr
    output_dir: StrictStr
    proxy_uds_path: StrictStr | None
    job: BrokerJob


class ListRequest(BrokerModel):
    operation: Literal["list"]
    container_id: StrictStr
    state_dir: StrictStr


class DeleteRequest(BrokerModel):
    operation: Literal["delete"]
    container_id: StrictStr
    state_dir: StrictStr


class CleanupRequest(BrokerModel):
    operation: Literal["cleanup"]
    minimum_age_seconds: StrictInt = Field(ge=0)


BrokerRequest = RunRequest | ListRequest | DeleteRequest | CleanupRequest


def _fail() -> None:
    """Emit one fixed diagnostic without reflecting untrusted input."""
    print("invalid runsc broker request", file=sys.stderr)
    raise SystemExit(64)


def _validate_container_id(container_id: str) -> str:
    if (
        len(container_id) != CONTAINER_ID_LENGTH
        or not container_id.startswith(CONTAINER_ID_PREFIX)
        or any(char not in "0123456789abcdef" for char in container_id[3:])
    ):
        raise BrokerError("invalid container id")
    return container_id


def _load_request(stream: TextIO) -> BrokerRequest:
    try:
        request = json.load(stream)
    except (ValueError, TypeError):
        raise BrokerError("request is not JSON") from None
    if not isinstance(request, dict):
        raise BrokerError("request is not an object")
    operation = request.get("operation")
    model_type: type[RunRequest | ListRequest | DeleteRequest | CleanupRequest]
    if operation == "run":
        model_type = RunRequest
    elif operation == "list":
        model_type = ListRequest
    elif operation == "delete":
        model_type = DeleteRequest
    elif operation == "cleanup":
        model_type = CleanupRequest
    else:
        raise BrokerError("unsupported operation")
    try:
        parsed = model_type.model_validate(request)
    except ValidationError as exc:
        raise BrokerError("request failed typed validation") from exc
    if isinstance(parsed, (RunRequest, ListRequest, DeleteRequest)):
        _validate_container_id(parsed.container_id)
    return parsed


def _safe_worker_path(
    path: Path,
    prefix: str,
    workspace_root: Path,
    worker_uid: int | None = None,
) -> None:
    if path.parent != workspace_root or not path.name.startswith(prefix):
        raise BrokerError("worker path is outside the approved workspace")
    try:
        facts = path.lstat()
    except OSError as exc:
        raise BrokerError("worker path is unavailable") from exc
    if (
        not stat.S_ISDIR(facts.st_mode)
        or stat.S_ISLNK(facts.st_mode)
        or (worker_uid is not None and facts.st_uid != worker_uid)
    ):
        raise BrokerError("worker workspace is not a directory")


def _safe_socket_path(
    path: Path,
    workspace_root: Path,
    worker_uid: int | None = None,
) -> None:
    if path.parent.parent != workspace_root or not path.parent.name.startswith("se-proxy-"):
        raise BrokerError("proxy path is outside the approved workspace")
    try:
        facts = path.lstat()
    except OSError as exc:
        raise BrokerError("proxy socket is unavailable") from exc
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISSOCK(facts.st_mode)
        or (worker_uid is not None and facts.st_uid != worker_uid)
    ):
        raise BrokerError("proxy path is not a socket")


def _root_directory(path: Path) -> None:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise BrokerError("broker root is unavailable") from exc
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISDIR(facts.st_mode)
        or facts.st_uid != 0
        or facts.st_mode & 0o022
    ):
        raise BrokerError("broker root is unsafe")


def _root_executable(path: Path) -> None:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise BrokerError("broker executable is unavailable") from exc
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISREG(facts.st_mode)
        or facts.st_uid != 0
        or facts.st_mode & 0o022
        or not facts.st_mode & 0o111
    ):
        raise BrokerError("broker executable is unsafe")


def _make_tree_read_only(path: Path) -> None:
    for child in path.rglob("*"):
        if child.is_symlink():
            raise BrokerError("worker workspace contains a symlink")
        facts = child.lstat()
        os.chown(child, SANDBOX_UID, SANDBOX_GID)
        if child.is_dir():
            os.chmod(child, 0o555)
        elif child.is_file():
            os.chmod(child, 0o444)
        else:
            raise BrokerError("worker workspace contains a special file")
    os.chown(path, SANDBOX_UID, SANDBOX_GID)
    os.chmod(path, 0o555)


def _make_output_writable(path: Path, config: BrokerConfig) -> None:
    for child in path.rglob("*"):
        if child.is_symlink():
            raise BrokerError("worker output contains a symlink")
        facts = child.lstat()
        if child.is_dir():
            os.chown(child, config.worker_uid, SANDBOX_GID)
            os.chmod(child, 0o770)
        elif child.is_file():
            os.chown(child, config.worker_uid, SANDBOX_GID)
            os.chmod(child, 0o660)
        else:
            raise BrokerError("worker output contains a special file")
    os.chown(path, config.worker_uid, SANDBOX_GID)
    os.chmod(path, 0o770)


def _fixed_config(
    config: BrokerConfig,
    state_dir: Path,
    bundle_dir: Path,
    job_path: Path,
    input_dir: Path,
    output_dir: Path,
    proxy_path: Path | None,
    container_id: str,
    cpu_limit_seconds: int = 120,
) -> dict[str, object]:
    mounts: list[dict[str, object]] = [
        {"destination": "/proc", "source": "proc", "type": "proc"},
        {
            "destination": "/tmp",
            "source": "tmpfs",
            "type": "tmpfs",
            "options": ["nosuid", "nodev", "noexec", "mode=1777", "size=128m"],
        },
        {
            "destination": "/runtime/job.json",
            "source": str(job_path),
            "type": "bind",
            "options": ["bind", "ro"],
        },
        {
            "destination": "/runtime/input",
            "source": str(input_dir),
            "type": "bind",
            "options": ["bind", "ro"],
        },
        {
            "destination": "/runtime/output",
            "source": str(output_dir),
            "type": "bind",
            "options": ["bind", "rw"],
        },
    ]
    if proxy_path is not None:
        mounts.append({
            "destination": "/runtime/proxy.sock",
            "source": str(proxy_path),
            "type": "bind",
            "options": ["bind", "rw"],
        })
    return {
        "ociVersion": "1.1.0",
        "process": {
            "terminal": False,
            "user": {"uid": 65532, "gid": 65532, "umask": 27},
            "args": ["/usr/bin/python3", "/app/webapp/hosted/runsc/sandbox_entry.py"],
            "env": [
                "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "PYTHONPATH=/app/venv/lib/python3.11/site-packages:/app",
                "SE_RUNTIME_JOB_PATH=/runtime/job.json",
                "SE_RUNTIME_RESULT_PATH=/runtime/output/result.json",
            ],
            "cwd": "/tmp",
            "rlimits": [
                {
                    "type": "RLIMIT_CPU",
                    "hard": cpu_limit_seconds,
                    "soft": cpu_limit_seconds,
                },
                {"type": "RLIMIT_AS", "hard": 2_000_000_000, "soft": 2_000_000_000},
                {"type": "RLIMIT_NOFILE", "hard": 1024, "soft": 1024},
                {"type": "RLIMIT_FSIZE", "hard": 100_000_000, "soft": 100_000_000},
                {"type": "RLIMIT_NPROC", "hard": 64, "soft": 64},
            ],
            "noNewPrivileges": True,
            "capabilities": {
                "bounding": [], "effective": [], "permitted": [],
                "inheritable": [], "ambient": [],
            },
        },
        "root": {"path": str(config.rootfs), "readonly": True},
        "hostname": container_id,
        "mounts": mounts,
        "linux": {
            "namespaces": [
                {"type": "pid"}, {"type": "network"}, {"type": "ipc"},
                {"type": "uts"}, {"type": "mount"},
            ],
            "resources": {
                "cpu": {"shares": 1024, "quota": 100000, "period": 100000},
                "memory": {"limit": 2147483648, "reservation": 268435456},
                "pids": {"limit": 64},
            },
            "maskedPaths": [
                "/proc/acpi", "/proc/asound", "/proc/kcore", "/proc/keys",
                "/proc/latency_stats", "/proc/timer_list", "/proc/timer_stats",
                "/proc/sched_debug", "/proc/scsi", "/sys/firmware",
                "/sys/devices/virtual/powercap",
            ],
            "readonlyPaths": [
                "/proc/bus", "/proc/fs", "/proc/irq", "/proc/sys",
                "/proc/sysrq-trigger",
            ],
        },
    }


def _run(config: BrokerConfig, request: RunRequest) -> int:
    for root in (
        config.state_root,
        config.bundle_root,
        config.staging_root,
        config.workspace_root,
    ):
        _root_directory(root)
    _root_directory(config.rootfs)
    _root_executable(config.runsc)
    input_dir = Path(request.input_dir)
    output_dir = Path(request.output_dir)
    proxy_uds_path = Path(request.proxy_uds_path) if request.proxy_uds_path else None
    _safe_worker_path(input_dir, "se-runtime-input-", config.workspace_root, config.worker_uid)
    _safe_worker_path(output_dir, "se-runtime-output-", config.workspace_root, config.worker_uid)
    if proxy_uds_path is not None:
        _safe_socket_path(proxy_uds_path, config.workspace_root, config.worker_uid)
    container_dir = config.staging_root / request.container_id
    bundle_dir = config.bundle_root / request.container_id
    state_dir = config.state_root / request.container_id
    if any(path.exists() or path.is_symlink() for path in (container_dir, bundle_dir, state_dir)):
        raise BrokerError("container state already exists")
    container_dir.mkdir(mode=0o700)
    bundle_dir.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    staged_input = container_dir / "input"
    staged_output = container_dir / "output"
    staged_proxy = container_dir / "proxy.sock"
    job_path = bundle_dir / "job.json"
    config_path = bundle_dir / "config.json"
    input_sealed = False
    output_sealed = False
    try:
        os.rename(input_dir, staged_input)
        input_sealed = True
        os.rename(output_dir, staged_output)
        output_sealed = True
        for staged_path in (staged_input, staged_output):
            facts = staged_path.lstat()
            if stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode):
                raise BrokerError("worker workspace sealing failed")
        os.mkdir(output_dir, 0o770)
        os.chown(output_dir, config.worker_uid, SANDBOX_GID)
        _make_tree_read_only(staged_input)
        _make_output_writable(staged_output, config)
        if proxy_uds_path is not None:
            os.rename(proxy_uds_path, staged_proxy)
            facts = staged_proxy.lstat()
            if (
                stat.S_ISLNK(facts.st_mode)
                or not stat.S_ISSOCK(facts.st_mode)
                or facts.st_uid != config.worker_uid
            ):
                raise BrokerError("proxy sealing failed")
        job_path.write_text(
            json.dumps(
                request.job.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chown(job_path, SANDBOX_UID, SANDBOX_GID)
        os.chmod(job_path, 0o444)
        try:
            deadline = datetime.fromisoformat(request.job.execution_deadline)
        except ValueError as exc:
            raise BrokerError("invalid execution deadline") from exc
        if deadline.tzinfo is None:
            raise BrokerError("invalid execution deadline")
        remaining = max(
            (deadline - datetime.now(timezone.utc)).total_seconds(),
            1.0,
        )
        cpu_limit_seconds = max(1, math.ceil(remaining))
        config_path.write_text(
            json.dumps(
                _fixed_config(
                    config, state_dir, bundle_dir, job_path, staged_input,
                    staged_output, staged_proxy if proxy_uds_path else None,
                    request.container_id,
                    cpu_limit_seconds,
                ),
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chown(config_path, 0, 0)
        os.chmod(config_path, 0o400)
        process = subprocess.run(
            [
                str(config.runsc), f"--root={state_dir}", "--network=none",
                "run", "--bundle", str(bundle_dir), request.container_id,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return process.returncode
    finally:
        if output_sealed and output_dir.exists() and output_dir.is_dir():
            shutil.rmtree(output_dir, ignore_errors=True)
        if output_sealed and staged_output.exists():
            os.rename(staged_output, output_dir)
            _make_output_writable(output_dir, config)
        if not output_sealed and output_dir.exists() and not output_dir.is_dir():
            raise BrokerError("worker output compensation failed")
        if input_sealed and staged_input.exists():
            shutil.rmtree(staged_input, ignore_errors=True)
        shutil.rmtree(container_dir, ignore_errors=True)
        shutil.rmtree(bundle_dir, ignore_errors=True)


def _run_simple(
    config: BrokerConfig,
    operation: Literal["list", "delete"],
    root_dir: Path,
    container_id: str,
) -> int:
    _root_directory(config.state_root)
    _root_executable(config.runsc)
    expected = config.state_root / container_id
    if root_dir != expected:
        raise BrokerError("state path is outside the approved root")
    if not expected.is_dir() or expected.is_symlink():
        raise BrokerError("state path is unavailable")
    command = [str(config.runsc), f"--root={expected}", operation]
    if operation == "list":
        command.append("--format=text")
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False
        )
        sys.stdout.buffer.write(result.stdout)
        return result.returncode
    command.extend(["--force", container_id])
    result = subprocess.run(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False
    )
    if result.returncode != 0:
        return result.returncode
    listed = subprocess.run(
        [str(config.runsc), f"--root={expected}", "list", "--format=text"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if listed.returncode != 0:
        return listed.returncode
    lines = listed.stdout.decode("utf-8", errors="replace").splitlines()
    if not lines or not lines[0].startswith("ID"):
        return 65
    if any(line.split()[:1] == [container_id] for line in lines[1:]):
        return 66
    shutil.rmtree(expected)
    return 0


def _run_cleanup(config: BrokerConfig, minimum_age_seconds: int) -> int:
    """Reclaim only old terminal or verified-absent broker-owned state."""
    _root_directory(config.state_root)
    _root_directory(config.bundle_root)
    _root_executable(config.runsc)
    now = int(time.time())
    for state in sorted(config.state_root.iterdir(), key=lambda path: path.name):
        if not state.is_dir() or state.is_symlink() or now - int(state.stat().st_mtime) < minimum_age_seconds:
            continue
        try:
            container_id = _validate_container_id(state.name)
            listed = subprocess.run(
                [str(config.runsc), f"--root={state}", "list", "--format=text"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if listed.returncode != 0:
                continue
            lines = listed.stdout.decode("utf-8", errors="replace").splitlines()
            if not lines or not lines[0].startswith("ID"):
                continue
            status = "absent"
            for line in lines[1:]:
                parts = line.split()
                if parts and parts[0] == container_id:
                    status = parts[2].lower() if len(parts) > 2 else ""
                    break
            if status not in {"dead", "stopped", "exited", "failed", "terminated", "absent"}:
                continue
            if status != "absent" and _run_simple(config, "delete", state, container_id) != 0:
                continue
            if status == "absent":
                shutil.rmtree(state)
            bundle = config.bundle_root / container_id
            if bundle.is_dir() and not bundle.is_symlink():
                shutil.rmtree(bundle)
        except (BrokerError, OSError, ValueError):
            continue
    for bundle in sorted(config.bundle_root.iterdir(), key=lambda path: path.name):
        if (
            bundle.is_dir()
            and not bundle.is_symlink()
            and now - int(bundle.stat().st_mtime) >= minimum_age_seconds
            and not (config.state_root / bundle.name).exists()
        ):
            try:
                _validate_container_id(bundle.name)
                shutil.rmtree(bundle)
            except (BrokerError, OSError):
                continue
    return 0


def main() -> int:
    try:
        config = BrokerConfig.load()
        request = _load_request(sys.stdin)
        if isinstance(request, RunRequest):
            return _run(config, request)
        if isinstance(request, CleanupRequest):
            return _run_cleanup(config, request.minimum_age_seconds)
        if isinstance(request, ListRequest):
            return _run_simple(
                config,
                request.operation,
                Path(request.state_dir),
                request.container_id,
            )
        return _run_simple(
            config,
            request.operation,
            Path(request.state_dir),
            request.container_id,
        )
    except (
        BrokerError,
        OSError,
        AssertionError,
        TypeError,
        ValueError,
        UnicodeError,
    ):
        _fail()
    return 64


if __name__ == "__main__":
    raise SystemExit(main())
