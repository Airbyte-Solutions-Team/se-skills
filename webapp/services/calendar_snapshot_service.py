"""Manual seven-day Calendar snapshot; page reads never contact Google."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from integrations.calendar_readonly import CalendarError, MAX_EVENTS
from services.private_store import atomic_write_private, mkdir_private


class CalendarSnapshotService:
    def __init__(self, *, customers_dir: Path, transport, configured: bool = False, clock=None) -> None:
        self._path = Path(customers_dir) / ".command-center" / "calendar-week.json"
        self._transport = transport
        self.configured = configured
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()

    def _saved(self) -> dict:
        try:
            value = json.loads(self._path.read_text(encoding="utf-8"))
            if isinstance(value, dict) and value.get("schema_version") == 1:
                return value
        except (OSError, ValueError):
            pass
        return {"schema_version": 1, "last_success": None, "last_attempt": None}

    def snapshot(self) -> dict:
        saved = self._saved()
        now = self._clock().astimezone(timezone.utc)
        success = saved.get("last_success")
        events = [event for event in (success or {}).get("events", [])
                  if now <= datetime.fromisoformat(event["end"]) and
                  datetime.fromisoformat(event["start"]) < now + timedelta(days=7)]
        attempt = saved.get("last_attempt")
        return {"configured": self.configured, "window_days": 7,
                "refreshed_at": success.get("refreshed_at") if success else None,
                "last_attempt": attempt, "events": events,
                "state": ("not_configured" if not self.configured else
                          "not_checked" if attempt is None else
                          "stale" if attempt["state"] != "ok" and success else
                          attempt["state"])}

    async def refresh(self) -> dict:
        async with self._lock:
            start = self._clock().astimezone(timezone.utc)
            end = start + timedelta(days=7)
            try:
                raw = await self._transport.list_events(start, end)
                if not isinstance(raw, list) or len(raw) > MAX_EVENTS:
                    raise CalendarError("result_truncated")
                events = []
                for item in raw:
                    if not isinstance(item, dict) or item.get("status") == "cancelled":
                        continue
                    if item.get("eventType", "default") != "default" or item.get("visibility") == "private":
                        continue
                    if item.get("transparency") == "transparent":
                        continue
                    if any(a.get("self") and a.get("responseStatus") == "declined"
                           for a in item.get("attendees", []) if isinstance(a, dict)):
                        continue
                    beginning = item.get("start", {}).get("dateTime")
                    finishing = item.get("end", {}).get("dateTime")
                    if not isinstance(beginning, str) or not isinstance(finishing, str):
                        continue  # all-day events are not customer calls
                    try:
                        first = datetime.fromisoformat(beginning.replace("Z", "+00:00")).astimezone(timezone.utc)
                        last = datetime.fromisoformat(finishing.replace("Z", "+00:00")).astimezone(timezone.utc)
                    except ValueError:
                        continue
                    if last <= start or first >= end or last <= first:
                        continue
                    title = item.get("summary")
                    if not isinstance(title, str) or not title.strip():
                        title = "Busy"
                    link = item.get("htmlLink", "")
                    parsed = urlparse(link) if isinstance(link, str) else None
                    if not parsed or parsed.scheme != "https" or parsed.hostname != "calendar.google.com":
                        link = None
                    events.append({"title": title.strip()[:200], "start": first.isoformat(),
                                   "end": last.isoformat(), "link": link})
                events.sort(key=lambda e: e["start"])
                state = "ok"
            except CalendarError as exc:
                state = exc.code
            except Exception:  # provider failures must not break saved state or leak data
                state = "provider_unavailable"
            saved = self._saved()
            saved["last_attempt"] = {"state": state, "at": start.isoformat()}
            if state == "ok":
                saved["last_success"] = {"refreshed_at": start.isoformat(),
                                         "window_end": end.isoformat(), "events": events}
            mkdir_private(self._path.parent)
            atomic_write_private(self._path, json.dumps(saved, ensure_ascii=False).encode("utf-8"))
            return self.snapshot()
