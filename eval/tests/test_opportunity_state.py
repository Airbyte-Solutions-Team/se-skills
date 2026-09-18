from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from opportunity_state import (
    EvidenceManifestEntry,
    EvidenceSourceType,
    GenerationProvenance,
    OpportunityIdentity,
    OpportunityStateCandidate,
    OpportunityStateChangeSet,
    deterministic_change_set,
    evidence_manifest_hash,
    validate_candidate_evidence,
)
from services.opportunity_state_service import OpportunityStateError, OpportunityStateService
from webapp.config import _safe
from eval.tests.opportunity_state_helpers import METADATA_ID, NOW, TRANSCRIPT_ID, candidate


def manifest() -> list[EvidenceManifestEntry]:
    return [
        EvidenceManifestEntry(
            source_type=EvidenceSourceType.OPPORTUNITY_METADATA,
            source_id=METADATA_ID,
            sha256="a" * 64,
            byte_count=100,
            observed_at=NOW,
            display_name="Opportunity metadata",
        ),
        EvidenceManifestEntry(
            source_type=EvidenceSourceType.TRANSCRIPT,
            source_id=TRANSCRIPT_ID,
            sha256="b" * 64,
            byte_count=200,
            observed_at=NOW,
            display_name="Synthetic-09.17.26.txt",
        ),
    ]


def test_candidate_forbids_extra_fields_and_oversized_text() -> None:
    payload = candidate().model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        OpportunityStateCandidate.model_validate(payload)

    payload = candidate().model_dump(mode="json")
    payload["brief"]["customer_objective"]["value"] = "x" * 2_001
    with pytest.raises(ValidationError):
        OpportunityStateCandidate.model_validate(payload)


def test_candidate_rejects_invalid_enum_duplicate_key_and_timestamp() -> None:
    payload = candidate().model_dump(mode="json")
    payload["health_indicators"][0]["status"] = "excellent"
    with pytest.raises(ValidationError):
        OpportunityStateCandidate.model_validate(payload)

    payload = candidate().model_dump(mode="json")
    payload["recommended_actions"].append(dict(payload["recommended_actions"][0]))
    with pytest.raises(ValidationError, match="duplicate recommended action key"):
        OpportunityStateCandidate.model_validate(payload)

    payload = candidate().model_dump(mode="json")
    payload["brief"]["customer_objective"]["last_confirmed_at"] = "2026-09-17T12:00:00"
    with pytest.raises(ValidationError, match="timezone"):
        OpportunityStateCandidate.model_validate(payload)


def test_candidate_rejects_unauthorized_evidence_reference() -> None:
    payload = candidate().model_dump(mode="json")
    payload["brief"]["customer_objective"]["evidence_refs"][0]["source_id"] = "tr_tampered-identifier"
    parsed = OpportunityStateCandidate.model_validate(payload)
    with pytest.raises(ValueError, match="unauthorized evidence"):
        validate_candidate_evidence(parsed, manifest())


def test_manifest_hash_is_order_independent_and_content_sensitive() -> None:
    entries = manifest()
    assert evidence_manifest_hash(entries) == evidence_manifest_hash(list(reversed(entries)))
    changed = [entry.model_copy(deep=True) for entry in entries]
    changed[1].sha256 = "c" * 64
    assert evidence_manifest_hash(entries) != evidence_manifest_hash(changed)
    relabeled = [entry.model_copy(deep=True) for entry in entries]
    relabeled[1].display_name = "Different safe label"
    assert evidence_manifest_hash(entries) == evidence_manifest_hash(relabeled)


def _service(tmp_path) -> OpportunityStateService:
    customers = tmp_path / "customers"
    (customers / "Acme").mkdir(parents=True)
    return OpportunityStateService(customers, safe_name=_safe)


def _promote(service: OpportunityStateService):
    entries = manifest()
    return service.promote_create(
        identity=OpportunityIdentity(
            account="Acme", opportunity_slug="synthetic-opportunity", opportunity_name="Synthetic Opportunity"
        ),
        evidence_manifest=entries,
        expected_manifest_hash=evidence_manifest_hash(entries),
        provenance=GenerationProvenance(
            updater_version="test-v1", model="fake", runtime="fake", cli_version="2.1.272"
        ),
        candidate=candidate(),
    )


def test_immutable_version_storage_and_create_once(tmp_path) -> None:
    service = _service(tmp_path)
    version = _promote(service)
    assert version.revision == 1
    loaded = service.read_current("Acme", "synthetic-opportunity")
    assert loaded == version
    state_dir = tmp_path / "customers" / "Acme" / "opportunities" / "synthetic-opportunity" / ".opportunity-state"
    versions = list((state_dir / "versions").glob("*.json"))
    assert len(versions) == 1
    with pytest.raises(OpportunityStateError, match="already exists"):
        _promote(service)
    assert len(list((state_dir / "versions").glob("*.json"))) == 1


def test_malformed_pointer_and_checksum_degrade_safely(tmp_path) -> None:
    service = _service(tmp_path)
    _promote(service)
    state_dir = tmp_path / "customers" / "Acme" / "opportunities" / "synthetic-opportunity" / ".opportunity-state"
    pointer = state_dir / "current.json"
    pointer.write_text('{"filename":"../escape"}', encoding="utf-8")
    assert service.inspect_current("Acme", "synthetic-opportunity") == {
        "available": False, "status": "malformed"
    }
    with pytest.raises(OpportunityStateError, match="pointer"):
        service.read_current("Acme", "synthetic-opportunity")


def test_restart_round_trip_and_orphan_version_recovery_is_fail_closed(tmp_path) -> None:
    service = _service(tmp_path)
    promoted = _promote(service)
    restarted = OpportunityStateService(tmp_path / "customers", safe_name=_safe)
    assert restarted.read_current("Acme", "synthetic-opportunity") == promoted

    state_dir = tmp_path / "customers" / "Acme" / "opportunities" / "synthetic-opportunity" / ".opportunity-state"
    (state_dir / "current.json").unlink()
    assert restarted.inspect_current("Acme", "synthetic-opportunity")["status"] == "malformed"
    with pytest.raises(OpportunityStateError, match="no valid current pointer"):
        _promote(restarted)


def test_version_file_tamper_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    _promote(service)
    version_path = next((
        tmp_path / "customers" / "Acme" / "opportunities" / "synthetic-opportunity" /
        ".opportunity-state" / "versions"
    ).glob("*.json"))
    envelope = json.loads(version_path.read_text(encoding="utf-8"))
    envelope["version"]["identity"]["opportunity_name"] = "Tampered"
    version_path.write_text(json.dumps(envelope), encoding="utf-8")
    assert service.inspect_current("Acme", "synthetic-opportunity")["status"] == "malformed"


def test_checksums_cannot_move_a_version_to_another_opportunity_scope(tmp_path) -> None:
    service = _service(tmp_path)
    _promote(service)
    state_dir = (
        tmp_path / "customers" / "Acme" / "opportunities" / "synthetic-opportunity" /
        ".opportunity-state"
    )
    version_path = next((state_dir / "versions").glob("*.json"))
    envelope = json.loads(version_path.read_text(encoding="utf-8"))
    envelope["version"]["identity"]["account"] = "Other"
    encoded = service._canonical_bytes(envelope["version"])
    envelope["checksum"] = hashlib.sha256(encoded).hexdigest()
    version_path.write_bytes(service._canonical_bytes(envelope))
    pointer = json.loads((state_dir / "current.json").read_text(encoding="utf-8"))
    pointer["checksum"] = envelope["checksum"]
    (state_dir / "current.json").write_bytes(service._canonical_bytes(pointer))

    assert service.inspect_current("Acme", "synthetic-opportunity")["status"] == "malformed"


def test_legacy_revision_one_without_parent_fields_remains_valid(tmp_path) -> None:
    service = _service(tmp_path)
    promoted = _promote(service)
    state_dir = (
        tmp_path / "customers" / "Acme" / "opportunities" / "synthetic-opportunity" /
        ".opportunity-state"
    )
    path = next((state_dir / "versions").glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    for key in ("parent_version_id", "parent_revision", "change_set"):
        envelope["version"].pop(key, None)
    checksum = hashlib.sha256(service._canonical_bytes(envelope["version"])).hexdigest()
    envelope["checksum"] = checksum
    path.write_bytes(service._canonical_bytes(envelope))
    pointer = json.loads((state_dir / "current.json").read_text(encoding="utf-8"))
    pointer["checksum"] = checksum
    (state_dir / "current.json").write_bytes(service._canonical_bytes(pointer))

    loaded = OpportunityStateService(tmp_path / "customers", safe_name=_safe).read_current(
        "Acme", "synthetic-opportunity"
    )
    assert loaded.version_id == promoted.version_id
    assert loaded.parent_version_id is None
    assert loaded.change_set is None


def test_deterministic_diff_ignores_order_only_changes_and_tracks_stable_keys(tmp_path) -> None:
    service = _service(tmp_path)
    parent = _promote(service)
    reordered = candidate().model_copy(deep=True)
    reordered.health_indicators = list(reversed(reordered.health_indicators))
    changes = deterministic_change_set(
        parent=parent,
        child_version_id="c" * 32,
        child_revision=2,
        child_state=reordered,
        child_manifest=list(reversed(manifest())),
    )
    assert changes.brief == []
    assert changes.health_indicators == []
    assert changes.recommended_actions == []
    assert changes.evidence_sources == []
    assert changes.metadata_changed is False

    changed = reordered.model_copy(deep=True)
    changed.brief.current_status.value = "A materially changed current status"
    changed.recommended_actions[0].goal = "A changed definition of the desired outcome."
    diff = deterministic_change_set(
        parent=parent,
        child_version_id="d" * 32,
        child_revision=2,
        child_state=changed,
        child_manifest=manifest(),
    )
    assert [(item.key, item.fields) for item in diff.brief] == [("current_status", ["value"])]
    assert diff.recommended_actions[0].key == "confirm-connectors"
    assert diff.recommended_actions[0].fields == ["goal"]


def test_change_set_rejects_duplicate_keys_and_relationship_mismatch(tmp_path) -> None:
    parent = _promote(_service(tmp_path))
    changes = deterministic_change_set(
        parent=parent,
        child_version_id="e" * 32,
        child_revision=2,
        child_state=candidate(),
        child_manifest=manifest(),
    ).model_dump(mode="json")
    changes["brief"] = [
        {"key": "current_status", "change_type": "changed", "fields": ["value"]},
        {"key": "current_status", "change_type": "changed", "fields": ["confidence"]},
    ]
    with pytest.raises(ValidationError, match="duplicate brief change key"):
        OpportunityStateChangeSet.model_validate(changes)
    changes["brief"] = []
    changes["child_revision"] = 3
    with pytest.raises(ValidationError, match="consecutive"):
        OpportunityStateChangeSet.model_validate(changes)


def test_update_promotion_preserves_history_and_rejects_stale_base(tmp_path) -> None:
    service = _service(tmp_path)
    parent = _promote(service)
    child = service.promote_update(
        identity=parent.identity,
        expected_parent_version_id=parent.version_id,
        expected_parent_revision=parent.revision,
        evidence_manifest=manifest(),
        expected_manifest_hash=evidence_manifest_hash(manifest()),
        provenance=parent.provenance,
        candidate=candidate(),
    )
    assert child.revision == 2
    assert service.read_version("Acme", "synthetic-opportunity", 1) == parent
    assert service.read_version("Acme", "synthetic-opportunity", 2) == child
    assert [item["revision"] for item in service.list_history("Acme", "synthetic-opportunity")] == [2, 1]
    with pytest.raises(OpportunityStateError) as stale:
        service.promote_update(
            identity=parent.identity,
            expected_parent_version_id=parent.version_id,
            expected_parent_revision=1,
            evidence_manifest=manifest(),
            expected_manifest_hash=evidence_manifest_hash(manifest()),
            provenance=parent.provenance,
            candidate=candidate(),
        )
    assert stale.value.code == "stale_base"


def test_history_rejects_tampered_parent_relationship_even_with_new_checksum(tmp_path) -> None:
    service = _service(tmp_path)
    parent = _promote(service)
    service.promote_update(
        identity=parent.identity,
        expected_parent_version_id=parent.version_id,
        expected_parent_revision=1,
        evidence_manifest=manifest(),
        expected_manifest_hash=evidence_manifest_hash(manifest()),
        provenance=parent.provenance,
        candidate=candidate(),
    )
    state_dir = (
        tmp_path / "customers" / "Acme" / "opportunities" / "synthetic-opportunity" /
        ".opportunity-state"
    )
    child_path = sorted((state_dir / "versions").glob("*.json"))[-1]
    envelope = json.loads(child_path.read_text(encoding="utf-8"))
    envelope["version"]["parent_version_id"] = "f" * 32
    envelope["version"]["change_set"]["parent_version_id"] = "f" * 32
    envelope["checksum"] = hashlib.sha256(
        service._canonical_bytes(envelope["version"])
    ).hexdigest()
    child_path.write_bytes(service._canonical_bytes(envelope))
    pointer = json.loads((state_dir / "current.json").read_text(encoding="utf-8"))
    pointer["checksum"] = envelope["checksum"]
    (state_dir / "current.json").write_bytes(service._canonical_bytes(pointer))
    with pytest.raises(OpportunityStateError, match="relationship"):
        service.read_history("Acme", "synthetic-opportunity")
