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

        env = self._derive_env()
        return Workspace(root=workspace_root, account=self.manifest.fixtures.account, env=env)

    def _install_config(self, workspace_root: Path) -> None:
        config_source = self.eval_root / self.manifest.fixtures.config
        if not config_source.exists():
            raise FileNotFoundError(f"Config fixture not found: {config_source}")
        raw = config_source.read_text(encoding="utf-8")
        raw = raw.replace("TMP_DIR", str(workspace_root))
        raw = raw.replace("{{ tmp_dir }}", str(workspace_root))
        config_path = workspace_root / ".se-config.yaml"
        config_path.write_text(raw, encoding="utf-8")

    def _copy_transcripts(self, workspace_root: Path) -> None:
        for item in self.manifest.fixtures.transcripts:
            source = self.eval_root / item.source
            target = workspace_root / item.target
            if not source.exists():
                raise FileNotFoundError(f"Transcript fixture not found: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    def _copy_existing_outputs(self, workspace_root: Path) -> None:
        for item in self.manifest.fixtures.existing_outputs:
            source = self.eval_root / item.source
            target = workspace_root / item.target
            if not source.exists():
                raise FileNotFoundError(f"Existing output fixture not found: {source}")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

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


class _ClaudeHome:
    """Prepare an isolated `$HOME` for the `claude` CLI."""

    def __init__(self, workspace_root: Path, repo_root: Path) -> None:
        self.workspace_root = workspace_root
        self.repo_root = repo_root
        self.home = workspace_root / ".claude-home"

    def prepare(self) -> Path:
        """Create an isolated home with a copy of the skills and stub external tools."""
        if self.home.exists():
            shutil.rmtree(self.home, ignore_errors=True)
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
        cmd.extend(["--disallowed-tools", " ".join(disallowed)])

        env = os.environ.copy()
        env["HOME"] = str(claude_home)
        env["XDG_CONFIG_HOME"] = str(claude_home / ".config")
        env["XDG_CACHE_HOME"] = str(claude_home / ".cache")
        env["SE_WORKSPACE"] = str(workspace_root)
        env["PATH"] = f"{claude_home / 'bin'}{os.pathsep}{env.get('PATH', '')}"

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
        "full-qual": ["Business Qual Summary", "Technical Qual Summary"],
        "connector-feasibility": ["Connector Coverage", "Fit Verdict", "Recommended Next Actions"],
        "poc-plan": ["Scope", "Success Criteria", "Timeline", "Risks & Mitigations"],
        "roi-business-case": ["At a Glance", "One-Slide Summary", "TCO Comparison", "Assumptions"],
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
            "unverified-connector",
            "unverified-entitlement",
            "hourly-sync-constraint",
            "sfdc-transcript-conflict",
            "next-move-low-evidence",
            "missing-technical-input",
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
        if scenario == "unverified-connector":
            return "🟡 cannot verify availability"
        if scenario == "next-move-low-evidence":
            return "🟡 Low confidence — gather more evidence"
        if scenario == "sfdc-transcript-conflict" and self.skill == "deal-assessment":
            return "🟡 SFDC stage conflicts with transcript"
        return "🟢 viable with standard caveats"

    def _confidence(self) -> str:
        scenario = self._scenario()
        if scenario in {"next-move-low-evidence", "unverified-connector", "unverified-entitlement"}:
            return "Low"
        if scenario == "sfdc-transcript-conflict":
            return "Medium-Low"
        return "Medium"

    def _constraints_contain(self, *needles: str) -> bool:
        combined = "\n".join(self.manifest.customer_constraints).lower()
        return any(needle.lower() in combined for needle in needles)

    def _section_body(self, section: str) -> str:
        if section == "Data Volume & Scale":
            return self._data_volume_body()
        if section == "Success Criteria":
            return self._success_criteria_body()
        if section == "Scope":
            return self._poc_scope_body()
        if section == "Connector Coverage":
            return self._connector_coverage_body()
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
        if scenario == "hourly-sync-constraint" or "hourly" in "\n".join(self.manifest.customer_constraints).lower():
            return (
                "- Demonstrate hourly sync for the full 2M rows/day volume within business-hours skew.\n"
                "- Metric: reduction in manual pipeline maintenance time (from biz-qual)."
            )
        return "- Success criteria map to MEDDPICC Metrics."

    def _poc_scope_body(self) -> str:
        if self._scenario() == "unverified-entitlement" or self._constraints_contain("byok", "kms"):
            return "POC scope is **blocked** pending resolution of the BYOK/KMS requirement."
        return "POC scope is bounded to the sources and destinations named in the transcript."

    def _connector_coverage_body(self) -> str:
        if self._scenario() == "unverified-connector":
            return (
                "- `source-foo-bar` could not be verified against the public registry or product sources.\n"
                "- `source-snowflake` and `destination-snowflake` exist in the registry.\n"
                "- **Availability for `source-foo-bar` is flagged as unverified; do not commit to the customer.**"
            )
        return "- Connector coverage is based on registry metadata and labeled accordingly."

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

    def __init__(self, timeout: int = 300, permission_mode: str = "readOnly", bare: bool = True) -> None:
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

        env = os.environ.copy()
        home = Path(tempfile.mkdtemp(prefix="claude-semantic-"))
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")
        env["XDG_CACHE_HOME"] = str(home / ".cache")

        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=env,
            )
        finally:
            shutil.rmtree(home, ignore_errors=True)

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

        for skill in self.manifest.skills_under_test:
            output_dir = workspace.customer_dir / "outputs"
            result = self.executor.run(
                skill=skill,
                workspace_root=workspace.root,
                account=workspace.account,
                extra_prompt=(
                    "This is a synthetic evaluation run. Do not use real customer data. "
                    "If the skill offers to skip missing upstream qualification docs, choose the 'skip' option "
                    "and produce the requested output with appropriate flags. Do not chain into other skills."
                ),
                output_dir=output_dir,
                manifest=self.manifest,
            )
            evaluation = self._evaluate_skill(skill, result, env=workspace.env)
            skill_results.append(evaluation)
            all_failures.extend(evaluation.failures)

        passed = not all_failures
        return ManifestResult(
            manifest_id=self.manifest.id,
            title=self.manifest.title,
            passed=passed,
            status="passed" if passed else "failed",
            skill_results=skill_results,
            failures=all_failures,
            report_path=None,
        )

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
        shutil.rmtree(work_dir, ignore_errors=True)

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
                shutil.rmtree(manifest_work_dir, ignore_errors=True)

            print(f"{result.manifest_id}: {result.status}")
            if result.failures:
                for failure in result.failures:
                    print(f"  - {failure}")

        EvaluationReport.write_combined(results, report_dir=report_dir)
    finally:
        if not retain_failures or all(r.passed for r in results):
            shutil.rmtree(suite_root, ignore_errors=True)
        else:
            print(f"\nRetained failed workspaces in: {suite_root}")

    return results


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

    if args.command == "list":
        manifest_dir = args.manifest_dir or repo_root / "eval" / "manifests" / "phase1"
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


if __name__ == "__main__":
    sys.exit(main())
