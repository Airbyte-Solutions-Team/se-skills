"""Workspace setup, skill execution, manifest evaluation, and CLI.

The runner is intentionally thin: it builds a temporary customer workspace,
invokes one or more skills, and checks the resulting Markdown against the
deterministic assertions in the manifest. Phase 1B adds a real `claude` executor,
CLI entry points, and an optional semantic evaluator.
"""

from __future__ import annotations

import abc
import argparse
import ast
import dataclasses
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import yaml

from eval.assertions import SafeExpressionEvaluator
from eval.schemas.manifest import Manifest


# Upstream documents a downstream skill may require before it can produce output.
_UPSTREAM_REQUIREMENTS: Dict[str, List[str]] = {
    "poc-plan": ["biz-qual", "deployment-qual", "tech-qual", "connector-feasibility"],
    "mutual-close-plan": ["biz-qual", "deployment-qual", "tech-qual", "connector-feasibility"],
    "roi-business-case": ["biz-qual", "deployment-qual", "tech-qual", "connector-feasibility"],
    "deal-assessment": ["biz-qual", "tech-qual"],
}


def _is_within(child: Path, parent: Path) -> bool:
    """Return True when `child` is inside `parent`, compatible with Python <3.9."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _approved_temp_roots() -> List[Path]:
    """Return directories considered safe for evaluation workspaces."""
    roots = [Path(tempfile.gettempdir()).resolve()]
    for env in ("TMPDIR", "TEMP", "TMP"):
        value = os.environ.get(env)
        if value:
            roots.append(Path(value).resolve())
    override = os.environ.get("SE_EVAL_TMP_ROOT")
    if override:
        roots.append(Path(override).resolve())
    return roots


def _is_approved_temp_dir(path: Path) -> bool:
    """Fail closed: only allow temp dirs under an approved temporary root."""
    resolved = path.resolve()
    # Explicitly exclude the configured real customer workspace paths.
    for forbidden in (Path.home() / ".se-skills", Path.home() / "airbyte-work"):
        if _is_within(resolved, forbidden):
            return False
    return any(_is_within(resolved, root) for root in _approved_temp_roots())


def _safe_join(base: Path, rel: str) -> Path:
    """Resolve `base / rel` and reject absolute paths or escapes.

    The returned path is guaranteed to be inside `base`.
    """
    if os.path.isabs(rel):
        raise ValueError(f"absolute paths are not allowed: {rel!r}")
    if ".." in Path(rel).parts:
        raise ValueError(f"path traversal is not allowed: {rel!r}")
    resolved_base = base.resolve()
    target = (resolved_base / rel).resolve()
    if not _is_within(target, resolved_base):
        raise ValueError(f"path escapes the approved directory: {rel!r}")
    return target


@dataclasses.dataclass
class SkillResult:
    """Result of invoking a single skill."""

    skill: str
    output_text: str
    output_path: Optional[Path]
    returncode: int
    stdout: str
    stderr: str
    files_written: List[Path]
    refused: bool


@dataclasses.dataclass
class SkillEvaluationResult:
    """Skill result plus the outcome of structural, invariant, and semantic checks."""

    skill: str
    passed: bool
    refused: bool
    output_text: str
    output_path: Optional[Path]
    failures: List[str]
    warnings: List[str]
    assertion_results: List[Dict[str, Any]]
    invocation_errors: List[str] = dataclasses.field(default_factory=list)
    structural_failures: List[str] = dataclasses.field(default_factory=list)
    invariant_failures: List[str] = dataclasses.field(default_factory=list)
    semantic_results: List[Dict[str, Any]] = dataclasses.field(default_factory=list)
    prerequisite_override_used: bool = False

    @property
    def output_excerpt(self) -> str:
        return self.output_text[:500]


@dataclasses.dataclass
class ManifestResult:
    """Aggregated result for one scenario."""

    manifest_id: str
    title: str
    passed: bool
    status: str
    skill_results: List[SkillEvaluationResult]
    failures: List[str]
    report_path: Optional[Path]
    prerequisite_mode: str = "enforce"
    classification: str = "normal"
    override_used: bool = False


class SkillExecutor(abc.ABC):
    """Abstract interface for running a skill inside a workspace."""

    @abc.abstractmethod
    def run(
        self,
        skill: str,
        workspace_root: Path,
        account: str,
        extra_prompt: str,
        output_dir: Path,
        manifest: Manifest,
    ) -> SkillResult:
        """Run `skill` and return the generated output."""


class Workspace:
    """A temporary customer workspace used for one evaluation run."""

    def __init__(self, root: Path, account: str, env: Dict[str, Any]) -> None:
        self.root = root
        self.account = account
        self.env = env
        self.customer_dir = root / "customers" / account


class WorkspaceBuilder:
    """Construct a temporary workspace from a manifest and fixture files."""

    def __init__(self, manifest: Manifest, tmp_dir: Path, repo_root: Path) -> None:
        self.manifest = manifest
        self.tmp_dir = tmp_dir
        self.repo_root = repo_root
        self.eval_root = repo_root / "eval"

    def build(self) -> Workspace:
        """Create the workspace, copy fixtures, and return a `Workspace`."""
        if not _is_approved_temp_dir(self.tmp_dir):
            raise ValueError(
                f"Refusing to build a workspace in {self.tmp_dir}: not under an approved temporary root."
            )

        workspace_root = self.tmp_dir / "workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        customers_dir = workspace_root / "customers"
        customers_dir.mkdir(exist_ok=True)
        transcripts_dir = customers_dir / "_transcripts"
        transcripts_dir.mkdir(exist_ok=True)

        account_dir = customers_dir / self.manifest.fixtures.account
        account_dir.mkdir(exist_ok=True)
        (account_dir / "outputs").mkdir(exist_ok=True)

        self._install_config(workspace_root)
        self._copy_transcripts(workspace_root)
        self._copy_existing_outputs(workspace_root)
        self._provide_upstream_outputs(workspace_root)

        env = self._derive_env()
        return Workspace(root=workspace_root, account=self.manifest.fixtures.account, env=env)

    def _install_config(self, workspace_root: Path) -> None:
        config_source = _safe_join(self.eval_root, self.manifest.fixtures.config)
        if not config_source.exists():
            raise FileNotFoundError(f"Config fixture not found: {config_source}")
        raw = config_source.read_text(encoding="utf-8")
        raw = raw.replace("TMP_DIR", str(workspace_root))
        raw = raw.replace("{{ tmp_dir }}", str(workspace_root))
        config_path = workspace_root / ".se-config.yaml"
        config_path.write_text(raw, encoding="utf-8")

    def _copy_transcripts(self, workspace_root: Path) -> None:
        for item in self.manifest.fixtures.transcripts:
            source = _safe_join(self.eval_root, item.source)
            target = _safe_join(workspace_root, item.target)
            if not source.exists():
                raise FileNotFoundError(f"Transcript fixture not found: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def _copy_existing_outputs(self, workspace_root: Path) -> None:
        for item in self.manifest.fixtures.existing_outputs:
            source = _safe_join(self.eval_root, item.source)
            target = _safe_join(workspace_root, item.target)
            if not source.exists():
                raise FileNotFoundError(f"Existing output fixture not found: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def _provide_upstream_outputs(self, workspace_root: Path) -> None:
        """For `provide_fixtures` mode, synthesize upstream docs needed by downstream skills.

        If `fixtures.existing_outputs` already provides a document, it is preferred and
        will not be overwritten.
        """
        if self.manifest.execution.prerequisite_mode != "provide_fixtures":
            return

        account = self.manifest.fixtures.account
        outputs_dir = workspace_root / "customers" / account / "outputs"
        for skill in self.manifest.skills_under_test:
            for upstream in _UPSTREAM_REQUIREMENTS.get(skill, []):
                existing = sorted((outputs_dir / upstream).glob(f"{upstream}-*.md"))
                if existing:
                    continue
                doc = _synthetic_upstream_doc(upstream, self.manifest)
                if doc is None:
                    continue
                dest_dir = outputs_dir / upstream
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_path = dest_dir / f"{upstream}-{datetime.date.today().isoformat()}.md"
                dest_path.write_text(doc, encoding="utf-8")

    def _derive_env(self) -> Dict[str, Any]:
        """Return a flat dict of booleans that assertion expressions can read."""
        ref = self.manifest.environment.reference_data or {}
        repos = ref.get("repos", {})
        salesforce = self.manifest.environment.salesforce
        if isinstance(salesforce, dict):
            salesforce_enabled = bool(salesforce.get("enabled", False))
        else:
            salesforce_enabled = bool(salesforce)
        return {
            "registry_available": bool(ref.get("registry")),
            "airbyte_platform_available": bool(repos.get("airbyte_platform")),
            "airbyte_enterprise_available": bool(repos.get("airbyte_enterprise")),
            "salesforce_enabled": salesforce_enabled,
            "connector_models_enabled": bool(ref.get("connector_models", {}).get("enabled")),
        }


def _has_hard_constraint(manifest: Manifest, *needles: str) -> bool:
    """Return True when every `needle` appears in the customer constraints."""
    combined = "\n".join(manifest.customer_constraints).lower()
    return all(needle.lower() in combined for needle in needles)


def _synthetic_upstream_doc(skill: str, manifest: Manifest) -> Optional[str]:
    """Generate a minimal but plausible upstream document for `skill`."""
    account = manifest.fixtures.account
    today = datetime.date.today().isoformat()
    hourly = _has_hard_constraint(manifest, "hourly") or "hourly" in manifest.id
    byok = _has_hard_constraint(manifest, "byok", "kms")
    unverified = "unverified-connector" in manifest.id

    if skill == "biz-qual":
        return (
            f"# {account} — biz-qual: viable with standard caveats\n\n"
            f"**Date:** {today} · **Skill:** biz-qual\n\n"
            "## MEDDPICC Scorecard\n"
            "| Letter | Status | Evidence |\n"
            "|---|---|---|\n"
            "| Metrics | 🟢 quantified | Engineering pipeline maintenance cost stated |\n"
            "| Economic Buyer | 🟡 suspected | Engineering leadership mentioned |\n"
            "| Decision Criteria | 🟡 early | Hourly sync, Cloud/SaaS preferred |\n"
            "| Decision Process | 🟡 early | POC driven |\n\n"
            "## Gaps\n"
            "- Confirm economic buyer.\n"
            "- Validate exact source systems and row volumes.\n\n"
            "## Next Actions\n"
            "- Run deployment-model-qual if VPC/BYOK is a concern.\n"
        )
    if skill == "deployment-qual":
        return (
            f"# {account} — deployment-model-qual: Cloud viable\n\n"
            f"**Date:** {today} · **Skill:** deployment-model-qual\n\n"
            "## The Five Qualifying Questions\n"
            "| Question | Answer | Implication |\n"
            "|---|---|---|\n"
            f"| Deployment preference | {'VPC/Self-hosted' if byok else 'Cloud/SaaS'} | {'No fit on current Cloud' if byok else 'Cloud viable'} |\n"
            "| Data residency | None | No dedicated region required |\n"
            "| Multi-tenancy | None | Cloud viable |\n"
            f"| BYOK/KMS | {'Hard requirement' if byok else 'Not required'} | {'Park until verified' if byok else 'Cloud acceptable'} |\n"
            "| VPC isolation | None | Cloud viable |\n\n"
            "## Verdict\n"
            f"**{'🔴 park / no fit today' if byok else '🟢 Cloud viable'}**\n\n"
            "## Recommended Next Action\n"
            "- Proceed to tech-qual and connector-feasibility.\n"
        )
    if skill == "tech-qual":
        return (
            f"# {account} — tech-qual: viable with standard caveats\n\n"
            f"**Date:** {today} · **Skill:** tech-qual\n\n"
            "## Technical Fit Summary\n"
            "- Airbyte Cloud is technically viable for the described sources and destinations.\n\n"
            "## Data Sources & Destinations\n"
            "- Postgres, Salesforce, Snowflake.\n\n"
            "## Data Volume & Scale\n"
            f"- **Sync frequency:** {'hourly' if hourly else 'daily or as required'}.\n"
            "- **Volume:** ~2M rows/day, business-hours skew.\n"
            "- **Capacity implication:** sized for the stated cadence; no frequency reduction assumed.\n\n"
            "## Deployment Model\n"
            f"- {'No BYOK/KMS requirement.' if not byok else 'BYOK/KMS is a hard requirement; confirm entitlement.'}\n\n"
            "## Recommended Next Actions\n"
            "- Run connector-feasibility, then poc-plan.\n"
        )
    if skill == "connector-feasibility":
        connector_list = (
            "- `source-foo-bar` could not be verified.\n"
            "- `source-postgres`, `source-salesforce`, `destination-snowflake` are available.\n"
            if unverified
            else "- `source-postgres`, `source-salesforce`, `destination-snowflake` are available.\n"
        )
        return (
            f"# {account} — connector-feasibility: viable with caveats\n\n"
            f"**Date:** {today} · **Skill:** connector-feasibility\n\n"
            "## Connector Coverage\n"
            f"{connector_list}"
            "- Availability is based on registry metadata and labeled accordingly.\n\n"
            "## Fit Verdict\n"
            f"{'🟡 cannot verify availability' if unverified else '🟢 viable with standard caveats'}\n\n"
            "## Recommended Next Actions\n"
            "- Validate any unverified connectors before committing to the customer.\n"
        )
    return None


def _rmtree_surfacing(path: Path) -> None:
    """Remove a directory tree and raise RuntimeError if cleanup fails."""
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise RuntimeError(f"failed to remove temporary workspace {path}: {exc}") from exc


class _ClaudeHome:
    """Prepare an isolated `$HOME` for the `claude` CLI."""

    def __init__(self, workspace_root: Path, repo_root: Path) -> None:
        self.workspace_root = workspace_root
        self.repo_root = repo_root
        self.home = workspace_root / ".claude-home"

    def prepare(self) -> Path:
        """Create an isolated home with a copy of the skills and stub external tools."""
        if self.home.exists():
            _rmtree_surfacing(self.home)
        self.home.mkdir(parents=True, exist_ok=True)
        claude_dir = self.home / ".claude"
        claude_dir.mkdir(exist_ok=True)

        skills_dir = claude_dir / "skills"
        shutil.copytree(self.repo_root / "skills", skills_dir, dirs_exist_ok=True)

        bin_dir = self.home / "bin"
        bin_dir.mkdir(exist_ok=True)
        self._write_stub(bin_dir / "sf", "sf (Salesforce CLI)")
        self._write_stub(bin_dir / "gh", "gh (GitHub CLI)")
        self._write_stub(bin_dir / "curl", "curl")
        self._write_stub(bin_dir / "wget", "wget")

        config_dir = self.home / ".config"
        config_dir.mkdir(exist_ok=True)
        cache_dir = self.home / ".cache"
        cache_dir.mkdir(exist_ok=True)

        return self.home

    @staticmethod
    def _write_stub(path: Path, name: str) -> None:
        path.write_text(
            f"#!/bin/sh\necho \"{name} is disabled in the evaluation environment\" >&2\nexit 1\n",
            encoding="utf-8",
        )
        path.chmod(0o755)


class ClaudeExecutor(SkillExecutor):
    """Execute a skill by shelling out to `claude -p` in an isolated home."""

    _repo_root: Optional[Path] = None

    def __init__(
        self,
        timeout: int = 300,
        permission_mode: str = "acceptEdits",
        bare: bool = True,
        repo_root: Optional[Path] = None,
    ) -> None:
        self.timeout = timeout
        self.permission_mode = permission_mode
        self.bare = bare
        self._repo_root = repo_root or Path(__file__).parent.parent

    @classmethod
    def available(cls) -> bool:
        return shutil.which("claude") is not None

    def run(
        self,
        skill: str,
        workspace_root: Path,
        account: str,
        extra_prompt: str,
        output_dir: Path,
        manifest: Manifest,
    ) -> SkillResult:
        claude_home = _ClaudeHome(workspace_root, self._repo_root).prepare()

        prompt = f"Use the {skill} skill for {account}."
        if extra_prompt:
            prompt += f" {extra_prompt}"

        cmd = ["claude", "-p", prompt]
        if self.bare:
            cmd.append("--bare")
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        disallowed = [
            "Bash(sf *)",
            "Bash(gh *)",
            "Bash(curl *)",
            "Bash(wget *)",
            "Bash(git push *)",
            "Bash(git clone *)",
        ]
        cmd.extend(["--disallowed-tools"] + disallowed)

        env = self._isolated_env(claude_home, workspace_root)

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace_root),
                env=env,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("claude CLI is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            proc = subprocess.CompletedProcess(
                args=cmd,
                returncode=-1,
                stdout="",
                stderr=f"claude subprocess timed out after {self.timeout}s",
            )

        return self._parse_result(skill, proc, output_dir)

    def _parse_result(self, skill: str, proc: subprocess.CompletedProcess, output_dir: Path) -> SkillResult:
        skill_output_dir = output_dir / skill
        skill_output_dir.mkdir(parents=True, exist_ok=True)

        files_written: List[Path] = []
        output_path: Optional[Path] = None
        output_text = proc.stdout or ""

        if skill_output_dir.exists():
            pattern = f"{skill}-*.md"
            files_written = sorted(skill_output_dir.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
            if files_written:
                output_path = files_written[0]
                output_text = output_path.read_text(encoding="utf-8")

        refused = proc.returncode != 0 or self._looks_like_refusal(output_text)

        return SkillResult(
            skill=skill,
            output_text=output_text,
            output_path=output_path,
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            files_written=files_written,
            refused=refused,
        )

    @staticmethod
    def _looks_like_refusal(text: str) -> bool:
        lowered = text.lower()
        return any(
            phrase in lowered
            for phrase in [
                "cannot generate",
                "refuse to run",
                "refused to run",
                "zero transcripts",
                "no transcripts",
                "cannot run",
            ]
        )

    @staticmethod
    def _isolated_env(claude_home: Path, workspace_root: Path) -> Dict[str, str]:
        """Return a minimal, isolated environment for the `claude` subprocess.

        Only well-known safe variables are inherited; secrets such as GitHub or
        cloud tokens are kept out of the child process where practical.
        """
        allowed = {
            "ANTHROPIC_API_KEY",
            "HOME",
            "LANG",
            "LC_ALL",
            "PATH",
            "PYTHONPATH",
            "SE_WORKSPACE",
            "TERM",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
        }
        env = {k: v for k, v in os.environ.items() if k in allowed}
        env["HOME"] = str(claude_home)
        env["XDG_CONFIG_HOME"] = str(claude_home / ".config")
        env["XDG_CACHE_HOME"] = str(claude_home / ".cache")
        env["SE_WORKSPACE"] = str(workspace_root)
        env["PATH"] = f"{claude_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"
        return env


class MockOutputBuilder:
    """Generate a plausible, scenario-aware Markdown output for testing."""

    _skill_sections: Dict[str, List[str]] = {
        "prep-call": ["What the AE Already Learned", "Reframe Hypothesis", "Upfront Contract"],
        "post-call": ["Key Takeaways", "Action Items", "Sources & Destinations"],
        "deployment-model-qual": ["The Five Qualifying Questions", "Verdict", "Recommended Next Action"],
        "biz-qual": ["MEDDPICC Scorecard", "Gaps", "Next Actions"],
        "tech-qual": [
            "Technical Fit Summary",
            "Data Sources & Destinations",
            "Data Volume & Scale",
            "Deployment Model",
            "Security & Compliance",
            "Recommended Next Actions",
        ],
        "full-qual": ["Business Qual Summary", "Technical Qual Summary", "Closing Summary"],
        "connector-feasibility": ["Connector Coverage", "Fit Verdict", "Recommended Next Actions"],
        "poc-plan": ["Scope", "Success Criteria", "Timeline", "Risks & Mitigations"],
        "roi-business-case": [
            "Current-State Baseline",
            "Airbyte Cost Projection",
            "3-Year TCO Comparison",
            "Payback & Sensitivity",
            "One-Slide Summary",
            "Assumptions & Confirms",
        ],
        "mutual-close-plan": ["Mutual Action Plan", "Owners & Dates", "Risk Mitigation"],
        "follow-up-email": ["Email"],
        "objection-handler": ["Objection", "Talk Track"],
        "internal-prep": ["Meeting Type", "Key Points", "Recommended Messaging"],
        "account-refresher": ["10-Second Version", "Players", "Open Items"],
        "next-move": ["At a Glance", "Current read", "Ranked Next Moves"],
        "deal-assessment": ["MEDDPICC Pre-Scorecard", "Probability Verdict", "Coaching Observations"],
        "coverage-handoff": ["Coverage Summary", "Open Deals", "Escalation Path"],
    }

    def __init__(self, manifest: Manifest, skill: str, env: Dict[str, Any], account: str = "Acme") -> None:
        self.manifest = manifest
        self.skill = skill
        self.env = env
        self.account = account

    def build(self) -> str:
        """Return a Markdown string that satisfies the manifest's assertions."""
        if self.skill in self.manifest.expected_refusal_for:
            return self._refusal_output()

        lines: List[str] = [f"# {self.account} — {self._skill_title()}: {self._verdict()}"]
        lines.append(f"**Date:** {datetime.date.today().isoformat()} · **Skill:** {self.skill}")
        lines.append("")

        lines.append("## At a Glance")
        lines.append(f"- **Verdict:** {self._verdict()}")
        lines.append(f"- **Confidence:** {self._confidence()}")
        lines.append(f"- **Source confidence:** Synthetic transcript only")
        lines.append("")

        for section in self._sections():
            lines.append(f"## {section}")
            lines.append(self._section_body(section))
            lines.append("")

        lines.append("## Source Coverage")
        lines.append("- Synthetic transcript used for evaluation.")
        lines.append(f"- Workspace: `TMP_DIR` (replaced at runtime)")
        lines.append(f"- Environment flags: {json.dumps(self.env, default=str)}")
        lines.append("")

        return "\n".join(lines)

    def _skill_title(self) -> str:
        return self.skill.replace("-", " ").title()

    def _scenario(self) -> str:
        """Return the scenario keyword embedded in the manifest id."""
        for key in (
            "connector-cdc-unverified",
            "poc-difficult-criterion",
            "tech-qual-missing-critical",
            "next-move-no-repeat",
            "unverified-connector",
            "unverified-entitlement",
            "hourly-sync-constraint",
            "sfdc-transcript-conflict",
            "next-move-low-evidence",
            "missing-technical-input",
            "full-qual-partial-failure",
        ):
            if key in self.manifest.id:
                return key
        return "default"

    def _sections(self) -> List[str]:
        expected = self.manifest.expected_sections_for_skill(self.skill)
        defaults = [s for s in self._skill_sections.get(self.skill, []) if s not in expected]
        combined = list(dict.fromkeys(expected + defaults))
        # At a Glance is already rendered as the decision card; Source Coverage is always last.
        return [
            s
            for s in combined
            if s.lower() not in {"at a glance", "source coverage"}
        ]

    def _verdict(self) -> str:
        scenario = self._scenario()
        if scenario == "unverified-entitlement" or self._constraints_contain("byok", "kms"):
            return "🔴 park / no fit today"
        if scenario == "connector-cdc-unverified" and self.skill == "connector-feasibility":
            return "🟡 cannot verify full use-case fit — CDC/sync mode unverified"
        if scenario == "poc-difficult-criterion" and self.skill == "poc-plan":
            return "🟡 viable with a hard success criterion"
        if scenario == "tech-qual-missing-critical" and self.skill == "tech-qual":
            return "🟡 Moderate — missing a critical requirement"
        if scenario == "next-move-no-repeat" and self.skill == "next-move":
            return "🟢 ready to move forward — do not re-run existing quals"
        if scenario == "unverified-connector":
            return "🟡 cannot verify availability"
        if scenario == "next-move-low-evidence":
            return "🟡 Low confidence — gather more evidence"
        if scenario == "sfdc-transcript-conflict" and self.skill == "deal-assessment":
            return "🟡 SFDC stage conflicts with transcript"
        if scenario == "full-qual-partial-failure" and self.skill == "full-qual":
            return "🟡 partial — one child refused"
        return "🟢 viable with standard caveats"

    def _confidence(self) -> str:
        scenario = self._scenario()
        if scenario in {"next-move-low-evidence", "unverified-connector", "unverified-entitlement", "connector-cdc-unverified"}:
            return "Low"
        if scenario in {"sfdc-transcript-conflict", "tech-qual-missing-critical"}:
            return "Medium-Low"
        if scenario == "next-move-no-repeat":
            return "Medium"
        return "Medium"

    def _constraints_contain(self, *needles: str) -> bool:
        combined = "\n".join(self.manifest.customer_constraints).lower()
        return any(needle.lower() in combined for needle in needles)

    def _full_qual_business_summary_body(self) -> str:
        if self._scenario() == "full-qual-partial-failure":
            return "- biz-qual ran to completion on the business-discovery transcript.\n- No business-qualification blockers identified."
        return "- biz-qual and tech-qual ran to completion."

    def _full_qual_technical_summary_body(self) -> str:
        if self._scenario() == "full-qual-partial-failure":
            return "- **tech-qual was not run:** the transcript contains no technical discovery to qualify against.\n- Recommendation: re-run `tech-qual` after a technical call."
        return "- tech-qual ran to completion."

    def _full_qual_closing_summary_body(self) -> str:
        if self._scenario() == "full-qual-partial-failure":
            return (
                "- biz-qual: ✓ produced → `outputs/biz-qual/biz-qual-2026-07-14-eval.md`\n"
                "- tech-qual: ✗ refused — transcript has no technical discovery. Run `tech-qual` after a technical call.\n"
                "- Next up per the workflow: `connector-feasibility`, then `poc-plan`."
            )
        return (
            "- biz-qual: ✓ produced\n"
            "- tech-qual: ✓ produced\n"
            "- Next up per the workflow: `connector-feasibility`, then `poc-plan`."
        )

    def _section_body(self, section: str) -> str:
        if section == "Business Qual Summary":
            return self._full_qual_business_summary_body()
        if section == "Technical Qual Summary":
            return self._full_qual_technical_summary_body()
        if section == "Closing Summary":
            return self._full_qual_closing_summary_body()
        if section == "Data Volume & Scale":
            return self._data_volume_body()
        if section == "Success Criteria":
            return self._success_criteria_body()
        if section == "Scope":
            return self._poc_scope_body()
        if section == "Connector Coverage":
            return self._connector_coverage_body()
        if section == "Fit Verdict":
            return self._fit_verdict_body()
        if section == "The Five Qualifying Questions":
            return self._five_questions_body()
        if section == "Verdict":
            return self._verdict_body()
        if section == "MEDDPICC Pre-Scorecard":
            return self._meddpicc_body()
        if section == "Probability Verdict":
            return self._probability_body()
        if section == "Current read":
            return self._current_read_body()
        if section == "Ranked Next Moves":
            return self._ranked_moves_body()
        if section == "Deployment Model":
            return self._deployment_model_body()
        if section == "Security & Compliance":
            return self._security_body()
        if section == "Current-State Baseline":
            return self._roi_current_state_body()
        if section == "Airbyte Cost Projection":
            return self._roi_cost_projection_body()
        if section == "3-Year TCO Comparison":
            return self._roi_tco_body()
        if section == "Payback & Sensitivity":
            return self._roi_payback_body()
        if section == "One-Slide Summary":
            return self._roi_summary_body()
        if section == "Assumptions & Confirms":
            return self._roi_assumptions_body()
        return f"*Section content for {section}."

    def _data_volume_body(self) -> str:
        scenario = self._scenario()
        if scenario == "hourly-sync-constraint" or "hourly" in "\n".join(self.manifest.customer_constraints).lower():
            return (
                "- **Sync frequency / latency:** Hourly refresh is a hard requirement.\n"
                "- **Volume:** ~2M rows/day, business-hours skew.\n"
                "- **Capacity implication:** Sized for hourly cadence; no frequency reduction assumed."
            )
        return "- Volume and latency assumptions are labeled as `[confirm]`."

    def _success_criteria_body(self) -> str:
        scenario = self._scenario()
        if scenario == "unverified-entitlement" or self._constraints_contain("byok", "kms"):
            return (
                "- POC cannot proceed until the BYOK/KMS requirement is waived or resolved.\n"
                "- If waived, success criteria will map to a Metric from biz-qual."
            )
        if scenario == "poc-difficult-criterion":
            return (
                "- **Must-have:** Sync 50M rows end-to-end within 5 minutes. This is a hard, customer-stated requirement and is preserved as a must-have success criterion.\n"
                "- **Must-have:** Demonstrate incremental sync for the Postgres transactions table.\n"
                "- **Nice-to-have:** SE can configure the connector without engineering support."
            )
        if scenario == "hourly-sync-constraint" or "hourly" in "\n".join(self.manifest.customer_constraints).lower():
            return (
                "- Demonstrate hourly sync for the full 2M rows/day volume within business-hours skew.\n"
                "- Metric: reduction in manual pipeline maintenance time (from biz-qual)."
            )
        return "- Success criteria map to MEDDPICC Metrics."

    def _poc_scope_body(self) -> str:
        if self._scenario() == "unverified-entitlement" or self._constraints_contain("byok", "kms"):
            return "POC scope is **blocked** pending resolution of the BYOK/KMS requirement."
        if self._scenario() == "poc-difficult-criterion":
            return (
                "- **Minimum viable POC scope:** Postgres transactions → Snowflake incremental sync; validate schema, auth, and basic throughput.\n"
                "- **Optional stretch scope:** Increase volume test to 50M rows if time permits.\n"
                "- **Production requirements:** 50M rows end-to-end within 5 minutes remains a production requirement if not fully proven in the POC.\n"
                "- **POC-specific simplifications:** Test uses a representative subset with agreed proxy validation; full 50M at-scale validation is out of POC scope unless stretch scope is reached."
            )
        return "POC scope is bounded to the sources and destinations named in the transcript."

    def _connector_coverage_body(self) -> str:
        if self._scenario() == "connector-cdc-unverified":
            return (
                "- `source-postgres` exists in the registry.\n"
                "- The customer needs CDC on the transactions table. Whether Postgres CDC (WAL / Debezium) is enabled on their database is **unverified**.\n"
                "- Without confirmed CDC, the use-case fit is **🟡 Unverified** — do not present it as native/full support."
            )
        if self._scenario() == "unverified-connector":
            return (
                "- `source-foo-bar` could not be verified against the public registry or product sources.\n"
                "- `source-snowflake` and `destination-snowflake` exist in the registry.\n"
                "- **Availability for `source-foo-bar` is flagged as unverified; do not commit to the customer.**"
            )
        return "- Connector coverage is based on registry metadata and labeled accordingly."

    def _fit_verdict_body(self) -> str:
        scenario = self._scenario()
        if scenario == "connector-cdc-unverified":
            return (
                "| System | Connector | Availability | Use-case fit | Confidence | Top risk / gap |\n"
                "|---|---|---|---|---|---|\n"
                "| Postgres | source-postgres | 🟢 Cloud + SM | 🟡 Unverified — CDC not confirmed | Low | Confirm Postgres WAL/Debezium is enabled and replication slot privileges are granted |"
            )
        return "- Connector fit is based on registry metadata and the customer's stated requirements."

    def _five_questions_body(self) -> str:
        return (
            "| Question | Answer | Implication |\n"
            "|---|---|---|\n"
            "| Deployment preference | Cloud/Flex | TBD from transcript |\n"
            "| Data residency | None | Cloud viable |\n"
            "| Multi-tenancy | None | Cloud viable |\n"
            "| BYOK/KMS | **Yes, hard requirement** | 🔴 no fit on any offered shape |\n"
            "| VPC isolation | Data-plane only | Flex viable if BYOK resolved |"
        )

    def _verdict_body(self) -> str:
        if self._scenario() == "unverified-entitlement" or self._constraints_contain("byok", "kms"):
            return (
                "**🔴 park / no fit today.** A hard BYOK/KMS requirement is not supported on "
                "Cloud Pro or Enterprise Flex as of the latest product reality. "
                "Entitlement claim should be verified with the product team before confirming."
            )
        return f"**{self._verdict()}**. See the Five Qualifying Questions table for details."

    def _meddpicc_body(self) -> str:
        return (
            "| Letter | Status | Evidence |\n"
            "|---|---|---|\n"
            "| Metrics | 🟡 unquantified | Engineering cost mentioned but not validated |\n"
            "| Economic Buyer | 🔴 unknown | CFO on vacation; no EB identified |\n"
            "| Decision Criteria | 🟡 early | Still evaluating options |\n"
            "| Decision Process | 🟡 early | Procurement wants POC first |"
        )

    def _probability_body(self) -> str:
        if self._scenario() == "sfdc-transcript-conflict":
            return (
                "SFDC shows **Closed-Won** with a close date next week, but the transcript "
                "says the deal is still evaluating with no economic buyer identified and a Q4 timeline. "
                "**Trust the transcript**; probability is lowered to **20–40%** until the conflict is resolved."
            )
        return "Probability band is based on MEDDPICC scoring and labeled assumptions."

    def _current_read_body(self) -> str:
        scenario = self._scenario()
        if scenario == "next-move-no-repeat":
            return (
                "Fresh `biz-qual`, `deployment-qual`, `tech-qual`, and `connector-feasibility` artifacts already exist. "
                "No new signal has appeared that would justify re-running them. The next logical step is to scope the POC, not to repeat the qualification cycle."
            )
        if scenario == "next-move-low-evidence":
            return (
                "Only one old intro transcript exists and no qualification docs are available. "
                "We are too early to recommend POC/ROI motion; next step is discovery."
            )
        if scenario == "sfdc-transcript-conflict":
            return (
                "SFDC says Closed-Won next week while the transcript says still evaluating with no EB and Q4. "
                "We should reconcile the two before trusting either."
            )
        return "Current read is grounded in available evidence and labeled assumptions."

    def _ranked_moves_body(self) -> str:
        scenario = self._scenario()
        if scenario == "next-move-no-repeat":
            return (
                "1. **Run `poc-plan`** — all qualification docs are fresh; time to contract the POC scope, not re-qualify.\n"
                "2. **Do not repeat `deployment-model-qual`, `biz-qual`, `tech-qual`, or `connector-feasibility`** unless a new transcript, objection, or blocker has appeared.\n"
                "3. **Prepare the POC kickoff** using `prep-call` once success criteria are drafted."
            )
        if scenario == "next-move-low-evidence":
            return (
                "1. **Run `account-refresher`** — catch up on full account context.\n"
                "2. **Plan a discovery call with `prep-call`** — gather technical/business requirements.\n"
                "3. **Defer proof-of-concept work** until a qualification doc exists."
            )
        if scenario == "sfdc-transcript-conflict":
            return (
                "1. **Reconcile SFDC stage with AE** — Closed-Won appears premature.\n"
                "2. **Run `biz-qual` or `deal-assessment`** after updating transcript.\n"
                "3. **Defer proof-of-concept work** until EB is identified."
            )
        return "1. **Run the next logical skill** in the workflow chain."

    def _deployment_model_body(self) -> str:
        if self._scenario() == "unverified-entitlement" or self._constraints_contain("byok", "kms"):
            return (
                "Deployment model is **park / no fit** because BYOK/KMS is a hard requirement. "
                "If the requirement is waived, Enterprise Flex (data plane in customer VPC) becomes viable."
            )
        return "Deployment model is based on the deployment-qual verdict and current entitlements."

    def _security_body(self) -> str:
        if self._scenario() == "unverified-entitlement" or self._constraints_contain("byok", "kms"):
            return (
                "BYOK/customer-managed KMS is **not supported** on any currently offered Airbyte shape "
                "per the product-reality stamp. If the `airbyte-platform` entitlement checkout is unavailable, "
                "this claim is marked **believed — verify with [team]** and confidence is capped."
            )
        return "Security and compliance requirements are mapped to named entitlements where available."

    def _roi_current_state_body(self) -> str:
        return (
            "- Estimated current-state engineering cost: $[X] per year (derived from customer-stated FTE burden).\n"
            "- Maintenance / on-call burden: labeled as `[confirm]` if not quantified."
        )

    def _roi_cost_projection_body(self) -> str:
        scenario = self._scenario()
        if scenario == "hourly-sync-constraint" or "hourly" in "\n".join(self.manifest.customer_constraints).lower():
            return (
                "- **Primary scenario:** Customer's requested operating model with **hourly sync**, full stated scope, and concurrency assumptions.\n"
                "- Data-worker estimate is sized for hourly cadence; no silent reduction in frequency or scope.\n"
                "- If an optimization is modeled, it is labeled as an **alternative scenario** and the trade-off is shown."
            )
        return (
            "- Airbyte cost estimate uses the customer's requested operating model as the primary scenario.\n"
            "- Frequency, scope, and concurrency assumptions are shown explicitly and not silently reduced."
        )

    def _roi_tco_body(self) -> str:
        return (
            "| Year | Current-state cost | Airbyte cost | Switching/ramp cost | Net savings |\n"
            "|------|-------------------:|-------------:|--------------------:|------------:|\n"
            "| 1 | $[X] | $[Y] | $[Z] | $[X-Y-Z] |\n"
            "| 2 | $[X] | $[Y] | — | $[X-Y] |\n"
            "| 3 | $[X] | $[Y] | — | $[X-Y] |"
        )

    def _roi_payback_body(self) -> str:
        return (
            "- **Payback period:** [N] months (range: [N-M]–[N+M] months) — directional until data-worker pricing is confirmed.\n"
            "- **Inputs that swing the case most:** true concurrency target, exact data-worker pricing, volume growth rate."
        )

    def _roi_summary_body(self) -> str:
        return (
            "> **One-slide summary:** [X]-month payback, $[A] 3-yr savings vs current state, based on hourly sync and customer-stated FTE burden. "
            "Assumptions are labeled; missing inputs are listed."
        )

    def _roi_assumptions_body(self) -> str:
        scenario = self._scenario()
        base = (
            "- **Customer-confirmed inputs:** [list]\n"
            "- **[confirm] inputs (SE must validate):** [list]\n"
        )
        if scenario == "hourly-sync-constraint" or "hourly" in "\n".join(self.manifest.customer_constraints).lower():
            return (
                base
                + "- **Missing inputs that materially affect the result:** true concurrency, data-worker pricing, exact volume growth. "
                "Hourly sync and 2M rows/day are the baseline; lowering frequency would materially change the worker estimate."
            )
        return base + "- **Missing inputs that materially affect the result:** [list and sensitivity]"

    def _refusal_output(self) -> str:
        reason = "the transcript does not contain the required customer voice"
        if self.skill == "tech-qual":
            reason = "the transcript does not contain technical discovery"
        recommend = self.manifest.required_behavior[0] if self.manifest.required_behavior else "run the upstream skill"
        return (
            f"# {self.account} — {self._skill_title()}: refused\n\n"
            f"**Cannot generate {self.skill} for {self.account}** — {reason}.\n\n"
            f"> Recommend: {recommend}.\n\n"
            "## Source Coverage\n"
            "- Checked synthetic transcript: business-only, no qualifying content.\n"
        )


class MockExecutor(SkillExecutor):
    """Execute a skill by generating a deterministic mock Markdown output."""

    def run(
        self,
        skill: str,
        workspace_root: Path,
        account: str,
        extra_prompt: str,
        output_dir: Path,
        manifest: Manifest,
    ) -> SkillResult:
        env = WorkspaceBuilder(manifest, workspace_root.parent, Path("/dev/null"))._derive_env()
        builder = MockOutputBuilder(manifest, skill, env, account)
        text = builder.build()

        skill_output_dir = output_dir / skill
        skill_output_dir.mkdir(parents=True, exist_ok=True)
        today = datetime.date.today().isoformat()
        output_path = skill_output_dir / f"{skill}-{today}-eval.md"
        output_path.write_text(text, encoding="utf-8")

        return SkillResult(
            skill=skill,
            output_text=text,
            output_path=output_path,
            returncode=0,
            stdout=text,
            stderr="",
            files_written=[output_path],
            refused=skill in manifest.expected_refusal_for,
        )


class SemanticEvaluator:
    """Optional LLM-as-judge evaluator using the `claude` CLI.

    The evaluator is intentionally minimal: it takes a rubric from the manifest,
    asks a model to assess the generated output, and returns machine-readable
    results with excerpts and confidence. It does not run unless explicitly
    requested, so deterministic tests remain fast and free.
    """

    def __init__(self, timeout: int = 300, permission_mode: str = "auto", bare: bool = True) -> None:
        self.timeout = timeout
        self.permission_mode = permission_mode
        self.bare = bare

    @classmethod
    def available(cls) -> bool:
        return shutil.which("claude") is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))

    def evaluate(self, output: str, manifest: Manifest, skill: str) -> List[Dict[str, Any]]:
        """Return a list of semantic check results for the given output."""
        rubric = self._rubric(manifest)
        if not rubric:
            return []

        prompt = self._build_prompt(output, manifest, skill, rubric)
        result = self._call_claude(prompt)
        return self._parse_result(result, rubric)

    def _rubric(self, manifest: Manifest) -> List[str]:
        if manifest.model_judge.enabled and manifest.model_judge.criteria:
            return manifest.model_judge.criteria
        combined: List[str] = []
        for item in manifest.required_behavior:
            combined.append(f"Required: {item}")
        for item in manifest.forbidden_behavior:
            combined.append(f"Forbidden: {item}")
        return combined

    def _build_prompt(self, output: str, manifest: Manifest, skill: str, rubric: List[str]) -> str:
        rubric_text = "\n".join(f"{i + 1}. {item}" for i, item in enumerate(rubric))
        return textwrap.dedent(
            f"""\
            You are an expert evaluator assessing an AI-generated SE skill output.

            Manifest: {manifest.id} ({manifest.title})
            Skill: {skill}

            Evaluate the output against each item in the rubric below. For each item,
            return a JSON object with these fields:
            - "criterion": the rubric item text
            - "passed": true/false
            - "confidence": "High", "Medium", or "Low"
            - "excerpt": a short, relevant quote from the output (or empty string if none)
            - "reasoning": one concise sentence explaining the verdict

            Return ONLY a JSON array of objects. Do not wrap it in markdown fences.

            Rubric:
            {rubric_text}

            Output:
            ---
            {output}
            ---
            """
        )

    def _call_claude(self, prompt: str) -> subprocess.CompletedProcess:
        cmd = ["claude", "-p", prompt]
        if self.bare:
            cmd.append("--bare")
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])

        home = Path(tempfile.mkdtemp(prefix="claude-semantic-"))
        # Keep the judge environment minimal so secrets do not leak to logs.
        env = ClaudeExecutor._isolated_env(home, home)

        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        finally:
            _rmtree_surfacing(home)

    def _parse_result(self, proc: subprocess.CompletedProcess, rubric: List[str]) -> List[Dict[str, Any]]:
        text = proc.stdout or ""
        if proc.returncode != 0:
            return [
                {
                    "criterion": rubric[0] if rubric else "semantic evaluation",
                    "passed": False,
                    "confidence": "Low",
                    "excerpt": "",
                    "reasoning": f"claude judge exited {proc.returncode}: {proc.stderr[:500]}",
                }
            ]

        # Find the first JSON array in the response.
        match = re.search(r"\[.*\]", text, re.DOTALL)
        if not match:
            return [
                {
                    "criterion": rubric[0] if rubric else "semantic evaluation",
                    "passed": False,
                    "confidence": "Low",
                    "excerpt": "",
                    "reasoning": "Could not parse a JSON array from the judge response.",
                }
            ]

        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return [self._normalize_item(item) for item in parsed]
        except json.JSONDecodeError:
            pass

        return [
            {
                "criterion": rubric[0] if rubric else "semantic evaluation",
                "passed": False,
                "confidence": "Low",
                "excerpt": "",
                "reasoning": "Judge response contained invalid JSON.",
            }
        ]

    @staticmethod
    def _normalize_item(item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            item = {"criterion": str(item), "passed": False, "confidence": "Low", "excerpt": "", "reasoning": "Unexpected item type."}
        return {
            "criterion": str(item.get("criterion", "")),
            "passed": bool(item.get("passed", False)),
            "confidence": str(item.get("confidence", "Low")),
            "excerpt": str(item.get("excerpt", "")),
            "reasoning": str(item.get("reasoning", "")),
        }


class ManifestEvaluator:
    """Evaluate one manifest against a skill executor."""

    def __init__(
        self,
        manifest: Manifest,
        executor: SkillExecutor,
        semantic_evaluator: Optional[SemanticEvaluator] = None,
    ) -> None:
        self.manifest = manifest
        self.executor = executor
        self.semantic_evaluator = semantic_evaluator

    def evaluate(self, workspace: Workspace) -> ManifestResult:
        """Run every skill in the manifest and check deterministic assertions."""
        skill_results: List[SkillEvaluationResult] = []
        all_failures: List[str] = []
        override_used = False

        extra_prompt = self._extra_prompt_for_mode()
        for skill in self.manifest.skills_under_test:
            output_dir = workspace.customer_dir / "outputs"
            result = self.executor.run(
                skill=skill,
                workspace_root=workspace.root,
                account=workspace.account,
                extra_prompt=extra_prompt,
                output_dir=output_dir,
                manifest=self.manifest,
            )
            evaluation = self._evaluate_skill(skill, result, env=workspace.env)
            skill_results.append(evaluation)
            all_failures.extend(evaluation.failures)
            if evaluation.prerequisite_override_used:
                override_used = True

        passed = not all_failures
        return ManifestResult(
            manifest_id=self.manifest.id,
            title=self.manifest.title,
            passed=passed,
            status="passed" if passed else "failed",
            skill_results=skill_results,
            failures=all_failures,
            report_path=None,
            prerequisite_mode=self.manifest.execution.prerequisite_mode,
            classification=self.manifest.execution.classification,
            override_used=override_used,
        )

    def _extra_prompt_for_mode(self) -> str:
        """Return a scenario-specific prompt based on prerequisite handling."""
        mode = self.manifest.execution.prerequisite_mode
        base = "This is a synthetic evaluation run. Do not use real customer data. Do not chain into other skills."
        if mode == "explicit_override":
            return (
                base
                + " If the skill offers to skip missing upstream qualification docs, choose the 'skip' option "
                "and produce the requested output with appropriate flags."
            )
        if mode == "provide_fixtures":
            return base + " Upstream qualification documents have been synthesized for this scenario."
        return base

    def _evaluate_skill(
        self, skill: str, result: SkillResult, env: Dict[str, Any]
    ) -> SkillEvaluationResult:
        invocation_errors: List[str] = []
        structural_failures: List[str] = []
        invariant_failures: List[str] = []
        warnings: List[str] = []
        assertion_results: List[Dict[str, Any]] = []
        output = result.output_text

        if result.returncode != 0:
            invocation_errors.append(
                f"[{skill}] claude exited with code {result.returncode}: {result.stderr[:500]}"
            )
        elif result.stderr:
            # Non-fatal stderr is recorded as a warning, not a failure.
            warnings.append(f"[{skill}] stderr: {result.stderr[:500]}")

        # Required sections (structural validation).
        for section in self.manifest.expected_sections_for_skill(skill):
            if not self._has_section(output, section):
                msg = f"[{skill}] missing required section: {section}"
                structural_failures.append(msg)

        # Deterministic assertions (business-invariant validation).
        for assertion in self.manifest.deterministic_assertions:
            if assertion.skills is not None and skill not in assertion.skills:
                continue
            applicable = True
            if assertion.when:
                try:
                    applicable = SafeExpressionEvaluator(
                        output=output, manifest=self.manifest.model_dump(), env=env
                    ).evaluate(assertion.when)
                except Exception as exc:
                    msg = f"[{skill}] assertion '{assertion.name}' when-clause error: {exc}"
                    invariant_failures.append(msg)
                    applicable = False
            if not applicable:
                continue
            try:
                passed = SafeExpressionEvaluator(
                    output=output, manifest=self.manifest.model_dump(), env=env
                ).evaluate(assertion.check)
            except Exception as exc:
                passed = False
                invariant_failures.append(f"[{skill}] assertion '{assertion.name}' error: {exc}")
            assertion_results.append(
                {
                    "name": assertion.name,
                    "severity": assertion.severity,
                    "passed": passed,
                }
            )
            if not passed:
                invariant_failures.append(
                    f"[{skill}] assertion '{assertion.name}' ({assertion.severity}) failed"
                )

        # Refusal expectations (informational warnings).
        if skill in self.manifest.expected_refusal_for and not result.refused:
            warnings.append(
                f"[{skill}] expected a refusal but the skill produced an output"
            )
        if result.refused and skill not in self.manifest.expected_refusal_for:
            warnings.append(
                f"[{skill}] skill refused unexpectedly; treating as a model-behavior warning"
            )

        # Optional semantic / model-behavior evaluation.
        semantic_results: List[Dict[str, Any]] = []
        if self.semantic_evaluator is not None:
            semantic_results = self.semantic_evaluator.evaluate(output, self.manifest, skill)

        failures = invocation_errors + structural_failures + invariant_failures
        semantic_failed = [s for s in semantic_results if not s.get("passed")]
        if semantic_failed:
            for item in semantic_failed:
                failures.append(
                    f"[{skill}] semantic check failed: {item['criterion']} ({item['confidence']})"
                )

        passed = not failures

        return SkillEvaluationResult(
            skill=skill,
            passed=passed,
            refused=result.refused,
            output_text=output,
            output_path=result.output_path,
            failures=failures,
            warnings=warnings,
            assertion_results=assertion_results,
            invocation_errors=invocation_errors,
            structural_failures=structural_failures,
            invariant_failures=invariant_failures,
            semantic_results=semantic_results,
            prerequisite_override_used=self.manifest.execution.prerequisite_mode == "explicit_override",
        )

    @staticmethod
    def _has_section(markdown: str, heading: str) -> bool:
        from eval.assertions import has_section

        return has_section(markdown, heading)


class EvaluationReport:
    """Write machine-readable evaluation reports."""

    @staticmethod
    def write(result: ManifestResult, report_dir: Optional[Path] = None) -> Path:
        report_dir = report_dir or Path(__file__).parent / "results"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / f"{result.manifest_id}.json"

        structural = []
        invariants = []
        invocation = []
        semantic = []
        warnings = []
        for sr in result.skill_results:
            structural.extend([{"skill": sr.skill, "failure": f} for f in sr.structural_failures])
            invariants.extend([{"skill": sr.skill, "failure": f} for f in sr.invariant_failures])
            invocation.extend([{"skill": sr.skill, "failure": f} for f in sr.invocation_errors])
            semantic.extend(
                [{"skill": sr.skill, **s} for s in sr.semantic_results]
            )
            warnings.extend([{"skill": sr.skill, "warning": w} for w in sr.warnings])

        payload = {
            "manifest_id": result.manifest_id,
            "title": result.title,
            "status": result.status,
            "execution": {
                "prerequisite_mode": result.prerequisite_mode,
                "classification": result.classification,
                "override_used": result.override_used,
            },
            "categories": {
                "invocation": {"passed": not invocation, "failures": invocation},
                "structural": {"passed": not structural, "failures": structural},
                "business_invariants": {"passed": not invariants, "failures": invariants},
                "semantic": {"passed": not [s for s in semantic if not s.get("passed")], "results": semantic},
                "warnings": warnings,
            },
            "failures": result.failures,
            "skill_results": [
                {
                    "skill": sr.skill,
                    "passed": sr.passed,
                    "refused": sr.refused,
                    "output_excerpt": sr.output_excerpt,
                    "output_text": sr.output_text,
                    "output_path": str(sr.output_path) if sr.output_path else None,
                    "failures": sr.failures,
                    "warnings": sr.warnings,
                    "assertion_results": sr.assertion_results,
                    "invocation_errors": sr.invocation_errors,
                    "structural_failures": sr.structural_failures,
                    "invariant_failures": sr.invariant_failures,
                    "semantic_results": sr.semantic_results,
                }
                for sr in result.skill_results
            ],
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path

    @staticmethod
    def write_combined(results: List[ManifestResult], report_dir: Optional[Path] = None) -> Path:
        report_dir = report_dir or Path(__file__).parent / "results"
        report_dir.mkdir(parents=True, exist_ok=True)
        path = report_dir / "phase1-report.json"
        payload = {
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "status": "passed" if all(r.passed for r in results) else "failed",
            "manifests": [
                {
                    "manifest_id": r.manifest_id,
                    "title": r.title,
                    "status": r.status,
                    "report_path": str(r.report_path) if r.report_path else None,
                    "execution": {
                        "prerequisite_mode": r.prerequisite_mode,
                        "classification": r.classification,
                        "override_used": r.override_used,
                    },
                }
                for r in results
            ],
        }
        path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return path


def _executor_for_name(name: str, repo_root: Path) -> SkillExecutor:
    if name == "mock":
        return MockExecutor()
    if name == "claude":
        if not ClaudeExecutor.available():
            raise RuntimeError("`claude` CLI is not installed or not on PATH")
        return ClaudeExecutor(repo_root=repo_root)
    if name == "anthropic":
        raise RuntimeError("Anthropic API executor is not implemented; use `claude` or `mock`.")
    raise ValueError(f"Unknown executor: {name}")


def _run_manifest(
    manifest_path: Path,
    repo_root: Path,
    executor_name: str,
    retain_workspace: bool,
    semantic: bool,
    report_dir: Optional[Path] = None,
    work_dir: Optional[Path] = None,
    quiet: bool = False,
) -> ManifestResult:
    """Run a single manifest inside a temporary or caller-provided work directory."""
    manifest = Manifest.from_yaml(manifest_path)
    executor = _executor_for_name(executor_name, repo_root)
    semantic_evaluator = SemanticEvaluator() if semantic and SemanticEvaluator.available() else None

    created_by_runner = work_dir is None
    if created_by_runner:
        work_dir = Path(tempfile.mkdtemp(prefix="se-eval-"))
    work_dir.mkdir(parents=True, exist_ok=True)

    workspace = WorkspaceBuilder(manifest, work_dir, repo_root).build()
    result = ManifestEvaluator(manifest, executor, semantic_evaluator=semantic_evaluator).evaluate(workspace)

    report_path = EvaluationReport.write(result, report_dir=report_dir)
    result.report_path = report_path

    if retain_workspace and not quiet:
        print(f"Retained workspace: {workspace.root}")
    elif created_by_runner:
        _rmtree_surfacing(work_dir)

    return result


def _run_suite(
    manifest_dir: Path,
    repo_root: Path,
    executor_name: str,
    retain_failures: bool,
    semantic: bool,
    report_dir: Optional[Path],
) -> List[ManifestResult]:
    """Run every manifest in `manifest_dir` sequentially, keeping failed workspaces when requested."""
    paths = sorted(manifest_dir.glob("*.yaml"))
    if not paths:
        raise FileNotFoundError(f"No manifests found in {manifest_dir}")

    suite_root = Path(tempfile.mkdtemp(prefix="se-suite-"))
    results: List[ManifestResult] = []
    try:
        for path in paths:
            print(f"\n=== Running {path.stem} ===")
            manifest_work_dir = suite_root / path.stem
            manifest_work_dir.mkdir(parents=True, exist_ok=True)
            result = _run_manifest(
                manifest_path=path,
                repo_root=repo_root,
                executor_name=executor_name,
                retain_workspace=True,  # caller cleans up non-failures
                semantic=semantic,
                report_dir=report_dir,
                work_dir=manifest_work_dir,
                quiet=True,
            )
            results.append(result)

            if result.passed and not retain_failures:
                _rmtree_surfacing(manifest_work_dir)

            print(f"{result.manifest_id}: {result.status}")
            if result.failures:
                for failure in result.failures:
                    print(f"  - {failure}")

        EvaluationReport.write_combined(results, report_dir=report_dir)
    finally:
        if not retain_failures or all(r.passed for r in results):
            _rmtree_surfacing(suite_root)
        else:
            print(f"\nRetained failed workspaces in: {suite_root}")

    return results


def _dispatch(args: argparse.Namespace, repo_root: Path) -> int:
    """Execute the CLI command selected by the user."""
    if args.command == "list":
        manifest_dir = args.manifest_dir or repo_root / "eval" / "manifests" / "phase1"
        if not manifest_dir.exists():
            raise FileNotFoundError(f"manifest directory not found: {manifest_dir}")
        for path in sorted(manifest_dir.glob("*.yaml")):
            manifest = Manifest.from_yaml(path)
            print(f"{path.stem}: {manifest.title}")
        return 0

    if args.command == "run":
        result = _run_manifest(
            manifest_path=args.manifest,
            repo_root=repo_root,
            executor_name=args.executor,
            retain_workspace=args.retain_workspace,
            semantic=args.semantic,
            report_dir=args.report_dir,
        )
        print(f"\n{result.manifest_id}: {result.status}")
        if result.failures:
            for failure in result.failures:
                print(f"  - {failure}")
        print(f"Report: {result.report_path}")
        return 0 if result.passed else 1

    if args.command == "run-suite":
        results = _run_suite(
            manifest_dir=args.manifest_dir,
            repo_root=repo_root,
            executor_name=args.executor,
            retain_failures=args.retain_failures,
            semantic=args.semantic,
            report_dir=args.report_dir,
        )
        print(f"\nCombined status: {'passed' if all(r.passed for r in results) else 'failed'}")
        return 0 if all(r.passed for r in results) else 1

    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval.runner", description="SE Skills evaluation runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--executor",
        choices=["mock", "claude", "anthropic"],
        default="mock",
        help="Executor to use. `claude` requires the `claude` CLI and ANTHROPIC_API_KEY.",
    )
    common.add_argument(
        "--semantic",
        action="store_true",
        default=False,
        help="Run the optional semantic (LLM-as-judge) evaluator.",
    )
    common.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="Directory for per-manifest and combined JSON reports (default: eval/results).",
    )

    run_parser = subparsers.add_parser("run", parents=[common], help="Evaluate a single manifest")
    run_parser.add_argument("--manifest", required=True, type=Path, help="Path to a manifest YAML file")
    run_parser.add_argument(
        "--retain-workspace",
        action="store_true",
        help="Keep the temporary workspace after the run for debugging.",
    )

    suite_parser = subparsers.add_parser("run-suite", parents=[common], help="Evaluate all manifests in a directory")
    suite_parser.add_argument("--manifest-dir", required=True, type=Path, help="Directory containing manifest YAML files")
    suite_parser.add_argument(
        "--retain-failures",
        action="store_true",
        help="Keep the workspace for any scenario that fails.",
    )

    list_parser = subparsers.add_parser("list", help="List available manifests")
    list_parser.add_argument("--manifest-dir", type=Path, default=None, help="Directory to list")

    args = parser.parse_args(argv)
    repo_root = Path(__file__).parent.parent

    try:
        return _dispatch(args, repo_root)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
