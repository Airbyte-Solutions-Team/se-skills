"""Golden fixture helpers used by the webapp feedback panel.

This is a small wrapper around the same `eval/golden/` directory used by the
regression suite. Keeping it separate from `eval/golden.py` avoids requiring the
eval package to be on the webapp's PYTHONPATH at runtime.
"""

from __future__ import annotations

from pathlib import Path

_GOLDEN_DIR = Path(__file__).resolve().parent.parent / "eval" / "golden"


def golden_path(skill: str, scenario: str) -> Path:
    """Return the file path for a skill's golden fixture under a scenario."""
    return _GOLDEN_DIR / skill / f"{scenario}.md"


def load_golden(skill: str, scenario: str) -> str | None:
    """Return the golden fixture text, or None if it does not exist."""
    path = golden_path(skill, scenario)
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def save_golden(skill: str, scenario: str, text: str) -> Path:
    """Write `text` as the golden fixture for `skill`/`scenario`."""
    path = golden_path(skill, scenario)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def list_golden_scenarios(skill: str | None = None) -> dict[str, list[str]]:
    """Return a mapping of skill -> list of scenario names with golden fixtures."""
    result: dict[str, list[str]] = {}
    search_dir = _GOLDEN_DIR if skill is None else _GOLDEN_DIR / skill
    if not search_dir.exists():
        return result
    for p in search_dir.rglob("*.md"):
        sk = p.parent.name
        scenario = p.stem
        result.setdefault(sk, []).append(scenario)
    for sk in result:
        result[sk].sort()
    return result
