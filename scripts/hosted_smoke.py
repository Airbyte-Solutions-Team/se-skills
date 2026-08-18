#!/usr/bin/env -S uv run --script
"""Run the hosted worker smoke harness."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root))

from webapp.hosted.smoke import SmokeReport, run_live_smoke, run_offline_smoke


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hosted worker smoke harness")
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _args()
    if args.mode == "live":
        print("estimated scope: one approved worker smoke attempt; estimated cost: provider rates apply")
        report = run_live_smoke()
    else:
        report = run_offline_smoke()
    if args.json:
        print(report.model_dump_json())
    else:
        for check in report.checks:
            print(f"{check.check_id}: {check.status} ({check.severity}) — {check.detail}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
