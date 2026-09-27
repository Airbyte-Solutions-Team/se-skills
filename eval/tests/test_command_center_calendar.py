"""Manual Calendar update and persisted one-week Today view, using synthetic events."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
import pytest

from integrations.calendar_readonly import (
    CalendarError, GoogleCalendarReadOnlyTransport, READONLY_SCOPE,
    CREDENTIALS_CONTRACT,
)
from integrations.gmail_live import HttpResponse
from services.calendar_snapshot_service import CalendarSnapshotService
from eval.tests.test_salesforce_portfolio import setup as crm_setup

NOW = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)


class FakeCalendar:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def list_events(self, start, end):
        self.calls.append((start, end))
        if self.fail:
            raise CalendarError("provider_unavailable")
        return [
            {"summary": "Customer discovery", "start": {"dateTime": (NOW + timedelta(days=1)).isoformat()},
             "end": {"dateTime": (NOW + timedelta(days=1, hours=1)).isoformat()},
             "htmlLink": "https://calendar.google.com/calendar/event?eid=abc"},
            {"summary": "Personal", "visibility": "private", "start": {"dateTime": NOW.isoformat()},
             "end": {"dateTime": (NOW + timedelta(hours=1)).isoformat()}},
            {"summary": "Out of office", "eventType": "outOfOffice", "start": {"dateTime": NOW.isoformat()},
             "end": {"dateTime": (NOW + timedelta(hours=1)).isoformat()}},
            {"summary": "After window", "start": {"dateTime": (NOW + timedelta(days=8)).isoformat()},
             "end": {"dateTime": (NOW + timedelta(days=8, hours=1)).isoformat()}},
        ]


def test_manual_update_and_saved_page_reads(tmp_path):
    h, sf, _ = crm_setup(tmp_path)
    fake = FakeCalendar()
    service = CalendarSnapshotService(customers_dir=h.customers, transport=fake,
                                     configured=True, clock=lambda: NOW)
    h.app.state.calendar_snapshot_service = service
    assert h.client.get("/api/command-center/calendar").json()["state"] == "not_checked"
    assert h.client.get("/api/command-center/update").json()["salesforce"]["state"] == "not_checked"
    assert not fake.calls and not sf.calls
    result = h.client.post("/api/command-center/update").json()
    assert result["salesforce"]["state"] == "scope_needed"
    assert result["calendar"]["state"] == "ok"
    assert result["calendar"]["window_days"] == 7
    assert [e["title"] for e in result["calendar"]["events"]] == ["Customer discovery"]
    assert fake.calls == [(NOW, NOW + timedelta(days=7))]
    assert not sf.calls  # no saved scope, no implicit broad CRM query
    h.client.get("/api/command-center/today")
    h.client.get("/api/command-center/portfolio")
    h.client.get("/api/command-center/calendar")
    assert len(fake.calls) == 1 and not sf.calls
    fake.fail = True
    result = h.client.post("/api/command-center/update").json()
    assert result["calendar"]["state"] == "stale"
    assert result["calendar"]["events"][0]["title"] == "Customer discovery"
    assert "Personal" not in (h.customers / ".command-center" / "calendar-week.json").read_text()


def test_existing_crm_scope_refreshes_with_calendar(tmp_path):
    h, sf, service = crm_setup(tmp_path)
    fake = FakeCalendar()
    h.app.state.calendar_snapshot_service = CalendarSnapshotService(
        customers_dir=h.customers, transport=fake, configured=True, clock=lambda: NOW)
    asyncio.run(service.refresh("se", ["Synthetic AE"]))
    response = h.client.post("/api/command-center/update").json()
    assert response["salesforce"]["state"] == "ok"
    assert len(sf.calls) == 2 and len(fake.calls) == 1


class FakeHttp:
    def __init__(self):
        self.calls = []
        self.scope = READONLY_SCOPE

    async def request(self, method, url, *, headers=None, params=None, data=None):
        self.calls.append((method, url, headers, params, data))
        if url.endswith("/token"):
            return HttpResponse(200, b'{"access_token":"fake", "expires_in":3600}')
        if url.endswith("/tokeninfo"):
            return HttpResponse(200, json.dumps({"scope": self.scope}).encode())
        return HttpResponse(200, json.dumps({"items": [{"summary": "Safe"}]}).encode())


def test_live_transport_bounds_oauth_scope_and_window(tmp_path, monkeypatch):
    from integrations import calendar_readonly
    monkeypatch.setattr(calendar_readonly, "validate_credentials_path", lambda p: p)
    path = tmp_path / "calendar-oauth.json"
    path.write_text(json.dumps({"contract": CREDENTIALS_CONTRACT,
        "scopes": [READONLY_SCOPE], "client_id": "client", "client_secret": "secret", "refresh_token": "refresh"}))
    http = FakeHttp()
    transport = GoogleCalendarReadOnlyTransport(credentials_file=path, http=http, clock=lambda: 0)
    asyncio.run(transport.list_events(NOW, NOW + timedelta(days=7)))
    req = http.calls[-1]
    assert req[3]["timeMin"] == NOW.isoformat()
    assert req[3]["timeMax"] == (NOW + timedelta(days=7)).isoformat()
    assert req[3]["singleEvents"] == "true"
    http.scope = READONLY_SCOPE + " https://www.googleapis.com/auth/calendar.events"
    with pytest.raises(CalendarError, match="not_authorized"):
        asyncio.run(transport.list_events(NOW, NOW + timedelta(days=7)))
    assert len([c for c in http.calls if c[1].endswith("/events")]) == 1
