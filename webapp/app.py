#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi", "uvicorn[standard]", "pyyaml",
#   "faster-whisper", "sounddevice", "numpy", "sse-starlette", "anthropic",
#   "markdown", "nh3", "keyring",
# ]
# ///
# NOTE: live-transcribe needs the PortAudio system lib for sounddevice:
#   brew install portaudio   (one-time)
# and BlackHole for system-audio capture (see README → Live Transcribe setup).
"""
SE Skills — local web app.

A thin UI over the filesystem the SE skills already produce, plus a button to
invoke a skill via Claude Code headless (`claude -p`).

Structure:
  Main page      → solutions team members (from team-members.yaml + .se-config.yaml)
  Member page    → that member's accounts (folders in 01-customers/) + create account
  Account page   → all outputs for that account + invoke a skill

Run:
  cd webapp && uv run app.py
  (or: uvicorn app:app --reload --port 8787)

This is LOCAL ONLY. It runs as you, on your machine, using your already-authed
Claude Code + MCPs + local files. Do not deploy this to a shared server without
solving multi-user auth + data isolation first (see README).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import queue
import re
import subprocess
import threading
import urllib.parse
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Any

import md_render
import orchestrator
import persistence
import security
import soql

from routes.jobs import router as jobs_router
from routes.outputs import router as outputs_router
from routes.feedback import router as feedback_router
from services.feedback_service import FeedbackService
from services.job_service import JobService
from services.output_service import OutputService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths — everything is relative to the airbyte-work workspace
# ---------------------------------------------------------------------------
WORKSPACE = Path(os.path.expanduser("~/airbyte-work"))
CUSTOMERS_DIR = WORKSPACE / "01-customers"
SE_CONFIG = WORKSPACE / ".se-config.yaml"
WEBAPP_DIR = Path(__file__).resolve().parent
TEAM_FILE = WEBAPP_DIR / "team-members.yaml"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

# internal.airbyte.ai clone — target for the "Push to repo" coverage-handoff
# action. Overridable via .se-config.yaml (`internal_repo_path`); see
# _internal_repo(). Default matches the standard workspace clone location.
INTERNAL_REPO_DEFAULT = WORKSPACE / "02-repos" / "internal.airbyte.ai"

# Where the skills live. Prefer the installed location; fall back to the repo
# copy next to this webapp (skills/ is a sibling of webapp/).
SKILLS_DIRS = [
    Path(os.path.expanduser("~/.claude/skills")),
    WEBAPP_DIR.parent / "skills",
]

# Optional presentation overrides: preferred display order + friendlier labels/
# blurbs than raw frontmatter. NOT the source of truth for WHICH skills exist —
# that's derived from the skill folders on disk (see discover_skills). A new
# skill appears automatically; add an entry here only to tune how it's shown.
# `tier` groups skills by their real dependency stage (see _se-playbook.md "Skill
# Sequencing Rules"). `step` is the suggested run-order WITHIN the linear workflow
# tiers — shown as a numbered prefix in the picker. Anytime/router/standalone
# skills have step=None (no number; they don't sit at a fixed point in the chain).
# `order` drives the overall sort. This is presentation only — folder names and the
# `name:` frontmatter are untouched, so skill discovery/triggering never changes.
TIER_WORKFLOW = "Workflow — run in order"
TIER_LATE     = "Late-stage — after POC"
TIER_ANYTIME  = "Anytime — as needed"
TIER_META     = "When you're not sure"

SKILL_PRESENTATION = {
    # ── Workflow chain (numbered) ──────────────────────────────────────────
    "prep-call":             {"label": "Prep Call",             "blurb": "Tech-discovery call prep — the only skill that needs no prior data", "tier": TIER_WORKFLOW, "step": 1, "order": 1},
    "post-call":             {"label": "Post-Call Summary",     "blurb": "Summarize the latest call (run after each call)",                    "tier": TIER_WORKFLOW, "step": 2, "order": 2},
    "deployment-model-qual": {"label": "Deployment Qual",       "blurb": "Cloud vs Self-Managed — the gate before technical scoping",          "tier": TIER_WORKFLOW, "step": 3, "order": 3},
    "biz-qual":              {"label": "Biz Qual (MEDDPICC)",   "blurb": "Business qualification (needs a transcript)",                        "tier": TIER_WORKFLOW, "step": 4, "order": 4},
    "tech-qual":             {"label": "Tech Qual",             "blurb": "Technical fit assessment (needs a transcript)",                      "tier": TIER_WORKFLOW, "step": 5, "order": 5},
    "full-qual":             {"label": "Full Qual (biz + tech)","blurb": "Shortcut: runs biz-qual + tech-qual back-to-back (two separate docs)","tier": TIER_WORKFLOW, "step": None, "order": 5.5},
    "connector-feasibility": {"label": "Connector Feasibility", "blurb": "Source/dest coverage check",                                         "tier": TIER_WORKFLOW, "step": 6, "order": 6},
    "poc-plan":              {"label": "POC Plan",              "blurb": "Scope a POC (needs biz-qual + tech-qual — will offer to run them)",  "tier": TIER_WORKFLOW, "step": 7, "order": 7},
    # ── Late-stage / closing (numbered, but only after POC data exists) ──────
    "roi-business-case":     {"label": "ROI Business Case",     "blurb": "Compile the economic buyer's TCO/ROI number",                      "tier": TIER_LATE, "step": 8, "order": 8},
    "mutual-close-plan":     {"label": "Mutual Close Plan",     "blurb": "Path from POC-success to signature (owners + dates)",              "tier": TIER_LATE, "step": 9, "order": 9},
    # ── Anytime / as-needed (unnumbered) ───────────────────────────────────
    "deal-assessment":       {"label": "Deal Assessment",       "blurb": "Honest deal-health read (run every ~2 weeks)",     "tier": TIER_ANYTIME, "step": None, "order": 20},
    "account-refresher":     {"label": "Account Refresher",     "blurb": "Fast catch-me-up briefing before a touchpoint",    "tier": TIER_ANYTIME, "step": None, "order": 21},
    "follow-up-email":       {"label": "Follow-up Email",       "blurb": "Draft a customer email in your voice",             "tier": TIER_ANYTIME, "step": None, "order": 22},
    "objection-handler":     {"label": "Objection Handler",     "blurb": "Talk track for a specific customer concern",       "tier": TIER_ANYTIME, "step": None, "order": 23},
    "internal-prep":         {"label": "Internal Prep",         "blurb": "AE sync / forecast / exec-readout prep (internal)","tier": TIER_ANYTIME, "step": None, "order": 24},
    "coverage-handoff":      {"label": "Coverage Handoff",      "blurb": "PTO handoff for a covering SE",                    "tier": TIER_ANYTIME, "step": None, "order": 25},
    "pov-gsheet":            {"label": "POV Google Sheet",      "blurb": "Create and pre-fill a POV Success Criteria Google Sheet", "tier": TIER_ANYTIME, "step": None, "order": 26},
    # ── Router (unnumbered) ────────────────────────────────────────────────
    "next-move":             {"label": "Next Move",             "blurb": "Not sure what to run? This inspects the deal and tells you", "tier": TIER_META, "step": None, "order": 30},
}

# Permission profiles for each skill. The webapp invokes `claude -p` with
# `--permission-mode acceptEdits`, which grants broad file/shell/MCP access.
# This classification is used to surface an explicit SE approval step before
# launching a skill that writes files, runs shell commands, or runs git commands.
class PermissionProfile(BaseModel):
    write: bool = True
    shell: bool = False
    git: bool = False

    def requires_approval(self) -> bool:
        return self.write or self.shell or self.git


# API response model for the permission profile of a specific skill invocation.
class SkillPermission(BaseModel):
    write: bool = True
    shell: bool = False
    git: bool = False
    requires_approval: bool = True
    summary: str = ""

    @classmethod
    def from_profile(cls, profile: PermissionProfile) -> "SkillPermission":
        caps = []
        if profile.write:
            caps.append("writes a file to the customer workspace")
        if profile.git:
            caps.append("runs git commands")
        if profile.shell:
            caps.append("runs shell commands")
        return cls(
            write=profile.write,
            shell=profile.shell,
            git=profile.git,
            requires_approval=profile.requires_approval(),
            summary="; ".join(caps) if caps else "performs this action",
        )


# Known skill permission overrides. Unknown skills default to write-only, which
# is the common case because every SE skill auto-saves its Markdown output.
SKILL_PERMISSIONS: dict[str, PermissionProfile] = {
    "connector-feasibility": PermissionProfile(write=True, shell=True, git=True),
    "freeform": PermissionProfile(write=True, shell=True, git=True),
    "pov-gsheet": PermissionProfile(write=True, shell=True, git=False),
}


def _permission_profile(skill_id: str | None, freeform: bool = False) -> SkillPermission:
    """Return the permission profile for a skill invocation.

    Free-form instructions always get the broadest profile because their content
    is uncontrolled. Known skills get the profile from SKILL_PERMISSIONS, with
    a safe write-only default for skills that only auto-save a Markdown output.
    """
    if freeform or not skill_id:
        profile = SKILL_PERMISSIONS["freeform"]
    else:
        profile = SKILL_PERMISSIONS.get(skill_id, PermissionProfile(write=True))
    return SkillPermission.from_profile(profile)


# Source of truth for WHICH skills belong to this suite: the repo's own
# skills/ folder (a sibling of webapp/). This scopes the app to the SE suite
# and deliberately excludes other skills the user may have in ~/.claude/skills/
# (remotion, find-skills, etc.). A new SE skill added to the repo appears
# automatically; unrelated global skills never leak in.
SUITE_SKILLS_DIR = WEBAPP_DIR.parent / "skills"


def discover_skills() -> list[dict]:
    """Every folder with a SKILL.md under the repo's skills/ dir is a suite skill.
    `_se-playbook.md` is shared reference (leading underscore → excluded).
    Presentation (label/blurb/order) overlaid from SKILL_PRESENTATION when present;
    otherwise a sensible default is derived so a newly-added skill still shows."""
    found = []
    if SUITE_SKILLS_DIR.exists():
        for d in sorted(SUITE_SKILLS_DIR.iterdir()):
            if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
                continue
            if not (d / "SKILL.md").exists():
                continue
            sid = d.name
            pres = SKILL_PRESENTATION.get(sid, {})
            found.append({
                "id": sid,
                "label": pres.get("label") or sid.replace("-", " ").title(),
                "blurb": pres.get("blurb") or "",
                "tier": pres.get("tier") or "Other",
                "step": pres.get("step"),
                "order": pres.get("order", 999),
            })
    # Fallback: if the repo skills/ dir isn't found, use the presentation list
    if not found:
        found = [{"id": k, **v} for k, v in SKILL_PRESENTATION.items()]
    found.sort(key=lambda s: (s.get("order", 999), s["label"]))
    return [{"id": s["id"], "label": s["label"], "blurb": s["blurb"],
             "tier": s.get("tier", "Other"), "step": s.get("step"),
             "permissions": _permission_profile(s["id"]).model_dump()} for s in found]


SKILLS = discover_skills()
SKILL_IDS = {s["id"] for s in SKILLS}

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


# ---------------------------------------------------------------------------
# Team members — from team-members.yaml, falling back to the local SE config
# ---------------------------------------------------------------------------
def load_team() -> list[dict]:
    if TEAM_FILE.exists():
        data = yaml.safe_load(TEAM_FILE.read_text()) or {}
        members = data.get("members", [])
        if members:
            return members
    # Fallback: just the local SE from .se-config.yaml
    if SE_CONFIG.exists():
        cfg = yaml.safe_load(SE_CONFIG.read_text()) or {}
        return [{"id": "me", "name": cfg.get("name", "Me"), "email": cfg.get("email", "")}]
    return [{"id": "me", "name": "Me", "email": ""}]


def member_by_id(member_id: str) -> dict | None:
    return next((m for m in load_team() if m.get("id") == member_id), None)


def _member_id_from_name(name: str, existing_ids: set[str]) -> str:
    """Stable, unique id from a display name (e.g. 'Ryan Waskewich' -> 'ryan-waskewich')."""
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "member"
    mid, n = base, 2
    while mid in existing_ids:
        mid = f"{base}-{n}"; n += 1
    return mid


def save_team(members: list[dict]) -> None:
    TEAM_FILE.write_text(yaml.safe_dump({"members": members}, sort_keys=False, allow_unicode=True))


# ---------------------------------------------------------------------------
# Accounts & outputs — read straight from the filesystem the skills produce
# ---------------------------------------------------------------------------
def list_accounts() -> list[dict]:
    """All customer folders. (v1: accounts are workspace-wide, tagged by owner in account meta.)"""
    if not CUSTOMERS_DIR.exists():
        return []
    out = []
    for d in sorted(CUSTOMERS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        out.append({"name": d.name, "owner": _read_owner(d), "archived": _is_archived(d)})
    return out


def _owner_file(account_dir: Path) -> Path:
    return account_dir / ".owner"


def _read_owner(account_dir: Path) -> str | None:
    f = _owner_file(account_dir)
    return f.read_text().strip() if f.exists() else None


def _archived_file(account_dir: Path) -> Path:
    return account_dir / ".archived"


def _is_archived(account_dir: Path) -> bool:
    return _archived_file(account_dir).exists()


def _sfdc_name_file(account_dir: Path) -> Path:
    return account_dir / ".sfdc-name"


def _read_sfdc_name(account: str) -> str | None:
    """The true SFDC Account.Name captured at create time, if any. Folder names
    are lossy (punctuation stripped for filesystem safety), so we cannot rebuild
    the SFDC name from the folder — we store it verbatim instead."""
    f = _sfdc_name_file(CUSTOMERS_DIR / account)
    return f.read_text().strip() if f.exists() else None


def _sfdc_like_prefix(account: str) -> str:
    """A SOQL-LIKE-safe prefix for matching this account's opportunities.

    Prefers the stored real SFDC name (exact). Falls back — for folders created
    before we captured it — to the first alphanumeric token of the folder, which
    survives punctuation loss (e.g. 'Octus-Fka-Reorg-Research' -> 'Octus') far
    better than the old hyphen->space reconstruction, which never matched a name
    like 'Octus (fka Reorg Research)'."""
    real = _read_sfdc_name(account)
    if real:
        return soql.soql_like_prefix(real)
    first = next((p for p in re.split(r"[^A-Za-z0-9]+", account) if p), account)
    return soql.soql_like_prefix(first)


# ---------------------------------------------------------------------------
# Per-member preferences (e.g. which AEs to pull accounts for). Small JSON file
# per member, mirroring the .owner/.archived single-purpose-file pattern.
# ---------------------------------------------------------------------------
def _member_prefs_file(member_id: str) -> Path:
    return WEBAPP_DIR / ".member-prefs" / f"{_safe(member_id)}.json"


def _read_member_prefs(member_id: str) -> dict:
    f = _member_prefs_file(member_id)
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text()) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_member_prefs(member_id: str, prefs: dict) -> None:
    f = _member_prefs_file(member_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(prefs, indent=2))


def _account_meta(account_dir: Path) -> dict:
    """Lightweight, filesystem-only card metadata: last-updated + output count.
    Counts BOTH the legacy account-level outputs/ AND every per-opportunity
    outputs/ folder, so the account total = the sum across its opportunities."""
    count = 0
    latest = 0.0
    # candidate output roots: account-level + each opportunity's outputs/
    roots = [account_dir / "outputs"]
    opp_dir = account_dir / "opportunities"
    if opp_dir.exists():
        roots += [d / "outputs" for d in opp_dir.iterdir() if d.is_dir()]
    for outputs_dir in roots:
        if not outputs_dir.exists():
            continue
        for f in outputs_dir.rglob("*.md"):
            if any(part.startswith(".") for part in f.relative_to(outputs_dir).parts):
                continue  # skip hidden dirs like .runs (job-result cache, not outputs)
            count += 1
            latest = max(latest, f.stat().st_mtime)
    return {
        "output_count": count,
        "last_updated": (
            datetime.fromtimestamp(latest, tz=timezone.utc).strftime("%Y-%m-%d") if latest else None
        ),
        "last_updated_ts": latest or None,
    }


def accounts_for_member(member_id: str) -> dict:
    """Accounts owned by this member, plus unowned ones — split into active and archived.
    Each account is enriched with filesystem card metadata (last-updated, output count)."""
    all_accounts = list_accounts()
    visible = [a for a in all_accounts if a["owner"] == member_id or a["owner"] is None]
    for a in visible:
        a.update(_account_meta(CUSTOMERS_DIR / a["name"]))
    # owned first, then unowned; within each, most-recently-updated first
    visible.sort(key=lambda a: (a["owner"] != member_id, -(a["last_updated_ts"] or 0), a["name"].lower()))
    return {
        "active": [a for a in visible if not a["archived"]],
        "archived": [a for a in visible if a["archived"]],
    }


def _slug(name: str) -> str:
    """Filesystem-safe slug for an opportunity name."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-")
    return s[:80] or "opportunity"


async def sfdc_opportunities(account: str) -> list[dict]:
    """All SFDC opportunities for an account (not just the 'best' one).
    Each: name, stage, stage_num, amount, close_date, type, is_closed, ae, slug.
    Best-effort; returns [] if SFDC unavailable."""
    sf = _sf_config()
    if not sf.get("enabled", True):
        return []
    alias = sf.get("org_alias", "airbyte-prod")
    like = _sfdc_like_prefix(account)
    soql = (
        "SELECT Name, StageName, Stage_Number__c, Amount, CloseDate, Type, "
        "IsClosed, Owner.Name "
        f"FROM Opportunity WHERE Account.Name LIKE '{like}%' ORDER BY CloseDate DESC"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "sf", "data", "query", "--query", soql, "--target-org", alias, "--json",
            cwd=str(WORKSPACE),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        if proc.returncode != 0:
            return []
        import json as _json
        records = _json.loads(out).get("result", {}).get("records", [])
    except Exception:
        return []
    opps = []
    for r in records:
        name = r.get("Name") or "Opportunity"
        opps.append({
            "name": name,
            "slug": _slug(name),
            "stage": r.get("StageName"),
            "stage_num": r.get("Stage_Number__c"),
            "amount": r.get("Amount"),
            "close_date": r.get("CloseDate"),
            "type": r.get("Type"),
            "is_closed": r.get("IsClosed"),
            "ae": ((r.get("Owner") or {}).get("Name")),
        })
    return opps


# ---------------------------------------------------------------------------
# Salesforce card enrichment — one batched SOQL for all accounts on a page.
# Optional + best-effort: if sf isn't authed/installed, returns {} and the UI
# just omits the stage/amount line. Loaded async so it never blocks the page.
# ---------------------------------------------------------------------------
def _se_config() -> dict:
    """Load the optional `.se-config.yaml` from the workspace root.

    Cached on the function object for the lifetime of the process; the
    config is read once at startup and again only when an explicit reload is
    requested. Callers that need fresh config should call `_se_config_clear()`."""
    cache = getattr(_se_config, "_cache", None)
    if cache is not None:
        return cache
    if SE_CONFIG.exists():
        cfg = yaml.safe_load(SE_CONFIG.read_text()) or {}
    else:
        cfg = {}
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


def _sf_config() -> dict:
    return _se_config().get("salesforce", {}) or {}


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
            *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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


async def sfdc_stage_amount(account_names: list[str]) -> dict:
    """Return {account_name: {stage, stage_num, amount, ae}} for the most relevant
    open (else latest) opportunity per account. One query for all names. Best-effort.
    `ae` is the Salesforce Account/Opportunity Owner. Returns {} on any failure."""
    sf = _sf_config()
    if not sf.get("enabled", True) or not account_names:
        return {}
    alias = sf.get("org_alias", "airbyte-prod")
    likes = " OR ".join(
        f"Account.Name LIKE '{_sfdc_like_prefix(n)}%'" for n in account_names[:50]
    )
    soql = (
        "SELECT Account.Name, StageName, Stage_Number__c, Amount, CloseDate, "
        "IsClosed, Type, Owner.Name "
        f"FROM Opportunity WHERE {likes} ORDER BY CloseDate DESC"
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            "sf", "data", "query", "--query", soql, "--target-org", alias, "--json",
            cwd=str(WORKSPACE),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        if proc.returncode != 0:
            return {}
        import json as _json
        records = _json.loads(out).get("result", {}).get("records", [])
    except Exception:
        return {}

    by_acct: dict[str, dict] = {}
    # Exact map from stored SFDC name → folder (authoritative), plus a lossy
    # token/prefix map for legacy folders with no captured .sfdc-name.
    exact_for = {}
    prefix_for = {}
    for n in account_names:
        real = _read_sfdc_name(n)
        if real:
            exact_for[real.lower()] = n
        prefix_for[_sfdc_like_prefix(n).lower()] = n
    for r in records:
        acct_name = ((r.get("Account") or {}).get("Name") or "").lower()
        folder = exact_for.get(acct_name)
        if not folder:
            folder = next((fn for key, fn in prefix_for.items()
                           if acct_name.startswith(key) or key.startswith(acct_name)), None)
        if not folder:
            continue
        cand = {
            "stage": r.get("StageName"),
            "stage_num": r.get("Stage_Number__c"),
            "amount": r.get("Amount"),
            "ae": ((r.get("Owner") or {}).get("Name")),
            "type": r.get("Type"),
            "close_date": r.get("CloseDate"),
            "is_closed": r.get("IsClosed"),
            "open": not r.get("IsClosed"),
            "renewal": (r.get("Type") == "Renewal"),
        }
        cur = by_acct.get(folder)
        def score(c): return (2 if c["open"] and not c["renewal"] else (1 if c["open"] else 0))
        if cur is None or score(cand) > score(cur):
            by_acct[folder] = cand
    return {
        k: {"stage": v["stage"], "stage_num": v["stage_num"], "amount": v["amount"],
            "ae": v["ae"], "type": v["type"], "close_date": v["close_date"], "is_closed": v["is_closed"]}
        for k, v in by_acct.items()
    }


# ---------------------------------------------------------------------------
# Auto-populate accounts from Salesforce.
#
# Airbyte SFDC model: the AE owns the Opportunity (Owner.Name); the SE is tagged
# separately via the custom SE_Name__c field. So "my accounts" for an SE means
# opps where SE_Name__c = me OR Owner.Name is one of my AEs — restricted to open
# opps with a future close date. The AE list is derived live from SFDC (not a
# static config), so it can never go stale.
# ---------------------------------------------------------------------------
async def _sf_query(soql: str) -> list[dict] | None:
    """Run a SOQL query via the `sf` CLI. Returns records, or None on any
    failure (not-authed, disabled, timeout). Mirrors the inline pattern used by
    sfdc_opportunities/sfdc_stage_amount so all SFDC access goes through `sf`."""
    sf = _sf_config()
    if not sf.get("enabled", True):
        return None
    alias = sf.get("org_alias", "airbyte-prod")
    try:
        proc = await asyncio.create_subprocess_exec(
            "sf", "data", "query", "--query", soql, "--target-org", alias, "--json",
            cwd=str(WORKSPACE),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
        if proc.returncode != 0:
            return None
        return json.loads(out).get("result", {}).get("records", [])
    except Exception:
        return None


def _sf_quote(value: str) -> str:
    """Escape `value` for use as a SOQL single-quoted string literal."""
    return soql.soql_string_literal(value or "")


async def sfdc_list_aes(member_id: str) -> list[str]:
    """All distinct AE names (Opportunity.Owner.Name) on open, future-dated opps
    org-wide, so the SE can pick any AE to pull accounts for. `member_id` is kept
    for the endpoint's 404 guard. Best-effort []."""
    if not member_by_id(member_id):
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    soql = (
        "SELECT Owner.Name FROM Opportunity "
        f"WHERE IsClosed = false AND CloseDate >= {today} "
        "ORDER BY Owner.Name"
    )
    records = await _sf_query(soql)
    if not records:
        return []
    aes = {((r.get("Owner") or {}).get("Name") or "").strip() for r in records}
    return sorted(a for a in aes if a)


async def sfdc_accounts_for_member(member_id: str, ae_names: list[str]) -> dict:
    """Open, future-dated opportunities where SE_Name__c = this member OR
    Owner.Name is one of `ae_names`. Deduped to the best opp per account and
    split into new_business / renewals. Best-effort: {} buckets on failure."""
    member = member_by_id(member_id)
    if not member:
        return {"new_business": [], "renewals": []}
    name = _sf_quote(member.get("name", ""))
    today = datetime.now().strftime("%Y-%m-%d")
    clauses = [f"SE_Name__c = '{name}'"]
    quoted_aes = [f"'{_sf_quote(a)}'" for a in ae_names if a]
    if quoted_aes:
        clauses.append(f"Owner.Name IN ({', '.join(quoted_aes)})")
    where_owner = " OR ".join(clauses)
    soql = (
        "SELECT Account.Name, Amount, StageName, Stage_Number__c, CloseDate, "
        "Type, Owner.Name, SE_Name__c FROM Opportunity "
        f"WHERE IsClosed = false AND CloseDate >= {today} "
        f"AND ({where_owner}) ORDER BY Account.Name"
    )
    records = await _sf_query(soql)
    if not records:
        return {"new_business": [], "renewals": []}

    # dedupe to the best opp per account (prefer open non-renewal, same score()
    # idea as sfdc_stage_amount).
    def score(rec: dict) -> int:
        return 1 if rec.get("Type") != "Renewal" else 0

    by_acct: dict[str, dict] = {}
    for r in records:
        acct = ((r.get("Account") or {}).get("Name") or "").strip()
        if not acct:
            continue
        cur = by_acct.get(acct)
        if cur is None or score(r) > score(cur):
            by_acct[acct] = r

    new_business, renewals = [], []
    for acct, r in sorted(by_acct.items()):
        folder = _titlecase_folder(acct)
        renewal = (r.get("Type") == "Renewal")
        item = {
            "name": folder,
            "account_name": acct,
            "amount": r.get("Amount"),
            "stage": r.get("StageName"),
            "stage_num": r.get("Stage_Number__c"),
            "close_date": r.get("CloseDate"),
            "type": r.get("Type"),
            "ae": ((r.get("Owner") or {}).get("Name")),
            "se": r.get("SE_Name__c"),
            "renewal": renewal,
            "exists": (CUSTOMERS_DIR / folder).exists(),
        }
        (renewals if renewal else new_business).append(item)
    return {"new_business": new_business, "renewals": renewals}


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="SE Skills — Local Hub")
app.include_router(jobs_router)


@app.get("/api/members")
def api_members():
    return load_team()


class CreateMember(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None


@app.post("/api/members")
def api_create_member(body: CreateMember):
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    members = load_team()
    existing = {m.get("id") for m in members}
    mid = _member_id_from_name(name, existing)
    member = {"id": mid, "name": name, "email": (body.email or "").strip(), "role": (body.role or "Solutions Engineer").strip()}
    members.append(member)
    save_team(members)
    return member


@app.get("/api/members/{member_id}/accounts")
def api_member_accounts(member_id: str):
    if not member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    return accounts_for_member(member_id)


@app.post("/api/sfdc/stage-amount")
async def api_sfdc_stage_amount(body: dict):
    """Batched SFDC stage+amount for a list of account names. Best-effort; the UI
    calls this after rendering cards and fills in the line if it resolves."""
    names = body.get("accounts", [])
    if not isinstance(names, list):
        raise HTTPException(400, "accounts must be a list")
    safe_names = [_safe(n) for n in names if isinstance(n, str)]
    return await sfdc_stage_amount(safe_names)


class CreateAccount(BaseModel):
    name: str
    owner: str | None = None
    sfdc_name: str | None = None  # true SFDC Account.Name, for exact opp matching


@app.post("/api/accounts")
def api_create_account(body: CreateAccount):
    folder = _titlecase_folder(_safe(body.name))
    if not folder:
        raise HTTPException(400, "Empty account name")
    acc_dir = CUSTOMERS_DIR / folder
    created = not acc_dir.exists()
    (acc_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (acc_dir / "raw").mkdir(parents=True, exist_ok=True)
    if body.owner:
        _owner_file(acc_dir).write_text(_safe(body.owner))
    # Capture the real SFDC name so opp lookups match punctuated names exactly.
    if body.sfdc_name and body.sfdc_name.strip():
        _sfdc_name_file(acc_dir).write_text(body.sfdc_name.strip())
    return {"name": folder, "created": created, "owner": body.owner}


# ---------------------------------------------------------------------------
# Auto-populate accounts from Salesforce — endpoints
# ---------------------------------------------------------------------------
@app.get("/api/members/{member_id}/sfdc-aes")
async def api_sfdc_aes(member_id: str):
    """AE names (from live SFDC) selectable for this member's account pull,
    plus the member's previously-saved selection."""
    if not member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    aes = await sfdc_list_aes(member_id)
    selected = _read_member_prefs(member_id).get("selected_aes", [])
    return {"aes": aes, "selected": selected}


class SelectedAes(BaseModel):
    selected: list[str] = []


@app.post("/api/members/{member_id}/sfdc-aes")
def api_save_sfdc_aes(member_id: str, body: SelectedAes):
    """Persist which AEs this member wants to pull accounts for."""
    if not member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    prefs = _read_member_prefs(member_id)
    prefs["selected_aes"] = [a for a in body.selected if isinstance(a, str) and a.strip()]
    _write_member_prefs(member_id, prefs)
    return {"ok": True, "selected": prefs["selected_aes"]}


class PullAccounts(BaseModel):
    aes: list[str] = []


@app.post("/api/members/{member_id}/sfdc-accounts")
async def api_sfdc_accounts(member_id: str, body: PullAccounts):
    """Open, future-dated opps for this member, split new_business / renewals."""
    if not member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    aes = [a for a in body.aes if isinstance(a, str) and a.strip()]
    return await sfdc_accounts_for_member(member_id, aes)


class BulkCreateAccounts(BaseModel):
    accounts: list[CreateAccount]


@app.post("/api/bulk-create-accounts")
def api_bulk_create_accounts(body: BulkCreateAccounts):
    """Create several account folders at once (from the SFDC pull), reusing the
    same folder/.owner creation as the single-create endpoint."""
    results = []
    for acc in body.accounts:
        try:
            r = api_create_account(acc)
            results.append({**r, "ok": True})
        except HTTPException as e:
            results.append({"name": acc.name, "ok": False, "error": e.detail})
    return {"created": sum(1 for r in results if r.get("ok") and r.get("created")), "results": results}


@app.get("/api/accounts/{account}")
def api_account(account: str):
    """Lightweight account meta — used by the frontend to build breadcrumbs
    (Team → owner → account → …)."""
    account = _safe(account)
    acc_dir = CUSTOMERS_DIR / account
    if not acc_dir.exists():
        raise HTTPException(404, "Unknown account")
    return {"name": account, "owner": _read_owner(acc_dir)}


@app.get("/api/accounts/{account}/outputs")
def api_outputs(account: str, opp: str | None = None):
    account = _safe(account)
    if opp:
        opp = _safe(opp)  # opp slug is also path-segment safe
    return output_service.list_outputs(account, opp)


@app.get("/api/accounts/{account}/opportunities")
async def api_opportunities(account: str):
    """SFDC opportunities for an account + per-opp local output counts.
    Falls back to a single synthetic 'General' opp if SFDC is unavailable so
    the account is still usable offline."""
    account = _safe(account)
    opps = await sfdc_opportunities(account)
    if not opps:
        # offline / no SFDC: present one default opportunity bucket
        opps = [{"name": "General", "slug": "general", "stage": None, "stage_num": None,
                 "amount": None, "close_date": None, "type": None, "is_closed": None, "ae": None}]
    # attach local output counts per opp
    for o in opps:
        odir = CUSTOMERS_DIR / account / "opportunities" / o["slug"] / "outputs"
        cnt = len(list(odir.rglob("*.md"))) if odir.exists() else 0
        o["output_count"] = cnt
    return opps


class SetOwner(BaseModel):
    owner: str


@app.post("/api/accounts/{account}/owner")
def api_set_owner(account: str, body: SetOwner):
    """Claim/assign ownership of an account (writes the .owner file)."""
    account = _safe(account)
    acc_dir = CUSTOMERS_DIR / account
    if not acc_dir.is_dir():
        raise HTTPException(404, "Unknown account")
    _owner_file(acc_dir).write_text(_safe(body.owner))
    return {"name": account, "owner": body.owner}


@app.post("/api/accounts/{account}/archive")
def api_archive(account: str):
    account = _safe(account)
    acc_dir = CUSTOMERS_DIR / account
    if not acc_dir.is_dir():
        raise HTTPException(404, "Unknown account")
    # Flag file only — data stays exactly in place, fully reversible.
    _archived_file(acc_dir).write_text(
        datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    return {"name": account, "archived": True}


@app.post("/api/accounts/{account}/unarchive")
def api_unarchive(account: str):
    account = _safe(account)
    acc_dir = CUSTOMERS_DIR / account
    if not acc_dir.is_dir():
        raise HTTPException(404, "Unknown account")
    f = _archived_file(acc_dir)
    if f.exists():
        f.unlink()
    return {"name": account, "archived": False}


import shutil  # noqa: E402


@app.delete("/api/accounts/{account}")
def api_delete_account(account: str):
    """Delete is RECOVERABLE: move the account folder to 01-customers/_trash/
    rather than hard-deleting. Never destroys customer data outright."""
    account = _safe(account)
    acc_dir = CUSTOMERS_DIR / account
    if not acc_dir.is_dir():
        raise HTTPException(404, "Unknown account")
    return _do_delete(account)


def _do_delete(account: str) -> dict:
    acc_dir = CUSTOMERS_DIR / account
    trash = CUSTOMERS_DIR / "_trash"
    trash.mkdir(exist_ok=True)
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = trash / f"{account}__{stamp}"
    shutil.move(str(acc_dir), str(dest))
    return {"name": account, "deleted": True, "trash_id": dest.name}


# ── Bulk actions (multi-select) ───────────────────────────────────────────
class BulkBody(BaseModel):
    accounts: list[str]
    owner: str | None = None  # for transfer / make-owner


@app.post("/api/bulk/{action}")
def api_bulk(action: str, body: BulkBody):
    """Apply an action to many accounts at once.
    action ∈ archive | unarchive | delete | set-owner."""
    names = [_safe(n) for n in body.accounts]
    if not names:
        raise HTTPException(400, "No accounts given")
    results = []
    for name in names:
        acc_dir = CUSTOMERS_DIR / name
        if not acc_dir.is_dir():
            results.append({"name": name, "ok": False, "error": "not found"})
            continue
        try:
            if action == "archive":
                _archived_file(acc_dir).write_text(datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            elif action == "unarchive":
                f = _archived_file(acc_dir)
                if f.exists():
                    f.unlink()
            elif action == "delete":
                _do_delete(name)
            elif action == "set-owner":
                if not body.owner:
                    raise ValueError("owner required for set-owner")
                _owner_file(acc_dir).write_text(_safe(body.owner))
            else:
                raise HTTPException(400, f"Unknown bulk action: {action}")
            results.append({"name": name, "ok": True})
        except HTTPException:
            raise
        except Exception as e:
            results.append({"name": name, "ok": False, "error": str(e)})
    return {"action": action, "owner": body.owner, "results": results,
            "ok": sum(1 for r in results if r["ok"]), "failed": sum(1 for r in results if not r["ok"])}


TRASH_DIR = CUSTOMERS_DIR / "_trash"
_TRASH_ID = re.compile(r"^[A-Za-z0-9 ._-]+__\d{8}-\d{6}$")  # <account>__<stamp>


@app.get("/api/trash")
def api_list_trash():
    """Deleted accounts available to restore."""
    if not TRASH_DIR.exists():
        return []
    out = []
    for d in sorted(TRASH_DIR.iterdir(), reverse=True):
        if not d.is_dir() or "__" not in d.name:
            continue
        orig, _, stamp = d.name.rpartition("__")
        try:
            deleted_at = datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            deleted_at = stamp
        out.append({"trash_id": d.name, "name": orig, "deleted_at": deleted_at})
    return out


@app.post("/api/trash/{trash_id}/restore")
def api_restore(trash_id: str):
    if not _TRASH_ID.match(trash_id):
        raise HTTPException(400, "Invalid trash id")
    src = TRASH_DIR / trash_id
    if not src.is_dir():
        raise HTTPException(404, "Not in trash")
    orig = trash_id.rpartition("__")[0]
    dest = CUSTOMERS_DIR / orig
    if dest.exists():
        raise HTTPException(409, f"An account named {orig} already exists — rename or remove it first.")
    shutil.move(str(src), str(dest))
    return {"name": orig, "restored": True}


class OutputAsk(BaseModel):
    path: str = Field(max_length=500)             # output file, relative to CUSTOMERS_DIR
    question: str = Field(max_length=5_000)
    account: str | None = Field(default=None, max_length=120)
    opportunity: str | None = Field(default=None, max_length=200)


@app.post("/api/output/ask")
async def api_output_ask(body: OutputAsk):
    """Follow-up Q&A against an opened output doc. Quick → Claude API (doc as
    context); deep (codebase/connectors) → claude -p. Mirrors the live ask."""
    target = (CUSTOMERS_DIR / body.path).resolve()
    if not str(target).startswith(str(CUSTOMERS_DIR.resolve())) or not target.is_file():
        raise HTTPException(404, "Not found")
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "Empty question")
    doc = target.read_text()[-16000:]   # tail if very long
    acct = body.account or ""
    deep = any(h in q.lower() for h in _DEEP_HINTS)

    if deep:
        prompt = (
            f"A Solutions Engineer is reviewing this generated document"
            f"{(' for the account ' + repr(acct)) if acct else ''}"
            f"{(', opportunity ' + repr(body.opportunity)) if body.opportunity else ''} and has a follow-up question.\n\n"
            f"=== DOCUMENT ===\n{doc}\n=== END DOCUMENT ===\n\n"
            f"Follow-up question: {q}\n\n"
            f"Answer concisely and practically. If it involves Airbyte connectors, deployment, or the "
            f"codebase, use the relevant SE skills / inspect the repo as needed."
        )
        job_id, persist_warn = await job_service.launch(
            account=acct or "?",
            opp_slug=None,
            skill="output-ask",
            opportunity=body.opportunity,
            sig=("output-ask", body.path, q[:60]),
            prompt=prompt,
            meta={"account": acct or "?", "opp_slug": None, "skill": "output-ask", "opportunity": body.opportunity},
        )
        return {"mode": "deep", "job_id": job_id,
                **({"persistence_warning": persist_warn} if persist_warn else {})}

    api_key = _anthropic_api_key()
    if not api_key:
        return JSONResponse({"mode": "needs_deep",
                             "reason": "No ANTHROPIC_API_KEY for the quick path — re-ask routes to claude -p."})

    from sse_starlette.sse import EventSourceResponse

    async def gen():
        acc = ""
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=api_key)
            system = ("You are a Solutions Engineer's copilot. Answer the follow-up briefly and directly "
                      "from the document provided. If the question needs the Airbyte codebase or a deep "
                      "skill, say so in one line.")
            async with client.messages.stream(
                model=_model_for("quick-ask"), max_tokens=800, system=system,
                messages=[{"role": "user", "content": f"Document:\n\n{doc}\n\nFollow-up question: {q}"}],
            ) as stream:
                async for text in stream.text_stream:
                    acc += text
                    yield {"event": "token", "data": json.dumps({"text": text, "html": md_render.markdown_to_body_html(acc)})}
            yield {"event": "done", "data": "{}"}
        except Exception as e:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"error": security.redact_sensitive(str(e))})}

    return EventSourceResponse(gen())


@app.get("/api/skills")
def api_skills():
    return SKILLS


@app.post("/api/reload")
def api_reload_skills():
    """Re-discover skills and app config from disk without restarting the server.

    Clears the cached `.se-config.yaml` so model/config changes take effect,
    then updates the in-memory skill list and SKILL_IDS used for validation.
    The help endpoint is stateless, so it picks up the new list on the next call."""
    global SKILLS, SKILL_IDS
    _se_config_clear()
    # Eagerly reload so malformed config surfaces as early as possible.
    _se_config()
    SKILLS = discover_skills()
    SKILL_IDS = {s["id"] for s in SKILLS}
    _ALL_SKILL_IDS.update(SKILL_IDS)
    return {"skills": SKILLS, "reloaded": True}


def _find_skill_file(skill_id: str) -> Path | None:
    for base in SKILLS_DIRS:
        f = base / skill_id / "SKILL.md"
        if f.exists():
            return f
    return None


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) for a `---`-delimited YAML frontmatter file."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = yaml.safe_load(text[3:end]) or {}
            return (fm if isinstance(fm, dict) else {}), text[end + 4:]
    return {}, text


def _extract_triggers(description: str) -> list[str]:
    """Pull the quoted trigger phrases out of a skill description (…says "x", "y"…)."""
    return re.findall(r'"([^"]+)"', description or "")


def _section(body: str, *header_keywords: str) -> str | None:
    """Find a markdown section whose heading contains any keyword; return its text (trimmed, capped)."""
    lines = body.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("#"):
            heading = ln.lstrip("#").strip().lower()
            if any(k in heading for k in header_keywords):
                # collect until next heading of same-or-higher level
                level = len(ln) - len(ln.lstrip("#"))
                out = []
                for nxt in lines[i + 1:]:
                    if nxt.startswith("#") and (len(nxt) - len(nxt.lstrip("#"))) <= level:
                        break
                    out.append(nxt)
                txt = "\n".join(out).strip()
                if txt:
                    return txt[:1200]
    return None


# Sales methodologies the skills are built on — detect which a skill uses.
METHODOLOGIES = {
    "MEDDPICC": ["meddpicc", "meddic"],
    "SPIN": ["spin selling", "spin ", "implication question", "need-payoff"],
    "Sandler": ["sandler", "pain funnel", "upfront contract", "negative reverse"],
    "Challenger": ["challenger", "reframe", "rational drowning", "commercial teaching"],
    "Chris Voss (tactical empathy)": ["voss", "mirror", "label", "calibrated question", "accusation"],
    "Command of the Message": ["command of the message", "value framing"],
}

# Skill ids, for detecting cross-skill references in a body.
_ALL_SKILL_IDS = set(SKILL_PRESENTATION.keys())


def _detect_methodologies(text: str) -> list[str]:
    low = text.lower()
    return [name for name, kws in METHODOLOGIES.items() if any(k in low for k in kws)]


def _detect_related_skills(self_id: str, text: str) -> list[str]:
    """Other suite skills this one references (e.g. 'run deal-assessment', 'see prep-call')."""
    found = []
    for sid in _ALL_SKILL_IDS:
        if sid == self_id:
            continue
        # match the bare id as a backticked ref or word boundary
        if re.search(rf"`{re.escape(sid)}`|\b{re.escape(sid)}\b", text):
            found.append(sid)
    return sorted(found)


def _clean_section(txt: str | None) -> str | None:
    """Light-touch cleanup so the help reads as prose, not raw markdown:
    strip leading list dashes/asterisks and heading hashes; collapse blank runs."""
    if not txt:
        return None
    out = []
    for ln in txt.splitlines():
        s = ln.rstrip()
        s = re.sub(r"^\s*#{1,6}\s*", "", s)        # drop heading hashes
        s = re.sub(r"^\s*[-*]\s+", "• ", s)         # list dash/star → bullet
        s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)      # drop bold markers
        s = re.sub(r"`([^`]+)`", r"\1", s)          # drop inline code backticks
        out.append(s)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned or None


def _derive_help(skill_id: str, label: str, blurb: str) -> dict:
    """Build a human-friendly help entry by parsing the skill's SKILL.md live."""
    f = _find_skill_file(skill_id)
    entry = {
        "id": skill_id,
        "label": label,
        "summary": blurb,
        "description": "",
        "triggers": [],
        "methodologies": [],
        "how_it_works": None,
        "related_skills": [],
        "prerequisites": None,
        "data_sources": None,
        "output_location": None,
        "found": False,
    }
    if not f:
        return entry
    fm, body = _parse_frontmatter(f.read_text())
    desc = fm.get("description", "")
    entry["found"] = True
    entry["description"] = desc
    entry["triggers"] = _extract_triggers(desc)

    # Methodology — which sales frameworks this skill applies
    entry["methodologies"] = _detect_methodologies(body)

    # How it works — the SE-craft / "best practices applied" / how-to section
    entry["how_it_works"] = _clean_section(
        _section(body, "se best practices", "how it works", "how to", "the holistic read", "framework")
    )

    # Related skills — other suite skills this one points to
    entry["related_skills"] = _detect_related_skills(skill_id, body)

    # Prerequisites — skills state these in a "Hard Prerequisite" section
    entry["prerequisites"] = _clean_section(
        _section(body, "prerequisite", "requires", "source sufficiency")
    )

    # Data sources — Salesforce Enrichment / Source Freshness / Sources sections
    entry["data_sources"] = _clean_section(
        _section(body, "salesforce enrichment", "sources", "source freshness")
    )

    # Output location — target the auto-save path specifically (under outputs/),
    # not the first 01-customers path (which is often a _transcripts source path).
    if "ephemeral" in body.lower() and re.search(r"saves? only on", body, re.I):
        entry["output_location"] = "Ephemeral — not auto-saved (saves only on request)"
    else:
        m = re.search(r"(~/airbyte-work/01-customers/\S*?/outputs/\S+)", body)
        if m:
            entry["output_location"] = m.group(1).strip("`")

    return entry


@app.get("/api/skills/help")
def api_skills_help():
    """Help doc content, auto-extracted from each skill's SKILL.md so it never drifts."""
    return [_derive_help(s["id"], s["label"], s["blurb"]) for s in SKILLS]


class InvokeBody(BaseModel):
    account: str = Field(max_length=120)
    skill: str | None = Field(default=None, max_length=80)       # a known skill id (dropdown)
    opportunity: str | None = Field(default=None, max_length=200)  # opp name (for context + output scoping)
    opp_slug: str | None = Field(default=None, max_length=120)   # opp folder slug
    extra: str | None = Field(default=None, max_length=10_000)      # free-text appended to the prompt
    freeform: str | None = Field(default=None, max_length=20_000) # full free-text instruction (instead of a dropdown skill)
    override_prerequisites: bool = Field(default=False)             # allow running when the planner reports missing prerequisites
    approve_permissions: bool = Field(default=False)                  # explicit SE approval for write/shell/git actions


# ---------------------------------------------------------------------------
# Skill invocation runs as a BACKGROUND JOB so the HTTP request returns
# immediately. A long-running `claude -p` held the request (and a browser
# connection) for minutes — navigating away abandoned the fetch, orphaned the
# subprocess, and saturated the per-host connection pool so other pages hung.
# Now: POST /api/invoke starts the job + returns a job_id; the run continues
# server-side even if you navigate away; GET /api/jobs/{id} polls for status.
# ---------------------------------------------------------------------------
# The in-memory job registry is owned by services/job_service.py. It is
# instantiated below after the run-persistence helpers it depends on.


def _build_prompt(body: InvokeBody, account: str, out_dir) -> str:
    if body.freeform:
        prompt = body.freeform.strip() + f" (for the account {account}"
        if body.opportunity:
            prompt += f", opportunity '{body.opportunity}'"
        prompt += ".)"
    else:
        prompt = f"Use the {body.skill} skill for {account}."
        if body.opportunity:
            prompt += f" This is for the opportunity '{body.opportunity}'."
        if body.extra:
            prompt += f" Additional context: {body.extra.strip()}"
    if out_dir:
        prompt += (f" IMPORTANT: save any output file under {out_dir}/<skill-name>/ "
                   f"instead of the default account outputs folder.")
    return prompt


# Shared service instances. Routes and skill-invocation handlers use these
# module-level objects so the in-memory registries survive imports/reloads.
output_service = OutputService(
    CUSTOMERS_DIR,
    WORKSPACE,
    WEBAPP_DIR.parent,
    se_config=_se_config,
    safe_name=_safe,
    slug=_slug,
    run_cmd=_run_cmd,
    internal_repo=_internal_repo,
)
feedback_service = FeedbackService(CUSTOMERS_DIR)
job_service = JobService(
    WORKSPACE,
    model_for=_model_for,
    persist_run=output_service.persist_run,
)

# Make the services reachable from route dependencies.
app.state.output_service = output_service
app.state.feedback_service = feedback_service
app.state.job_service = job_service

app.include_router(outputs_router)
app.include_router(feedback_router)
@app.get("/api/plan")
def api_plan(account: str, skill: str, opp_slug: str | None = None):
    """Return the prerequisite plan for a proposed skill invocation.

    The planner uses structured sidecars (STRUCT-003) to check whether the
    selected skill has the transcripts and valid upstream outputs it needs.
    The result always includes `can_override: true` so the UI can offer a
    "Run anyway" path.
    """
    account = _safe(account)
    if skill not in SKILL_IDS:
        raise HTTPException(400, f"Unknown skill: {skill}")
    plan = orchestrator.check_prerequisites(skill, account, _safe(opp_slug) if opp_slug else None, CUSTOMERS_DIR)
    return plan.model_dump()


@app.post("/api/invoke")
async def api_invoke(body: InvokeBody):
    account = _safe(body.account)

    # Determine the opportunity output folder (scope outputs per-opportunity).
    opp_slug = _safe(body.opp_slug) if body.opp_slug else None
    out_dir = None
    if opp_slug:
        out_dir = CUSTOMERS_DIR / account / "opportunities" / opp_slug / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)

    if not body.freeform and body.skill not in SKILL_IDS:
        raise HTTPException(400, f"Unknown skill: {body.skill}")

    # Deterministic prerequisite check. Free-form instructions and explicit
    # overrides skip the planner.
    if not body.freeform and not body.override_prerequisites and body.skill in SKILL_IDS:
        plan = orchestrator.check_prerequisites(body.skill, account, opp_slug, CUSTOMERS_DIR)
        if not plan.ready:
            return JSONResponse({"prerequisites": plan.model_dump(), "blocked": True})

    # Permission approval check. Free-form instructions always require approval
    # because their capabilities are unbounded; known skills are classified by
    # write/shell/git needs and must be explicitly approved before we launch
    # Claude with --permission-mode acceptEdits.
    profile = _permission_profile(body.skill, freeform=bool(body.freeform))
    if profile.requires_approval and not body.approve_permissions:
        return JSONResponse({"permissions": profile.model_dump(), "blocked": True})

    prompt = _build_prompt(body, account, out_dir)

    # Reuse an already-running job for the same (account, opp, skill/freeform)
    # so re-entering the page doesn't kick off a duplicate run.
    sig = (account, opp_slug, body.skill or "freeform", (body.freeform or body.extra or "")[:80])
    reused = job_service.find_reused_job(sig)
    if reused:
        jid, j = reused
        reused_resp = {"job_id": jid, "status": "running", "reused": True}
        if j.get("persistence_warning"):
            reused_resp["persistence_warning"] = j["persistence_warning"]
        return JSONResponse(reused_resp)

    skill_id = body.skill or "freeform"
    job_id, persist_warn = await job_service.launch(
        account=account,
        opp_slug=opp_slug,
        skill=skill_id,
        opportunity=body.opportunity,
        sig=sig,
        prompt=prompt,
        meta={"account": account, "opp_slug": opp_slug, "skill": skill_id, "opportunity": body.opportunity},
    )
    new_resp = {"job_id": job_id, "status": "running", "reused": False}
    if persist_warn:
        new_resp["persistence_warning"] = persist_warn
    return JSONResponse(new_resp)


@app.get("/api/permissions")
def api_permissions(skill: str | None = None, freeform: bool = False):
    """Return the permission profile for a proposed skill invocation.

    Free-form instructions get the broadest profile because their content is
    uncontrolled. Known skills return their SKILL_PERMISSIONS classification;
    unknown skills default to write-only, which is the safe common case.
    """
    if freeform or not skill:
        return _permission_profile(None, freeform=True)
    if skill not in SKILL_IDS:
        raise HTTPException(400, f"Unknown skill: {skill}")
    return _permission_profile(skill)


@app.get("/api/accounts/{account}/last-run")
def api_last_run(account: str, opp_slug: str | None = None):
    """The most recent finished run for an opportunity, read from disk. Lets a
    result survive both navigation and a server restart. 204 if none."""
    account = _safe(account)
    opp_slug = _safe(opp_slug) if opp_slug else None
    rec = output_service.latest_run(account, opp_slug)
    if not rec:
        return Response(status_code=204)  # empty body — see note in /active
    return rec


# ---------------------------------------------------------------------------
# Transitional compatibility wrappers for callers/tests that still import
# job helpers from webapp.app. They delegate to services/job_service.py.
# ---------------------------------------------------------------------------
def api_job(job_id: str) -> dict:
    """Return a single job, including stdout/stderr but not the dedupe signature."""
    job = job_service.get_job(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return {k: v for k, v in job.items() if k != "sig"}


def api_jobs_for(account: str | None = None, opp_slug: str | None = None) -> list[dict]:
    """List jobs, optionally filtered by account and/or opportunity slug."""
    return job_service.list_jobs(account, opp_slug)


async def _save_jobs_snapshot(source_job_id: str | None = None) -> str | None:
    """Persist the jobs snapshot and manage per-job persistence warnings."""
    return await job_service.save_snapshot(source_job_id)


# ---------------------------------------------------------------------------
# Team overview — lightweight aggregation for the landing page
# ---------------------------------------------------------------------------

_LONG_RUNNING_MINUTES = 5
_ATTENTION_FAILURE_HOURS = 24
_STALE_ACTIVITY_DAYS = 7
_MAX_ATTENTION = 10
_MAX_RECENT = 12


def _build_overview(jobs: dict[str, dict]) -> dict:
    """Aggregate filesystem + job state into a calm operational overview.

    Keeps all computation in one pass over the workspace so the landing page
    stays fast for the local SE workspace. Does not compute live reference
    freshness (that remains on the output list); it only reads existing sidecars.
    """
    now = datetime.now(timezone.utc).timestamp()
    members = load_team()
    all_accounts = list_accounts()
    active_accounts = [a for a in all_accounts if not a["archived"]]
    archived_count = len(all_accounts) - len(active_accounts)

    account_meta: dict[str, dict] = {}
    recent_outputs: list[dict] = []
    needs_attention_outputs: list[dict] = []

    recent_outputs, needs_attention_outputs, account_meta = output_service.walk_all_outputs(CUSTOMERS_DIR)
    # Running / finished jobs (shallow-copy values so the snapshot is safe).
    running_jobs = []
    failed_jobs = []
    done_jobs = []
    for jid, j in jobs.items():
        status = j.get("status")
        job_copy = dict(j)
        job_copy["job_id"] = jid
        if status == "running":
            running_jobs.append(job_copy)
        elif status == "error" or (status == "done" and j.get("ok") is False):
            failed_jobs.append(job_copy)
        elif status == "done":
            done_jobs.append(job_copy)

    # Per-account job-driven timestamps.
    for j in [*running_jobs, *failed_jobs, *done_jobs]:
        account = j.get("account")
        if not account:
            continue
        when = j.get("finished_at") or j.get("started_at") or 0
        meta = account_meta.get(account)
        if meta and when > meta["last_updated_ts"]:
            meta["last_updated_ts"] = when

    # Per-member aggregates.
    member_rows = []
    for m in members:
        visible = [a for a in active_accounts if a["owner"] == m["id"] or a["owner"] is None]
        names = [a["name"] for a in visible]
        row = {
            "id": m["id"],
            "name": m["name"],
            "role": m.get("role"),
            "email": m.get("email"),
            "account_count": len(names),
            "output_count": 0,
            "needs_attention": 0,
            "opp_count": 0,
            "running_jobs": 0,
            "recent_failures": 0,
            "last_activity_ts": 0.0,
            "last_output": None,
        }
        last_output_entry = None
        for name in names:
            am = account_meta.get(name)
            if not am:
                continue
            row["output_count"] += am["output_count"]
            row["needs_attention"] += am["needs_attention"]
            row["opp_count"] += am["opp_count"]
            if am["last_updated_ts"] > row["last_activity_ts"]:
                row["last_activity_ts"] = am["last_updated_ts"]
            if am["last_output"] and (last_output_entry is None or am["last_output"]["mtime"] > last_output_entry["mtime"]):
                last_output_entry = am["last_output"]

        for j in running_jobs:
            if j.get("account") in names:
                row["running_jobs"] += 1
                started = j.get("started_at") or 0
                if started > row["last_activity_ts"]:
                    row["last_activity_ts"] = started
        for j in failed_jobs:
            if j.get("account") in names:
                finished = j.get("finished_at")
                if finished is None or now - finished <= _ATTENTION_FAILURE_HOURS * 3600:
                    row["recent_failures"] += 1
                if finished and finished > row["last_activity_ts"]:
                    row["last_activity_ts"] = finished
        for j in done_jobs:
            if j.get("account") in names:
                finished = j.get("finished_at") or 0
                if finished > row["last_activity_ts"]:
                    row["last_activity_ts"] = finished
        row["last_output"] = last_output_entry
        member_rows.append(row)

    # Team summary.
    total_outputs = sum(m["output_count"] for m in account_meta.values())
    total_needs_attention = sum(m["needs_attention"] for m in account_meta.values())
    recent_failure_count = sum(
        1 for j in failed_jobs
        if j.get("finished_at") is None or now - j["finished_at"] <= _ATTENTION_FAILURE_HOURS * 3600
    )
    all_activity_ts = (
        [m["last_updated_ts"] for m in account_meta.values()] +
        [j.get("started_at") or 0 for j in running_jobs] +
        [j.get("finished_at") or 0 for j in [*failed_jobs, *done_jobs]]
    )
    global_last_activity = max(all_activity_ts) if all_activity_ts else 0.0

    summary = {
        "members": len(members),
        "active_accounts": len(active_accounts),
        "archived_accounts": archived_count,
        "opportunities": sum(
            account_meta[a["name"]]["opp_count"]
            for a in active_accounts
            if a["name"] in account_meta
        ),
        "outputs": total_outputs,
        "running_jobs": len(running_jobs),
        "recent_failures": recent_failure_count,
        "needs_attention": total_needs_attention,
        "last_activity": global_last_activity,
    }

    # Attention-needed items.
    attention: list[dict] = []
    for j in running_jobs:
        account = j.get("account", "unknown")
        opp_slug = j.get("opp_slug")
        opp_name = j.get("opportunity") or (opp_slug.capitalize() if opp_slug else account)
        started = j.get("started_at")
        duration_min = int((now - started) / 60) if started else None
        long_running = started and duration_min is not None and duration_min >= _LONG_RUNNING_MINUTES
        attention.append({
            "type": "long-running" if long_running else "running",
            "level": "warn" if long_running else "info",
            "skill": j.get("skill", "skill"),
            "account": account,
            "opp_slug": opp_slug,
            "opp_name": opp_name,
            "when": started or now,
            "duration_min": duration_min,
            "job_id": j.get("job_id"),
            "href": (
                f"#/opp/{urllib.parse.quote(account)}/{urllib.parse.quote(opp_slug or '')}/{urllib.parse.quote(opp_name)}"
                if opp_slug else f"#/account/{urllib.parse.quote(account)}"
            ),
        })

    for j in failed_jobs:
        finished = j.get("finished_at")
        if finished and now - finished > _ATTENTION_FAILURE_HOURS * 3600:
            continue
        account = j.get("account", "unknown")
        opp_slug = j.get("opp_slug")
        opp_name = j.get("opportunity") or (opp_slug.capitalize() if opp_slug else account)
        attention.append({
            "type": "failure",
            "level": "error",
            "skill": j.get("skill", "skill"),
            "account": account,
            "opp_slug": opp_slug,
            "opp_name": opp_name,
            "when": finished or now,
            "error": (j.get("stderr") or "").splitlines()[0][:120] if j.get("stderr") else "",
            "job_id": j.get("job_id"),
            "href": (
                f"#/opp/{urllib.parse.quote(account)}/{urllib.parse.quote(opp_slug or '')}/{urllib.parse.quote(opp_name)}"
                if opp_slug else f"#/account/{urllib.parse.quote(account)}"
            ),
        })

    for out in sorted(needs_attention_outputs, key=lambda x: x["mtime"], reverse=True)[:_MAX_ATTENTION]:
        # Validation issues are more urgent than review queue items; stale and
        # incomplete outputs still need attention but are less severe than invalid.
        if out["type"] == "review":
            level = "warn"
        else:
            level = "error" if out["validation_status"] == "invalid" else "warn"
        attention.append({
            "type": out["type"],
            "level": level,
            "skill": out["skill"],
            "account": out["account"],
            "opp_slug": out["opp_slug"],
            "opp_name": out["opp_name"],
            "filename": out["filename"],
            "when": out["mtime"],
            "status": out["status"],
            "validation_status": out["validation_status"],
            "review_status": out["review_status"],
            "href": output_service.output_href(out["account"], out["opp_slug"], out["opp_name"], out["path"]),
        })

    for a in active_accounts:
        am = account_meta.get(a["name"])
        if not am:
            continue
        last = am["last_updated_ts"]
        if last and now - last > _STALE_ACTIVITY_DAYS * 24 * 3600 and am["output_count"] > 0:
            attention.append({
                "type": "stale",
                "level": "info",
                "account": a["name"],
                "when": last,
                "href": f"#/account/{urllib.parse.quote(a['name'])}",
            })

    attention.sort(key=lambda x: (0 if x["level"] == "error" else (1 if x["level"] == "warn" else 2), -x["when"]))
    attention = attention[:_MAX_ATTENTION]

    # Recent activity: outputs + job events.
    recent: list[dict] = []
    for out in recent_outputs:
        recent.append({
            "type": "output",
            "skill": out["skill"],
            "account": out["account"],
            "opp_slug": out["opp_slug"],
            "opp_name": out["opp_name"],
            "filename": out["filename"],
            "when": out["mtime"],
            "needs_attention": out["needs_attention"],
            "href": output_service.output_href(out["account"], out["opp_slug"], out["opp_name"], out["path"]),
        })

    for j in [*running_jobs, *failed_jobs, *done_jobs]:
        account = j.get("account", "unknown")
        opp_slug = j.get("opp_slug")
        opp_name = j.get("opportunity") or (opp_slug.capitalize() if opp_slug else account)
        status = j.get("status")
        ok = j.get("ok")
        if status == "running":
            when = j.get("started_at") or now
            event_type = "job_started"
        elif status == "error" or ok is False:
            when = j.get("finished_at") or now
            event_type = "job_recovered" if "Server restarted" in (j.get("stderr") or "") else "job_error"
        else:
            when = j.get("finished_at") or now
            event_type = "job_done"
        recent.append({
            "type": event_type,
            "skill": j.get("skill", "skill"),
            "account": account,
            "opp_slug": opp_slug,
            "opp_name": opp_name,
            "when": when,
            "ok": j.get("ok"),
            "job_id": j.get("job_id"),
            "href": (
                f"#/opp/{urllib.parse.quote(account)}/{urllib.parse.quote(opp_slug or '')}/{urllib.parse.quote(opp_name)}"
                if opp_slug else f"#/account/{urllib.parse.quote(account)}"
            ),
        })

    recent.sort(key=lambda x: x["when"], reverse=True)
    recent = recent[:_MAX_RECENT]

    empty = {
        "members": not members,
        "accounts": not active_accounts,
        "attention": not attention,
        "recent": not recent,
    }

    return {
        "summary": summary,
        "attention": attention,
        "recent": recent,
        "members": member_rows,
        "empty": empty,
    }


@app.get("/api/overview")
async def api_overview():
    """Operational overview for the team landing page.

    Runs the aggregation in a thread pool because it walks the filesystem and
    may briefly block the event loop. A failure anywhere in the aggregation
    returns a safe empty fallback rather than a 500 so the landing page still
    renders.
    """
    jobs_snapshot = {k: dict(v) for k, v in job_service.jobs.items()}
    try:
        return await asyncio.to_thread(_build_overview, jobs_snapshot)
    except (OSError, ValueError, TypeError) as e:
        logger.warning("Overview aggregation failed: %s", type(e).__name__)
        return {
            "summary": {
                "members": 0, "active_accounts": 0, "archived_accounts": 0,
                "opportunities": 0, "outputs": 0, "running_jobs": 0,
                "recent_failures": 0, "needs_attention": 0, "last_activity": 0.0,
            },
            "attention": [],
            "recent": [],
            "members": [],
            "empty": {"members": True, "accounts": True, "attention": True, "recent": True},
        }


# ---------------------------------------------------------------------------
# Live Transcribe — capture Mac audio, transcribe with faster-whisper, stream
# segments to the browser over SSE, and answer questions against the rolling
# transcript (quick → Claude API; deep → claude -p via the job system).
#
# Audio libs (sounddevice, faster_whisper, numpy) and the Claude SDK are
# imported LAZILY inside the session so the rest of the app boots even if the
# user hasn't installed PortAudio/BlackHole yet.
# ---------------------------------------------------------------------------
SESSIONS: dict[str, "LiveSession"] = {}

TARGET_SR = 16000          # whisper wants 16k mono
WINDOW_SEC = 5.0           # transcribe ~5s windows
SILENCE_RMS = 0.004        # skip near-silent windows
WHISPER_MODEL = os.environ.get("SE_WHISPER_MODEL", "small")  # tiny/base/small/medium

_WHISPER = None            # lazily-loaded shared model


def _get_whisper():
    global _WHISPER
    if _WHISPER is None:
        from faster_whisper import WhisperModel  # lazy
        _WHISPER = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    return _WHISPER


class _Channel:
    """One capture stream (sounddevice) → 16k mono windows → faster-whisper.
    `label` is the speaker tag for segments from this channel (e.g. You/Call)."""

    def __init__(self, device_index, label, on_segment):
        import sounddevice as sd  # lazy
        import numpy as np
        self.np = np
        self.label = label
        self.on_segment = on_segment      # callback(label, text) — runs in worker thread
        self._stop = threading.Event()
        self._q: "queue.Queue" = queue.Queue()
        info = sd.query_devices(device_index, "input")
        self.src_sr = int(info["default_samplerate"])
        self.in_ch = info["max_input_channels"]

        def cb(indata, frames, time_info, status):  # audio thread
            mono = indata.mean(axis=1) if indata.ndim > 1 and indata.shape[1] > 1 else indata.reshape(-1)
            if self.src_sr != TARGET_SR:
                n_out = max(1, int(len(mono) * TARGET_SR / self.src_sr))
                mono = np.interp(np.linspace(0, len(mono), n_out, endpoint=False),
                                 np.arange(len(mono)), mono).astype(np.float32)
            self._q.put(mono.astype(np.float32))

        self._stream = sd.InputStream(device=device_index, channels=self.in_ch,
                                      samplerate=self.src_sr, dtype="float32",
                                      callback=cb, blocksize=int(self.src_sr * 0.1))
        self._worker = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._stream.start()
        self._worker.start()

    def _run(self):
        import collections
        model = _get_whisper()
        buf = collections.deque(); buflen = 0
        need = int(TARGET_SR * WINDOW_SEC)
        while not self._stop.is_set() or not self._q.empty():
            try:
                buf.append(self._q.get(timeout=0.3)); buflen += len(buf[-1])
            except queue.Empty:
                pass
            if buflen >= need:
                window = self.np.concatenate(list(buf)); buf.clear(); buflen = 0
                if float(self.np.sqrt(self.np.mean(window ** 2))) < SILENCE_RMS:
                    continue
                try:
                    segs, _ = model.transcribe(window, language="en", beam_size=1)
                    text = " ".join(s.text.strip() for s in segs).strip()
                except Exception:
                    text = ""
                if text:
                    self.on_segment(self.label, text)

    def stop(self):
        self._stop.set()
        try:
            self._stream.stop(); self._stream.close()
        except Exception:
            pass


# Echo de-dupe: when capturing with open speakers, the mic ("You") also hears
# the call's audio ("Call"), producing a near-duplicate line ~1s apart. We hold
# each "You" segment briefly; if a near-identical "Call" segment shows up in the
# window, the "You" line is an echo and is suppressed. "Call" emits immediately.
ECHO_HOLD_SEC = 2.5
ECHO_SIM = 0.5   # token-overlap to call two near-simultaneous lines an echo.
                 # 0.5 catches garbled echoes (the two channels transcribe a bit
                 # differently) while real back-and-forth dialogue scores ~0.1–0.3.


def _text_similarity(a: str, b: str) -> float:
    """Jaccard overlap of lowercased word sets — cheap, good enough for echoes."""
    wa = set(re.findall(r"[a-z0-9]+", (a or "").lower()))
    wb = set(re.findall(r"[a-z0-9]+", (b or "").lower()))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


class LiveSession:
    def __init__(self, account, opp_slug, mic_device, call_device,
                 mic_label="You", call_label="Call", opportunity=None,
                 recovered=False, segments=None, started_at=None, session_id=None):
        self.session_id = session_id
        self.account = account
        self.opp_slug = opp_slug
        self.opportunity = opportunity
        self.mic_device = mic_device
        self.call_device = call_device
        self.mic_label = (mic_label or "You").strip() or "You"
        self.call_label = (call_label or "Call").strip() or "Call"
        self.labeled = call_device is not None
        self.recovered = recovered
        self.ended = recovered  # recovered sessions are no longer capturing audio
        self.started_at = started_at or datetime.now(timezone.utc)
        self.persistence_warning: str | None = None
        self.segments: list[dict] = list(segments or [])
        self.queue: asyncio.Queue = asyncio.Queue()
        self._lock = threading.Lock()
        self._recent_call: deque = deque(maxlen=12)   # (monotonic_ts, text) for echo matching
        self._pending_you: list = []                  # held mic segs awaiting echo check
        self.channels: list[_Channel] = []
        if recovered:
            try:
                self.loop = asyncio.get_running_loop()
            except RuntimeError:
                self.loop = None
        else:
            self.loop = asyncio.get_running_loop()
            self.__post_channels(mic_device, call_device)

    def _emit(self, seg):
        self.segments.append(seg)
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, seg)
        self._persist()

    def _persist(self):
        if self.session_id:
            ok = persistence.save_session(self.to_dict(), WORKSPACE)
            if ok:
                self.persistence_warning = None
            else:
                self.persistence_warning = "Live transcript will not survive a server restart because state could not be saved."

    def _flush_pending_you(self, now):
        """Emit any held mic segments older than the hold window that were not
        matched by a call echo."""
        still = []
        for ts, seg in self._pending_you:
            if now - ts >= ECHO_HOLD_SEC:
                self._emit(seg)
            else:
                still.append((ts, seg))
        self._pending_you = still

    def _on_segment(self, label, text):          # called from worker threads
        import time
        now = time.monotonic()
        seg = {"t": datetime.now(timezone.utc).strftime("%H:%M:%S"), "speaker": label, "text": text}
        with self._lock:
            self._flush_pending_you(now)
            if not self.labeled:
                self._emit(seg); return
            if label == self.call_label:
                # drop any held mic segment that this call line echoes
                self._pending_you = [
                    (ts, s) for (ts, s) in self._pending_you
                    if _text_similarity(s["text"], text) < ECHO_SIM
                ]
                self._recent_call.append((now, text))
                self._emit(seg)
            else:  # mic label — suppress if it echoes a recent call; else hold briefly
                if any(now - cts <= ECHO_HOLD_SEC and _text_similarity(text, ctext) >= ECHO_SIM
                       for cts, ctext in self._recent_call):
                    return  # echo of the call audio — drop
                self._pending_you.append((now, seg))

    def __post_channels(self, mic_device, call_device):
        if self.labeled:
            self.channels.append(_Channel(mic_device, self.mic_label, self._on_segment))
            self.channels.append(_Channel(call_device, self.call_label, self._on_segment))
        else:
            self.channels.append(_Channel(mic_device, self.mic_label, self._on_segment))

    def start(self):
        for c in self.channels:
            c.start()

    def stop(self):
        for c in self.channels:
            c.stop()
        # flush any held mic segments that never got an echo match
        with self._lock:
            for _ts, seg in self._pending_you:
                self._emit(seg)
            self._pending_you = []

    def transcript_text(self) -> str:
        header = f"# Live transcript — {self.account} — {self.started_at.astimezone().strftime('%B %d, %Y %H:%M')}\n"
        header += f"# mic-label: {self.mic_label}\n"
        if self.labeled:
            header += f"# call-label: {self.call_label}\n"
        header += "\n"
        lines = []
        for s in self.segments:
            who = f"{s['speaker']}: " if s["speaker"] else ""
            lines.append(f"[{s['t']}] {who}{s['text']}")
        return header + "\n".join(lines) + "\n"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "account": self.account,
            "opp_slug": self.opp_slug,
            "opportunity": self.opportunity,
            "mic_device": self.mic_device,
            "call_device": self.call_device,
            "mic_label": self.mic_label,
            "call_label": self.call_label,
            "labeled": self.labeled,
            "recovered": self.recovered,
            "ended": self.ended,
            "started_at": self.started_at.timestamp(),
            "segments": list(self.segments),
        }

    @classmethod
    def from_state(cls, data: dict) -> "LiveSession":
        started_at = datetime.fromtimestamp(data["started_at"], tz=timezone.utc)
        return cls(
            account=data["account"],
            opp_slug=data.get("opp_slug"),
            mic_device=data.get("mic_device", 0),
            call_device=data.get("call_device"),
            mic_label=data.get("mic_label", "You"),
            call_label=data.get("call_label", "Call"),
            opportunity=data.get("opportunity"),
            recovered=True,
            segments=data.get("segments", []),
            started_at=started_at,
            session_id=data.get("session_id"),
        )


@app.get("/api/audio-devices")
def api_audio_devices():
    """Input devices for the mic/call pickers. Flags BlackHole presence."""
    try:
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, security.redact_sensitive(f"Audio capture unavailable: {e}. Run `brew install portaudio` and reinstall deps."))
    devices, has_blackhole = [], False
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            name = d["name"]
            if "blackhole" in name.lower() or "aggregate" in name.lower():
                has_blackhole = True
            devices.append({"index": i, "name": name,
                            "channels": d["max_input_channels"],
                            "sample_rate": int(d["default_samplerate"])})
    return {"devices": devices, "has_blackhole": has_blackhole, "model": WHISPER_MODEL}


class StartLive(BaseModel):
    account: str = Field(max_length=120)
    opp_slug: str | None = Field(default=None, max_length=120)
    opportunity: str | None = Field(default=None, max_length=200)
    mic_device: int
    call_device: int | None = None
    mic_label: str | None = Field(default="You", max_length=80)
    call_label: str | None = Field(default="Call", max_length=80)


@app.post("/api/transcribe/start")
async def api_transcribe_start(body: StartLive):
    account = _safe(body.account)
    opp_slug = _safe(body.opp_slug) if body.opp_slug else None
    try:
        sess = LiveSession(
            account, opp_slug, body.mic_device, body.call_device,
            mic_label=body.mic_label or "You",
            call_label=body.call_label or "Call",
            opportunity=body.opportunity,
        )
        sess.start()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, security.redact_sensitive(f"Could not start capture: {e}"))
    sid = uuid.uuid4().hex[:12]
    sess.session_id = sid
    SESSIONS[sid] = sess
    session_ok = await asyncio.to_thread(persistence.save_session, sess.to_dict(), WORKSPACE)
    if not session_ok:
        sess.persistence_warning = "Live transcript will not survive a server restart because state could not be saved."
    return {"session_id": sid, "labeled": sess.labeled,
            "mic_label": sess.mic_label, "call_label": sess.call_label,
            **({"persistence_warning": sess.persistence_warning} if sess.persistence_warning else {})}


@app.get("/api/transcribe/active")
def api_transcribe_active(account: str, opp_slug: str | None = None):
    """If a live session is active or recovered for this opportunity, return it
    so the page can reconnect or save it. 204 if none."""
    account = _safe(account)
    opp_slug = _safe(opp_slug) if opp_slug else None
    for sid, sess in SESSIONS.items():
        if sess.account == account and sess.opp_slug == opp_slug:
            return {"session_id": sid, "labeled": sess.labeled,
                    "started_at": sess.started_at.timestamp(),
                    "segments": list(sess.segments),
                    "mic_label": sess.mic_label, "call_label": sess.call_label,
                    "recovered": sess.recovered,
                    **({"persistence_warning": sess.persistence_warning} if sess.persistence_warning else {})}
    # 204 No Content must have an EMPTY body — a serialized `null` (4 bytes)
    # trips Starlette's "content longer than Content-Length" check.
    return Response(status_code=204)


def _parse_saved_transcript(text: str) -> dict:
    """Parse a saved transcript file back into segments and speaker labels.

    Saved format (one per line): `[HH:MM:SS] Speaker: text`. Leading `#` header
    lines are skipped; `# mic-label:` and `# call-label:` set the expected
    speaker names (falling back to `You` / `Call`). Lines that don't match the
    pattern are appended to the previous segment (whisper sometimes wraps long
    utterances). A colon in the body text is protected by only accepting a known
    speaker label as the prefix.
    """
    mic_label, call_label = "You", "Call"
    segs: list[dict] = []
    line_re = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(?:([^\n:]+?):\s)?(.*)$")
    label_re = re.compile(r"^#\s*(mic-label|call-label):\s*(.+)$", re.IGNORECASE)
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = label_re.match(line)
            if m:
                value = m.group(2).strip()
                if m.group(1).lower() == "mic-label":
                    mic_label = value or mic_label
                else:
                    call_label = value or call_label
            continue
        m = line_re.match(line)
        if m:
            speaker = (m.group(2) or "").strip()
            body = m.group(3)
            if speaker and speaker not in {mic_label, call_label}:
                # the prefix was text, not a speaker label
                body = f"{speaker}: {body}"
                speaker = ""
            segs.append({"t": m.group(1), "speaker": speaker, "text": body})
        elif segs:
            segs[-1]["text"] += " " + line
    return {"segments": segs, "mic_label": mic_label, "call_label": call_label}


def _transcript_path(account: str, name: str) -> Path:
    """Resolve a saved-transcript filename safely under _transcripts/, scoped to
    the account so one opp can't load another's files."""
    name = _safe(name)
    if not name.endswith(".txt"):
        raise HTTPException(400, "Not a transcript file")
    cust = _titlecase_folder(account)
    if not name.startswith(cust + "-"):
        raise HTTPException(403, "Transcript does not belong to this account")
    path = (CUSTOMERS_DIR / "_transcripts" / name).resolve()
    if path.parent != (CUSTOMERS_DIR / "_transcripts").resolve():
        raise HTTPException(400, "Invalid path")
    return path


@app.get("/api/transcripts")
def api_list_transcripts(account: str):
    """List saved transcripts for this account, newest first (by filename date
    then mtime). Powers the 'Past transcripts' list on the transcribe page."""
    account = _safe(account)
    cust = _titlecase_folder(account)
    tdir = CUSTOMERS_DIR / "_transcripts"
    if not tdir.exists():
        return {"transcripts": []}
    items = []
    for p in tdir.glob(f"{cust}-*.txt"):
        try:
            items.append({"name": p.name, "mtime": p.stat().st_mtime,
                          "size": p.stat().st_size})
        except OSError:
            pass
    items.sort(key=lambda x: (x["name"], x["mtime"]), reverse=True)
    return {"transcripts": items}


@app.get("/api/transcripts/{name}")
def api_load_transcript(name: str, account: str):
    """Load one saved transcript as segments + raw text so the page can render
    it read-only and the copilot can answer questions about it."""
    path = _transcript_path(account, name)
    if not path.exists():
        raise HTTPException(404, "Transcript not found")
    text = path.read_text()
    parsed = _parse_saved_transcript(text)
    return {"name": name, "segments": parsed["segments"], "transcript": text,
            "mic_label": parsed["mic_label"], "call_label": parsed["call_label"]}


@app.get("/api/transcribe/{session_id}/stream")
async def api_transcribe_stream(session_id: str):
    from sse_starlette.sse import EventSourceResponse
    sess = SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "Unknown session")

    async def gen():
        # replay any segments already captured (e.g. reconnect)
        for seg in list(sess.segments):
            yield {"event": "segment", "data": json.dumps(seg)}
        if sess.recovered:
            # Audio capture is gone after a restart; don't hold the connection open.
            return
        while session_id in SESSIONS:
            try:
                seg = await asyncio.wait_for(sess.queue.get(), timeout=15)
                yield {"event": "segment", "data": json.dumps(seg)}
            except asyncio.TimeoutError:
                yield {"event": "ping", "data": "{}"}  # keep-alive

    return EventSourceResponse(gen())


# Heuristic: questions that need the codebase / a skill go to claude -p.
_DEEP_HINTS = ("codebase", "connector", "feasib", "troubleshoot", "schema", "api ",
               "rate limit", "cdc", "deployment", "self-managed", "repo", "error",
               "poc", "meddpicc", "qualif", "edge case")


class AskLive(BaseModel):
    question: str = Field(max_length=5_000)
    # File-backed ask: when the session_id is the "file" sentinel, the page is
    # querying a SAVED transcript (reopened), not a live recording. The client
    # passes the transcript name + account so the server loads it from disk.
    transcript_name: str | None = Field(default=None, max_length=500)
    account: str | None = Field(default=None, max_length=120)
    opportunity: str | None = Field(default=None, max_length=200)


@app.post("/api/transcribe/{session_id}/ask")
async def api_transcribe_ask(session_id: str, body: AskLive):
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "Empty question")

    # Resolve the transcript + context from either a LIVE session or a SAVED
    # file. session_id == "file" means the page reopened a saved transcript.
    if session_id == "file":
        if not body.transcript_name or not body.account:
            raise HTTPException(400, "File ask requires transcript_name + account")
        path = _transcript_path(_safe(body.account), body.transcript_name)
        if not path.exists():
            raise HTTPException(404, "Transcript not found")
        full = path.read_text()
        account, opportunity, opp_slug = body.account, body.opportunity, None
        live = False
    else:
        sess = SESSIONS.get(session_id)
        if not sess:
            raise HTTPException(404, "Unknown session")
        full = sess.transcript_text()
        account, opportunity, opp_slug = sess.account, getattr(sess, "opportunity", None), sess.opp_slug
        live = True

    # Live: tail window (recent context matters most, transcript still growing).
    # Saved: send the whole thing — the SE is reviewing a finished call and
    # questions may target anything in it. Cap generously to bound token cost.
    transcript = full[-12000:] if live else full[-60000:]
    deep = any(h in q.lower() for h in _DEEP_HINTS)

    if deep:
        # Route to claude -p (full repo + skill access) via the job system.
        when = "LIVE during a customer call" if live else "reviewing a saved call transcript"
        tlabel = "live call transcript so far" if live else "full saved call transcript"
        prompt = (
            f"You are assisting a Solutions Engineer {when} for the account "
            f"'{account}'{(', opportunity ' + repr(opportunity)) if opportunity else ''}. "
            f"Here is the {tlabel}:\n\n{transcript}\n\n"
            f"The SE asks: {q}\n\n"
            f"Answer concisely and practically. If it involves Airbyte connectors, "
            f"deployment, or the codebase, use the relevant SE skills / inspect the repo as needed."
        )
        job_id, persist_warn = await job_service.launch(
            account=account or "?",
            opp_slug=None,
            skill="live-ask",
            opportunity=opportunity,
            sig=("live", session_id, q[:60]),
            prompt=prompt,
            meta={"account": account or "?", "opp_slug": None, "skill": "live-ask",
                  "opportunity": opportunity},
        )
        return {"mode": "deep", "job_id": job_id,
                **({"persistence_warning": persist_warn} if persist_warn else {})}

    # Quick path → Claude API streaming (SSE).
    api_key = _anthropic_api_key()
    if not api_key:
        # No key → tell the client to re-route as deep so the ask-bar still works.
        return JSONResponse({"mode": "needs_deep",
                             "reason": "No ANTHROPIC_API_KEY for the quick path — re-ask routes to claude -p."})

    from fastapi.responses import StreamingResponse

    # Emit raw SSE frames via a plain StreamingResponse rather than
    # EventSourceResponse: sse_starlette's disconnect-polling can race a
    # fetch()+getReader() client and close the stream before any token is
    # delivered (curl is unaffected, the browser sees an empty body). A plain
    # streaming response just writes chunks — the wire format is identical, so
    # the existing front-end parser (event:/data: split on \n\n) is unchanged.
    def _frame(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

    async def gen():
        acc = ""
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=api_key)
            system = ("You are a Solutions Engineer's live call copilot. Answer briefly and "
                      "directly from the call transcript provided. If the question needs the "
                      "Airbyte codebase or a deep skill, say so in one line.")
            async with client.messages.stream(
                model=_model_for("live-ask"),
                max_tokens=700,
                system=system,
                messages=[{"role": "user", "content":
                           f"Transcript:\n\n{transcript}\n\nQuestion: {q}"}],
            ) as stream:
                async for text in stream.text_stream:
                    acc += text
                    yield _frame("token", {"text": text, "html": md_render.markdown_to_body_html(acc)})
            yield _frame("done", {})
        except Exception as e:  # noqa: BLE001
            yield _frame("error", {"error": security.redact_sensitive(str(e))})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"cache-control": "no-store", "x-accel-buffering": "no"})


def _anthropic_key_from_keyring() -> str | None:
    """Best-effort: read ANTHROPIC_API_KEY from the OS keyring via the
    `keyring` module. Any backend error (missing service, locked keychain,
    unsupported platform) is treated as "no key" so the app degrades to the
    deep `claude -p` path.
    """
    try:
        import keyring
        import keyring.errors

        return keyring.get_password("se-skills", "ANTHROPIC_API_KEY")
    except (ImportError, keyring.errors.KeyringError, RuntimeError, OSError):
        return None
    except Exception:
        return None


def _anthropic_api_key() -> str | None:
    """Return the Anthropic API key for the quick ask-bar path.

    Priority: `ANTHROPIC_API_KEY` environment variable, then the OS keyring.
    No plaintext `~/.mcp/*.env` files are read.
    """
    return os.environ.get("ANTHROPIC_API_KEY") or _anthropic_key_from_keyring()


@app.get("/api/ai-status")
def api_ai_status():
    """Report whether the fast ⚡ ask-bar path is available.

    The Anthropic key is optional — without it, questions fall back to the
    slower claude -p deep path. The front-end uses this to show a badge so the
    SE knows which mode they're in instead of wondering why answers are slow.
    """
    has_key = bool(_anthropic_api_key())
    return {"quick_path": has_key}


@app.post("/api/transcribe/{session_id}/stop")
def api_transcribe_stop(session_id: str):
    sess = SESSIONS.pop(session_id, None)
    if not sess:
        raise HTTPException(404, "Unknown session")
    sess.stop()
    text = sess.transcript_text()
    # Save to _transcripts/<Customer>-MM.DD.YY.txt (the convention post-call consumes).
    transcripts_dir = CUSTOMERS_DIR / "_transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    cust = _titlecase_folder(sess.account)
    datestr = sess.started_at.astimezone().strftime("%m.%d.%y")
    base = f"{cust}-{datestr}"
    path = transcripts_dir / f"{base}.txt"
    n = 2
    while path.exists():
        path = transcripts_dir / f"{base}-v{n}.txt"; n += 1
    path.write_text(text)
    delete_ok = persistence.delete_session(session_id, WORKSPACE)
    result = {"saved_to": str(path), "segments": len(sess.segments),
              "chars": len(text), "transcript": text}
    if not delete_ok:
        result["persistence_warning"] = "The saved transcript was written, but the session state file could not be removed; it may reappear on restart."
    return result


# Favicon: inline SVG (also linked in index.html <head>). Serving it here too
# silences the browser's default GET /favicon.ico even for clients that ignore
# the <link>. Must be registered before the catch-all static mount below.
_FAVICON_SVG = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'>"
    "<rect width='32' height='32' rx='7' fill='#151a24'/>"
    "<text x='16' y='22' font-family='Inter,system-ui,sans-serif' font-size='19' "
    "font-weight='700' fill='#4263eb' text-anchor='middle'>S</text></svg>"
)


@app.get("/favicon.ico")
async def favicon() -> Response:
    return Response(content=_FAVICON_SVG, media_type="image/svg+xml")


# Serve the static frontend at root
app.mount("/", StaticFiles(directory=str(WEBAPP_DIR / "static"), html=True), name="static")


# Recover any live-transcribe sessions that were persisted before a restart.
# Done at module load so the recovered sessions are available immediately.
for _sess_data in persistence.load_sessions(WORKSPACE):
    try:
        _recovered = LiveSession.from_state(_sess_data)
        if _recovered.session_id:
            SESSIONS[_recovered.session_id] = _recovered
    except Exception:
        pass  # corrupted session file — ignore and continue


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
