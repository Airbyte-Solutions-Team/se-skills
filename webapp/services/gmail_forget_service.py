"""User-initiated, retryable removal of one imported Gmail message.

The deletion receipt is committed first. All later work is repeatable and the
receipt contains only an opaque source id, workspace id, timestamp and status.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from opportunity_state import GenerationProvenance, OpportunityStateCandidate, evidence_manifest_hash
from services.command_center_operations_service import CommandCenterOperationsService
from services.evidence_ledger_service import EvidenceLedgerService
from services.gmail_intake_service import GmailIntakeService
from services.opportunity_state_service import OpportunityStateError, OpportunityStateService


class _UnsafeMerge(Exception):
    """A later edit shares a field changed by Gmail; withhold instead of guessing."""


_MISSING = object()


def _copy(value: Any) -> Any:
    return _MISSING if value is _MISSING else deepcopy(value)


def _key(item: Any) -> Any:
    if not isinstance(item, dict):
        return None
    if "key" in item:
        return ("key", item["key"])
    if "source_type" in item and "source_id" in item:
        return ("evidence", item["source_type"], item["source_id"], item.get("locator"))
    return None


def _undo(before: Any, after: Any, current: Any) -> Any:
    """Reverse one Gmail delta, preserving later edits to disjoint fields/items."""
    if before == after:
        return _copy(current)
    if current == before:
        return _copy(current)
    if current == after:
        return _copy(before)
    if isinstance(before, dict) and isinstance(after, dict) and isinstance(current, dict):
        result = deepcopy(current)
        for name in before.keys() | after.keys():
            previous = before.get(name, _MISSING)
            introduced = after.get(name, _MISSING)
            now = current.get(name, _MISSING)
            restored = _undo(previous, introduced, now)
            if restored is _MISSING:
                result.pop(name, None)
            else:
                result[name] = restored
        return result
    if all(isinstance(value, list) for value in (before, after, current)):
        groups = [[_key(item) for item in value] for value in (before, after, current)]
        if all(all(key is not None for key in group) and len(group) == len(set(group)) for group in groups):
            old = dict(zip(groups[0], before))
            new = dict(zip(groups[1], after))
            now = dict(zip(groups[2], current))
            result = dict(now)
            for key in old.keys() | new.keys():
                restored = _undo(old.get(key, _MISSING), new.get(key, _MISSING), now.get(key, _MISSING))
                if restored is _MISSING:
                    result.pop(key, None)
                else:
                    result[key] = restored
            order = [key for key in groups[2] if key in result]
            order += [key for key in groups[0] if key in result and key not in order]
            return [result[key] for key in order]
    raise _UnsafeMerge


class GmailForgetService:
    def __init__(
        self, *, ledger: EvidenceLedgerService, gmail: GmailIntakeService,
        operations: CommandCenterOperationsService, state: OpportunityStateService,
    ) -> None:
        self._ledger = ledger
        self._gmail = gmail
        self._ops = operations
        self._state = state

    def _affected_opportunities(self, source_id: str) -> list[tuple[str, str]]:
        """Find every local Overview, including an older association target."""
        found: list[tuple[str, str]] = []
        root = self._ledger.customers_dir
        for account in root.iterdir():
            opportunities = account / "opportunities"
            if account.is_symlink() or not opportunities.is_dir() or opportunities.is_symlink():
                continue
            for opportunity in opportunities.iterdir():
                if opportunity.is_symlink() or not opportunity.is_dir():
                    continue
                try:
                    history = self._state._read_history_unfiltered(account.name, opportunity.name)
                except OpportunityStateError as exc:
                    if exc.code == "unknown_account":
                        continue
                    raise
                if any(
                    entry.source_id.startswith(f"{source_id}_r")
                    for version in history for entry in version.evidence_manifest
                ):
                    found.append((account.name, opportunity.name))
        return found

    def _repair_overview(self, source_id: str, account: str, slug: str) -> str:
        history = self._state._read_history_unfiltered(account, slug)
        if not history:
            return "not_present"
        current = history[-1]
        repair_tag = f"gmail-forget-v1-{source_id}"
        if any(version.provenance.updater_version == repair_tag for version in history):
            return "repaired"
        transitions = [
            (parent, child) for parent, child in zip(history, history[1:])
            if child.change_set and any(
                item.source_id.startswith(f"{source_id}_r") for item in child.change_set.evidence_sources
            )
        ]
        if not transitions:
            return "withheld"
        # A later model run sees the tainted Overview as its base. Even when it
        # changes only a different field, it may have copied or paraphrased the
        # forgotten message there. A structural inverse cannot prove otherwise.
        attributed = {child.version_id for _parent, child in transitions}
        first_revision = transitions[0][1].revision
        if any(
            version.revision > first_revision
            and version.version_id not in attributed
            and version.provenance.runtime not in {"human_edit", "gmail_forget"}
            for version in history
        ):
            return "withheld"
        candidate: Any = current.state.model_dump(mode="json")
        try:
            for parent, child in reversed(transitions):
                candidate = _undo(
                    parent.state.model_dump(mode="json"),
                    child.state.model_dump(mode="json"), candidate,
                )
            state = OpportunityStateCandidate.model_validate(candidate)
            manifest = [entry for entry in current.evidence_manifest
                        if not entry.source_id.startswith(f"{source_id}_r")]
            self._state.promote_update(
                identity=current.identity,
                expected_parent_version_id=current.version_id,
                expected_parent_revision=current.revision,
                evidence_manifest=manifest,
                expected_manifest_hash=evidence_manifest_hash(manifest),
                provenance=GenerationProvenance(
                    updater_version=repair_tag, model="none", runtime="gmail_forget", cli_version="n/a",
                ),
                candidate=state,
            )
        except (_UnsafeMerge, ValueError, OpportunityStateError):
            # The read barrier remains active. Human revisions stay on disk.
            return "withheld"
        return "repaired"

    def forget(self, source_id: str) -> dict[str, Any]:
        with self._ops._exclusive():
            receipt = self._ledger.forget_receipt(source_id)
            if receipt is not None and receipt["status"] == "complete":
                return receipt
            if receipt is None:
                self._ledger.begin_forget(source_id)
            # The barrier now blocks reads, re-import and reconciliation even if
            # any following step fails or this process exits.
            self._gmail.forget_index(source_id)
            self._ops._remove_pending(source_id)
            for account, slug in sorted({
                (action.account, action.opportunity_slug)
                for action in self._ops._all_actions()
                if action.origin.source_id == source_id
                or any(ref.source_id == source_id for ref in action.evidence)
                or any(item.source.source_id == source_id for item in action.completion_suggestions)
            }):
                self._ops._retract_source(
                    source_id, from_account=account, from_opportunity_slug=slug,
                    to_account=None, to_opportunity_slug=None,
                    reason="Gmail message forgotten", revert_overview=False,
                )
            overview_status = "not_present"
            for account, slug in self._affected_opportunities(source_id):
                result = self._repair_overview(source_id, account, slug)
                if result == "withheld":
                    overview_status = "withheld"
                elif overview_status != "withheld":
                    overview_status = "repaired"
            self._ledger.purge_forgotten_source(source_id)
            return self._ledger.finish_forget(source_id, overview_status=overview_status)
