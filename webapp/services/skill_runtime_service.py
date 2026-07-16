"""Skill discovery, presentation, planning, permission, and invocation service.

Owns everything the web UI needs to list skills, show their help, check
prerequisites, request permission approval, and launch a skill as a background
job. It delegates job execution to `JobService` and filesystem/output work to
`OutputService`; it does not import `webapp.app`.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import orchestrator
import yaml
from fastapi import HTTPException
from pydantic import BaseModel, Field

from services.job_service import JobService
from services.output_service import OutputError, OutputService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Presentation metadata
# ---------------------------------------------------------------------------
TIER_WORKFLOW = "Workflow — run in order"
TIER_LATE = "Late-stage — after POC"
TIER_ANYTIME = "Anytime — as needed"
TIER_META = "When you're not sure"

# Optional presentation overrides: preferred display order + friendlier labels/
# blurbs than raw frontmatter. NOT the source of truth for WHICH skills exist —
# that's derived from the skill folders on disk. A new skill appears
# automatically; add an entry here only to tune how it's shown.
SKILL_PRESENTATION: dict[str, dict[str, Any]] = {
    # Workflow chain (numbered)
    "prep-call": {"label": "Prep Call", "blurb": "Tech-discovery call prep — the only skill that needs no prior data", "tier": TIER_WORKFLOW, "step": 1, "order": 1},
    "post-call": {"label": "Post-Call Summary", "blurb": "Summarize the latest call (run after each call)", "tier": TIER_WORKFLOW, "step": 2, "order": 2},
    "deployment-model-qual": {"label": "Deployment Qual", "blurb": "Cloud vs Self-Managed — the gate before technical scoping", "tier": TIER_WORKFLOW, "step": 3, "order": 3},
    "biz-qual": {"label": "Biz Qual (MEDDPICC)", "blurb": "Business qualification (needs a transcript)", "tier": TIER_WORKFLOW, "step": 4, "order": 4},
    "tech-qual": {"label": "Tech Qual", "blurb": "Technical fit assessment (needs a transcript)", "tier": TIER_WORKFLOW, "step": 5, "order": 5},
    "full-qual": {"label": "Full Qual (biz + tech)", "blurb": "Shortcut: runs biz-qual + tech-qual back-to-back (two separate docs)", "tier": TIER_WORKFLOW, "step": None, "order": 5.5},
    "connector-feasibility": {"label": "Connector Feasibility", "blurb": "Source/dest coverage check", "tier": TIER_WORKFLOW, "step": 6, "order": 6},
    "poc-plan": {"label": "POC Plan", "blurb": "Scope a POC (needs biz-qual + tech-qual — will offer to run them)", "tier": TIER_WORKFLOW, "step": 7, "order": 7},
    # Late-stage / closing (numbered, but only after POC data exists)
    "roi-business-case": {"label": "ROI Business Case", "blurb": "Compile the economic buyer's TCO/ROI number", "tier": TIER_LATE, "step": 8, "order": 8},
    "mutual-close-plan": {"label": "Mutual Close Plan", "blurb": "Path from POC-success to signature (owners + dates)", "tier": TIER_LATE, "step": 9, "order": 9},
    # Anytime / as-needed (unnumbered)
    "deal-assessment": {"label": "Deal Assessment", "blurb": "Honest deal-health read (run every ~2 weeks)", "tier": TIER_ANYTIME, "step": None, "order": 20},
    "account-refresher": {"label": "Account Refresher", "blurb": "Fast catch-me-up briefing before a touchpoint", "tier": TIER_ANYTIME, "step": None, "order": 21},
    "follow-up-email": {"label": "Follow-up Email", "blurb": "Draft a customer email in your voice", "tier": TIER_ANYTIME, "step": None, "order": 22},
    "objection-handler": {"label": "Objection Handler", "blurb": "Talk track for a specific customer concern", "tier": TIER_ANYTIME, "step": None, "order": 23},
    "internal-prep": {"label": "Internal Prep", "blurb": "AE sync / forecast / exec-readout prep (internal)", "tier": TIER_ANYTIME, "step": None, "order": 24},
    "coverage-handoff": {"label": "Coverage Handoff", "blurb": "PTO handoff for a covering SE", "tier": TIER_ANYTIME, "step": None, "order": 25},
    "pov-gsheet": {"label": "POV Google Sheet", "blurb": "Create and pre-fill a POV Success Criteria Google Sheet", "tier": TIER_ANYTIME, "step": None, "order": 26},
    # Router (unnumbered)
    "next-move": {"label": "Next Move", "blurb": "Not sure what to run? This inspects the deal and tells you", "tier": TIER_META, "step": None, "order": 30},
}


class PermissionProfile(BaseModel):
    write: bool = True
    shell: bool = False
    git: bool = False

    def requires_approval(self) -> bool:
        return self.write or self.shell or self.git


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


# Sales methodologies the skills are built on — detect which a skill uses.
METHODOLOGIES: dict[str, list[str]] = {
    "MEDDPICC": ["meddpicc", "meddic"],
    "SPIN": ["spin selling", "spin ", "implication question", "need-payoff"],
    "Sandler": ["sandler", "pain funnel", "upfront contract", "negative reverse"],
    "Challenger": ["challenger", "reframe", "rational drowning", "commercial teaching"],
    "Chris Voss (tactical empathy)": ["voss", "mirror", "label", "calibrated question", "accusation"],
    "Command of the Message": ["command of the message", "value framing"],
}


class SkillRuntimeError(Exception):
    """Domain exception carrying an HTTP-like status code and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class SkillRuntimeService:
    """Cohesive skill runtime: discovery, help, planning, permissions, invocation."""

    def __init__(
        self,
        customers_dir: Path,
        workspace: Path,
        output_service: OutputService,
        job_service: JobService,
        *,
        se_config: Callable[[], dict],
        se_config_clear: Callable[[], None],
        safe_name: Callable[[str], str],
        skills_dir: Path,
        skills_dirs: list[Path],
    ) -> None:
        self.customers_dir = customers_dir
        self.workspace = workspace
        self.output_service = output_service
        self.job_service = job_service
        self._se_config = se_config
        self._se_config_clear = se_config_clear
        self._safe_name = safe_name
        self._skills_dir = skills_dir
        self._skills_dirs = skills_dirs
        self.skills = self.discover_skills()
        self.skill_ids: set[str] = {s["id"] for s in self.skills}
        self._all_skill_ids: set[str] = set(SKILL_PRESENTATION.keys()) | self.skill_ids

    # -----------------------------------------------------------------------
    # Safe-name wrapper
    # -----------------------------------------------------------------------
    def _safe(self, name: str | None) -> str | None:
        if name is None:
            return None
        try:
            return self._safe_name(name)
        except HTTPException as exc:
            raise SkillRuntimeError(exc.status_code, exc.detail) from exc

    # -----------------------------------------------------------------------
    # Permissions
    # -----------------------------------------------------------------------
    def _permission_profile(self, skill_id: str | None, freeform: bool = False) -> SkillPermission:
        if freeform or not skill_id:
            profile = SKILL_PERMISSIONS["freeform"]
        else:
            profile = SKILL_PERMISSIONS.get(skill_id, PermissionProfile(write=True))
        return SkillPermission.from_profile(profile)

    def permission_for(self, skill: str | None, freeform: bool = False) -> dict:
        if freeform or not skill:
            return self._permission_profile(None, freeform=True).model_dump()
        if skill not in self.skill_ids:
            raise SkillRuntimeError(400, f"Unknown skill: {skill}")
        return self._permission_profile(skill).model_dump()

    # -----------------------------------------------------------------------
    # Skill discovery
    # -----------------------------------------------------------------------
    def discover_skills(self) -> list[dict]:
        """Every folder with a SKILL.md under the repo's skills/ dir is a suite skill.

        Presentation (label/blurb/order) overlaid from SKILL_PRESENTATION when present;
        otherwise a sensible default is derived so a newly-added skill still shows."""
        found = []
        if self._skills_dir.exists():
            for d in sorted(self._skills_dir.iterdir()):
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
        return [{
            "id": s["id"],
            "label": s["label"],
            "blurb": s["blurb"],
            "tier": s.get("tier", "Other"),
            "step": s.get("step"),
            "permissions": self._permission_profile(s["id"]).model_dump(),
        } for s in found]

    def reload(self) -> dict:
        """Re-discover skills and config from disk without restarting the server."""
        self._se_config_clear()
        # Eagerly reload so malformed config surfaces as early as possible.
        self._se_config()
        self.skills = self.discover_skills()
        self.skill_ids = {s["id"] for s in self.skills}
        self._all_skill_ids = set(SKILL_PRESENTATION.keys()) | self.skill_ids
        return {"skills": self.skills, "reloaded": True}

    # -----------------------------------------------------------------------
    # Skill help
    # -----------------------------------------------------------------------
    def _find_skill_file(self, skill_id: str) -> Path | None:
        for base in self._skills_dirs:
            f = base / skill_id / "SKILL.md"
            if f.exists():
                return f
        return None

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict, str]:
        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                fm = yaml.safe_load(text[3:end]) or {}
                return (fm if isinstance(fm, dict) else {}), text[end + 4:]
        return {}, text

    @staticmethod
    def _extract_triggers(description: str) -> list[str]:
        return re.findall(r'"([^"]+)"', description or "")

    @staticmethod
    def _section(body: str, *header_keywords: str) -> str | None:
        lines = body.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("#"):
                heading = ln.lstrip("#").strip().lower()
                if any(k in heading for k in header_keywords):
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

    @staticmethod
    def _detect_methodologies(text: str) -> list[str]:
        low = text.lower()
        return [name for name, kws in METHODOLOGIES.items() if any(k in low for k in kws)]

    def _detect_related_skills(self, self_id: str, text: str) -> list[str]:
        found = []
        for sid in self._all_skill_ids:
            if sid == self_id:
                continue
            if re.search(rf"`{re.escape(sid)}`|\b{re.escape(sid)}\b", text):
                found.append(sid)
        return sorted(found)

    @staticmethod
    def _clean_section(txt: str | None) -> str | None:
        if not txt:
            return None
        out = []
        for ln in txt.splitlines():
            s = ln.rstrip()
            s = re.sub(r"^\s*#{1,6}\s*", "", s)
            s = re.sub(r"^\s*[-*]\s+", "• ", s)
            s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
            s = re.sub(r"`([^`]+)`", r"\1", s)
            out.append(s)
        cleaned = "\n".join(out)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
        return cleaned or None

    def _derive_help(self, skill_id: str, label: str, blurb: str) -> dict:
        f = self._find_skill_file(skill_id)
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
        fm, body = self._parse_frontmatter(f.read_text())
        desc = fm.get("description", "")
        entry["found"] = True
        entry["description"] = desc
        entry["triggers"] = self._extract_triggers(desc)
        entry["methodologies"] = self._detect_methodologies(body)
        entry["how_it_works"] = self._clean_section(
            self._section(body, "se best practices", "how it works", "how to", "the holistic read", "framework")
        )
        entry["related_skills"] = self._detect_related_skills(skill_id, body)
        entry["prerequisites"] = self._clean_section(
            self._section(body, "prerequisite", "requires", "source sufficiency")
        )
        entry["data_sources"] = self._clean_section(
            self._section(body, "salesforce enrichment", "sources", "source freshness")
        )
        if "ephemeral" in body.lower() and re.search(r"saves? only on", body, re.I):
            entry["output_location"] = "Ephemeral — not auto-saved (saves only on request)"
        else:
            m = re.search(r"(~/airbyte-work/01-customers/\S*?/outputs/\S+)", body)
            if m:
                entry["output_location"] = m.group(1).strip("`")
        return entry

    def help(self) -> list[dict]:
        """Help doc content, auto-extracted from each skill's SKILL.md."""
        return [self._derive_help(s["id"], s["label"], s["blurb"]) for s in self.skills]

    # -----------------------------------------------------------------------
    # Planning and invocation
    # -----------------------------------------------------------------------
    def plan(self, skill: str, account: str, opp_slug: str | None) -> dict:
        if skill not in self.skill_ids:
            raise SkillRuntimeError(400, f"Unknown skill: {skill}")
        safe_account = self._safe(account)
        safe_opp = self._safe(opp_slug)
        result = orchestrator.check_prerequisites(skill, safe_account, safe_opp, self.customers_dir)
        return result.model_dump()

    @staticmethod
    def _build_prompt(
        *,
        freeform: str | None,
        skill: str | None,
        account: str,
        opportunity: str | None,
        extra: str | None,
        out_dir: Path | None,
    ) -> str:
        if freeform:
            prompt = freeform.strip() + f" (for the account {account}"
            if opportunity:
                prompt += f", opportunity '{opportunity}'"
            prompt += ".)"
        else:
            prompt = f"Use the {skill} skill for {account}."
            if opportunity:
                prompt += f" This is for the opportunity '{opportunity}'."
            if extra:
                prompt += f" Additional context: {extra.strip()}"
        if out_dir:
            prompt += (
                f" IMPORTANT: save any output file under {out_dir}/<skill-name>/ "
                f"instead of the default account outputs folder."
            )
        return prompt

    async def invoke(
        self,
        *,
        account: str,
        skill: str | None,
        opportunity: str | None,
        opp_slug: str | None,
        extra: str | None,
        freeform: str | None,
        override_prerequisites: bool,
        approve_permissions: bool,
    ) -> dict:
        safe_account = self._safe(account)
        safe_opp = self._safe(opp_slug)

        if not freeform and skill not in self.skill_ids:
            raise SkillRuntimeError(400, f"Unknown skill: {skill}")

        out_dir = None
        if safe_opp:
            try:
                out_dir = self.output_service.opp_outputs_dir(safe_account, safe_opp)
            except OutputError as exc:
                raise SkillRuntimeError(exc.status_code, exc.detail) from exc

        # Deterministic prerequisite check. Free-form instructions and explicit
        # overrides skip the planner.
        if not freeform and not override_prerequisites and skill in self.skill_ids:
            plan = orchestrator.check_prerequisites(skill, safe_account, safe_opp, self.customers_dir)
            if not plan.ready:
                return {"prerequisites": plan.model_dump(), "blocked": True}

        # Permission approval check.
        profile = self._permission_profile(skill, freeform=bool(freeform))
        if profile.requires_approval and not approve_permissions:
            return {"permissions": profile.model_dump(), "blocked": True}

        prompt = self._build_prompt(
            freeform=freeform,
            skill=skill,
            account=safe_account,
            opportunity=opportunity,
            extra=extra,
            out_dir=out_dir,
        )

        sig: Any = (safe_account, safe_opp, skill or "freeform", (freeform or extra or "")[:80])
        reused = self.job_service.find_reused_job(sig)
        if reused:
            jid, j = reused
            reused_resp = {"job_id": jid, "status": "running", "reused": True}
            if j.get("persistence_warning"):
                reused_resp["persistence_warning"] = j["persistence_warning"]
            return reused_resp

        skill_id = skill or "freeform"
        try:
            job_id, persist_warn = await self.job_service.launch(
                account=safe_account,
                opp_slug=safe_opp,
                skill=skill_id,
                opportunity=opportunity,
                sig=sig,
                prompt=prompt,
                meta={
                    "account": safe_account,
                    "opp_slug": safe_opp,
                    "skill": skill_id,
                    "opportunity": opportunity,
                },
            )
        except HTTPException as exc:
            # JobService may raise HTTPException for launch failures; wrap consistently.
            raise SkillRuntimeError(exc.status_code, exc.detail) from exc

        new_resp = {"job_id": job_id, "status": "running", "reused": False}
        if persist_warn:
            new_resp["persistence_warning"] = persist_warn
        return new_resp
