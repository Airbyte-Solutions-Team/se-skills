"""Shared configuration and filesystem-safe naming helpers for the webapp.

This module contains no service or route logic and does not import `webapp.app`.
It is imported by `app.py` and by services that need path/constants helpers.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import security
import yaml
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Workspace root resolution (see skills/_se-playbook.md "Workspace Paths").
#
# Resolution order: $SE_WORKSPACE env var > .se-config.yaml `workspace_root` >
# ~/.se-skills (default). The config file itself is looked up at
# $SE_WORKSPACE/.se-config.yaml, else ~/.se-skills/.se-config.yaml, else the
# legacy ~/airbyte-work/.se-config.yaml — mirrors the resolution skills use so
# the webapp and skill invocations agree on where customer data lives.
# ---------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except Exception:
        return {}


def _resolve_workspace() -> tuple[Path, Path, dict]:
    """Return (workspace_root, config_file_path, config_dict)."""
    env_root = os.environ.get("SE_WORKSPACE")
    if env_root:
        root = Path(os.path.expanduser(env_root))
        cfg_path = root / ".se-config.yaml"
        cfg = _load_yaml(cfg_path) if cfg_path.exists() else {}
        declared = cfg.get("workspace_root")
        return (Path(os.path.expanduser(declared)) if declared else root), cfg_path, cfg

    for default_root in (Path(os.path.expanduser("~/.se-skills")), Path(os.path.expanduser("~/airbyte-work"))):
        cfg_path = default_root / ".se-config.yaml"
        if cfg_path.exists():
            cfg = _load_yaml(cfg_path)
            declared = cfg.get("workspace_root")
            resolved = Path(os.path.expanduser(declared)) if declared else default_root
            return resolved, cfg_path, cfg

    default_root = Path(os.path.expanduser("~/.se-skills"))
    return default_root, default_root / ".se-config.yaml", {}


WORKSPACE, SE_CONFIG, _RESOLVED_SE_CONFIG = _resolve_workspace()
_LAYOUT = _RESOLVED_SE_CONFIG.get("layout") or {}
# Existing ~/airbyte-work setups predate the flat `customers/` convention and use
# the numbered `01-customers/` folder; only workspaces resolving elsewhere (e.g. the
# newer ~/.se-skills default) get the flat name. An explicit `layout.customers_dir`
# always wins. This keeps pre-existing installs pointed at their real data.
_IS_LEGACY_WORKSPACE = WORKSPACE == Path(os.path.expanduser("~/airbyte-work"))
CUSTOMERS_DIR = (
    Path(os.path.expanduser(_LAYOUT["customers_dir"]))
    if _LAYOUT.get("customers_dir")
    else WORKSPACE / ("01-customers" if _IS_LEGACY_WORKSPACE else "customers")
)
WEBAPP_DIR = Path(__file__).resolve().parent
TEAM_FILE = WEBAPP_DIR / "team-members.yaml"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

# internal.airbyte.ai clone — target for the "Push to repo" coverage-handoff
# action. Overridable via .se-config.yaml (`internal_repo_path`); see
# _internal_repo(). Default matches the standard workspace clone location; legacy
# ~/airbyte-work installs keep their existing 02-repos/ nesting.
INTERNAL_REPO_DEFAULT = (
    WORKSPACE / "02-repos" / "internal.airbyte.ai" if _IS_LEGACY_WORKSPACE else WORKSPACE / "internal.airbyte.ai"
)

# Where the skills live. Prefer the installed location; fall back to the repo
# copy next to this webapp (skills/ is a sibling of webapp/).
SKILLS_DIRS = [
    Path(os.path.expanduser("~/.claude/skills")),
    WEBAPP_DIR.parent / "skills",
]

SUITE_SKILLS_DIR = WEBAPP_DIR.parent / "skills"

# ---------------------------------------------------------------------------
# Safety: customer/member names are used in filesystem paths. Allowlist only.
# ---------------------------------------------------------------------------
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,80}$")


def _safe(name: str) -> str:
    name = (name or "").strip()
    if not SAFE_NAME.match(name) or ".." in name or "/" in name or "\\" in name:
        raise HTTPException(status_code=400, detail=f"Invalid name: {name!r}")
    return name


def _titlecase_folder(name: str) -> str:
    """Title-Case-Hyphenated, matching the workspace convention (e.g. Build-Manufacturing).

    Splits on any run of non-alphanumeric characters so SFDC account names with
    punctuation (e.g. "Octus (fka Reorg Research)") yield a SAFE_NAME-valid folder
    ("Octus-Fka-Reorg-Research") rather than one with stray parens that _safe() rejects."""
    return "-".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", name.strip()) if part)


def _slug(name: str) -> str:
    """Filesystem-safe slug for an opportunity name."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-")
    return s[:80] or "opportunity"


# ---------------------------------------------------------------------------
# Configuration loading
# ---------------------------------------------------------------------------
def _se_config() -> dict:
    """Load the optional `.se-config.yaml` from the workspace root.

    Cached on the function object for the lifetime of the process; the
    config is read once at startup and again only when an explicit reload is
    requested. Callers that need fresh config should call `_se_config_clear()`."""
    cache = getattr(_se_config, "_cache", None)
    if cache is not None:
        return cache
    cfg = _load_yaml(SE_CONFIG) if SE_CONFIG.exists() else {}
    _se_config._cache = cfg
    return cfg


def _se_config_clear() -> None:
    _se_config._cache = None


def _model_for(use: str) -> str:
    """Return the Claude model name configured for `use` (a skill id, or one of
    `quick-ask` / `live-ask` / `output-ask` / `default`). Falls back to
    `models.default` in `.se-config.yaml`, then `DEFAULT_CLAUDE_MODEL`."""
    models = _se_config().get("models", {}) or {}
    return models.get(use) or models.get("default") or DEFAULT_CLAUDE_MODEL


def _internal_repo() -> Path:
    """Path to the internal.airbyte.ai clone. Overridable via .se-config.yaml
    (`internal_repo_path`); defaults to the standard workspace location."""
    p = _se_config().get("internal_repo_path")
    if p:
        return Path(os.path.expanduser(p))
    return INTERNAL_REPO_DEFAULT


async def _run_cmd(args: list[str], cwd: Path, timeout: int = 120) -> tuple[int, str, str]:
    """Run a subprocess (git/gh) and return (returncode, stdout, stderr). Raises
    HTTPException(500) with stderr on a nonzero exit — callers that want to handle
    failures themselves should check the returncode instead."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        raise HTTPException(500, security.redact_sensitive(f"`{args[0]}` not found on PATH"))
    except asyncio.TimeoutError:
        raise HTTPException(504, security.redact_sensitive(f"`{' '.join(args[:2])}` timed out after {timeout}s"))
    rc = proc.returncode or 0
    so, se = out.decode(errors="replace"), err.decode(errors="replace")
    so = security.redact_sensitive(so)
    se = security.redact_sensitive(se)
    if rc != 0:
        raise HTTPException(500, f"`{' '.join(args[:2])}` failed: {se.strip() or so.strip()}")
    return rc, so, se
