#!/usr/bin/env -S uv run --script
"""Run the hosted-worker host contract preflight."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from webapp.hosted.preflight import LocalHostProbe, PreflightConfig, PreflightReport, run_preflight


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the hosted worker host contract")
    parser.add_argument("--offline", action="store_true", help="Do not perform network-backed checks")
    parser.add_argument("--json", action="store_true", help="Emit structured redacted results")
    return parser.parse_args()


def _config(offline: bool) -> PreflightConfig:
    pins = json.loads((repo_root / "deploy" / "pins.json").read_text(encoding="utf-8"))
    runsc = pins["runsc"]
    limits = pins["limits"]
    present = frozenset(name for name in (
        "DATABASE_WORKER_URL",
        "ANTHROPIC_API_KEY",
        "MODEL_PROXY_SECRET",
        "RUNSC_ROOTFS",
        "SANDBOX_IMAGE_DIGEST",
        "RUNSC_ROOTFS_DIGEST",
    ) if os.environ.get(name))
    supply_chain = None
    return PreflightConfig(
        runsc_path=os.environ.get("RUNSC_BINARY", "/usr/local/bin/runsc"),
        runsc_version=runsc["version"],
        runsc_sha512=runsc["sha512"]["x86_64"],
        present_config_names=present,
        model_proxy_secret=os.environ.get("MODEL_PROXY_SECRET", ""),
        anthropic_api_url=os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com"),
        database_url=os.environ.get("DATABASE_WORKER_URL", ""),
        storage_url=os.environ.get("SUPABASE_STORAGE_ENDPOINT", ""),
        rootfs_path=os.environ.get("RUNSC_ROOTFS", ""),
        firewall_policy_path=os.environ.get(
            "HOSTED_FIREWALL_POLICY_PATH", "/etc/se-skills/firewall.nft"
        ),
        min_root_free_bytes=limits["min_root_free_bytes"],
        min_temp_free_bytes=limits["min_temp_free_bytes"],
        supply_chain=supply_chain,
        supply_chain_skipped=offline,
    )


def _emit(report: PreflightReport, as_json: bool) -> None:
    if as_json:
        print(report.model_dump_json())
        return
    for check in report.checks:
        print(f"{check.check_id}: {check.status} ({check.severity}) — {check.detail}")


def main() -> int:
    args = _parse_args()
    report = run_preflight(_config(args.offline), LocalHostProbe())
    _emit(report, args.json)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
