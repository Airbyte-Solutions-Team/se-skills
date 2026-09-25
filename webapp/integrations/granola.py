"""Granola source adapter boundary for the Command Center pilot.

Only one adapter exists: `ManualGranolaImportAdapter`, which normalizes note
payloads the signed-in user pastes or uploads. It performs no network calls,
holds no credentials, and cannot discover meetings on its own. A live adapter
(MCP or REST) implements the same `GranolaSourceAdapter` protocol once its
authentication and behavior have been verified against a test workspace.

Two payload contracts are accepted, chosen by the shape of `id`:

* `granola-rest-note-v1` — the documented REST `Note` object
  (`GET /v1/notes/{note_id}?include=transcript`): `id` matching `not_` + 14
  alphanumerics, `title`, `owner`, `created_at`, `updated_at`, `web_url`,
  `attendees`, `summary_text`, `summary_markdown`, `private_notes_text`, and
  `transcript` items with `speaker.source`, optional `attribution`,
  `diarization_label`, `name`, and `text`.
* `granola-mcp-meeting-v1` — the shape observed on 2026-09-24 from the
  per-user OAuth MCP server, merging `get_meetings` (`id` UUID, `title`,
  `date` display string, `url`, `known_participants`, `summary`) with
  `get_meeting_transcript` (`created_at` ISO timestamp, `transcript` as one
  string, `recording_context`). MCP exposes no `updated_at` or revision field,
  so edits are only detectable by content hash on a later user-triggered
  re-import; that is recorded as `provider_updated_at = None`.

Fields outside either shape are rejected so an unexpected provider change
fails loudly.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from command_center_evidence import (
    NormalizedMeeting,
    NormalizedMeetingContent,
    NormalizedParticipant,
    NormalizedTranscriptSegment,
    SourceIdentity,
    canonical_bytes,
)


GRANOLA_NOTE_ID = re.compile(r"^not_[a-zA-Z0-9]{14}$")
GRANOLA_MCP_MEETING_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
GRANOLA_UNAVAILABLE_ID = re.compile(f"(?:{GRANOLA_NOTE_ID.pattern})|(?:{GRANOLA_MCP_MEETING_ID.pattern})")
REST_CONTRACT = "granola-rest-note-v1"
MCP_CONTRACT = "granola-mcp-meeting-v1"
MAX_NOTE_BYTES = 2_000_000
MAX_NOTES_PER_IMPORT = 25

AdapterMode = Literal["manual_import"]


class AdapterDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["granola"] = "granola"
    transport: Literal["manual_import"]
    mode: AdapterMode
    unattended_discovery: bool
    requires_credentials: bool
    label: str
    payload_contracts: list[str]


class GranolaImportError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str) -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(detail)


class GranolaSourceAdapter(Protocol):
    """Boundary every Granola transport implements."""

    def describe(self) -> AdapterDescription: ...

    def normalize(self, payload: Mapping[str, Any], *, connection_id: str) -> NormalizedMeeting: ...


# --- Documented REST payload -------------------------------------------------

class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GranolaUser(_Doc):
    name: str | None = Field(default=None, max_length=200)
    email: str = Field(max_length=320)


class GranolaFolder(_Doc):
    id: str = Field(pattern=r"^fol_[a-zA-Z0-9]{14}$")
    object: Literal["folder"] = "folder"
    name: str | None = Field(default=None, max_length=300)
    parent_folder_id: str | None = Field(default=None, pattern=r"^fol_[a-zA-Z0-9]{14}$")


class GranolaCalendarEvent(_Doc):
    model_config = ConfigDict(extra="allow")


class GranolaSpeaker(_Doc):
    source: Literal["microphone", "speaker"]
    attribution: Literal["me", "them"] | None = None
    diarization_label: str | None = Field(default=None, max_length=120)
    name: str | None = Field(default=None, max_length=200)


class GranolaTranscriptItem(_Doc):
    speaker: GranolaSpeaker
    text: str = Field(min_length=1, max_length=20_000)
    start_time: datetime | None = None
    end_time: datetime | None = None


class GranolaNote(_Doc):
    id: str = Field(pattern=GRANOLA_NOTE_ID.pattern)
    object: Literal["note"] = "note"
    title: str | None = Field(default=None, max_length=500)
    owner: GranolaUser | None = None
    created_at: datetime
    updated_at: datetime
    web_url: str | None = Field(default=None, max_length=1000)
    calendar_event: GranolaCalendarEvent | None = None
    attendees: list[GranolaUser] = Field(default_factory=list, max_length=1000)
    folder_membership: list[GranolaFolder] = Field(default_factory=list, max_length=200)
    summary_text: str | None = Field(default=None, max_length=200_000)
    summary_markdown: str | None = Field(default=None, max_length=400_000)
    private_notes_text: str | None = Field(default=None, max_length=200_000)
    private_notes_markdown: str | None = Field(default=None, max_length=400_000)
    transcript: list[GranolaTranscriptItem] | None = Field(default=None, max_length=20_000)


class GranolaUnavailableNote(_Doc):
    """Metadata-only import when the transcript/summary could not be retrieved.

    Mirrors the documented failure shapes: `413 TRANSCRIPT_TOO_LARGE`, `404`
    (`NOT_FOUND`), `401` when access is lost, and `TRANSCRIPT_DELETED` after
    transcript auto-deletion. Granola returns 404 both for deleted notes and
    for notes that are still processing / were never summarized, so
    `NOT_FOUND` maps to `pending_unknown`, never to `deleted`; deletion has to
    be established separately.
    """

    id: str = Field(pattern=GRANOLA_UNAVAILABLE_ID.pattern)
    title: str | None = Field(default=None, max_length=500)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    attendees: list[GranolaUser] = Field(default_factory=list, max_length=1000)
    error_code: Literal["TRANSCRIPT_TOO_LARGE", "NOT_FOUND", "UNAUTHORIZED", "TRANSCRIPT_DELETED"]


# --- Observed MCP payload (per-user OAuth; see module docstring) --------------

class GranolaMcpParticipant(_Doc):
    name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=320)


class GranolaMcpRecordingContext(_Doc):
    model_config = ConfigDict(extra="allow")


class GranolaMcpMeeting(_Doc):
    id: str = Field(pattern=GRANOLA_MCP_MEETING_ID.pattern)
    title: str | None = Field(default=None, max_length=500)
    date: str | None = Field(default=None, max_length=120)
    created_at: datetime | None = None
    url: str | None = Field(default=None, max_length=1000)
    known_participants: list[GranolaMcpParticipant] = Field(default_factory=list, max_length=1000)
    summary: str | None = Field(default=None, max_length=200_000)
    transcript: str | None = Field(default=None, max_length=2_000_000)
    recording_context: GranolaMcpRecordingContext | None = None


def _payload_size_ok(payload: Mapping[str, Any]) -> bool:
    try:
        return len(canonical_bytes(dict(payload))) <= MAX_NOTE_BYTES
    except (TypeError, ValueError):
        return False


def _chunk(text: str, size: int = 20_000) -> list[str]:
    return [text[start:start + size] for start in range(0, len(text), size)]


class ManualGranolaImportAdapter:
    """User-selected, manually triggered import of documented Granola payloads."""

    def describe(self) -> AdapterDescription:
        return AdapterDescription(
            transport="manual_import",
            mode="manual_import",
            unattended_discovery=False,
            requires_credentials=False,
            label="Manual Granola import (user-selected notes)",
            payload_contracts=[REST_CONTRACT, MCP_CONTRACT],
        )

    def normalize(self, payload: Mapping[str, Any], *, connection_id: str) -> NormalizedMeeting:
        if not isinstance(payload, Mapping):
            raise GranolaImportError(400, "Each note must be a JSON object.", code="malformed_payload")
        if not _payload_size_ok(payload):
            raise GranolaImportError(413, "Note payload exceeds the import bound.", code="payload_too_large")
        try:
            if "error_code" in payload:
                return self._normalize_unavailable(payload, connection_id=connection_id)
            if isinstance(payload.get("id"), str) and GRANOLA_MCP_MEETING_ID.match(payload["id"]):
                return self._normalize_mcp_meeting(payload, connection_id=connection_id)
            return self._normalize_note(payload, connection_id=connection_id)
        except ValidationError as exc:
            raise GranolaImportError(
                400, "Note payload does not match the documented Granola note shape.", code="malformed_payload"
            ) from exc

    @staticmethod
    def _normalize_mcp_meeting(payload: Mapping[str, Any], *, connection_id: str) -> NormalizedMeeting:
        meeting = GranolaMcpMeeting.model_validate(dict(payload))
        transcript_text = (meeting.transcript or "").strip()
        content = NormalizedMeetingContent(
            summary_text=meeting.summary or None,
            transcript=[NormalizedTranscriptSegment(text=chunk) for chunk in _chunk(transcript_text)],
        )
        has_content = bool(content.summary_text or content.transcript)
        return NormalizedMeeting(
            identity=SourceIdentity(
                provider="granola",
                connection_id=connection_id,
                source_workspace_id=None,
                provider_object_id=meeting.id.lower(),
                kind="meeting",
            ),
            title=meeting.title,
            occurred_at=meeting.created_at,
            provider_updated_at=None,
            attendees=[
                NormalizedParticipant(name=person.name, email=person.email)
                for person in meeting.known_participants
            ],
            availability="content_available" if has_content else "metadata_only",
            unavailable_reason=None if has_content else "meeting_has_no_summary_or_transcript",
            content=content,
            provider_web_url=meeting.url,
        )

    @staticmethod
    def _normalize_note(payload: Mapping[str, Any], *, connection_id: str) -> NormalizedMeeting:
        note = GranolaNote.model_validate(dict(payload))
        content = NormalizedMeetingContent(
            summary_text=note.summary_text or None,
            summary_markdown=note.summary_markdown or None,
            private_notes_text=note.private_notes_text or None,
            private_notes_markdown=note.private_notes_markdown or None,
            transcript=[
                NormalizedTranscriptSegment(
                    speaker_source=item.speaker.source,
                    attribution=item.speaker.attribution,
                    speaker_label=item.speaker.name or item.speaker.diarization_label,
                    text=item.text,
                    start_time=item.start_time,
                    end_time=item.end_time,
                )
                for item in (note.transcript or [])
            ],
        )
        has_content = not content.is_empty()
        return NormalizedMeeting(
            identity=SourceIdentity(
                provider="granola",
                connection_id=connection_id,
                source_workspace_id=None,
                provider_object_id=note.id,
                kind="meeting",
            ),
            title=note.title,
            occurred_at=note.created_at,
            provider_updated_at=note.updated_at,
            attendees=[NormalizedParticipant(name=user.name, email=user.email) for user in note.attendees],
            availability="content_available" if has_content else "metadata_only",
            unavailable_reason=None if has_content else "note_has_no_summary_or_transcript",
            content=content,
            provider_web_url=note.web_url,
        )

    @staticmethod
    def _normalize_unavailable(payload: Mapping[str, Any], *, connection_id: str) -> NormalizedMeeting:
        note = GranolaUnavailableNote.model_validate(dict(payload))
        availability = {
            "TRANSCRIPT_TOO_LARGE": "metadata_only",
            "NOT_FOUND": "pending_unknown",
            "UNAUTHORIZED": "access_lost",
            "TRANSCRIPT_DELETED": "metadata_only",
        }[note.error_code]
        return NormalizedMeeting(
            identity=SourceIdentity(
                provider="granola",
                connection_id=connection_id,
                source_workspace_id=None,
                provider_object_id=note.id,
                kind="meeting",
            ),
            title=note.title,
            occurred_at=note.created_at,
            provider_updated_at=note.updated_at,
            attendees=[NormalizedParticipant(name=user.name, email=user.email) for user in note.attendees],
            availability=availability,
            unavailable_reason=note.error_code.lower(),
        )
