"""Real-signal custody tests through `RunscSandboxRunner` and the broker."""
from __future__ import annotations

import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
BROKER = ROOT / "scripts/runsc_broker.py"
EXPECTED_LIFECYCLE_CASES = 29


def _lifecycle_command_prefix() -> tuple[str, ...] | None:
    if shutil.which("unshare") is not None:
        result = subprocess.run(
            [
                "unshare",
                "--map-root-user",
                "--mount",
                "--fork",
                sys.executable,
                "-c",
                "import os; assert os.getuid() == 0",
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return ("unshare", "--map-root-user", "--mount", "--fork")
    if shutil.which("sudo") is not None:
        result = subprocess.run(
            ["sudo", "--non-interactive", "unshare", "--mount", "--fork", "true"],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return ("sudo", "--non-interactive", "unshare", "--mount", "--fork")
    return None


_LIFECYCLE_COMMAND_PREFIX = _lifecycle_command_prefix()


def _skip_reason() -> str:
    namespace = (
        "unshare is unavailable"
        if shutil.which("unshare") is None
        else "unshare --map-root-user --mount user namespace is unavailable"
    )
    sudo = (
        "sudo is unavailable"
        if shutil.which("sudo") is None
        else "passwordless sudo -n root fallback is unavailable"
    )
    return f"{namespace}; {sudo}"


pytestmark = pytest.mark.skipif(
    _LIFECYCLE_COMMAND_PREFIX is None,
    reason=_skip_reason(),
)


@pytest.mark.parametrize(
    "phase",
    (
        "prepared",
        "input-sealed",
        "output-sealed",
        "proxy-sealed",
        "configured",
        "running",
    ),
)
@pytest.mark.parametrize("signum", (signal.SIGTERM, signal.SIGKILL))
@pytest.mark.parametrize("hung", (False, True))
def test_real_runner_finalizes_after_broker_kill(
    tmp_path: Path,
    phase: str,
    signum: signal.Signals,
    hung: bool,
) -> None:
    _run_real_runner_case(tmp_path, phase=phase, signum=int(signum), hung=hung)


def test_real_runner_normal_success_releases_capability_material(
    tmp_path: Path,
) -> None:
    _run_real_runner_case(tmp_path, phase=None, signum=None, hung=False)


def test_real_runner_rejects_raced_output_publication(
    tmp_path: Path,
) -> None:
    _run_real_runner_case(
        tmp_path,
        phase=None,
        signum=None,
        hung=False,
        hostile_output=True,
        expect_failure=True,
    )


def test_real_runner_aborts_child_when_identity_journal_fails(
    tmp_path: Path,
) -> None:
    _run_real_runner_case(
        tmp_path,
        phase=None,
        signum=None,
        hung=False,
        fail_on_start=True,
    )


def test_sigterm_escalates_for_sigterm_ignoring_runsc(tmp_path: Path) -> None:
    _run_real_runner_case(
        tmp_path,
        phase="running",
        signum=signal.SIGTERM,
        hung=True,
    )


def test_pid_reuse_never_signals_unrelated_group(tmp_path: Path) -> None:
    child = tmp_path / "pid_reuse.py"
    child.write_text(_pid_reuse_script(BROKER), encoding="utf-8")
    assert _LIFECYCLE_COMMAND_PREFIX is not None
    result = subprocess.run(
        [*_LIFECYCLE_COMMAND_PREFIX, sys.executable, str(child)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _run_real_runner_case(
    tmp_path: Path,
    *,
    phase: str | None,
    signum: int | None,
    hung: bool,
    hostile_output: bool = False,
    expect_failure: bool = False,
    fail_on_start: bool = False,
) -> None:
    child = tmp_path / "runner_case.py"
    child.write_text(
        _runner_case_script(
            BROKER,
            phase,
            signum,
            hung,
            hostile_output=hostile_output,
            expect_failure=expect_failure,
            fail_on_start=fail_on_start,
        ),
        encoding="utf-8",
    )
    assert _LIFECYCLE_COMMAND_PREFIX is not None
    result = subprocess.run(
        [*_LIFECYCLE_COMMAND_PREFIX, sys.executable, str(child)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _runner_case_script(
    broker: Path,
    phase: str | None,
    signum: int | None,
    hung: bool,
    *,
    hostile_output: bool,
    expect_failure: bool,
    fail_on_start: bool,
) -> str:
    restore_output = phase is None
    kill_block = ""
    special_setup = ""
    special_async = ""
    special_main = ""
    special_cleanup = "        pass"
    special_assert = ""
    normal_assertions = ""
    if phase is not None and signum is not None:
        kill_block = f"""
    deadline = time.time() + 8
    while time.time() < deadline:
        journal_paths = list(journal.glob("se-*.json"))
        if journal_paths:
            journal_path = journal_paths[0]
            record = json.loads(journal_path.read_text())
            if record.get("phase") == {phase!r}:
                os.kill(int(broker_marker.read_text()), {signum})
                break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("custody phase was not reached")
"""
    if hostile_output:
        special_setup = """
sentinel = root / "sentinel.txt"
sentinel.write_text("sentinel", encoding="utf-8")
os.chmod(sentinel, 0o640)
sentinel_before = sentinel.stat()
attack_stop = root / "attack.stop"
"""
        special_async = """
async def watch_output_phase():
    deadline = time.time() + 8
    while time.time() < deadline:
        journal_paths = list(journal.glob("se-*.json"))
        if journal_paths:
            record = json.loads(journal_paths[0].read_text())
            if record.get("phase") == "output-sealed":
                attacker_code = (
                    "import pathlib, shutil, sys, time\\n"
                    "workspace = pathlib.Path(sys.argv[1])\\n"
                    "sentinel = pathlib.Path(sys.argv[2])\\n"
                    "stop = pathlib.Path(sys.argv[3])\\n"
                    "output = workspace / 'se-runtime-output-attempt'\\n"
                    "while not stop.exists():\\n"
                    "    try:\\n"
                    "        if output.is_symlink():\\n"
                    "            output.unlink()\\n"
                    "        elif output.exists():\\n"
                    "            shutil.rmtree(output)\\n"
                    "        if time.time_ns() % 2:\\n"
                    "            output.mkdir()\\n"
                    "            (output / 'nested').symlink_to(sentinel)\\n"
                    "        else:\\n"
                    "            output.symlink_to(sentinel)\\n"
                    "    except (FileNotFoundError, OSError):\\n"
                    "        pass\\n"
                )
                return subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        attacker_code,
                        str(workspace),
                        str(sentinel),
                        str(attack_stop),
                    ]
                )
        await asyncio.sleep(0.01)
    raise AssertionError("output sealing phase was not reached")
"""
        special_main = """
    attack_task = asyncio.create_task(watch_output_phase())
"""
        special_cleanup = """
        if not attack_task.done():
            attack_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await attack_task
        else:
            attacker = attack_task.result()
            attack_stop.write_text("stop", encoding="utf-8")
            attacker.wait(timeout=5)
"""
        special_assert = """
assert list(journal.glob("se-*.json"))
sentinel_after = sentinel.stat()
assert (
    sentinel_before.st_uid,
    sentinel_before.st_gid,
    sentinel_before.st_mode,
) == (
    sentinel_after.st_uid,
    sentinel_after.st_gid,
    sentinel_after.st_mode,
)
"""
    elif fail_on_start:
        special_setup = """
fault_injected = False
"""
        special_async = """
async def fail_identity_journal():
    global fault_injected
    deadline = time.time() + 8
    while time.time() < deadline:
        journal_paths = list(journal.glob("se-*.json"))
        if journal_paths:
            record = json.loads(journal_paths[0].read_text())
            if record.get("phase") == "configured":
                journal.chmod(0o500)
                fault_injected = True
                await asyncio.sleep(0.25)
                journal.chmod(0o700)
                return
        await asyncio.sleep(0.01)
    raise AssertionError("configured phase was not reached")
"""
        special_main = """
    fault_task = asyncio.create_task(fail_identity_journal())
"""
        special_cleanup = """
        await fault_task
"""
        special_assert = """
assert fault_injected
"""
    if not hostile_output:
        normal_assertions = f"""
assert not list(state.iterdir())
assert not list(bundles.iterdir())
assert not list(staging.iterdir())
assert not list(journal.glob("se-*.json"))
assert not (workspace / "se-runtime-input-attempt").exists()
assert not (workspace / "se-proxy-attempt").exists()
assert (workspace / "se-runtime-output-attempt").exists()
assert (
    list((workspace / "se-runtime-output-attempt").iterdir())
    if {restore_output!r}
    else not list((workspace / "se-runtime-output-attempt").iterdir())
)
if marker.exists():
    try:
        os.kill(int(marker.read_text()), 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("fake runsc survived finalization")
"""
    return f"""
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0, {str(broker.parent.parent)!r})

from webapp.hosted import config as hosted_config
from webapp.hosted.runsc_executor import RunscSandboxRunner
from webapp.hosted.runtime_contract import RuntimeJob

root = Path("/tmp") / ("se-finalize-" + str(os.getpid()))
root.mkdir()
state = root / "state"
bundles = root / "bundles"
staging = root / "staging"
journal = root / "journal"
workspace = root / "workspace"
rootfs = root / "rootfs"
for path in (state, bundles, staging, journal, workspace, rootfs):
    path.mkdir()
workspace.chmod(0o730)
{special_setup}
runsc = root / "runsc"
marker = root / "runsc.pid"
runsc.write_text(
    "#!/bin/sh\\n"
    "if [ \\"$3\\" = run ]; then "
    + ("trap '' TERM; " if {hung!r} else "")
    + "echo $$ > " + str(marker)
    + "; sleep {600 if hung else (2 if (phase == "running" or hostile_output) else 0)}; exit 0; "
    "elif [ \\"$2\\" = list ]; then printf 'ID\\\\tPID\\\\tSTATUS\\\\n'; "
    "elif [ \\"$2\\" = delete ]; then exit 0; fi\\n",
    encoding="utf-8",
)
runsc.chmod(0o755)
sudo = root / "sudo"
broker_marker = root / "broker.pid"
sudo.write_text(
    "#!/bin/sh\\n"
    "echo $$ > " + str(broker_marker) + "\\n"
    "shift\\n"
    "exec \\"$@\\"\\n",
    encoding="utf-8",
)
sudo.chmod(0o755)
os.environ["PATH"] = str(root) + ":/usr/bin:/bin"
hosted_config.RUNSC_STATE_DIR = str(state)
hosted_config.RUNSC_BUNDLE_DIR = str(bundles)
hosted_config.RUNSC_WORKSPACE_ROOT = str(workspace)
input_dir = workspace / "se-runtime-input-attempt"
output_dir = workspace / "se-runtime-output-attempt"
input_dir.mkdir(mode=0o770)
output_dir.mkdir(mode=0o770)
(input_dir / "transcript.txt").write_text("customer-transcript", encoding="utf-8")
(output_dir / "preexisting.txt").write_text("output", encoding="utf-8")
proxy_dir = workspace / "se-proxy-attempt"
proxy_dir.mkdir(mode=0o700)
proxy_path = proxy_dir / "proxy.sock"
listener = socket.socket(socket.AF_UNIX)
listener.bind(str(proxy_path))
config_path = root / "runsc-broker.json"
config_path.write_text(json.dumps({{
    "runsc": str(runsc),
    "rootfs": str(rootfs),
    "state_root": str(state),
    "bundle_root": str(bundles),
    "staging_root": str(staging),
    "workspace_root": str(workspace),
    "worker_uid": 0,
    "worker_gid": 0,
    "sandbox_uid": 0,
    "sandbox_gid": 0,
    "cleanup_min_age_seconds": 1,
    "operation_timeout_seconds": 2,
    "journal_root": str(journal),
    "journal_phase_pause_seconds": 0.05,
}}), encoding="utf-8")
etc_root = root / "etc"
etc_root.mkdir()
(etc_root / "se-skills").mkdir()
(etc_root / "se-skills" / "runsc-broker.json").write_text(
    config_path.read_text(), encoding="utf-8"
)
subprocess.run(["mount", "--bind", str(etc_root), "/etc"], check=True)
container_id = "se-" + "b" * 12
job = RuntimeJob.model_validate({{
    "job_id": "00000000-0000-0000-0000-000000000001",
    "org_id": "00000000-0000-0000-0000-000000000002",
    "account_id": "00000000-0000-0000-0000-000000000003",
    "transcript_id": "00000000-0000-0000-0000-000000000004",
    "requester_id": "00000000-0000-0000-0000-000000000005",
    "skill": "post-call",
    "requested_model": "claude",
    "requested_runtime_version": "1.0",
    "input_manifest": {{
        "transcript_id": "00000000-0000-0000-0000-000000000004",
        "transcript_ref": "ref-1",
        "account_id": "00000000-0000-0000-0000-000000000003",
        "org_id": "00000000-0000-0000-0000-000000000002",
        "prior_context_refs": [],
    }},
    "allowlist": {{"tools": [], "network": []}},
    "execution_deadline": (
        datetime.now(timezone.utc) + timedelta(seconds=60)
    ).isoformat(),
    "input_workspace": "/runtime/input",
    "output_workspace": "/runtime/output",
    "attempt_id": "attempt-1",
    "proxy_token": "capability-secret",
    "proxy_uds_path": "/runtime/proxy.sock",
}})

class NeverCancelled:
    def is_cancelled(self):
        return False
    async def wait(self):
        await asyncio.Future()

{special_async}

async def main():
    runner = RunscSandboxRunner(
        runsc_binary=str(runsc),
        runsc_helper={str(broker)!r},
        rootfs=str(rootfs),
        rootless=False,
    )
    task = asyncio.create_task(
        runner.run(
            job,
            input_dir,
            output_dir,
            output_dir / "result.json",
            NeverCancelled(),
            proxy_uds_path=proxy_path,
        )
    )
{special_main}
{kill_block}
    try:
        await asyncio.wait_for(task, timeout=15)
    except Exception:
        if {phase is None!r} and not ({expect_failure!r} or {fail_on_start!r}):
            raise
    finally:
{special_cleanup}

asyncio.run(main())
{special_assert}
{normal_assertions}
listener.close()
"""


def _pid_reuse_script(broker: Path) -> str:
    return f"""
import json
import os
import signal
import subprocess
from pathlib import Path

root = Path("/tmp") / ("se-reuse-" + str(os.getpid()))
root.mkdir()
for name in ("state", "bundles", "staging", "journal", "workspace", "rootfs"):
    (root / name).mkdir()
workspace = root / "workspace"
workspace.chmod(0o730)
runsc = root / "runsc"
runsc.write_text("#!/bin/sh\\nprintf 'ID\\\\tPID\\\\tSTATUS\\\\n'\\n", encoding="utf-8")
runsc.chmod(0o755)
container_id = "se-" + "c" * 12
input_dir = workspace / "se-runtime-input-attempt"
output_dir = workspace / "se-runtime-output-attempt"
input_dir.mkdir()
output_dir.mkdir()
(input_dir / "secret").write_text("secret")
(root / "etc").mkdir()
(root / "etc" / "se-skills").mkdir()
config = {{
    "runsc": str(runsc),
    "rootfs": str(root / "rootfs"),
    "state_root": str(root / "state"),
    "bundle_root": str(root / "bundles"),
    "staging_root": str(root / "staging"),
    "workspace_root": str(workspace),
    "worker_uid": 0,
    "worker_gid": 0,
    "sandbox_uid": 0,
    "sandbox_gid": 0,
    "cleanup_min_age_seconds": 1,
    "journal_root": str(root / "journal"),
}}
(root / "etc" / "se-skills" / "runsc-broker.json").write_text(json.dumps(config))
subprocess.run(["mount", "--bind", str(root / "etc"), "/etc"], check=True)
unrelated = subprocess.Popen(["sleep", "30"], start_new_session=True)
journal = root / "journal" / (container_id + ".json")
journal.write_text(json.dumps({{
    "container_id": container_id,
    "input_dir": str(input_dir),
    "output_dir": str(output_dir),
    "proxy_uds_path": None,
    "staging_dir": str(root / "staging" / container_id),
    "state_dir": str(root / "state" / container_id),
    "bundle_dir": str(root / "bundles" / container_id),
    "phase": "running",
    "output_terminal": "discard",
    "runsc_pid": unrelated.pid,
    "runsc_pgid": os.getpgid(unrelated.pid),
    "runsc_start_time_ticks": 0,
}}))
subprocess.run(
    ["/usr/bin/python3", "-I", {str(broker)!r}],
    input=json.dumps({{"operation": "finalize", "container_id": container_id}}).encode() + b"\\n",
    env={{"PATH": "/usr/bin:/bin"}},
    check=False,
)
assert unrelated.poll() is None
assert journal.exists()
assert (input_dir / "secret").exists()
os.killpg(os.getpgid(unrelated.pid), signal.SIGKILL)
"""
