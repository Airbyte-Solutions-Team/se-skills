"""Reconciliation and action lifecycle for the Command Center local pilot (PR C).

One end-to-end flow on top of the PR B ledger and the existing Opportunity
Overview runtime:

    user-confirmed association  ->  private revision snapshot  ->  existing
    `CanonicalStateExecutor`  ->  validated candidate  ->  compare-and-swap
    `OpportunityStateService.promote_update`  ->  trusted-layer observations
    ->  durable `ActionRecord`s  ->  deterministic `ChangeEntry`s.

Guarantees:

* Prior accepted state survives every failure: nothing is written to the
  overview or the action store until the candidate validated and the CAS
  promotion succeeded against the exact base the user saw. A failed, stale,
  cancelled, or interrupted run leaves the source `failed` (retry-eligible)
  and writes only a `ReconciliationRun` receipt.
* Repeated processing of the same `(source, observation key)` returns the
  existing action; a new revision of the same source *links* to it instead of
  creating another. Dismissed or completed actions are therefore never
  recreated from re-imports.
* Human transitions always win: analysis never changes the status, owner, or
  due date of an action that exists, it can only append a completion
  suggestion. Undo is an attributable new transition, never a deletion.
* `recommended_actions` remain suggestions. Only a recommendation that cites
  this source revision becomes an observation; it opens an action only when the
  responsible party is literal in the owner text and a due date is present,
  otherwise it is `proposed` for review.

Storage under `<customers_dir>/.command-center/operations/` (same private
permissions and cross-process lock discipline as the ledger). No record here
contains meeting bodies; the snapshot is read once and handed to the runtime.
"""
from __future__ import annotations

import json
import stat
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import ValidationError

from command_center_evidence import SAFE_TOKEN, SOURCE_ID, canonical_bytes, sha256_hex
from command_center_operations import (
    HUMAN_TRANSITIONS,
    ActionRecord,
    ActionTransition,
    ChangeEntry,
    CompletionSuggestion,
    DurableActionStatus,
    Observation,
    ReconciliationRun,
    Retraction,
    SourceRef,
    action_id_for,
    derive_party,
    normalize_text,
)
from opportunity_state import (
    ActionStatus,
    EvidenceSourceType,
    GenerationProvenance,
    OpportunityIdentity,
    OpportunityStateCandidate,
    OpportunityStateVersion,
    evidence_manifest_hash,
    validate_candidate_evidence,
)
from services.account_service import AccountError
from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService, IdentityResolver
from services.job_service import JobService, ManagedJobError
from services.opportunity_state_create_service import METADATA_SOURCE_ID, OpportunityStateCreateService
from services.opportunity_state_executor import (
    UPDATER_VERSION,
    CanonicalStateExecutionError,
    CanonicalStateExecutionRequest,
    CanonicalStateExecutor,
)
from services.opportunity_state_service import OpportunityStateError, OpportunityStateService
from services.opportunity_state_update_service import OpportunityStateUpdateService
from services.opportunity_workspace_service import OpportunityWorkspaceService
from services.path_utils import resolve_within
from services.private_store import atomic_write_private, exclusive_file_lock, mkdir_private
from services.transcription_service import ResolvedTranscriptEvidence


RECONCILE_JOB_KIND = "command_center_reconcile"
_MAX_RECORD_BYTES = 2_000_000
_MAX_LIST_LIMIT = 500
_MAX_EVIDENCE_BYTES = 2_000_000


class CommandCenterOperationsError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str = "operations_error") -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(detail)


def evidence_id_for(source_id: str, revision: int) -> str:
    """Manifest/evidence ID of one ledger revision as seen by the overview runtime."""
    return f"{source_id}_r{revision}"


def render_snapshot_text(snapshot: dict[str, Any]) -> bytes:
    """Deterministic plain-text rendering of a private revision snapshot for the runtime."""
    metadata = snapshot.get("metadata") or {}
    content = snapshot.get("content") or {}
    lines: list[str] = []
    title = metadata.get("title")
    if isinstance(title, str) and title:
        lines.append(f"# {title}")
    occurred = metadata.get("occurred_at")
    if isinstance(occurred, str) and occurred:
        lines.append(f"Occurred: {occurred}")
    attendees = metadata.get("attendees") or []
    names = [
        item.get("name") or item.get("email") for item in attendees
        if isinstance(item, dict) and (item.get("name") or item.get("email"))
    ]
    if names:
        lines.append("Attendees: " + ", ".join(str(name) for name in names))
    for label, field in (("Summary", "summary_markdown"), ("Summary", "summary_text"),
                         ("Private notes", "private_notes_markdown"), ("Private notes", "private_notes_text")):
        value = content.get(field)
        if isinstance(value, str) and value:
            lines.extend(["", f"## {label}", value])
    transcript = content.get("transcript") or []
    if transcript:
        lines.extend(["", "## Transcript"])
        for segment in transcript:
            if not isinstance(segment, dict):
                continue
            speaker = segment.get("speaker_label") or segment.get("attribution") or "speaker"
            lines.append(f"[{speaker}] {segment.get('text', '')}")
    return ("\n".join(lines).strip() + "\n").encode("utf-8")


class CommandCenterOperationsService:
    OPERATIONS_DIR_NAME = "operations"

    def __init__(
        self,
        *,
        ledger: EvidenceLedgerService,
        workspace_service: OpportunityWorkspaceService,
        state_service: OpportunityStateService,
        job_service: JobService,
        executor: CanonicalStateExecutor,
        clock: Callable[[], datetime] | None = None,
        actor: str = "local_user",
    ) -> None:
        self._ledger = ledger
        self._workspace_service = workspace_service
        self._state_service = state_service
        self._job_service = job_service
        self._executor = executor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._actor = actor
        self._lock = threading.Lock()
        self._start_lock = job_service.opportunity_state_start_lock

    # ------------------------------------------------------------- storage

    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    def _workspace_id(self) -> str:
        return self._ledger.scope().workspace_id

    def _root(self, *, create: bool = False) -> Path:
        ledger_dir = self._ledger._ledger_dir(create=create)
        root = resolve_within(ledger_dir, self.OPERATIONS_DIR_NAME)
        if root.exists() and root.is_symlink():
            raise CommandCenterOperationsError(409, "Operations storage is unsafe.", code="unsafe_storage")
        if create:
            for name in ("", "actions", "changes", "runs"):
                mkdir_private(root / name if name else root)
        return root

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        with self._lock, exclusive_file_lock(self._root(create=True) / ".lock"):
            yield

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise CommandCenterOperationsError(409, "Operations storage is malformed.", code="malformed_storage")
            if info.st_size <= 0 or info.st_size > _MAX_RECORD_BYTES:
                raise CommandCenterOperationsError(409, "Operations storage is malformed.", code="malformed_storage")
            return json.loads(path.read_text(encoding="utf-8"))
        except CommandCenterOperationsError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise CommandCenterOperationsError(409, "Operations storage is malformed.", code="malformed_storage") from exc

    @staticmethod
    def _write_record(path: Path, payload: dict[str, Any]) -> None:
        envelope = {"record": payload, "checksum": sha256_hex(canonical_bytes(payload))}
        atomic_write_private(path, canonical_bytes(envelope))

    def _read_record(self, path: Path) -> dict[str, Any]:
        raw = self._read_json(path)
        if not isinstance(raw, dict) or set(raw) != {"record", "checksum"}:
            raise CommandCenterOperationsError(409, "Operations record is malformed.", code="malformed_storage")
        if raw["checksum"] != sha256_hex(canonical_bytes(raw["record"])):
            raise CommandCenterOperationsError(409, "Operations record checksum mismatch.", code="malformed_storage")
        record = raw["record"]
        if not isinstance(record, dict) or record.get("workspace_id") != self._workspace_id():
            raise CommandCenterOperationsError(409, "Record belongs to another workspace.", code="scope_mismatch")
        return record

    def _action_path(self, action_id: str, *, create: bool = False) -> Path:
        return resolve_within(self._root(create=create) / "actions", f"{action_id}.json")

    def _changes_dir(self, account: str, opportunity_slug: str, *, create: bool = False) -> Path:
        for token in (account, opportunity_slug):
            if not SAFE_TOKEN.fullmatch(token):
                raise CommandCenterOperationsError(400, "Invalid account or opportunity.", code="invalid_scope")
        directory = resolve_within(self._root(create=create) / "changes", f"{account}__{opportunity_slug}")
        if create:
            mkdir_private(directory)
        return directory

    def _runs_dir(self, source_id: str, *, create: bool = False) -> Path:
        if not SOURCE_ID.fullmatch(source_id):
            raise CommandCenterOperationsError(404, "Unknown source.", code="unknown_source")
        directory = resolve_within(self._root(create=create) / "runs", source_id)
        if create:
            mkdir_private(directory)
        return directory

    def _write_action(self, action: ActionRecord) -> None:
        self._write_record(self._action_path(action.action_id, create=True), action.model_dump(mode="json"))

    def _load_action(self, action_id: str) -> ActionRecord:
        if not action_id.startswith("act_") or len(action_id) != 36:
            raise CommandCenterOperationsError(404, "Unknown action.", code="unknown_action")
        path = self._action_path(action_id)
        if not path.exists():
            raise CommandCenterOperationsError(404, "Unknown action.", code="unknown_action")
        try:
            action = ActionRecord.model_validate(self._read_record(path))
        except ValidationError as exc:
            raise CommandCenterOperationsError(409, "Action record is invalid.", code="malformed_storage") from exc
        if action.action_id != action_id:
            raise CommandCenterOperationsError(409, "Action record identity mismatch.", code="malformed_storage")
        return action

    def _all_actions(self) -> list[ActionRecord]:
        directory = self._root() / "actions"
        if not directory.exists():
            return []
        actions: list[ActionRecord] = []
        for path in sorted(directory.glob("act_*.json")):
            actions.append(self._load_action(path.stem))
        return actions

    def _opportunity_actions(self, account: str, opportunity_slug: str) -> list[ActionRecord]:
        return [
            item for item in self._all_actions()
            if item.account == account and item.opportunity_slug == opportunity_slug
        ]

    def _append_change(
        self,
        *,
        account: str,
        opportunity_slug: str,
        change_type: str,
        subject_id: str,
        before: dict[str, Any],
        after: dict[str, Any],
        source: SourceRef | None,
        actor: str,
        occurred_at: datetime,
        link: str,
    ) -> ChangeEntry:
        directory = self._changes_dir(account, opportunity_slug, create=True)
        existing = sorted(directory.glob("*.json"))
        sequence = len(existing) + 1
        now = self._now()
        payload = {
            "workspace_id": self._workspace_id(),
            "account": account,
            "opportunity_slug": opportunity_slug,
            "sequence": sequence,
            "change_type": change_type,
            "subject_id": subject_id,
            "before": before,
            "after": after,
            "source": source.model_dump(mode="json") if source else None,
            "actor": actor,
            "actor_id": self._actor,
            "occurred_at": occurred_at.isoformat(),
            "applied_at": now.isoformat(),
            "link": link,
        }
        change_id = "chg_" + sha256_hex(canonical_bytes(payload))[:32]
        entry = ChangeEntry.model_validate({**payload, "change_id": change_id})
        self._write_record(directory / f"{sequence:08d}.json", entry.model_dump(mode="json"))
        return entry

    def _write_run(self, run: ReconciliationRun) -> None:
        self._write_record(self._runs_dir(run.source_id, create=True) / f"{run.run_id}.json", run.model_dump(mode="json"))

    def list_runs(self, source_id: str) -> list[dict[str, Any]]:
        directory = self._runs_dir(source_id)
        if not directory.exists():
            return []
        runs = [ReconciliationRun.model_validate(self._read_record(path)) for path in directory.glob("run_*.json")]
        runs.sort(key=lambda item: (item.started_at, item.run_id))
        return [item.model_dump(mode="json") for item in runs]

    # ------------------------------------------------------------- reading

    def get_action(self, action_id: str) -> dict[str, Any]:
        return self._present(self._load_action(action_id))

    @staticmethod
    def _present(action: ActionRecord) -> dict[str, Any]:
        payload = action.model_dump(mode="json")
        payload["human_touched"] = action.human_touched
        payload["retracted"] = action.retraction is not None
        return payload

    def list_actions(
        self, account: str, opportunity_slug: str, *, status: str | None = None, include_retracted: bool = False
    ) -> dict[str, Any]:
        actions = self._opportunity_actions(account, opportunity_slug)
        if not include_retracted:
            actions = [item for item in actions if item.retraction is None]
        if status is not None:
            actions = [item for item in actions if item.status == status]
        actions.sort(key=lambda item: (item.created_at, item.action_id))
        counts: dict[str, int] = {}
        for item in actions:
            counts[item.status] = counts.get(item.status, 0) + 1
        return {
            "account": account,
            "opportunity_slug": opportunity_slug,
            "total": len(actions),
            "counts_by_status": counts,
            "actions": [self._present(item) for item in actions],
        }

    def list_changes(self, account: str, opportunity_slug: str, *, limit: int = 200, offset: int = 0) -> dict[str, Any]:
        if not 1 <= limit <= _MAX_LIST_LIMIT or offset < 0:
            raise CommandCenterOperationsError(400, "Invalid paging.", code="invalid_paging")
        directory = self._changes_dir(account, opportunity_slug)
        paths = sorted(directory.glob("*.json"), reverse=True) if directory.exists() else []
        page = paths[offset:offset + limit]
        entries = [ChangeEntry.model_validate(self._read_record(path)).model_dump(mode="json") for path in page]
        next_offset = offset + limit if offset + limit < len(paths) else None
        return {
            "account": account,
            "opportunity_slug": opportunity_slug,
            "total": len(paths),
            "changes": entries,
            "next_offset": next_offset,
        }

    # -------------------------------------------------- human transitions

    def transition_action(
        self,
        action_id: str,
        *,
        to_status: DurableActionStatus,
        reason: str,
        owner: str | None = None,
        due_date: str | None = None,
        clear_due_date: bool = False,
    ) -> dict[str, Any]:
        with self._exclusive():
            action = self._load_action(action_id)
            if action.retraction is not None:
                raise CommandCenterOperationsError(409, "Action was retracted by an association correction.", code="retracted")
            if (action.status, to_status) not in HUMAN_TRANSITIONS:
                raise CommandCenterOperationsError(
                    409, f"Cannot move an action from {action.status} to {to_status}.", code="invalid_transition"
                )
            now = self._now()
            transition = ActionTransition(
                sequence=len(action.transitions) + 1,
                from_status=action.status,
                to_status=to_status,
                actor="user",
                actor_id=self._actor,
                reason=reason,
                prior_owner=action.owner,
                prior_due_date=action.due_date,
                recorded_at=now,
            )
            update: dict[str, Any] = {
                "status": to_status,
                "transitions": [*action.transitions, transition],
                "updated_at": now,
            }
            if owner is not None:
                update["owner"] = owner
                update["party"] = derive_party(owner)
            if clear_due_date:
                update["due_date"] = None
            elif due_date is not None:
                update["due_date"] = due_date
            try:
                updated = action.model_copy(update=update)
                updated = ActionRecord.model_validate(updated.model_dump(mode="json"))
            except ValidationError as exc:
                raise CommandCenterOperationsError(400, "Invalid action update.", code="invalid_action") from exc
            self._write_action(updated)
            self._append_change(
                account=action.account,
                opportunity_slug=action.opportunity_slug,
                change_type="action_transition",
                subject_id=action.action_id,
                before={"status": action.status, "owner": action.owner, "due_date": action.due_date},
                after={"status": updated.status, "owner": updated.owner, "due_date": updated.due_date, "reason": reason},
                source=None,
                actor="user",
                occurred_at=now,
                link=f"/api/command-center/actions/{action.action_id}",
            )
            return self._present(updated)

    def undo_last_transition(self, action_id: str, *, reason: str) -> dict[str, Any]:
        """Revert the most recent user transition with a new, attributable transition."""
        with self._exclusive():
            action = self._load_action(action_id)
            last = action.transitions[-1]
            if last.actor != "user" or last.undoes_sequence is not None or last.from_status is None:
                raise CommandCenterOperationsError(409, "Nothing to undo on this action.", code="nothing_to_undo")
            now = self._now()
            transition = ActionTransition(
                sequence=len(action.transitions) + 1,
                from_status=action.status,
                to_status=last.from_status,
                actor="user",
                actor_id=self._actor,
                reason=reason,
                undoes_sequence=last.sequence,
                prior_owner=action.owner,
                prior_due_date=action.due_date,
                recorded_at=now,
            )
            updated = action.model_copy(update={
                "status": last.from_status,
                "owner": last.prior_owner,
                "party": derive_party(last.prior_owner),
                "due_date": last.prior_due_date,
                "transitions": [*action.transitions, transition],
                "updated_at": now,
            })
            self._write_action(updated)
            self._append_change(
                account=action.account,
                opportunity_slug=action.opportunity_slug,
                change_type="action_transition",
                subject_id=action.action_id,
                before={"status": action.status, "owner": action.owner, "due_date": action.due_date},
                after={
                    "status": updated.status, "owner": updated.owner, "due_date": updated.due_date,
                    "reason": reason, "undoes_sequence": last.sequence,
                },
                source=None,
                actor="user",
                occurred_at=now,
                link=f"/api/command-center/actions/{action.action_id}",
            )
            return self._present(updated)

    # ------------------------------------------------ association wrappers

    async def confirm_association(
        self,
        source_id: str,
        *,
        account: str,
        opportunity_slug: str,
        reason: str,
        resolve_identity: IdentityResolver,
    ) -> dict[str, Any]:
        before = self._ledger.get_source(source_id)["association"]
        summary = await self._ledger.confirm_association(
            source_id,
            account=account,
            opportunity_slug=opportunity_slug,
            reason=reason,
            resolve_identity=resolve_identity,
        )
        after = summary["association"]
        summary["retracted_actions"] = []
        if before["state"] == "associated" and (
            before["account"] != after["account"] or before["opportunity_slug"] != after["opportunity_slug"]
        ):
            summary["retracted_actions"] = self._retract_source(
                source_id,
                from_account=before["account"],
                from_opportunity_slug=before["opportunity_slug"],
                to_account=after["account"],
                to_opportunity_slug=after["opportunity_slug"],
                reason=reason,
            )
        return summary

    def clear_association(self, source_id: str, *, reason: str) -> dict[str, Any]:
        before = self._ledger.get_source(source_id)["association"]
        summary = self._ledger.clear_association(source_id, reason=reason)
        summary["retracted_actions"] = []
        if before["state"] == "associated":
            summary["retracted_actions"] = self._retract_source(
                source_id,
                from_account=before["account"],
                from_opportunity_slug=before["opportunity_slug"],
                to_account=None,
                to_opportunity_slug=None,
                reason=reason,
            )
        return summary

    def _retract_source(
        self,
        source_id: str,
        *,
        from_account: str,
        from_opportunity_slug: str,
        to_account: str | None,
        to_opportunity_slug: str | None,
        reason: str,
    ) -> list[str]:
        """Supersede derived actions from a mis-associated source; history and status are kept."""
        retracted: list[str] = []
        with self._exclusive():
            now = self._now()
            for action in self._opportunity_actions(from_account, from_opportunity_slug):
                if action.origin.source_id != source_id or action.retraction is not None:
                    continue
                updated = action.model_copy(update={
                    "retraction": Retraction(
                        from_account=from_account,
                        from_opportunity_slug=from_opportunity_slug,
                        to_account=to_account,
                        to_opportunity_slug=to_opportunity_slug,
                        reason=reason,
                        recorded_at=now,
                    ),
                    "updated_at": now,
                })
                self._write_action(updated)
                retracted.append(action.action_id)
                self._append_change(
                    account=from_account,
                    opportunity_slug=from_opportunity_slug,
                    change_type="association_corrected",
                    subject_id=action.action_id,
                    before={"account": from_account, "opportunity_slug": from_opportunity_slug, "status": action.status},
                    after={"account": to_account, "opportunity_slug": to_opportunity_slug, "retracted": True},
                    source=action.origin,
                    actor="user",
                    occurred_at=now,
                    link=f"/api/command-center/actions/{action.action_id}",
                )
        return retracted

    # ------------------------------------------------------- reconciliation

    async def _identity(self, account: str, opportunity_slug: str) -> dict[str, Any]:
        try:
            return await self._workspace_service.resolve_identity(account, opportunity_slug)
        except AccountError as exc:
            raise CommandCenterOperationsError(exc.status_code, exc.detail, code="unknown_opportunity") from exc

    def _current_or_error(self, account: str, opp_slug: str) -> OpportunityStateVersion:
        try:
            current = self._state_service.read_current(account, opp_slug)
        except OpportunityStateError as exc:
            raise CommandCenterOperationsError(exc.status_code, exc.detail, code=exc.code) from exc
        if current is None:
            raise CommandCenterOperationsError(
                409, "Create the Opportunity Overview before reconciling meeting evidence.", code="overview_missing"
            )
        return current

    def _running_job_for_source(self, source_id: str) -> tuple[str, dict[str, Any]] | None:
        for job_id, job in self._job_service.jobs.items():
            if job.get("kind") == RECONCILE_JOB_KIND and job.get("source_id") == source_id and job.get("status") == "running":
                return job_id, job
        return None

    async def start_reconciliation(
        self, source_id: str, *, base_version_id: str, base_revision: int
    ) -> dict[str, Any]:
        async with self._start_lock:
            try:
                source = self._ledger.get_source(source_id)
            except EvidenceLedgerError as exc:
                raise CommandCenterOperationsError(exc.status_code, exc.detail, code=exc.code) from exc
            association = source["association"]
            if association["state"] != "associated":
                raise CommandCenterOperationsError(
                    409, "Confirm the opportunity association before reconciling.", code="not_associated"
                )
            account, opp_slug = association["account"], association["opportunity_slug"]
            identity = await self._identity(account, opp_slug)
            if identity["safe_account"] != account or identity["safe_opp"] != opp_slug:
                raise CommandCenterOperationsError(409, "Association does not match this workspace.", code="scope_mismatch")
            current = self._current_or_error(account, opp_slug)
            if current.version_id != base_version_id or current.revision != base_revision:
                raise CommandCenterOperationsError(
                    409, "The overview changed since this base was read; reload and retry.", code="stale_base"
                )
            running = self._running_job_for_source(source_id)
            if running is not None:
                return {"job_id": running[0], "reused": True, "source_id": source_id}
            processing = source["processing"]
            if processing["status"] == "processed" and processing["processed_revision"] == source["latest_revision"]:
                raise CommandCenterOperationsError(
                    409, "This revision was already reconciled; import an edited note to process again.",
                    code="already_processed",
                )
            if processing["status"] == "processing":
                # No job owns it (e.g. process restart mid-run): make it retryable and record why.
                self._ledger.mark_failed(source_id, error_code="interrupted", retry_eligible=True)
                self._record_terminal_run(
                    source, account, opp_slug, base_version_id, base_revision,
                    status="interrupted", error_code="interrupted", started_at=self._now(),
                )
            if self._job_service.active_opportunity_state_job(account=account, opp_slug=opp_slug) is not None:
                raise CommandCenterOperationsError(
                    409, "Overview work is already running for this opportunity.", code="update_in_progress"
                )
            try:
                self._ledger.retry(source_id)
                self._ledger.mark_processing(source_id)
            except EvidenceLedgerError as exc:
                raise CommandCenterOperationsError(exc.status_code, exc.detail, code=exc.code) from exc
            revision = source["latest_revision"]

            async def runner(_job_id: str) -> dict[str, Any]:
                return await self._reconcile(
                    source_id, revision=revision, account=account, opp_slug=opp_slug,
                    identity=identity, base_version_id=base_version_id, base_revision=base_revision,
                )

            job_id, persist_warn = await self._job_service.launch_managed(
                kind=RECONCILE_JOB_KIND,
                account=account,
                opp_slug=opp_slug,
                opportunity=identity["opportunity"]["name"],
                sig=(RECONCILE_JOB_KIND, source_id, revision, base_version_id, base_revision, UPDATER_VERSION),
                safe_metadata={"source_id": source_id, "revision": revision, "base_version_id": base_version_id},
                runner=runner,
            )
            return {"job_id": job_id, "reused": False, "source_id": source_id, "persist_warn": persist_warn}

    def _record_terminal_run(
        self,
        source: dict[str, Any],
        account: str,
        opp_slug: str,
        base_version_id: str,
        base_revision: int,
        *,
        status: str,
        error_code: str | None,
        started_at: datetime,
        **extra: Any,
    ) -> ReconciliationRun:
        now = self._now()
        payload = {
            "workspace_id": self._workspace_id(),
            "source_id": source["source_id"],
            "revision": source["latest_revision"],
            "account": account,
            "opportunity_slug": opp_slug,
            "base_version_id": base_version_id,
            "base_revision": base_revision,
            "status": status,
            "error_code": error_code,
            "started_at": started_at.isoformat(),
            "finished_at": now.isoformat(),
            **extra,
        }
        run_id = "run_" + sha256_hex(canonical_bytes(payload))[:32]
        run = ReconciliationRun.model_validate({**payload, "run_id": run_id})
        self._write_run(run)
        return run

    def _evidence(self, source_id: str, revision: int, observed_at: datetime) -> ResolvedTranscriptEvidence:
        try:
            snapshot = self._ledger.read_content(source_id, revision=revision)
        except EvidenceLedgerError as exc:
            raise ManagedJobError(exc.code, exc.detail) from exc
        content = render_snapshot_text(snapshot)
        if len(content) > _MAX_EVIDENCE_BYTES or len(content.strip()) == 0:
            raise ManagedJobError("content_unusable", "Revision content is empty or exceeds the analysis bound.")
        return ResolvedTranscriptEvidence(
            evidence_id=evidence_id_for(source_id, revision),
            display_name=f"Meeting {source_id[-12:]} r{revision}",
            content=content,
            sha256=sha256_hex(content),
            byte_count=len(content),
            observed_at=observed_at,
        )

    async def _reconcile(
        self,
        source_id: str,
        *,
        revision: int,
        account: str,
        opp_slug: str,
        identity: dict[str, Any],
        base_version_id: str,
        base_revision: int,
    ) -> dict[str, Any]:
        started = self._now()
        source = self._ledger.get_source(source_id)
        try:
            if source["latest_revision"] != revision:
                raise ManagedJobError("revision_superseded", "The note was edited before analysis; process the new revision.")
            if source["association"]["state"] != "associated" or source["association"]["account"] != account \
                    or source["association"]["opportunity_slug"] != opp_slug:
                raise ManagedJobError("association_changed", "The association changed before analysis; no state was saved.")
            latest = source["latest"]
            observed_at = datetime.fromisoformat(latest["observed_at"])
            evidence = self._evidence(source_id, revision, observed_at)
            source_ref = SourceRef(source_id=source_id, revision=revision, evidence_id=evidence.evidence_id)

            current = self._current_or_error(account, opp_slug)
            resumed = self._promoted_but_unapplied(current, base_version_id, evidence)
            if resumed:
                version = current
                model = version.provenance.model
                runtime = version.provenance.runtime
            else:
                if current.version_id != base_version_id or current.revision != base_revision:
                    raise ManagedJobError("stale_base", "The overview changed since this base was read; reload and retry.")
                metadata = OpportunityStateUpdateService._metadata_entry(identity["opportunity"])
                manifest = OpportunityStateUpdateService._cumulative_manifest(current, metadata, [evidence])
                manifest_hash = evidence_manifest_hash(manifest)
                try:
                    result = await self._executor.execute(CanonicalStateExecutionRequest(
                        account=account,
                        opportunity_slug=opp_slug,
                        opportunity_name=identity["opportunity"]["name"],
                        opportunity_metadata=OpportunityStateCreateService._metadata_payload(identity["opportunity"]),
                        metadata_source_id=METADATA_SOURCE_ID,
                        transcripts=[evidence],
                        base_state=current.state,
                        base_version_id=current.version_id,
                        base_revision=current.revision,
                    ))
                except CanonicalStateExecutionError as exc:
                    raise ManagedJobError(exc.code, "Analysis failed; the accepted overview is unchanged.") from exc
                except Exception as exc:  # runtime error must never expose content
                    raise ManagedJobError("analysis_failed", f"Analysis failed ({type(exc).__name__}); no state was saved.") from exc
                try:
                    validate_candidate_evidence(result.candidate, manifest)
                except (ValidationError, ValueError) as exc:
                    raise ManagedJobError("invalid_candidate", "Analysis output failed validation; no state was saved.") from exc
                try:
                    version = self._state_service.promote_update(
                        identity=OpportunityIdentity(
                            account=account, opportunity_slug=opp_slug, opportunity_name=identity["opportunity"]["name"],
                        ),
                        expected_parent_version_id=base_version_id,
                        expected_parent_revision=base_revision,
                        evidence_manifest=manifest,
                        expected_manifest_hash=manifest_hash,
                        provenance=GenerationProvenance(
                            updater_version=UPDATER_VERSION, model=result.model,
                            runtime=result.runtime, cli_version=result.cli_version,
                        ),
                        candidate=result.candidate,
                    )
                except OpportunityStateError as exc:
                    raise ManagedJobError(exc.code, exc.detail) from exc
                model, runtime = result.model, result.runtime

            observations = self.derive_observations(version.state, source_ref)
            applied = self._apply_observations(
                observations, account=account, opp_slug=opp_slug, version=version, source_ref=source_ref,
                occurred_at=observed_at,
            )
            processed = self._ledger.mark_processed(source_id, revision=revision)
            run = self._record_terminal_run(
                source, account, opp_slug, base_version_id, base_revision,
                status="succeeded", error_code=None, started_at=started,
                promoted_version_id=version.version_id, promoted_revision=version.revision,
                observation_count=len(observations), model=model, runtime=runtime, **applied,
            )
            return {
                "run_id": run.run_id,
                "result_revision": version.revision,
                "result_version_id": version.version_id,
                "processing_outcome": processed["outcome"],
                "resumed": resumed,
                **applied,
            }
        except ManagedJobError as exc:
            self._fail(source, account, opp_slug, base_version_id, base_revision, exc.code, started)
            raise
        except (EvidenceLedgerError, CommandCenterOperationsError) as exc:
            self._fail(source, account, opp_slug, base_version_id, base_revision, exc.code, started)
            raise ManagedJobError(exc.code, exc.detail) from exc

    def _fail(
        self, source: dict[str, Any], account: str, opp_slug: str, base_version_id: str,
        base_revision: int, code: str, started: datetime,
    ) -> None:
        try:
            self._ledger.mark_failed(source["source_id"], error_code=code[:80], retry_eligible=True)
        except EvidenceLedgerError:
            pass
        status = "stale_base" if code == "stale_base" else "superseded" if code == "revision_superseded" else "failed"
        self._record_terminal_run(
            source, account, opp_slug, base_version_id, base_revision,
            status=status, error_code=code[:80], started_at=started,
        )

    @staticmethod
    def _promoted_but_unapplied(
        current: OpportunityStateVersion, base_version_id: str, evidence: ResolvedTranscriptEvidence
    ) -> bool:
        """True when a previous attempt promoted this exact evidence but died before finishing."""
        if current.parent_version_id != base_version_id:
            return False
        return any(
            entry.source_type == EvidenceSourceType.TRANSCRIPT
            and entry.source_id == evidence.evidence_id and entry.sha256 == evidence.sha256
            for entry in current.evidence_manifest
        )

    # -------------------------------------------------------- observations

    @staticmethod
    def derive_observations(candidate: OpportunityStateCandidate, source_ref: SourceRef) -> list[Observation]:
        """Trusted-layer observations: only recommendations that cite this revision, keyed by code."""
        observations: list[Observation] = []
        seen: set[str] = set()
        for recommendation in candidate.recommended_actions:
            refs = [ref for ref in recommendation.evidence_refs if ref.source_id == source_ref.evidence_id]
            if not refs:
                continue
            party = derive_party(recommendation.owner)
            kind = "completion_suggestion" if recommendation.status == ActionStatus.DONE else "commitment"
            key = Observation.key_for(
                source_id=source_ref.source_id, kind=kind, commitment=recommendation.action, party=party,
            )
            if key in seen:
                continue
            seen.add(key)
            observations.append(Observation(
                observation_key=key,
                kind=kind,
                commitment=recommendation.action,
                definition_of_done=recommendation.definition_of_done,
                party=party,
                owner=recommendation.owner,
                due_date=recommendation.due_date,
                explicit=party != "Unknown" and recommendation.due_date is not None,
                source=source_ref.model_copy(update={"locator": refs[0].locator}),
            ))
        return observations

    def _apply_observations(
        self,
        observations: list[Observation],
        *,
        account: str,
        opp_slug: str,
        version: OpportunityStateVersion,
        source_ref: SourceRef,
        occurred_at: datetime,
    ) -> dict[str, list[str]]:
        created: list[str] = []
        linked: list[str] = []
        suggested: list[str] = []
        duplicates: list[str] = []
        overview_link = f"/api/accounts/{account}/opportunities/{opp_slug}/overview/history/{version.revision}"
        with self._exclusive():
            existing = self._opportunity_actions(account, opp_slug)
            now = self._now()
            if not self._change_exists(account, opp_slug, "overview_revision", version.version_id):
                change_set = version.change_set
                counts = {} if change_set is None else {
                    section: len(items)
                    for section, items in (
                        ("brief", change_set.brief), ("business_case", change_set.business_case),
                        ("meddpicc", change_set.meddpicc), ("stakeholders", change_set.stakeholders),
                        ("health_indicators", change_set.health_indicators), ("risks", change_set.risks),
                        ("recommended_actions", change_set.recommended_actions),
                        ("missing_information", change_set.missing_information),
                    ) if items
                }
                self._append_change(
                    account=account, opportunity_slug=opp_slug, change_type="overview_revision",
                    subject_id=version.version_id,
                    before={"revision": version.parent_revision, "version_id": version.parent_version_id},
                    after={"revision": version.revision, "version_id": version.version_id, "changed_sections": counts},
                    source=source_ref, actor="analysis", occurred_at=occurred_at, link=overview_link,
                )
            for observation in observations:
                if observation.kind == "completion_suggestion":
                    for action in existing:
                        if action.retraction is not None or action.status not in {"open", "blocked"}:
                            continue
                        if normalize_text(action.commitment) != normalize_text(observation.commitment):
                            continue
                        if observation.party != "Unknown" and action.party != observation.party:
                            continue
                        if any(s.source == observation.source for s in action.completion_suggestions):
                            continue
                        updated = action.model_copy(update={
                            "completion_suggestions": [
                                *action.completion_suggestions,
                                CompletionSuggestion(
                                    source=observation.source, observation_key=observation.observation_key, recorded_at=now,
                                ),
                            ],
                            "updated_at": now,
                        })
                        self._write_action(updated)
                        existing = [updated if item.action_id == action.action_id else item for item in existing]
                        suggested.append(action.action_id)
                        self._append_change(
                            account=account, opportunity_slug=opp_slug, change_type="completion_suggested",
                            subject_id=action.action_id, before={"status": action.status},
                            after={"status": action.status, "suggested": "completed"},
                            source=observation.source, actor="analysis", occurred_at=occurred_at,
                            link=f"/api/command-center/actions/{action.action_id}",
                        )
                    continue

                action_id = action_id_for(
                    workspace_id=self._workspace_id(), account=account, opportunity_slug=opp_slug,
                    source_id=source_ref.source_id, observation_key=observation.observation_key,
                )
                match = next((item for item in existing if item.action_id == action_id), None)
                if match is not None:
                    if any(ref == observation.source for ref in match.evidence):
                        continue  # replay of the same revision: nothing to add
                    updated = match.model_copy(update={"evidence": [*match.evidence, observation.source], "updated_at": now})
                    self._write_action(updated)
                    existing = [updated if item.action_id == match.action_id else item for item in existing]
                    linked.append(match.action_id)
                    self._append_change(
                        account=account, opportunity_slug=opp_slug, change_type="action_linked",
                        subject_id=match.action_id, before={"revision": match.evidence[-1].revision},
                        after={"revision": observation.source.revision, "status": match.status},
                        source=observation.source, actor="analysis", occurred_at=occurred_at,
                        link=f"/api/command-center/actions/{match.action_id}",
                    )
                    continue

                duplicate_of = next((
                    item.action_id for item in existing
                    if item.retraction is None
                    and normalize_text(item.commitment) == normalize_text(observation.commitment)
                    and item.party == observation.party
                ), None)
                status: DurableActionStatus = "open" if observation.explicit and duplicate_of is None else "proposed"
                reason = (
                    "Explicit commitment with responsible party and due date"
                    if status == "open" and duplicate_of is None
                    else "Possible duplicate of an existing action; review before opening"
                    if duplicate_of is not None
                    else "Responsible party or due date not explicit; review required"
                )
                action = ActionRecord(
                    action_id=action_id,
                    workspace_id=self._workspace_id(),
                    account=account,
                    opportunity_slug=opp_slug,
                    commitment=observation.commitment,
                    definition_of_done=observation.definition_of_done,
                    party=observation.party,
                    owner=observation.owner,
                    due_date=observation.due_date,
                    status=status,
                    origin=observation.source,
                    observation_key=observation.observation_key,
                    evidence=[observation.source],
                    transitions=[ActionTransition(
                        sequence=1, from_status=None, to_status=status, actor="analysis", actor_id=self._actor,
                        reason=reason, source=observation.source, recorded_at=now,
                    )],
                    possible_duplicate_of=duplicate_of,
                    created_at=now,
                    updated_at=now,
                )
                self._write_action(action)
                existing.append(action)
                created.append(action_id)
                self._append_change(
                    account=account, opportunity_slug=opp_slug, change_type="action_created",
                    subject_id=action_id, before={},
                    after={"status": status, "party": observation.party, "due_date": observation.due_date},
                    source=observation.source, actor="analysis", occurred_at=occurred_at,
                    link=f"/api/command-center/actions/{action_id}",
                )
                if duplicate_of is not None:
                    duplicates.append(action_id)
                    self._append_change(
                        account=account, opportunity_slug=opp_slug, change_type="possible_duplicate_flagged",
                        subject_id=action_id, before={}, after={"possible_duplicate_of": duplicate_of},
                        source=observation.source, actor="analysis", occurred_at=occurred_at,
                        link=f"/api/command-center/actions/{action_id}",
                    )
        return {
            "actions_created": created,
            "actions_linked": linked,
            "completion_suggestions": suggested,
            "possible_duplicates": duplicates,
        }

    def _change_exists(self, account: str, opp_slug: str, change_type: str, subject_id: str) -> bool:
        directory = self._changes_dir(account, opp_slug)
        if not directory.exists():
            return False
        for path in directory.glob("*.json"):
            record = self._read_record(path)
            if record.get("change_type") == change_type and record.get("subject_id") == subject_id:
                return True
        return False
