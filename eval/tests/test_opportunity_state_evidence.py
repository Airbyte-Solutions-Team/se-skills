from __future__ import annotations

import pytest

from services.transcription_service import TranscriptionError, TranscriptionService
from webapp.config import _safe


def _service(tmp_path) -> TranscriptionService:
    customers = tmp_path / "customers"
    customers.mkdir(parents=True)
    return TranscriptionService(
        customers_dir=customers,
        workspace=tmp_path,
        safe_name=_safe,
        titlecase=lambda value: value,
        whisper_model="tiny",
    )


def _write(tmp_path, name: str, content: str = "[12:00:00] Customer: synthetic evidence") -> None:
    directory = tmp_path / "customers" / "_transcripts"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(content, encoding="utf-8")


def test_explicit_opaque_selection_hashes_exact_bytes(tmp_path) -> None:
    service = _service(tmp_path)
    _write(tmp_path, "Acme-09.17.26.txt")
    listed = service.list_evidence_transcripts("Acme")
    assert len(listed) == 1
    assert listed[0]["id"].startswith("tr_")
    assert "Acme-09.17.26.txt" not in listed[0]["id"]

    resolved = service.resolve_evidence_transcripts("Acme", [listed[0]["id"]])
    assert resolved[0].content == b"[12:00:00] Customer: synthetic evidence"
    assert resolved[0].byte_count == len(resolved[0].content)
    assert len(resolved[0].sha256) == 64


def test_selection_rejects_empty_arbitrary_path_traversal_and_tamper(tmp_path) -> None:
    service = _service(tmp_path)
    _write(tmp_path, "Acme-09.17.26.txt")
    evidence_id = service.list_evidence_transcripts("Acme")[0]["id"]
    replacement = "A" if evidence_id[-1] != "A" else "B"
    for selected in ([], ["C:/customer/transcript.txt"], ["../Acme-09.17.26.txt"], [evidence_id[:-1] + replacement]):
        with pytest.raises(TranscriptionError):
            service.resolve_evidence_transcripts("Acme", selected)


def test_cross_account_identifier_is_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    _write(tmp_path, "Acme-09.17.26.txt")
    _write(tmp_path, "Other-09.17.26.txt")
    other_id = service.list_evidence_transcripts("Other")[0]["id"]
    with pytest.raises(TranscriptionError, match="invalid or unavailable"):
        service.resolve_evidence_transcripts("Acme", [other_id])


def test_filename_does_not_infer_opportunity_membership(tmp_path) -> None:
    service = _service(tmp_path)
    _write(tmp_path, "Acme-Other-Opportunity-09.17.26.txt")
    # Eligibility is account-scoped only. No filename parser accepts or assigns
    # an opportunity; the browser must explicitly select the opaque id.
    items = service.list_evidence_transcripts("Acme")
    assert len(items) == 1
    assert service.resolve_evidence_transcripts("Acme", [items[0]["id"]])


def test_evidence_change_changes_hash_without_changing_opaque_identity(tmp_path) -> None:
    service = _service(tmp_path)
    _write(tmp_path, "Acme-09.17.26.txt", "first synthetic content")
    evidence_id = service.list_evidence_transcripts("Acme")[0]["id"]
    before = service.resolve_evidence_transcripts("Acme", [evidence_id])[0]
    _write(tmp_path, "Acme-09.17.26.txt", "changed synthetic content")
    after = service.resolve_evidence_transcripts("Acme", [evidence_id])[0]
    assert before.evidence_id == after.evidence_id
    assert before.sha256 != after.sha256
