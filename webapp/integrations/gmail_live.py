"""Local, explicitly enabled Gmail read-only HTTP transport (Command Center PR E.1).

`LiveGmailReadOnlyTransport` implements `GmailReadOnlyTransport` against the Gmail
REST API with exactly one scope, `gmail.readonly`. It is **never** selected by
default: `webapp/app.py` builds it only when `SE_GMAIL_TRANSPORT=live_readonly` is
set, and even then every call fails closed until the user has run
`scripts/gmail_local_authorize.py` in their own browser session, which writes an
OAuth refresh token to a 0600 file in the local user context
(`~/.config/se-skills/gmail-oauth.json` unless `SE_GMAIL_OAUTH_FILE` says otherwise).

Boundary, enforced here before any provider request:

* `check_access` — one `tokeninfo` lookup (granted scopes) plus one
  `users.getProfile` (mailbox address). The intake service binds both and re-checks
  them before every listing, retrieval and import; a re-authorization with a different
  account or a broader grant therefore fails closed there.
* `list_threads` — refuses an empty participant bound, more than `MAX_LIST_TERMS`
  terms, malformed terms, an inverted range or one longer than `MAX_LIST_DAYS`; caps
  `max_results` at `MAX_LIST_RESULTS` and pages at `MAX_LIST_PAGES`. Listing uses
  `users.threads.list` + `users.threads.get?format=metadata` with an allow-listed
  header set — the API's `snippet` fields are discarded, never returned. Threads whose
  participants match none of the bound are dropped (the query is only a hint).
* `get_messages` — refuses more than `MAX_MESSAGES_PER_GET` ids or a malformed id;
  fetches `users.messages.get?format=full` for exactly those ids; decodes only
  `text/plain` (fallback: tag-stripped `text/html`) parts; counts attachment parts and
  never requests `attachments.get`. A message whose MIME cannot be decoded is returned
  as `{"id", "thread_id", "error_code": "MALFORMED"}`, which the adapter rejects.

Failure model (`GmailTransportError.code`): `credentials_missing` / `not_authorized`
(fail closed, not retryable), `access_revoked` (refresh token rejected — the user must
re-authorize), `rate_limited` and `provider_unavailable` (retryable), `malformed_response`
(not retryable). Error details never carry tokens, response bodies, addresses or
subjects. Nothing here polls, caches mail, writes to the mailbox, calls a model, or
shares anything beyond this process.
"""
from __future__ import annotations

import base64
import binascii
import html
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from integrations.gmail import (
    GMAIL_ID,
    MAX_MESSAGE_BYTES,
    MAX_MESSAGES_PER_GET,
    MAX_THREADS_PER_LIST,
    MESSAGE_CONTRACT,
    READONLY_SCOPE,
    GmailTransportDescription,
    GmailTransportError,
    domain_of,
    parse_address,
)

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"

DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "se-skills" / "gmail-oauth.json"
CREDENTIALS_ENV = "SE_GMAIL_OAUTH_FILE"
TRANSPORT_ENV = "SE_GMAIL_TRANSPORT"
CREDENTIALS_CONTRACT = "se-skills-gmail-oauth-v1"

MAX_LIST_TERMS = 50
MAX_LIST_DAYS = 92
MAX_LIST_RESULTS = MAX_THREADS_PER_LIST * 2
MAX_LIST_PAGES = 3
PAGE_SIZE = 100
MAX_RESPONSE_BYTES = 4 * MAX_MESSAGE_BYTES
REQUEST_TIMEOUT_SECONDS = 20.0
TOKEN_REFRESH_SKEW_SECONDS = 60
_METADATA_HEADERS = ("From", "To", "Cc", "Subject", "Date")
_FULL_HEADERS = _METADATA_HEADERS + ("Message-ID", "In-Reply-To", "References")
_TERM = re.compile(r"^(?:[A-Za-z0-9._%+\-']+@)?[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_TAG = re.compile(r"<[^>]{0,500}>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n{3,}")
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "dailyLimitExceeded"})


# --- HTTP seam -----------------------------------------------------------------

@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict)


class HttpClient(Protocol):
    """Minimal async HTTP seam so tests can substitute canned provider responses."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


class HttpxClient:
    def __init__(self, *, timeout: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
                response = await client.request(method, url, headers=headers, params=params, data=data)
        except httpx.HTTPError as exc:
            raise GmailTransportError("provider_unavailable", "Gmail could not be reached.", retryable=True) from exc
        body = response.content
        if len(body) > MAX_RESPONSE_BYTES:
            raise GmailTransportError("malformed_response", "Gmail response exceeded the size bound.", retryable=False)
        return HttpResponse(response.status_code, body, dict(response.headers))


# --- Local credentials -------------------------------------------------------------

@dataclass(frozen=True)
class LocalOAuthCredentials:
    """Installed-app OAuth client + refresh token, read from the user's own 0600 file.
    `client_secret` is the installed-app value Google issues (not a confidential secret,
    but kept off the repo and out of every log all the same)."""

    client_id: str
    client_secret: str
    refresh_token: str
    scopes: tuple[str, ...]
    authorized_at: str | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> LocalOAuthCredentials:
        if raw.get("contract") != CREDENTIALS_CONTRACT:
            raise GmailTransportError("credentials_missing", "The local Gmail credential file has an unknown contract.", retryable=False)
        fields = {k: raw.get(k) for k in ("client_id", "client_secret", "refresh_token")}
        if not all(isinstance(v, str) and v.strip() for v in fields.values()):
            raise GmailTransportError("credentials_missing", "The local Gmail credential file is incomplete.", retryable=False)
        scopes = raw.get("scopes")
        if not isinstance(scopes, list) or not all(isinstance(s, str) for s in scopes):
            raise GmailTransportError("credentials_missing", "The local Gmail credential file has no scope list.", retryable=False)
        authorized_at = raw.get("authorized_at")
        return cls(
            client_id=fields["client_id"].strip(),
            client_secret=fields["client_secret"].strip(),
            refresh_token=fields["refresh_token"].strip(),
            scopes=tuple(sorted(scopes)),
            authorized_at=authorized_at if isinstance(authorized_at, str) else None,
        )


def credentials_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = env.get(CREDENTIALS_ENV)
    return Path(override).expanduser() if override else DEFAULT_CREDENTIALS_PATH


def load_local_credentials(path: Path) -> LocalOAuthCredentials:
    try:
        raw = json.loads(path.read_bytes()[:65_536])
    except FileNotFoundError:
        raise GmailTransportError(
            "credentials_missing",
            "No local Gmail authorization on this machine; run scripts/gmail_local_authorize.py.",
            retryable=False,
        ) from None
    except (OSError, ValueError) as exc:
        raise GmailTransportError("credentials_missing", "The local Gmail credential file could not be read.", retryable=False) from exc
    if not isinstance(raw, Mapping):
        raise GmailTransportError("credentials_missing", "The local Gmail credential file is not an object.", retryable=False)
    return LocalOAuthCredentials.from_mapping(raw)


# --- Transport --------------------------------------------------------------------

class LiveGmailReadOnlyTransport:
    """Gmail REST transport for one locally authorized mailbox. See module docstring."""

    def __init__(
        self,
        *,
        credentials_file: Path,
        http: HttpClient | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._credentials_file = credentials_file
        self._http = http or HttpxClient()
        self._clock = clock
        self._access_token: str | None = None
        self._token_expires_at = 0.0
        self._token_refresh_token: str | None = None

    # -- description

    def describe(self) -> GmailTransportDescription:
        authorized = self._credentials_file.is_file()
        return GmailTransportDescription(
            mode="live_readonly",
            live_retrieval_available=authorized,
            requires_credentials=True,
            label=(
                "Local Gmail read-only route (your own OAuth grant, gmail.readonly only; "
                "verified with fake responses — no live mailbox has been exercised)"
                if authorized
                else "Local Gmail read-only route is enabled but not authorized on this machine; "
                "run scripts/gmail_local_authorize.py"
            ),
            payload_contracts=[MESSAGE_CONTRACT],
        )

    # -- auth

    async def _token(self) -> str:
        credentials = load_local_credentials(self._credentials_file)
        now = self._clock()
        if (
            self._access_token
            and self._token_refresh_token == credentials.refresh_token
            and now < self._token_expires_at - TOKEN_REFRESH_SKEW_SECONDS
        ):
            return self._access_token
        response = await self._http.request(
            "POST",
            TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "grant_type": "refresh_token",
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
                "refresh_token": credentials.refresh_token,
            },
        )
        payload = _json_object(response, allow_error=True)
        if response.status != 200:
            error = payload.get("error") if isinstance(payload.get("error"), str) else ""
            if response.status in (400, 401) and error in {"invalid_grant", "unauthorized_client", "invalid_client"}:
                self._access_token = None
                raise GmailTransportError(
                    "access_revoked", "Gmail refresh was rejected; the grant was revoked or expired.", retryable=False
                )
            raise _status_error(response.status, payload)
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not token:
            raise GmailTransportError("malformed_response", "Token response had no access token.", retryable=False)
        self._access_token = token
        self._token_refresh_token = credentials.refresh_token
        self._token_expires_at = now + (float(expires_in) if isinstance(expires_in, (int, float)) else 0.0)
        return token

    async def _get(self, url: str, params: Mapping[str, Any] | None = None) -> tuple[HttpResponse, dict[str, Any]]:
        token = await self._token()
        response = await self._http.request(
            "GET", url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, params=params
        )
        if response.status == 200:
            return response, _json_object(response)
        payload = _json_object(response, allow_error=True)
        raise _status_error(response.status, payload)

    async def check_access(self) -> Mapping[str, Any]:
        token = await self._token()
        info = await self._http.request("GET", TOKENINFO_URL, params={"access_token": token})
        if info.status != 200:
            self._access_token = None
            raise GmailTransportError("not_authorized", "Gmail token is not valid for this mailbox.", retryable=False)
        scope_field = _json_object(info).get("scope")
        if not isinstance(scope_field, str):
            raise GmailTransportError("malformed_response", "Token info had no scope field.", retryable=False)
        scopes = sorted({s for s in scope_field.split() if s})
        _response, profile = await self._get(f"{GMAIL_API}/profile")
        address = profile.get("emailAddress")
        if not isinstance(address, str) or "@" not in address or len(address) > 320:
            raise GmailTransportError("malformed_response", "Profile response had no mailbox address.", retryable=False)
        return {"email_address": address.strip().lower(), "scopes": scopes}

    # -- listing (metadata only)

    async def list_threads(
        self, *, after: date, before: date, participants: Sequence[str], max_results: int
    ) -> Sequence[Mapping[str, Any]]:
        terms = _validate_terms(participants)
        if before < after:
            raise GmailTransportError("invalid_bound", "Listing range end precedes its start.", retryable=False)
        if (before - after).days > MAX_LIST_DAYS:
            raise GmailTransportError("invalid_bound", f"Listing range exceeds {MAX_LIST_DAYS} days.", retryable=False)
        if max_results < 1:
            raise GmailTransportError("invalid_bound", "Listing needs a positive result bound.", retryable=False)
        limit = min(max_results, MAX_LIST_RESULTS)
        query = _build_query(after, before, terms)
        bound = set(terms)

        thread_ids: list[str] = []
        page_token: str | None = None
        for _page in range(MAX_LIST_PAGES):
            params: dict[str, Any] = {"q": query, "maxResults": min(PAGE_SIZE, limit - len(thread_ids))}
            if page_token:
                params["pageToken"] = page_token
            _response, page = await self._get(f"{GMAIL_API}/threads", params)
            raw_threads = page.get("threads", [])
            if not isinstance(raw_threads, list):
                raise GmailTransportError("malformed_response", "Thread list was not a list.", retryable=False)
            for entry in raw_threads:
                thread_id = entry.get("id") if isinstance(entry, Mapping) else None
                if isinstance(thread_id, str) and GMAIL_ID.match(thread_id) and thread_id not in thread_ids:
                    thread_ids.append(thread_id)
                if len(thread_ids) >= limit:
                    break
            page_token = page.get("nextPageToken") if isinstance(page.get("nextPageToken"), str) else None
            if len(thread_ids) >= limit or not page_token:
                break

        rows: list[dict[str, Any]] = []
        for thread_id in thread_ids:
            try:
                _response, thread = await self._get(
                    f"{GMAIL_API}/threads/{thread_id}",
                    {"format": "metadata", "metadataHeaders": list(_METADATA_HEADERS)},
                )
            except GmailTransportError as exc:
                if exc.code == "not_found":
                    continue
                raise
            row = _thread_row(thread_id, thread)
            if row is None:
                continue
            if not any(_matches_bound(address, bound) for address in row["participants"]):
                continue
            rows.append(row)
        return rows

    # -- retrieval (selected ids only)

    async def get_messages(self, message_ids: Sequence[str]) -> Sequence[Mapping[str, Any]]:
        ids: list[str] = []
        for raw in message_ids:
            if not isinstance(raw, str) or not GMAIL_ID.match(raw):
                raise GmailTransportError("invalid_bound", "A selected message id is malformed.", retryable=False)
            if raw not in ids:
                ids.append(raw)
        if len(ids) > MAX_MESSAGES_PER_GET:
            raise GmailTransportError(
                "invalid_bound", f"At most {MAX_MESSAGES_PER_GET} messages may be retrieved at once.", retryable=False
            )
        rows: list[Mapping[str, Any]] = []
        for message_id in ids:
            token = await self._token()
            response = await self._http.request(
                "GET",
                f"{GMAIL_API}/messages/{message_id}",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                params={"format": "full", "metadataHeaders": list(_FULL_HEADERS)},
            )
            if response.status == 404:
                rows.append({"id": message_id, "error_code": "NOT_FOUND"})
                continue
            if response.status != 200:
                error = _status_error(response.status, _json_object(response, allow_error=True))
                if error.code == "not_authorized" and response.status == 403:
                    rows.append({"id": message_id, "error_code": "UNAUTHORIZED"})
                    continue
                raise error
            rows.append(_message_row(message_id, _json_object(response)))
        return rows


# --- Response helpers ---------------------------------------------------------------

def _json_object(response: HttpResponse, *, allow_error: bool = False) -> dict[str, Any]:
    try:
        payload = json.loads(response.body or b"{}")
    except ValueError:
        if allow_error:
            return {}
        raise GmailTransportError("malformed_response", "Gmail returned a non-JSON body.", retryable=False) from None
    if not isinstance(payload, dict):
        if allow_error:
            return {}
        raise GmailTransportError("malformed_response", "Gmail returned a non-object body.", retryable=False)
    return payload


def _error_reasons(payload: Mapping[str, Any]) -> set[str]:
    error = payload.get("error")
    reasons: set[str] = set()
    if isinstance(error, Mapping):
        errors = error.get("errors")
        if isinstance(errors, list):
            for item in errors:
                if isinstance(item, Mapping) and isinstance(item.get("reason"), str):
                    reasons.add(item["reason"])
        status = error.get("status")
        if isinstance(status, str):
            reasons.add(status)
    return reasons


def _status_error(status: int, payload: Mapping[str, Any]) -> GmailTransportError:
    reasons = _error_reasons(payload)
    if status == 429 or (status == 403 and reasons & _RATE_LIMIT_REASONS):
        return GmailTransportError("rate_limited", "Gmail rate-limited the request.", retryable=True)
    if status == 401:
        return GmailTransportError("not_authorized", "Gmail rejected the authorization.", retryable=False)
    if status == 403:
        return GmailTransportError("not_authorized", "Gmail refused access for this grant.", retryable=False)
    if status == 404:
        return GmailTransportError("not_found", "Gmail resource not found.", retryable=False)
    if status >= 500:
        return GmailTransportError("provider_unavailable", "Gmail returned a server error.", retryable=True)
    return GmailTransportError("malformed_response", f"Gmail returned an unexpected status {status}.", retryable=False)


def _validate_terms(participants: Sequence[str]) -> list[str]:
    terms: list[str] = []
    for raw in participants:
        if not isinstance(raw, str):
            raise GmailTransportError("invalid_bound", "Participant terms must be strings.", retryable=False)
        term = raw.strip().lower()
        if not _TERM.match(term) or len(term) > 320:
            raise GmailTransportError("invalid_bound", "A participant term is not an address or domain.", retryable=False)
        if term not in terms:
            terms.append(term)
    if not terms:
        raise GmailTransportError("invalid_bound", "Listing needs at least one participant term.", retryable=False)
    if len(terms) > MAX_LIST_TERMS:
        raise GmailTransportError("invalid_bound", f"Listing accepts at most {MAX_LIST_TERMS} participant terms.", retryable=False)
    return terms


def _build_query(after: date, before: date, terms: Sequence[str]) -> str:
    """Gmail search: `after:` is inclusive, `before:` exclusive, so add one day."""
    clauses = " OR ".join(f"from:{t} OR to:{t} OR cc:{t}" for t in terms)
    return (
        f"after:{after.strftime('%Y/%m/%d')} before:{(before + timedelta(days=1)).strftime('%Y/%m/%d')} "
        f"({clauses}) -in:chats"
    )


def _matches_bound(address: str, bound: set[str]) -> bool:
    return address in bound or domain_of(address) in bound


def _headers(payload: Mapping[str, Any], allowed: Sequence[str]) -> dict[str, str]:
    raw = payload.get("headers")
    out: dict[str, str] = {}
    if not isinstance(raw, list):
        return out
    wanted = {h.lower(): h for h in allowed}
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        name, value = item.get("name"), item.get("value")
        if isinstance(name, str) and isinstance(value, str) and name.lower() in wanted and wanted[name.lower()] not in out:
            out[wanted[name.lower()]] = value[:10_000]
    return out


def _addresses(value: str | None) -> list[str]:
    if not value:
        return []
    found: list[str] = []
    for part in value.split(","):
        parsed = parse_address(part)
        if parsed is not None and parsed.email.lower() not in found:
            found.append(parsed.email.lower())
    return found


def _internal_date(payload: Mapping[str, Any]) -> datetime | None:
    raw = payload.get("internalDate")
    try:
        millis = int(raw)
    except (TypeError, ValueError):
        return None
    if millis < 0 or millis > 4_102_444_800_000:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=UTC)


def _thread_row(thread_id: str, thread: Mapping[str, Any]) -> dict[str, Any] | None:
    if thread.get("id") != thread_id:
        return None
    messages = thread.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    participants: list[str] = []
    message_ids: list[str] = []
    subject: str | None = None
    last: datetime | None = None
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        message_id = message.get("id")
        if not isinstance(message_id, str) or not GMAIL_ID.match(message_id):
            continue
        payload = message.get("payload") if isinstance(message.get("payload"), Mapping) else {}
        headers = _headers(payload, _METADATA_HEADERS)
        if subject is None and headers.get("Subject"):
            subject = headers["Subject"][:500]
        for address in _addresses(headers.get("From")) + _addresses(headers.get("To")) + _addresses(headers.get("Cc")):
            if address not in participants:
                participants.append(address)
        stamp = _internal_date(message)
        if stamp is not None and (last is None or stamp > last):
            last = stamp
        message_ids.append(message_id)
    if not message_ids or last is None:
        return None
    return {
        "id": thread_id,
        "subject": subject,
        "message_count": len(message_ids),
        "last_message_at": last.isoformat(),
        "participants": participants[:1000],
        "message_ids": message_ids,
    }


def _decode_body(part: Mapping[str, Any]) -> str:
    body = part.get("body")
    data = body.get("data") if isinstance(body, Mapping) else None
    if not isinstance(data, str) or not data:
        return ""
    if len(data) > MAX_MESSAGE_BYTES:
        raise _Malformed("body part exceeds the size bound")
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError) as exc:
        raise _Malformed("body part is not base64url") from exc
    return raw.decode("utf-8", errors="replace")


def _html_to_text(markup: str) -> str:
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", markup)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", text)
    text = html.unescape(_TAG.sub("", text))
    return _BLANKS.sub("\n\n", _WS.sub(" ", text)).strip()


class _Malformed(Exception):
    pass


def _walk_parts(payload: Mapping[str, Any], *, depth: int = 0, seen: int = 0) -> tuple[list[str], list[str], int]:
    """Returns (plain_texts, html_texts, attachment_count) without touching attachment ids."""
    if depth > 20 or seen > 500:
        raise _Malformed("MIME tree too deep or too wide")
    plain: list[str] = []
    rich: list[str] = []
    attachments = 0
    mime = payload.get("mimeType") if isinstance(payload.get("mimeType"), str) else ""
    filename = payload.get("filename") if isinstance(payload.get("filename"), str) else ""
    body = payload.get("body") if isinstance(payload.get("body"), Mapping) else {}
    if filename or body.get("attachmentId"):
        attachments += 1
    elif mime.lower() == "text/plain":
        plain.append(_decode_body(payload))
    elif mime.lower() == "text/html":
        rich.append(_decode_body(payload))
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, Mapping):
                raise _Malformed("MIME part is not an object")
            seen += 1
            sub_plain, sub_rich, sub_attachments = _walk_parts(part, depth=depth + 1, seen=seen)
            plain.extend(sub_plain)
            rich.extend(sub_rich)
            attachments += sub_attachments
    return plain, rich, attachments


def _message_row(message_id: str, message: Mapping[str, Any]) -> dict[str, Any]:
    thread_id = message.get("threadId")
    malformed = {"id": message_id, "error_code": "MALFORMED"}
    if isinstance(thread_id, str) and GMAIL_ID.match(thread_id):
        malformed["thread_id"] = thread_id
    if message.get("id") != message_id or "thread_id" not in malformed:
        return malformed
    size = message.get("sizeEstimate")
    if isinstance(size, int) and size > MAX_MESSAGE_BYTES:
        return malformed
    payload = message.get("payload")
    if not isinstance(payload, Mapping):
        return malformed
    headers = _headers(payload, _FULL_HEADERS)
    sender = parse_address(headers.get("From", ""))
    if sender is None:
        return malformed
    stamp: datetime | None = None
    if headers.get("Date"):
        try:
            stamp = parsedate_to_datetime(headers["Date"])
        except (TypeError, ValueError, IndexError):
            stamp = None
        if stamp is not None and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
    internal = _internal_date(message)
    if stamp is None:
        stamp = internal
    if stamp is None:
        return malformed
    try:
        plain, rich, attachments = _walk_parts(payload)
    except _Malformed:
        return malformed
    body_text = "\n\n".join(t for t in plain if t.strip()).strip() or "\n\n".join(_html_to_text(t) for t in rich if t.strip()).strip()
    if len(body_text) > 400_000:
        return malformed

    def address_list(name: str) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for part in (headers.get(name) or "").split(","):
            parsed = parse_address(part)
            if parsed is not None:
                out.append({"name": parsed.name, "email": parsed.email} if parsed.name else {"email": parsed.email})
        return out[:500]

    references = [r for r in (headers.get("References") or "").split() if r][:500]
    return {
        "id": message_id,
        "thread_id": thread_id,
        "subject": headers.get("Subject", "")[:500] or None,
        "from": {"name": sender.name, "email": sender.email} if sender.name else {"email": sender.email},
        "to": address_list("To"),
        "cc": address_list("Cc"),
        "date": stamp.isoformat(),
        "internal_date": internal.isoformat() if internal else None,
        "message_id_header": (headers.get("Message-ID") or None) and headers["Message-ID"][:998],
        "in_reply_to": (headers.get("In-Reply-To") or None) and headers["In-Reply-To"][:998],
        "references": references,
        "body_text": body_text or None,
        "attachment_count": min(attachments, 10_000),
        "web_url": f"https://mail.google.com/mail/u/0/#all/{message_id}",
    }


def authorization_url(*, client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
    """Installed-app consent URL: exactly one scope, offline access, PKCE."""
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    })
