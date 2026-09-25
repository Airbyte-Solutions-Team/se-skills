"""User-triggered, read-only Gmail evidence intake (Command Center PR E).

Mirrors the Granola retrieval flow: explicit access check → bounded, metadata-only
thread listing → the user selects messages → only those ids are fetched, normalized,
and recorded in the private evidence ledger. Nothing polls, nothing scans in the
background, and no attachment is ever fetched.

Discovery is bounded (spec §8): a listing needs at least one contact or domain the
user names, or contacts learned from already-associated Gmail evidence. Threads whose
participants are all inside the signed-in mailbox's own domain are dropped as internal;
threads matching nothing are dropped as unrelated. Both are returned as counts only.

Association is never decided here. After import the service records *proposals*
(thread mapping, known contact, account-domain match) that stay reviewable until the
user confirms one; a message that matches two opportunities of the same account is
exactly the ambiguous case the review surface exists for.

Bodies never appear in this module's return values, job metadata, or the private
index it keeps (`gmail-index.json`: source_id → thread id + participant addresses).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from command_center_evidence import (
    SAFE_TOKEN,
    AssociationCandidate,
    NormalizedEmailMessage,
)
from integrations.gmail import (
    GMAIL_ID,
    MAX_MESSAGES_PER_GET,
    MAX_THREADS_PER_LIST,
    READONLY_SCOPE,
    GmailImportError,
    GmailReadOnlyTransport,
    GmailSourceAdapter,
    GmailThreadRow,
    GmailTransportError,
    domain_of,
)
from pydantic import ValidationError

from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService
from services.job_service import JobService, ManagedJobError
from services.private_store import atomic_write_private, mkdir_private

INTAKE_JOB_KIND = "command_center_gmail_intake"
CONNECTION_ID = "gmail-readonly-local"
MAX_SELECTION = MAX_MESSAGES_PER_GET
MAX_LISTED = MAX_THREADS_PER_LIST
MAX_QUERY_TERMS = 20
MAX_CANDIDATES = 20
TimeRange = Literal["today", "yesterday", "this_week", "last_week", "last_30_days", "custom"]

_CHECK_FILE = "gmail-connection.json"
_INDEX_FILE = "gmail-index.json"
_MAX_INDEX_BYTES = 8_000_000
_TERM = re.compile(r"^(?:[A-Za-z0-9._%+\-']+@)?[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_GENERIC_DOMAINS = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com", "proton.me", "protonmail.com"})

LocalOpportunities = Callable[[], list[dict[str, Any]]]


class GmailIntakeError(Exception):
    def __init__(self, status: int, detail: str, *, code: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code


def _range_dates(time_range: TimeRange, custom_start: date | None, custom_end: date | None) -> tuple[date, date]:
    today = datetime.now(UTC).date()
    if time_range == "today":
        return today, today
    if time_range == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if time_range == "this_week":
        return today - timedelta(days=today.weekday()), today
    if time_range == "last_week":
        start = today - timedelta(days=today.weekday() + 7)
        return start, start + timedelta(days=6)
    if time_range == "last_30_days":
        return today - timedelta(days=30), today
    if custom_start is None or custom_end is None or custom_end < custom_start:
        raise GmailIntakeError(400, "Custom range needs a start and end date.", code="invalid_range")
    if (custom_end - custom_start).days > 92:
        raise GmailIntakeError(400, "Custom range is limited to 92 days.", code="invalid_range")
    return custom_start, custom_end


def _account_token(account: str) -> str:
    return re.sub(r"[^a-z0-9]", "", account.casefold())


class GmailIntakeService:
    def __init__(
        self,
        *,
        transport: GmailReadOnlyTransport,
        adapter: GmailSourceAdapter,
        ledger: EvidenceLedgerService,
        job_service: JobService,
        local_opportunities: LocalOpportunities,
    ) -> None:
        self._transport = transport
        self._adapter = adapter
        self._ledger = ledger
        self._jobs = job_service
        self._local_opportunities = local_opportunities

    # ------------------------------------------------------------ describe

    def describe(self) -> dict[str, Any]:
        return {
            **self._transport.describe().model_dump(mode="json"),
            "trigger": "user_triggered_retrieval",
            "max_selection": MAX_SELECTION,
            "max_listed": MAX_LISTED,
            "connection_id": CONNECTION_ID,
            "last_check": self._last_check(),
            "background_scanning": False,
            "calendar": False,
            "organization_sharing": False,
        }

    # ---------------------------------------------------------- connection

    def _private_path(self, name: str, *, create: bool = False) -> Path:
        return self._ledger._ledger_dir(create=create) / name

    def _last_check(self) -> dict[str, Any] | None:
        try:
            path = self._private_path(_CHECK_FILE)
        except EvidenceLedgerError:
            return None
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_bytes()[:4096])
        except ValueError:
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("checked_at"), str):
            return None
        return {
            "checked_at": raw["checked_at"],
            "mailbox_domain": raw.get("mailbox_domain") if isinstance(raw.get("mailbox_domain"), str) else None,
        }

    def _record_check(self, mailbox: str) -> dict[str, Any]:
        marker = {"checked_at": datetime.now(UTC).isoformat(), "mailbox_domain": domain_of(mailbox)}
        path = self._private_path(_CHECK_FILE, create=True)
        mkdir_private(path.parent)
        atomic_write_private(path, json.dumps(marker, separators=(",", ":")).encode("utf-8"))
        return marker

    def _clear_check(self) -> None:
        try:
            path = self._private_path(_CHECK_FILE)
        except EvidenceLedgerError:
            return
        if path.exists():
            path.unlink()

    def connection(self) -> dict[str, Any]:
        """Persisted marker only; no provider call."""
        return {
            "transport": self._transport.describe().model_dump(mode="json"),
            "last_check": self._last_check(),
            "checked": False,
            "note": "Manual check only. Nothing is polled; this reflects the last explicit check you ran.",
        }

    async def check_access(self) -> dict[str, Any]:
        try:
            raw = await self._transport.check_access()
        except GmailTransportError as exc:
            return self._transport_failure(exc)
        mailbox = raw.get("email_address") if isinstance(raw, Mapping) else None
        scopes = raw.get("scopes") if isinstance(raw, Mapping) else None
        if not isinstance(mailbox, str) or "@" not in mailbox or not isinstance(scopes, list):
            raise GmailIntakeError(502, "Access check returned an unexpected shape.", code="unexpected_shape")
        if any(not isinstance(s, str) or s != READONLY_SCOPE for s in scopes) or not scopes:
            self._clear_check()
            return {
                "checked": True, "connected": False, "error_code": "scope_not_readonly", "retryable": False,
                "checked_at": datetime.now(UTC).isoformat(),
            }
        marker = self._record_check(mailbox)
        return {
            "checked": True,
            "connected": True,
            "mailbox_domain": marker["mailbox_domain"],
            "scopes": [READONLY_SCOPE],
            "last_check": marker,
            "checked_at": marker["checked_at"],
        }

    @staticmethod
    def _transport_failure(exc: GmailTransportError) -> dict[str, Any]:
        return {
            "checked": True,
            "connected": False,
            "error_code": exc.code,
            "retryable": exc.retryable,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    def _assert_checked(self) -> dict[str, Any]:
        check = self._last_check()
        if check is None:
            raise GmailIntakeError(409, "Run the access check before listing or retrieving.", code="not_checked")
        return check

    async def _assert_access(self) -> str:
        raw = await self._transport.check_access()
        mailbox = raw.get("email_address") if isinstance(raw, Mapping) else None
        if not isinstance(mailbox, str) or "@" not in mailbox:
            raise GmailIntakeError(502, "Access check returned an unexpected shape.", code="unexpected_shape")
        return mailbox.lower()

    # --------------------------------------------------------------- index

    def _read_index(self) -> dict[str, dict[str, Any]]:
        try:
            path = self._private_path(_INDEX_FILE)
        except EvidenceLedgerError:
            return {}
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_bytes()[:_MAX_INDEX_BYTES])
        except ValueError:
            return {}
        if not isinstance(raw, dict):
            return {}
        index: dict[str, dict[str, Any]] = {}
        for source_id, entry in raw.items():
            if (
                isinstance(entry, dict) and isinstance(entry.get("thread_id"), str)
                and isinstance(entry.get("participants"), list)
            ):
                index[source_id] = {
                    "thread_id": entry["thread_id"],
                    "participants": [p for p in entry["participants"] if isinstance(p, str)],
                }
        return index

    def _write_index(self, index: dict[str, dict[str, Any]]) -> None:
        path = self._private_path(_INDEX_FILE, create=True)
        atomic_write_private(path, json.dumps(index, sort_keys=True, separators=(",", ":")).encode("utf-8"))

    def _gmail_sources(self) -> dict[str, dict[str, Any]]:
        known: dict[str, dict[str, Any]] = {}
        offset: int | None = 0
        while offset is not None:
            page = self._ledger.list_sources(limit=200, offset=offset)
            for item in page["sources"]:
                if item["provider"] == "gmail" and item["connection_id"] == CONNECTION_ID:
                    known[item["source_id"]] = item
            offset = page["next_offset"]
        return known

    def _known_context(self, mailbox_domain: str | None) -> dict[str, Any]:
        """Contacts, domains, and thread ids learned from *associated* Gmail evidence."""
        sources = self._gmail_sources()
        index = self._read_index()
        contacts: dict[str, list[tuple[str, str]]] = {}
        domains: dict[str, list[tuple[str, str]]] = {}
        threads: dict[str, list[tuple[str, str]]] = {}
        by_object: dict[str, dict[str, Any]] = {}
        for source_id, item in sources.items():
            by_object[item["provider_object_id"]] = item
            assoc = item["association"]
            if assoc["state"] != "associated":
                continue
            target = (assoc["account"], assoc["opportunity_slug"])
            entry = index.get(source_id)
            if entry is None:
                continue
            threads.setdefault(entry["thread_id"], []).append(target)
            for address in entry["participants"]:
                if mailbox_domain and domain_of(address) == mailbox_domain:
                    continue
                contacts.setdefault(address, []).append(target)
                if domain_of(address) not in _GENERIC_DOMAINS:
                    domains.setdefault(domain_of(address), []).append(target)
        return {"contacts": contacts, "domains": domains, "threads": threads, "by_object": by_object}

    # -------------------------------------------------------------- listing

    @staticmethod
    def _normalize_terms(terms: Sequence[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in terms:
            term = raw.strip().lower().lstrip("@")
            if not term:
                continue
            if not _TERM.match(term) or len(term) > 320:
                raise GmailIntakeError(400, "Contacts must be email addresses or domains.", code="invalid_term")
            if term not in cleaned:
                cleaned.append(term)
        if len(cleaned) > MAX_QUERY_TERMS:
            raise GmailIntakeError(400, f"Name at most {MAX_QUERY_TERMS} contacts or domains.", code="too_many_terms")
        return cleaned

    async def list_threads(
        self,
        *,
        time_range: TimeRange,
        custom_start: date | None = None,
        custom_end: date | None = None,
        participants: Sequence[str] = (),
    ) -> dict[str, Any]:
        after, before = _range_dates(time_range, custom_start, custom_end)
        check = self._assert_checked()
        terms = self._normalize_terms(participants)
        context = self._known_context(check.get("mailbox_domain"))
        bound = set(terms) | set(context["contacts"]) | set(context["domains"])
        if not bound and not context["threads"]:
            raise GmailIntakeError(
                400,
                "Name at least one contact or domain; the mailbox is never scanned unbounded.",
                code="unbounded_discovery",
            )
        try:
            mailbox = await self._assert_access()
            rows = await self._transport.list_threads(
                after=after, before=before, participants=sorted(bound), max_results=MAX_LISTED * 2
            )
        except GmailTransportError as exc:
            raise GmailIntakeError(502 if exc.retryable else 409, "Gmail listing failed.", code=exc.code) from exc
        mailbox_domain = domain_of(mailbox)
        threads: list[dict[str, Any]] = []
        excluded_unrelated = excluded_internal = rejected = 0
        for raw in rows:
            try:
                row = GmailThreadRow.model_validate(raw if isinstance(raw, Mapping) else {})
            except ValidationError:
                rejected += 1
                continue
            addresses = sorted({a.lower() for a in row.participants})
            external = [a for a in addresses if domain_of(a) != mailbox_domain]
            if not external:
                excluded_internal += 1
                continue
            matched_on: list[str] = []
            if row.id.lower() in context["threads"]:
                matched_on.append("thread")
            if any(a in bound for a in external):
                matched_on.append("contact")
            if any(domain_of(a) in bound for a in external):
                matched_on.append("domain")
            if not matched_on:
                excluded_unrelated += 1
                continue
            messages = []
            for message_id in row.message_ids:
                source = context["by_object"].get(message_id.lower())
                messages.append({
                    "message_id": message_id.lower(),
                    "ledger": None if source is None else {
                        "source_id": source["source_id"],
                        "latest_revision": source["latest_revision"],
                        "availability": source["availability"],
                        "processing_status": source["processing"]["status"],
                        "association_state": source["association"]["state"],
                    },
                })
            threads.append({
                "thread_id": row.id.lower(),
                "subject": row.subject,
                "message_count": row.message_count,
                "last_message_at": row.last_message_at.isoformat(),
                "external_participants": external,
                "matched_on": matched_on,
                "messages": messages,
            })
            if len(threads) >= MAX_LISTED:
                break
        return {
            "trigger": "user_triggered_retrieval",
            "unattended_discovery": False,
            "listed_at": datetime.now(UTC).isoformat(),
            "time_range": time_range,
            "after": after.isoformat(),
            "before": before.isoformat(),
            "bound": {"named": terms, "known_contacts": len(context["contacts"]), "known_threads": len(context["threads"])},
            "returned": len(rows),
            "shown": len(threads),
            "truncated": len(rows) > len(threads) + excluded_unrelated + excluded_internal + rejected,
            "excluded_unrelated": excluded_unrelated,
            "excluded_internal": excluded_internal,
            "rejected": rejected,
            "max_selection": MAX_SELECTION,
            "threads": threads,
        }

    # ------------------------------------------------------------ retrieval

    async def start_retrieval(self, message_ids: Sequence[str]) -> dict[str, Any]:
        ids: list[str] = []
        for raw in message_ids:
            if not GMAIL_ID.match(raw.lower()):
                raise GmailIntakeError(400, "Message ids must be Gmail hex ids.", code="invalid_message_id")
            if raw.lower() not in ids:
                ids.append(raw.lower())
        if not ids:
            raise GmailIntakeError(400, "Select at least one message.", code="empty_selection")
        if len(ids) > MAX_SELECTION:
            raise GmailIntakeError(400, f"Select at most {MAX_SELECTION} messages per retrieval.", code="selection_too_large")
        self._assert_checked()
        for job in self._jobs.jobs.values():
            if job.get("kind") == INTAKE_JOB_KIND and job.get("status") == "running":
                raise GmailIntakeError(409, "A retrieval is already running.", code="retrieval_in_progress")

        async def runner(job_id: str) -> dict[str, Any]:
            return await self._run(ids)

        job_id, persist_warn = await self._jobs.launch_managed(
            kind=INTAKE_JOB_KIND,
            account="",
            opp_slug="",
            opportunity="",
            sig=None,
            safe_metadata={
                "trigger": "user_triggered_retrieval",
                "unattended_discovery": False,
                "message_ids": ids,
                "requested": len(ids),
            },
            runner=runner,
        )
        return {"job_id": job_id, "status": "running", "requested": len(ids), "persist_warn": persist_warn}

    def job(self, job_id: str) -> dict[str, Any] | None:
        job = self._jobs.get_job(job_id)
        if job is None or job.get("kind") != INTAKE_JOB_KIND:
            return None
        return {"job_id": job_id, **{key: value for key, value in job.items() if key != "sig"}}

    async def _run(self, ids: list[str]) -> dict[str, Any]:
        try:
            mailbox = await self._assert_access()
        except GmailTransportError as exc:
            if exc.code == "access_revoked":
                self._clear_check()
            raise ManagedJobError(exc.code, "Gmail access check failed before retrieval; nothing was imported.")
        except GmailIntakeError as exc:
            raise ManagedJobError(exc.code, exc.detail)

        outcomes: dict[str, dict[str, Any]] = {}
        details: dict[str, Mapping[str, Any]] = {}
        unrequested = 0
        code: str | None = None
        try:
            for row in await self._transport.get_messages(ids):
                if not (isinstance(row, Mapping) and isinstance(row.get("id"), str)):
                    unrequested += 1
                    continue
                row_id = row["id"].lower()
                if row_id not in ids or row_id in details:
                    unrequested += 1
                    continue
                details[row_id] = row
        except GmailTransportError as exc:
            if exc.retryable:
                for message_id in ids:
                    outcomes[message_id] = {"outcome": "failed_retryable", "error_code": exc.code}
                return self._summary(ids, outcomes, None, unrequested, 0)
            code = "UNAUTHORIZED" if exc.code in {"access_revoked", "transport_unavailable", "not_authorized"} else "NOT_FOUND"

        messages: list[NormalizedEmailMessage] = []
        order: list[str] = []
        for message_id in ids:
            if code is not None:
                payload: Mapping[str, Any] = {"id": message_id, "error_code": code}
            elif message_id not in details:
                payload = {"id": message_id, "error_code": "NOT_FOUND"}
            else:
                payload = details[message_id]
            try:
                messages.append(self._adapter.normalize(payload, connection_id=CONNECTION_ID))
                order.append(message_id)
            except GmailImportError as exc:
                outcomes[message_id] = {"outcome": "rejected", "error_code": exc.code}

        import_id: str | None = None
        proposals = 0
        if messages:
            try:
                recorded = self._ledger.import_meetings(messages, trigger="user_triggered_retrieval")
            except EvidenceLedgerError as exc:
                raise ManagedJobError(exc.code, "The ledger rejected the retrieval; nothing was imported.")
            import_id = recorded["import_id"]
            index = self._read_index()
            for message, result in zip(messages, recorded["results"]):
                if message.availability == "content_available" or message.availability == "metadata_only":
                    index[result["source_id"]] = {
                        "thread_id": message.thread_id,
                        "participants": sorted({p.email for p in message.attendees}),
                    }
            self._write_index(index)
            context = self._known_context(domain_of(mailbox))
            for message, result in zip(messages, recorded["results"]):
                outcome = _classify(result)
                if result["created_revision"] and result["association_state"] == "unassociated" and message.availability in {"content_available", "metadata_only"}:
                    candidates = self._candidates(message, context, domain_of(mailbox))
                    if candidates:
                        try:
                            proposed = self._ledger.propose_association(
                                result["source_id"], candidates, reason="Proposed from Gmail thread, contact, and domain signals."
                            )
                            outcome["proposed_candidates"] = len(candidates)
                            outcome["association_state"] = proposed["association"]["state"]
                            outcome["processing_status"] = proposed["processing"]["status"]
                            proposals += 1
                        except EvidenceLedgerError:
                            outcome["proposed_candidates"] = 0
                    else:
                        outcome["proposed_candidates"] = 0
                outcomes[message.identity.provider_object_id] = outcome
        return self._summary(ids, outcomes, import_id, unrequested, proposals)

    def _candidates(
        self, message: NormalizedEmailMessage, context: Mapping[str, Any], mailbox_domain: str
    ) -> list[AssociationCandidate]:
        """Signals only; every candidate is a proposal the user must confirm (spec §5.4)."""
        seen: dict[tuple[str, str], AssociationCandidate] = {}

        def add(target: tuple[str, str], method: str, reason: str) -> None:
            account, slug = target
            if not (SAFE_TOKEN.match(account) and SAFE_TOKEN.match(slug)) or len(seen) >= MAX_CANDIDATES:
                return
            seen.setdefault(target, AssociationCandidate(account=account, opportunity_slug=slug, method=method, reason=reason))

        for target in context["threads"].get(message.thread_id, []):
            add(target, "thread_mapping", "Another message in this thread is associated here.")
        external = [p.email for p in message.attendees if domain_of(p.email) != mailbox_domain]
        for address in external:
            for target in context["contacts"].get(address, []):
                add(target, "contact", f"{address} appears on associated evidence.")
        external_domains = {domain_of(a) for a in external} - _GENERIC_DOMAINS
        for domain in sorted(external_domains):
            for target in context["domains"].get(domain, []):
                add(target, "domain", f"Domain {domain} appears on associated evidence.")
        opportunities = self._local_opportunities()
        for domain in sorted(external_domains):
            label = _account_token(domain.split(".")[0])
            for opp in opportunities:
                if label and _account_token(opp["account"]) == label:
                    add((opp["account"], opp["opportunity_slug"]), "domain", f"Domain {domain} matches account {opp['account']}.")
        return list(seen.values())

    @staticmethod
    def _summary(
        ids: list[str], outcomes: dict[str, dict[str, Any]], import_id: str | None, unrequested: int, proposals: int
    ) -> dict[str, Any]:
        fallback = {"outcome": "failed_retryable", "error_code": "no_result"}
        rows = [{"message_id": message_id, **outcomes.get(message_id, fallback)} for message_id in ids]
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
        return {
            "trigger": "user_triggered_retrieval",
            "unattended_discovery": False,
            "import_id": import_id,
            "unrequested_dropped": unrequested,
            "proposals_recorded": proposals,
            "counts": counts,
            "results": rows,
        }

    # ----------------------------------------------------------- revocation

    def revoke_access(self) -> dict[str, Any]:
        """Explicit user step after revoking Gmail authorization: every Gmail source on this
        connection gets an `access_lost` revision (cached bodies are withheld) and the access
        marker is removed so nothing lists or retrieves until a fresh check succeeds."""
        sources = self._gmail_sources()
        messages: list[NormalizedEmailMessage] = []
        index = self._read_index()
        for source_id, item in sources.items():
            if item["availability"] == "access_lost":
                continue
            entry = index.get(source_id)
            messages.append(self._adapter.normalize(
                {
                    "id": item["provider_object_id"],
                    "thread_id": entry["thread_id"] if entry else item["provider_object_id"],
                    "error_code": "UNAUTHORIZED",
                },
                connection_id=CONNECTION_ID,
            ))
        results: list[dict[str, Any]] = []
        if messages:
            results = self._ledger.import_meetings(messages, trigger="user_triggered_retrieval")["results"]
        self._clear_check()
        return {
            "revoked": True,
            "sources_marked": sum(1 for r in results if r["created_revision"]),
            "already_lost": len(sources) - len(messages),
            "results": [_classify(r) for r in results],
        }


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
    if availability == "pending_unknown":
        return {"outcome": "not_found", **base}
    if availability == "metadata_only":
        return {"outcome": "no_body", **base} if result.get("created_revision") else {"outcome": "already_known", **base}
    if not result.get("created_revision"):
        return {"outcome": "already_known", **base}
    if result.get("revision", 1) > 1:
        return {"outcome": "edited", **base}
    return {"outcome": "imported", **base}
