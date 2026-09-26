"""Immutable local persistence for canonical opportunity-state versions."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from opportunity_state import (
    EvidenceManifestEntry,
    GenerationProvenance,
    OpportunityIdentity,
    OpportunityStateCandidate,
    OpportunityStateVersion,
    deterministic_change_set,
    evidence_manifest_hash,
    validate_candidate_evidence,
)
from services.path_utils import resolve_within


_VERSION_FILE = re.compile(r"^(?P<revision>\d{8})-(?P<version>[a-f0-9]{32})\.json$")
_MAX_STATE_FILE_BYTES = 1_000_000


class OpportunityStateError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str = "state_error") -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(detail)


class OpportunityStateService:
    """Store validated state separately from generated output artifacts."""

    STATE_DIR_NAME = ".opportunity-state"

    def __init__(self, customers_dir: Path, *, safe_name: Callable[[str], str]) -> None:
        self.customers_dir = Path(customers_dir)
        self._safe_name = safe_name
        self._lock = threading.Lock()

    def _forgotten_ids(self) -> set[str]:
        directory = resolve_within(self.customers_dir, Path(".command-center") / "forgotten")
        return {path.stem for path in directory.glob("src_*.json")} if directory.is_dir() else set()

    def _is_withheld(self, version: OpportunityStateVersion, forgotten: set[str] | None = None) -> bool:
        forgotten = self._forgotten_ids() if forgotten is None else forgotten
        return any(
            entry.source_type.value == "transcript"
            and any(entry.source_id.startswith(f"{source_id}_r") for source_id in forgotten)
            for entry in version.evidence_manifest
        )

    def _visible_history(self, versions: list[OpportunityStateVersion]) -> list[OpportunityStateVersion]:
        forgotten = self._forgotten_ids()
        if not forgotten:
            return versions
        visible: list[OpportunityStateVersion] = []
        tainted: set[str] = set()
        for version in versions:
            cited = {
                source_id for source_id in forgotten
                if any(entry.source_type.value == "transcript" and entry.source_id.startswith(f"{source_id}_r")
                       for entry in version.evidence_manifest)
            }
            tainted.update(cited)
            if version.provenance.runtime == "gmail_forget":
                for source_id in forgotten - cited:
                    if version.provenance.updater_version == f"gmail-forget-v1-{source_id}":
                        tainted.discard(source_id)
            if not tainted:
                visible.append(version)
        return visible

    def _opportunity_dir(self, account: str, opp_slug: str, *, create: bool = False) -> Path:
        account = self._safe_name(account)
        opp_slug = self._safe_name(opp_slug)
        account_dir = resolve_within(self.customers_dir, account)
        if not account_dir.is_dir():
            raise OpportunityStateError(404, "Unknown account", code="unknown_account")
        opportunity_dir = resolve_within(account_dir, Path("opportunities") / opp_slug)
        if create:
            opportunity_dir.mkdir(parents=True, exist_ok=True)
        return opportunity_dir

    def _paths(self, account: str, opp_slug: str, *, create: bool = False) -> tuple[Path, Path, Path]:
        opportunity_dir = self._opportunity_dir(account, opp_slug, create=create)
        state_dir = resolve_within(opportunity_dir, self.STATE_DIR_NAME)
        versions_dir = resolve_within(state_dir, "versions")
        pointer = resolve_within(state_dir, "current.json")
        if state_dir.exists() and state_dir.is_symlink():
            raise OpportunityStateError(409, "Opportunity state storage is unsafe.", code="malformed_storage")
        if versions_dir.exists() and versions_dir.is_symlink():
            raise OpportunityStateError(409, "Opportunity state storage is unsafe.", code="malformed_storage")
        if create:
            versions_dir.mkdir(parents=True, exist_ok=True)
        return state_dir, versions_dir, pointer

    @staticmethod
    def _canonical_bytes(value: Any) -> bytes:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _read_json_file(path: Path) -> Any:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OpportunityStateError(409, "Opportunity state storage is malformed.", code="malformed_storage")
            if info.st_size <= 0 or info.st_size > _MAX_STATE_FILE_BYTES:
                raise OpportunityStateError(409, "Opportunity state storage is malformed.", code="malformed_storage")
            return json.loads(path.read_text(encoding="utf-8"))
        except OpportunityStateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise OpportunityStateError(
                409, "Opportunity state storage is malformed.", code="malformed_storage"
            ) from exc

    def _read_current(self, account: str, opp_slug: str) -> OpportunityStateVersion | None:
        _state_dir, versions_dir, pointer = self._paths(account, opp_slug)
        if not pointer.exists():
            if versions_dir.exists() and any(versions_dir.iterdir()):
                raise OpportunityStateError(
                    409, "Opportunity state storage has no valid current pointer.", code="malformed_storage"
                )
            return None
        raw_pointer = self._read_json_file(pointer)
        if not isinstance(raw_pointer, dict) or set(raw_pointer) != {
            "schema_version", "revision", "filename", "checksum"
        }:
            raise OpportunityStateError(409, "Opportunity state pointer is malformed.", code="malformed_storage")
        if raw_pointer.get("schema_version") != 1 or not isinstance(raw_pointer.get("revision"), int):
            raise OpportunityStateError(409, "Opportunity state pointer is malformed.", code="malformed_storage")
        filename = raw_pointer.get("filename")
        checksum = raw_pointer.get("checksum")
        if not isinstance(filename, str) or not _VERSION_FILE.fullmatch(filename):
            raise OpportunityStateError(409, "Opportunity state pointer is malformed.", code="malformed_storage")
        if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise OpportunityStateError(409, "Opportunity state pointer is malformed.", code="malformed_storage")
        version_path = resolve_within(versions_dir, filename)
        if not version_path.exists():
            raise OpportunityStateError(409, "Opportunity state version is unavailable.", code="malformed_storage")
        version = self._read_version_path(account, opp_slug, version_path)
        match = _VERSION_FILE.fullmatch(filename)
        if version.revision != raw_pointer["revision"] or version.revision != int(match.group("revision")):
            raise OpportunityStateError(409, "Opportunity state revision mismatch.", code="malformed_storage")
        if version.version_id != match.group("version"):
            raise OpportunityStateError(409, "Opportunity state identity mismatch.", code="malformed_storage")
        raw_envelope = self._read_json_file(version_path)
        if checksum != raw_envelope.get("checksum"):
            raise OpportunityStateError(409, "Opportunity state checksum mismatch.", code="malformed_storage")
        return version

    def _read_version_path(self, account: str, opp_slug: str, version_path: Path) -> OpportunityStateVersion:
        """Read one immutable version envelope and enforce checksum and scope."""
        match = _VERSION_FILE.fullmatch(version_path.name)
        if match is None:
            raise OpportunityStateError(409, "Opportunity state version is malformed.", code="malformed_storage")
        raw_envelope = self._read_json_file(version_path)
        if not isinstance(raw_envelope, dict) or set(raw_envelope) != {"version", "checksum"}:
            raise OpportunityStateError(409, "Opportunity state version is malformed.", code="malformed_storage")
        encoded = self._canonical_bytes(raw_envelope.get("version"))
        actual_checksum = hashlib.sha256(encoded).hexdigest()
        if raw_envelope.get("checksum") != actual_checksum:
            raise OpportunityStateError(409, "Opportunity state checksum mismatch.", code="malformed_storage")
        try:
            version = OpportunityStateVersion.model_validate(raw_envelope["version"])
        except (TypeError, ValueError) as exc:
            raise OpportunityStateError(409, "Opportunity state version is invalid.", code="malformed_storage") from exc
        if version.revision != int(match.group("revision")):
            raise OpportunityStateError(409, "Opportunity state revision mismatch.", code="malformed_storage")
        if version.version_id != match.group("version"):
            raise OpportunityStateError(409, "Opportunity state identity mismatch.", code="malformed_storage")
        if (
            version.identity.account != self._safe_name(account)
            or version.identity.opportunity_slug != self._safe_name(opp_slug)
        ):
            raise OpportunityStateError(409, "Opportunity state scope mismatch.", code="malformed_storage")
        return version

    def read_current(self, account: str, opp_slug: str) -> OpportunityStateVersion | None:
        version = self._read_current(account, opp_slug)
        if version is None or not self._forgotten_ids():
            return version
        if version is not None and version not in self._visible_history(self._read_history_unfiltered(account, opp_slug)):
            raise OpportunityStateError(410, "Overview is withheld while forgotten Gmail evidence is removed.", code="source_forgotten")
        return version

    def inspect_current(self, account: str, opp_slug: str) -> dict[str, Any]:
        try:
            current = self.read_current(account, opp_slug)
        except OpportunityStateError as exc:
            if exc.code == "source_forgotten":
                return {"available": False, "status": "withheld"}
            if exc.code != "malformed_storage":
                raise
            return {"available": False, "status": "malformed"}
        if current is None:
            return {"available": False, "status": "not_created"}
        return {
            "available": True,
            "status": "current",
            "metadata": {
                "version_id": current.version_id,
                "revision": current.revision,
                "schema_version": current.schema_version,
                "created_at": current.created_at.isoformat(),
                "evidence_manifest_hash": current.evidence_manifest_hash,
                "evidence_count": len(current.evidence_manifest),
                "updater_version": current.provenance.updater_version,
            },
            "current": current.model_dump(mode="json"),
        }

    def _read_history_unfiltered(self, account: str, opp_slug: str) -> list[OpportunityStateVersion]:
        """Load and validate the complete immutable chain oldest first."""
        _state_dir, versions_dir, _pointer = self._paths(account, opp_slug)
        current = self._read_current(account, opp_slug)
        if current is None:
            return []
        entries = list(versions_dir.iterdir())
        if any(not _VERSION_FILE.fullmatch(path.name) for path in entries):
            raise OpportunityStateError(409, "Opportunity state history is malformed.", code="malformed_storage")
        paths = sorted(
            entries,
            key=lambda path: int(_VERSION_FILE.fullmatch(path.name).group("revision")),
        )
        versions = [self._read_version_path(account, opp_slug, path) for path in paths]
        if not versions or versions[-1].version_id != current.version_id:
            raise OpportunityStateError(409, "Opportunity state history is malformed.", code="malformed_storage")
        for index, version in enumerate(versions):
            if version.revision != index + 1:
                raise OpportunityStateError(409, "Opportunity state history has a revision gap.", code="malformed_storage")
            if index == 0:
                continue
            parent = versions[index - 1]
            if version.parent_revision != parent.revision or version.parent_version_id != parent.version_id:
                raise OpportunityStateError(409, "Opportunity state history relationship is invalid.", code="malformed_storage")
        return versions

    def read_history(self, account: str, opp_slug: str) -> list[OpportunityStateVersion]:
        """Return only revisions that do not cite forgotten evidence."""
        return self._visible_history(self._read_history_unfiltered(account, opp_slug))

    def list_history(self, account: str, opp_slug: str) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for version in reversed(self.read_history(account, opp_slug)):
            summaries.append({
                "version_id": version.version_id,
                "revision": version.revision,
                "created_at": version.created_at.isoformat(),
                "parent_revision": version.parent_revision,
                "evidence_count": len(version.evidence_manifest),
                "evidence_manifest_hash": version.evidence_manifest_hash,
                "provenance": version.provenance.model_dump(mode="json"),
                "change_counts": version.change_set.high_level_counts() if version.change_set else {},
            })
        return summaries

    def read_version(self, account: str, opp_slug: str, revision: int) -> OpportunityStateVersion:
        if revision < 1 or revision > 1_000_000:
            raise OpportunityStateError(404, "Unknown opportunity state revision.", code="unknown_version")
        for version in self.read_history(account, opp_slug):
            if version.revision == revision:
                return version
        raise OpportunityStateError(404, "Unknown opportunity state revision.", code="unknown_version")

    @staticmethod
    def _next_revision(versions_dir: Path) -> int:
        revisions: list[int] = []
        if versions_dir.exists():
            for path in versions_dir.iterdir():
                match = _VERSION_FILE.fullmatch(path.name)
                if match:
                    revisions.append(int(match.group("revision")))
        revision = max(revisions, default=0) + 1
        if revision > 1_000_000:
            raise OpportunityStateError(409, "Opportunity state revision limit reached.", code="revision_limit")
        return revision

    def promote_create(
        self,
        *,
        identity: OpportunityIdentity,
        evidence_manifest: list[EvidenceManifestEntry],
        expected_manifest_hash: str,
        provenance: GenerationProvenance,
        candidate: OpportunityStateCandidate,
    ) -> OpportunityStateVersion:
        """Persist and atomically promote the first canonical state version."""
        validate_candidate_evidence(candidate, evidence_manifest)
        actual_manifest_hash = evidence_manifest_hash(evidence_manifest)
        if actual_manifest_hash != expected_manifest_hash:
            raise OpportunityStateError(409, "Authorized evidence changed.", code="evidence_changed")

        with self._lock:
            current = self._read_current(identity.account, identity.opportunity_slug)
            if current is not None:
                raise OpportunityStateError(
                    409, "An overview already exists; Update Overview belongs to Slice 2B.", code="already_created"
                )
            _state_dir, versions_dir, pointer = self._paths(
                identity.account, identity.opportunity_slug, create=True
            )
            revision = self._next_revision(versions_dir)
            version = OpportunityStateVersion(
                schema_version=1,
                version_id=uuid.uuid4().hex,
                revision=revision,
                identity=identity,
                created_at=datetime.now(timezone.utc),
                evidence_manifest=evidence_manifest,
                evidence_manifest_hash=actual_manifest_hash,
                provenance=provenance,
                state=candidate,
            )
            version_payload = version.model_dump(mode="json")
            version_bytes = self._canonical_bytes(version_payload)
            checksum = hashlib.sha256(version_bytes).hexdigest()
            filename = f"{revision:08d}-{version.version_id}.json"
            version_path = resolve_within(versions_dir, filename)
            envelope = self._canonical_bytes({"version": version_payload, "checksum": checksum})
            if version_path.exists():
                raise OpportunityStateError(409, "Opportunity state version collision.", code="storage_conflict")
            self._atomic_write(version_path, envelope)
            pointer_payload = self._canonical_bytes({
                "schema_version": 1,
                "revision": revision,
                "filename": filename,
                "checksum": checksum,
            })
            self._atomic_write(pointer, pointer_payload)
            return version

    def promote_update(
        self,
        *,
        identity: OpportunityIdentity,
        expected_parent_version_id: str,
        expected_parent_revision: int,
        evidence_manifest: list[EvidenceManifestEntry],
        expected_manifest_hash: str,
        provenance: GenerationProvenance,
        candidate: OpportunityStateCandidate,
    ) -> OpportunityStateVersion:
        """Atomically promote a later revision only from the exact current base."""
        validate_candidate_evidence(candidate, evidence_manifest)
        actual_manifest_hash = evidence_manifest_hash(evidence_manifest)
        if actual_manifest_hash != expected_manifest_hash:
            raise OpportunityStateError(409, "Authorized evidence changed.", code="evidence_changed")

        with self._lock:
            parent = self._read_current(identity.account, identity.opportunity_slug)
            if (
                parent is None
                or parent.version_id != expected_parent_version_id
                or parent.revision != expected_parent_revision
            ):
                raise OpportunityStateError(409, "The overview changed while this update ran.", code="stale_base")
            _state_dir, versions_dir, pointer = self._paths(
                identity.account, identity.opportunity_slug, create=True
            )
            revision = self._next_revision(versions_dir)
            if revision != parent.revision + 1:
                raise OpportunityStateError(409, "Opportunity state history is inconsistent.", code="malformed_storage")
            version_id = uuid.uuid4().hex
            change_set = deterministic_change_set(
                parent=parent,
                child_version_id=version_id,
                child_revision=revision,
                child_state=candidate,
                child_manifest=evidence_manifest,
            )
            version = OpportunityStateVersion(
                schema_version=1,
                version_id=version_id,
                revision=revision,
                identity=identity,
                created_at=datetime.now(timezone.utc),
                evidence_manifest=evidence_manifest,
                evidence_manifest_hash=actual_manifest_hash,
                provenance=provenance,
                state=candidate,
                parent_version_id=parent.version_id,
                parent_revision=parent.revision,
                change_set=change_set,
            )
            version_payload = version.model_dump(mode="json")
            version_bytes = self._canonical_bytes(version_payload)
            checksum = hashlib.sha256(version_bytes).hexdigest()
            filename = f"{revision:08d}-{version.version_id}.json"
            version_path = resolve_within(versions_dir, filename)
            if version_path.exists():
                raise OpportunityStateError(409, "Opportunity state version collision.", code="storage_conflict")
            self._atomic_write(
                version_path,
                self._canonical_bytes({"version": version_payload, "checksum": checksum}),
            )
            try:
                self._atomic_write(pointer, self._canonical_bytes({
                    "schema_version": 1,
                    "revision": revision,
                    "filename": filename,
                    "checksum": checksum,
                }))
            except OSError:
                # The immutable child is not committed until the current pointer
                # is durable. Remove only the version created by this call.
                try:
                    version_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return version
