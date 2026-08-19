"""Focused correctness checks for the no-build local webapp frontend."""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_app_js_parses_and_normalizes_output_meta(repo_root: Path) -> None:
    """The frontend has no JS test harness, so verify syntax and shared routing."""
    app_js = (repo_root / "webapp" / "static" / "app.js").read_text(encoding="utf-8")
    subprocess.run(["node", "--check", str(repo_root / "webapp" / "static" / "app.js")], check=True)

    assert "function normalizeOutputMeta(o)" in app_js
    assert "normalizeOutputMeta(o)]" in app_js
    assert "const meta = rawMeta ? normalizeOutputMeta(rawMeta) : null;" in app_js
    assert app_js.count("normalizeOutputMeta(") >= 4
