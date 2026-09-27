"""Command Center aggregate read model (PR D of `docs/COMMAND_CENTER.md`).

Everything here is derived from persisted local records only: the current
Opportunity Overview version, PR C action records / change entries / run
receipts, and PR B ledger source summaries. No provider, CRM, or model is
contacted while a page is rendered. Opportunities are discovered from local
opportunity folders and records that name an account/opportunity, never from
Salesforce. This is not a verified CRM active-opportunity set.

Responses are body-free: they carry identifiers, statuses, counts, dates and
short human-authored/model-authored *fields* that already live in state JSON
(commitments, next steps), never meeting text. Every list is paginated and
bounded. Freshness is reported per source with the honest vocabulary of §7:
a manual import is labelled as such and never presented as a healthy connector.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from command_center_operations import HUMAN_TRANSITIONS, ActionRecord, ChangeEntry
from opportunity_state import (
    ConfirmationMode,
    HealthIndicatorKey,
    HealthStatus,
    KnowledgeState,
    OpportunityStateVersion,
    StakeholderBlockerStatus,
)

from services.command_center_operations_service import (
    CommandCenterOperationsError,
    CommandCenterOperationsService,
)
from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService
from services.opportunity_state_service import (
    OpportunityStateError,
    OpportunityStateService,
)

MAX_LIMIT = 200
DEFAULT_LIMIT = 50
DUE_SOON_DAYS = 7
RECENT_CHANGE_LIMIT = 10

AttentionKind = Literal[
    "overdue_action", "confirmed_blocker", "risk_review", "due_action", "proposal_review",
    "association_review", "source_pending", "source_failure", "reconciliation_failure",
]
# Rule-based order (§8): the reason is inspectable, there is no score.
_ATTENTION_ORDER: dict[str, int] = {
    "overdue_action": 0, "confirmed_blocker": 1, "risk_review": 2,
    "due_action": 3, "proposal_review": 4, "association_review": 5,
    "source_pending": 6, "source_failure": 7, "reconciliation_failure": 8,
}
_UNPROCESSED = frozenset({
    "discovered", "awaiting_association", "awaiting_content", "queued", "processing", "failed",
})
_WITHHELD = frozenset({"pending_unknown", "access_lost", "deleted", "failed"})
_OPEN = frozenset({"open", "blocked"})
_COVERAGE = {
    "scope": "locally_known",
    "complete": False,
    "label": "Local opportunities only; active CRM coverage is unverified",
    "detail": "Includes local opportunity folders, Overviews, Actions, and associated sources. "
              "An active CRM opportunity with no local record may be missing; local records are not proof it is still active.",
}


class CommandCenterReadError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str = "read_error") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


def _page(items: list[Any], *, limit: int, offset: int) -> dict[str, Any]:
    if not 1 <= limit <= MAX_LIMIT or offset < 0:
        raise CommandCenterReadError(400, "Invalid paging.", code="invalid_paging")
    page = items[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(items) else None
    return {"total": len(items), "offset": offset, "limit": limit, "next_offset": next_offset, "items": page}


def opportunity_link(account: str, slug: str) -> str:
    return f"#/opp/{account}/{slug}/{slug}"


def risk_link(account: str, slug: str, key: str) -> str:
    return f"{opportunity_link(account, slug)}/risk/{key}"


def risks_link(account: str, slug: str) -> str:
    return f"{opportunity_link(account, slug)}/risks"


class CommandCenterReadService:
    """Bounded aggregate reads over persisted local Command Center state."""

    def __init__(
        self,
        *,
        customers_dir: Path,
        ledger: EvidenceLedgerService,
        operations: CommandCenterOperationsService,
        state_service: OpportunityStateService,
        tech_eval_summary: Callable[[str, str], dict[str, Any] | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._customers = Path(customers_dir)
        self._ledger = ledger
        self._ops = operations
        self._state = state_service
        self._tech_eval_summary = tech_eval_summary
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------ helpers

    def _today(self) -> date:
        return self._clock().astimezone(timezone.utc).date()

    def _sources(self) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self._ledger.list_sources(limit=500, offset=offset)
            collected.extend(page["sources"])
            if page["next_offset"] is None:
                return collected
            offset = page["next_offset"]

    def _actions(self) -> list[ActionRecord]:
        return [action for action in self._ops._all_actions()
                if not self._ledger.is_forgotten(action.origin.source_id)
                and not self._ops._action_retired(action)]

    def _current(self, account: str, slug: str) -> tuple[OpportunityStateVersion | None, str]:
        """Return the effective head and its availability status."""
        try:
            version = self._state.read_current(account, slug)
        except OpportunityStateError as exc:
            if exc.code == "overview_retired":
                return None, "retired"
            if exc.code == "source_forgotten":
                return None, "withheld"
            return None, "malformed" if exc.code == "malformed_storage" else "unknown_account"
        return version, "current" if version is not None else "not_created"

    def local_opportunities(self) -> list[dict[str, Any]]:
        """Every locally known (account, opportunity), including empty opportunity folders."""
        found: dict[tuple[str, str], dict[str, Any]] = {}

        def add(account: str, slug: str, name: str | None = None) -> None:
            key = (account, slug)
            entry = found.setdefault(key, {"account": account, "opportunity_slug": slug, "opportunity_name": None})
            if name and not entry["opportunity_name"]:
                entry["opportunity_name"] = name

        if self._customers.is_dir():
            for account_dir in sorted(self._customers.iterdir()):
                if not account_dir.is_dir() or account_dir.name.startswith((".", "_")) or account_dir.is_symlink():
                    continue
                opps = account_dir / "opportunities"
                if not opps.is_dir():
                    continue
                for opp_dir in sorted(opps.iterdir()):
                    if not opp_dir.is_dir() or opp_dir.name.startswith(".") or opp_dir.is_symlink():
                        continue
                    version, _ = self._current(account_dir.name, opp_dir.name)
                    add(account_dir.name, opp_dir.name, version.identity.opportunity_name if version else None)
        for action in self._actions():
            add(action.account, action.opportunity_slug)
        for source in self._sources():
            assoc = source["association"]
            if assoc["state"] == "associated":
                add(assoc["account"], assoc["opportunity_slug"])
        return sorted(found.values(), key=lambda item: (item["account"].casefold(), item["opportunity_slug"]))

    # ----------------------------------------------------------- freshness

    def _source_freshness(
        self, account: str, slug: str, sources: list[dict[str, Any]], version: OpportunityStateVersion | None
    ) -> dict[str, Any]:
        """Per-source freshness for one opportunity (§7). Never claims continuous sync."""
        mine = [
            s for s in sources
            if s["association"]["state"] == "associated"
            and s["association"]["account"] == account and s["association"]["opportunity_slug"] == slug
        ]
        manifest_ids = {entry.source_id for entry in version.evidence_manifest} if version else set()
        processed = [s for s in mine if s["processing"]["status"] == "processed"]
        pending = [s for s in mine if s["processing"]["status"] in _UNPROCESSED - {"failed"}]
        failed = [s for s in mine if s["processing"]["status"] == "failed"]
        withheld = [s for s in mine if s["availability"] in _WITHHELD]
        stale = [
            s for s in processed
            if s["processing"]["processed_revision"] is not None
            and s["processing"]["processed_revision"] < s["latest_revision"]
        ]
        latest_processed_meeting = max(
            (s["latest"]["occurred_at"] for s in processed
             if s["kind"] == "meeting" and s["latest"]["occurred_at"]), default=None
        )
        last_import = max((s["latest"]["observed_at"] for s in mine), default=None)
        overview_missing = [
            s for s in processed
            if version is not None and not any(sid.startswith(s["source_id"]) for sid in manifest_ids)
        ]

        if not mine:
            state, label = "no_sources", "No sources imported for this opportunity"
        elif withheld:
            state, label = "unavailable", f"{len(withheld)} source(s) inaccessible · freshness unknown"
        elif failed:
            state, label = "failed", f"{len(failed)} source(s) failed processing · freshness unknown"
        elif pending or stale:
            state = "source_pending"
            label = f"{len(pending) + len(stale)} imported source(s) not yet reflected in the Overview"
        elif overview_missing:
            state, label = "overview_behind", f"Overview predates {len(overview_missing)} processed source(s)"
        else:
            state = "processed_latest_import"
            label = "Current through the latest user-initiated import"
        source_to_review = next(iter(withheld or failed or pending or stale or overview_missing or mine), None)
        return {
            "state": state,
            "label": label,
            "connector": {
                "provider": ", ".join(sorted({s["provider"] for s in mine})) or "none",
                "mode": "manual_import",
                "unattended_discovery": False,
                "health": "not_monitored",
                "note": "Sources arrive through user-initiated intake; there is no unattended discovery or sync.",
            },
            "last_manual_import_at": last_import,
            "source_link": f"#/command-center/sources/{source_to_review['source_id']}" if source_to_review else None,
            "latest_processed_meeting_at": latest_processed_meeting,
            "source_counts": {
                "total": len(mine), "processed": len(processed), "pending": len(pending),
                "failed": len(failed), "withheld": len(withheld), "stale": len(stale),
                "not_in_overview": len(overview_missing),
            },
            "overview_revision": version.revision if version else None,
            "overview_created_at": version.created_at.isoformat() if version else None,
        }

    # ---------------------------------------------------------- overview

    @staticmethod
    def _next_step(version: OpportunityStateVersion | None) -> dict[str, Any]:
        if version is None:
            return {"state": "unknown", "value": None, "confirmation": None}
        claim = version.state.brief.immediate_priority
        if claim.knowledge_state in (KnowledgeState.KNOWN, KnowledgeState.PARTIAL) and claim.value:
            return {"state": claim.knowledge_state.value, "value": claim.value, "confirmation": claim.confirmation.value}
        return {"state": claim.knowledge_state.value, "value": None, "confirmation": claim.confirmation.value}

    @staticmethod
    def _confirmed_blockers(version: OpportunityStateVersion | None) -> list[dict[str, Any]]:
        """Only persisted, evidence-backed blockers; inferred risks are not blockers."""
        if version is None:
            return []
        out: list[dict[str, Any]] = []
        for person in version.state.stakeholders.stakeholders:
            if person.blocker_status == StakeholderBlockerStatus.ACTIVE_BLOCKER and person.evidence_refs:
                out.append({
                    "kind": "stakeholder", "key": person.key, "title": person.name,
                    "reason": person.blocker_reason or "Recorded as an active blocker in the Overview.",
                    "evidence_refs": [ref.model_dump(mode="json") for ref in person.evidence_refs],
                })
        for indicator in version.state.health_indicators:
            if (
                indicator.key == HealthIndicatorKey.TECHNICAL_FIT
                and indicator.status == HealthStatus.BLOCKED
                and indicator.confirmation == ConfirmationMode.EVIDENCE_BACKED
            ):
                out.append({
                    "kind": "health_indicator", "key": indicator.key.value, "title": "Technical fit blocked",
                    "reason": indicator.reason,
                    "evidence_refs": [ref.model_dump(mode="json") for ref in indicator.evidence_refs],
                })
        return out

    @staticmethod
    def _risks(
        version: OpportunityStateVersion | None, freshness: dict[str, Any], account: str, slug: str,
    ) -> list[dict[str, Any]]:
        """Read only the effective validated revision. An inaccessible source withholds its risk conclusions."""
        if version is None or freshness["state"] == "unavailable":
            return []
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        risks = []
        for risk in version.state.risks:
            refs = [ref.model_dump(mode="json") for ref in risk.evidence_refs]
            transcript_count = sum(ref["source_type"] == "transcript" for ref in refs)
            if not refs:
                evidence_label = "No cited evidence; verify before acting"
            elif transcript_count:
                evidence_label = f"{transcript_count} transcript citation(s)"
                locators = [ref["locator"] for ref in refs if ref["source_type"] == "transcript" and ref["locator"]]
                if locators:
                    evidence_label += " · " + ", ".join(locators[:2])
            else:
                evidence_label = "Opportunity metadata only; verify with the customer"
            risks.append({
                "key": risk.key, "title": risk.title, "reason": risk.description,
                "severity": risk.severity.value, "classification": risk.classification.value,
                "status": "potential", "evidence_refs": refs, "evidence_label": evidence_label,
                "last_updated_at": risk.last_updated_at.isoformat() if risk.last_updated_at else None,
                "overview_revision": version.revision, "overview_created_at": version.created_at.isoformat(),
                "link": risk_link(account, slug, risk.key),
            })
        return sorted(risks, key=lambda risk: (order[risk["severity"]], risk["title"], risk["key"]))

    def _evaluation(self, account: str, slug: str) -> dict[str, Any]:
        if self._tech_eval_summary is None:
            return {"supported": False, "overall": None, "current_phase": None}
        summary = self._tech_eval_summary(account, slug)
        if not summary:
            return {"supported": True, "overall": None, "current_phase": None}
        return {
            "supported": True,
            "overall": summary.get("overall"),
            "current_phase": summary.get("current_phase"),
        }

    # ------------------------------------------------------------ actions

    def _present_action(self, action: ActionRecord) -> dict[str, Any]:
        payload = self._ops._present(action)
        today = self._today()
        due = date.fromisoformat(action.due_date) if action.due_date else None
        payload["overdue"] = bool(due and due < today and action.status in _OPEN and not payload["retracted"])
        payload["due_soon"] = bool(
            due and today <= due <= today + timedelta(days=DUE_SOON_DAYS) and action.status in _OPEN
            and not payload["retracted"]
        )
        payload["opportunity_link"] = opportunity_link(action.account, action.opportunity_slug)
        payload["provenance"] = {
            "origin_source_id": action.origin.source_id,
            "origin_revision": action.origin.revision,
            "origin_locator": action.origin.locator,
            "evidence_count": len(payload["effective_evidence"]),
            "retracted_evidence_count": len(payload["evidence_retractions"]),
            "source_link": f"#/command-center/sources/{action.origin.source_id}",
        }
        payload["allowed_transitions"] = (
            sorted(to for (frm, to) in HUMAN_TRANSITIONS if frm == action.status)
            if not payload["retracted"] else []
        )
        return payload

    def action(self, action_id: str) -> dict[str, Any]:
        """One action by stable ID with the same derived fields as the list rows."""
        action = self._ops._load_action(action_id)
        if self._ledger.is_forgotten(action.origin.source_id):
            raise CommandCenterReadError(410, "Action was derived from a forgotten message.", code="source_forgotten")
        return self._present_action(action)

    def actions(
        self,
        *,
        account: str | None = None,
        opportunity_slug: str | None = None,
        status: str | None = None,
        party: str | None = None,
        overdue: bool | None = None,
        include_retracted: bool = False,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        if opportunity_slug is not None and account is None:
            raise CommandCenterReadError(400, "An opportunity filter needs its account.", code="invalid_filter")
        items = self._actions()
        if not include_retracted:
            items = [a for a in items if a.retraction is None]
        if account is not None:
            items = [a for a in items if a.account == account]
        if opportunity_slug is not None:
            items = [a for a in items if a.opportunity_slug == opportunity_slug]
        if status is not None:
            items = [a for a in items if a.status == status]
        if party is not None:
            items = [a for a in items if a.party == party]
        presented = [self._present_action(a) for a in items]
        if overdue is not None:
            presented = [p for p in presented if p["overdue"] is overdue]
        presented.sort(key=lambda p: (
            not p["overdue"], p["due_date"] or "9999-99-99", p["account"].casefold(), p["opportunity_slug"], p["action_id"],
        ))
        counts: dict[str, int] = {}
        for p in presented:
            counts[p["status"]] = counts.get(p["status"], 0) + 1
        page = _page(presented, limit=limit, offset=offset)
        page["actions"] = page.pop("items")
        page["counts_by_status"] = counts
        page["filters"] = {
            "account": account, "opportunity_slug": opportunity_slug, "status": status, "party": party,
            "overdue": overdue, "include_retracted": include_retracted,
        }
        return page

    # ------------------------------------------------------------ changes

    def _all_changes(self) -> list[ChangeEntry]:
        root = self._ops._root() / "changes"
        entries: list[ChangeEntry] = []
        if not root.exists():
            return entries
        for directory in sorted(root.iterdir()):
            if not directory.is_dir() or directory.is_symlink() or "__" not in directory.name:
                continue
            account, slug = directory.name.split("__", 1)
            try:
                self._ops.repair(account, slug)
            except CommandCenterOperationsError:
                continue
            for path in directory.glob("*.json"):
                try:
                    entry = ChangeEntry.model_validate(self._ops._read_record(path))
                    if not self._ops._change_forgotten(entry) and not self._ops._change_retired(entry):
                        entries.append(entry)
                except (CommandCenterOperationsError, ValueError):
                    continue
        return entries

    def changes(
        self,
        *,
        account: str | None = None,
        opportunity_slug: str | None = None,
        change_type: str | None = None,
        actor: str | None = None,
        limit: int = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        if opportunity_slug is not None and account is None:
            raise CommandCenterReadError(400, "An opportunity filter needs its account.", code="invalid_filter")
        items = self._all_changes()
        if account is not None:
            items = [c for c in items if c.account == account]
        if opportunity_slug is not None:
            items = [c for c in items if c.opportunity_slug == opportunity_slug]
        if change_type is not None:
            items = [c for c in items if c.change_type == change_type]
        if actor is not None:
            items = [c for c in items if c.actor == actor]
        items.sort(key=lambda c: (c.applied_at, c.account, c.opportunity_slug, c.sequence), reverse=True)
        presented = []
        for c in items:
            payload = c.model_dump(mode="json")
            payload["opportunity_link"] = opportunity_link(c.account, c.opportunity_slug)
            payload["source_link"] = f"#/command-center/sources/{c.source.source_id}" if c.source else None
            presented.append(payload)
        page = _page(presented, limit=limit, offset=offset)
        page["changes"] = page.pop("items")
        page["filters"] = {"account": account, "opportunity_slug": opportunity_slug, "change_type": change_type, "actor": actor}
        return page

    # ---------------------------------------------------------- portfolio

    def _portfolio_row(
        self,
        entry: dict[str, Any],
        sources: list[dict[str, Any]],
        actions: list[ActionRecord],
        changes: list[ChangeEntry],
    ) -> dict[str, Any]:
        account, slug = entry["account"], entry["opportunity_slug"]
        version, overview_status = self._current(account, slug)
        mine = [a for a in actions if a.account == account and a.opportunity_slug == slug and a.retraction is None]
        counts: dict[str, int] = {}
        for a in mine:
            counts[a.status] = counts.get(a.status, 0) + 1
        today = self._today()
        overdue = [
            a for a in mine
            if a.status in _OPEN and a.due_date and date.fromisoformat(a.due_date) < today
        ]
        outstanding = sorted(
            (a for a in mine if a.status in _OPEN),
            key=lambda a: (a.due_date or "9999-99-99", a.created_at, a.action_id),
        )
        next_action = None
        if outstanding:
            presented = self._present_action(outstanding[0])
            next_action = {key: presented[key] for key in (
                "action_id", "commitment", "party", "due_date", "overdue", "due_soon",
            )}
        waiting_on = sorted({a.party for a in mine if a.status in _OPEN and a.party not in ("Airbyte", "Unknown")})
        freshness = self._source_freshness(account, slug, sources, version)
        blockers = self._confirmed_blockers(version) if freshness["state"] != "unavailable" else []
        risks = self._risks(version, freshness, account, slug)
        attention: list[str] = []
        if overdue:
            attention.append(f"{len(overdue)} overdue action(s)")
        if blockers:
            attention.append(f"{len(blockers)} confirmed blocker(s)")
        material_risks = sum(risk["severity"] in ("high", "critical") for risk in risks)
        if material_risks:
            attention.append(f"{material_risks} potential high/critical risk(s) to review")
        if counts.get("proposed"):
            attention.append(f"{counts['proposed']} proposal(s) to review")
        if freshness["state"] in ("unavailable", "failed", "source_pending", "overview_behind"):
            attention.append(freshness["label"])
        own_changes = [c for c in changes if c.account == account and c.opportunity_slug == slug]
        latest_change = max(own_changes, key=lambda c: (c.applied_at, c.sequence), default=None)
        return {
            "account": account,
            "opportunity_slug": slug,
            "opportunity_name": (version.identity.opportunity_name if version else entry.get("opportunity_name")) or slug,
            "opportunity_link": opportunity_link(account, slug),
            "risks_link": risks_link(account, slug),
            "overview": {
                "status": overview_status,
                "revision": version.revision if version else None,
                "version_id": version.version_id if version else None,
                "created_at": version.created_at.isoformat() if version else None,
            },
            "next_step": self._next_step(version),
            "next_action": next_action,
            "waiting_on": waiting_on,
            "action_counts": {"open": counts.get("open", 0), "blocked": counts.get("blocked", 0),
                              "proposed": counts.get("proposed", 0), "completed": counts.get("completed", 0),
                              "overdue": len(overdue)},
            "confirmed_blockers": blockers,
            "risks": risks,
            "evaluation": self._evaluation(account, slug),
            "freshness": freshness,
            "latest_change_at": latest_change.applied_at.isoformat() if latest_change else None,
            "latest_change_type": latest_change.change_type if latest_change else None,
            "attention": attention,
        }

    def portfolio(
        self, *, account: str | None = None, attention_only: bool = False, limit: int = DEFAULT_LIMIT, offset: int = 0
    ) -> dict[str, Any]:
        sources = self._sources()
        actions = self._actions()
        changes = self._all_changes()
        opportunities = self.local_opportunities()
        rows = [
            self._portfolio_row(entry, sources, actions, changes)
            for entry in opportunities
            if account is None or entry["account"] == account
        ]
        if attention_only:
            rows = [r for r in rows if r["attention"]]
        page = _page(rows, limit=limit, offset=offset)
        page["opportunities"] = page.pop("items")
        page["filters"] = {"account": account, "attention_only": attention_only}
        page["accounts"] = sorted({e["account"] for e in opportunities}, key=str.casefold)
        page["coverage"] = _COVERAGE.copy()
        return page

    # -------------------------------------------------------------- today

    def today(self, *, limit: int = DEFAULT_LIMIT, offset: int = 0) -> dict[str, Any]:
        today = self._today()
        items: list[dict[str, Any]] = []
        actions = [a for a in self._actions() if a.retraction is None]
        sources = self._sources()

        def item(kind: str, *, title: str, reason: str, account: str | None, slug: str | None,
                 next_step: str, link: str, when: str | None, **extra: Any) -> None:
            items.append({
                "kind": kind, "title": title, "reason": reason, "account": account, "opportunity_slug": slug,
                "opportunity_link": opportunity_link(account, slug) if account and slug else None,
                "next_step": next_step, "link": link, "when": when, **extra,
            })

        for a in actions:
            due = date.fromisoformat(a.due_date) if a.due_date else None
            if a.status in _OPEN and due and due < today:
                item("overdue_action", title=a.commitment,
                     reason=f"Due {a.due_date}, {(today - due).days} day(s) ago; still {a.status}.",
                     account=a.account, slug=a.opportunity_slug, next_step="Complete or reschedule the action",
                     link=f"#/command-center/actions/{a.action_id}", when=a.due_date,
                     action_id=a.action_id, party=a.party, owner=a.owner, status=a.status)
            elif a.status in _OPEN and due and due <= today + timedelta(days=DUE_SOON_DAYS):
                item("due_action", title=a.commitment,
                     reason=f"Due {a.due_date} ({(due - today).days} day(s)); {a.status}.",
                     account=a.account, slug=a.opportunity_slug, next_step="Confirm progress with the owner",
                     link=f"#/command-center/actions/{a.action_id}", when=a.due_date,
                     action_id=a.action_id, party=a.party, owner=a.owner, status=a.status)
            elif a.status == "proposed":
                item("proposal_review", title=a.commitment,
                     reason="The analysis could not attribute owner, date and commitment to one source passage; a human must accept or dismiss it.",
                     account=a.account, slug=a.opportunity_slug, next_step="Accept as open or dismiss",
                     link=f"#/command-center/actions/{a.action_id}", when=a.created_at.isoformat(),
                     action_id=a.action_id, party=a.party, owner=a.owner, status=a.status)
        opportunities = self.local_opportunities()
        for entry in opportunities:
            version, _ = self._current(entry["account"], entry["opportunity_slug"])
            freshness = self._source_freshness(entry["account"], entry["opportunity_slug"], sources, version)
            blockers = self._confirmed_blockers(version) if freshness["state"] != "unavailable" else []
            for blocker in blockers:
                item("confirmed_blocker", title=blocker["title"], reason=blocker["reason"],
                     account=entry["account"], slug=entry["opportunity_slug"],
                     next_step="Open the Opportunity Overview stakeholder map",
                     link=opportunity_link(entry["account"], entry["opportunity_slug"]),
                     when=version.created_at.isoformat() if version else None, blocker=blocker)
            for risk in self._risks(version, freshness, entry["account"], entry["opportunity_slug"]):
                if risk["severity"] not in ("high", "critical"):
                    continue
                item("risk_review", title=risk["title"], reason=risk["reason"],
                     account=entry["account"], slug=entry["opportunity_slug"],
                     next_step="Review the risk and verify its evidence in the Opportunity Overview",
                     link=risk["link"], when=risk["overview_created_at"],
                     risk=risk, freshness={
                         "state": freshness["state"], "label": freshness["label"],
                         "overview_revision": freshness["overview_revision"],
                         "overview_created_at": freshness["overview_created_at"],
                     })
        for s in sources:
            assoc = s["association"]
            acct = assoc.get("account") if assoc["state"] == "associated" else None
            slug = assoc.get("opportunity_slug") if assoc["state"] == "associated" else None
            link = f"#/command-center/sources/{s['source_id']}"
            status = s["processing"]["status"]
            if status in ("discovered", "awaiting_association"):
                item("association_review", title=f"Imported {s['kind']} needs an opportunity",
                     reason=("Candidates were proposed but none is confirmed." if assoc["state"] == "proposed"
                             else "The note is not linked to any opportunity, so nothing can be derived from it."),
                     account=acct, slug=slug, next_step="Review and confirm the association", link=link,
                     when=s["latest"]["observed_at"], source_id=s["source_id"], status=status)
            elif s["availability"] in _WITHHELD:
                item("source_failure", title="Source content is unavailable",
                     reason=f"Latest revision is {s['availability']}: {s['latest'].get('unavailable_reason') or 'content withheld'}.",
                     account=acct, slug=slug, next_step="Re-import the note once access is restored", link=link,
                     when=s["latest"]["observed_at"], source_id=s["source_id"], status=status)
            elif status == "failed":
                code = s["processing"].get("last_error_code") or "unknown_error"
                item("reconciliation_failure", title="Analysis of an imported source failed",
                     reason=f"Last attempt ended with `{code}`; the accepted Overview was left unchanged.",
                     account=acct, slug=slug,
                     next_step="Retry" if s["processing"].get("retry_eligible") else "Inspect the run history",
                     link=link, when=s["processing"]["updated_at"], source_id=s["source_id"], status=status)
            elif status == "awaiting_content":
                item("source_failure", title="Imported note has no content yet",
                     reason="Only metadata was available at import time.", account=acct, slug=slug,
                     next_step="Re-import when the transcript is ready", link=link,
                     when=s["latest"]["observed_at"], source_id=s["source_id"], status=status)
            elif assoc["state"] == "associated" and (
                status == "queued" or (
                    s["processing"]["processed_revision"] is not None
                    and s["processing"]["processed_revision"] < s["latest_revision"]
                    and status != "processing"
                )
            ):
                item("source_pending", title="Imported source needs reconciliation",
                     reason="The latest source revision is not reflected in the accepted Overview.",
                     account=acct, slug=slug, next_step="Review the source and reconcile it",
                     link=link, when=s["latest"]["observed_at"], source_id=s["source_id"], status=status)
        items.sort(key=lambda i: (_ATTENTION_ORDER[i["kind"]], i["when"] or "", i["title"]))
        counts: dict[str, int] = {}
        for i in items:
            counts[i["kind"]] = counts.get(i["kind"], 0) + 1
        page = _page(items, limit=limit, offset=offset)
        page["attention"] = page.pop("items")
        page["counts_by_kind"] = counts
        page["recent_changes"] = self.changes(limit=RECENT_CHANGE_LIMIT)["changes"]
        page["as_of"] = self._clock().astimezone(timezone.utc).isoformat()
        page["opportunity_count"] = len(opportunities)
        page["coverage"] = _COVERAGE.copy()
        return page

    # -------------------------------------------------------------- sources

    def source_review(self, source_id: str) -> dict[str, Any]:
        """Everything the Unprocessed Sources queue needs to act on one source, body-free."""
        try:
            source = self._ledger.get_source(source_id)
        except EvidenceLedgerError as exc:
            raise CommandCenterReadError(exc.status_code, exc.detail, code=exc.code) from exc
        assoc = source["association"]
        overview_base = None
        if assoc["state"] == "associated":
            version, status = self._current(assoc["account"], assoc["opportunity_slug"])
            overview_base = {
                "status": status,
                "version_id": version.version_id if version else None,
                "revision": version.revision if version else None,
                "opportunity_link": opportunity_link(assoc["account"], assoc["opportunity_slug"]),
            }
        try:
            runs = self._ops.list_runs(source_id)
        except CommandCenterOperationsError:
            runs = []
        derived = [
            self._present_action(a) for a in self._actions()
            if a.origin.source_id == source_id or any(ref.source_id == source_id for ref in a.evidence)
        ]
        processing = source["processing"]
        active = self._ops._running_job_for_source(source_id)
        pending = self._ops._read_pending(source_id)
        can_start_processing = (
            processing["status"] in ("queued", "failed")
            or (processing["status"] == "processing" and active is None)
            or (pending is not None and active is None)
        )
        can_reconcile = (
            assoc["state"] == "associated" and overview_base is not None and overview_base["status"] == "current"
            and can_start_processing
            and source["availability"] == "content_available"
        )
        can_create_first = (
            assoc["state"] == "associated" and overview_base is not None
            and overview_base["status"] == "not_created"
            and can_start_processing
            and source["availability"] == "content_available"
        )
        return {
            "source": source,
            "overview_base": overview_base,
            "runs": runs,
            "derived_actions": derived,
            "active_reconciliation_job_id": active[0] if active else None,
            "pending_action_application": pending is not None,
            "candidates": self.local_opportunities(),
            "capabilities": {
                "confirm_association": True,
                "clear_association": assoc["state"] == "associated",
                "retry": processing["status"] == "failed" and bool(processing.get("retry_eligible")),
                "reconcile": can_reconcile,
                "create_first_overview": can_create_first,
                "reconcile_blocked_reason": None if can_reconcile else self._reconcile_blocker(source, overview_base),
            },
        }

    @staticmethod
    def _reconcile_blocker(source: dict[str, Any], base: dict[str, Any] | None) -> str:
        if source["association"]["state"] != "associated":
            return "Confirm an opportunity association first."
        if source["availability"] != "content_available":
            return f"Source content is {source['availability']}; nothing can be analysed."
        if base is None or base["status"] != "current":
            return "This opportunity has no current Overview. Create its first Overview from this meeting."
        if source["processing"]["status"] == "processing":
            return "A reconciliation is already running for this source."
        return "The source is not in a state that can be reconciled."
