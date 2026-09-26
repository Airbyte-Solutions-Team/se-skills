"""Gmail transport boundary for the Command Center pilot (PR E).

Status: nothing here holds an OAuth client, refresh token, or hosted credential, and
nothing scans a mailbox in the background. The module defines the read-only boundary
every transport must satisfy and ships two implementations; the explicitly enabled
local HTTP transport lives in `integrations/gmail_live.py` and is selected only by
`SE_GMAIL_TRANSPORT=live_readonly` (see `webapp/app.py`):

* `UnavailableGmailTransport` — the production default. It describes itself as
  unauthorized and fails every call closed, so the UI can say plainly that live
  retrieval is not available yet.
* `SyntheticGmailTransport` — an in-memory mailbox loaded from a synthetic
  fixture (`eval/fixtures/command_center/gmail/`). Tests and the local UI review
  run against it. It exposes deterministic knobs (`revoke`, `fail_next`,
  `edit_body`, `remove`) so revocation, retry, and edited-message paths can be
  exercised without touching a real account.

Payload contract (`gmail-message-v1`): the *normalized* shape a live transport
would build from `users.messages.get(format=full)` — headers already extracted,
`text/plain` body already decoded, attachments reduced to a count. Attachments
are never fetched; a transport that passed attachment content would be rejected
by the strict model (`extra="forbid"`).

A transport may also return `{"id", "thread_id", "error_code": "MALFORMED"}` for a
message whose MIME it could not decode; the adapter rejects it (`malformed_mime`)
rather than importing a guessed body.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from command_center_evidence import (
    NormalizedEmailContent,
    NormalizedEmailMessage,
    NormalizedEmailParticipant,
    SourceIdentity,
    canonical_bytes,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

MESSAGE_CONTRACT = "gmail-message-v1"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_ID = re.compile(r"^[0-9a-f]{8,32}$")
MAX_MESSAGE_BYTES = 1_000_000
MAX_MESSAGES_PER_GET = 25
MAX_THREADS_PER_LIST = 100
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "eval" / "fixtures" / "command_center" / "gmail"

TransportMode = Literal["unavailable", "synthetic_fixture", "live_readonly"]

_QUOTE_HEADER = re.compile(r"^On .{3,200} wrote:\s*$")
_FORWARD_MARKER = re.compile(r"^-{2,}\s*(Forwarded message|Original Message)\s*-{2,}\s*$", re.IGNORECASE)
_SIGNATURE_MARKER = re.compile(r"^--\s*$")
_ADDRESS = re.compile(r"^(?:\"?([^\"<]*)\"?\s*)?<?([A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})>?$")


class GmailTransportError(Exception):
    """Raised by transports. `retryable` drives job outcome classification."""

    def __init__(self, code: str, detail: str, *, retryable: bool) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.retryable = retryable


class GmailImportError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


class GmailTransportDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["gmail"] = "gmail"
    mode: TransportMode
    live_retrieval_available: bool
    readonly: Literal[True] = True
    unattended_discovery: Literal[False] = False
    requires_credentials: bool
    hosted_credentials: Literal[False] = False
    attachments: Literal["never_fetched"] = "never_fetched"
    label: str
    payload_contracts: list[str]


class GmailReadOnlyTransport(Protocol):
    """Boundary every Gmail transport implements. Read-only by construction: there is
    no method that could modify, label, send, or delete mail."""

    def describe(self) -> GmailTransportDescription: ...

    async def check_access(self) -> Mapping[str, Any]:
        """Return `{"email_address": str, "scopes": [str, ...]}` for the signed-in mailbox.
        Must raise `GmailTransportError` when unauthorized or revoked."""
        ...

    async def list_threads(
        self, *, after: date, before: date, participants: Sequence[str], max_results: int
    ) -> Sequence[Mapping[str, Any]]:
        """Bounded, metadata-only thread rows: id, subject, message_count, last_message_at,
        participants (addresses only). Never bodies, never snippets."""
        ...

    async def get_messages(self, message_ids: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        """Full `gmail-message-v1` rows for explicitly selected ids only."""
        ...


# --- Payload contract ---------------------------------------------------------

class _Doc(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GmailAddress(_Doc):
    name: str | None = Field(default=None, max_length=200)
    email: str = Field(min_length=3, max_length=320)


class GmailMessage(_Doc):
    """`gmail-message-v1`. `body_text` is None when the message has no text part."""

    id: str = Field(pattern=GMAIL_ID.pattern)
    thread_id: str = Field(pattern=GMAIL_ID.pattern)
    subject: str | None = Field(default=None, max_length=500)
    from_: GmailAddress = Field(alias="from")
    to: list[GmailAddress] = Field(default_factory=list, max_length=500)
    cc: list[GmailAddress] = Field(default_factory=list, max_length=500)
    date: datetime
    internal_date: datetime | None = None
    message_id_header: str | None = Field(default=None, max_length=998)
    in_reply_to: str | None = Field(default=None, max_length=998)
    references: list[str] = Field(default_factory=list, max_length=500)
    body_text: str | None = Field(default=None, max_length=400_000)
    attachment_count: int = Field(default=0, ge=0, le=10_000)
    web_url: str | None = Field(default=None, max_length=1000)

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


class GmailUnavailableMessage(_Doc):
    id: str = Field(pattern=GMAIL_ID.pattern)
    thread_id: str | None = Field(default=None, pattern=GMAIL_ID.pattern)
    error_code: Literal["NOT_FOUND", "UNAUTHORIZED"]


class GmailThreadRow(_Doc):
    id: str = Field(pattern=GMAIL_ID.pattern)
    subject: str | None = Field(default=None, max_length=500)
    message_count: int = Field(ge=1, le=10_000)
    last_message_at: datetime
    participants: list[str] = Field(default_factory=list, max_length=1000)
    message_ids: list[str] = Field(default_factory=list, max_length=10_000)


def parse_address(raw: str) -> GmailAddress | None:
    match = _ADDRESS.match(raw.strip())
    if not match:
        return None
    name, email = match.group(1), match.group(2).lower()
    return GmailAddress(name=(name or "").strip().strip('"') or None, email=email)


def domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1].lower()


def strip_quoted_reply(body: str) -> tuple[str, bool]:
    """Drop quoted earlier messages and a trailing signature so a reply's own words are
    what gets hashed and analyzed. Conservative: only well-known markers are honored."""
    lines = body.replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    removed = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(">") or _QUOTE_HEADER.match(stripped) or _FORWARD_MARKER.match(stripped):
            removed = True
            break
        if _SIGNATURE_MARKER.match(stripped):
            removed = True
            break
        kept.append(line)
    text = "\n".join(kept).strip()
    return text, removed


# --- Adapter -------------------------------------------------------------------

def _payload_size_ok(payload: Mapping[str, Any]) -> bool:
    return len(canonical_bytes(dict(payload))) <= MAX_MESSAGE_BYTES


class GmailSourceAdapter:
    """Normalizes `gmail-message-v1` rows into ledger sources. No network, no credentials."""

    def describe(self) -> dict[str, Any]:
        return {
            "provider": "gmail",
            "transport": "gmail_readonly",
            "mode": "user_selected_messages",
            "unattended_discovery": False,
            "requires_credentials": True,
            "label": "Gmail read-only intake (user-selected messages)",
            "payload_contracts": [MESSAGE_CONTRACT],
        }

    def normalize(self, payload: Mapping[str, Any], *, connection_id: str) -> NormalizedEmailMessage:
        if not isinstance(payload, Mapping):
            raise GmailImportError(400, "Each message must be a JSON object.", code="malformed_payload")
        if not _payload_size_ok(payload):
            raise GmailImportError(413, "Message payload exceeds the import bound.", code="payload_too_large")
        try:
            if payload.get("error_code") == "MALFORMED":
                raise GmailImportError(422, "Message MIME could not be decoded.", code="malformed_mime")
            if "error_code" in payload:
                return self._normalize_unavailable(payload, connection_id=connection_id)
            return self._normalize_message(payload, connection_id=connection_id)
        except ValidationError as exc:
            raise GmailImportError(
                400, "Message payload does not match the gmail-message-v1 shape.", code="malformed_payload"
            ) from exc

    @staticmethod
    def _identity(message_id: str, connection_id: str) -> SourceIdentity:
        return SourceIdentity(
            provider="gmail",
            connection_id=connection_id,
            source_workspace_id=None,
            provider_object_id=message_id.lower(),
            kind="email_message",
        )

    @classmethod
    def _normalize_message(cls, payload: Mapping[str, Any], *, connection_id: str) -> NormalizedEmailMessage:
        message = GmailMessage.model_validate(dict(payload))
        body_text, removed = strip_quoted_reply(message.body_text or "")
        if body_text:
            availability = "content_available"
            reason = None
            content = NormalizedEmailContent(body_text=body_text, quoted_text_removed=removed)
        else:
            availability = "metadata_only"
            reason = "quoted_only" if removed else "no_body"
            content = NormalizedEmailContent()
        attendees = [NormalizedEmailParticipant(role="from", name=message.from_.name, email=message.from_.email.lower())]
        attendees.extend(NormalizedEmailParticipant(role="to", name=p.name, email=p.email.lower()) for p in message.to)
        attendees.extend(NormalizedEmailParticipant(role="cc", name=p.name, email=p.email.lower()) for p in message.cc)
        return NormalizedEmailMessage(
            identity=cls._identity(message.id, connection_id),
            thread_id=message.thread_id.lower(),
            title=message.subject or None,
            occurred_at=message.date,
            provider_updated_at=message.internal_date,
            attendees=attendees,
            message_id_header=message.message_id_header,
            in_reply_to=message.in_reply_to,
            reply_depth=len(message.references),
            attachment_count=message.attachment_count,
            availability=availability,
            unavailable_reason=reason,
            content=content,
            provider_web_url=message.web_url,
        )

    @classmethod
    def _normalize_unavailable(cls, payload: Mapping[str, Any], *, connection_id: str) -> NormalizedEmailMessage:
        row = GmailUnavailableMessage.model_validate(dict(payload))
        availability = {"NOT_FOUND": "pending_unknown", "UNAUTHORIZED": "access_lost"}[row.error_code]
        return NormalizedEmailMessage(
            identity=cls._identity(row.id, connection_id),
            thread_id=(row.thread_id or row.id).lower(),
            availability=availability,
            unavailable_reason=f"provider reported {row.error_code}",
        )


# --- Transports ----------------------------------------------------------------

class UnavailableGmailTransport:
    """Production default until a read-only route is authorized: every call fails closed."""

    def describe(self) -> GmailTransportDescription:
        return GmailTransportDescription(
            mode="unavailable",
            live_retrieval_available=False,
            requires_credentials=True,
            label="Gmail (no authorized read-only route configured)",
            payload_contracts=[MESSAGE_CONTRACT],
        )

    @staticmethod
    def _refuse() -> GmailTransportError:
        return GmailTransportError(
            "transport_unavailable",
            "No authorized read-only Gmail route is configured on this machine.",
            retryable=False,
        )

    async def check_access(self) -> Mapping[str, Any]:
        raise self._refuse()

    async def list_threads(
        self, *, after: date, before: date, participants: Sequence[str], max_results: int
    ) -> Sequence[Mapping[str, Any]]:
        raise self._refuse()

    async def get_messages(self, message_ids: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        raise self._refuse()


class SyntheticGmailTransport:
    """In-memory synthetic mailbox. Deterministic; carries no real mail."""

    def __init__(self, *, mailbox: str, messages: Sequence[Mapping[str, Any]] = ()) -> None:
        self.mailbox = mailbox.lower()
        self.scopes: list[str] = [READONLY_SCOPE]
        self.messages: dict[str, dict[str, Any]] = {}
        self.revoked = False
        self.calls: list[str] = []
        self._fail_next: list[GmailTransportError] = []
        for row in messages:
            self.add(row)

    @classmethod
    def from_fixture(cls, path: Path | None = None) -> SyntheticGmailTransport:
        raw = json.loads((path or FIXTURES_DIR / "mailbox_synthetic.json").read_text(encoding="utf-8"))
        return cls(mailbox=raw["mailbox"], messages=raw["messages"])

    # knobs ------------------------------------------------------------------

    def add(self, row: Mapping[str, Any]) -> None:
        message = GmailMessage.model_validate(dict(row))
        self.messages[message.id.lower()] = dict(row)

    def edit_body(self, message_id: str, body_text: str | None) -> None:
        self.messages[message_id.lower()]["body_text"] = body_text

    def remove(self, message_id: str) -> None:
        self.messages.pop(message_id.lower(), None)

    def revoke(self) -> None:
        self.revoked = True

    def restore(self) -> None:
        self.revoked = False

    def fail_next(self, code: str = "transient", *, retryable: bool = True) -> None:
        self._fail_next.append(GmailTransportError(code, f"synthetic {code}", retryable=retryable))

    # protocol -----------------------------------------------------------------

    def describe(self) -> GmailTransportDescription:
        return GmailTransportDescription(
            mode="synthetic_fixture",
            live_retrieval_available=False,
            requires_credentials=False,
            label="Synthetic Gmail fixture (no real mailbox)",
            payload_contracts=[MESSAGE_CONTRACT],
        )

    def _gate(self, call: str) -> None:
        self.calls.append(call)
        if self._fail_next:
            raise self._fail_next.pop(0)
        if self.revoked:
            raise GmailTransportError("access_revoked", "Gmail authorization was revoked.", retryable=False)

    async def check_access(self) -> Mapping[str, Any]:
        self._gate("check_access")
        return {"email_address": self.mailbox, "scopes": list(self.scopes)}

    async def list_threads(
        self, *, after: date, before: date, participants: Sequence[str], max_results: int
    ) -> Sequence[Mapping[str, Any]]:
        self._gate("list_threads")
        wanted = {p.lower() for p in participants}
        threads: dict[str, dict[str, Any]] = {}
        for raw in self.messages.values():
            message = GmailMessage.model_validate(raw)
            when = message.date.astimezone(UTC)
            if not (after <= when.date() <= before):
                continue
            addresses = {message.from_.email.lower(), *(p.email.lower() for p in [*message.to, *message.cc])}
            if wanted and not any(a in wanted or domain_of(a) in wanted for a in addresses):
                continue
            row = threads.setdefault(message.thread_id.lower(), {
                "id": message.thread_id.lower(), "subject": message.subject, "message_count": 0,
                "last_message_at": when, "participants": set(), "message_ids": [],
            })
            row["message_count"] += 1
            row["last_message_at"] = max(row["last_message_at"], when)
            row["participants"].update(addresses)
            row["message_ids"].append(message.id.lower())
        rows = []
        for row in sorted(threads.values(), key=lambda r: r["last_message_at"], reverse=True)[:max_results]:
            row["participants"] = sorted(row["participants"])
            row["last_message_at"] = row["last_message_at"].isoformat()
            rows.append(row)
        return rows

    async def get_messages(self, message_ids: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        self._gate("get_messages")
        rows: list[dict[str, Any]] = []
        for message_id in message_ids[:MAX_MESSAGES_PER_GET]:
            raw = self.messages.get(message_id.lower())
            rows.append(dict(raw) if raw is not None else {"id": message_id.lower(), "error_code": "NOT_FOUND"})
        return rows

