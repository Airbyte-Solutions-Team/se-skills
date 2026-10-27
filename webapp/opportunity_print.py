"""Print-ready HTML of the Opportunity workspace Overview, for the PDF export.

Mirrors what the user sees in the workspace (`static/app.js` `renderOverview*`,
`renderBusinessCase`, `renderMeddpicc`, `renderStakeholders`): same sections in the
same order, same class names, and a light-theme port of the relevant `style.css`
rules. Disclosures are always expanded and interactive affordances (evidence
buttons, toolbar) become plain text.

Every model-authored string goes through `html.escape`; nothing here emits raw
state values as markup. Only validated canonical state is read -- no transcript
text, evidence hashes, or filesystem paths.
"""
from __future__ import annotations

import html
from datetime import date, datetime
from typing import Any

from opportunity_state import (
    Claim,
    KnowledgeState,
    OpportunityStateVersion,
    RiskSeverity,
    StakeholderBlockerStatus,
)

_BRIEF_FALLBACK = {
    KnowledgeState.UNKNOWN: "Unknown",
    KnowledgeState.NOT_APPLICABLE: "Not applicable",
    KnowledgeState.CONFLICTING: "Conflicting evidence — review needed",
    KnowledgeState.PARTIAL: "Insufficient evidence",
}
_FRAMEWORK_FALLBACK = {
    KnowledgeState.UNKNOWN: "Not established",
    KnowledgeState.NOT_APPLICABLE: "Not applicable",
    KnowledgeState.CONFLICTING: "Conflicting evidence — review needed",
    KnowledgeState.PARTIAL: "Partial information only",
}
_BRIEF_FIELDS = (
    ("customer_objective", "Customer objective"),
    ("why_airbyte", "Why Airbyte"),
    ("current_status", "Current status"),
    ("path_to_decision", "Path to decision"),
    ("immediate_priority", "Immediate priority"),
)
_BUSINESS_CASE_AREAS = (
    ("current_state", "Current state"),
    ("future_state", "Future state"),
    ("negative_consequences", "Negative consequences"),
    ("positive_business_outcomes", "Positive business outcomes"),
)
_HEALTH_TONE = {
    "strong": "success", "comfortable": "success", "positive": "success",
    "weak": "error", "blocked": "error", "at_risk": "error", "negative": "error",
    "tight": "warn", "mixed": "warn",
}

# Light-theme port of the workspace stylesheet (static/style.css) for print.
_CSS = """
@page { size: Letter; margin: 12mm 11mm 14mm 11mm; @bottom-right { content: counter(page) " / " counter(pages); font: 9px sans-serif; color: #8b93a3; } }
:root {
  --bg: #ffffff; --panel: #ffffff; --panel2: #f0f2f6; --line: #d9dde6; --line-strong: #c2c8d4;
  --text: #1a1d26; --muted: #5b6472; --accent: #2563eb; --accent2: #2f9e44; --warn: #b8860b; --danger: #e03131;
}
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body { margin: 0; background: var(--bg); color: var(--text); font: 12px/1.5 Inter, "Segoe UI", ui-sans-serif, system-ui, -apple-system, Arial, sans-serif; }
h1 { font-size: 24px; margin: 0 0 4px; line-height: 1.2; }
h2 { font-size: 16px; margin: 0; }
h3 { font-size: 13px; margin: 0; }
p { margin: 0 0 8px; }
.sub { color: var(--muted); margin: 0 0 12px; font-size: 13px; }
.muted { color: var(--muted); }
.opp-workspace { display: flex; flex-direction: column; gap: 16px; }

.workspace-eyebrow { display: block; color: var(--muted); font-size: 9.5px; font-weight: 700; letter-spacing: .08em; line-height: 1.3; text-transform: uppercase; margin-bottom: 4px; }
.workspace-hero { background: linear-gradient(135deg, #eaf0fd 0%, #ffffff 58%); border: 1px solid var(--line); border-radius: 12px; padding: 20px; break-inside: avoid; }
.workspace-meta { display: flex; flex-wrap: wrap; gap: 6px; }
.workspace-meta-pill, .workspace-state, .workspace-count {
  display: inline-flex; align-items: center; width: fit-content; border: 1px solid var(--line-strong);
  border-radius: 999px; color: var(--muted); background: var(--panel2);
  font-size: 10px; font-weight: 650; line-height: 1.2; padding: 3px 8px;
}
.workspace-facts { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 18px; border-top: 1px solid var(--line); }
.workspace-fact { display: flex; flex-direction: column; gap: 2px; min-width: 0; padding: 12px 14px 0 0; }
.workspace-fact + .workspace-fact { border-left: 1px solid var(--line); padding-left: 14px; }
.workspace-fact-value { font-size: 13px; font-weight: 700; overflow-wrap: anywhere; }
.workspace-fact-label { color: var(--muted); font-size: 9.5px; letter-spacing: .04em; text-transform: uppercase; }

.workspace-panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; }
.workspace-panel-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 14px; margin-bottom: 4px; }
.workspace-panel-head small { display: block; color: var(--muted); font-size: 11px; margin-top: 2px; }
.workspace-panel-head .workspace-state { max-width: 55%; text-align: right; flex: 0 1 auto; }
.workspace-brief { border-left: 3px solid var(--accent); }
.workspace-status { display: inline-flex; align-items: center; border: 1px solid var(--line); border-radius: 999px; padding: 2px 7px; color: var(--muted); font-size: 9.5px; line-height: 1.25; white-space: nowrap; }
.workspace-status--success { color: var(--accent2); border-color: #a9d8b3; }
.workspace-status--warn { color: var(--warn); border-color: #e0cb8e; }
.workspace-status--error { color: var(--danger); border-color: #f0a9a9; }

.overview-brief-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1px; margin-top: 12px; background: var(--line); border: 1px solid var(--line); }
.overview-claim { min-width: 0; background: var(--panel); padding: 13px; break-inside: avoid; }
.overview-claim:last-child { grid-column: 1 / -1; }
.overview-claim p { margin: 4px 0 10px; font-size: 12.5px; line-height: 1.5; }
.overview-claim-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 9px; color: var(--muted); font-size: 10px; }
.overview-evidence-count { color: var(--muted); font-size: 10px; }

.overview-health-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
.overview-health-card { min-width: 0; border: 1px solid var(--line); border-top: 3px solid var(--accent); border-radius: 10px; padding: 12px; break-inside: avoid; }
.overview-health-card > strong { display: block; font-size: 15px; margin-bottom: 5px; }
.overview-health-card p { color: var(--muted); font-size: 11px; line-height: 1.45; }
.overview-health-card--success { border-top-color: var(--accent2); }
.overview-health-card--warn { border-top-color: var(--warn); }
.overview-health-card--error { border-top-color: var(--danger); }

.overview-two-column { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; align-items: start; break-inside: avoid; }
.workspace-panel-head { break-after: avoid; }
.overview-list-item { border-top: 1px solid var(--line); padding: 11px 0 2px; break-inside: avoid; }
.overview-list-item p { color: var(--muted); font-size: 11.5px; line-height: 1.5; margin: 6px 0; }
.overview-list-item small { display: block; color: var(--muted); margin-bottom: 6px; font-size: 10.5px; }
.overview-risk--critical, .overview-risk--high { border-left: 3px solid var(--danger); padding-left: 10px; }

.overview-business-case-quad { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.overview-framework-area { min-width: 0; border: 1px solid var(--line); border-radius: 9px; background: var(--panel2); padding: 13px; break-inside: avoid; }
.overview-framework-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.overview-framework-points { margin: 8px 0 0; padding-left: 16px; }
.overview-framework-points li { margin: 3px 0; font-size: 11.5px; line-height: 1.4; }
.overview-framework-content { min-width: 0; }
.overview-framework-content section { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 10px; }
.overview-framework-content h4, .overview-stakeholder section h4, .overview-stakeholder-signals dt { margin: 0 0 4px; color: var(--muted); font-size: 9px; font-weight: 700; letter-spacing: .06em; text-transform: uppercase; }
.overview-framework-content p { margin: 0; font-size: 12px; line-height: 1.5; }
.overview-framework-content ul, .overview-stakeholder section ul, .overview-stakeholder-gaps ul { margin: 0; padding-left: 16px; }
.overview-framework-content li, .overview-stakeholder section li, .overview-stakeholder-gaps li { margin: 3px 0; font-size: 12px; line-height: 1.4; }
.overview-meddpicc-dimensions { display: flex; flex-direction: column; gap: 7px; margin-top: 12px; }
.overview-meddpicc-dimension { min-width: 0; border: 1px solid var(--line); border-left: 3px solid var(--line-strong); border-radius: 9px; background: var(--panel2); padding: 11px 13px; break-inside: avoid; }
.overview-meddpicc-dimension--known { border-left-color: var(--accent2); }
.overview-meddpicc-dimension--partial { border-left-color: var(--warn); }
.overview-meddpicc-dimension--conflicting { border-left-color: var(--danger); }
.overview-meddpicc-dimension-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.overview-meddpicc-gap { display: flex; align-items: baseline; gap: 7px; margin: 7px 0 0; font-size: 11.5px; color: var(--muted); }
.overview-meddpicc-gap-label { flex: 0 0 auto; padding: 1px 6px; border-radius: 999px; border: 1px solid #e0cb8e; color: var(--warn); font-size: 9px; font-weight: 750; letter-spacing: .04em; text-transform: uppercase; }
.overview-meddpicc-dimension > .overview-framework-content { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 2px; }

.overview-stakeholder-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 12px; align-items: start; }
.overview-stakeholder { min-width: 0; border: 1px solid var(--line); border-radius: 9px; background: var(--panel2); padding: 13px; overflow-wrap: anywhere; break-inside: avoid; }
.overview-stakeholder-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.overview-stakeholder-head h3 { font-size: 13.5px; }
.overview-stakeholder-head p { margin: 2px 0 0; color: var(--muted); font-size: 11px; }
.overview-stakeholder-signals { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1px; margin: 11px 0 0; background: var(--line); border: 1px solid var(--line); }
.overview-stakeholder-signals > div { background: var(--panel); padding: 7px 9px; }
.overview-stakeholder-signals dd { margin: 2px 0 0; font-size: 11px; }
.overview-stakeholder section { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 10px; }
.overview-stakeholder section p { margin: 0; font-size: 12px; line-height: 1.45; }
.overview-stakeholder-gaps { margin-top: 14px; break-inside: avoid; }
.overview-stakeholder-gaps h3 { margin: 0 0 6px; font-size: 12px; }

.overview-missing-list { margin: 10px 0 0; padding-left: 18px; }
.overview-missing-list li { margin-bottom: 6px; font-size: 12px; }
.overview-missing-list small { display: block; color: var(--muted); }
.overview-evidence-list { display: flex; flex-direction: column; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; margin-top: 10px; }
.overview-evidence-row { padding: 8px 11px; background: var(--panel2); break-inside: avoid; }
.overview-evidence-row + .overview-evidence-row { border-top: 1px solid var(--line); }
.overview-evidence-row small { color: var(--muted); margin-left: 6px; font-size: 10.5px; }
.print-footnote { color: var(--muted); font-size: 10px; margin: 8px 0 0; }
"""


def label(value: Any) -> str:
    """'at_risk' / HealthStatus.AT_RISK -> 'At Risk' (mirrors the web UI's overviewLabel)."""
    raw = getattr(value, "value", value)
    return str(raw or "unknown").replace("_", " ").title()


def _e(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _day(value: date | datetime) -> str:
    return f"{value:%b} {value.day}, {value.year}"


def _chip(text: str, tone: str = "neutral") -> str:
    return f'<span class="workspace-status workspace-status--{tone}">{_e(text)}</span>'


def _evidence(refs: list[Any]) -> str:
    if not refs:
        return '<span class="overview-evidence-count">No supporting evidence</span>'
    return f'<span class="overview-evidence-count">{_e(_plural(len(refs), "evidence reference"))}</span>'


def _bullets(items: list[Any]) -> str:
    return f"<ul>{''.join(f'<li>{_e(item)}</li>' for item in items)}</ul>"


def _panel(eyebrow: str, title: str, *, state: str = "", sub: str = "", body: str = "", cls: str = "") -> str:
    state_html = f'<span class="workspace-state">{_e(state)}</span>' if state else ""
    sub_html = f"<small>{_e(sub)}</small>" if sub else ""
    return (
        f'<section class="workspace-panel {cls}"><div class="workspace-panel-head"><div>'
        f'<div class="workspace-eyebrow">{_e(eyebrow)}</div><h2>{_e(title)}</h2>{sub_html}</div>'
        f"{state_html}</div>{body}</section>"
    )


def _knowledge_summary(claims: list[Claim]) -> str:
    counts = {"established": 0, "partial": 0, "conflicting": 0, "unknown": 0, "not applicable": 0}
    names = {
        KnowledgeState.KNOWN: "established", KnowledgeState.PARTIAL: "partial",
        KnowledgeState.CONFLICTING: "conflicting", KnowledgeState.NOT_APPLICABLE: "not applicable",
    }
    for claim in claims:
        counts[names.get(claim.knowledge_state, "unknown")] += 1
    always = {"established", "partial", "unknown"}
    return " · ".join(f"{n} {k}" for k, n in counts.items() if n or k in always)


def _hero(version: OpportunityStateVersion, account: dict[str, Any], opportunity: dict[str, Any]) -> str:
    name = opportunity.get("name") or version.identity.opportunity_name
    account_name = account.get("name") or version.identity.account
    amount = opportunity.get("amount")
    stage = " · ".join(str(p) for p in (opportunity.get("stage_num"), opportunity.get("stage")) if p)
    pills = [
        stage,
        opportunity.get("type"),
        f"${amount:,.0f}" if isinstance(amount, (int, float)) and not isinstance(amount, bool) else "",
        f"Target close {opportunity['close_date']}" if opportunity.get("close_date") else "",
        f"Owner {account['owner_name']}" if account.get("owner_name") else "",
        f"AE {opportunity['ae']}" if opportunity.get("ae") else "",
    ]
    pills_html = "".join(f'<span class="workspace-meta-pill">{_e(p)}</span>' for p in pills if p)
    note = ""
    if opportunity.get("metadata_complete") is False:
        note = ('<p class="muted" style="margin:8px 0 0;font-size:11px">Salesforce metadata unavailable; '
                "name derived from the local opportunity folder.</p>")

    def fact(value: str, caption: str) -> str:
        return (f'<div class="workspace-fact"><span class="workspace-fact-value">{_e(value or "Unknown")}</span>'
                f'<span class="workspace-fact-label">{_e(caption)}</span></div>')

    facts = "".join([
        fact(stage, "Current stage"),
        fact(str(opportunity.get("close_date") or "Not set"), "Target close"),
        fact(str(account.get("owner_name") or "Unassigned"), "Account owner"),
        fact(f"Revision {version.revision}", f"Overview · {_day(version.created_at)}"),
    ])
    return (
        '<section class="workspace-hero"><div class="workspace-eyebrow">Opportunity workspace</div>'
        f'<h1>{_e(name)}</h1><p class="sub">{_e(account_name)} · Opportunity Overview</p>'
        f'<div class="workspace-meta">{pills_html}</div>{note}'
        f'<div class="workspace-facts">{facts}</div></section>'
    )


def _brief(version: OpportunityStateVersion) -> str:
    cards = []
    for key, title in _BRIEF_FIELDS:
        claim = getattr(version.state.brief, key)
        value = claim.value or _BRIEF_FALLBACK.get(claim.knowledge_state, "Not established")
        cards.append(
            f'<article class="overview-claim"><div class="workspace-eyebrow">{_e(title)}</div><p>{_e(value)}</p>'
            f'<div class="overview-claim-meta">{_chip(label(claim.knowledge_state))}'
            f"<span>{_e(label(claim.confidence))} confidence</span>{_evidence(claim.evidence_refs)}</div></article>"
        )
    return _panel(
        "Current understanding", "Opportunity Brief", state=f"Revision {version.revision}",
        body=f'<div class="overview-brief-grid">{"".join(cards)}</div>', cls="workspace-brief",
    )


def _health(version: OpportunityStateVersion) -> str:
    cards = "".join(
        f'<article class="overview-health-card overview-health-card--{_HEALTH_TONE.get(item.status.value, "neutral")}">'
        f'<div class="workspace-eyebrow">{_e(label(item.key))}</div><strong>{_e(label(item.status))}</strong>'
        f"<p>{_e(item.reason)}</p>{_evidence(item.evidence_refs)}</article>"
        for item in version.state.health_indicators
    )
    return _panel("Explainable status", "Health indicators", body=f'<div class="overview-health-grid">{cards}</div>')


def _risks_and_actions(version: OpportunityStateVersion) -> str:
    risks, actions = version.state.risks, version.state.recommended_actions
    severe = {RiskSeverity.CRITICAL, RiskSeverity.HIGH}
    risk_html = "".join(
        f'<article class="overview-list-item overview-risk--{_e(r.severity.value)}"><div>'
        f'{_chip(label(r.severity), "error" if r.severity in severe else "warn")} <strong>{_e(r.title)}</strong></div>'
        f"<p>{_e(r.description)}</p>"
        + (f"<p><strong>Mitigation:</strong> {_e(r.mitigation)}</p>" if r.mitigation else "")
        + f"<small>{_e(label(r.classification))} · {'Owner ' + _e(r.owner) if r.owner else 'Owner TBD'}</small>"
        f"{_evidence(r.evidence_refs)}</article>"
        for r in risks
    ) or '<p class="muted">No risks are recorded in this Overview.</p>'
    action_html = "".join(
        f'<article class="overview-list-item"><div>{_chip(label(a.status))} <strong>{_e(a.action)}</strong></div>'
        f"<p>{_e(a.goal)}</p><p><strong>Done when:</strong> {_e(a.definition_of_done)}</p>"
        f"<small>{'Owner ' + _e(a.owner) if a.owner else 'Owner TBD'} · "
        f"{'Due ' + _e(a.due_date) if a.due_date else 'Due date TBD'}</small>{_evidence(a.evidence_refs)}</article>"
        for a in actions
    ) or '<p class="muted">No recommended actions were established.</p>'
    left = _panel("Operational blockers", "Top Risks", state=_plural(len(risks), "risk"), body=risk_html)
    right = _panel("Next best work", "Recommended Actions", state=str(len(actions)), body=action_html)
    return f'<div class="overview-two-column">{left}{right}</div>'


def _framework_content(area: Any, *, discovery: bool = False) -> str:
    claim: Claim = area.knowledge
    value = claim.value or _FRAMEWORK_FALLBACK.get(claim.knowledge_state, "Not established")
    missing = area.missing_information
    confirmed = f"Last confirmed {_day(claim.last_confirmed_at)}" if claim.last_confirmed_at else "Not confirmed"
    parts = [
        f"<section><h4>Known information</h4><p>{_e(value)}</p></section>",
        "<section><h4>Missing information</h4>"
        + (_bullets(missing) if missing else '<p class="muted">No additional gap was recorded.</p>')
        + "</section>",
        '<section><h4>Evidence</h4><div class="overview-claim-meta">'
        f"<span>{_e(label(claim.confidence))} confidence</span><span>{_e(label(claim.confirmation))}</span>"
        f"<span>{_e(confirmed)}</span>{_evidence(claim.evidence_refs)}</div></section>",
    ]
    if discovery:
        found = area.suggested_discovery
        parts.append(
            "<section><h4>Suggested discovery</h4>"
            + (_bullets(found) if found else '<p class="muted">No discovery prompt was recorded.</p>')
            + "</section>"
        )
    return f'<div class="overview-framework-content">{"".join(parts)}</div>'


def _business_case(version: OpportunityStateVersion) -> str:
    areas = [getattr(version.state.business_case, key) for key, _ in _BUSINESS_CASE_AREAS]
    cards = "".join(
        f'<article class="overview-framework-area"><div class="overview-framework-head"><h3>{_e(title)}</h3>'
        f"{_chip(label(area.knowledge.knowledge_state))}</div>{_framework_content(area)}"
        + (f'<ul class="overview-framework-points">{"".join(f"<li>{_e(p)}</li>" for p in area.points[:6])}</ul>'
           if area.points else "")
        + "</article>"
        for (_, title), area in zip(_BUSINESS_CASE_AREAS, areas)
    )
    return _panel(
        "Commercial case", "Business Case", sub="Current state, future state, consequences, and outcomes.",
        state=_knowledge_summary([a.knowledge for a in areas]),
        body=f'<div class="overview-business-case-quad">{cards}</div>',
    )


def _meddpicc(version: OpportunityStateVersion) -> str:
    dims = version.state.meddpicc.dimensions
    rows = []
    for dim in dims:
        state = dim.knowledge.knowledge_state
        gap = (
            f'<p class="overview-meddpicc-gap"><span class="overview-meddpicc-gap-label">GAP</span> '
            f"{_e(dim.missing_information[0])}</p>" if dim.missing_information else ""
        )
        rows.append(
            f'<div class="overview-meddpicc-dimension overview-meddpicc-dimension--{state.value}">'
            f'<div class="overview-meddpicc-dimension-head"><strong>{_e(label(dim.key))}</strong>'
            f"{_chip(label(state))}</div>{gap}{_framework_content(dim, discovery=True)}</div>"
        )
    return _panel(
        "Qualification", "MEDDPICC", sub="Qualification dimensions, evidence, gaps, and discovery.",
        state=_knowledge_summary([d.knowledge for d in dims]),
        body=f'<div class="overview-meddpicc-dimensions">{"".join(rows)}</div>',
    )


def _stakeholders(version: OpportunityStateVersion) -> str:
    people = version.state.stakeholders
    blockers = [
        s for s in people.stakeholders
        if s.blocker_status in (StakeholderBlockerStatus.ACTIVE_BLOCKER, StakeholderBlockerStatus.POTENTIAL_BLOCKER)
    ]
    missing_label = (
        f"Missing {', '.join(label(r) for r in people.missing_key_roles)}"
        if people.missing_key_roles else "Key roles established"
    )
    summary = f"{len(people.stakeholders)} established · {missing_label} · {_plural(len(blockers), 'blocker')}"
    cards = "".join(
        '<article class="overview-stakeholder"><div class="overview-stakeholder-head">'
        f"<div><h3>{_e(s.name)}</h3><p>{_e(s.title_or_role or 'Title or customer role not established')}</p></div>"
        f"{_chip(label(s.category))}</div>"
        '<dl class="overview-stakeholder-signals">'
        f"<div><dt>Influence</dt><dd>{_e(label(s.influence))}</dd></div>"
        f"<div><dt>Engagement</dt><dd>{_e(label(s.engagement))}</dd></div>"
        f"<div><dt>Stance</dt><dd>{_e(label(s.stance))}</dd></div>"
        f"<div><dt>Blocker</dt><dd>{_e(label(s.blocker_status))}</dd></div></dl>"
        f"<section><h4>Blocker information</h4><p>{_e(s.blocker_reason or 'No blocker reason established.')}</p></section>"
        "<section><h4>Recommended next engagement step</h4>"
        f"<p>{_e(s.recommended_next_step or 'Not established.')}</p></section>"
        "<section><h4>Missing information</h4>"
        + (_bullets(s.missing_information) if s.missing_information
           else '<p class="muted">No additional stakeholder gap was recorded.</p>')
        + f"</section><section><h4>Evidence</h4>{_evidence(s.evidence_refs)}</section></article>"
        for s in people.stakeholders
    )
    grid = (f'<div class="overview-stakeholder-grid">{cards}</div>' if cards
            else '<p class="muted">No people are established from authorized evidence.</p>')
    gaps = (
        '<section class="overview-stakeholder-gaps"><h3>Important roles still missing</h3>'
        + (_bullets([label(r) for r in people.missing_key_roles]) if people.missing_key_roles
           else '<p class="muted">Champion, economic buyer, and technical decision maker are established.</p>')
        + '</section><section class="overview-stakeholder-gaps"><h3>Map-level missing information</h3>'
        + (_bullets(people.missing_information) if people.missing_information
           else '<p class="muted">No additional stakeholder-map gap was recorded.</p>')
        + "</section>"
    )
    return _panel(
        "People", "Stakeholders",
        sub="Roles, influence, engagement, stance, blockers, and next steps." if people.stakeholders
        else "Not established from authorized evidence.",
        state=summary, body=grid + gaps,
    )


def _missing_and_evidence(version: OpportunityStateVersion) -> str:
    missing = version.state.missing_information
    missing_html = (
        '<ul class="overview-missing-list">'
        + "".join(f"<li><strong>{_e(m.description)}</strong><small>{_e(m.category)}</small></li>" for m in missing)
        + "</ul>"
    ) if missing else '<p class="muted">No additional missing-information items were recorded.</p>'
    rows = "".join(
        f'<div class="overview-evidence-row"><strong>{_e(e.display_name)}</strong>'
        f"<small>{_e(label(e.source_type))}</small></div>"
        for e in version.evidence_manifest
    )
    body = (
        f"<h3 style='margin-top:12px'>Missing or unknown</h3>{missing_html}"
        f"<h3 style='margin-top:14px'>Evidence used</h3><div class='overview-evidence-list'>{rows}</div>"
        "<p class='print-footnote'>Only the selected saved transcripts and read-only opportunity metadata "
        "supported this version. Generated outputs were not ingested.</p>"
    )
    return _panel(
        "Attributable state", "Evidence and missing information",
        state=f"{_plural(len(version.evidence_manifest), 'authorized source')} · revision {version.revision}",
        body=body,
    )


def render_print_html(
    version: OpportunityStateVersion, *, account: dict[str, Any], opportunity: dict[str, Any]
) -> str:
    """A self-contained, print-styled HTML document of the workspace Overview."""
    name = opportunity.get("name") or version.identity.opportunity_name
    sections = (
        _hero(version, account, opportunity), _brief(version), _health(version),
        _risks_and_actions(version), _business_case(version), _meddpicc(version),
        _stakeholders(version), _missing_and_evidence(version),
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{_e(name)} — Opportunity Overview</title><style>{_CSS}</style></head>"
        f'<body><div class="opp-workspace">{"".join(sections)}</div></body></html>'
    )
