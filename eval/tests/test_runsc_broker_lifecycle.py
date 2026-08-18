"""Real-signal custody tests for the root broker."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[2]
BROKER = ROOT / "scripts/runsc_broker.py"


def _unshare_available() -> bool:
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
    return result.returncode == 0


pytestmark = pytest.mark.skipif(
    not _unshare_available(),
    reason="user namespaces unavailable: unshare --map-root-user --mount failed",
)


@pytest.mark.parametrize("phase", ("prepared", "input-sealed", "output-sealed", "proxy-sealed", "configured", "running"))
@pytest.mark.parametrize("signum", (signal.SIGTERM, signal.SIGKILL))
@pytest.mark.parametrize("hung", (False, True))
def test_real_signal_reconciles_every_custody_phase(
    tmp_path: Path,
    phase: str,
    signum: signal.Signals,
    hung: bool,
) -> None:
    child = tmp_path / "lifecycle_child.py"
    child.write_text(
        _child_script(
            BROKER,
            tmp_path / "lifecycle-runtime",
            phase,
            int(signum),
            hung,
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "unshare",
            "--map-root-user",
            "--mount",
            "--fork",
            sys.executable,
            str(child),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def _child_script(
    broker: Path,
    runtime_root: Path,
    phase: str,
    signum: int,
    hung: bool,
) -> str:
    return f"""
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

root = Path("/tmp") / ("se-lifecycle-" + str(os.getpid()))
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
runsc = root / "runsc"
marker = root / "runsc.pid"
runsc.write_text(
    "#!/bin/sh\\n"
    "if [ \\"$3\\" = run ]; then echo $$ > " + str(marker) + "; sleep {600 if hung else 2}; "
    "elif [ \\"$2\\" = list ]; then printf 'ID\\\\tPID\\\\tSTATUS\\\\n'; "
    "elif [ \\"$2\\" = delete ]; then exit 0; fi\\n",
    encoding="utf-8",
)
runsc.chmod(0o755)
container_id = "se-" + "b" * 12
input_dir = workspace / "se-runtime-input-attempt"
output_dir = workspace / "se-runtime-output-attempt"
input_dir.mkdir(mode=0o770)
output_dir.mkdir(mode=0o770)
(input_dir / "transcript.txt").write_text("customer-transcript", encoding="utf-8")
proxy_dir = workspace / "se-proxy-attempt"
proxy_dir.mkdir(mode=0o700)
proxy_path = proxy_dir / "proxy.sock"
listener = socket.socket(socket.AF_UNIX)
listener.bind(str(proxy_path))
config_path = root / "broker.json"
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
    "operation_timeout_seconds": 2,
    "journal_root": str(journal),
    "journal_phase_pause_seconds": 0.05,
}}), encoding="utf-8")
etc_root = root / "etc"
etc_root.mkdir()
etc_dir = etc_root / "se-skills"
etc_dir.mkdir()
(etc_dir / "runsc-broker.json").write_text(config_path.read_text(), encoding="utf-8")
subprocess.run(["mount", "--bind", str(etc_root), "/etc"], check=True)
job = {{
    "job_id": "job-1",
    "org_id": "org-1",
    "account_id": "account-1",
    "transcript_id": "transcript-1",
    "requester_id": "requester-1",
    "opportunity_id": None,
    "skill": "post-call",
    "skill_version": "1.0",
    "requested_model": "claude",
    "requested_runtime_version": "1.0",
    "mode": "full",
    "attempt_number": 1,
    "input_manifest": {{
        "transcript_id": "transcript-1",
        "transcript_ref": "ref-1",
        "account_id": "account-1",
        "org_id": "org-1",
        "opportunity_id": None,
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
    "proxy_uds_path": str(proxy_path),
}}
request = {{
    "operation": "run",
    "container_id": container_id,
    "input_dir": str(input_dir),
    "output_dir": str(output_dir),
    "proxy_uds_path": str(proxy_path),
    "job": job,
}}
broker = [{str(sys.executable)!r}, "-I", {str(broker)!r}]
proc = subprocess.Popen(
    broker,
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env={{"PATH": "/usr/bin:/bin"}},
)
proc.stdin.write((json.dumps(request) + "\\n").encode())
proc.stdin.close()
journal_path = journal / (container_id + ".json")
deadline = time.time() + 8
while time.time() < deadline:
    if journal_path.exists():
        record = json.loads(journal_path.read_text())
        if record.get("phase") == {phase!r}:
            break
    time.sleep(0.01)
else:
    raise AssertionError(
        "phase not reached: "
        + {phase!r}
        + " stderr="
        + proc.stderr.read().decode()
    )
proc.send_signal({signum})
proc.wait(timeout=8)
if proc.returncode is None:
    raise AssertionError("broker did not terminate")
record_snapshot = journal_path.read_text() if journal_path.exists() else "missing"
cleanup_request = json.dumps({{"operation": "cleanup", "minimum_age_seconds": 0}}) + "\\n"
cleanup = subprocess.run(
    broker,
    input=cleanup_request.encode(),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    env={{"PATH": "/usr/bin:/bin"}},
    check=False,
    timeout=8,
)
assert cleanup.returncode == 0, cleanup.stderr.decode()
assert not journal_path.exists()
assert not list(state.iterdir())
assert not list(bundles.iterdir())
assert not list(staging.iterdir())
assert not proxy_path.exists()
assert output_dir.exists() and not list(output_dir.iterdir())
assert not input_dir.exists(), record_snapshot
if marker.exists():
    pid = int(marker.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        raise AssertionError("fake runsc survived cleanup")
listener.close()
"""
