#!/usr/bin/python3 -I
"""Root-owned broker for the hosted worker's gVisor OCI contract.

The worker supplies only a typed request on stdin. This broker owns the OCI
configuration, durable runsc state, and bundle creation so a worker compromise
cannot author a privileged runsc document.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import select
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

CONFIG_PATH = Path("/etc/se-skills/runsc-broker.json")
CONTAINER_ID_PREFIX = "se-"
CONTAINER_ID_LENGTH = 15
REQUEST_OPERATIONS = frozenset({"run", "list", "finalize", "cleanup"})
_ACTIVE_PROCESS: subprocess.Popen[bytes] | None = None
_ACTIVE_IDENTITY: dict[str, int] | None = None
_ACTIVE_TIMEOUT = 1.0


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
    sandbox_uid: int = 65532
    sandbox_gid: int = 65532
    max_request_bytes: int = 1_000_000
    request_read_timeout_seconds: float = 5.0
    max_string_length: int = 8_192
    max_collection_length: int = 256
    max_nesting_depth: int = 16
    max_concurrent_operations: int = 4
    max_attempt_duration_seconds: int = 900
    operation_timeout_seconds: float = 30.0
    cleanup_min_age_seconds: int = 3600
    journal_root: Path = Path("/var/lib/se-skills/journal")
    journal_phase_pause_seconds: float = 0.0

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
        if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - (
            required
            | {
                "max_request_bytes",
                "request_read_timeout_seconds",
                "max_string_length",
                "max_collection_length",
                "max_nesting_depth",
                "max_concurrent_operations",
                "max_attempt_duration_seconds",
                "operation_timeout_seconds",
                "cleanup_min_age_seconds",
                "journal_root",
                "sandbox_uid",
                "sandbox_gid",
                "journal_phase_pause_seconds",
            }
        ):
            raise BrokerError("broker configuration has unexpected fields")
        if not all(isinstance(raw[key], str) for key in required - {"worker_uid", "worker_gid"}):
            raise BrokerError("broker configuration has invalid paths")
        defaults = {
            "max_request_bytes": 1_000_000,
            "max_string_length": 8_192,
            "max_collection_length": 256,
            "max_nesting_depth": 16,
            "max_concurrent_operations": 4,
            "max_attempt_duration_seconds": 900,
            "cleanup_min_age_seconds": 3600,
        }
        if not all(
            isinstance(raw.get(key, defaults.get(key)), int)
            and not isinstance(raw.get(key, defaults.get(key)), bool)
            for key in (
                "worker_uid",
                "worker_gid",
                "max_request_bytes",
                "max_string_length",
                "max_collection_length",
                "max_nesting_depth",
                "max_concurrent_operations",
                "max_attempt_duration_seconds",
                "sandbox_uid",
                "sandbox_gid",
                "cleanup_min_age_seconds",
            )
        ) or not isinstance(
            raw.get("request_read_timeout_seconds", 5.0),
            (int, float),
        ) or isinstance(
            raw.get("request_read_timeout_seconds", 5.0),
            bool,
        ) or not isinstance(
            raw.get("operation_timeout_seconds", 30.0),
            (int, float),
        ) or isinstance(
            raw.get("operation_timeout_seconds", 30.0),
            bool,
        ):
            raise BrokerError("broker configuration has invalid ownership")
        if not isinstance(
            raw.get("journal_phase_pause_seconds", 0.0), (int, float)
        ) or isinstance(raw.get("journal_phase_pause_seconds", 0.0), bool):
            raise BrokerError("broker configuration has invalid journal timing")
        if any(
            raw.get(key, defaults.get(key, 1)) <= 0
            for key in (
                "max_request_bytes",
                "request_read_timeout_seconds",
                "max_string_length",
                "max_collection_length",
                "max_nesting_depth",
                "max_concurrent_operations",
                "max_attempt_duration_seconds",
                "operation_timeout_seconds",
                "cleanup_min_age_seconds",
            )
        ):
            raise BrokerError("broker configuration has invalid limits")
        if not 0 <= float(raw.get("journal_phase_pause_seconds", 0.0)) <= 1:
            raise BrokerError("broker configuration has invalid journal timing")
        return cls(
            runsc=Path(raw["runsc"]),
            rootfs=Path(raw["rootfs"]),
            state_root=Path(raw["state_root"]),
            bundle_root=Path(raw["bundle_root"]),
            staging_root=Path(raw["staging_root"]),
            workspace_root=Path(raw["workspace_root"]),
            worker_uid=raw["worker_uid"],
            worker_gid=raw["worker_gid"],
            sandbox_uid=raw.get("sandbox_uid", 65532),
            sandbox_gid=raw.get("sandbox_gid", 65532),
            max_request_bytes=raw.get("max_request_bytes", 1_000_000),
            request_read_timeout_seconds=float(
                raw.get("request_read_timeout_seconds", 5.0)
            ),
            max_string_length=raw.get("max_string_length", 8_192),
            max_collection_length=raw.get(
                "max_collection_length", 256
            ),
            max_nesting_depth=raw.get("max_nesting_depth", 16),
            max_concurrent_operations=raw.get(
                "max_concurrent_operations", 4
            ),
            max_attempt_duration_seconds=raw.get(
                "max_attempt_duration_seconds", 900
            ),
            operation_timeout_seconds=float(
                raw.get("operation_timeout_seconds", 30.0)
            ),
            cleanup_min_age_seconds=raw.get("cleanup_min_age_seconds", 3600),
            journal_root=Path(raw.get("journal_root", str(path.parent / "runsc-journal"))),
            journal_phase_pause_seconds=float(
                raw.get("journal_phase_pause_seconds", 0.0)
            ),
        )


@dataclass(frozen=True)
class BrokerJob:
    values: dict[str, object]

    def __getitem__(self, key: str) -> object:
        return self.values[key]


@dataclass(frozen=True)
class RunRequest:
    container_id: str
    input_dir: str
    output_dir: str
    proxy_uds_path: str | None
    job: BrokerJob


@dataclass(frozen=True)
class ListRequest:
    container_id: str
    state_dir: str


@dataclass(frozen=True)
class FinalizeRequest:
    container_id: str


@dataclass(frozen=True)
class CleanupRequest:
    pass


BrokerRequest = RunRequest | ListRequest | FinalizeRequest | CleanupRequest


def _fail() -> None:
    """Emit one fixed diagnostic without reflecting untrusted input."""
    print("invalid runsc broker request", file=sys.stderr)
    raise SystemExit(64)


def _handle_signal(signum: int, _frame: object) -> None:
    process = _ACTIVE_PROCESS
    identity = _ACTIVE_IDENTITY
    if process is not None and identity is not None:
        _signal_owned_process(identity, signal.SIGTERM)
        try:
            process.wait(timeout=_ACTIVE_TIMEOUT)
        except subprocess.TimeoutExpired:
            _signal_owned_process(identity, signal.SIGKILL)
            try:
                process.wait(timeout=_ACTIVE_TIMEOUT)
            except subprocess.TimeoutExpired:
                pass
    raise SystemExit(128 + signum)


def _validate_container_id(container_id: str) -> str:
    if (
        len(container_id) != CONTAINER_ID_LENGTH
        or not container_id.startswith(CONTAINER_ID_PREFIX)
        or any(char not in "0123456789abcdef" for char in container_id[3:])
    ):
        raise BrokerError("invalid container id")
    return container_id


def _read_request(stream: TextIO, config: BrokerConfig) -> dict[str, object]:
    deadline = time.monotonic() + config.request_read_timeout_seconds
    try:
        fd = stream.fileno()
        chunks = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BrokerError("request read timed out")
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                raise BrokerError("request read timed out")
            chunk = os.read(fd, min(65536, config.max_request_bytes - len(chunks) + 1))
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > config.max_request_bytes:
                raise BrokerError("request is too large")
    except (AttributeError, OSError, ValueError):
        text = stream.read(config.max_request_bytes + 1)
        chunks = text.encode("utf-8")
    if len(chunks) > config.max_request_bytes:
        raise BrokerError("request is too large")
    try:
        decoded = bytes(chunks).decode("utf-8")
        request = json.loads(decoded)
    except (UnicodeError, ValueError, TypeError):
        raise BrokerError("request is not JSON") from None
    if not isinstance(request, dict):
        raise BrokerError("request is not an object")
    return request


def _check_bounded_value(
    value: object,
    config: BrokerConfig,
    *,
    depth: int = 0,
) -> None:
    if depth > config.max_nesting_depth:
        raise BrokerError("request nesting is too deep")
    if isinstance(value, str):
        if len(value) > config.max_string_length:
            raise BrokerError("request string is too large")
    elif isinstance(value, list):
        if len(value) > config.max_collection_length:
            raise BrokerError("request collection is too large")
        for item in value:
            _check_bounded_value(item, config, depth=depth + 1)
    elif isinstance(value, dict):
        if len(value) > config.max_collection_length:
            raise BrokerError("request object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > config.max_string_length:
                raise BrokerError("request key is invalid")
            _check_bounded_value(item, config, depth=depth + 1)


def _string(value: object, config: BrokerConfig) -> str:
    if not isinstance(value, str) or not value or len(value) > config.max_string_length:
        raise BrokerError("request string is invalid")
    return value


def _optional_string(value: object, config: BrokerConfig) -> str | None:
    if value is None:
        return None
    return _string(value, config)


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BrokerError("request object is invalid")
    return value


def _exact_fields(value: dict[str, object], fields: set[str]) -> None:
    if set(value) != fields:
        raise BrokerError("request has unexpected fields")


def _parse_job(value: object, config: BrokerConfig) -> BrokerJob:
    job = _object(value)
    fields = {
        "job_id", "org_id", "account_id", "transcript_id", "requester_id",
        "opportunity_id", "skill", "skill_version", "requested_model",
        "requested_runtime_version", "mode", "attempt_number",
        "input_manifest", "allowlist", "execution_deadline",
        "input_workspace", "output_workspace", "attempt_id", "proxy_token",
        "proxy_uds_path",
    }
    _exact_fields(job, fields)
    manifest = _object(job["input_manifest"])
    _exact_fields(
        manifest,
        {"transcript_id", "transcript_ref", "account_id", "org_id",
         "opportunity_id", "prior_context_refs"},
    )
    allowlist = _object(job["allowlist"])
    _exact_fields(allowlist, {"tools", "network"})
    tools = allowlist["tools"]
    network = allowlist["network"]
    if not isinstance(tools, list) or not all(
        isinstance(item, str) for item in tools
    ):
        raise BrokerError("request tools are invalid")
    if not isinstance(network, list):
        raise BrokerError("request network is invalid")
    for destination_value in network:
        destination = _object(destination_value)
        _exact_fields(destination, {"host", "port", "scheme", "path_prefix"})
        _string(destination["host"], config)
        if destination["port"] is not None and (
            not isinstance(destination["port"], int)
            or isinstance(destination["port"], bool)
            or not 1 <= destination["port"] <= 65535
        ):
            raise BrokerError("request network port is invalid")
        if destination["scheme"] not in {"http", "https"}:
            raise BrokerError("request network scheme is invalid")
        _string(destination["path_prefix"], config)
    for key in (
        "job_id", "org_id", "account_id", "transcript_id", "requester_id",
        "skill", "skill_version", "requested_model", "execution_deadline",
        "input_workspace", "output_workspace", "attempt_id",
    ):
        _string(job[key], config)
    for key in ("opportunity_id", "requested_runtime_version", "proxy_token", "proxy_uds_path"):
        _optional_string(job[key], config)
    if job["mode"] not in {"full", "brief"}:
        raise BrokerError("request mode is invalid")
    if (
        not isinstance(job["attempt_number"], int)
        or isinstance(job["attempt_number"], bool)
        or job["attempt_number"] < 0
    ):
        raise BrokerError("request attempt number is invalid")
    _parse_deadline(str(job["execution_deadline"]), config)
    return BrokerJob(job)


def _parse_deadline(value: str, config: BrokerConfig) -> datetime:
    try:
        deadline = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError as exc:
        raise BrokerError("invalid execution deadline") from exc
    if deadline.tzinfo is None:
        raise BrokerError("invalid execution deadline")
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0 or remaining > config.max_attempt_duration_seconds:
        raise BrokerError("invalid execution deadline")
    return deadline


def _load_request(stream: TextIO, config: BrokerConfig) -> BrokerRequest:
    request = _read_request(stream, config)
    _check_bounded_value(request, config)
    operation = request.get("operation")
    if not isinstance(operation, str):
        raise BrokerError("unsupported operation")
    if operation == "run":
        _exact_fields(
            request,
            {"operation", "container_id", "input_dir", "output_dir",
             "proxy_uds_path", "job"},
        )
        return RunRequest(
            _validate_container_id(_string(request["container_id"], config)),
            _string(request["input_dir"], config),
            _string(request["output_dir"], config),
            _optional_string(request["proxy_uds_path"], config),
            _parse_job(request["job"], config),
        )
    if operation == "list":
        _exact_fields(request, {"operation", "container_id", "state_dir"})
        container_id = _validate_container_id(
            _string(request["container_id"], config)
        )
        state_dir = _string(request["state_dir"], config)
        return ListRequest(container_id, state_dir)
    if operation == "finalize":
        _exact_fields(request, {"operation", "container_id"})
        return FinalizeRequest(
            _validate_container_id(_string(request["container_id"], config))
        )
    if operation == "cleanup":
        _exact_fields(request, {"operation"})
        return CleanupRequest()
    raise BrokerError("unsupported operation")


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


def _worker_workspace_root(path: Path, worker_gid: int) -> None:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise BrokerError("worker workspace root is unavailable") from exc
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISDIR(facts.st_mode)
        or facts.st_uid != 0
        or facts.st_gid != worker_gid
        or facts.st_mode & 0o007
        or facts.st_mode & 0o020 == 0
    ):
        raise BrokerError("worker workspace root is unsafe")


def _journal_path(config: BrokerConfig, container_id: str) -> Path:
    return config.journal_root / f"{container_id}.json"


def _write_journal(config: BrokerConfig, container_id: str, record: dict[str, object]) -> None:
    config.journal_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _root_directory(config.journal_root)
    target = _journal_path(config, container_id)
    temporary = target.with_suffix(".tmp")
    payload = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    directory_fd = os.open(config.journal_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    if config.journal_phase_pause_seconds:
        time.sleep(config.journal_phase_pause_seconds)


def _remove_journal(config: BrokerConfig, container_id: str) -> None:
    try:
        _journal_path(config, container_id).unlink()
    except FileNotFoundError:
        return


def _process_identity(pid: int) -> dict[str, int] | None:
    """Read Linux process identity, including `/proc/<pid>/stat` start time.

    Linux documents `starttime` as field 22 of `/proc/<pid>/stat`; because the
    comm field may contain spaces or closing parentheses, parsing starts after
    its final closing parenthesis.  The remaining field 3 is therefore index
    0 and starttime is index 19.
    """
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = stat_text[stat_text.rfind(")") + 2 :].split()
        if len(fields) <= 19:
            return None
        state = fields[0]
        if state == "Z":
            return None
        return {
            "pid": pid,
            "pgid": int(fields[2]),
            "start_time_ticks": int(fields[19]),
        }
    except (OSError, IndexError, ValueError):
        return None


def _process_exists(pid: int) -> bool:
    return _process_identity(pid) is not None


def _identity_matches(
    identity: dict[str, int],
    expected: dict[str, int],
) -> bool:
    return (
        identity.get("pid") == expected.get("pid")
        and identity.get("pgid") == expected.get("pgid")
        and identity.get("start_time_ticks") == expected.get("start_time_ticks")
    )


def _signal_owned_process(
    identity: dict[str, int],
    signum: signal.Signals,
) -> bool:
    """Signal only a process group whose leader identity still matches."""
    current = _process_identity(identity["pid"])
    if current is not None and _identity_matches(current, identity):
        try:
            os.killpg(identity["pgid"], signum)
            return True
        except OSError:
            return False
    members = identity.get("members")
    if isinstance(members, dict):
        signaled = False
        for pid_text, start_time in members.items():
            try:
                pid = int(pid_text)
                expected = {
                    "pid": pid,
                    "pgid": identity["pgid"],
                    "start_time_ticks": int(start_time),
                }
            except (TypeError, ValueError):
                continue
            member = _process_identity(pid)
            if member is not None and _identity_matches(member, expected):
                try:
                    os.kill(pid, signum)
                    signaled = True
                except OSError:
                    pass
        return signaled
    return False


def _group_members(pgid: int) -> list[int]:
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        identity = _process_identity(int(entry.name))
        if identity is not None and identity["pgid"] == pgid:
            members.append(identity["pid"])
    return members


def _terminate_owned_identity(
    identity: dict[str, int],
    timeout: float,
) -> bool:
    """Terminate an attempt group only while its recorded identity matches."""
    current = _process_identity(identity["pid"])
    if current is None:
        if not _group_members(identity["pgid"]):
            return True
        _signal_owned_process(identity, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while _group_members(identity["pgid"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        if not _group_members(identity["pgid"]):
            return True
        _signal_owned_process(identity, signal.SIGKILL)
        deadline = time.monotonic() + timeout
        while _group_members(identity["pgid"]) and time.monotonic() < deadline:
            time.sleep(0.01)
        return not _group_members(identity["pgid"])
    if not _identity_matches(current, identity):
        raise BrokerError("sandbox process identity unverifiable")
    try:
        os.killpg(identity["pgid"], signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while _group_members(identity["pgid"]) and time.monotonic() < deadline:
        time.sleep(0.01)
    if not _group_members(identity["pgid"]):
        return True
    current = _process_identity(identity["pid"])
    if current is None or not _identity_matches(current, identity):
        raise BrokerError("sandbox process group cleanup unverifiable")
    try:
        os.killpg(identity["pgid"], signal.SIGKILL)
    except OSError:
        pass
    deadline = time.monotonic() + timeout
    while _group_members(identity["pgid"]) and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _group_members(identity["pgid"])


def _terminate_owned_process(
    process: subprocess.Popen[bytes],
    identity: dict[str, int],
    timeout: float,
) -> bool:
    if process.poll() is not None:
        return True
    _signal_owned_process(identity, signal.SIGTERM)
    try:
        process.wait(timeout=timeout)
        return not _group_members(identity["pgid"])
    except subprocess.TimeoutExpired:
        _signal_owned_process(identity, signal.SIGKILL)
        try:
            process.wait(timeout=timeout)
            return not _group_members(identity["pgid"])
        except subprocess.TimeoutExpired:
            return False


def _wait_process_exit(pid: int, timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while _process_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    return not _process_exists(pid)


def _discard_worker_directory(
    path: Path,
    config: BrokerConfig,
    *,
    prefix: str,
    recreate: bool = True,
) -> None:
    if path.parent != config.workspace_root or not path.name.startswith(prefix):
        return
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    if not recreate:
        return
    path.mkdir(mode=0o770)
    os.chmod(path, 0o770)
    os.chown(path, config.worker_uid, config.sandbox_gid)


def _discard_worker_socket(
    path: Path,
    config: BrokerConfig,
) -> None:
    if (
        path.parent.parent != config.workspace_root
        or not path.parent.name.startswith("se-proxy-")
    ):
        return
    if path.is_socket() or path.is_symlink():
        path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass


def _operation_lock(config: BrokerConfig) -> object:
    config.journal_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    _root_directory(config.journal_root)
    for index in range(config.max_concurrent_operations):
        stream = (config.journal_root / f".operation-{index}.lock").open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return stream
        except OSError:
            stream.close()
    raise BrokerError("broker operation capacity exhausted")


def _run_command(
    command: list[str],
    config: BrokerConfig,
    *,
    capture_stdout: bool,
    on_start: object | None = None,
) -> tuple[int, bytes]:
    global _ACTIVE_PROCESS, _ACTIVE_IDENTITY
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        identity = _process_identity(process.pid)
        if identity is None:
            if process.poll() is not None:
                stdout, _ = process.communicate()
                return process.returncode or 0, stdout or b""
            raise BrokerError("broker operation identity unavailable")
        _ACTIVE_PROCESS = process
        _ACTIVE_IDENTITY = identity
        if on_start is not None:
            on_start(identity)
    except OSError as exc:
        raise BrokerError("broker operation unavailable") from exc
    try:
        stdout, _ = process.communicate(timeout=config.operation_timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        if not _terminate_owned_process(process, identity, 1.0):
            _ACTIVE_PROCESS = None
            _ACTIVE_IDENTITY = None
            raise BrokerError("broker operation cleanup unverifiable") from exc
        _ACTIVE_PROCESS = None
        _ACTIVE_IDENTITY = None
        raise BrokerError("broker operation timed out") from exc
    except BaseException:
        _ACTIVE_PROCESS = None
        _ACTIVE_IDENTITY = None
        raise
    _ACTIVE_PROCESS = None
    _ACTIVE_IDENTITY = None
    return process.returncode, stdout or b""


def _make_tree_read_only(path: Path, config: BrokerConfig) -> None:
    for child in path.rglob("*"):
        if child.is_symlink():
            raise BrokerError("worker workspace contains a symlink")
        facts = child.lstat()
        os.chown(child, config.sandbox_uid, config.sandbox_gid)
        if child.is_dir():
            os.chmod(child, 0o555)
        elif child.is_file():
            os.chmod(child, 0o444)
        else:
            raise BrokerError("worker workspace contains a special file")
    os.chown(path, config.sandbox_uid, config.sandbox_gid)
    os.chmod(path, 0o555)


def _make_output_writable(path: Path, config: BrokerConfig) -> None:
    for child in path.rglob("*"):
        if child.is_symlink():
            raise BrokerError("worker output contains a symlink")
        facts = child.lstat()
        if child.is_dir():
            os.chown(child, config.worker_uid, config.sandbox_gid)
            os.chmod(child, 0o770)
        elif child.is_file():
            os.chown(child, config.worker_uid, config.sandbox_gid)
            os.chmod(child, 0o660)
        else:
            raise BrokerError("worker output contains a special file")
    os.chown(path, config.worker_uid, config.sandbox_gid)
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
        config.journal_root,
    ):
        _root_directory(root)
    _worker_workspace_root(config.workspace_root, config.worker_gid)
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
    staged_input = container_dir / "input"
    staged_output = container_dir / "output"
    staged_proxy = container_dir / "proxy.sock"
    job_path = bundle_dir / "job.json"
    config_path = bundle_dir / "config.json"
    journal = {
        "container_id": request.container_id,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "proxy_uds_path": str(proxy_uds_path) if proxy_uds_path else None,
        "staging_dir": str(container_dir),
        "staged_input": str(staged_input),
        "staged_output": str(staged_output),
        "staged_proxy": str(staged_proxy),
        "state_dir": str(state_dir),
        "bundle_dir": str(bundle_dir),
        "phase": "prepared",
        "output_terminal": "discard",
    }
    _write_journal(config, request.container_id, journal)
    container_dir.mkdir(mode=0o700)
    bundle_dir.mkdir(mode=0o700)
    state_dir.mkdir(mode=0o700)
    os.rename(input_dir, staged_input)
    journal["phase"] = "input-sealed"
    _write_journal(config, request.container_id, journal)
    os.rename(output_dir, staged_output)
    journal["phase"] = "output-sealed"
    _write_journal(config, request.container_id, journal)
    for staged_path in (staged_input, staged_output):
        facts = staged_path.lstat()
        if stat.S_ISLNK(facts.st_mode) or not stat.S_ISDIR(facts.st_mode):
            raise BrokerError("worker workspace sealing failed")
    output_dir.mkdir(mode=0o770)
    os.chmod(output_dir, 0o770)
    os.chown(output_dir, config.worker_uid, config.sandbox_gid)
    _make_tree_read_only(staged_input, config)
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
        journal["phase"] = "proxy-sealed"
        _write_journal(config, request.container_id, journal)
    job_path.write_text(
        json.dumps(request.job.values, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.chown(job_path, config.sandbox_uid, config.sandbox_gid)
    os.chmod(job_path, 0o444)
    deadline = _parse_deadline(str(request.job["execution_deadline"]), config)
    remaining = max((deadline - datetime.now(timezone.utc)).total_seconds(), 1.0)
    cpu_limit_seconds = max(1, math.ceil(remaining))
    config_path.write_text(
        json.dumps(
            _fixed_config(
                config, state_dir, bundle_dir, job_path, staged_input,
                staged_output, staged_proxy if proxy_uds_path else None,
                request.container_id, cpu_limit_seconds,
            ),
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chown(config_path, 0, 0)
    os.chmod(config_path, 0o400)
    journal["phase"] = "configured"
    _write_journal(config, request.container_id, journal)
    returncode, _ = _run_command(
        [
            str(config.runsc), f"--root={state_dir}", "--network=none",
            "run", "--bundle", str(bundle_dir), request.container_id,
        ],
        config,
        capture_stdout=False,
        on_start=lambda identity: (
            journal.update({
                "phase": "running",
                "runsc_pid": identity["pid"],
                "runsc_pgid": identity["pgid"],
                "runsc_start_time_ticks": identity["start_time_ticks"],
                "runsc_members": {
                    str(pid): member["start_time_ticks"]
                    for pid in _group_members(identity["pgid"])
                    if (member := _process_identity(pid)) is not None
                },
            }),
            _write_journal(config, request.container_id, journal),
        ),
    )
    journal["phase"] = "run-finished" if returncode == 0 else "run-failed"
    journal["output_terminal"] = "restore" if returncode == 0 else "discard"
    _write_journal(config, request.container_id, journal)
    return returncode


def _run_list(
    config: BrokerConfig,
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
    command = [str(config.runsc), f"--root={expected}", "list", "--format=text"]
    returncode, stdout = _run_command(command, config, capture_stdout=True)
    sys.stdout.buffer.write(stdout)
    return returncode


def _load_journal(config: BrokerConfig, container_id: str) -> dict[str, object]:
    path = _journal_path(config, container_id)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BrokerError("lifecycle journal unavailable") from exc
    if not isinstance(record, dict):
        raise BrokerError("lifecycle journal is invalid")
    if record.get("container_id") != container_id:
        raise BrokerError("lifecycle journal identity mismatch")
    expected = {
        "staging_dir": config.staging_root / container_id,
        "state_dir": config.state_root / container_id,
        "bundle_dir": config.bundle_root / container_id,
    }
    for field, path in expected.items():
        if record.get(field) != str(path):
            raise BrokerError("lifecycle journal path mismatch")
    return record


def _runsc_container_absent(
    config: BrokerConfig,
    state_dir: Path,
    container_id: str,
) -> bool:
    code, output = _run_command(
        [str(config.runsc), f"--root={state_dir}", "list", "--format=text"],
        config,
        capture_stdout=True,
    )
    if code != 0:
        raise BrokerError("sandbox state verification failed")
    lines = output.decode("utf-8", errors="replace").splitlines()
    if not lines or not lines[0].startswith("ID"):
        raise BrokerError("sandbox state verification failed")
    return not any(line.split()[:1] == [container_id] for line in lines[1:])


def _finalize_container(config: BrokerConfig, container_id: str) -> int:
    """Release one attempt only after process and container absence are proven."""
    _root_directory(config.state_root)
    _root_directory(config.bundle_root)
    _root_directory(config.staging_root)
    _root_directory(config.journal_root)
    _root_executable(config.runsc)
    record = _load_journal(config, container_id)
    pid = record.get("runsc_pid")
    start_ticks = record.get("runsc_start_time_ticks")
    pgid = record.get("runsc_pgid")
    if (
        isinstance(pid, int)
        and isinstance(start_ticks, int)
        and isinstance(pgid, int)
    ):
        identity = {
            "pid": pid,
            "pgid": pgid,
            "start_time_ticks": start_ticks,
        }
        members = record.get("runsc_members")
        if isinstance(members, dict):
            identity["members"] = members
        current = _process_identity(pid)
        if current is not None and _identity_matches(current, identity):
            if not _terminate_owned_identity(identity, 1.0):
                raise BrokerError("sandbox process cleanup unverifiable")
        elif current is not None:
            raise BrokerError("sandbox process identity unverifiable")
    state = config.state_root / container_id
    if state.exists() and not _runsc_container_absent(config, state, container_id):
        code, _ = _run_command(
            [str(config.runsc), f"--root={state}", "delete", "--force", container_id],
            config,
            capture_stdout=False,
        )
        if code != 0 or not _runsc_container_absent(config, state, container_id):
            raise BrokerError("sandbox remained after finalize")
    if state.exists():
        shutil.rmtree(state)
    staging = config.staging_root / container_id
    bundle = config.bundle_root / container_id
    input_path = Path(str(record["input_dir"]))
    output = Path(str(record["output_dir"]))
    proxy_value = record.get("proxy_uds_path")
    proxy_path = Path(proxy_value) if isinstance(proxy_value, str) and proxy_value else None
    staged_input = staging / "input"
    staged_output = staging / "output"
    staged_proxy = staging / "proxy.sock"
    if record.get("output_terminal") == "restore" and staged_output.exists():
        if output.exists():
            shutil.rmtree(output)
        os.rename(staged_output, output)
        _make_output_writable(output, config)
    else:
        _discard_worker_directory(output, config, prefix="se-runtime-output-")
        if staged_output.exists():
            shutil.rmtree(staged_output)
    _discard_worker_directory(
        input_path, config, prefix="se-runtime-input-", recreate=False
    )
    if staged_input.exists():
        shutil.rmtree(staged_input)
    if proxy_path is not None:
        _discard_worker_socket(proxy_path, config)
    if staged_proxy.exists():
        staged_proxy.unlink()
    if staging.exists():
        shutil.rmtree(staging)
    if bundle.exists():
        shutil.rmtree(bundle)
    if state.exists() or staging.exists() or bundle.exists():
        raise BrokerError("sandbox custody reconciliation incomplete")
    _remove_journal(config, container_id)
    return 0


def _safe_reclaim_directory(path: Path) -> None:
    try:
        facts = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise BrokerError("stale custody path is unavailable") from exc
    if stat.S_ISLNK(facts.st_mode):
        raise BrokerError("stale custody path is unsafe")
    _root_directory(path)


def _safe_journal_file(path: Path) -> None:
    if not path.exists():
        return
    try:
        facts = path.lstat()
    except OSError as exc:
        raise BrokerError("lifecycle journal unavailable") from exc
    if (
        stat.S_ISLNK(facts.st_mode)
        or not stat.S_ISREG(facts.st_mode)
        or facts.st_uid != 0
        or facts.st_mode & 0o022
    ):
        raise BrokerError("lifecycle journal is unsafe")


def _reclaim_without_journal(
    config: BrokerConfig,
    container_id: str,
    journal_path: Path | None,
    cutoff: float,
) -> None:
    """Reclaim only root-owned residue with no trustworthy custody record."""
    state = config.state_root / container_id
    bundle = config.bundle_root / container_id
    staging = config.staging_root / container_id
    _safe_reclaim_directory(state)
    _safe_reclaim_directory(bundle)
    _safe_reclaim_directory(staging)
    if journal_path is not None:
        _safe_journal_file(journal_path)

    if not any(path.exists() for path in (state, bundle, staging)):
        return
    candidates = [state, bundle, staging]
    if journal_path is not None and journal_path.exists():
        candidates.append(journal_path)
    for path in candidates:
        if path.exists() and path.stat().st_mtime > cutoff:
            return
    if not _runsc_container_absent(config, state, container_id):
        raise BrokerError("sandbox remains during stale reconciliation")

    if bundle.exists():
        shutil.rmtree(bundle)
    if staging.exists():
        shutil.rmtree(staging)
    if state.exists():
        shutil.rmtree(state)
    if journal_path is not None and journal_path.exists():
        journal_path.unlink()


def _run_cleanup(config: BrokerConfig) -> int:
    """Reconcile old attempts through finalize or safe residue reclamation."""
    _root_directory(config.journal_root)
    _root_directory(config.state_root)
    _root_directory(config.bundle_root)
    _root_directory(config.staging_root)
    now = time.time()
    cutoff = now - config.cleanup_min_age_seconds
    candidates: set[str] = set()
    for root in (config.state_root, config.bundle_root, config.staging_root):
        try:
            entries = tuple(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                candidates.add(_validate_container_id(entry.name))
            except BrokerError:
                continue
    try:
        journal_entries = tuple(config.journal_root.glob("se-*.json"))
    except OSError:
        journal_entries = ()
    for journal_path in journal_entries:
        if journal_path.is_symlink():
            continue
        try:
            candidates.add(_validate_container_id(journal_path.stem))
        except BrokerError:
            continue

    for container_id in sorted(candidates):
        journal_path = _journal_path(config, container_id)
        journal_present = journal_path.exists() or journal_path.is_symlink()
        if journal_present:
            try:
                _safe_journal_file(journal_path)
                if journal_path.stat().st_mtime > cutoff:
                    continue
            except (BrokerError, OSError, ValueError, TypeError):
                continue
            try:
                _load_journal(config, container_id)
            except (BrokerError, OSError, ValueError, TypeError):
                pass
            else:
                try:
                    _finalize_container(config, container_id)
                except (BrokerError, OSError, ValueError, TypeError):
                    pass
                continue
        try:
            _reclaim_without_journal(
                config,
                container_id,
                journal_path if journal_present else None,
                cutoff,
            )
        except (BrokerError, OSError, ValueError, TypeError):
            continue
    return 0


def main() -> int:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        if len(sys.argv) != 1:
            raise BrokerError("invalid broker invocation")
        config = BrokerConfig.load()
        lock = _operation_lock(config)
        try:
            request = _load_request(sys.stdin, config)
            if isinstance(request, RunRequest):
                return _run(config, request)
            if isinstance(request, CleanupRequest):
                return _run_cleanup(config)
            if isinstance(request, ListRequest):
                return _run_list(
                    config,
                    Path(request.state_dir),
                    request.container_id,
                )
            return _finalize_container(config, request.container_id)
        finally:
            lock.close()
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
