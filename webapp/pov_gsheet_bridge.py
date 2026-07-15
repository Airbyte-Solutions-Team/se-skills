#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pydantic>=2.0", "pyyaml>=6.0"]
# ///
"""Thin bridge from raw MCP/tool output to normalized `ExternalEvidence` for `pov-gsheet`.

The skill itself calls the configured MCP tools (`mcp__salesforce__run_soql_query`,
`mcp__gong__search_calls`, `gong://calls/{callId}/transcript`). This script turns the
raw tool responses into the `ExternalEvidence` JSON files that
`pov_gsheet_context.py` already consumes.

It does not call MCPs itself, does not store credentials, and writes only to the
path passed with `--out`.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pov_gsheet_context import ExternalEvidence, _find_connectors


# Same canonical destination set used in the loader, so the bridge and context agree.
_DESTINATION_CANONICAL: set[str] = {
    "Snowflake",
    "BigQuery",
    "Redshift",
    "S3",
    "GCS",
    "Azure Blob Storage",
    "Kafka",
    "Apache Iceberg",
    "Databricks",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sf_record_id(record: dict[str, Any]) -> str | None:
    return record.get("Id") or record.get("id") or record.get("Id")


def _sf_pick_active_opp(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Choose the most relevant open opportunity, excluding renewals unless only one."""
    if not records:
        return None
    open_non_renewal = [
        r for r in records if not r.get("IsClosed") and r.get("Type") != "Renewal"
    ]
    if open_non_renewal:
        return open_non_renewal[0]
    renewals = [r for r in records if not r.get("IsClosed") and r.get("Type") == "Renewal"]
    if renewals:
        return renewals[0]
    return records[0]


def _sf_full_name(record: dict[str, Any]) -> str | None:
    first = record.get("FirstName") or ""
    last = record.get("LastName") or ""
    name = f"{first} {last}".strip()
    return name or record.get("Name")


def _sf_owner_name(record: dict[str, Any]) -> str | None:
    owner = record.get("Owner") or {}
    if isinstance(owner, dict):
        return owner.get("Name") or owner.get("Name")
    return None


def _sf_account_name(record: dict[str, Any]) -> str | None:
    account = record.get("Account") or {}
    if isinstance(account, dict):
        return account.get("Name") or account.get("name")
    return record.get("AccountName") or record.get("Account.Name")


def _sf_account_id(record: dict[str, Any]) -> str | None:
    account = record.get("Account") or {}
    if isinstance(account, dict):
        return account.get("Id") or account.get("id")
    return record.get("AccountId") or record.get("Account.id")


def _sf_field_kind(field: str) -> str | None:
    """Infer source/destination/use_case from Salesforce field names."""
    lower = field.lower()
    if "destination" in lower:
        return "destination"
    if "source" in lower or "originating" in lower:
        return "source"
    if "use_case" in lower:
        return "use_case"
    return None


def _classify_in_text(name: str, text: str, default: str = "unknown") -> str:
    """Classify a connector mention as source/destination by sentence context.

    Heuristic: in a sentence containing 'into' or 'to', connectors before are
    sources and connectors after are destinations. Destination-canonical names
    override to destination. If no marker, fall back to canonical set.
    """
    name_lower = name.lower()
    if name in _DESTINATION_CANONICAL or name_lower in {d.lower() for d in _DESTINATION_CANONICAL}:
        return "destination"

    # Split into sentences and look for the first sentence that mentions the name.
    sentences = re.split(r"[.!?]\s+", text)
    for sentence in sentences:
        if name not in sentence and name_lower not in sentence.lower():
            continue
        norm = sentence.lower()
        # Find connector tokens in this sentence.
        tokens = re.findall(r"[A-Za-z0-9_\-]+", sentence)
        matches = [i for i, t in enumerate(tokens) if t.lower() == name_lower or t.lower() == name_lower.replace(" ", "_")]
        if not matches:
            continue
        for marker in ("into", "to", "towards"):
            if marker in norm:
                marker_idx = next((i for i, t in enumerate(tokens) if t.lower() == marker), None)
                if marker_idx is not None:
                    # If the connector appears before the marker, it's likely a source.
                    if all(idx < marker_idx for idx in matches):
                        return "source"
                    # If after the marker, likely a destination.
                    if all(idx > marker_idx for idx in matches):
                        return "destination"
        # Sentence-level source marker.
        if any(t.lower() in ("from", "source") for t in tokens):
            return "source"
    return default


def normalize_salesforce(
    raw: Any,
    account_name: str,
    opportunity_name: str | None = None,
    retrieved_at: str | None = None,
) -> list[ExternalEvidence]:
    """Convert a Salesforce SOQL result (or list of records) into `ExternalEvidence`.

    Handles both `sf data query --json` style (`{"result": {"records": [...]}}`)
    and a top-level list of records. Produces `prospect` and `contact` evidence.
    """
    evidence: list[ExternalEvidence] = []
    if not raw:
        return evidence

    records: list[dict[str, Any]] = []
    if isinstance(raw, list):
        records = raw
    elif isinstance(raw, dict):
        if "records" in raw:
            records = raw["records"]
        elif "result" in raw and isinstance(raw["result"], dict):
            records = raw["result"].get("records", [])
        else:
            records = [raw]
    records = [r for r in records if isinstance(r, dict)]

    # Separate opportunities from contacts/accounts by shape.
    opps = [r for r in records if "StageName" in r or "CloseDate" in r or "Amount" in r]
    contacts = [r for r in records if "Email" in r or "FirstName" in r or "LastName" in r]
    accounts = [r for r in records if "Account" in r or "Account.Name" in r or "Type" in r]
    if not opps and not contacts and accounts and len(accounts) == 1:
        # Treat a lone account as prospect metadata.
        account = accounts[0]
        evidence.append(
            ExternalEvidence(
                source="salesforce",
                source_id=_sf_record_id(account),
                retrieved_at=retrieved_at or _now_iso(),
                account_name=_sf_account_name(account) or account_name,
                opportunity_name=opportunity_name,
                direct_customer=False,
                fact_type="prospect",
                fact={
                    "account_name": _sf_account_name(account) or account_name,
                    "account_id": _sf_account_id(account),
                    "type": account.get("Type") or "Account",
                },
                status="ok",
                note="Salesforce Account record",
            )
        )

    if opps:
        active = _sf_pick_active_opp(opps)
        if active:
            opp_name = active.get("Name") or opportunity_name or account_name
            evidence.append(
                ExternalEvidence(
                    source="salesforce",
                    source_id=_sf_record_id(active),
                    retrieved_at=retrieved_at or _now_iso(),
                    account_name=_sf_account_name(active) or account_name,
                    opportunity_name=opp_name,
                    direct_customer=False,
                    fact_type="prospect",
                    fact={
                        "account_name": _sf_account_name(active) or account_name,
                        "account_id": _sf_account_id(active),
                        "opportunity_name": opp_name,
                        "opportunity_id": _sf_record_id(active),
                        "stage": active.get("StageName"),
                        "owner": _sf_owner_name(active),
                        "se_name": active.get("SE_Name__c"),
                        "close_date": active.get("CloseDate"),
                        "next_step": active.get("Next_Step__c"),
                        "amount": active.get("Amount"),
                        "type": active.get("Type"),
                        "se_deal_risks": active.get("SE_Deal_Risks__c"),
                        "why_buy_anything": active.get("Why_buy_anything__c"),
                        "why_buy_now": active.get("Why_buy_now__c"),
                        "why_buy_from_airbyte": active.get("Why_buy_from_Airbyte__c"),
                        "required_features": active.get("Required_features_functionality__c"),
                        "most_important_sources": active.get("Most_important_sources__c"),
                        "most_important_destinations": active.get("Most_Important_Destinations__c"),
                    },
                    status="ok",
                    note=f"Salesforce Opportunity: {opp_name}",
                )
            )
            # Surface POV/technical fields as business_objective or technical_system evidence.
            for field, label, fact_type_base in [
                ("Required_features_functionality__c", "required features/functionality", "business_objective"),
                ("Most_important_sources__c", "most important sources", "technical_system"),
                ("Most_Important_Destinations__c", "most important destinations", "technical_system"),
                ("Use_case_description__c", "use case description", "business_objective"),
                ("Airbyte_Use_Case__c", "Airbyte use case", "business_objective"),
            ]:
                value = active.get(field)
                if value:
                    fact = {"title": label, "description": str(value), "field": field}
                    if fact_type_base == "technical_system":
                        fact["kind"] = _sf_field_kind(field) or "unknown"
                    evidence.append(
                        ExternalEvidence(
                            source="salesforce",
                            source_id=_sf_record_id(active),
                            retrieved_at=retrieved_at or _now_iso(),
                            account_name=_sf_account_name(active) or account_name,
                            opportunity_name=opp_name,
                            direct_customer=False,
                            fact_type=fact_type_base,
                            fact=fact,
                            raw=str(value)[:500],
                            status="ok",
                            note=f"Salesforce field {field} on {opp_name}",
                        )
                    )

    for contact in contacts:
        email = contact.get("Email") or contact.get("email")
        name = _sf_full_name(contact) or contact.get("Name") or contact.get("name")
        title = contact.get("Title") or contact.get("title")
        evidence.append(
            ExternalEvidence(
                source="salesforce",
                source_id=_sf_record_id(contact),
                retrieved_at=retrieved_at or _now_iso(),
                account_name=_sf_account_name(contact) or account_name,
                opportunity_name=opportunity_name,
                direct_customer=False,
                fact_type="contact",
                fact={
                    "name": name,
                    "role": title,
                    "email": email,
                    "side": "Customer",
                },
                raw=json.dumps({"name": name, "title": title, "email": email}) if (name or email) else None,
                status="ok",
                note="Salesforce Contact" + (f" — {title}" if title else ""),
            )
        )

    return evidence


def normalize_gong(
    search_result: Any,
    transcripts: dict[str, Any] | None = None,
    account_name: str = "",
    opportunity_name: str | None = None,
    retrieved_at: str | None = None,
) -> list[ExternalEvidence]:
    """Convert a Gong `search_calls` result plus per-call transcripts into evidence.

    `transcripts` is a dict mapping `callId` to the transcript resource payload.
    """
    evidence: list[ExternalEvidence] = []
    if not search_result:
        return evidence

    if isinstance(search_result, dict):
        calls = search_result.get("calls") or search_result.get("result", {}).get("calls", [])
    else:
        calls = search_result if isinstance(search_result, list) else []

    transcripts = transcripts or {}
    retrieved = retrieved_at or _now_iso()

    for call in calls:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id") or call.get("callId") or call.get("call_id")
        title = call.get("title") or call.get("subject") or "Gong call"
        started = call.get("started") or call.get("startedDateTime") or call.get("date")
        participants = call.get("parties") or call.get("participants") or []
        participant_names = [
            (p.get("name") or p.get("email") or str(p)) for p in participants if isinstance(p, dict)
        ]
        # Basic transcript evidence.
        transcript_text = ""
        transcript_obj = transcripts.get(call_id) if isinstance(transcripts, dict) else None
        if transcript_obj and isinstance(transcript_obj, dict):
            transcript_text = _gong_transcript_text(transcript_obj)
        evidence.append(
            ExternalEvidence(
                source="gong",
                source_id=call_id,
                retrieved_at=retrieved,
                account_name=account_name,
                opportunity_name=opportunity_name,
                direct_customer=True,
                fact_type="transcript",
                fact={
                    "title": title,
                    "started": started,
                    "participants": participant_names,
                    "call_id": call_id,
                    "url": call.get("url") or call.get("callUrl"),
                },
                raw=transcript_text[:2000] if transcript_text else None,
                status="ok",
                note=f"Gong call transcript — {title}",
            )
        )

        # Extract requirements / decisions / systems from the transcript text.
        if transcript_text:
            text = transcript_text
            # Look for requirement-like statements.
            for match in re.finditer(r"(?i)(?:we need|we require|must have|need to|requirement)\s+(.{0,120})", text):
                snippet = match.group(0).strip()
                evidence.append(
                    ExternalEvidence(
                        source="gong",
                        source_id=call_id,
                        retrieved_at=retrieved,
                        account_name=account_name,
                        opportunity_name=opportunity_name,
                        direct_customer=True,
                        fact_type="requirement",
                        fact={"title": "Customer requirement", "description": snippet},
                        raw=snippet,
                        status="ok",
                        note=f"Extracted from Gong call {call_id}",
                    )
                )
            # Look for connector/system mentions, classifying by sentence context.
            for name, explicit_kind in _find_connectors(text):
                kind = explicit_kind or _classify_in_text(name, text)
                evidence.append(
                    ExternalEvidence(
                        source="gong",
                        source_id=call_id,
                        retrieved_at=retrieved,
                        account_name=account_name,
                        opportunity_name=opportunity_name,
                        direct_customer=True,
                        fact_type="technical_system",
                        fact={
                            "name": name,
                            "kind": kind,
                            "description": f"Mentioned on Gong call {call_id}",
                        },
                        raw=f"{name} ({kind})",
                        status="ok",
                        note=f"Connector/system mention from Gong call {call_id}",
                    )
                )
            # Simple decision / action item patterns.
            for pattern, fact_type, label in [
                (r"(?i)(?:decided|decision|agreed that|we agreed)\s*[\:\-]?\s*(.{0,150})", "decision", "Customer decision"),
                (r"(?i)(?:action item|todo|follow[- ]?up)\s*[\:\-]?\s*(.{0,150})", "action_item", "Action item"),
            ]:
                for match in re.finditer(pattern, text):
                    snippet = match.group(0).strip()
                    evidence.append(
                        ExternalEvidence(
                            source="gong",
                            source_id=call_id,
                            retrieved_at=retrieved,
                            account_name=account_name,
                            opportunity_name=opportunity_name,
                            direct_customer=True,
                            fact_type=fact_type,
                            fact={"title": label, "description": snippet},
                            raw=snippet,
                            status="ok",
                            note=f"Extracted from Gong call {call_id}",
                        )
                    )

    return evidence


def _gong_transcript_text(transcript_obj: Any) -> str:
    """Best-effort flatten a Gong transcript payload into a single string."""
    if not isinstance(transcript_obj, dict):
        return str(transcript_obj) if transcript_obj else ""

    # Handle { transcript: { monologues: [...] } }
    transcript = transcript_obj.get("transcript") or transcript_obj
    if isinstance(transcript, list):
        parts: list[str] = []
        for item in transcript:
            if isinstance(item, dict):
                speaker = item.get("speakerId") or item.get("speaker") or ""
                text = item.get("text") or ""
                parts.append(f"{speaker}: {text}".strip(": "))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)

    if isinstance(transcript, dict):
        monologues = transcript.get("monologues") or transcript.get("speakers") or []
        if isinstance(monologues, list):
            parts = []
            for m in monologues:
                if isinstance(m, dict):
                    speaker = m.get("speakerId") or m.get("speaker") or ""
                    text = m.get("text") or ""
                    parts.append(f"{speaker}: {text}".strip(": "))
            return "\n".join(parts)
        return json.dumps(transcript, indent=2)

    return str(transcript)


def _unavailable_evidence(source: str, note: str) -> ExternalEvidence:
    return ExternalEvidence(
        source=source,
        source_id=None,
        retrieved_at=_now_iso(),
        account_name=None,
        opportunity_name=None,
        direct_customer=False,
        fact_type="note",
        fact={"reason": note},
        status="unavailable",
        note=note,
    )


def _write_evidence(path: Path, evidence: list[ExternalEvidence]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps([e.model_dump(exclude_none=True) for e in evidence], indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize raw MCP output to ExternalEvidence for pov-gsheet.")
    parser.add_argument("--source", required=True, choices=["salesforce", "gong", "granola", "gmail", "slack"])
    parser.add_argument("--account", required=True, help="Account name")
    parser.add_argument("--opportunity", default=None, help="Opportunity name")
    parser.add_argument("--retrieved-at", default=None, help="ISO-8601 retrieval timestamp")
    parser.add_argument("--raw-input", default=None, help="Raw MCP/tool output JSON file")
    parser.add_argument("--transcripts", default=None, help="For Gong: JSON object mapping callId -> transcript payload")
    parser.add_argument("--status", default="ok", choices=["ok", "unavailable", "skipped", "failed"])
    parser.add_argument("--note", default="", help="Human-readable provenance or failure reason")
    parser.add_argument("--out", required=True, help="Output ExternalEvidence JSON file")
    args = parser.parse_args()

    raw: Any = None
    if args.raw_input:
        try:
            raw = json.loads(Path(args.raw_input).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            _write_evidence(
                Path(args.out),
                [_unavailable_evidence(args.source, f"Could not read raw input: {e}")],
            )
            sys.exit(0)

    if args.status != "ok":
        evidence = [_unavailable_evidence(args.source, args.note or args.status)]
    elif args.source == "salesforce":
        evidence = normalize_salesforce(raw, args.account, args.opportunity, args.retrieved_at)
        if not evidence and not args.note:
            evidence = [_unavailable_evidence(args.source, "No Salesforce records matched the account/opportunity")]
    elif args.source == "gong":
        transcripts: dict[str, Any] | None = None
        if args.transcripts:
            try:
                transcripts = json.loads(Path(args.transcripts).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                _write_evidence(
                    Path(args.out),
                    [_unavailable_evidence(args.source, f"Could not read transcripts: {e}")],
                )
                sys.exit(0)
        evidence = normalize_gong(raw, transcripts, args.account, args.opportunity, args.retrieved_at)
        if not evidence and not args.note:
            evidence = [_unavailable_evidence(args.source, "No Gong calls matched the account/opportunity")]
    else:
        # Granola/Gmail/Slack normalization left to future work; treat raw as already normalized evidence list.
        if isinstance(raw, list):
            evidence = [ExternalEvidence.model_validate(item) for item in raw if isinstance(item, dict)]
        else:
            evidence = [_unavailable_evidence(args.source, args.note or f"{args.source} raw output not normalized")]

    _write_evidence(Path(args.out), evidence)


if __name__ == "__main__":
    main()
