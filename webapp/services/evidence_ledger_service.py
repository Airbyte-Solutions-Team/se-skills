"""Workspace-scoped, append-only evidence ledger for the Command Center pilot.

Layout under `<customers_dir>/.command-center/`:

    scope.json                         scope identity for this workspace
    .lock                              cross-process write lock (advisory)
    sources/<source_id>.json           source record (metadata, statuses, history)
    content/<source_id>/<hash>.json    private snapshot per revision (never listed)

Source records grow by appending revisions and association decisions; snapshot
files are written once and never rewritten. Every record embeds the scope
identity and is rejected on read if it belongs to another workspace.

A snapshot holds the private per-revision metadata (title, attendees, times,
URL) plus the body, and its hash is the revision key, so a metadata-only edit
is a new revision rather than a silent `duplicate`.

Access policy: once a source's latest revision is `access_lost`, `deleted`, or
`pending_unknown`, `read_content` withholds *every* cached revision. Files stay
on disk unchanged; whether they are purged or unlocked again is a documented
follow-up decision (retention / re-authorization), not something the pilot
decides implicitly.

File permissions: directories are created 0700 and files 0600 on POSIX. On
Windows `os.chmod` cannot express this; the ledger then relies on the ACLs the
user profile directory inherits (private to the logged-in user by default) and
sets nothing further. The local pilot is single-user by definition.

Concurrency: all mutations take an in-process lock *and* an advisory
cross-process lock on `.lock` (`fcntl.flock` on POSIX, `msvcrt.locking` on
Windows), so a second process (e.g. a future reconciliation worker) cannot
interleave a read-modify-write of the same source record. Readers do not lock;
they rely on the atomic rename plus the record checksum.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Iterator

from pydantic import ValidationError

from command_center_evidence import (
    AUTO_ASSOCIATION_METHODS,
    HEX64,
    HUMAN_ASSOCIATION_METHODS,
    SOURCE_ID,
    WITHHOLD_CONTENT_AVAILABILITY,
    AssociationCandidate,
    AssociationDecision,
    AssociationMethod,
    EvidenceSource,
    NormalizedMeeting,
    ProcessingState,
    ProcessingStatus,
    ScopeIdentity,
    SourceRevision,
    canonical_bytes,
    sha256_hex,
    source_id_for,
)
from services.path_utils import resolve_within
from services.private_store import atomic_write_private, exclusive_file_lock, mkdir_private


_MAX_RECORD_BYTES = 2_000_000
_MAX_CONTENT_BYTES = 4_000_000
_UNPROCESSED: frozenset[str] = frozenset({
    "discovered", "awaiting_association", "awaiting_content", "queued", "processing", "failed"
})
_LIST_LIMIT = 200
_MAX_LIST_LIMIT = 500

_mkdir_private = mkdir_private


IdentityResolver = Callable[[str, str], Awaitable[dict[str, Any]]]


class EvidenceLedgerError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str = "ledger_error") -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(detail)


class EvidenceLedgerService:
    """Persist source records and private content for one local workspace."""

    LEDGER_DIR_NAME = ".command-center"

    def __init__(
        self,
        customers_dir: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        actor: str = "local_user",
    ) -> None:
        self.customers_dir = Path(customers_dir)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._actor = actor
        self._lock = threading.Lock()
        self._scope: ScopeIdentity | None = None

    # ----------------------------------------------------------------- scope

    def scope(self) -> ScopeIdentity:
        """Scope identity: a stable opaque digest of the resolved workspace path."""
        if self._scope is None:
            resolved = str(self.customers_dir.resolve())
            digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
            computed = ScopeIdentity(workspace_id=digest)
            recorded = self._read_scope_file()
            if recorded is not None and recorded != computed:
                raise EvidenceLedgerError(
                    409, "Ledger storage belongs to a different workspace.", code="scope_mismatch"
                )
            self._scope = computed
        return self._scope

    def _now(self) -> datetime:
        return self._clock().astimezone(timezone.utc)

    # ----------------------------------------------------------------- paths

    def _ledger_dir(self, *, create: bool = False) -> Path:
        ledger = resolve_within(self.customers_dir, self.LEDGER_DIR_NAME)
        if ledger.exists() and ledger.is_symlink():
            raise EvidenceLedgerError(409, "Ledger storage is unsafe.", code="unsafe_storage")
        if create:
            _mkdir_private(ledger)
            _mkdir_private(ledger / "sources")
            _mkdir_private(ledger / "content")
        return ledger

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """In-process lock plus advisory cross-process lock on `<ledger>/.lock`."""
        with self._lock, exclusive_file_lock(self._ledger_dir(create=True) / ".lock"):
            yield

    def _scope_path(self) -> Path:
        return self._ledger_dir() / "scope.json"

    def _source_path(self, source_id: str, *, create: bool = False) -> Path:
        if not SOURCE_ID.fullmatch(source_id):
            raise EvidenceLedgerError(404, "Unknown source.", code="unknown_source")
        return resolve_within(self._ledger_dir(create=create) / "sources", f"{source_id}.json")

    def _content_path(self, source_id: str, content_hash: str, *, create: bool = False) -> Path:
        if not SOURCE_ID.fullmatch(source_id) or not HEX64.fullmatch(content_hash):
            raise EvidenceLedgerError(404, "Unknown content revision.", code="unknown_content")
        directory = resolve_within(self._ledger_dir(create=create) / "content", source_id)
        if create:
            _mkdir_private(directory)
        return resolve_within(directory, f"{content_hash}.json")

    # -------------------------------------------------------------------- io

    _atomic_write = staticmethod(atomic_write_private)

    @staticmethod
    def _read_json(path: Path, *, limit: int) -> Any:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise EvidenceLedgerError(409, "Ledger storage is malformed.", code="malformed_storage")
            if info.st_size <= 0 or info.st_size > limit:
                raise EvidenceLedgerError(409, "Ledger storage is malformed.", code="malformed_storage")
            return json.loads(path.read_text(encoding="utf-8"))
        except EvidenceLedgerError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise EvidenceLedgerError(409, "Ledger storage is malformed.", code="malformed_storage") from exc

    def _read_scope_file(self) -> ScopeIdentity | None:
        path = self._scope_path()
        if not path.exists():
            return None
        raw = self._read_json(path, limit=10_000)
        try:
            return ScopeIdentity.model_validate(raw)
        except ValidationError as exc:
            raise EvidenceLedgerError(409, "Ledger scope file is malformed.", code="malformed_storage") from exc

    def _ensure_scope_file(self) -> None:
        scope = self.scope()
        path = self._ledger_dir(create=True) / "scope.json"
        if not path.exists():
            self._atomic_write(path, canonical_bytes(scope.model_dump(mode="json")))

    def _write_source(self, source: EvidenceSource) -> None:
        payload = source.model_dump(mode="json")
        envelope = {"source": payload, "checksum": sha256_hex(canonical_bytes(payload))}
        self._atomic_write(self._source_path(source.source_id, create=True), canonical_bytes(envelope))

    def _read_source(self, source_id: str) -> EvidenceSource:
        path = self._source_path(source_id)
        if not path.exists():
            raise EvidenceLedgerError(404, "Unknown source.", code="unknown_source")
        raw = self._read_json(path, limit=_MAX_RECORD_BYTES)
        if not isinstance(raw, dict) or set(raw) != {"source", "checksum"}:
            raise EvidenceLedgerError(409, "Source record is malformed.", code="malformed_storage")
        if raw["checksum"] != sha256_hex(canonical_bytes(raw["source"])):
            raise EvidenceLedgerError(409, "Source record checksum mismatch.", code="malformed_storage")
        try:
            source = EvidenceSource.model_validate(raw["source"])
        except ValidationError as exc:
            raise EvidenceLedgerError(409, "Source record is invalid.", code="malformed_storage") from exc
        if source.source_id != source_id:
            raise EvidenceLedgerError(409, "Source record identity mismatch.", code="malformed_storage")
        if source.scope != self.scope():
            raise EvidenceLedgerError(409, "Source record belongs to another workspace.", code="scope_mismatch")
        return source

    # --------------------------------------------------------------- imports

    def import_meetings(
        self,
        meetings: Iterable[NormalizedMeeting],
        *,
        import_id: str | None = None,
    ) -> dict[str, Any]:
        """Record user-selected meetings; idempotent on (scope, object, snapshot hash)."""
        import_token = import_id or uuid.uuid4().hex[:16]
        if not re.fullmatch(r"[a-f0-9]{16}", import_token):
            raise EvidenceLedgerError(400, "Invalid import id.", code="invalid_import_id")
        results: list[dict[str, Any]] = []
        with self._exclusive():
            self._ensure_scope_file()
            for meeting in meetings:
                results.append(self._record_meeting(meeting, import_token))
        return {
            "import_id": import_token,
            "trigger": "manual_import",
            "unattended_discovery": False,
            "results": results,
        }

    def _record_meeting(self, meeting: NormalizedMeeting, import_id: str) -> dict[str, Any]:
        scope = self.scope()
        source_id = source_id_for(scope, meeting.identity)
        now = self._now()
        snapshot = meeting.snapshot()
        snapshot_bytes = canonical_bytes(snapshot)
        if len(snapshot_bytes) > _MAX_CONTENT_BYTES:
            raise EvidenceLedgerError(413, "Content exceeds the ledger bound.", code="content_too_large")
        content_hash = sha256_hex(snapshot_bytes)
        body_hash = meeting.content.content_hash()
        metrics = meeting.content.metrics(
            title_present=meeting.title is not None, attendee_count=len(meeting.attendees)
        )

        path = self._source_path(source_id, create=True)
        existing = self._read_source(source_id) if path.exists() else None

        change = "initial"
        if existing is not None:
            if existing.identity != meeting.identity:
                raise EvidenceLedgerError(409, "Source identity collision.", code="storage_conflict")
            latest = existing.revisions[-1]
            if latest.content_hash == content_hash:
                return self._import_result(existing, latest, created=False, outcome="duplicate")
            if latest.availability != meeting.availability:
                change = "availability"
            elif latest.body_hash != body_hash:
                change = "content"
            else:
                change = "metadata"

        revision_number = 1 if existing is None else existing.latest_revision + 1
        revision = SourceRevision(
            revision=revision_number,
            content_hash=content_hash,
            body_hash=body_hash,
            change=change,
            availability=meeting.availability,
            trigger="manual_import",
            import_id=import_id,
            occurred_at=meeting.occurred_at,
            provider_updated_at=meeting.provider_updated_at,
            observed_at=now,
            retrieved_at=now,
            metrics=metrics,
            unavailable_reason=meeting.unavailable_reason,
        )
        content_path = self._content_path(source_id, content_hash, create=True)
        if not content_path.exists():
            self._atomic_write(content_path, snapshot_bytes)

        if existing is None:
            decision = AssociationDecision(
                sequence=1, state="unassociated", actor="system", reason="Imported without association.",
                recorded_at=now,
            )
            source = EvidenceSource(
                source_id=source_id,
                scope=scope,
                identity=meeting.identity,
                created_at=now,
                updated_at=now,
                latest_revision=1,
                availability=meeting.availability,
                processing=ProcessingState(status="discovered", updated_at=now),
                association=decision,
                association_history=[decision],
                revisions=[revision],
            )
            outcome = "created"
        else:
            processing = existing.processing
            if processing.status != "processing":
                processing = processing.model_copy(update={
                    "status": "discovered", "retry_eligible": False, "last_error_code": None, "updated_at": now,
                })
            source = existing.model_copy(update={
                "updated_at": now,
                "latest_revision": revision_number,
                "availability": meeting.availability,
                "revisions": [*existing.revisions, revision],
                "processing": processing,
            })
            outcome = "new_revision"
        source = self._derive_processing(source, now)
        self._write_source(source)
        return self._import_result(source, revision, created=True, outcome=outcome)

    @staticmethod
    def _derive_processing(source: EvidenceSource, now: datetime) -> EvidenceSource:
        """Move `discovered` to the honest waiting state; never invent progress."""
        processing = source.processing
        if processing.status not in {"discovered", "awaiting_association", "awaiting_content", "queued"}:
            return source
        if source.association.state != "associated":
            status: ProcessingStatus = "awaiting_association"
        elif source.availability != "content_available":
            status = "awaiting_content"
        else:
            status = "queued"
        if processing.processed_revision == source.latest_revision:
            status = "processed"
        return source.model_copy(update={
            "processing": processing.model_copy(update={"status": status, "updated_at": now}),
        })

    def _import_result(
        self, source: EvidenceSource, revision: SourceRevision, *, created: bool, outcome: str
    ) -> dict[str, Any]:
        return {
            "source_id": source.source_id,
            "provider_object_id": source.identity.provider_object_id,
            "outcome": outcome,
            "created_revision": created,
            "revision": revision.revision,
            "content_hash": revision.content_hash,
            "body_hash": revision.body_hash,
            "change": revision.change,
            "availability": revision.availability,
            "processing_status": source.processing.status,
            "association_state": source.association.state,
        }

    # ------------------------------------------------------------ association

    def propose_association(
        self, source_id: str, candidates: list[AssociationCandidate], *, reason: str
    ) -> dict[str, Any]:
        """Record review candidates. Proposals never change the effective association."""
        if not candidates:
            raise EvidenceLedgerError(400, "At least one candidate is required.", code="no_candidates")
        if any(candidate.method in AUTO_ASSOCIATION_METHODS for candidate in candidates):
            raise EvidenceLedgerError(400, "Explicit methods must confirm, not propose.", code="invalid_method")
        with self._exclusive():
            source = self._read_source(source_id)
            if source.association.state == "associated":
                raise EvidenceLedgerError(
                    409, "Source is already associated; correct it instead.", code="already_associated"
                )
            now = self._now()
            decision = AssociationDecision(
                sequence=len(source.association_history) + 1,
                state="proposed",
                actor="system",
                reason=reason,
                candidates=candidates,
                recorded_at=now,
            )
            source = self._append_decision(source, decision, now)
            self._write_source(source)
            return self.summarize(source)

    async def confirm_association(
        self,
        source_id: str,
        *,
        account: str,
        opportunity_slug: str,
        reason: str,
        resolve_identity: IdentityResolver,
        method: AssociationMethod = "explicit",
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Human-confirmed association, verified against the scoped opportunity record."""
        if method not in HUMAN_ASSOCIATION_METHODS:
            raise EvidenceLedgerError(400, "Only explicit or manual confirmation is accepted.", code="invalid_method")
        identity = await resolve_identity(account, opportunity_slug)
        safe_account = identity["safe_account"]
        safe_opp = identity["safe_opp"]
        opportunity = identity.get("opportunity") or {}
        with self._exclusive():
            source = self._read_source(source_id)
            current = source.association
            if (
                current.state == "associated"
                and current.account == safe_account
                and current.opportunity_slug == safe_opp
            ):
                return self.summarize(source)
            now = self._now()
            decision = AssociationDecision(
                sequence=len(source.association_history) + 1,
                state="associated",
                account=safe_account,
                opportunity_slug=safe_opp,
                method=method,
                actor=actor or self._actor,
                reason=reason,
                crm_account_id=opportunity.get("sfdc_account_id"),
                crm_opportunity_id=opportunity.get("sfdc_id"),
                supersedes_sequence=current.sequence if current.state == "associated" else None,
                recorded_at=now,
            )
            source = self._append_decision(source, decision, now)
            if decision.supersedes_sequence is not None:
                # A corrected association invalidates anything derived from the
                # earlier target; the revision itself stays immutable.
                source = source.model_copy(update={
                    "processing": source.processing.model_copy(update={
                        "status": "discovered", "processed_revision": None, "retry_eligible": False,
                        "last_error_code": None, "updated_at": now,
                    }),
                })
                source = self._derive_processing(source, now)
            self._write_source(source)
            return self.summarize(source)

    def clear_association(self, source_id: str, *, reason: str, actor: str | None = None) -> dict[str, Any]:
        with self._exclusive():
            source = self._read_source(source_id)
            current = source.association
            if current.state == "unassociated":
                return self.summarize(source)
            now = self._now()
            decision = AssociationDecision(
                sequence=len(source.association_history) + 1,
                state="unassociated",
                actor=actor or self._actor,
                reason=reason,
                supersedes_sequence=current.sequence if current.state == "associated" else None,
                recorded_at=now,
            )
            source = self._append_decision(source, decision, now)
            source = source.model_copy(update={
                "processing": source.processing.model_copy(update={
                    "status": "discovered", "processed_revision": None, "retry_eligible": False,
                    "last_error_code": None, "updated_at": now,
                }),
            })
            source = self._derive_processing(source, now)
            self._write_source(source)
            return self.summarize(source)

    def _append_decision(self, source: EvidenceSource, decision: AssociationDecision, now: datetime) -> EvidenceSource:
        source = source.model_copy(update={
            "updated_at": now,
            "association": decision,
            "association_history": [*source.association_history, decision],
        })
        return self._derive_processing(source, now)

    # ------------------------------------------------------------- processing

    def mark_processing(self, source_id: str) -> dict[str, Any]:
        with self._exclusive():
            source = self._read_source(source_id)
            if source.processing.status not in {"queued", "failed"}:
                raise EvidenceLedgerError(
                    409, f"Source is {source.processing.status}, not queued.", code="not_queued"
                )
            if source.processing.status == "failed" and not source.processing.retry_eligible:
                raise EvidenceLedgerError(409, "Source is not retry-eligible.", code="retry_blocked")
            now = self._now()
            source = source.model_copy(update={
                "updated_at": now,
                "processing": source.processing.model_copy(update={
                    "status": "processing", "attempts": source.processing.attempts + 1, "updated_at": now,
                }),
            })
            self._write_source(source)
            return self.summarize(source)

    def mark_processed(self, source_id: str, *, revision: int) -> dict[str, Any]:
        """Complete processing of exactly `revision`; a newer revision supersedes it."""
        with self._exclusive():
            source = self._read_source(source_id)
            if source.processing.status != "processing":
                raise EvidenceLedgerError(409, "Source is not being processed.", code="not_processing")
            now = self._now()
            superseded = revision != source.latest_revision
            source = source.model_copy(update={
                "updated_at": now,
                "processing": source.processing.model_copy(update={
                    "status": "discovered" if superseded else "processed",
                    "processed_revision": source.processing.processed_revision if superseded else revision,
                    "retry_eligible": False,
                    "last_error_code": None,
                    "updated_at": now,
                }),
            })
            if superseded:
                source = self._derive_processing(source, now)
            self._write_source(source)
            summary = self.summarize(source)
            summary["outcome"] = "superseded" if superseded else "processed"
            return summary

    def mark_failed(self, source_id: str, *, error_code: str, retry_eligible: bool) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9_]{1,80}", error_code):
            raise EvidenceLedgerError(400, "Invalid error code.", code="invalid_error_code")
        with self._exclusive():
            source = self._read_source(source_id)
            if source.processing.status != "processing":
                raise EvidenceLedgerError(409, "Source is not being processed.", code="not_processing")
            now = self._now()
            source = source.model_copy(update={
                "updated_at": now,
                "processing": source.processing.model_copy(update={
                    "status": "failed", "retry_eligible": retry_eligible,
                    "last_error_code": error_code, "updated_at": now,
                }),
            })
            self._write_source(source)
            return self.summarize(source)

    def retry(self, source_id: str) -> dict[str, Any]:
        """Idempotent manual retry: failed+eligible -> queued; anything else is a no-op."""
        with self._exclusive():
            source = self._read_source(source_id)
            processing = source.processing
            if processing.status == "failed" and processing.retry_eligible:
                now = self._now()
                source = source.model_copy(update={
                    "updated_at": now,
                    "processing": processing.model_copy(update={
                        "status": "discovered", "retry_eligible": False, "updated_at": now,
                    }),
                })
                source = self._derive_processing(source, now)
                self._write_source(source)
            return self.summarize(source)

    # ------------------------------------------------------------------ reads

    def get_source(self, source_id: str) -> dict[str, Any]:
        return self.summarize(self._read_source(source_id), include_history=True)

    def read_content(self, source_id: str, *, revision: int) -> dict[str, Any]:
        """Private snapshot for one revision; for the analysis path only, never for lists.

        Withheld for every revision once the source's *latest* state is
        `access_lost`, `deleted`, or `pending_unknown` (see module docstring).
        """
        source = self._read_source(source_id)
        if source.availability in WITHHOLD_CONTENT_AVAILABILITY:
            raise EvidenceLedgerError(
                403,
                f"Content is withheld: source is {source.availability}.",
                code="content_withheld",
            )
        matching = next((item for item in source.revisions if item.revision == revision), None)
        if matching is None:
            raise EvidenceLedgerError(404, "Unknown source revision.", code="unknown_revision")
        if matching.availability != "content_available":
            raise EvidenceLedgerError(409, "Revision has no retrievable content.", code="content_unavailable")
        path = self._content_path(source_id, matching.content_hash)
        if not path.exists():
            raise EvidenceLedgerError(409, "Revision content is missing.", code="content_missing")
        raw = self._read_json(path, limit=_MAX_CONTENT_BYTES)
        if sha256_hex(canonical_bytes(raw)) != matching.content_hash:
            raise EvidenceLedgerError(409, "Revision content checksum mismatch.", code="malformed_storage")
        return raw

    def list_sources(
        self,
        *,
        status: str | None = None,
        unprocessed_only: bool = False,
        limit: int = _LIST_LIMIT,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not 1 <= limit <= _MAX_LIST_LIMIT or offset < 0:
            raise EvidenceLedgerError(400, "Invalid page bounds.", code="invalid_page")
        ledger = self._ledger_dir()
        sources_dir = ledger / "sources"
        items: list[dict[str, Any]] = []
        malformed = 0
        if sources_dir.is_dir():
            for path in sorted(sources_dir.iterdir()):
                if not path.name.endswith(".json") or path.name.startswith("."):
                    continue
                try:
                    source = self._read_source(path.name[:-5])
                except EvidenceLedgerError as exc:
                    if exc.code in {"malformed_storage", "scope_mismatch", "unknown_source"}:
                        malformed += 1
                        continue
                    raise
                if status is not None and source.processing.status != status:
                    continue
                if unprocessed_only and source.processing.status not in _UNPROCESSED:
                    continue
                items.append(self.summarize(source))
        items.sort(key=lambda item: (item["updated_at"], item["source_id"]), reverse=True)
        counts: dict[str, int] = {}
        for item in items:
            counts[item["processing"]["status"]] = counts.get(item["processing"]["status"], 0) + 1
        page = items[offset:offset + limit]
        next_offset = offset + limit if offset + limit < len(items) else None
        return {
            "scope": self.scope().model_dump(mode="json"),
            "total": len(items),
            "counts_by_status": counts,
            "offset": offset,
            "limit": limit,
            "next_offset": next_offset,
            "truncated": next_offset is not None,
            "malformed_records": malformed,
            "sources": page,
        }

    def unprocessed_sources(self, *, limit: int = _LIST_LIMIT, offset: int = 0) -> dict[str, Any]:
        """The Unprocessed Sources queue: everything not yet processed for its latest revision.

        `total` and `counts_by_status` cover every match; `sources` is one page
        and `next_offset` makes the remainder reachable.
        """
        return self.list_sources(unprocessed_only=True, limit=limit, offset=offset)

    @staticmethod
    def summarize(source: EvidenceSource, *, include_history: bool = False) -> dict[str, Any]:
        """Body-free projection of a source record."""
        latest = source.revisions[-1]
        payload: dict[str, Any] = {
            "source_id": source.source_id,
            "scope": source.scope.model_dump(mode="json"),
            "provider": source.identity.provider,
            "connection_id": source.identity.connection_id,
            "kind": source.identity.kind,
            "provider_object_id": source.identity.provider_object_id,
            "created_at": source.created_at.isoformat(),
            "updated_at": source.updated_at.isoformat(),
            "latest_revision": source.latest_revision,
            "revision_count": len(source.revisions),
            "availability": source.availability,
            "latest": {
                "revision": latest.revision,
                "content_hash": latest.content_hash,
                "body_hash": latest.body_hash,
                "change": latest.change,
                "availability": latest.availability,
                "trigger": latest.trigger,
                "import_id": latest.import_id,
                "occurred_at": latest.occurred_at.isoformat() if latest.occurred_at else None,
                "provider_updated_at": (
                    latest.provider_updated_at.isoformat() if latest.provider_updated_at else None
                ),
                "observed_at": latest.observed_at.isoformat(),
                "metrics": latest.metrics.model_dump(mode="json"),
                "unavailable_reason": latest.unavailable_reason,
            },
            "processing": source.processing.model_dump(mode="json"),
            "association": source.association.model_dump(mode="json"),
        }
        if include_history:
            payload["revisions"] = [item.model_dump(mode="json") for item in source.revisions]
            payload["association_history"] = [
                item.model_dump(mode="json") for item in source.association_history
            ]
        return payload
