"""Ledger semantics for the Command Center pilot: idempotent revisions,
conservative association with correction history, processing/access status,
workspace isolation, and restart safety. All fixtures are synthetic."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from command_center_evidence import AssociationCandidate, AssociationDecision
from integrations.granola import GranolaImportError, ManualGranolaImportAdapter
from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "command_center" / "granola"
ADAPTER = ManualGranolaImportAdapter()


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def meeting(name: str, connection: str = "local-manual"):
    return ADAPTER.normalize(fixture(name), connection_id=connection)


def ledger(tmp_path: Path, name: str = "customers") -> EvidenceLedgerService:
    (tmp_path / name).mkdir(exist_ok=True)
    return EvidenceLedgerService(tmp_path / name, clock=Clock())


async def resolve_identity(account: str, opp_slug: str) -> dict:
    if account != "Acme":
        from services.account_service import AccountError

        raise AccountError(404, "Unknown account")
    return {
        "safe_account": "Acme",
        "safe_opp": opp_slug,
        "opportunity": {"sfdc_id": "006SYNTHETIC00001", "sfdc_account_id": "001SYNTHETIC00001"},
    }


def test_import_is_idempotent_and_edits_create_new_immutable_revisions(tmp_path) -> None:
    service = ledger(tmp_path)
    first = service.import_meetings([meeting("note_synthetic_v1.json")])
    assert first["trigger"] == "manual_import"
    assert first["unattended_discovery"] is False
    result = first["results"][0]
    assert result["outcome"] == "created"
    assert result["revision"] == 1
    assert result["processing_status"] == "awaiting_association"

    duplicate = service.import_meetings([meeting("note_synthetic_v1.json")])["results"][0]
    assert duplicate["outcome"] == "duplicate"
    assert duplicate["created_revision"] is False
    assert duplicate["revision"] == 1
    assert duplicate["source_id"] == result["source_id"]

    edited = service.import_meetings([meeting("note_synthetic_v2_edited.json")])["results"][0]
    assert edited["outcome"] == "new_revision"
    assert edited["revision"] == 2
    assert edited["content_hash"] != result["content_hash"]

    detail = service.get_source(result["source_id"])
    assert [item["revision"] for item in detail["revisions"]] == [1, 2]
    assert detail["revisions"][0]["content_hash"] == result["content_hash"]
    assert detail["revisions"][1]["provider_updated_at"] == "2026-09-23T09:00:00Z"
    assert all(item["trigger"] == "manual_import" for item in detail["revisions"])

    original = service.read_content(result["source_id"], revision=1)
    assert original["summary_text"] == "Synthetic summary. Fixture only; no customer content."
    content_files = list((tmp_path / "customers" / ".command-center" / "content" / result["source_id"]).iterdir())
    assert len(content_files) == 2


def test_summaries_and_listings_never_expose_content(tmp_path) -> None:
    service = ledger(tmp_path)
    service.import_meetings([meeting("note_synthetic_v1.json")])
    serialized = json.dumps(service.list_sources()) + json.dumps(
        service.get_source(service.list_sources()["sources"][0]["source_id"])
    )
    assert "Synthetic line one" not in serialized
    assert "Synthetic summary" not in serialized
    assert "SE-SKILLS-CAPCHECK" not in serialized
    assert "example.invalid" not in serialized
    metrics = service.list_sources()["sources"][0]["latest"]["metrics"]
    assert metrics["transcript_segments"] == 2
    assert metrics["attendee_count"] == 2


@pytest.mark.asyncio
async def test_association_is_conservative_and_corrections_keep_history(tmp_path) -> None:
    service = ledger(tmp_path)
    source_id = service.import_meetings([meeting("note_synthetic_v1.json")])["results"][0]["source_id"]

    candidates = [
        AssociationCandidate(account="Acme", opportunity_slug="expansion", method="attendee", reason="domain match"),
        AssociationCandidate(account="Acme", opportunity_slug="renewal", method="title", reason="title match"),
    ]
    proposed = service.propose_association(source_id, candidates, reason="Ambiguous attendee/title match")
    assert proposed["association"]["state"] == "proposed"
    assert proposed["association"]["account"] is None
    assert len(proposed["association"]["candidates"]) == 2
    assert proposed["processing"]["status"] == "awaiting_association"

    with pytest.raises(EvidenceLedgerError) as exc:
        service.propose_association(
            source_id,
            [AssociationCandidate(account="Acme", opportunity_slug="x", method="explicit", reason="no")],
            reason="explicit cannot propose",
        )
    assert exc.value.code == "invalid_method"

    with pytest.raises(ValueError):
        AssociationDecision(
            sequence=1, state="associated", account="Acme", opportunity_slug="x", method="attendee",
            actor="system", reason="never auto", recorded_at=datetime.now(timezone.utc),
        )

    confirmed = await service.confirm_association(
        source_id, account="Acme", opportunity_slug="expansion", reason="SE confirmed",
        resolve_identity=resolve_identity,
    )
    assert confirmed["association"]["state"] == "associated"
    assert confirmed["association"]["method"] == "explicit"
    assert confirmed["association"]["actor"] == "local_user"
    assert confirmed["association"]["crm_opportunity_id"] == "006SYNTHETIC00001"
    assert confirmed["association"]["supersedes_sequence"] is None
    assert confirmed["processing"]["status"] == "queued"

    again = await service.confirm_association(
        source_id, account="Acme", opportunity_slug="expansion", reason="repeat",
        resolve_identity=resolve_identity,
    )
    assert again["association"]["sequence"] == confirmed["association"]["sequence"]

    service.mark_processing(source_id)
    service.mark_processed(source_id, revision=1)
    assert service.get_source(source_id)["processing"]["status"] == "processed"

    corrected = await service.confirm_association(
        source_id, account="Acme", opportunity_slug="renewal", reason="Wrong opportunity",
        resolve_identity=resolve_identity,
    )
    assert corrected["association"]["opportunity_slug"] == "renewal"
    assert corrected["association"]["supersedes_sequence"] == confirmed["association"]["sequence"]
    assert corrected["processing"]["status"] == "queued"
    assert corrected["processing"]["processed_revision"] is None

    history = service.get_source(source_id)["association_history"]
    assert [item["state"] for item in history] == ["unassociated", "proposed", "associated", "associated"]
    assert history[2]["opportunity_slug"] == "expansion"

    cleared = service.clear_association(source_id, reason="Not a customer meeting")
    assert cleared["association"]["state"] == "unassociated"
    assert cleared["association"]["supersedes_sequence"] == corrected["association"]["sequence"]
    assert cleared["processing"]["status"] == "awaiting_association"
    assert len(service.get_source(source_id)["association_history"]) == 5

    from services.account_service import AccountError

    with pytest.raises(AccountError):
        await service.confirm_association(
            source_id, account="Unknown", opportunity_slug="x", reason="bad",
            resolve_identity=resolve_identity,
        )


@pytest.mark.asyncio
async def test_processing_lifecycle_retry_and_superseded_revisions(tmp_path) -> None:
    service = ledger(tmp_path)
    source_id = service.import_meetings([meeting("note_synthetic_v1.json")])["results"][0]["source_id"]
    with pytest.raises(EvidenceLedgerError) as exc:
        service.mark_processing(source_id)
    assert exc.value.code == "not_queued"

    await service.confirm_association(
        source_id, account="Acme", opportunity_slug="expansion", reason="ok", resolve_identity=resolve_identity
    )
    assert service.mark_processing(source_id)["processing"]["attempts"] == 1
    failed = service.mark_failed(source_id, error_code="model_timeout", retry_eligible=True)
    assert failed["processing"]["status"] == "failed"
    assert service.unprocessed_sources()["counts_by_status"] == {"failed": 1}

    retried = service.retry(source_id)
    assert retried["processing"]["status"] == "queued"
    assert service.retry(source_id)["processing"]["status"] == "queued"

    service.mark_processing(source_id)
    service.import_meetings([meeting("note_synthetic_v2_edited.json")])
    stale = service.mark_processed(source_id, revision=1)
    assert stale["outcome"] == "superseded"
    assert stale["processing"]["status"] == "queued"
    assert stale["processing"]["processed_revision"] is None

    service.mark_processing(source_id)
    done = service.mark_processed(source_id, revision=2)
    assert done["outcome"] == "processed"
    assert done["processing"]["processed_revision"] == 2
    assert service.unprocessed_sources()["total"] == 0

    with pytest.raises(EvidenceLedgerError) as exc:
        service.mark_failed(source_id, error_code="x", retry_eligible=True)
    assert exc.value.code == "not_processing"


def test_missing_content_and_access_loss_are_visible_not_fatal(tmp_path) -> None:
    service = ledger(tmp_path)
    results = service.import_meetings([
        meeting("note_synthetic_transcript_too_large.json"),
        meeting("note_synthetic_access_lost.json"),
    ])["results"]
    assert [item["availability"] for item in results] == ["metadata_only", "access_lost"]
    assert all(item["processing_status"] == "awaiting_association" for item in results)
    too_large = service.get_source(results[0]["source_id"])
    assert too_large["latest"]["unavailable_reason"] == "transcript_too_large"
    with pytest.raises(EvidenceLedgerError) as exc:
        service.read_content(results[0]["source_id"], revision=1)
    assert exc.value.code == "content_unavailable"

    # Content later becomes retrievable: a new revision, the old one stays.
    full = fixture("note_synthetic_v1.json")
    full["id"] = "not_SYNTH000000002"
    later = service.import_meetings([ADAPTER.normalize(full, connection_id="local-manual")])["results"][0]
    assert later["source_id"] == results[0]["source_id"]
    assert later["revision"] == 2
    assert later["availability"] == "content_available"

    # Content file removed on disk after import surfaces as a checked error.
    content_dir = tmp_path / "customers" / ".command-center" / "content" / later["source_id"]
    for path in content_dir.iterdir():
        path.unlink()
    with pytest.raises(EvidenceLedgerError) as exc:
        service.read_content(later["source_id"], revision=2)
    assert exc.value.code == "content_missing"


def test_malformed_payloads_are_rejected_before_any_write(tmp_path) -> None:
    service = ledger(tmp_path)
    with pytest.raises(GranolaImportError) as exc:
        ADAPTER.normalize(fixture("note_malformed.json"), connection_id="local-manual")
    assert exc.value.code == "malformed_payload"
    with pytest.raises(GranolaImportError):
        ADAPTER.normalize({**fixture("note_synthetic_v1.json"), "unexpected_field": 1}, connection_id="c")
    with pytest.raises(GranolaImportError):
        ADAPTER.normalize({**fixture("note_synthetic_v1.json"), "web_url": "http://insecure.invalid"}, connection_id="c")
    with pytest.raises(GranolaImportError) as exc:
        ADAPTER.normalize({"id": "not_SYNTH000000009", "error_code": "SOMETHING_ELSE"}, connection_id="c")
    assert exc.value.code == "malformed_payload"
    with pytest.raises(GranolaImportError) as exc:
        ADAPTER.normalize(
            {**fixture("note_synthetic_v1.json"), "summary_text": "x" * 2_100_000}, connection_id="c"
        )
    assert exc.value.code == "payload_too_large"
    with pytest.raises(GranolaImportError):
        ADAPTER.normalize(["not", "an", "object"], connection_id="c")  # type: ignore[arg-type]
    assert not (tmp_path / "customers" / ".command-center").exists()
    assert service.list_sources()["total"] == 0


def test_workspace_isolation_by_scope_and_connection(tmp_path) -> None:
    first = ledger(tmp_path, "workspace-a")
    second = ledger(tmp_path, "workspace-b")
    a = first.import_meetings([meeting("note_synthetic_v1.json")])["results"][0]
    b = second.import_meetings([meeting("note_synthetic_v1.json")])["results"][0]
    assert a["source_id"] != b["source_id"]
    assert first.scope() != second.scope()
    with pytest.raises(EvidenceLedgerError) as exc:
        first.get_source(b["source_id"])
    assert exc.value.code == "unknown_source"

    # Copying another workspace's record into this ledger is detected, not trusted.
    foreign = tmp_path / "workspace-b" / ".command-center" / "sources" / f"{b['source_id']}.json"
    target = tmp_path / "workspace-a" / ".command-center" / "sources" / f"{b['source_id']}.json"
    target.write_bytes(foreign.read_bytes())
    with pytest.raises(EvidenceLedgerError) as exc:
        first.get_source(b["source_id"])
    assert exc.value.code == "scope_mismatch"
    listing = first.list_sources()
    assert listing["total"] == 1
    assert listing["malformed_records"] == 1

    # A different connection to the same provider object is a different source.
    other_connection = first.import_meetings([meeting("note_synthetic_v1.json", "second-account")])["results"][0]
    assert other_connection["source_id"] != a["source_id"]

    # Pointing a service at storage created for another path is refused.
    moved = tmp_path / "moved"
    (tmp_path / "workspace-b").rename(moved)
    with pytest.raises(EvidenceLedgerError) as exc:
        EvidenceLedgerService(moved).list_sources()
    assert exc.value.code == "scope_mismatch"


@pytest.mark.asyncio
async def test_restart_reloads_state_and_rejects_tampered_records(tmp_path) -> None:
    service = ledger(tmp_path)
    source_id = service.import_meetings([meeting("note_synthetic_v1.json")])["results"][0]["source_id"]
    await service.confirm_association(
        source_id, account="Acme", opportunity_slug="expansion", reason="ok", resolve_identity=resolve_identity
    )
    service.mark_processing(source_id)

    restarted = EvidenceLedgerService(tmp_path / "customers", clock=Clock())
    loaded = restarted.get_source(source_id)
    assert loaded["processing"]["status"] == "processing"
    assert loaded["association"]["opportunity_slug"] == "expansion"
    assert restarted.unprocessed_sources()["counts_by_status"] == {"processing": 1}
    assert restarted.read_content(source_id, revision=1)["transcript"][0]["text"] == "Synthetic line one."
    duplicate = restarted.import_meetings([meeting("note_synthetic_v1.json")])["results"][0]
    assert duplicate["outcome"] == "duplicate"

    record = tmp_path / "customers" / ".command-center" / "sources" / f"{source_id}.json"
    raw = json.loads(record.read_text())
    raw["source"]["association"]["opportunity_slug"] = "renewal"
    record.write_text(json.dumps(raw))
    with pytest.raises(EvidenceLedgerError) as exc:
        restarted.get_source(source_id)
    assert exc.value.code == "malformed_storage"

    leftover = record.with_name(f".{record.name}.deadbeef.tmp")
    leftover.write_text("{")
    assert restarted.list_sources()["malformed_records"] == 1


def test_observed_mcp_meeting_shape_imports_without_edit_signal(tmp_path) -> None:
    service = ledger(tmp_path)
    normalized = meeting("mcp_meeting_synthetic.json")
    assert normalized.identity.provider_object_id == "0f9c4b2e-7d31-4c8a-9e5b-1a2b3c4d5e6f"
    assert normalized.provider_updated_at is None
    assert normalized.occurred_at is not None
    result = service.import_meetings([normalized])["results"][0]
    assert result["availability"] == "content_available"
    latest = service.get_source(result["source_id"])["latest"]
    assert latest["provider_updated_at"] is None
    assert latest["metrics"]["transcript_segments"] == 1

    # Same meeting re-imported after an edit: only the content hash reveals it.
    edited = fixture("mcp_meeting_synthetic.json")
    edited["summary"] = "Synthetic MCP summary, edited later. Fixture only."
    again = service.import_meetings([ADAPTER.normalize(edited, connection_id="local-manual")])["results"][0]
    assert again["outcome"] == "new_revision"
    assert again["revision"] == 2

    with pytest.raises(GranolaImportError):
        ADAPTER.normalize({**fixture("mcp_meeting_synthetic.json"), "updated_at": "2026-09-24T00:00:00Z"}, connection_id="c")
    with pytest.raises(GranolaImportError):
        ADAPTER.normalize({**fixture("mcp_meeting_synthetic.json"), "transcript": [{"text": "x"}]}, connection_id="c")
