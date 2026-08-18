#!/usr/bin/env -S uv run --script
"""Run the hosted-worker host contract preflight."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from webapp.hosted.preflight import LocalHostProbe, PreflightReport
from webapp.hosted.production_preflight import (
    ProductionPreflightSettings,
    run_production_preflight,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check the hosted worker host contract")
    parser.add_argument("--offline", action="store_true", help="Do not perform network-backed checks")
    parser.add_argument("--json", action="store_true", help="Emit structured redacted results")
    return parser.parse_args()


def _config(offline: bool) -> ProductionPreflightSettings:
    present = frozenset(
        name
        for name in (
            "DATABASE_WORKER_URL",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_EGRESS_PROXY_URL",
            "MODEL_PROXY_SECRET",
            "RUNSC_ROOTFS",
            "SANDBOX_IMAGE_DIGEST",
            "SANDBOX_MANIFEST_PATH",
        )
        if os.environ.get(name) or name == "SANDBOX_MANIFEST_PATH"
    )
    return ProductionPreflightSettings(
        runsc_path=os.environ.get("RUNSC_BINARY", "/usr/local/bin/runsc"),
        present_config_names=present,
        model_proxy_secret=os.environ.get("MODEL_PROXY_SECRET", ""),
        anthropic_api_url=os.environ.get("ANTHROPIC_API_URL", "https://api.anthropic.com"),
        anthropic_proxy_url=os.environ.get("ANTHROPIC_EGRESS_PROXY_URL", ""),
        database_url=os.environ.get("DATABASE_WORKER_URL", ""),
        storage_url=os.environ.get("SUPABASE_STORAGE_ENDPOINT", ""),
        rootfs_path=os.environ.get("RUNSC_ROOTFS", ""),
        manifest_path=os.environ.get(
            "SANDBOX_MANIFEST_PATH", "/etc/se-skills/sandbox-manifest.json"
        ),
        hosted_env=os.environ.get("HOSTED_ENV", "development").lower(),
        runtime=os.environ.get("HOSTED_RUNTIME", "post-call-runsc"),
        worker_user=os.environ.get("HOSTED_WORKER_USER", "se-worker"),
        worker_group=os.environ.get("HOSTED_WORKER_GROUP", "se-worker"),
        worker_uid=int(os.environ.get("HOSTED_WORKER_UID", "995")),
        non_worker_uid=int(os.environ.get("HOSTED_NON_WORKER_UID", "994")),
        approved_https_destinations=frozenset(
            value
            for value in os.environ.get("HOSTED_APPROVED_HTTPS_DESTINATIONS", "").split(",")
            if value
        ),
        sandbox_image_digest=os.environ.get("SANDBOX_IMAGE_DIGEST", ""),
        offline=offline,
    )


def _emit(report: PreflightReport, as_json: bool) -> None:
    if as_json:
        print(report.model_dump_json())
        return
    for check in report.checks:
        print(f"{check.check_id}: {check.status} ({check.severity}) — {check.detail}")


def main() -> int:
    args = _parse_args()
    if args.offline:
        print("offline: supply chain unverified")
    report = run_production_preflight(_config(args.offline), LocalHostProbe())
    _emit(report, args.json)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
