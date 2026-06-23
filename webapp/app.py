#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "fastapi", "uvicorn[standard]", "pyyaml",
#   "faster-whisper", "sounddevice", "numpy", "sse-starlette", "anthropic",
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
import os
import queue
import re
import subprocess
import threading
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Paths — everything is relative to the airbyte-work workspace
# ---------------------------------------------------------------------------
WORKSPACE = Path(os.path.expanduser("~/airbyte-work"))
CUSTOMERS_DIR = WORKSPACE / "01-customers"
SE_CONFIG = WORKSPACE / ".se-config.yaml"
WEBAPP_DIR = Path(__file__).resolve().parent
TEAM_FILE = WEBAPP_DIR / "team-members.yaml"

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
SKILL_PRESENTATION = {
    "account-refresher":     {"label": "Account Refresher",     "blurb": "Fast catch-me-up briefing", "order": 1},
    "prep-call":             {"label": "Prep Call",             "blurb": "Tech-discovery call prep", "order": 2},
    "post-call":             {"label": "Post-Call Summary",     "blurb": "Summarize latest call", "order": 3},
    "biz-qual":              {"label": "Biz Qual (MEDDPICC)",   "blurb": "Business qualification", "order": 4},
    "tech-qual":             {"label": "Tech Qual",             "blurb": "Technical fit assessment", "order": 5},
    "deployment-model-qual": {"label": "Deployment Qual",       "blurb": "Cloud vs Self-Managed", "order": 6},
    "connector-feasibility": {"label": "Connector Feasibility", "blurb": "Source/dest coverage", "order": 7},
    "poc-plan":              {"label": "POC Plan",              "blurb": "Scope a proof of concept", "order": 8},
    "deal-assessment":       {"label": "Deal Assessment",       "blurb": "Honest deal-health read", "order": 9},
    "follow-up-email":       {"label": "Follow-up Email",       "blurb": "Draft an email", "order": 10},
    "objection-handler":     {"label": "Objection Handler",     "blurb": "Talk track for a concern", "order": 11},
    "internal-prep":         {"label": "Internal Prep",         "blurb": "AE sync / forecast / exec readout prep", "order": 12},
    "next-move":             {"label": "Next Move",             "blurb": "What to do next on this deal", "order": 13},
}

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
                "order": pres.get("order", 999),
            })
    # Fallback: if the repo skills/ dir isn't found, use the presentation list
    if not found:
        found = [{"id": k, **v} for k, v in SKILL_PRESENTATION.items()]
    found.sort(key=lambda s: (s.get("order", 999), s["label"]))
    return [{"id": s["id"], "label": s["label"], "blurb": s["blurb"]} for s in found]


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
    """Title-Case-Hyphenated, matching the workspace convention (e.g. Build-Manufacturing)."""
    return "-".join(part.capitalize() for part in re.split(r"[ _-]+", name.strip()) if part)


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


def _account_meta(account_dir: Path) -> dict:
    """Lightweight, filesystem-only card metadata: last-updated + output count."""
    outputs_dir = account_dir / "outputs"
    count = 0
    latest = 0.0
    if outputs_dir.exists():
        for f in outputs_dir.rglob("*.md"):
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


def list_outputs(account: str, opp: str | None = None) -> list[dict]:
    """Saved skill outputs, newest first. If `opp` given, scope to that
    opportunity's outputs; otherwise the account-level outputs folder
    (legacy / account-wide)."""
    base = (CUSTOMERS_DIR / account / "opportunities" / opp / "outputs") if opp \
        else (CUSTOMERS_DIR / account / "outputs")
    if not base.exists():
        return []
    items = []
    for skill_dir in sorted(base.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue  # skip hidden dirs like .runs (internal run-result cache)
        for f in sorted(skill_dir.glob("*.md")):
            st = f.stat()
            items.append({
                "skill": skill_dir.name,
                "filename": f.name,
                "path": str(f.relative_to(CUSTOMERS_DIR)),
                "mtime": st.st_mtime,
                "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "size": st.st_size,
            })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


async def sfdc_opportunities(account: str) -> list[dict]:
    """All SFDC opportunities for an account (not just the 'best' one).
    Each: name, stage, stage_num, amount, close_date, type, is_closed, ae, slug.
    Best-effort; returns [] if SFDC unavailable."""
    sf = _sf_config()
    if not sf.get("enabled", True):
        return []
    alias = sf.get("org_alias", "airbyte-prod")
    like = account.replace("-", " ").replace("'", "")
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
def _sf_config() -> dict:
    if SE_CONFIG.exists():
        cfg = yaml.safe_load(SE_CONFIG.read_text()) or {}
        return cfg.get("salesforce", {}) or {}
    return {}


async def sfdc_stage_amount(account_names: list[str]) -> dict:
    """Return {account_name: {stage, stage_num, amount, ae}} for the most relevant
    open (else latest) opportunity per account. One query for all names. Best-effort.
    `ae` is the Salesforce Account/Opportunity Owner. Returns {} on any failure."""
    sf = _sf_config()
    if not sf.get("enabled", True) or not account_names:
        return {}
    alias = sf.get("org_alias", "airbyte-prod")
    likes = " OR ".join(
        f"Account.Name LIKE '{n.replace('-', ' ').replace(chr(39), '')}%'" for n in account_names[:50]
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
    folder_for = {n.replace("-", " ").lower(): n for n in account_names}
    for r in records:
        acct_name = ((r.get("Account") or {}).get("Name") or "").lower()
        folder = next((fn for key, fn in folder_for.items() if acct_name.startswith(key) or key.startswith(acct_name)), None)
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
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="SE Skills — Local Hub")


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
    return {"name": folder, "created": created, "owner": body.owner}


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
    return list_outputs(account, opp)


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


@app.get("/api/output", response_class=PlainTextResponse)
def api_output_content(path: str):
    # path is relative to CUSTOMERS_DIR; resolve and confirm it stays inside
    target = (CUSTOMERS_DIR / path).resolve()
    if not str(target).startswith(str(CUSTOMERS_DIR.resolve())) or not target.is_file():
        raise HTTPException(404, "Not found")
    return target.read_text()


class OutputAsk(BaseModel):
    path: str                       # output file, relative to CUSTOMERS_DIR
    question: str
    account: str | None = None
    opportunity: str | None = None


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
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"status": "running", "ok": None, "stdout": "", "stderr": "",
                        "skill": "output-ask", "account": acct, "opportunity": body.opportunity,
                        "opp_slug": None, "sig": ("output-ask", body.path, q[:60])}
        meta = {"account": acct or "?", "opp_slug": None, "skill": "output-ask", "opportunity": body.opportunity}
        asyncio.create_task(_run_job(job_id, prompt, meta))
        return {"mode": "deep", "job_id": job_id}

    api_key = os.environ.get("ANTHROPIC_API_KEY") or _anthropic_key_from_mcp()
    if not api_key:
        return JSONResponse({"mode": "needs_deep",
                             "reason": "No ANTHROPIC_API_KEY for the quick path — re-ask routes to claude -p."})

    from sse_starlette.sse import EventSourceResponse

    async def gen():
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=api_key)
            system = ("You are a Solutions Engineer's copilot. Answer the follow-up briefly and directly "
                      "from the document provided. If the question needs the Airbyte codebase or a deep "
                      "skill, say so in one line.")
            async with client.messages.stream(
                model="claude-sonnet-4-6", max_tokens=800, system=system,
                messages=[{"role": "user", "content": f"Document:\n\n{doc}\n\nFollow-up question: {q}"}],
            ) as stream:
                async for text in stream.text_stream:
                    yield {"event": "token", "data": json.dumps({"text": text})}
            yield {"event": "done", "data": "{}"}
        except Exception as e:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"error": str(e)})}

    return EventSourceResponse(gen())


@app.get("/api/skills")
def api_skills():
    return SKILLS


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
    account: str
    skill: str | None = None        # a known skill id (dropdown)
    opportunity: str | None = None  # opp name (for context + output scoping)
    opp_slug: str | None = None     # opp folder slug
    extra: str | None = None        # free-text appended to the prompt
    freeform: str | None = None     # full free-text instruction (instead of a dropdown skill)


# ---------------------------------------------------------------------------
# Skill invocation runs as a BACKGROUND JOB so the HTTP request returns
# immediately. A long-running `claude -p` held the request (and a browser
# connection) for minutes — navigating away abandoned the fetch, orphaned the
# subprocess, and saturated the per-host connection pool so other pages hung.
# Now: POST /api/invoke starts the job + returns a job_id; the run continues
# server-side even if you navigate away; GET /api/jobs/{id} polls for status.
# ---------------------------------------------------------------------------
JOBS: dict[str, dict] = {}


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


def _runs_dir(account: str, opp_slug: str | None) -> Path | None:
    """Hidden per-opportunity folder holding the last result of each skill run.
    One file per skill (overwritten on re-run) — so e.g. next-move always has
    exactly one record. Hidden (.runs) so it never shows in the outputs list.
    Returns None when there's no opportunity scope (nothing to persist)."""
    if not opp_slug:
        return None
    return CUSTOMERS_DIR / account / "opportunities" / opp_slug / "outputs" / ".runs"


def _persist_run(account: str, opp_slug: str | None, skill: str, record: dict) -> None:
    d = _runs_dir(account, opp_slug)
    if not d:
        return
    d.mkdir(parents=True, exist_ok=True)
    # one file per skill id — overwrite on re-run
    safe_skill = re.sub(r"[^A-Za-z0-9._-]", "-", skill or "freeform")
    (d / f"{safe_skill}.json").write_text(json.dumps(record))


def _latest_run(account: str, opp_slug: str | None) -> dict | None:
    """The most recently finished run for this opportunity (newest across all
    skill files). Read on page load so a result survives navigating away and
    even a server restart."""
    d = _runs_dir(account, opp_slug)
    if not d or not d.exists():
        return None
    best = None
    for f in d.glob("*.json"):
        try:
            rec = json.loads(f.read_text())
        except Exception:
            continue
        if best is None or (rec.get("finished_at", 0) > best.get("finished_at", 0)):
            best = rec
    return best


async def _run_job(job_id: str, prompt: str, meta: dict):
    job = JOBS[job_id]
    cmd = ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(WORKSPACE),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        job["pid"] = proc.pid
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
        job.update(status="done", ok=proc.returncode == 0,
                   stdout=stdout.decode(errors="replace"),
                   stderr=stderr.decode(errors="replace"))
    except FileNotFoundError:
        job.update(status="error", ok=False, stdout="",
                   stderr="`claude` CLI not found on PATH — is Claude Code installed?")
    except asyncio.TimeoutError:
        job.update(status="error", ok=False, stdout="",
                   stderr="Skill run timed out after 10 minutes.")
    except Exception as e:  # noqa: BLE001 — surface any launch failure to the UI
        job.update(status="error", ok=False, stdout="", stderr=f"{type(e).__name__}: {e}")

    # Persist the finished result to disk (one file per skill, overwritten).
    try:
        _persist_run(meta["account"], meta.get("opp_slug"), meta["skill"], {
            "skill": meta["skill"], "opportunity": meta.get("opportunity"),
            "ok": job.get("ok"), "stdout": job.get("stdout", ""), "stderr": job.get("stderr", ""),
            "finished_at": datetime.now(timezone.utc).timestamp(),
        })
    except Exception:
        pass  # persistence is best-effort; the in-memory job still works


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

    prompt = _build_prompt(body, account, out_dir)

    # Reuse an already-running job for the same (account, opp, skill/freeform)
    # so re-entering the page doesn't kick off a duplicate run.
    sig = (account, opp_slug, body.skill or "freeform", (body.freeform or body.extra or "")[:80])
    for jid, j in JOBS.items():
        if j.get("sig") == sig and j.get("status") == "running":
            return JSONResponse({"job_id": jid, "status": "running", "reused": True})

    job_id = uuid.uuid4().hex[:12]
    skill_id = body.skill or "freeform"
    JOBS[job_id] = {
        "status": "running", "ok": None, "stdout": "", "stderr": "",
        "skill": skill_id, "account": account,
        "opportunity": body.opportunity, "opp_slug": opp_slug, "sig": sig,
    }
    meta = {"account": account, "opp_slug": opp_slug, "skill": skill_id, "opportunity": body.opportunity}
    asyncio.create_task(_run_job(job_id, prompt, meta))
    return JSONResponse({"job_id": job_id, "status": "running", "reused": False})


@app.get("/api/accounts/{account}/last-run")
def api_last_run(account: str, opp_slug: str | None = None):
    """The most recent finished run for an opportunity, read from disk. Lets a
    result survive both navigation and a server restart. 204 if none."""
    account = _safe(account)
    opp_slug = _safe(opp_slug) if opp_slug else None
    rec = _latest_run(account, opp_slug)
    if not rec:
        return JSONResponse(status_code=204, content=None)
    return rec


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job")
    return {k: v for k, v in job.items() if k != "sig"}


@app.get("/api/jobs")
def api_jobs_for(account: str | None = None, opp_slug: str | None = None):
    """List jobs (optionally filtered) so a page can recover a run it launched
    earlier — e.g. after you backed out and came back."""
    out = []
    for jid, j in JOBS.items():
        if account and j.get("account") != account:
            continue
        if opp_slug and j.get("opp_slug") != opp_slug:
            continue
        out.append({"job_id": jid, **{k: v for k, v in j.items() if k not in ("sig", "stdout", "stderr")}})
    return out


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
    def __init__(self, account, opp_slug, mic_device, call_device):
        self.account = account
        self.opp_slug = opp_slug
        self.started_at = datetime.now(timezone.utc)
        self.segments: list[dict] = []          # {t, speaker, text}
        self.queue: "asyncio.Queue" = asyncio.Queue()
        self.loop = asyncio.get_running_loop()
        self.labeled = call_device is not None
        self.channels: list[_Channel] = []
        self._lock = threading.Lock()
        self._recent_call = deque(maxlen=12)   # (monotonic_ts, text) for echo matching
        self._pending_you = []                  # held You segs awaiting echo check
        self.__post_channels(mic_device, call_device)

    def _emit(self, seg):
        self.segments.append(seg)
        self.loop.call_soon_threadsafe(self.queue.put_nowait, seg)

    def _flush_pending_you(self, now):
        """Emit any held 'You' segments older than the hold window that were not
        matched by a 'Call' echo."""
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
            if label == "Call":
                # drop any held 'You' that this 'Call' line echoes
                self._pending_you = [
                    (ts, s) for (ts, s) in self._pending_you
                    if _text_similarity(s["text"], text) < ECHO_SIM
                ]
                self._recent_call.append((now, text))
                self._emit(seg)
            else:  # "You" — suppress if it echoes a recent 'Call'; else hold briefly
                if any(now - cts <= ECHO_HOLD_SEC and _text_similarity(text, ctext) >= ECHO_SIM
                       for cts, ctext in self._recent_call):
                    return  # echo of the call audio — drop
                self._pending_you.append((now, seg))

    def __post_channels(self, mic_device, call_device):
        if self.labeled:
            self.channels.append(_Channel(mic_device, "You", self._on_segment))
            self.channels.append(_Channel(call_device, "Call", self._on_segment))
        else:
            self.channels.append(_Channel(mic_device, "", self._on_segment))

    def start(self):
        for c in self.channels:
            c.start()

    def stop(self):
        for c in self.channels:
            c.stop()
        # flush any held 'You' segments that never got an echo match
        with self._lock:
            for _ts, seg in self._pending_you:
                self._emit(seg)
            self._pending_you = []

    def transcript_text(self) -> str:
        lines = []
        for s in self.segments:
            who = f"{s['speaker']}: " if s["speaker"] else ""
            lines.append(f"[{s['t']}] {who}{s['text']}")
        return "\n".join(lines)


@app.get("/api/audio-devices")
def api_audio_devices():
    """Input devices for the mic/call pickers. Flags BlackHole presence."""
    try:
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Audio capture unavailable: {e}. Run `brew install portaudio` and reinstall deps.")
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
    account: str
    opp_slug: str | None = None
    opportunity: str | None = None
    mic_device: int
    call_device: int | None = None


@app.post("/api/transcribe/start")
async def api_transcribe_start(body: StartLive):
    account = _safe(body.account)
    opp_slug = _safe(body.opp_slug) if body.opp_slug else None
    try:
        sess = LiveSession(account, opp_slug, body.mic_device, body.call_device)
        sess.opportunity = body.opportunity
        sess.start()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Could not start capture: {e}")
    sid = uuid.uuid4().hex[:12]
    SESSIONS[sid] = sess
    return {"session_id": sid, "labeled": sess.labeled}


@app.get("/api/transcribe/active")
def api_transcribe_active(account: str, opp_slug: str | None = None):
    """If a live session is still recording for this opportunity, return it so
    the page can reconnect (segments replay over the stream). 204 if none."""
    account = _safe(account)
    opp_slug = _safe(opp_slug) if opp_slug else None
    for sid, sess in SESSIONS.items():
        if sess.account == account and sess.opp_slug == opp_slug:
            return {"session_id": sid, "labeled": sess.labeled,
                    "started_at": sess.started_at.timestamp(),
                    "segments": list(sess.segments)}
    return JSONResponse(status_code=204, content=None)


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
    question: str


@app.post("/api/transcribe/{session_id}/ask")
async def api_transcribe_ask(session_id: str, body: AskLive):
    sess = SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(404, "Unknown session")
    q = (body.question or "").strip()
    if not q:
        raise HTTPException(400, "Empty question")
    transcript = sess.transcript_text()[-12000:]  # rolling window (tail)
    deep = any(h in q.lower() for h in _DEEP_HINTS)

    if deep:
        # Route to claude -p (full repo + skill access) via the job system.
        prompt = (
            f"You are assisting a Solutions Engineer LIVE during a customer call for the account "
            f"'{sess.account}'{(', opportunity ' + repr(sess.opportunity)) if getattr(sess, 'opportunity', None) else ''}. "
            f"Here is the live call transcript so far:\n\n{transcript}\n\n"
            f"The SE asks: {q}\n\n"
            f"Answer concisely and practically for use mid-call. If it involves Airbyte connectors, "
            f"deployment, or the codebase, use the relevant SE skills / inspect the repo as needed."
        )
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"status": "running", "ok": None, "stdout": "", "stderr": "",
                        "skill": "live-ask", "account": sess.account,
                        "opportunity": getattr(sess, "opportunity", None),
                        "opp_slug": sess.opp_slug, "sig": ("live", session_id, q[:60])}
        meta = {"account": sess.account, "opp_slug": None, "skill": "live-ask",
                "opportunity": getattr(sess, "opportunity", None)}
        asyncio.create_task(_run_job(job_id, prompt, meta))
        return {"mode": "deep", "job_id": job_id}

    # Quick path → Claude API streaming (SSE).
    api_key = os.environ.get("ANTHROPIC_API_KEY") or _anthropic_key_from_mcp()
    if not api_key:
        # No key → tell the client to re-route as deep so the ask-bar still works.
        return JSONResponse({"mode": "needs_deep",
                             "reason": "No ANTHROPIC_API_KEY for the quick path — re-ask routes to claude -p."})

    from sse_starlette.sse import EventSourceResponse

    async def gen():
        try:
            from anthropic import AsyncAnthropic
            client = AsyncAnthropic(api_key=api_key)
            system = ("You are a Solutions Engineer's live call copilot. Answer briefly and "
                      "directly from the call transcript provided. If the question needs the "
                      "Airbyte codebase or a deep skill, say so in one line.")
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=700,
                system=system,
                messages=[{"role": "user", "content":
                           f"Live transcript so far:\n\n{transcript}\n\nQuestion: {q}"}],
            ) as stream:
                async for text in stream.text_stream:
                    yield {"event": "token", "data": json.dumps({"text": text})}
            yield {"event": "done", "data": "{}"}
        except Exception as e:  # noqa: BLE001
            yield {"event": "error", "data": json.dumps({"error": str(e)})}

    return EventSourceResponse(gen())


def _anthropic_key_from_mcp() -> str | None:
    """Best-effort: read ANTHROPIC_API_KEY from a ~/.mcp/*.env if present."""
    mcp = Path(os.path.expanduser("~/.mcp"))
    if not mcp.exists():
        return None
    for f in mcp.glob("*.env"):
        try:
            for line in f.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return None


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
    header = f"# Live transcript — {sess.account} — {sess.started_at.astimezone().strftime('%B %d, %Y %H:%M')}\n\n"
    path.write_text(header + text + "\n")
    return {"saved_to": str(path), "segments": len(sess.segments),
            "chars": len(text), "transcript": text}


# Serve the static frontend at root
app.mount("/", StaticFiles(directory=str(WEBAPP_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
