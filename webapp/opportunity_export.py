"""Export the current canonical Opportunity Overview as a shareable HTML or PDF file.

Pipeline: validated `OpportunityStateVersion` -> Markdown -> the shared exporters
(`internal_html.render_internal_html` / `pdf_render.render_pdf`). Going through
Markdown keeps one sanitizing render path for every export; model-authored
strings are escaped here so they can never introduce structure (headings, tables,
admonitions, raw HTML) into the document.

Only the validated canonical state is exported -- conclusions, gaps, and evidence
*names*. Transcript bodies, hashes, and filesystem paths are never part of the
state contract and are not emitted.
"""
from __future__ import annotations

import html
import re
from typing import Any, Literal

import internal_html
import pdf_render
from opportunity_state import Claim, KnowledgeState, OpportunityStateVersion

ExportFormat = Literal["html", "pdf"]

_ESCAPE_SPECIALS = re.compile(r"([\\`*_\[\]#|])")

_CLAIM_FALLBACK = {
    KnowledgeState.UNKNOWN: "Not established",
    KnowledgeState.NOT_APPLICABLE: "Not applicable",
    KnowledgeState.CONFLICTING: "Conflicting evidence — review needed",
    KnowledgeState.PARTIAL: "Partial information only",
}

_BRIEF_ORDER = (
    ("customer_objective", "Customer objective"),
    ("why_airbyte", "Why Airbyte"),
    ("current_status", "Current status"),
    ("path_to_decision", "Path to decision"),
    ("immediate_priority", "Immediate priority"),
)
_BUSINESS_CASE_ORDER = (
    ("current_state", "Current state"),
    ("future_state", "Future state"),
    ("negative_consequences", "Negative consequences"),
    ("positive_business_outcomes", "Positive business outcomes"),
)


def label(value: Any) -> str:
    """'at_risk' / HealthStatus.AT_RISK -> 'At Risk' (mirrors the web UI's overviewLabel)."""
    raw = getattr(value, "value", value)
    return str(raw or "unknown").replace("_", " ").title()


def _t(value: Any) -> str:
    """Inline-safe text: one line, HTML-escaped, Markdown specials neutralized."""
    text = html.escape(" ".join(str(value or "").split()), quote=False)
    text = text.replace("==", "&#61;=")  # md_render turns ==x== into <mark>
    text = _ESCAPE_SPECIALS.sub(r"\\\1", text)
    return re.sub(r"^(\d+)\.", r"\1\\.", re.sub(r"^([-+])", r"\\\1", text))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _claim_text(claim: Claim | None) -> str:
    if claim is None:
        return "Not established"
    return _t(claim.value or _CLAIM_FALLBACK.get(claim.knowledge_state, "Not established"))


def _claim_meta(claim: Claim | None) -> str:
    if claim is None:
        return ""
    parts = [
        label(claim.knowledge_state),
        f"{label(claim.confidence)} confidence",
        _plural(len(claim.evidence_refs), "evidence reference"),
    ]
    return f"*{' · '.join(parts)}*"


def _bullets(items: list[Any]) -> list[str]:
    return [f"- {_t(item)}" for item in items] + [""]


def _section(lines: list[str], title: str) -> None:
    lines += [f"## {title}", ""]


def _brief(state, lines: list[str]) -> None:
    _section(lines, "Opportunity Brief")
    for key, title in _BRIEF_ORDER:
        claim = getattr(state.brief, key)
        lines += [f"### {title}", "", _claim_text(claim), "", _claim_meta(claim), ""]


def _health(state, lines: list[str]) -> None:
    _section(lines, "Health Indicators")
    for item in state.health_indicators:
        lines += [
            f"### {label(item.key)} — {label(item.status)}", "", _t(item.reason), "",
            f"*{label(item.confidence)} confidence · {_plural(len(item.evidence_refs), 'evidence reference')}*", "",
        ]


def _risks(state, lines: list[str]) -> None:
    _section(lines, "Top Risks")
    if not state.risks:
        lines += ["No risks are recorded in this Overview.", ""]
    for risk in state.risks:
        owner = f"Owner {_t(risk.owner)}" if risk.owner else "Owner TBD"
        lines += [
            f"### {_t(risk.title)}", "",
            f"**{label(risk.severity)}** · {label(risk.classification)} · {owner}", "",
            _t(risk.description), "",
        ]
        if risk.mitigation:
            lines += [f"**Mitigation:** {_t(risk.mitigation)}", ""]


def _actions(state, lines: list[str]) -> None:
    _section(lines, "Recommended Actions")
    if not state.recommended_actions:
        lines += ["No recommended actions were established.", ""]
    for action in state.recommended_actions:
        owner = f"Owner {_t(action.owner)}" if action.owner else "Owner TBD"
        due = f"Due {action.due_date}" if action.due_date else "Due date TBD"
        lines += [
            f"### {_t(action.action)}", "",
            f"**{label(action.status)}** · {owner} · {due}", "",
            _t(action.goal), "",
            f"**Done when:** {_t(action.definition_of_done)}", "",
        ]


def _framework_area(title: str, area, lines: list[str], *, discovery: bool = False) -> None:
    claim = area.knowledge
    lines += [f"### {title} — {label(claim.knowledge_state)}", "", _claim_text(claim), ""]
    points = getattr(area, "points", [])
    if points:
        lines += _bullets(points)
    if area.missing_information:
        lines += ["**Missing information**", ""] + _bullets(area.missing_information)
    if discovery and area.suggested_discovery:
        lines += ["**Suggested discovery**", ""] + _bullets(area.suggested_discovery)
    lines += [_claim_meta(claim), ""]


def _business_case(state, lines: list[str]) -> None:
    _section(lines, "Business Case")
    for key, title in _BUSINESS_CASE_ORDER:
        _framework_area(title, getattr(state.business_case, key), lines)


def _meddpicc(state, lines: list[str]) -> None:
    _section(lines, "MEDDPICC")
    for dimension in state.meddpicc.dimensions:
        _framework_area(label(dimension.key), dimension, lines, discovery=True)


def _stakeholders(state, lines: list[str]) -> None:
    people = state.stakeholders
    _section(lines, "Stakeholders")
    if not people.stakeholders:
        lines += ["No people are established from authorized evidence.", ""]
    for person in people.stakeholders:
        role = f" — {_t(person.title_or_role)}" if person.title_or_role else ""
        lines += [
            f"### {_t(person.name)}{role}", "",
            f"- **Category:** {label(person.category)}",
            f"- **Influence:** {label(person.influence)}",
            f"- **Engagement:** {label(person.engagement)}",
            f"- **Stance:** {label(person.stance)}",
            f"- **Blocker:** {label(person.blocker_status)}",
            "",
        ]
        if person.blocker_reason:
            lines += [f"**Blocker information:** {_t(person.blocker_reason)}", ""]
        if person.recommended_next_step:
            lines += [f"**Recommended next step:** {_t(person.recommended_next_step)}", ""]
        if person.missing_information:
            lines += ["**Missing information**", ""] + _bullets(person.missing_information)
        lines += [f"*{_plural(len(person.evidence_refs), 'evidence reference')}*", ""]
    if people.missing_key_roles:
        lines += ["### Important roles still missing", ""]
        lines += _bullets([label(role) for role in people.missing_key_roles])
    if people.missing_information:
        lines += ["### Stakeholder map gaps", ""] + _bullets(people.missing_information)


def _missing_and_evidence(version: OpportunityStateVersion, lines: list[str]) -> None:
    state = version.state
    _section(lines, "Missing Information")
    if state.missing_information:
        lines += [f"- **{_t(item.description)}** ({_t(item.category)})" for item in state.missing_information]
        lines.append("")
    else:
        lines += ["No additional missing-information items were recorded.", ""]
    _section(lines, "Evidence Used")
    lines += [
        f"- {_t(entry.display_name)} ({label(entry.source_type)})" for entry in version.evidence_manifest
    ]
    lines += [
        "",
        "*Only the selected saved transcripts and read-only opportunity metadata supported this version.*",
        "",
    ]


def _header_facts(opportunity: dict[str, Any], account: dict[str, Any]) -> str:
    stage = " · ".join(str(p) for p in (opportunity.get("stage_num"), opportunity.get("stage")) if p)
    amount = opportunity.get("amount")
    facts = [
        stage,
        opportunity.get("type") or "",
        f"${amount:,.0f}" if isinstance(amount, (int, float)) and not isinstance(amount, bool) else "",
        f"Target close {opportunity['close_date']}" if opportunity.get("close_date") else "",
        f"Owner {account['owner_name']}" if account.get("owner_name") else "",
        f"AE {opportunity['ae']}" if opportunity.get("ae") else "",
    ]
    return " · ".join(_t(fact) for fact in facts if fact)


def build_markdown(
    version: OpportunityStateVersion, *, account: dict[str, Any], opportunity: dict[str, Any]
) -> str:
    """The full export document for one canonical revision."""
    state = version.state
    name = opportunity.get("name") or version.identity.opportunity_name
    lines = [f"# {_t(name)}", "", f"**As of:** {version.created_at.date().isoformat()}", ""]
    facts = _header_facts(opportunity, account)
    context = [_t(account.get("name") or version.identity.account)]
    if facts:
        context.append(facts)
    context.append(f"Overview revision {version.revision}")
    lines += [" · ".join(context), ""]
    for build in (_brief, _health, _risks, _actions, _business_case, _meddpicc, _stakeholders):
        build(state, lines)
    _missing_and_evidence(version, lines)
    return "\n".join(lines)


def export_filename(opp_slug: str, revision: int, fmt: ExportFormat) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", opp_slug).strip("-") or "opportunity"
    return f"{safe}-opportunity-brief-r{revision}.{fmt}"


def render_export(
    version: OpportunityStateVersion,
    fmt: ExportFormat,
    *,
    account: dict[str, Any],
    opportunity: dict[str, Any],
) -> str | bytes:
    """HTML (str) or PDF (bytes). PDF raises RuntimeError when Chrome is unavailable."""
    markdown_text = build_markdown(version, account=account, opportunity=opportunity)
    if fmt == "html":
        return internal_html.render_internal_html(markdown_text, customer=str(account.get("name") or ""))
    return pdf_render.render_pdf(markdown_text, page_per_section=False)
