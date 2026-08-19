"""Adversarial tests for the root-owned runsc broker entrypoint."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "runsc_broker", Path(__file__).parents[2] / "scripts/runsc_broker.py"
)
assert _SPEC is not None and _SPEC.loader is not None
broker = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = broker
_SPEC.loader.exec_module(broker)

CONTAINER_ID = "se-" + "a" * 12


def _job_payload() -> dict[str, object]:
    return {
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
        "input_manifest": {
            "transcript_id": "transcript-1",
            "transcript_ref": "ref-1",
            "account_id": "account-1",
            "org_id": "org-1",
            "opportunity_id": None,
            "prior_context_refs": [],
        },
        "allowlist": {"tools": ["write_output"], "network": []},
        "execution_deadline": (
            datetime.now(timezone.utc) + timedelta(seconds=90)
        ).isoformat(),
        "input_workspace": "/runtime/input",
        "output_workspace": "/runtime/output",
        "attempt_id": "attempt-1",
        "proxy_token": None,
        "proxy_uds_path": None,
    }


def _config(
    tmp_path: Path, cleanup_min_age_seconds: int = 3600
) -> broker.BrokerConfig:
    for name in ("state", "bundles", "staging", "workspace", "rootfs", "journal"):
        (tmp_path / name).mkdir()
    runsc = tmp_path / "runsc"
    runsc.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    runsc.chmod(0o755)
    return broker.BrokerConfig(
        runsc=runsc,
        rootfs=tmp_path / "rootfs",
        state_root=tmp_path / "state",
        bundle_root=tmp_path / "bundles",
        staging_root=tmp_path / "staging",
        workspace_root=tmp_path / "workspace",
        worker_uid=os.getuid(),
        worker_gid=os.getgid(),
        cleanup_min_age_seconds=cleanup_min_age_seconds,
        journal_root=tmp_path / "journal",
    )


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    config: broker.BrokerConfig,
    request: dict[str, object],
    chown_calls: list[tuple[object, ...]] | None = None,
) -> int:
    config_path = config.state_root.parent / "broker.json"
    config_path.write_text(
        json.dumps(
            {
                "runsc": str(config.runsc),
                "rootfs": str(config.rootfs),
                "state_root": str(config.state_root),
                "bundle_root": str(config.bundle_root),
                "staging_root": str(config.staging_root),
                "workspace_root": str(config.workspace_root),
                "worker_uid": config.worker_uid,
                "worker_gid": config.worker_gid,
                "sandbox_uid": config.sandbox_uid,
                "sandbox_gid": config.sandbox_gid,
                "cleanup_min_age_seconds": config.cleanup_min_age_seconds,
                "journal_root": str(config.journal_root),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(broker, "CONFIG_PATH", config_path)
    monkeypatch.setattr(sys, "argv", ["se-skills-runsc"])
    monkeypatch.setattr(broker, "_root_directory", lambda path: None)
    monkeypatch.setattr(broker, "_worker_workspace_root", lambda path, gid: None)
    monkeypatch.setattr(broker, "_root_executable", lambda path: None)
    monkeypatch.setattr(
        broker.os,
        "chown",
        lambda *args: chown_calls.append(args) if chown_calls is not None else None,
    )
    monkeypatch.setattr(
        broker.os,
        "fchown",
        lambda *args: chown_calls.append(args) if chown_calls is not None else None,
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    return broker.main()


def _run_request(input_dir: Path, output_dir: Path) -> dict[str, object]:
    return {
        "operation": "run",
        "container_id": CONTAINER_ID,
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "proxy_uds_path": None,
        "job": _job_payload(),
    }


def test_entrypoint_rejects_all_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(broker, "CONFIG_PATH", config_path := tmp_path / "unused.json")
    monkeypatch.setattr(sys, "argv", ["se-skills-runsc", "--config", str(config_path)])
    with pytest.raises(SystemExit) as error:
        broker.main()
    assert error.value.code == 64


def test_run_entrypoint_authors_fixed_config_and_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    input_dir = config.workspace_root / "se-runtime-input-attempt"
    output_dir = config.workspace_root / "se-runtime-output-attempt"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "input.txt").write_text("input", encoding="utf-8")
    snapshot = tmp_path / "config.snapshot"
    job_snapshot = tmp_path / "job.snapshot"
    chown_calls: list[tuple[object, ...]] = []
    config.runsc.write_text(
        f"""#!/bin/sh
bundle=
for arg in "$@"; do
  case "$arg" in --bundle) next=1;; *) if [ "$next" = 1 ]; then bundle="$arg"; next=0; fi;; esac
done
cp "$bundle/config.json" "{snapshot}"
stat -c '%u:%g:%a' "$bundle/job.json" > "{job_snapshot}"
exit 0
""",
        encoding="utf-8",
    )
    config.runsc.chmod(0o755)

    assert (
        _invoke(
            monkeypatch,
            config,
            _run_request(input_dir, output_dir),
            chown_calls,
        )
        == 0
    )

    document = json.loads(snapshot.read_text(encoding="utf-8"))
    assert document["root"] == {"path": str(config.rootfs), "readonly": True}
    assert document["process"]["args"] == [
        "/usr/bin/python3",
        "/app/webapp/hosted/runsc/sandbox_entry.py",
    ]
    assert document["process"]["capabilities"] == {
        "bounding": [],
        "effective": [],
        "permitted": [],
        "inheritable": [],
        "ambient": [],
    }
    assert "devices" not in document["linux"]
    assert all(item["source"] != "/etc/passwd" for item in document["mounts"])
    assert job_snapshot.read_text(encoding="utf-8").strip().endswith(":444")
    assert any(
        Path(str(call[0])) == config.bundle_root / CONTAINER_ID / "job.json"
        and call[1:] == (config.sandbox_uid, config.sandbox_gid)
        for call in chown_calls
    )
    assert not output_dir.exists()
    staged_output = config.staging_root / CONTAINER_ID / "output"
    assert (staged_output.stat().st_mode & 0o777) == 0o770


def test_run_rejects_output_symlink_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    input_dir = config.workspace_root / "se-runtime-input-attempt"
    output_dir = config.workspace_root / "se-runtime-output-attempt"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "input.txt").write_text("input", encoding="utf-8")
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("sentinel", encoding="utf-8")
    (output_dir / "link").symlink_to(sentinel)

    with pytest.raises(SystemExit) as error:
        _invoke(monkeypatch, config, _run_request(input_dir, output_dir))

    assert error.value.code == 64
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_run_rejects_output_special_file_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    input_dir = config.workspace_root / "se-runtime-input-attempt"
    output_dir = config.workspace_root / "se-runtime-output-attempt"
    input_dir.mkdir()
    output_dir.mkdir()
    (input_dir / "input.txt").write_text("input", encoding="utf-8")
    os.mkfifo(output_dir / "pipe")

    with pytest.raises(SystemExit) as error:
        _invoke(monkeypatch, config, _run_request(input_dir, output_dir))

    assert error.value.code == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda request: request.update(extra="unknown"),
        lambda request: request.update(input_dir="/etc"),
        lambda request: request.update(output_dir="/etc"),
        lambda request: request.update(
            job={**request["job"], "rootfs": "/etc"}
        ),
        lambda request: request.update(
            job={
                **request["job"],
                "allowlist": {"tools": [], "network": []},
                "capabilities": [],
            }
        ),
    ],
)
def test_run_entrypoint_rejects_adversarial_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: object,
) -> None:
    config = _config(tmp_path)
    input_dir = config.workspace_root / "se-runtime-input-attempt"
    output_dir = config.workspace_root / "se-runtime-output-attempt"
    input_dir.mkdir()
    output_dir.mkdir()
    request = _run_request(input_dir, output_dir)
    mutation(request)
    with pytest.raises(SystemExit) as exc_info:
        _invoke(monkeypatch, config, request)
    assert exc_info.value.code == 64
    assert not input_dir.is_symlink()
    assert not output_dir.is_symlink()


def test_list_entrypoint_uses_typed_state_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    state_dir = config.state_root / CONTAINER_ID
    state_dir.mkdir()
    config.runsc.write_text(
        "#!/bin/sh\nprintf 'ID\\tPID\\tSTATUS\\n'\n", encoding="utf-8"
    )
    config.runsc.chmod(0o755)
    request = {
        "operation": "list",
        "container_id": CONTAINER_ID,
        "state_dir": str(state_dir),
    }
    assert _invoke(monkeypatch, config, request) == 0


def test_cleanup_rejects_worker_supplied_age(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            monkeypatch,
            config,
            {"operation": "cleanup", "minimum_age_seconds": 0},
        )
    assert exc_info.value.code == 64


def _stale_residue(config: broker.BrokerConfig) -> tuple[Path, Path, Path]:
    state = config.state_root / CONTAINER_ID
    bundle = config.bundle_root / CONTAINER_ID
    staging = config.staging_root / CONTAINER_ID
    state.mkdir()
    bundle.mkdir()
    staging.mkdir()
    (bundle / "job.json").write_text("capability", encoding="utf-8")
    old = time.time() - 10
    for path in (state, bundle, staging):
        os.utime(path, (old, old))
    return state, bundle, staging


def test_cleanup_reclaims_old_corrupt_journal_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, cleanup_min_age_seconds=1)
    state, bundle, staging = _stale_residue(config)
    journal = config.journal_root / f"{CONTAINER_ID}.json"
    journal.write_text("{not-json", encoding="utf-8")
    os.utime(journal, (time.time() - 10, time.time() - 10))
    config.runsc.write_text(
        "#!/bin/sh\nprintf 'ID\\tPID\\tSTATUS\\n'\n", encoding="utf-8"
    )
    config.runsc.chmod(0o755)
    monkeypatch.setattr(broker, "_safe_journal_file", lambda path: None)

    assert _invoke(monkeypatch, config, {"operation": "cleanup"}) == 0
    assert not state.exists()
    assert not bundle.exists()
    assert not staging.exists()
    assert not journal.exists()


def test_cleanup_reclaims_old_mismatched_journal_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, cleanup_min_age_seconds=1)
    state, bundle, staging = _stale_residue(config)
    journal = config.journal_root / f"{CONTAINER_ID}.json"
    journal.write_text(
        json.dumps({"container_id": "se-" + "b" * 12}),
        encoding="utf-8",
    )
    os.utime(journal, (time.time() - 10, time.time() - 10))
    config.runsc.write_text(
        "#!/bin/sh\nprintf 'ID\\tPID\\tSTATUS\\n'\n", encoding="utf-8"
    )
    config.runsc.chmod(0o755)
    monkeypatch.setattr(broker, "_safe_journal_file", lambda path: None)

    assert _invoke(monkeypatch, config, {"operation": "cleanup"}) == 0
    assert not state.exists()
    assert not bundle.exists()
    assert not staging.exists()
    assert not journal.exists()


def test_cleanup_reclaims_old_journalless_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, cleanup_min_age_seconds=1)
    state, bundle, staging = _stale_residue(config)
    config.runsc.write_text(
        "#!/bin/sh\nprintf 'ID\\tPID\\tSTATUS\\n'\n", encoding="utf-8"
    )
    config.runsc.chmod(0o755)

    assert _invoke(monkeypatch, config, {"operation": "cleanup"}) == 0
    assert not state.exists()
    assert not bundle.exists()
    assert not staging.exists()


def test_cleanup_reclaims_old_bundle_only_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, cleanup_min_age_seconds=1)
    _, bundle, staging = _stale_residue(config)
    shutil.rmtree(config.state_root / CONTAINER_ID)
    config.runsc.write_text(
        "#!/bin/sh\nprintf 'ID\\tPID\\tSTATUS\\n'\n", encoding="utf-8"
    )
    config.runsc.chmod(0o755)

    assert _invoke(monkeypatch, config, {"operation": "cleanup"}) == 0
    assert not bundle.exists()
    assert not staging.exists()


@pytest.mark.parametrize("corrupt_journal", [False, True])
def test_cleanup_retains_residue_when_container_absence_is_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_journal: bool,
) -> None:
    config = _config(tmp_path, cleanup_min_age_seconds=1)
    state, bundle, staging = _stale_residue(config)
    journal = config.journal_root / f"{CONTAINER_ID}.json"
    if corrupt_journal:
        journal.write_text("{not-json", encoding="utf-8")
        os.utime(journal, (time.time() - 10, time.time() - 10))
        monkeypatch.setattr(broker, "_safe_journal_file", lambda path: None)
    config.runsc.write_text(
        "#!/bin/sh\nprintf 'ID\\tPID\\tSTATUS\\n%s\\t-\\trunning\\n' "
        f"{CONTAINER_ID}\n",
        encoding="utf-8",
    )
    config.runsc.chmod(0o755)

    assert _invoke(monkeypatch, config, {"operation": "cleanup"}) == 0
    assert state.exists()
    assert bundle.exists()
    assert staging.exists()
    assert (bundle / "job.json").exists()
    assert journal.exists() is corrupt_journal


def test_entrypoint_rejects_state_root_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    request = {
        "operation": "list",
        "container_id": CONTAINER_ID,
        "state_dir": str(outside / CONTAINER_ID),
    }
    with pytest.raises(SystemExit) as exc_info:
        _invoke(monkeypatch, config, request)
    assert exc_info.value.code == 64
    assert outside.is_dir()


def test_entrypoint_rejects_bundle_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    input_dir = config.workspace_root / "se-runtime-input-attempt"
    output_dir = config.workspace_root / "se-runtime-output-attempt"
    input_dir.mkdir()
    output_dir.mkdir()
    bundle_link = config.bundle_root / CONTAINER_ID
    bundle_link.symlink_to(tmp_path / "outside", target_is_directory=True)
    with pytest.raises(SystemExit) as exc_info:
        _invoke(monkeypatch, config, _run_request(input_dir, output_dir))
    assert exc_info.value.code == 64
    assert bundle_link.is_symlink()


def test_request_models_reject_operation_specific_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        _invoke(
            monkeypatch,
            config,
            {
                "operation": "cleanup",
                "minimum_age_seconds": 0,
                "container_id": CONTAINER_ID,
            },
        )
    assert exc_info.value.code == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda request: request["job"].update(skill="x" * 8193),
        lambda request: request["job"].update(
            execution_deadline=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        ),
        lambda request: request["job"].update(
            execution_deadline=(datetime.now(timezone.utc) + timedelta(seconds=901)).isoformat()
        ),
        lambda request: request["job"]["input_manifest"].update(
            prior_context_refs=["x"] * 257
        ),
    ],
)
def test_broker_bounds_worker_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: object,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _config(tmp_path)
    input_dir = config.workspace_root / "se-runtime-input-attempt"
    output_dir = config.workspace_root / "se-runtime-output-attempt"
    input_dir.mkdir()
    output_dir.mkdir()
    request = _run_request(input_dir, output_dir)
    mutation(request)
    with pytest.raises(SystemExit) as exc_info:
        _invoke(monkeypatch, config, request)
    assert exc_info.value.code == 64
    assert capsys.readouterr().err.strip() == "invalid runsc broker request"
