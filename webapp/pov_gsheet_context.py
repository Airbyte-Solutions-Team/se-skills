#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "pyyaml>=6.0"]
# ///
"""Deterministic account-context loader for the `pov-gsheet` skill.

Builds a structured POV context from the repository's existing workspace files,
prior skill outputs, transcripts, and optional external evidence (Salesforce,
Gong, Granola, Gmail, Slack). The skill then uses this context to populate the
Airbyte POV Success Criteria Google Sheet.

No `se-assistant`, DuckDB, personal paths, or Ryan-specific dependencies.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

# This script lives in webapp/; output_schema.py is a sibling module used by the
# webapp to parse generated Markdown outputs. Python puts the script directory on
# sys.path[0], so the direct import works.
import output_schema


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class SourceCoverageEntry(BaseModel):
    source: str
    available: bool
    path: str | None = None
    freshness: str | None = None
    material: bool = False
    note: str = ""
    status: str | None = None  # searched | unavailable | failed | skipped


class ExternalEvidence(BaseModel):
    """One extracted fact from an external source (Salesforce, Gong, Granola, Gmail, Slack).

    `fact_type` tells the normalizer how to merge this fact into the PovContext.
    `fact` is a typed payload keyed to the target model fields. `direct_customer`
    distinguishes a customer statement from internal interpretation.
    """

    source: str  # e.g. "salesforce", "gong", "granola", "gmail", "slack"
    source_id: str | None = None  # record/call/message id, or URL if safe
    retrieved_at: str | None = None  # ISO-8601 timestamp of retrieval
    account_name: str | None = None
    opportunity_name: str | None = None
    direct_customer: bool = False
    fact_type: str  # prospect | contact | business_objective | technical_system | success_criterion | milestone | feature_request | requirement | transcript | decision | action_item | note | unknown
    fact: dict[str, Any] = Field(default_factory=dict)
    raw: str | None = None  # verbatim snippet, kept short and PII-safe
    status: str = "ok"  # ok | unavailable | skipped | failed
    note: str = ""


class Contact(BaseModel):
    name: str
    role: str | None = None
    email: str | None = None
    side: str | None = None  # "Airbyte", "Customer", "Partner"
    source: str | None = None
    source_path: str | None = None
    notes: str | None = None


class BusinessObjective(BaseModel):
    objective: str
    desired_outcome: str | None = None
    evidence: str | None = None
    sources: list[str] = Field(default_factory=list)


class TechnicalSystem(BaseModel):
    name: str
    kind: str | None = None  # "source", "destination", "unknown"
    integration_type: str | None = None
    use_case: str | None = None
    evidence: str | None = None
    sources: list[str] = Field(default_factory=list)


class SuccessCriterion(BaseModel):
    use_case: str | None = None
    feature_or_capability: str
    validation_method: str | None = None
    acceptance_threshold: str | None = None
    in_scope: str | None = None  # Yes/No/TBD
    priority: str | None = None  # Must Have/Nice to Have/Out of Scope
    notes: str | None = None
    evidence: str | None = None
    sources: list[str] = Field(default_factory=list)


class Milestone(BaseModel):
    name: str
    target_date: str | None = None
    status: str = "Not Started"
    evidence: str | None = None
    sources: list[str] = Field(default_factory=list)


class FeatureRequest(BaseModel):
    date: str | None = None
    product_area: str | None = None
    description: str
    priority: str | None = None
    evidence: str | None = None
    sources: list[str] = Field(default_factory=list)


class Prospect(BaseModel):
    account_name: str
    opportunity_name: str | None = None
    stage: str | None = None
    owner: str | None = None
    se_name: str | None = None
    se_title: str | None = None
    next_step: str | None = None
    pov_start_date: str | None = None
    target_completion_date: str | None = None


class PovContext(BaseModel):
    prospect: Prospect
    contacts: dict[str, list[Contact]] = Field(default_factory=lambda: {"internal": [], "prospect": []})
    business_objectives: list[BusinessObjective] = Field(default_factory=list)
    technical_scope: dict[str, list[Any]] = Field(default_factory=lambda: {"sources": [], "destinations": [], "use_cases": [], "requirements": [], "dependencies": []})
    success_criteria: list[SuccessCriterion] = Field(default_factory=list)
    milestones: list[Milestone] = Field(default_factory=list)
    feature_requests: list[FeatureRequest] = Field(default_factory=list)
    architecture_notes: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    source_coverage: list[SourceCoverageEntry] = Field(default_factory=list)
    status: str = "blocked"  # complete | partial | blocked
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _classification(self) -> "PovContext":
        # Keep the classification honest based on evidence, not on whether a
        # specific connector was available.
        has_prospect = bool(self.prospect.account_name and self.prospect.account_name.strip())
        has_business_context = bool(self.business_objectives)
        has_connector_or_use_case = (
            self.technical_scope["sources"]
            or self.technical_scope["destinations"]
            or self.technical_scope["use_cases"]
        )
        has_success_criteria = bool(self.success_criteria)
        if has_prospect and (has_business_context or has_connector_or_use_case) and has_success_criteria:
            self.status = "complete"
        elif has_prospect and (has_business_context or has_connector_or_use_case or has_success_criteria):
            self.status = "partial"
        else:
            self.status = "blocked"
        return self


# ---------------------------------------------------------------------------
# Config and workspace resolution (mirrors _se-playbook.md Workspace Paths)
# ---------------------------------------------------------------------------
def _resolve_se_config(workspace_arg: str | None) -> tuple[Path, dict[str, Any]]:
    """Return (workspace_root, parsed_config).

    Resolution order:
    1. $SE_WORKSPACE/.se-config.yaml
    2. workspace_arg/.se-config.yaml
    3. ~/.se-skills/.se-config.yaml
    4. ~/airbyte-work/.se-config.yaml (legacy)
    """
    candidates: list[Path] = []
    env_ws = os.environ.get("SE_WORKSPACE")
    if env_ws:
        candidates.append(Path(env_ws).expanduser() / ".se-config.yaml")
    if workspace_arg:
        candidates.append(Path(workspace_arg).expanduser() / ".se-config.yaml")
    candidates.append(Path("~/.se-skills/.se-config.yaml").expanduser())
    candidates.append(Path("~/airbyte-work/.se-config.yaml").expanduser())

    for cfg_path in candidates:
        if cfg_path.exists():
            try:
                data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue
            # workspace_root resolution: env > config > default parent of config
            workspace = _resolve_workspace_root(cfg_path, data)
            return workspace, data

    # No config found; default to a safe workspace root.
    default_ws = Path(workspace_arg or env_ws or "~/.se-skills").expanduser()
    return default_ws, {}


def _resolve_workspace_root(config_path: Path, config: dict[str, Any]) -> Path:
    env_ws = os.environ.get("SE_WORKSPACE")
    if env_ws:
        return Path(env_ws).expanduser()
    if config.get("workspace_root"):
        return Path(config["workspace_root"]).expanduser()
    # Default is the directory containing the config file.
    return config_path.parent


def _get_path(config: dict[str, Any], workspace: Path, name: str, default_relative: str) -> Path:
    layout = config.get("layout", {})
    if layout.get(name):
        p = Path(layout[name]).expanduser()
        # Relative layout paths are resolved against the workspace root so that
        # a legacy numbered layout like "01-customers" keeps working.
        if not p.is_absolute():
            p = workspace / p
        return p
    if name == "customers_dir":
        return workspace / default_relative
    if name == "transcripts_dir":
        return (workspace / default_relative).parent / "_transcripts"
    if name == "notes_dir":
        return workspace / default_relative
    return workspace / default_relative


def _derive_paths(config: dict[str, Any], workspace: Path) -> dict[str, Path]:
    customers_dir = _get_path(config, workspace, "customers_dir", "customers")
    transcripts_dir = _get_path(config, workspace, "transcripts_dir", "customers/_transcripts")
    notes_dir = _get_path(config, workspace, "notes_dir", "notes")
    return {
        "customers_dir": customers_dir,
        "transcripts_dir": transcripts_dir,
        "notes_dir": notes_dir,
    }


# ---------------------------------------------------------------------------
# Salesforce (optional, best-effort)
# ---------------------------------------------------------------------------
def _salesforce_query(config: dict[str, Any], account_name: str, account_dir: Path) -> dict[str, Any]:
    """Best-effort Salesforce lookup via the `sf` CLI. Returns {} on any failure."""
    sf_cfg = config.get("salesforce", {}) or {}
    if not sf_cfg.get("enabled", True):
        return {}
    alias = sf_cfg.get("org_alias", "airbyte-prod")

    # Prefer the stored SFDC name if the account folder captured it.
    sfdc_name_path = account_dir / ".sfdc-name"
    account_like = sfdc_name_path.read_text().strip() if sfdc_name_path.exists() else account_name

    account_like = account_like.replace("'", "\\'")
    # Lightweight query; we intentionally do not query every field to keep the
    # dependency optional and fast.
    soql = (
        "SELECT Name, StageName, Amount, CloseDate, Owner.Name, SE_Name__c, "
        "Next_Step__c, Account.Name, Account.Id, Type "
        f"FROM Opportunity WHERE Account.Name LIKE '%{account_like}%' "
        "ORDER BY CloseDate DESC"
    )
    try:
        proc = subprocess.run(
            ["sf", "data", "query", "--query", soql, "--target-org", alias, "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return {}

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}

    records = payload.get("result", {}).get("records", []) if isinstance(payload, dict) else []
    if not records:
        return {}

    # Pick the active open opp (excluding renewals unless only one), then the
    # most recent closed/won.
    open_records = [r for r in records if not r.get("IsClosed") and r.get("Type") != "Renewal"]
    record = open_records[0] if open_records else records[0]

    account_record = record.get("Account") or {}
    return {
        "opportunity_name": record.get("Name"),
        "stage": record.get("StageName"),
        "owner": ((record.get("Owner") or {}).get("Name"))
        or record.get("OwnerId"),
        "se_name": record.get("SE_Name__c"),
        "close_date": record.get("CloseDate"),
        "next_step": record.get("Next_Step__c"),
        "account_name": account_record.get("Name") or account_name,
        "amount": record.get("Amount"),
        "type": record.get("Type"),
    }


# ---------------------------------------------------------------------------
# Markdown parsing helpers
# ---------------------------------------------------------------------------
def _find_section(text: str, heading_pattern: str) -> str | None:
    """Return the body under the first heading matching `heading_pattern`."""
    for line in text.splitlines():
        m = re.match(r"^#{2,4}\s+(.+)$", line, re.IGNORECASE)
        if m and re.search(heading_pattern, m.group(1), re.IGNORECASE):
            parts: list[str] = []
            capture = False
            for ln in text.splitlines():
                if not capture:
                    if ln == line:
                        capture = True
                    continue
                # Stop at a heading of same or higher level.
                nm = re.match(r"^(#{1,4})\s+", ln)
                if nm and len(nm.group(1)) <= len(re.match(r"^(#+)", line).group(1)):
                    break
                parts.append(ln)
            return "\n".join(parts).strip()
    return None


def _extract_bullets(text: str) -> list[str]:
    """Extract top-level markdown bullet lines, stripping marker and leading bold labels."""
    items: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(("- ", "* ")):
            continue
        content = re.sub(r"^[-*]\s+", "", line)
        content = re.sub(r"^\*\*[^*]+:\*\*\s*", "", content)
        content = content.strip()
        if content:
            items.append(content)
    return items


def _extract_table(text: str) -> list[dict[str, str]]:
    """Parse a simple markdown table into list-of-dicts."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return []
    headers = [h.strip().lower() for h in lines[0].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for ln in lines[2:]:
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < len(headers):
            continue
        rows.append({headers[i]: cells[i] for i in range(len(headers))})
    return rows


def _strip_markup(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"==(.+?)==", r"\1", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    return text.strip()


def _emails(text: str) -> list[str]:
    return re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)


def _canonical_section(key: str) -> str:
    """Convert a normalized heading key back to spaces for regex matching."""
    return re.sub(r"[-_]+", " ", key).strip()


# ---------------------------------------------------------------------------
# Connector / system extraction for technical scope
# ---------------------------------------------------------------------------
# Map common aliases (lowercase, spaces allowed) to a canonical display name.
_CONNECTOR_ALIASES: dict[str, str] = {
    "sql server": "SQL Server",
    "sqlserver": "SQL Server",
    "mssql": "SQL Server",
    "salesforce": "Salesforce",
    "postgres": "Postgres",
    "postgresql": "Postgres",
    "netsuite": "NetSuite",
    "snowflake": "Snowflake",
    "bigquery": "BigQuery",
    "redshift": "Redshift",
    "mysql": "MySQL",
    "mongodb": "MongoDB",
    "hubspot": "HubSpot",
    "zendesk": "Zendesk",
    "slack": "Slack",
    "jira": "Jira",
    "github": "GitHub",
    "s3": "S3",
    "gcs": "GCS",
    "google cloud storage": "GCS",
    "azure": "Azure Blob Storage",
    "azure blob storage": "Azure Blob Storage",
    "kafka": "Kafka",
    "iceberg": "Apache Iceberg",
    "apache iceberg": "Apache Iceberg",
    "databricks": "Databricks",
    "oracle": "Oracle",
    "sap": "SAP",
    "workday": "Workday",
    "marketo": "Marketo",
    "iterable": "Iterable",
    "mixpanel": "Mixpanel",
    "amplitude": "Amplitude",
    "stripe": "Stripe",
    "shopify": "Shopify",
    "google ads": "Google Ads",
    "facebook": "Facebook Marketing",
    "linkedin": "LinkedIn Ads",
}

_DESTINATION_CANONICAL: set[str] = {
    "Snowflake",
    "BigQuery",
    "Redshift",
    "S3",
    "GCS",
    "Azure Blob Storage",
    "Kafka",
    "Apache Iceberg",
    "Databricks",
}

# Lookbehind/lookahead ensures we do not match inside identifiers like
# `source-postgres` or `my_postgres_db`.
_CONNECTOR_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    + "|".join(re.escape(a) for a in sorted(_CONNECTOR_ALIASES, key=len, reverse=True))
    + r")(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_BACKTICK_SYSTEM_RE = re.compile(r"`(source|destination)-([a-z0-9_-]+)`", re.IGNORECASE)


def _classify_system(name: str, window: str, explicit_kind: str | None = None) -> str | None:
    """Classify a connector name as source or destination."""
    if explicit_kind in {"source", "destination"}:
        return explicit_kind
    name_lower = name.lower()
    if name in _DESTINATION_CANONICAL or any(k in name_lower for k in ("warehouse", "lake", "s3", "blob")):
        return "destination"
    src = len(re.findall(r"\bfrom\b", window, re.IGNORECASE)) + window.lower().count("source-")
    dst = len(re.findall(r"\binto\b|\bto\s+(?=[A-Z])", window, re.IGNORECASE)) + window.lower().count("destination-")
    if src and not dst:
        return "source"
    if dst and not src:
        return "destination"
    return "source"


def _find_connectors(text: str) -> list[tuple[str, str | None]]:
    """Return (canonical_name, explicit_kind) tuples found in text."""
    found: list[tuple[str, str | None]] = []
    positions: list[tuple[int, int]] = []

    for m in _BACKTICK_SYSTEM_RE.finditer(text):
        kind = m.group(1).lower()
        raw = m.group(2).lower()
        canonical = _CONNECTOR_ALIASES.get(raw) or _CONNECTOR_ALIASES.get(raw.replace("-", " ")) or raw.capitalize()
        found.append((canonical, kind))
        positions.append((m.start(), m.end()))

    for m in _CONNECTOR_RE.finditer(text):
        if any(start <= m.start() < end for start, end in positions):
            continue
        alias = m.group(0).lower()
        canonical = _CONNECTOR_ALIASES[alias]
        found.append((canonical, None))
        positions.append((m.start(), m.end()))

    deduped: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()
    for name, kind in found:
        key = (name.lower(), kind)
        if key not in seen:
            seen.add(key)
            deduped.append((name, kind))
    return deduped


# ---------------------------------------------------------------------------
# Prior output extraction
# ---------------------------------------------------------------------------
def _prior_output_paths(customers_dir: Path, account: str, opp: str | None) -> list[Path]:
    paths: list[Path] = []
    roots = [customers_dir / account / "outputs"]
    if opp:
        roots.append(customers_dir / account / "opportunities" / opp / "outputs")
    for root in roots:
        if not root.exists():
            continue
        for skill_dir in root.iterdir():
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            for md in sorted(skill_dir.glob("*.md")):
                paths.append(md)
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def _parse_md_file(path: Path) -> output_schema.OutputMetadata | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    skill = path.parent.name
    return output_schema.parse_output(skill, text)


def _output_source_label(path: Path, customers_dir: Path) -> str:
    try:
        return str(path.relative_to(customers_dir))
    except ValueError:
        return str(path)


def _extract_contacts(meta: output_schema.OutputMetadata, path: Path, customers_dir: Path) -> list[Contact]:
    contacts: list[Contact] = []
    source = _output_source_label(path, customers_dir)
    text = path.read_text(encoding="utf-8")

    # Try a "Who's Who" / stakeholder table.
    for section_name, section_text in meta.sections.items():
        if "who" in section_name or "stakehold" in section_name or "attendee" in section_name or "player" in section_name:
            for row in _extract_table(section_text):
                name = _strip_markup(row.get("person", row.get("name", row.get("attendee", ""))))
                if not name:
                    continue
                role = _strip_markup(row.get("role", row.get("title", ""))) or None
                side = _strip_markup(row.get("side", row.get("team", ""))) or None
                notes = _strip_markup(row.get("notes", "")) or None
                contacts.append(Contact(name=name, role=role, side=side, source=source, source_path=str(path), notes=notes))

    # Look for explicit prospect contact bullets.
    contact_section = _find_section(text, r"contact|attendee|participant")
    if contact_section:
        for bullet in _extract_bullets(contact_section):
            name = bullet.split(",")[0].split(" — ")[0].strip()
            if not name or any(name.lower() in c.name.lower() for c in contacts):
                continue
            emails = _emails(bullet)
            contacts.append(Contact(name=name, email=emails[0] if emails else None, side="Customer", source=source, source_path=str(path)))

    return contacts


def _extract_business_objectives(meta: output_schema.OutputMetadata, path: Path, customers_dir: Path) -> list[BusinessObjective]:
    objectives: list[BusinessObjective] = []
    source = _output_source_label(path, customers_dir)

    headings = [
        r"business\s*objective",
        r"objectives",
        r"driver",
        r"need",
        r"urgency",
        r"bottom\s*line",
    ]
    seen: set[str] = set()
    for section_name, section_text in meta.sections.items():
        if any(re.search(h, _canonical_section(section_name), re.IGNORECASE) for h in headings):
            bullets = _extract_bullets(section_text)
            if bullets:
                for bullet in bullets:
                    b = _strip_markup(bullet)
                    if b and b not in seen:
                        seen.add(b)
                        objectives.append(BusinessObjective(objective=b, sources=[source]))
            else:
                # Some sections (e.g. Driver, Need) are prose paragraphs.
                para = _strip_markup(section_text).split("\n\n")[0].strip()
                para = re.sub(r"\s+", " ", para)
                if para and len(para) < 500 and para not in seen:
                    seen.add(para)
                    objectives.append(BusinessObjective(objective=para, sources=[source]))
    # Also harvest any MEDDPICC / scorecard table with a "why it matters" column.
    for section_name, section_text in meta.sections.items():
        if "meddpicc" in section_name or "scorecard" in section_name:
            for row in _extract_table(section_text):
                why = _strip_markup(row.get("why it matters", row.get("why", "")))
                if why and why not in seen:
                    seen.add(why)
                    objectives.append(BusinessObjective(objective=why, sources=[source]))
    return objectives


def _extract_technical_scope(meta: output_schema.OutputMetadata, path: Path, customers_dir: Path) -> dict[str, list[Any]]:
    scope: dict[str, list[Any]] = {"sources": [], "destinations": [], "use_cases": [], "requirements": [], "dependencies": []}
    source = _output_source_label(path, customers_dir)

    relevant = {
        "connector", "technical", "in-scope", "in scope", "source systems",
        "destination systems", "sources and destinations", "connectors",
        "architecture", "security", "cdc", "schema", "performance", "capacity", "throughput",
        "need", "what would close", "what would lose",
    }
    excluded = {"source coverage", "sources used", "source-coverage", "sources-used"}

    for section_name, section_text in meta.sections.items():
        canon = _canonical_section(section_name)
        if any(ex in canon for ex in excluded):
            continue
        if not any(k in canon for k in relevant):
            continue

        # Connector/system extraction
        for name, explicit_kind in _find_connectors(section_text):
            kind = _classify_system(name, section_text, explicit_kind)
            target = scope["sources"] if kind == "source" else scope["destinations"] if kind == "destination" else scope["sources"]
            if not any(s.name == name for s in target):
                target.append(TechnicalSystem(name=name, kind=kind, evidence=f"found in {section_name}", sources=[source]))

        # Use cases and requirements
        for bullet in _extract_bullets(section_text):
            b = _strip_markup(bullet)
            if re.search(r"\buse\s*case\b|\bprove\b|\bvalidate\b", b, re.IGNORECASE):
                if b and not any(u.name == b for u in scope["use_cases"]):
                    scope["use_cases"].append(TechnicalSystem(name=b, kind="use_case", evidence=b, sources=[source]))
            if any(k in b.lower() for k in ("requirement", "must ", "needs to", " require", "hard requirement")):
                if b and b not in scope["requirements"]:
                    scope["requirements"].append(b)
            if any(k in b.lower() for k in ("dependency", "depends on", "blocked by")):
                if b and b not in scope["dependencies"]:
                    scope["dependencies"].append(b)

    return scope


def _extract_success_criteria(meta: output_schema.OutputMetadata, path: Path, customers_dir: Path) -> list[SuccessCriterion]:
    criteria: list[SuccessCriterion] = []
    source = _output_source_label(path, customers_dir)

    headings = [
        r"success\s*criteria",
        r"success",
        r"what\s*would\s*close",
        r"close\s*criteria",
        r"validation",
    ]
    seen: set[str] = set()
    for section_name, section_text in meta.sections.items():
        if any(re.search(h, _canonical_section(section_name), re.IGNORECASE) for h in headings):
            for bullet in _extract_bullets(section_text):
                b = _strip_markup(bullet)
                if not b or b in seen:
                    continue
                seen.add(b)
                validation_method = "Customer validation during POV"
                lower = b.lower()
                if any(k in lower for k in ("count", "record", "loss", "missing")):
                    validation_method = "Record-level comparison against source"
                elif any(k in lower for k in ("latency", "minute", "hour", "throughput")):
                    validation_method = "Latency / throughput measurement"
                elif "security" in lower or "infosec" in lower or "sign-off" in lower:
                    validation_method = "Documented customer sign-off"
                criteria.append(
                    SuccessCriterion(
                        feature_or_capability=b,
                        validation_method=validation_method,
                        in_scope="Yes",
                        priority="Must Have",
                        evidence=b,
                        sources=[source],
                    )
                )
    return criteria


def _extract_milestones(meta: output_schema.OutputMetadata, path: Path, customers_dir: Path) -> list[Milestone]:
    milestones: list[Milestone] = []
    source = _output_source_label(path, customers_dir)

    headings = [r"milestone", r"timeline", r"schedule"]
    seen: set[str] = set()
    for section_name, section_text in meta.sections.items():
        if any(re.search(h, _canonical_section(section_name), re.IGNORECASE) for h in headings):
            for bullet in _extract_bullets(section_text):
                b = _strip_markup(bullet)
                if not b or b in seen:
                    continue
                seen.add(b)
                m = re.search(r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})\b", b)
                milestones.append(
                    Milestone(name=b, target_date=m.group(1) if m else None, status="Not Started", sources=[source])
                )
    return milestones


def _extract_feature_requests(meta: output_schema.OutputMetadata, path: Path, customers_dir: Path) -> list[FeatureRequest]:
    requests: list[FeatureRequest] = []
    source = _output_source_label(path, customers_dir)

    headings = [r"feature\s*request", r"product\s*gap", r"product\s*ask", r"gap"]
    seen: set[str] = set()
    for section_name, section_text in meta.sections.items():
        if any(re.search(h, _canonical_section(section_name), re.IGNORECASE) for h in headings):
            for bullet in _extract_bullets(section_text):
                b = _strip_markup(bullet)
                if not b or b in seen:
                    continue
                seen.add(b)
                area = None
                for a in ("Connectors", "Platform", "Cloud", "Enterprise"):
                    if a.lower() in b.lower():
                        area = a
                        break
                requests.append(FeatureRequest(description=b, product_area=area, sources=[source]))
    return requests


# ---------------------------------------------------------------------------
# Transcripts
# ---------------------------------------------------------------------------
def _read_transcripts(transcripts_dir: Path, account: str) -> tuple[list[str], list[SourceCoverageEntry]]:
    coverage: list[SourceCoverageEntry] = []
    if not transcripts_dir.exists():
        return [], [SourceCoverageEntry(source="workspace transcripts", available=False, note=f"{transcripts_dir} does not exist")]

    # Files like Acme-07.01.26.txt
    prefix = re.sub(r"[^A-Za-z0-9]+", "-", account).strip("-")
    files = [f for f in transcripts_dir.iterdir() if f.is_file() and f.name.lower().startswith(prefix.lower())]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    if not files:
        return [], [SourceCoverageEntry(source="workspace transcripts", available=False, note=f"no {prefix}-* files in {transcripts_dir}")]

    contents: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        contents.append(text[:8000])  # cap per transcript to avoid context bloat
        coverage.append(
            SourceCoverageEntry(
                source="workspace transcript",
                available=True,
                path=str(f),
                freshness=datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).isoformat(),
                material=False,  # set later if actually used
            )
        )
    return contents, coverage


# ---------------------------------------------------------------------------
# External source evidence
# ---------------------------------------------------------------------------

def _parse_iso_timestamp(ts: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp, treating trailing Z as UTC."""
    if not ts:
        return None
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _is_more_recent(new_ts: str | None, existing_ts: str | None) -> bool:
    """Return True when `new_ts` is a valid timestamp later than `existing_ts`."""
    new_dt = _parse_iso_timestamp(new_ts)
    existing_dt = _parse_iso_timestamp(existing_ts)
    if not new_dt:
        return False
    if not existing_dt:
        return True
    return new_dt > existing_dt


def _load_external_evidence(path: Path | None) -> list[ExternalEvidence]:
    """Load a JSON file of ExternalEvidence entries.

    The file may be a top-level list of evidence objects or an object with an
    `entries` or `evidence` key. Invalid items are silently dropped.
    """
    if not path or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict):
        data = data.get("entries") or data.get("evidence") or []
    if not isinstance(data, list):
        return []
    items: list[ExternalEvidence] = []
    for raw in data:
        try:
            items.append(ExternalEvidence.model_validate(raw))
        except ValidationError:
            continue
    return items


def _extract_transcript_text(text: str, source: str) -> dict[str, list[Any]]:
    """Pull connector/system signals out of raw transcript text."""
    scope: dict[str, list[Any]] = {"sources": [], "destinations": [], "use_cases": [], "requirements": [], "dependencies": []}
    if not text:
        return scope
    for name, explicit_kind in _find_connectors(text):
        kind = _classify_system(name, text, explicit_kind)
        target = scope["sources"] if kind == "source" else scope["destinations"] if kind == "destination" else scope["sources"]
        if not any(s.name == name for s in target):
            target.append(TechnicalSystem(name=name, kind=kind, evidence="found in transcript", sources=[source]))
    return scope


def _coalesce_prospect(prospect: Prospect, evidence: ExternalEvidence) -> list[str]:
    """Merge a `prospect` fact into the existing prospect, preferring fresher/direct evidence."""
    warnings: list[str] = []
    if evidence.fact_type != "prospect":
        return warnings
    fact = evidence.fact or {}
    for field in (
        "opportunity_name",
        "stage",
        "owner",
        "se_name",
        "se_title",
        "next_step",
        "pov_start_date",
        "target_completion_date",
    ):
        new_val = fact.get(field)
        if not new_val:
            continue
        old_val = getattr(prospect, field)
        if not old_val:
            setattr(prospect, field, str(new_val))
            continue
        if str(old_val).lower() == str(new_val).lower():
            continue
        prefer_new = evidence.direct_customer or _is_more_recent(evidence.retrieved_at, None)
        if prefer_new:
            warnings.append(
                f"Prospect conflict on {field}: overriding '{old_val}' with '{new_val}' "
                f"from {evidence.source} (direct_customer={evidence.direct_customer}, "
                f"retrieved_at={evidence.retrieved_at})."
            )
            setattr(prospect, field, str(new_val))
        else:
            warnings.append(
                f"Prospect conflict on {field}: keeping '{old_val}' over '{new_val}' "
                f"from {evidence.source} (not a direct customer statement)."
            )
    return warnings


def _coalesce_contact(contacts: list[Contact], evidence: ExternalEvidence) -> Contact:
    """Merge a contact fact, matching by name or email and filling in missing fields."""
    fact = evidence.fact or {}
    name = str(fact.get("name") or "").strip()
    email = str(fact.get("email") or fact.get("emailAddress") or fact.get("mail") or "").strip() or None
    raw_side = fact.get("side") or "Customer"
    side = raw_side if raw_side in {"Airbyte", "Customer", "Partner"} else "Customer"
    role = str(fact.get("title") or fact.get("role") or "").strip() or None
    source = evidence.source
    source_id = evidence.source_id
    notes = fact.get("notes") or evidence.raw or evidence.note

    for c in contacts:
        name_match = bool(name and c.name and c.name.lower() == name.lower())
        email_match = bool(email and c.email and c.email.lower() == email.lower())
        if name_match or email_match:
            if not c.email and email:
                c.email = email
            if not c.role and role:
                c.role = role
            if not c.side or (c.side == "Customer" and side != "Customer"):
                c.side = side
            if source and source not in (c.source or ""):
                extra = f"{source}: {evidence.retrieved_at or 'unknown'}"
                c.notes = "; ".join(filter(None, [c.notes, extra]))
            return c

    new = Contact(
        name=name,
        email=email,
        role=role,
        side=side,
        source=source,
        source_path=source_id,
        notes=notes,
    )
    contacts.append(new)
    return new


def _business_objective_from_evidence(evidence: ExternalEvidence) -> BusinessObjective | None:
    if evidence.fact_type != "business_objective":
        return None
    fact = evidence.fact or {}
    obj = fact.get("objective") or fact.get("description")
    if not obj:
        return None
    return BusinessObjective(
        objective=str(obj).strip(),
        desired_outcome=str(fact.get("desired_outcome") or fact.get("outcome") or "").strip() or None,
        evidence=str(evidence.raw or fact.get("evidence") or obj).strip()[:500],
        sources=[evidence.source],
    )


def _success_criterion_from_evidence(evidence: ExternalEvidence) -> SuccessCriterion | None:
    if evidence.fact_type != "success_criterion":
        return None
    fact = evidence.fact or {}
    feature = fact.get("feature_or_capability") or fact.get("feature") or fact.get("description")
    if not feature:
        return None
    return SuccessCriterion(
        use_case=fact.get("use_case") or None,
        feature_or_capability=str(feature).strip(),
        validation_method=str(fact.get("validation_method") or "Customer validation during POV").strip(),
        acceptance_threshold=fact.get("acceptance_threshold") or fact.get("threshold") or None,
        in_scope=str(fact.get("in_scope") or "Yes").strip(),
        priority=str(fact.get("priority") or "Must Have").strip(),
        notes=str(evidence.raw or fact.get("notes") or "").strip()[:500] or None,
        evidence=str(evidence.raw or fact.get("evidence") or feature).strip()[:500],
        sources=[evidence.source],
    )


def _milestone_from_evidence(evidence: ExternalEvidence) -> Milestone | None:
    if evidence.fact_type != "milestone":
        return None
    fact = evidence.fact or {}
    name = fact.get("name") or fact.get("description")
    if not name:
        return None
    return Milestone(
        name=str(name).strip(),
        target_date=fact.get("target_date") or fact.get("date") or None,
        status=str(fact.get("status") or "Not Started").strip(),
        evidence=str(evidence.raw or fact.get("evidence") or name).strip()[:500],
        sources=[evidence.source],
    )


def _feature_request_from_evidence(evidence: ExternalEvidence) -> FeatureRequest | None:
    if evidence.fact_type != "feature_request":
        return None
    fact = evidence.fact or {}
    desc = fact.get("description") or fact.get("feature")
    if not desc:
        return None
    area = fact.get("product_area") or fact.get("area")
    if not area:
        for a in ("Connectors", "Platform", "Cloud", "Enterprise"):
            if a.lower() in str(desc).lower():
                area = a
                break
    return FeatureRequest(
        date=fact.get("date") or None,
        product_area=area,
        description=str(desc).strip(),
        priority=fact.get("priority") or None,
        evidence=str(evidence.raw or fact.get("evidence") or desc).strip()[:500],
        sources=[evidence.source],
    )


def _technical_system_from_evidence(evidence: ExternalEvidence) -> tuple[str, TechnicalSystem] | None:
    if evidence.fact_type != "technical_system":
        return None
    fact = evidence.fact or {}
    name = fact.get("name")
    if not name:
        return None
    kind = fact.get("kind") or "unknown"
    if kind not in {"source", "destination", "use_case", "unknown"}:
        kind = "unknown"
    ts = TechnicalSystem(
        name=str(name).strip(),
        kind=kind,
        integration_type=fact.get("integration_type") or fact.get("integration") or None,
        use_case=fact.get("use_case") or None,
        evidence=str(evidence.raw or fact.get("evidence") or name).strip()[:500],
        sources=[evidence.source],
    )
    if kind == "destination":
        return ("destinations", ts)
    if kind == "use_case":
        return ("use_cases", ts)
    return ("sources", ts)


def _dedupe_append(items: list[Any], new_items: list[Any], key_func: Any) -> None:
    """Append items from `new_items` whose key is not already in `items`."""
    seen = {key_func(i) for i in items}
    for item in new_items:
        key = key_func(item)
        if key not in seen:
            seen.add(key)
            items.append(item)


def _merge_external_evidence(ctx: PovContext, evidence_list: list[ExternalEvidence]) -> None:
    """Normalize raw external evidence into the typed PovContext."""
    for evidence in evidence_list:
        if evidence.status != "ok":
            continue
        fact_type = evidence.fact_type

        if fact_type == "prospect":
            ctx.warnings.extend(_coalesce_prospect(ctx.prospect, evidence))
            continue

        if fact_type == "contact":
            side = evidence.fact.get("side") if evidence.fact else None
            target = ctx.contacts["internal"] if side == "Airbyte" else ctx.contacts["prospect"]
            _coalesce_contact(target, evidence)
            continue

        if fact_type == "business_objective":
            obj = _business_objective_from_evidence(evidence)
            if obj:
                _dedupe_append(ctx.business_objectives, [obj], lambda o: o.objective.lower())
            continue

        if fact_type == "success_criterion":
            crit = _success_criterion_from_evidence(evidence)
            if crit:
                _dedupe_append(ctx.success_criteria, [crit], lambda c: c.feature_or_capability.lower())
            continue

        if fact_type == "milestone":
            ms = _milestone_from_evidence(evidence)
            if ms:
                _dedupe_append(ctx.milestones, [ms], lambda m: m.name.lower())
            continue

        if fact_type == "feature_request":
            fr = _feature_request_from_evidence(evidence)
            if fr:
                _dedupe_append(ctx.feature_requests, [fr], lambda f: f.description.lower())
            continue

        if fact_type == "technical_system":
            result = _technical_system_from_evidence(evidence)
            if result:
                key, ts = result
                _dedupe_append(ctx.technical_scope[key], [ts], lambda s: s.name.lower())
            continue

        if fact_type == "requirement":
            req = evidence.fact.get("description") if evidence.fact else None
            if req and req not in ctx.technical_scope["requirements"]:
                ctx.technical_scope["requirements"].append(str(req))
            continue

        if fact_type == "dependency":
            dep = evidence.fact.get("description") if evidence.fact else None
            if dep and dep not in ctx.technical_scope["dependencies"]:
                ctx.technical_scope["dependencies"].append(str(dep))
            continue

        if fact_type == "transcript":
            # Only extract from an explicit `text` field in the fact, not from `raw`.
            # The bridge already extracts technical systems from the transcript and emits
            # separate `technical_system` evidence with correct source/destination kind.
            text = evidence.fact.get("text") or ""
            if text:
                scope = _extract_transcript_text(str(text), evidence.source)
                for key in ("sources", "destinations", "use_cases"):
                    _dedupe_append(ctx.technical_scope[key], scope[key], lambda s: s.name.lower())
            continue

        if fact_type == "note" and evidence.raw:
            if evidence.raw not in ctx.architecture_notes:
                ctx.architecture_notes.append(str(evidence.raw))
            continue

        if fact_type == "unknown" and evidence.raw:
            if evidence.raw not in ctx.unknowns:
                ctx.unknowns.append(str(evidence.raw))


def _source_coverage_from_evidence(evidence_list: list[ExternalEvidence]) -> list[SourceCoverageEntry]:
    """Summarize external evidence by source for the receipt."""
    by_source: dict[str, list[ExternalEvidence]] = {}
    for e in evidence_list:
        by_source.setdefault(e.source, []).append(e)

    coverage: list[SourceCoverageEntry] = []
    for source, items in sorted(by_source.items()):
        ok_items = [e for e in items if e.status == "ok"]
        material = any(e.fact_type not in {"note", "unknown"} for e in ok_items)
        if ok_items:
            freshness = max((e.retrieved_at for e in ok_items if e.retrieved_at), default=None)
            coverage.append(
                SourceCoverageEntry(
                    source=source,
                    available=True,
                    freshness=freshness,
                    material=material,
                    note=f"{len(ok_items)} fact(s) merged",
                    status="searched",
                )
            )
        else:
            representative = items[0]
            coverage.append(
                SourceCoverageEntry(
                    source=source,
                    available=False,
                    freshness=representative.retrieved_at,
                    material=False,
                    note=representative.note or f"{representative.status} in this run",
                    status=representative.status,
                )
            )
    return coverage


def _dedupe_contacts(contacts: list[Contact]) -> list[Contact]:
    """De-duplicate contacts by name, preserving the richest record."""
    seen: dict[str, Contact] = {}
    for c in contacts:
        key = (c.name or "").lower()
        if not key:
            continue
        if key not in seen:
            seen[key] = c
            continue
        existing = seen[key]
        if not existing.email and c.email:
            existing.email = c.email
        if not existing.role and c.role:
            existing.role = c.role
        if c.source and c.source not in (existing.source or ""):
            extra = f"{c.source}: {c.source_path or 'unknown'}"
            existing.notes = "; ".join(filter(None, [existing.notes, extra]))
    return list(seen.values())


def _dedupe_by(items: list[Any], key_func: Any) -> list[Any]:
    """De-duplicate a list of objects using `key_func`."""
    seen: set[Any] = set()
    unique: list[Any] = []
    for item in items:
        key = key_func(item)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _dedupe_strings(items: list[str]) -> list[str]:
    """De-duplicate a list of strings case-insensitively while preserving order."""
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _merge_coverage(
    existing: list[SourceCoverageEntry], new: list[SourceCoverageEntry]
) -> list[SourceCoverageEntry]:
    """Merge external source coverage into existing coverage without duplicates."""
    by_source: dict[str, SourceCoverageEntry] = {e.source: e for e in existing}
    for entry in new:
        old = by_source.get(entry.source)
        if not old:
            by_source[entry.source] = entry
            continue
        old.available = old.available or entry.available
        old.material = old.material or entry.material
        if entry.freshness and (not old.freshness or _is_more_recent(entry.freshness, old.freshness)):
            old.freshness = entry.freshness
        if entry.note:
            old.note = entry.note
        if entry.status:
            old.status = entry.status
    return list(by_source.values())


def _classify_status(ctx: PovContext) -> None:
    """Recompute the context status after all evidence has been merged.

    Mirrors the PovContext `_classification` model validator so we can avoid
    re-validating the model (which would turn `TechnicalSystem` instances in the
    loosely-typed `technical_scope` back into plain dicts).
    """
    has_prospect = bool(ctx.prospect.account_name and ctx.prospect.account_name.strip())
    has_business_context = bool(ctx.business_objectives)
    has_connector_or_use_case = (
        ctx.technical_scope["sources"]
        or ctx.technical_scope["destinations"]
        or ctx.technical_scope["use_cases"]
    )
    has_success_criteria = bool(ctx.success_criteria)
    if has_prospect and (has_business_context or has_connector_or_use_case) and has_success_criteria:
        ctx.status = "complete"
    elif has_prospect and (has_business_context or has_connector_or_use_case or has_success_criteria):
        ctx.status = "partial"
    else:
        ctx.status = "blocked"


def _derive_success_criteria(
    technical_scope: dict[str, list[Any]],
    business_objectives: list[BusinessObjective],
    source: str,
) -> list[SuccessCriterion]:
    """Create placeholder success criteria from use cases, objectives, or systems."""
    criteria: list[SuccessCriterion] = []
    if technical_scope.get("use_cases"):
        for uc in technical_scope["use_cases"][:2]:
            criteria.append(
                SuccessCriterion(
                    feature_or_capability=uc.name,
                    validation_method="Customer validation during POV",
                    in_scope="Yes",
                    priority="Must Have",
                    evidence=uc.evidence or uc.name,
                    sources=[source],
                )
            )
    elif business_objectives:
        for o in business_objectives[:2]:
            criteria.append(
                SuccessCriterion(
                    feature_or_capability=o.objective,
                    validation_method="Confirm with customer during POV",
                    in_scope="Yes",
                    priority="Must Have",
                    evidence=o.objective,
                    sources=[source],
                )
            )
    elif technical_scope.get("sources") or technical_scope.get("destinations"):
        systems = [s.name for s in technical_scope["sources"] + technical_scope["destinations"]]
        if systems:
            criteria.append(
                SuccessCriterion(
                    feature_or_capability=f"Replicate data from {', '.join(systems[:3])} end-to-end",
                    validation_method="Customer validation during POV",
                    in_scope="Yes",
                    priority="Must Have",
                    evidence="derived from connector-feasibility / tech-qual outputs",
                    sources=[source],
                )
            )
    return criteria


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def build_context(
    account: str,
    opp: str | None = None,
    workspace_arg: str | None = None,
    config_path: str | None = None,
    external_evidence_paths: dict[str, Path | None] | None = None,
) -> PovContext:
    """Build the structured POV context deterministically.

    `external_evidence_paths` is an optional mapping from source name (salesforce,
    gong, granola, gmail, slack) to a JSON file of `ExternalEvidence` objects.
    This lets the skill feed in data from configured MCP integrations without the
    deterministic loader needing to know how to invoke those tools.
    """
    workspace, config = _resolve_se_config(workspace_arg)
    paths = _derive_paths(config, workspace)
    customers_dir = paths["customers_dir"]
    transcripts_dir = paths["transcripts_dir"]
    account_dir = customers_dir / account

    source_coverage: list[SourceCoverageEntry] = []

    # Prospect identity
    se_cfg = config.get("pov_gsheet", {}) or {}
    se_name = config.get("name") or se_cfg.get("se_name")
    se_title = config.get("role") or se_cfg.get("se_title")

    # Salesforce (optional)
    sf_data: dict[str, Any] = {}
    if config.get("salesforce", {}).get("enabled", True):
        sf_data = _salesforce_query(config, account, account_dir)
        if sf_data:
            source_coverage.append(
                SourceCoverageEntry(
                    source="salesforce",
                    available=True,
                    note=f"queried {len(sf_data)} fields",
                    material=True,
                )
            )
        else:
            source_coverage.append(
                SourceCoverageEntry(
                    source="salesforce",
                    available=False,
                    note="sf CLI not available, not authed, or no matching records",
                )
            )
    else:
        source_coverage.append(
            SourceCoverageEntry(source="salesforce", available=False, note="disabled in .se-config.yaml")
        )

    # Prior outputs
    output_paths = _prior_output_paths(customers_dir, account, opp)
    parsed_outputs = [(p, _parse_md_file(p)) for p in output_paths]
    parsed_outputs = [(p, m) for p, m in parsed_outputs if m is not None]
    if parsed_outputs:
        source_coverage.append(
            SourceCoverageEntry(
                source="prior skill outputs",
                available=True,
                note=f"{len(parsed_outputs)} parsed outputs",
                material=True,
            )
        )
    else:
        source_coverage.append(
            SourceCoverageEntry(
                source="prior skill outputs",
                available=False,
                note=f"no outputs found under {customers_dir / account}",
            )
        )

    # Transcripts
    transcript_texts, transcript_coverage = _read_transcripts(transcripts_dir, account)
    source_coverage.extend(transcript_coverage)
    if transcript_texts:
        # Mark the most recent transcript as material if it is the only customer voice.
        transcript_coverage[0].material = True

    # Build prospect
    prospect = Prospect(
        account_name=sf_data.get("account_name") or account,
        opportunity_name=sf_data.get("opportunity_name") or (opp if opp else None),
        stage=sf_data.get("stage"),
        owner=sf_data.get("owner") or (config.get("ae_pairings", [{}])[0].get("name") if config.get("ae_pairings") else None),
        se_name=se_name,
        se_title=se_title,
        next_step=sf_data.get("next_step"),
        pov_start_date=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        target_completion_date=sf_data.get("close_date"),
    )

    # Internal contacts
    internal_contacts: list[Contact] = []
    if se_name:
        internal_contacts.append(Contact(name=se_name, role=se_title, side="Airbyte", source=".se-config.yaml"))
    for ae in config.get("ae_pairings", []):
        internal_contacts.append(Contact(name=ae.get("name"), role=ae.get("role", "AE"), side="Airbyte", source=".se-config.yaml"))
    if sf_data.get("owner"):
        if not any(c.name == sf_data["owner"] for c in internal_contacts):
            internal_contacts.append(Contact(name=sf_data["owner"], role="Owner", side="Airbyte", source="salesforce"))

    # Prospect contacts and artifacts
    prospect_contacts: list[Contact] = []
    business_objectives: list[BusinessObjective] = []
    technical_scope: dict[str, list[Any]] = {"sources": [], "destinations": [], "use_cases": [], "requirements": [], "dependencies": []}
    success_criteria: list[SuccessCriterion] = []
    milestones: list[Milestone] = []
    feature_requests: list[FeatureRequest] = []
    architecture_notes: list[str] = []
    unknowns: list[str] = []

    for path, meta in parsed_outputs:
        prospect_contacts.extend(_extract_contacts(meta, path, customers_dir))
        business_objectives.extend(_extract_business_objectives(meta, path, customers_dir))
        scope = _extract_technical_scope(meta, path, customers_dir)
        for k, items in scope.items():
            existing: dict[str, Any] = {getattr(s, "name", s): s for s in technical_scope[k]}
            for item in items:
                name = getattr(item, "name", item)
                if name in existing and isinstance(item, TechnicalSystem) and isinstance(existing[name], TechnicalSystem):
                    prev = existing[name]
                    for src in item.sources:
                        if src not in prev.sources:
                            prev.sources.append(src)
                    if item.evidence and not prev.evidence:
                        prev.evidence = item.evidence
                    continue
                technical_scope[k].append(item)
                existing[name] = item
        success_criteria.extend(_extract_success_criteria(meta, path, customers_dir))
        milestones.extend(_extract_milestones(meta, path, customers_dir))
        feature_requests.extend(_extract_feature_requests(meta, path, customers_dir))

    # Preliminary context; external evidence will be merged before final dedup/derive.
    ctx = PovContext(
        prospect=prospect,
        contacts={"internal": internal_contacts, "prospect": prospect_contacts},
        business_objectives=business_objectives,
        technical_scope=technical_scope,
        success_criteria=success_criteria,
        milestones=milestones,
        feature_requests=feature_requests,
        architecture_notes=architecture_notes,
        unknowns=unknowns,
        source_coverage=source_coverage,
        warnings=[],
    )

    # Merge external evidence provided by the skill from configured MCP integrations.
    external_evidence: list[ExternalEvidence] = []
    for src_path in (external_evidence_paths or {}).values():
        if src_path:
            external_evidence.extend(_load_external_evidence(src_path))
    if external_evidence:
        _merge_external_evidence(ctx, external_evidence)
        external_coverage = _source_coverage_from_evidence(external_evidence)
        source_coverage = _merge_coverage(source_coverage, external_coverage)

    # Final deduplication across workspace and external sources.
    ctx.contacts["prospect"] = _dedupe_contacts(ctx.contacts["prospect"])
    ctx.contacts["internal"] = _dedupe_contacts(ctx.contacts["internal"])
    ctx.business_objectives = _dedupe_by(ctx.business_objectives, lambda o: o.objective.lower())
    ctx.success_criteria = _dedupe_by(ctx.success_criteria, lambda c: c.feature_or_capability.lower())
    ctx.milestones = _dedupe_by(ctx.milestones, lambda m: m.name.lower())
    ctx.feature_requests = _dedupe_by(ctx.feature_requests, lambda f: f.description.lower())
    ctx.technical_scope["sources"] = _dedupe_by(ctx.technical_scope["sources"], lambda s: s.name.lower())
    ctx.technical_scope["destinations"] = _dedupe_by(ctx.technical_scope["destinations"], lambda s: s.name.lower())
    ctx.technical_scope["use_cases"] = _dedupe_by(ctx.technical_scope["use_cases"], lambda s: s.name.lower())
    ctx.technical_scope["requirements"] = _dedupe_strings(ctx.technical_scope["requirements"])
    ctx.technical_scope["dependencies"] = _dedupe_strings(ctx.technical_scope["dependencies"])
    ctx.architecture_notes = _dedupe_strings(ctx.architecture_notes)
    ctx.unknowns = _dedupe_strings(ctx.unknowns)

    # Derive placeholder success criteria if none exist.
    if not ctx.success_criteria:
        ctx.success_criteria = _derive_success_criteria(ctx.technical_scope, ctx.business_objectives, "prior skill outputs")

    # Warnings for missing optional integrations and source coverage.
    warnings: list[str] = ctx.warnings[:]
    if not transcript_texts and not any(
        s.source in {"gong", "granola"} and s.available for s in source_coverage
    ):
        warnings.append("No local transcripts found and no Gong/Granola integration returned calls; customer voice is limited to prior outputs.")
    if not any(s.source == "salesforce" and s.available for s in source_coverage):
        warnings.append("Salesforce not available; opportunity metadata may be incomplete.")
    if not parsed_outputs and not any(
        s.source in {"gong", "granola", "salesforce"} and s.available for s in source_coverage
    ):
        warnings.append("No prior skill outputs found; run biz-qual / tech-qual / poc-plan for richer context.")
    for entry in source_coverage:
        if entry.available or entry.status in {"skipped"}:
            continue
        if entry.source in {"gong", "granola", "gmail", "slack"}:
            warnings.append(f"{entry.source} not available in this run: {entry.note}")

    ctx.source_coverage = source_coverage
    ctx.warnings = warnings

    # Re-classify so the status reflects all merged evidence.
    _classify_status(ctx)
    return ctx


def _main() -> None:
    parser = argparse.ArgumentParser(description="Build a POV context for pov-gsheet.")
    parser.add_argument("--account", required=True, help="Account folder name")
    parser.add_argument("--opportunity", default=None, help="Opportunity slug")
    parser.add_argument("--workspace", default=None, help="Workspace root override")
    parser.add_argument("--out", default=None, help="Output JSON file (default: stdout)")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    parser.add_argument("--salesforce-evidence", default=None, help="JSON file of ExternalEvidence from Salesforce")
    parser.add_argument("--gong-evidence", default=None, help="JSON file of ExternalEvidence from Gong")
    parser.add_argument("--granola-evidence", default=None, help="JSON file of ExternalEvidence from Granola")
    parser.add_argument("--gmail-evidence", default=None, help="JSON file of ExternalEvidence from Gmail")
    parser.add_argument("--slack-evidence", default=None, help="JSON file of ExternalEvidence from Slack")
    args = parser.parse_args()

    external_evidence_paths = {
        "salesforce": Path(args.salesforce_evidence) if args.salesforce_evidence else None,
        "gong": Path(args.gong_evidence) if args.gong_evidence else None,
        "granola": Path(args.granola_evidence) if args.granola_evidence else None,
        "gmail": Path(args.gmail_evidence) if args.gmail_evidence else None,
        "slack": Path(args.slack_evidence) if args.slack_evidence else None,
    }
    ctx = build_context(
        account=args.account,
        opp=args.opportunity,
        workspace_arg=args.workspace,
        external_evidence_paths=external_evidence_paths,
    )
    payload = ctx.model_dump(mode="json")
    text = json.dumps(payload, indent=2 if args.pretty else None)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    _main()
