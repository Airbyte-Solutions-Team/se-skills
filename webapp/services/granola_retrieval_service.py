"""User-triggered Granola retrieval for the local pilot.

Three explicit user steps, each one bounded and each one a fresh provider call:

1. **Check connection** — `get_account_info` through the relay. Success means
   the user's own Claude Code reached Granola with its current sign-in; the
   check records only *that* it succeeded for this ledger scope. Meetings come
   from whichever Granola account/workspace is active in Claude Code, and this
   pilot does **not** verify workspace switches by a stable provider id: the
   observed account response carries no documented stable workspace id, so
   the user inspects and selects meetings before anything is imported.
2. **List meetings** — one `list_meetings` call for a user-chosen range. The
   response is metadata only (id, title, date, participant count, access
   flags, URL) annotated with what the ledger already knows. Nothing from the
   listing is persisted: it is a snapshot from the moment the user clicked,
   not discovery, and no freshness is implied.
3. **Retrieve selected** — for up to `MAX_SELECTION` ids, `get_meetings` in
   one batch plus one `get_meeting_transcript` per meeting, merged into the
   observed MCP payload shape and passed through the existing adapter and
   ledger. Only the ids the user requested are imported: returned rows whose
   id was not requested are dropped and counted, and a transcript whose id
   does not match its meeting is discarded. A connection failure before the
   batch fails the whole job closed with nothing imported. Runs as a managed
   job whose metadata and result hold ids, counts and outcome codes only —
   never titles, summaries, transcript text, tokens or raw tool output.

Per-meeting outcomes: `imported`, `already_known`, `edited`, `pending_content`,
`inaccessible`, `failed_retryable`, `rejected`.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from command_center_evidence import NormalizedMeeting
from integrations.granola import (
    GRANOLA_MCP_MEETING_ID,
    GranolaImportError,
    GranolaSourceAdapter,
)
from integrations.granola_mcp_relay import (
    MAX_MEETINGS_PER_GET,
    GranolaRelayError,
    GranolaRetrievalTransport,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService
from services.job_service import JobService, ManagedJobError
from services.private_store import atomic_write_private, mkdir_private

RETRIEVAL_JOB_KIND = "command_center_granola_retrieval"
RETRIEVAL_CONNECTION_ID = "claude-code-mcp"
MAX_SELECTION = MAX_MEETINGS_PER_GET
MAX_LISTED = 100
TimeRange = Literal["today", "yesterday", "this_week", "last_week", "last_30_days", "custom"]
_CHECK_FILE = "granola-connection.json"
ACCOUNT_SOURCE = "claude_code_active_granola_account"

# Only these fixed labels are ever surfaced from the provider's scope list; anything else is
# counted, never echoed (scope entries are arbitrary provider strings).
SCOPE_LABELS = frozenset({"personal", "workspace", "shared", "team", "organization", "private", "public"})


def _scope_labels(scope: list[str] | str | None) -> tuple[list[str], int]:
    items = [scope] if isinstance(scope, str) else list(scope or [])
    known = sorted({item.strip().lower() for item in items if isinstance(item, str)} & SCOPE_LABELS)
    return known, len(items) - len(known)


WORKSPACE_NOTE = (
    "Meetings come from the Granola account/workspace currently active in your Claude Code sign-in. "
    "This pilot cannot verify workspace switches by a stable provider id; inspect and select meetings before import."
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,120}$")

Outcome = Literal[
    "imported", "already_known", "edited", "pending_content", "inaccessible", "failed_retryable", "rejected"
]


class GranolaRetrievalError(Exception):
    def __init__(self, status: int, detail: str, *, code: str) -> None:
        self.status = status
        self.detail = detail
        self.code = code
        super().__init__(detail)


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _AccountInfo(_Loose):
    """Only the documented, non-identifying field is read. No workspace/user identity is
    extracted or persisted: none of the observed shapes carries a documented stable id."""

    note_access_scope: list[str] | str | None = None


# Shape probe: structure only. Key names outside this allow-list are counted, never echoed;
# string values are reduced to a class + length bucket; numbers/bools to their type.
_SHAPE_KEYS = frozenset(
    {
        "id", "ids", "uuid", "slug", "name", "display_name", "title", "type", "kind", "status", "code", "error",
        "message", "detail", "count", "created_at", "updated_at", "url", "email", "user", "user_id", "account",
        "account_id", "accounts", "workspace", "workspace_id", "workspace_name", "workspaces", "organization",
        "organization_id", "org", "org_id", "team", "team_id", "tenant", "tenant_id", "plan", "role", "roles",
        "scope", "scopes", "note_access_scope", "access_scope", "access_notice", "folders", "folder_id",
        "settings", "features", "data", "result", "results", "items", "meta", "metadata", "current", "default",
        "primary", "active", "is_active", "is_default", "personal", "public",
    }
)
_SHAPE_MAX_DEPTH = 5
_SHAPE_MAX_KEYS = 40
_SHAPE_MAX_ITEMS = 3
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ERROR_ENVELOPE_KEYS = frozenset({"error", "message", "code", "detail", "status"})


def _string_class(text: str) -> str:
    if text == "":
        return "empty"
    if _UUID_RE.fullmatch(text):
        return "uuid"
    if "@" in text and "." in text.rsplit("@", 1)[-1] and " " not in text:
        return "email"
    if text.startswith(("http://", "https://")):
        return "url"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([T ].*)?", text):
        return "datetime"
    if re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", text):
        return "token"
    return "text"


def _length_bucket(length: int) -> str:
    for bound in (0, 16, 64, 256):
        if length <= bound:
            return f"<={bound}"
    return ">256"


def shape_of(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Bounded, value-free description of a parsed tool result (see `account_shape_report`)."""
    if depth >= _SHAPE_MAX_DEPTH:
        return {"kind": "truncated"}
    if value is None:
        return {"kind": "null"}
    if isinstance(value, bool):
        return {"kind": "bool"}
    if isinstance(value, int | float):
        return {"kind": "number"}
    if isinstance(value, str):
        return {"kind": "string", "class": _string_class(value), "length": _length_bucket(len(value))}
    if isinstance(value, list):
        out: dict[str, Any] = {"kind": "array", "length": len(value)}
        if value:
            out["items"] = [shape_of(item, depth=depth + 1) for item in value[:_SHAPE_MAX_ITEMS]]
        return out
    if isinstance(value, Mapping):
        keys = sorted(str(k) for k in value)
        listed = [k for k in keys if k in _SHAPE_KEYS][:_SHAPE_MAX_KEYS]
        out = {
            "kind": "object",
            "key_count": len(keys),
            "other_keys": len(keys) - len(listed),
            "keys": {k: shape_of(value[k], depth=depth + 1) for k in listed},
        }
        return out
    return {"kind": "other"}


def _id_paths(value: Any, prefix: str, depth: int = 0) -> list[str]:
    if depth >= _SHAPE_MAX_DEPTH:
        return []
    found: list[str] = []
    if isinstance(value, Mapping):
        for key in sorted(str(k) for k in value):
            if key not in _SHAPE_KEYS:
                continue
            path = f"{prefix}.{key}" if prefix else key
            item = value[key]
            if isinstance(item, str) and (_UUID_RE.fullmatch(item) or key in {"id", "workspace_id", "account_id"}):
                found.append(path)
            found.extend(_id_paths(item, path, depth + 1))
    elif isinstance(value, list):
        for index, item in enumerate(value[:_SHAPE_MAX_ITEMS]):
            found.extend(_id_paths(item, f"{prefix}[{index}]", depth + 1))
    return found[:20]


def account_shape_report(raw: Any) -> dict[str, Any]:
    """Safe description of a successful `get_account_info` result: structure, allow-listed key
    names, value classes and presence flags only. No values are copied."""
    top_keys = {str(k) for k in raw} if isinstance(raw, Mapping) else set()
    return {
        "top_kind": shape_of(raw)["kind"],
        "looks_like_error_envelope": bool(top_keys) and top_keys <= _ERROR_ENVELOPE_KEYS,
        "candidate_id_paths": _id_paths(raw, ""),
        "shape": shape_of(raw),
    }


class _ListedParticipant(_Loose):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class _ListedMeeting(_Loose):
    id: str = Field(pattern=GRANOLA_MCP_MEETING_ID.pattern)
    title: str | None = Field(default=None, max_length=500)
    date: str | None = Field(default=None, max_length=120)
    url: str | None = Field(default=None, max_length=2000)
    known_participants: list[_ListedParticipant] = Field(default_factory=list, max_length=500)
    captured_by_me: bool | None = None
    listed_as_participant: bool | None = None
    is_workspace_visible: bool | None = None


class _Transcript(_Loose):
    id: str | None = Field(default=None, pattern=GRANOLA_MCP_MEETING_ID.pattern)
    created_at: datetime | None = None
    transcript: str | None = None
    recording_context: dict[str, Any] | None = None


def _meeting_list(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, Mapping):
        for key in ("meetings", "results", "documents", "items"):
            if isinstance(raw.get(key), list):
                return list(raw[key])
        if isinstance(raw.get("id"), str):
            return [raw]
    raise GranolaRetrievalError(502, "Granola returned an unexpected listing shape.", code="unexpected_shape")


class GranolaRetrievalService:
    def __init__(
        self,
        *,
        transport: GranolaRetrievalTransport,
        adapter: GranolaSourceAdapter,
        ledger: EvidenceLedgerService,
        job_service: JobService,
    ) -> None:
        self._transport = transport
        self._adapter = adapter
        self._ledger = ledger
        self._jobs = job_service

    # ------------------------------------------------------------ describe

    def describe(self) -> dict[str, Any]:
        return {
            **self._transport.describe(),
            "trigger": "user_triggered_retrieval",
            "max_selection": MAX_SELECTION,
            "max_listed": MAX_LISTED,
            "connection_id": RETRIEVAL_CONNECTION_ID,
            "last_check": self._last_check(),
            **self._account_terms(),
        }

    # ---------------------------------------------------------- connection

    def _check_path(self) -> Path:
        return self._ledger._ledger_dir(create=True) / _CHECK_FILE

    def _last_check(self) -> dict[str, Any] | None:
        try:
            path = self._ledger._ledger_dir()
        except EvidenceLedgerError:
            return None
        path = path / _CHECK_FILE
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes()[:4096])
        except ValueError:
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("checked_at"), str):
            return None
        return {"checked_at": raw["checked_at"]}

    def _record_check(self) -> dict[str, Any]:
        marker = {"checked_at": datetime.now(UTC).isoformat()}
        path = self._check_path()
        mkdir_private(path.parent)
        atomic_write_private(path, json.dumps(marker, separators=(",", ":")).encode("utf-8"))
        return marker

    @staticmethod
    def _account_terms() -> dict[str, Any]:
        return {
            "account_source": ACCOUNT_SOURCE,
            "workspace_guarantee": False,
            "workspace_note": WORKSPACE_NOTE,
        }

    def connection(self) -> dict[str, Any]:
        """Persisted marker only; no provider call."""
        return {
            "transport": self._transport.describe(),
            "last_check": self._last_check(),
            "checked": False,
            "note": "Manual check only. Nothing is polled; this reflects the last explicit check you ran.",
            **self._account_terms(),
        }

    async def check_connection(self) -> dict[str, Any]:
        try:
            raw = await self._transport.call("get_account_info", {})
        except GranolaRelayError as exc:
            return self._relay_failure(exc)
        if not isinstance(raw, Mapping):
            raise GranolaRetrievalError(502, "Connection check returned an unexpected shape.", code="unexpected_shape")
        info = _AccountInfo.model_validate(raw)
        marker = self._record_check()
        labels, hidden = _scope_labels(info.note_access_scope)
        return {
            "checked": True,
            "connected": True,
            "note_access_scope": labels,
            "note_access_scope_hidden": hidden,
            "last_check": marker,
            "checked_at": marker["checked_at"],
            **self._account_terms(),
        }

    @staticmethod
    def _relay_failure(exc: GranolaRelayError) -> dict[str, Any]:
        return {
            "checked": True,
            "connected": False,
            "error_code": exc.code,
            "retryable": exc.retryable,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def _assert_checked(self) -> None:
        if self._last_check() is None:
            raise GranolaRetrievalError(409, "Run the connection check before listing meetings.", code="not_checked")

    async def _assert_connected(self) -> None:
        """Fresh `get_account_info`: auth/access failures fail closed. Proves reachability with the
        sign-in active right now, not which workspace that is."""
        raw = await self._transport.call("get_account_info", {})
        if not isinstance(raw, Mapping):
            raise GranolaRetrievalError(502, "Connection check returned an unexpected shape.", code="unexpected_shape")

    # -------------------------------------------------------------- listing

    def _known_by_object(self) -> dict[str, dict[str, Any]]:
        known: dict[str, dict[str, Any]] = {}
        offset: int | None = 0
        while offset is not None:
            page = self._ledger.list_sources(limit=200, offset=offset)
            for item in page["sources"]:
                if item["provider"] == "granola":
                    known.setdefault(item["provider_object_id"], item)
            offset = page["next_offset"]
        return known

    async def list_meetings(
        self,
        *,
        time_range: TimeRange,
        custom_start: date | None = None,
        custom_end: date | None = None,
        workspace_only: bool = False,
    ) -> dict[str, Any]:
        if time_range == "custom":
            if custom_start is None or custom_end is None or custom_end < custom_start:
                raise GranolaRetrievalError(400, "Custom range needs a start and end date.", code="invalid_range")
            if (custom_end - custom_start).days > 92:
                raise GranolaRetrievalError(400, "Custom range is limited to 92 days.", code="invalid_range")
        self._assert_checked()
        try:
            await self._assert_connected()
            arguments: dict[str, Any] = {"time_range": time_range}
            if time_range == "custom":
                arguments["custom_start"] = custom_start.isoformat()
                arguments["custom_end"] = custom_end.isoformat()
            if workspace_only:
                arguments["workspace_only"] = True
            raw = await self._transport.call("list_meetings", arguments)
        except GranolaRelayError as exc:
            raise GranolaRetrievalError(
                502 if exc.retryable else 409, "Granola listing failed.", code=exc.code
            ) from exc
        rows = _meeting_list(raw)
        known = self._known_by_object()
        meetings: list[dict[str, Any]] = []
        rejected = 0
        for row in rows[:MAX_LISTED]:
            try:
                item = _ListedMeeting.model_validate(row if isinstance(row, Mapping) else {})
            except ValidationError:
                rejected += 1
                continue
            source = known.get(item.id.lower())
            meetings.append({
                "meeting_id": item.id.lower(),
                "title": item.title,
                "date": item.date,
                "url": item.url,
                "participant_count": len(item.known_participants),
                "captured_by_me": item.captured_by_me,
                "listed_as_participant": item.listed_as_participant,
                "is_workspace_visible": item.is_workspace_visible,
                "ledger": None if source is None else {
                    "source_id": source["source_id"],
                    "latest_revision": source["latest_revision"],
                    "availability": source["availability"],
                    "processing_status": source["processing"]["status"],
                    "association_state": source["association"]["state"],
                    "last_observed_at": source["latest"]["observed_at"],
                },
            })
        return {
            "trigger": "user_triggered_retrieval",
            "unattended_discovery": False,
            **self._account_terms(),
            "listed_at": datetime.now(UTC).isoformat(),
            "time_range": time_range,
            "returned": len(rows),
            "shown": len(meetings),
            "truncated": len(rows) > MAX_LISTED,
            "rejected": rejected,
            "max_selection": MAX_SELECTION,
            "meetings": meetings,
        }

    # ------------------------------------------------------------ retrieval

    async def start_retrieval(self, meeting_ids: list[str]) -> dict[str, Any]:
        ids = []
        for raw in meeting_ids:
            if not GRANOLA_MCP_MEETING_ID.match(raw):
                raise GranolaRetrievalError(400, "Meeting ids must be Granola UUIDs.", code="invalid_meeting_id")
            lowered = raw.lower()
            if lowered not in ids:
                ids.append(lowered)
        if not ids:
            raise GranolaRetrievalError(400, "Select at least one meeting.", code="empty_selection")
        if len(ids) > MAX_SELECTION:
            raise GranolaRetrievalError(
                400, f"Select at most {MAX_SELECTION} meetings per retrieval.", code="selection_too_large"
            )
        if self._last_check() is None:
            raise GranolaRetrievalError(409, "Run the connection check before retrieving.", code="not_checked")
        for job in self._jobs.jobs.values():
            if job.get("kind") == RETRIEVAL_JOB_KIND and job.get("status") == "running":
                raise GranolaRetrievalError(409, "A retrieval is already running.", code="retrieval_in_progress")

        async def runner(job_id: str) -> dict[str, Any]:
            return await self._run(ids)

        job_id, persist_warn = await self._jobs.launch_managed(
            kind=RETRIEVAL_JOB_KIND,
            account="",
            opp_slug="",
            opportunity="",
            sig=None,
            safe_metadata={
                "trigger": "user_triggered_retrieval",
                "unattended_discovery": False,
                "meeting_ids": ids,
                "requested": len(ids),
            },
            runner=runner,
        )
        return {"job_id": job_id, "status": "running", "requested": len(ids), "persist_warn": persist_warn}

    def job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get_job(job_id)
        if job is None or job.get("kind") != RETRIEVAL_JOB_KIND:
            return None
        return {"job_id": job_id, **{key: value for key, value in job.items() if key != "sig"}}

    async def _run(self, ids: list[str]) -> dict[str, Any]:
        try:
            await self._assert_connected()
        except GranolaRelayError as exc:
            raise ManagedJobError(exc.code, "Granola connection check failed before retrieval; nothing was imported.")
        except GranolaRetrievalError as exc:
            raise ManagedJobError(exc.code, exc.detail)

        outcomes: dict[str, dict[str, Any]] = {}
        details: dict[str, Mapping[str, Any]] = {}
        unrequested = 0
        try:
            raw = await self._transport.call("get_meetings", {"meeting_ids": ids})
            for row in _meeting_list(raw):
                if not (isinstance(row, Mapping) and isinstance(row.get("id"), str)):
                    unrequested += 1
                    continue
                row_id = row["id"].lower()
                if row_id not in ids or row_id in details:
                    unrequested += 1  # not selected by the user (or a duplicate row): never imported
                    continue
                details[row_id] = row
        except GranolaRelayError as exc:
            if exc.retryable:
                for meeting_id in ids:
                    outcomes[meeting_id] = {"outcome": "failed_retryable", "error_code": exc.code}
                return self._summary(ids, outcomes, None, unrequested)
            code = exc.code
        except GranolaRetrievalError as exc:
            code = exc.code
        else:
            code = None

        meetings: list[NormalizedMeeting] = []
        order: list[str] = []
        for meeting_id in ids:
            if code is not None:
                payload: dict[str, Any] = {"id": meeting_id, "error_code": _error_code_for(code)}
            elif meeting_id not in details:
                payload = {"id": meeting_id, "error_code": "NOT_FOUND"}
            else:
                payload = _pick(details[meeting_id])
                try:
                    transcript_raw = await self._transport.call("get_meeting_transcript", {"meeting_id": meeting_id})
                    transcript = _Transcript.model_validate(
                        transcript_raw if isinstance(transcript_raw, Mapping) else {}
                    )
                    if transcript.id is not None and transcript.id.lower() != meeting_id:
                        transcript = _Transcript()  # mismatched meeting: discard, leave transcript pending
                    if transcript.transcript:
                        payload["transcript"] = transcript.transcript
                    if transcript.created_at is not None:
                        payload["created_at"] = transcript.created_at.isoformat()
                    if transcript.recording_context is not None:
                        payload["recording_context"] = transcript.recording_context
                except GranolaRelayError as exc:
                    if exc.retryable:
                        outcomes[meeting_id] = {"outcome": "failed_retryable", "error_code": exc.code}
                        continue
                    if exc.code in {"tool_access_denied", "tool_auth_required"}:
                        payload = {"id": meeting_id, "error_code": "UNAUTHORIZED"}
                    # tool_not_found / tool_error: metadata + summary only; transcript pending.
                except ValidationError:
                    pass
            try:
                meetings.append(self._adapter.normalize(payload, connection_id=RETRIEVAL_CONNECTION_ID))
                order.append(meeting_id)
            except GranolaImportError as exc:
                outcomes[meeting_id] = {"outcome": "rejected", "error_code": exc.code}

        results: list[dict[str, Any]] = []
        import_id: str | None = None
        if meetings:
            try:
                recorded = self._ledger.import_meetings(meetings, trigger="user_triggered_retrieval")
            except EvidenceLedgerError as exc:
                raise ManagedJobError(exc.code, "The ledger rejected the retrieval; nothing was imported.")
            results = recorded["results"]
            import_id = recorded["import_id"]
            for meeting_id, result in zip(order, results):
                outcomes[meeting_id] = _classify(result)
        return self._summary(ids, outcomes, import_id, unrequested)

    @classmethod
    def _summary(
        cls, ids: list[str], outcomes: dict[str, dict[str, Any]], import_id: str | None, unrequested: int
    ) -> dict[str, Any]:
        fallback = {"outcome": "failed_retryable", "error_code": "relay_no_tool_result"}
        rows = [{"meeting_id": meeting_id, **outcomes.get(meeting_id, fallback)} for meeting_id in ids]
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
        return {
            "trigger": "user_triggered_retrieval",
            "unattended_discovery": False,
            **cls._account_terms(),
            "import_id": import_id,
            "unrequested_dropped": unrequested,
            "counts": counts,
            "results": rows,
        }


_PICK_KEYS = ("id", "title", "date", "url", "known_participants", "summary", "created_at")


def _pick(row: Mapping[str, Any]) -> dict[str, Any]:
    payload = {key: row[key] for key in _PICK_KEYS if key in row}
    payload["id"] = str(payload["id"]).lower()
    participants = payload.get("known_participants")
    if isinstance(participants, list):
        payload["known_participants"] = [
            {key: person.get(key) for key in ("name", "email") if isinstance(person, Mapping) and person.get(key) is not None}
            for person in participants
            if isinstance(person, Mapping)
        ]
    return payload


def _error_code_for(relay_code: str) -> str:
    if relay_code in {"tool_access_denied", "tool_auth_required"}:
        return "UNAUTHORIZED"
    return "NOT_FOUND"


def _classify(result: Mapping[str, Any]) -> dict[str, Any]:
    availability = result.get("availability")
    base = {
        "source_id": result.get("source_id"),
        "revision": result.get("revision"),
        "change": result.get("change"),
        "availability": availability,
        "processing_status": result.get("processing_status"),
        "association_state": result.get("association_state"),
    }
    if availability == "access_lost":
        return {"outcome": "inaccessible", **base}
    if availability in {"pending_unknown", "metadata_only"}:
        return {"outcome": "pending_content", **base}
    if not result.get("created_revision"):
        return {"outcome": "already_known", **base}
    if result.get("revision", 1) > 1:
        return {"outcome": "edited", **base}
    return {"outcome": "imported", **base}
