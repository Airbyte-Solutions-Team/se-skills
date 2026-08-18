"""Adversarial tests for the root-owned runsc broker contract."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "runsc_broker", Path(__file__).parents[2] / "scripts/runsc_broker.py"
)
assert _SPEC is not None and _SPEC.loader is not None
broker = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = broker
_SPEC.loader.exec_module(broker)


def test_request_rejects_unknown_fields() -> None:
    request = {
        "operation": "run",
        "container_id": "se-" + "a" * 12,
        "input_dir": "/tmp/se-runtime-input-test",
        "output_dir": "/tmp/se-runtime-output-test",
        "proxy_uds_path": None,
        "minimum_age_seconds": None,
        "job": {},
        "extra": "host-bind",
    }
    with pytest.raises(broker.BrokerError, match="unexpected"):
        broker._load_request(__import__("io").StringIO(json.dumps(request)))


def test_worker_paths_reject_escape_and_symlink(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(broker.BrokerError):
        broker._safe_worker_path(Path("/etc/passwd"), "se-runtime-input-")
    link = Path("/tmp/se-runtime-input-broker-test")
    try:
        link.symlink_to(outside, target_is_directory=True)
        with pytest.raises(broker.BrokerError):
            broker._safe_worker_path(link, "se-runtime-input-")
    finally:
        link.unlink(missing_ok=True)


def test_fixed_config_ignores_worker_rootfs_and_mount_fields(tmp_path: Path) -> None:
    config = broker.BrokerConfig(
        runsc=tmp_path / "runsc",
        rootfs=tmp_path / "broker-rootfs",
        state_root=tmp_path / "state",
        bundle_root=tmp_path / "bundles",
        staging_root=tmp_path / "staging",
        worker_uid=os.getuid(),
        worker_gid=os.getgid(),
    )
    document = broker._fixed_config(
        config,
        tmp_path / "state" / ("se-" + "a" * 12),
        tmp_path / "bundle",
        tmp_path / "job.json",
        tmp_path / "sealed-input",
        tmp_path / "sealed-output",
        None,
        "se-" + "a" * 12,
    )
    assert document["root"]["path"] == str(config.rootfs)
    assert document["process"]["args"] == [
        "/usr/bin/python3",
        "/app/webapp/hosted/runsc/sandbox_entry.py",
    ]
    assert document["process"]["env"] == [
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PYTHONPATH=/app/venv/lib/python3.11/site-packages:/app",
        "SE_RUNTIME_JOB_PATH=/runtime/job.json",
        "SE_RUNTIME_RESULT_PATH=/runtime/output/result.json",
    ]
    assert document["process"]["capabilities"] == {
        "bounding": [], "effective": [], "permitted": [],
        "inheritable": [], "ambient": [],
    }
    assert all(mount["source"] != "/etc/passwd" for mount in document["mounts"])


def test_fixed_config_has_no_devices_or_user_namespace(tmp_path: Path) -> None:
    config = broker.BrokerConfig(
        runsc=tmp_path / "runsc",
        rootfs=tmp_path / "rootfs",
        state_root=tmp_path / "state",
        bundle_root=tmp_path / "bundles",
        staging_root=tmp_path / "staging",
        worker_uid=995,
        worker_gid=995,
    )
    document = broker._fixed_config(
        config,
        tmp_path / "state",
        tmp_path / "bundle",
        tmp_path / "job",
        tmp_path / "input",
        tmp_path / "output",
        None,
        "se-" + "b" * 12,
    )
    assert "devices" not in document["linux"]
    assert {item["type"] for item in document["linux"]["namespaces"]} == {
        "pid", "network", "ipc", "uts", "mount",
    }


def test_cleanup_reclaims_verified_dead_but_not_live_or_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    bundles = tmp_path / "bundles"
    staging = tmp_path / "staging"
    for directory in (state, bundles, staging):
        directory.mkdir()
    for name in ("se-" + "a" * 12, "se-" + "b" * 12, "se-" + "c" * 12):
        (state / name).mkdir()
        (bundles / name).mkdir()
    runsc = tmp_path / "runsc"
    runsc.write_text(
        """#!/bin/sh
        root="${1#--root=}"
        case "$*" in
          *list*)
            case "$*" in
              *aaaaaaaaaaaa*) [ -e "$root/.deleted" ] && printf 'ID\tPID\tSTATUS\n' || printf 'ID\tPID\tSTATUS\nse-aaaaaaaaaaaa\t1\tstopped\n' ;;
              *bbbbbbbbbbbb*) printf 'ID\tPID\tSTATUS\nse-bbbbbbbbbbbb\t1\trunning\n' ;;
              *) printf 'malformed\n' ;;
            esac ;;
          *delete*) touch "$root/.deleted" ;;
          *) exit 1 ;;
        esac
        """,
        encoding="utf-8",
    )
    runsc.chmod(0o755)
    monkeypatch.setattr(broker, "_root_executable", lambda path: None)
    monkeypatch.setattr(broker, "_root_directory", lambda path: None)
    config = broker.BrokerConfig(
        runsc=runsc,
        rootfs=tmp_path,
        state_root=state,
        bundle_root=bundles,
        staging_root=staging,
        worker_uid=os.getuid(),
        worker_gid=os.getgid(),
    )
    assert broker._run_cleanup(config, 0) == 0
    assert not (state / ("se-" + "a" * 12)).exists()
    assert (state / ("se-" + "b" * 12)).exists()
    assert (state / ("se-" + "c" * 12)).exists()
