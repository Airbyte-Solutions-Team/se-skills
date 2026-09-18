"""Consent-gated updates and server-authoritative freshness for Opportunity Overview."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from opportunity_state import (
    EvidenceManifestEntry,
    EvidenceSourceType,
    GenerationProvenance,
    OpportunityIdentity,
    OpportunityStateVersion,
    evidence_manifest_hash,
    validate_candidate_evidence,
)
from services.job_service import JobService, ManagedJobError
from services.opportunity_state_create_service import METADATA_SOURCE_ID, OpportunityStateCreateService
from services.opportunity_state_executor import (
    UPDATER_VERSION,
    CanonicalStateExecutionError,
    CanonicalStateExecutionRequest,
    CanonicalStateExecutor,
)
from services.opportunity_state_service import OpportunityStateError, OpportunityStateService
from services.opportunity_workspace_service import OpportunityWorkspaceService
from services.transcription_service import ResolvedTranscriptEvidence, TranscriptionError, TranscriptionService


class OpportunityStateUpdateError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str = "update_error") -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(detail)


class OpportunityStateUpdateService:
    def __init__(
        self,
        *,
        workspace_service: OpportunityWorkspaceService,
        transcription_service: TranscriptionService,
        state_service: OpportunityStateService,
        job_service: JobService,
        executor: CanonicalStateExecutor,
    ) -> None:
        self._workspace_service = workspace_service
        self._transcription_service = transcription_service
        self._state_service = state_service
        self._job_service = job_service
        self._executor = executor
        self._start_lock = job_service.opportunity_state_start_lock

    async def resolve_identity(self, account: str, opp_slug: str) -> dict[str, Any]:
        return await self._workspace_service.resolve_identity(account, opp_slug)

    @staticmethod
    def _metadata_entry(opportunity: dict[str, Any]) -> EvidenceManifestEntry:
        content = OpportunityStateCreateService._metadata_bytes(opportunity)
        return EvidenceManifestEntry(
            source_type=EvidenceSourceType.OPPORTUNITY_METADATA,
            source_id=METADATA_SOURCE_ID,
            sha256=hashlib.sha256(content).hexdigest(),
            byte_count=len(content),
            observed_at=datetime.now(timezone.utc),
            display_name="Opportunity metadata",
        )

    @staticmethod
    def _transcript_entry(item: ResolvedTranscriptEvidence) -> EvidenceManifestEntry:
        return EvidenceManifestEntry(
            source_type=EvidenceSourceType.TRANSCRIPT,
            source_id=item.evidence_id,
            sha256=item.sha256,
            byte_count=item.byte_count,
            observed_at=item.observed_at,
            display_name=item.display_name,
        )

    @staticmethod
    def _current_metadata(parent: OpportunityStateVersion) -> EvidenceManifestEntry:
        entries = [
            item for item in parent.evidence_manifest
            if item.source_type == EvidenceSourceType.OPPORTUNITY_METADATA
        ]
        if len(entries) != 1:
            raise OpportunityStateUpdateError(
                409, "Canonical overview evidence is malformed.", code="malformed_storage"
            )
        return entries[0]

    def _resolve_selected(self, account: str, transcript_ids: list[str]) -> list[ResolvedTranscriptEvidence]:
        if not transcript_ids:
            return []
        return self._transcription_service.resolve_evidence_transcripts(account, transcript_ids)

    @staticmethod
    def _classify_selected(
        parent: OpportunityStateVersion,
        selected: list[ResolvedTranscriptEvidence],
    ) -> None:
        inherited = {
            item.source_id: item
            for item in parent.evidence_manifest
            if item.source_type == EvidenceSourceType.TRANSCRIPT
        }
        for item in selected:
            previous = inherited.get(item.evidence_id)
            if previous is not None and previous.sha256 == item.sha256:
                raise OpportunityStateUpdateError(
                    400,
                    "Previously used unchanged transcripts are inherited and cannot be selected again.",
                    code="invalid_selection",
                )

    @classmethod
    def _cumulative_manifest(
        cls,
        parent: OpportunityStateVersion,
        metadata: EvidenceManifestEntry,
        selected: list[ResolvedTranscriptEvidence],
    ) -> list[EvidenceManifestEntry]:
        replacements = {item.evidence_id: cls._transcript_entry(item) for item in selected}
        manifest: list[EvidenceManifestEntry] = [metadata]
        seen: set[str] = set()
        for entry in parent.evidence_manifest:
            if entry.source_type == EvidenceSourceType.OPPORTUNITY_METADATA:
                continue
            replacement = replacements.get(entry.source_id)
            manifest.append(replacement or entry)
            seen.add(entry.source_id)
        for item in selected:
            if item.evidence_id not in seen:
                manifest.append(replacements[item.evidence_id])
                seen.add(item.evidence_id)
        if len(manifest) > 51:
            raise OpportunityStateUpdateError(
                413, "The cumulative evidence manifest has reached its safe limit.", code="manifest_limit"
            )
        return manifest

    @staticmethod
    def _delta_hash(
        *,
        base_version_id: str,
        base_revision: int,
        metadata: EvidenceManifestEntry,
        selected: list[ResolvedTranscriptEvidence],
    ) -> str:
        payload = {
            "base_version_id": base_version_id,
            "base_revision": base_revision,
            "metadata": metadata.sha256,
            "selected": sorted((item.evidence_id, item.sha256, item.byte_count) for item in selected),
        }
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()

    async def get_readiness(self, account: str, opp_slug: str) -> dict[str, Any]:
        identity = await self.resolve_identity(account, opp_slug)
        parent = self._state_service.read_current(identity["safe_account"], identity["safe_opp"])
        if parent is None:
            raise OpportunityStateUpdateError(409, "Create the overview before updating it.", code="not_created")
        metadata = self._metadata_entry(identity["opportunity"])
        metadata_changed = metadata.sha256 != self._current_metadata(parent).sha256
        available = self._transcription_service.inspect_evidence_transcripts(identity["safe_account"])
        available_by_id = {item.evidence_id: item for item in available}
        inherited = {
            item.source_id: item
            for item in parent.evidence_manifest
            if item.source_type == EvidenceSourceType.TRANSCRIPT
        }

        def public(item: ResolvedTranscriptEvidence, status: str) -> dict[str, Any]:
            return {
                "id": item.evidence_id,
                "display_name": item.display_name,
                "modified_at": item.observed_at.isoformat(),
                "size_bytes": item.byte_count,
                "status": status,
            }

        new_sources = [public(item, "new") for key, item in available_by_id.items() if key not in inherited]
        changed_sources = [
            public(item, "changed") for key, item in available_by_id.items()
            if key in inherited and inherited[key].sha256 != item.sha256
        ]
        inherited_sources = [
            {
                "id": entry.source_id,
                "display_name": entry.display_name,
                "observed_at": entry.observed_at.isoformat(),
                "status": "inherited",
            }
            for key, entry in inherited.items()
            if key in available_by_id and entry.sha256 == available_by_id[key].sha256
        ]
        missing_sources = [
            {"id": entry.source_id, "display_name": entry.display_name, "status": "missing"}
            for key, entry in inherited.items() if key not in available_by_id
        ]
        new_sources.sort(key=lambda item: (item["display_name"], item["id"]))
        changed_sources.sort(key=lambda item: (item["display_name"], item["id"]))
        inherited_sources.sort(key=lambda item: (item["display_name"], item["id"]))
        missing_sources.sort(key=lambda item: (item["display_name"], item["id"]))

        active = self._job_service.active_opportunity_state_job(
            account=identity["safe_account"], opp_slug=identity["safe_opp"]
        )
        latest_update = self._job_service.latest_job(
            kind="opportunity_state_update", account=identity["safe_account"], opp_slug=identity["safe_opp"]
        )
        active_payload = None
        if active is not None:
            job_id, job = active
            active_payload = {"job_id": job_id, "kind": job.get("kind"), "status": job.get("status")}
        return {
            "schema_version": 1,
            "base_version_id": parent.version_id,
            "base_revision": parent.revision,
            "metadata": OpportunityStateCreateService._metadata_payload(identity["opportunity"]),
            "metadata_changed": metadata_changed,
            "new_source_count": len(new_sources),
            "changed_source_count": len(changed_sources),
            "missing_source_count": len(missing_sources),
            "update_active": active_payload is not None,
            "active_job": active_payload,
            "new_sources": new_sources,
            "changed_sources": changed_sources,
            "inherited_sources": inherited_sources,
            "missing_sources": missing_sources,
            "up_to_date": not metadata_changed and not new_sources and not changed_sources,
            "last_successful_update": {
                "revision": parent.revision,
                "created_at": parent.created_at.isoformat(),
                "job_id": (
                    latest_update.get("job_id")
                    if latest_update and latest_update.get("ok") is True else None
                ),
            },
        }

    async def _snapshot(
        self,
        account: str,
        opp_slug: str,
        *,
        expected_base_version_id: str,
        expected_base_revision: int,
        transcript_ids: list[str],
    ) -> tuple[
        dict[str, Any], OpportunityStateVersion, list[ResolvedTranscriptEvidence],
        list[EvidenceManifestEntry], str, str,
    ]:
        identity = await self.resolve_identity(account, opp_slug)
        parent = self._state_service.read_current(identity["safe_account"], identity["safe_opp"])
        if (
            parent is None
            or parent.version_id != expected_base_version_id
            or parent.revision != expected_base_revision
        ):
            raise OpportunityStateUpdateError(
                409, "The overview changed; refresh before updating.", code="stale_base"
            )
        selected = self._resolve_selected(identity["safe_account"], transcript_ids)
        self._classify_selected(parent, selected)
        metadata = self._metadata_entry(identity["opportunity"])
        if not selected and metadata.sha256 == self._current_metadata(parent).sha256:
            raise OpportunityStateUpdateError(
                409, "No metadata change or selected new evidence is available.", code="no_op"
            )
        manifest = self._cumulative_manifest(parent, metadata, selected)
        manifest_hash = evidence_manifest_hash(manifest)
        delta_hash = self._delta_hash(
            base_version_id=parent.version_id,
            base_revision=parent.revision,
            metadata=metadata,
            selected=selected,
        )
        return identity, parent, selected, manifest, manifest_hash, delta_hash

    async def start_update(
        self,
        account: str,
        opp_slug: str,
        *,
        base_version_id: str,
        base_revision: int,
        transcript_ids: list[str],
    ) -> dict[str, Any]:
        async with self._start_lock:
            identity = await self.resolve_identity(account, opp_slug)
            selected_now = self._resolve_selected(identity["safe_account"], transcript_ids)
            metadata_now = self._metadata_entry(identity["opportunity"])
            delta_hash_now = self._delta_hash(
                base_version_id=base_version_id,
                base_revision=base_revision,
                metadata=metadata_now,
                selected=selected_now,
            )
            sig = (
                "opportunity_state_update", identity["safe_account"], identity["safe_opp"],
                base_version_id, base_revision, delta_hash_now, UPDATER_VERSION,
            )
            existing = self._job_service.find_managed_job(kind="opportunity_state_update", sig=sig)
            if existing is not None and existing[1].get("status") in {"running", "done"}:
                return {"job_id": existing[0], "reused": True}

            snapshot = await self._snapshot(
                identity["safe_account"], identity["safe_opp"],
                expected_base_version_id=base_version_id,
                expected_base_revision=base_revision,
                transcript_ids=transcript_ids,
            )
            identity, parent, _selected, manifest, manifest_hash, delta_hash = snapshot
            if delta_hash != delta_hash_now:
                raise OpportunityStateUpdateError(
                    409, "Selected evidence changed before the update started.", code="evidence_changed"
                )
            active = self._job_service.active_opportunity_state_job(
                account=identity["safe_account"], opp_slug=identity["safe_opp"]
            )
            if active is not None:
                raise OpportunityStateUpdateError(
                    409, "Overview work is already running for this opportunity.", code="update_in_progress"
                )

            async def runner(_job_id: str) -> dict[str, Any]:
                try:
                    before = await self._snapshot(
                        identity["safe_account"], identity["safe_opp"],
                        expected_base_version_id=base_version_id,
                        expected_base_revision=base_revision,
                        transcript_ids=transcript_ids,
                    )
                    before_identity, before_parent, before_selected, before_manifest, before_hash, before_delta = before
                    if before_hash != manifest_hash or before_delta != delta_hash:
                        raise ManagedJobError(
                            "evidence_changed", "Selected evidence changed before analysis; choose it again and retry."
                        )
                    result = await self._executor.execute(CanonicalStateExecutionRequest(
                        account=before_identity["safe_account"],
                        opportunity_slug=before_identity["safe_opp"],
                        opportunity_name=before_identity["opportunity"]["name"],
                        opportunity_metadata=OpportunityStateCreateService._metadata_payload(
                            before_identity["opportunity"]
                        ),
                        metadata_source_id=METADATA_SOURCE_ID,
                        transcripts=before_selected,
                        base_state=before_parent.state,
                        base_version_id=before_parent.version_id,
                        base_revision=before_parent.revision,
                    ))
                    validate_candidate_evidence(result.candidate, before_manifest)
                    after = await self._snapshot(
                        identity["safe_account"], identity["safe_opp"],
                        expected_base_version_id=base_version_id,
                        expected_base_revision=base_revision,
                        transcript_ids=transcript_ids,
                    )
                    after_identity, _after_parent, _after_selected, after_manifest, after_hash, after_delta = after
                    if after_hash != manifest_hash or after_delta != delta_hash:
                        raise ManagedJobError(
                            "evidence_changed", "Selected evidence or metadata changed during analysis; no state was saved."
                        )
                    version = self._state_service.promote_update(
                        identity=OpportunityIdentity(
                            account=after_identity["safe_account"],
                            opportunity_slug=after_identity["safe_opp"],
                            opportunity_name=after_identity["opportunity"]["name"],
                        ),
                        expected_parent_version_id=base_version_id,
                        expected_parent_revision=base_revision,
                        evidence_manifest=after_manifest,
                        expected_manifest_hash=manifest_hash,
                        provenance=GenerationProvenance(
                            updater_version=UPDATER_VERSION,
                            model=result.model,
                            runtime=result.runtime,
                            cli_version=result.cli_version,
                        ),
                        candidate=result.candidate,
                    )
                    return {"result_revision": version.revision, "result_version_id": version.version_id}
                except ManagedJobError:
                    raise
                except OpportunityStateUpdateError as exc:
                    raise ManagedJobError(exc.code, exc.detail) from exc
                except CanonicalStateExecutionError as exc:
                    raise ManagedJobError(exc.code, exc.detail) from exc
                except TranscriptionError as exc:
                    raise ManagedJobError(
                        "evidence_unavailable", "Selected evidence changed or became unavailable; no state was saved."
                    ) from exc
                except OpportunityStateError as exc:
                    safe_messages = {
                        "stale_base": "The overview changed while this update ran; refresh and retry.",
                        "evidence_changed": "Selected evidence changed during analysis; no state was saved.",
                        "malformed_storage": "Opportunity state storage is malformed; no state was saved.",
                    }
                    raise ManagedJobError(
                        exc.code, safe_messages.get(exc.code, "Overview update failed safely; no state was saved.")
                    ) from exc
                except (ValidationError, ValueError) as exc:
                    raise ManagedJobError(
                        "invalid_model_output", "Claude returned an invalid canonical-state candidate; no state was saved."
                    ) from exc

            job_id, warning = await self._job_service.launch_managed(
                kind="opportunity_state_update",
                account=identity["safe_account"],
                opp_slug=identity["safe_opp"],
                opportunity=identity["opportunity"]["name"],
                sig=sig,
                safe_metadata={
                    "base_version_id": parent.version_id,
                    "base_revision": parent.revision,
                    "authorized_delta_hash": delta_hash,
                    "evidence_manifest_hash": manifest_hash,
                    "selected_transcript_count": len(transcript_ids),
                    "evidence_count": len(manifest),
                },
                runner=runner,
            )
            payload: dict[str, Any] = {"job_id": job_id, "reused": False}
            if warning:
                payload["persistence_warning"] = warning
            return payload

    def get_update_job(self, account: str, opp_slug: str, job_id: str) -> dict[str, Any]:
        job = self._job_service.get_job(job_id)
        if (
            not job
            or job.get("kind") != "opportunity_state_update"
            or job.get("account") != account
            or job.get("opp_slug") != opp_slug
        ):
            raise OpportunityStateUpdateError(404, "Unknown overview update job.", code="unknown_job")
        allowed = (
            "kind", "status", "ok", "started_at", "finished_at", "base_version_id",
            "base_revision", "authorized_delta_hash", "evidence_manifest_hash",
            "selected_transcript_count", "evidence_count", "result_revision",
            "result_version_id", "error_code", "error_message", "persistence_warning",
        )
        return {"job_id": job_id, **{key: job[key] for key in allowed if key in job}}
