"""Post-run backstop: save Granola transcripts a skill run read but didn't save.

Every skill is told to save any transcript it pulls from Gong or Granola to
`_transcripts/` before using it (`skills/_se-playbook.md` → "Every pulled
transcript is saved"). That is an instruction to a model, and runs have skipped
it: a recap run read a same-day Granola transcript without saving it, so the
next post-call run couldn't see the call and wrote up the previous one.

This module is the deterministic half. After a skill run finishes, it reads the
run's final reply and the Markdown outputs the run wrote, collects Granola
meeting ids cited on lines that mention Granola (skills record
`Granola meeting ID: <uuid>` in Source Coverage), and for any id with no saved
`# Source: Granola | <id> | ...` transcript it fetches the transcript through the
same restricted Claude Code MCP relay the Command Center uses and saves it with
the standard name and provenance header. It runs only as the tail of a skill
run the SE started, never on a schedule, and only for meetings the run itself
cited. Results are ids, file names and outcome codes only (no transcript text),
so they are safe to keep in the job record.

Outcomes per meeting: `saved`, `already_saved` (a `# Source: Granola | <id>`
file exists), `same_day_exists` (a transcript for that customer and call date is
already saved, e.g. the Gong copy; no duplicate is written), `not_found`,
`pending_content` (Granola has no transcript yet; a summary is never saved in
its place), `rejected`, `failed` (relay error; `error_code` says which),
`skipped` (over the per-run cap or the time budget).
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

from integrations.granola_mcp_relay import GranolaRelayError, GranolaRetrievalTransport

from services.transcription_service import TranscriptionError, TranscriptionService

logger = logging.getLogger(__name__)

MAX_MEETINGS_PER_RUN = 3
TIME_BUDGET_SECONDS = 420.0
MAX_TEXT_BYTES = 2_000_000
_MEETING_ID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_DISPLAY_DATE_FORMATS = ("%Y-%m-%d", "%b %d, %Y", "%B %d, %Y", "%a, %b %d, %Y", "%A, %B %d, %Y", "%m/%d/%Y")


def cited_granola_meeting_ids(texts: Iterable[str]) -> list[str]:
    """Granola meeting ids (lowercased, first-seen order) on lines that mention Granola.

    Restricting to Granola lines keeps unrelated UUIDs (job ids, Salesforce-like
    ids in prose) out; Gong call ids are numeric and never match.
    """
    ids: list[str] = []
    for text in texts:
        for line in (text or "").splitlines():
            if "granola" not in line.lower():
                continue
            for match in _MEETING_ID.findall(line):
                meeting_id = match.lower()
                if meeting_id not in ids:
                    ids.append(meeting_id)
    return ids


def _rows(raw: Any) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, Mapping):
        for key in ("meetings", "results", "documents", "items"):
            if isinstance(raw.get(key), list):
                return list(raw[key])
        if isinstance(raw.get("id"), str):
            return [raw]
    return []


def _find_meeting(raw: Any, meeting_id: str) -> Mapping[str, Any] | None:
    for row in _rows(raw):
        if isinstance(row, Mapping) and isinstance(row.get("id"), str) and row["id"].lower() == meeting_id:
            return row
    return None


def _parse_iso(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # Local calendar date, matching how live transcription names its files.
    return parsed.astimezone().date() if parsed.tzinfo else parsed.date()


def _parse_display(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(value.split())
    for candidate in (text, text.split(" at ")[0], ",".join(text.split(",")[:2]).strip()):
        for fmt in _DISPLAY_DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


def call_date_for(transcript: Mapping[str, Any], meeting: Mapping[str, Any], *, today: date) -> tuple[date, str]:
    """(call date, where it came from). Falls back to today only when Granola gives no usable date."""
    for value, source in (
        (transcript.get("created_at"), "transcript_created_at"),
        (meeting.get("created_at"), "meeting_created_at"),
    ):
        parsed = _parse_iso(value)
        if parsed:
            return parsed, source
    parsed = _parse_display(meeting.get("date"))
    if parsed:
        return parsed, "meeting_date"
    return today, "save_date"


class GranolaTranscriptBackstop:
    """Called by `JobService` after a successful skill run (see module docstring)."""

    def __init__(
        self,
        *,
        transport: GranolaRetrievalTransport,
        transcripts: TranscriptionService,
        outputs_since: Callable[[str, str | None, str, float], list[Path]],
        enabled: Callable[[], bool],
        max_meetings: int = MAX_MEETINGS_PER_RUN,
        time_budget_seconds: float = TIME_BUDGET_SECONDS,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._transport = transport
        self._transcripts = transcripts
        self._outputs_since = outputs_since
        self._enabled = enabled
        self._max_meetings = max_meetings
        self._time_budget = time_budget_seconds
        self._today = today

    def _run_texts(self, meta: Mapping[str, Any], started_at: float, stdout: str) -> list[str]:
        texts = [stdout or ""]
        try:
            paths = self._outputs_since(meta["account"], meta.get("opp_slug"), meta.get("skill") or "freeform", started_at)
        except Exception:  # noqa: BLE001 — a lookup failure must never fail the run
            logger.warning("Could not list outputs for the Granola transcript backstop", exc_info=True)
            paths = []
        for path in paths:
            try:
                if path.stat().st_size <= MAX_TEXT_BYTES:
                    texts.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
        return texts

    async def after_run(self, meta: Mapping[str, Any], started_at: float, stdout: str) -> dict[str, Any] | None:
        """Save any cited-but-unsaved Granola transcript. Returns None when nothing was cited."""
        try:
            if not self._enabled():
                return None
        except Exception:  # noqa: BLE001
            return None
        cited = cited_granola_meeting_ids(self._run_texts(meta, started_at, stdout))
        if not cited:
            return None
        saved_ids = self._transcripts.saved_granola_meeting_ids()
        results: dict[str, dict[str, Any]] = {
            meeting_id: {"meeting_id": meeting_id, "outcome": "already_saved"}
            for meeting_id in cited
            if meeting_id in saved_ids
        }
        pending = [meeting_id for meeting_id in cited if meeting_id not in saved_ids]
        to_fetch = pending[: self._max_meetings]
        for meeting_id in pending[self._max_meetings:]:
            results[meeting_id] = {"meeting_id": meeting_id, "outcome": "skipped", "error_code": "over_run_cap"}

        async def fetch_all() -> None:
            for meeting_id in to_fetch:
                try:
                    results[meeting_id] = await self._save_one(meta["account"], meeting_id)
                except Exception:  # noqa: BLE001 — report it; never lose the other meetings' results
                    logger.warning("Granola transcript backstop failed for one meeting", exc_info=True)
                    results[meeting_id] = {"meeting_id": meeting_id, "outcome": "failed", "error_code": "unexpected_error"}

        if to_fetch:
            try:
                await asyncio.wait_for(fetch_all(), timeout=self._time_budget)
            except TimeoutError:
                for meeting_id in to_fetch:
                    results.setdefault(
                        meeting_id, {"meeting_id": meeting_id, "outcome": "skipped", "error_code": "time_budget"}
                    )
        return {
            "kind": "granola_transcripts",
            "cited": len(cited),
            "results": [results[meeting_id] for meeting_id in cited],
        }

    async def _save_one(self, account: str, meeting_id: str) -> dict[str, Any]:
        base: dict[str, Any] = {"meeting_id": meeting_id}
        try:
            meeting = _find_meeting(await self._transport.call("get_meetings", {"meeting_ids": [meeting_id]}), meeting_id)
        except GranolaRelayError as exc:
            return {**base, "outcome": "failed", "error_code": exc.code}
        if meeting is None:
            return {**base, "outcome": "not_found"}
        try:
            raw = await self._transport.call("get_meeting_transcript", {"meeting_id": meeting_id})
        except GranolaRelayError as exc:
            if exc.code == "tool_not_found":
                return {**base, "outcome": "pending_content"}
            return {**base, "outcome": "failed", "error_code": exc.code}
        transcript = raw if isinstance(raw, Mapping) else {}
        returned_id = transcript.get("id")
        if isinstance(returned_id, str) and returned_id.lower() != meeting_id:
            return {**base, "outcome": "rejected", "error_code": "transcript_id_mismatch"}
        text = transcript.get("transcript")
        if not isinstance(text, str) or not text.strip():
            return {**base, "outcome": "pending_content"}
        call_date, date_source = call_date_for(transcript, meeting, today=self._today())
        # A transcript already saved for this call date is almost always this call — saved
        # from Gong (Granola "also had it, not used"), or by the run without the header.
        # A second copy would be double-counted by every later skill, so don't write one.
        existing = self._transcripts.transcripts_for_call_date(account, call_date)
        if existing:
            return {**base, "outcome": "same_day_exists", "name": existing[0]}
        title = meeting.get("title") if isinstance(meeting.get("title"), str) else None
        try:
            saved = self._transcripts.save_granola_transcript(
                account, meeting_id=meeting_id, title=title, call_date=call_date, text=text
            )
        except (TranscriptionError, OSError) as exc:
            code = "too_large" if isinstance(exc, TranscriptionError) and exc.status_code == 413 else "save_failed"
            return {**base, "outcome": "failed", "error_code": code}
        return {**base, "outcome": "saved", "name": saved["name"], "date_source": date_source}
