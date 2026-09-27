"""Explicit local Google Calendar read-only transport for the primary calendar."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from integrations.gmail_live import (
    HttpClient, HttpxClient, TOKEN_URL, TOKENINFO_URL, validate_credentials_path,
)

READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.events.readonly"
CREDENTIALS_CONTRACT = "se-skills-calendar-oauth-v1"
CREDENTIALS_ENV = "SE_CALENDAR_OAUTH_FILE"
TRANSPORT_ENV = "SE_CALENDAR_TRANSPORT"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "se-skills" / "calendar-oauth.json"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
MAX_EVENTS = 100
MAX_PAGES = 3


class CalendarError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def credentials_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env[CREDENTIALS_ENV]).expanduser() if env.get(CREDENTIALS_ENV) else DEFAULT_CREDENTIALS_PATH


def load_credentials(path: Path) -> dict[str, Any]:
    try:
        path = validate_credentials_path(path)
        raw = json.loads(path.read_bytes()[:65_536])
    except Exception as exc:  # file/path errors never include provider content in the response
        raise CalendarError("not_authorized") from exc
    if not isinstance(raw, dict) or raw.get("contract") != CREDENTIALS_CONTRACT:
        raise CalendarError("not_authorized")
    if raw.get("scopes") != [READONLY_SCOPE] or not all(
        isinstance(raw.get(k), str) and raw[k] for k in ("client_id", "client_secret", "refresh_token")
    ):
        raise CalendarError("not_authorized")
    return raw


def _payload(response: Any) -> dict[str, Any]:
    if len(response.body) > 1_000_000:
        raise CalendarError("invalid_response")
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeError) as exc:
        raise CalendarError("invalid_response") from exc
    if not isinstance(value, dict):
        raise CalendarError("invalid_response")
    return value


class UnavailableCalendarTransport:
    async def list_events(self, start: datetime, end: datetime) -> list[dict]:
        raise CalendarError("not_configured")


class GoogleCalendarReadOnlyTransport:
    def __init__(self, *, credentials_file: Path, http: HttpClient | None = None,
                 clock=time.monotonic) -> None:
        self.credentials_file = credentials_file
        self._http = http or HttpxClient()
        self._clock = clock
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._refresh_token: str | None = None

    async def _token(self) -> str:
        credentials = load_credentials(self.credentials_file)
        if (self._access_token and self._refresh_token == credentials["refresh_token"]
                and self._clock() < self._expires_at - 60):
            return self._access_token
        try:
            response = await self._http.request("POST", TOKEN_URL, data={
                "grant_type": "refresh_token", "client_id": credentials["client_id"],
                "client_secret": credentials["client_secret"], "refresh_token": credentials["refresh_token"],
            })
        except Exception as exc:
            raise CalendarError("provider_unavailable") from exc
        if response.status != 200:
            raise CalendarError("not_authorized" if response.status in (400, 401) else "provider_unavailable")
        payload = _payload(response)
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise CalendarError("invalid_response")
        self._access_token = token
        self._refresh_token = credentials["refresh_token"]
        self._expires_at = self._clock() + float(payload.get("expires_in") or 0)
        return token

    async def list_events(self, start: datetime, end: datetime) -> list[dict]:
        token = await self._token()
        try:
            info = await self._http.request("GET", TOKENINFO_URL, params={"access_token": token})
        except Exception as exc:
            raise CalendarError("provider_unavailable") from exc
        if info.status != 200 or set(str(_payload(info).get("scope", "")).split()) != {READONLY_SCOPE}:
            raise CalendarError("not_authorized")
        rows: list[dict] = []
        page_token: str | None = None
        for _ in range(MAX_PAGES):
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(),
                      "singleEvents": "true", "orderBy": "startTime", "maxResults": MAX_EVENTS}
            if page_token:
                params["pageToken"] = page_token
            try:
                response = await self._http.request("GET", EVENTS_URL,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}, params=params)
            except Exception as exc:
                raise CalendarError("provider_unavailable") from exc
            if response.status != 200:
                raise CalendarError("not_authorized" if response.status in (401, 403) else "provider_unavailable")
            payload = _payload(response)
            items = payload.get("items")
            if not isinstance(items, list):
                raise CalendarError("invalid_response")
            rows.extend(items)
            page_token = payload.get("nextPageToken")
            if not page_token:
                return rows
            if not isinstance(page_token, str) or len(page_token) > 4096:
                raise CalendarError("invalid_response")
        # A partial list could silently omit the user's next call.
        raise CalendarError("result_truncated")
