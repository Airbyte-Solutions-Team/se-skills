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


class ReferenceChange(BaseModel):
    """A change in a reference source between output generation and now."""

    source: str
    label: str
    old_date: str | None = None
    new_date: str | None = None
    old_status: str | None = None
    new_status: str | None = None


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
# Per-skill source mapping
# ---------------------------------------------------------------------------
# Only skills that actually consume product/connector reference data are
# mapped. Unknown skills get an empty list so they do not warn for sources
# they never touched.
_SKILL_REFERENCE_SOURCES: dict[str, set[str]] = {
    "biz-qual": set(),
    "prep-call": set(),
    "post-call": set(),
    "full-qual": set(),
    "next-move": set(),
    "account-refresher": set(),
    "forecast-prep": set(),
    "internal-prep": set(),
    "deal-assessment": set(),
    "roi-business-case": set(),
    "mutual-close-plan": set(),
    "pov-gsheet": set(),
    "connector-feasibility": {"registry", "airbyte_enterprise"},
    "deployment-model-qual": {"airbyte_platform", "airbyte_enterprise"},
    "tech-qual": {"airbyte_platform", "airbyte_enterprise"},
    "objection-handler": {"objection_reference", "airbyte_platform", "airbyte_enterprise"},
    "poc-plan": {"registry", "airbyte_platform", "airbyte_enterprise"},
}


def get_relevant_sources(skill: str | None) -> set[str] | None:
    """Return the reference sources a skill is known to depend on.

    Returns `None` when `skill` is not provided, which means "all known
    sources" (useful for diagnostics or explicit all-source checks).
    """
    if skill is None:
        return None
    return _SKILL_REFERENCE_SOURCES.get(skill, set())


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
    skill: str | None = None,
) -> list[ReferenceFreshness]:
    """Return the freshness of product/connector reference sources.

    `config` is the parsed `.se-config.yaml` (may be empty). `workspace` is the
    SE workspace root (used to resolve relative paths). `repo_root` points to
    the `se-skills` checkout so we can locate `skills/_reference/airbyte-objection-reference.md`.

    If `skill` is provided, only sources known to be relevant to that skill
    are returned. This prevents every output from warning about data the skill
    never consumed.
    """
    cfg = (config or {}).get("reference_data") or {}
    workspace = Path(os.path.expanduser(workspace))
    repos_dir = _resolve_path(config.get("airbyte_repos_dir"), workspace, "02-repos")

    # Build all known sources first, then filter by skill relevance.
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

    relevant = get_relevant_sources(skill)
    if relevant is None:
        return results
    return [r for r in results if r.source in relevant]


def compare_to_generation(
    current: list[ReferenceFreshness],
    generation: list[ReferenceFreshness] | None,
) -> list[ReferenceChange] | None:
    """Return the sources whose freshness has changed since output generation.

    When `generation` is `None` (legacy output or before the first read), this
    returns `None` because we cannot know what changed. An empty list means the
    output was generated with an empty (known) source set.
    """
    if generation is None:
        return None

    current_by_source = {r.source: r for r in current}
    generation_by_source = {r.source: r for r in generation}
    changes: list[ReferenceChange] = []

    for source, cur in current_by_source.items():
        gen = generation_by_source.get(source)
        if gen is None:
            changes.append(
                ReferenceChange(
                    source=source,
                    label=cur.label,
                    old_date=None,
                    new_date=cur.date,
                    old_status=None,
                    new_status=cur.status,
                )
            )
            continue
        if cur.status != gen.status or cur.date != gen.date:
            changes.append(
                ReferenceChange(
                    source=source,
                    label=cur.label,
                    old_date=gen.date,
                    new_date=cur.date,
                    old_status=gen.status,
                    new_status=cur.status,
                )
            )

    return changes
