#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn[standard]", "pyyaml"]
# ///
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
import os
import re
import subprocess
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


def accounts_for_member(member_id: str) -> dict:
    """Accounts owned by this member, plus unowned ones — split into active and archived."""
    all_accounts = list_accounts()
    visible = [a for a in all_accounts if a["owner"] == member_id or a["owner"] is None]
    # owned first, then unowned, within each bucket
    visible.sort(key=lambda a: (a["owner"] != member_id, a["name"].lower()))
    return {
        "active": [a for a in visible if not a["archived"]],
        "archived": [a for a in visible if a["archived"]],
    }


def list_outputs(account: str) -> list[dict]:
    """Every saved skill output for an account, newest first."""
    base = CUSTOMERS_DIR / account / "outputs"
    if not base.exists():
        return []
    items = []
    for skill_dir in sorted(base.iterdir()):
        if not skill_dir.is_dir():
            continue
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


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="SE Skills — Local Hub")


@app.get("/api/members")
def api_members():
    return load_team()


@app.get("/api/members/{member_id}/accounts")
def api_member_accounts(member_id: str):
    if not member_by_id(member_id):
        raise HTTPException(404, "Unknown member")
    return accounts_for_member(member_id)


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


@app.get("/api/accounts/{account}/outputs")
def api_outputs(account: str):
    account = _safe(account)
    return list_outputs(account)


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


@app.get("/api/output", response_class=PlainTextResponse)
def api_output_content(path: str):
    # path is relative to CUSTOMERS_DIR; resolve and confirm it stays inside
    target = (CUSTOMERS_DIR / path).resolve()
    if not str(target).startswith(str(CUSTOMERS_DIR.resolve())) or not target.is_file():
        raise HTTPException(404, "Not found")
    return target.read_text()


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


def _derive_help(skill_id: str, label: str, blurb: str) -> dict:
    """Build a human-friendly help entry by parsing the skill's SKILL.md live."""
    f = _find_skill_file(skill_id)
    entry = {
        "id": skill_id,
        "label": label,
        "summary": blurb,
        "description": "",
        "triggers": [],
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

    # Prerequisites — skills state these in a "Hard Prerequisite" section
    entry["prerequisites"] = _section(body, "prerequisite", "requires", "source sufficiency")

    # Data sources — Salesforce Enrichment / Source Freshness / Sources sections
    entry["data_sources"] = _section(body, "salesforce enrichment", "sources", "source freshness")

    # Output location — target the auto-save path specifically (under outputs/),
    # not the first 01-customers path (which is often a _transcripts source path).
    # Paths appear in fenced code blocks (no backticks), so match the bare path.
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
    skill: str
    account: str
    extra: str | None = None  # optional free-text appended to the prompt (e.g. an objection)


@app.post("/api/invoke")
async def api_invoke(body: InvokeBody):
    if body.skill not in SKILL_IDS:
        raise HTTPException(400, f"Unknown skill: {body.skill}")
    account = _safe(body.account)

    # Build the natural-language prompt that triggers the skill, the same way
    # you'd type it into Claude Code. The skill's own description handles routing.
    prompt = f"Use the {body.skill} skill for {account}."
    if body.extra:
        prompt += f" Additional context: {body.extra.strip()}"

    # Headless Claude Code, run from the workspace so skills + MCPs + files resolve.
    cmd = ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(WORKSPACE),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)
    except FileNotFoundError:
        raise HTTPException(500, "`claude` CLI not found on PATH — is Claude Code installed?")
    except asyncio.TimeoutError:
        raise HTTPException(504, "Skill run timed out after 10 minutes")

    return JSONResponse({
        "skill": body.skill,
        "account": account,
        "ok": proc.returncode == 0,
        "stdout": stdout.decode(errors="replace"),
        "stderr": stderr.decode(errors="replace"),
    })


# Serve the static frontend at root
app.mount("/", StaticFiles(directory=str(WEBAPP_DIR / "static"), html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)
