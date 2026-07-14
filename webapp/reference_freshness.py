"""Reference-data freshness for product/connector sources used by SE skills.

Skills consume a mix of cached public registries, local monorepo checkouts, and
a repo-bundled objection-reference file. This module turns the filesystem
metadata (file mtimes, git refs) into a small typed freshness report that the
webapp can attach to each output sidecar and surface in the UI.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel


class ReferenceFreshness(BaseModel):
    """One product/connector reference source and how current it is."""

    source: str
    label: str
    status: str  # "fresh", "stale", or "missing"
    date: str | None = None
    age_days: int | None = None
    fresh: bool = False
    threshold_days: int = 7
    path: str | None = None


# ---------------------------------------------------------------------------
# Default thresholds and source names
# ---------------------------------------------------------------------------
_DEFAULT_THRESHOLDS = {
    "registry": 7,
    "objection_reference": 7,
    "airbyte_platform": 14,
    "airbyte_enterprise": 14,
    "connector_models": 7,
}

_SOURCE_LABELS = {
    "registry": "Connector registry cache",
    "objection_reference": "Objection reference",
    "airbyte_platform": "airbyte-platform repo",
    "airbyte_enterprise": "airbyte-enterprise repo",
    "connector_models": "airbyte-connector-models package",
}


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _resolve_path(value: str | None, base: Path, default: str) -> Path:
    """Resolve a configured path string.

    Absolute paths are used as-is. Relative paths are resolved under `base`.
    If no value is supplied, `base / default` is returned.
    """
    if not value:
        return base / default
    p = Path(os.path.expanduser(value))
    if p.is_absolute():
        return p
    return base / p


def _file_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _repo_mtime(repo: Path) -> float | None:
    """Best-effort freshness for a git checkout.

    Uses `.git/FETCH_HEAD` when available (records the last `git fetch`),
    otherwise `.git/HEAD`. Falls back to the directory mtime if `.git` is
    missing or unreadable.
    """
    for probe in (repo / ".git" / "FETCH_HEAD", repo / ".git" / "HEAD"):
        t = _file_mtime(probe)
        if t is not None:
            return t
    return _file_mtime(repo)


def _age_days(ts: float) -> int:
    now = datetime.now(timezone.utc)
    then = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (now - then).days


def _fmt_date(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _build_entry(
    source: str,
    label: str,
    threshold_days: int,
    path: Path,
    mtime: float | None,
) -> ReferenceFreshness:
    if mtime is None:
        return ReferenceFreshness(
            source=source,
            label=label,
            status="missing",
            date="unknown",
            age_days=None,
            fresh=False,
            threshold_days=threshold_days,
            path=str(path),
        )

    age = _age_days(mtime)
    fresh = age <= threshold_days
    return ReferenceFreshness(
        source=source,
        label=label,
        status="fresh" if fresh else "stale",
        date=_fmt_date(mtime),
        age_days=age,
        fresh=fresh,
        threshold_days=threshold_days,
        path=str(path),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def compute_reference_freshness(
    config: dict,
    workspace: Path,
    repo_root: Path,
) -> list[ReferenceFreshness]:
    """Return the freshness of every product/connector reference source.

    `config` is the parsed `.se-config.yaml` (may be empty). `workspace` is the
    SE workspace root (used to resolve relative paths). `repo_root` points to
    the `se-skills` checkout so we can locate `skills/_reference/airbyte-objection-reference.md`.

    The result is a list of `ReferenceFreshness` records; `fresh=False` means the
    source is either stale or missing and the UI should warn the SE.
    """
    cfg = (config or {}).get("reference_data") or {}
    workspace = Path(os.path.expanduser(workspace))
    repos_dir = _resolve_path(config.get("airbyte_repos_dir"), workspace, "02-repos")

    results: list[ReferenceFreshness] = []

    # DS1 — Connector registry cache (public HTTPS, cached locally)
    registry_cfg = cfg.get("registry") or {}
    registry_dir = _resolve_path(
        registry_cfg.get("cache_dir"),
        repos_dir,
        "registry",
    )
    registry_files = [
        registry_dir / "oss_registry.json",
        registry_dir / "cloud_registry.json",
    ]
    newest_mtime: float | None = None
    for f in registry_files:
        t = _file_mtime(f)
        if t is not None and (newest_mtime is None or t > newest_mtime):
            newest_mtime = t
    results.append(
        _build_entry(
            "registry",
            _SOURCE_LABELS["registry"],
            _DEFAULT_THRESHOLDS["registry"],
            registry_dir,
            newest_mtime,
        )
    )

    # DS2 / DS3 — Local monorepo checkouts
    repos_cfg = cfg.get("repos") or {}
    defaults = {
        "airbyte_platform": "airbyte-platform",
        "airbyte_enterprise": "airbyte-enterprise",
    }
    for source, default_name in defaults.items():
        repo_name = repos_cfg.get(source) or default_name
        repo_path = _resolve_path(repo_name, repos_dir, default_name)
        mtime = _repo_mtime(repo_path)
        results.append(
            _build_entry(
                source,
                _SOURCE_LABELS[source],
                _DEFAULT_THRESHOLDS[source],
                repo_path,
                mtime,
            )
        )

    # DS4 — Optional connector models package
    if cfg.get("connector_models", {}).get("enabled"):
        try:
            import airbyte_connector_models as acm

            pkg_path = Path(acm.__file__).resolve().parent
            pkg_mtime = _file_mtime(pkg_path)
        except Exception:
            pkg_path = repos_dir / "airbyte-connector-models"
            pkg_mtime = _file_mtime(pkg_path)
        results.append(
            _build_entry(
                "connector_models",
                _SOURCE_LABELS["connector_models"],
                _DEFAULT_THRESHOLDS["connector_models"],
                pkg_path,
                pkg_mtime,
            )
        )

    # Objection reference (shipped with the repo and/or installed skills)
    objection_paths = [
        repo_root / "skills" / "_reference" / "airbyte-objection-reference.md",
        Path.home() / ".claude" / "skills" / "_reference" / "airbyte-objection-reference.md",
    ]
    objection_mtime: float | None = None
    objection_path = objection_paths[0]
    for p in objection_paths:
        t = _file_mtime(p)
        if t is not None:
            objection_mtime = t
            objection_path = p
            break
    results.append(
        _build_entry(
            "objection_reference",
            _SOURCE_LABELS["objection_reference"],
            _DEFAULT_THRESHOLDS["objection_reference"],
            objection_path,
            objection_mtime,
        )
    )

    return results
