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
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import orchestrator
import security

from integrations.salesforce import SalesforceIntegration
from routes.accounts import router as accounts_router
from routes.jobs import router as jobs_router
from routes.outputs import router as outputs_router
from routes.feedback import router as feedback_router
from routes.overview import router as overview_router
from routes.ask import router as ask_router
from routes.salesforce import router as salesforce_router
from routes.transcription import router as transcription_router
from services.account_service import AccountService
from services.ask_service import AskService, anthropic_api_key
from services.overview_service import OverviewService
from services.feedback_service import FeedbackService
from services.job_service import JobService
from services.output_service import OutputService
from services.transcription_service import TranscriptionService

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








def _slug(name: str) -> str:
    """Filesystem-safe slug for an opportunity name."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", (name or "").strip()).strip("-")
    return s[:80] or "opportunity"




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











# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="SE Skills — Local Hub")
app.include_router(jobs_router)


app.include_router(accounts_router)




# ---------------------------------------------------------------------------
# Auto-populate accounts from Salesforce — endpoints
# ---------------------------------------------------------------------------













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

salesforce_integration = SalesforceIntegration(
    customers_dir=CUSTOMERS_DIR,
    workspace=WORKSPACE,
    sf_config=lambda: _se_config().get("salesforce", {}) or {},
    titlecase=_titlecase_folder,
    slug=_slug,
)

account_service = AccountService(
    customers_dir=CUSTOMERS_DIR,
    webapp_dir=WEBAPP_DIR,
    output_service=output_service,
    job_service=job_service,
    safe_name=_safe,
    titlecase=_titlecase_folder,
    slug=_slug,
    team_file=TEAM_FILE,
    member_prefs_dir=WEBAPP_DIR / ".member-prefs",
    se_config_file=SE_CONFIG,
    sfdc_opportunities=salesforce_integration.opportunities_for_account,
)

# Make the services reachable from route dependencies.
app.state.output_service = output_service
app.state.feedback_service = feedback_service
app.state.job_service = job_service
app.state.account_service = account_service
app.state.salesforce_integration = salesforce_integration

overview_service = OverviewService(
    account_service=account_service,
    output_service=output_service,
    job_service=job_service,
)
app.state.overview_service = overview_service

ask_service = AskService(
    output_service=output_service,
    job_service=job_service,
    api_key=anthropic_api_key,
    model_for=_model_for,
)
app.state.ask_service = ask_service

transcription_service = TranscriptionService(
    customers_dir=CUSTOMERS_DIR,
    workspace=WORKSPACE,
    safe_name=_safe,
    titlecase=_titlecase_folder,
    whisper_model=os.environ.get("SE_WHISPER_MODEL", "small"),
)
app.state.transcription_service = transcription_service


app.include_router(accounts_router)
app.include_router(outputs_router)
app.include_router(feedback_router)
app.include_router(overview_router)
app.include_router(salesforce_router)
app.include_router(ask_router)
app.include_router(transcription_router)


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



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
