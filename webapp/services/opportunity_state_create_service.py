"""Create-once orchestration for local canonical Opportunity Overview state."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from opportunity_state import (
    EvidenceManifestEntry,
    EvidenceSourceType,
    GenerationProvenance,
    OpportunityIdentity,
    evidence_manifest_hash,
    validate_candidate_evidence,
)
from services.job_service import JobService, ManagedJobError
from services.opportunity_state_executor import (
    UPDATER_VERSION,
    CanonicalStateExecutionError,
    CanonicalStateExecutionRequest,
    CanonicalStateExecutor,
)
from services.opportunity_state_service import OpportunityStateError, OpportunityStateService
from services.opportunity_workspace_service import OpportunityWorkspaceService
from services.transcription_service import (
    ResolvedTranscriptEvidence,
    TranscriptionError,
    TranscriptionService,
)

logger = logging.getLogger(__name__)

METADATA_SOURCE_ID = "opportunity-metadata-v1"
_METADATA_FIELDS = (
    "name", "slug", "stage", "stage_num", "amount", "close_date", "type",
    "is_closed", "ae", "sfdc_url", "metadata_source", "metadata_complete",
)


class OpportunityStateCreateError(Exception):
    def __init__(self, status_code: int, detail: str, *, code: str = "create_error") -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(detail)


class OpportunityStateCreateService:
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

    async def list_eligible_evidence(self, account: str, opp_slug: str) -> dict[str, Any]:
        identity = await self.resolve_identity(account, opp_slug)
        items = self._transcription_service.list_evidence_transcripts(identity["safe_account"])
        return {
            "schema_version": 1,
            "account": identity["safe_account"],
            "opportunity_slug": identity["safe_opp"],
            "metadata_included": True,
            "transcripts": items,
        }

    @staticmethod
    def _metadata_payload(opportunity: dict[str, Any]) -> dict[str, Any]:
        return {field: opportunity.get(field) for field in _METADATA_FIELDS}

    @classmethod
    def _metadata_bytes(cls, opportunity: dict[str, Any]) -> bytes:
        return json.dumps(
            cls._metadata_payload(opportunity), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    @classmethod
    def _manifest(
        cls,
        opportunity: dict[str, Any],
        transcripts: list[ResolvedTranscriptEvidence],
    ) -> list[EvidenceManifestEntry]:
        metadata = cls._metadata_bytes(opportunity)
        entries = [EvidenceManifestEntry(
            source_type=EvidenceSourceType.OPPORTUNITY_METADATA,
            source_id=METADATA_SOURCE_ID,
            sha256=hashlib.sha256(metadata).hexdigest(),
            byte_count=len(metadata),
            observed_at=datetime.now(timezone.utc),
            display_name="Opportunity metadata",
        )]
        entries.extend(
            EvidenceManifestEntry(
                source_type=EvidenceSourceType.TRANSCRIPT,
                source_id=item.evidence_id,
                sha256=item.sha256,
                byte_count=item.byte_count,
                observed_at=item.observed_at,
                display_name=item.display_name,
            )
            for item in transcripts
        )
        return entries

    async def _snapshot(
        self,
        account: str,
        opp_slug: str,
        transcript_ids: list[str],
    ) -> tuple[dict[str, Any], list[ResolvedTranscriptEvidence], list[EvidenceManifestEntry], str]:
        identity = await self.resolve_identity(account, opp_slug)
        transcripts = self._transcription_service.resolve_evidence_transcripts(
            identity["safe_account"], transcript_ids
        )
        manifest = self._manifest(identity["opportunity"], transcripts)
        return identity, transcripts, manifest, evidence_manifest_hash(manifest)

    async def start_create(
        self,
        account: str,
        opp_slug: str,
        transcript_ids: list[str],
    ) -> dict[str, Any]:
        async with self._start_lock:
            identity, _transcripts, manifest, manifest_hash = await self._snapshot(
                account, opp_slug, transcript_ids
            )
            current = self._state_service.inspect_current(identity["safe_account"], identity["safe_opp"])
            if current["status"] == "current":
                raise OpportunityStateCreateError(
                    409, "An overview already exists; Update Overview belongs to Slice 2B.", code="already_created"
                )
            if current["status"] == "malformed":
                raise OpportunityStateCreateError(
                    409, "Existing opportunity state storage must be repaired before creating an overview.",
                    code="malformed_storage",
                )
            active = self._job_service.active_opportunity_state_job(
                account=identity["safe_account"], opp_slug=identity["safe_opp"]
            )
            if active is not None:
                job_id, job = active
                if job.get("kind") == "opportunity_state_create" and job.get("evidence_manifest_hash") == manifest_hash:
                    return {"job_id": job_id, "reused": True}
                raise OpportunityStateCreateError(
                    409, "Overview work is already running for this opportunity.", code="create_in_progress"
                )

            async def runner(_job_id: str) -> dict[str, Any]:
                try:
                    before_identity, before_transcripts, before_manifest, before_hash = await self._snapshot(
                        identity["safe_account"], identity["safe_opp"], transcript_ids
                    )
                    if before_hash != manifest_hash:
                        raise ManagedJobError(
                            "evidence_changed", "Selected evidence changed before analysis; choose it again and retry."
                        )
                    result = await self._executor.execute(CanonicalStateExecutionRequest(
                        account=before_identity["safe_account"],
                        opportunity_slug=before_identity["safe_opp"],
                        opportunity_name=before_identity["opportunity"]["name"],
                        opportunity_metadata=self._metadata_payload(before_identity["opportunity"]),
                        metadata_source_id=METADATA_SOURCE_ID,
                        transcripts=before_transcripts,
                    ))
                    validate_candidate_evidence(result.candidate, before_manifest)
                    after_identity, _after_transcripts, after_manifest, after_hash = await self._snapshot(
                        identity["safe_account"], identity["safe_opp"], transcript_ids
                    )
                    if after_hash != manifest_hash:
                        raise ManagedJobError(
                            "evidence_changed", "Selected evidence changed during analysis; no state was saved."
                        )
                    version = self._state_service.promote_create(
                        identity=OpportunityIdentity(
                            account=after_identity["safe_account"],
                            opportunity_slug=after_identity["safe_opp"],
                            opportunity_name=after_identity["opportunity"]["name"],
                        ),
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
                except CanonicalStateExecutionError as exc:
                    raise ManagedJobError(exc.code, exc.detail) from exc
                except TranscriptionError as exc:
                    raise ManagedJobError(
                        "evidence_unavailable", "Selected evidence changed or became unavailable; no state was saved."
                    ) from exc
                except OpportunityStateError as exc:
                    safe_messages = {
                        "already_created": "An overview already exists; Update Overview belongs to Slice 2B.",
                        "evidence_changed": "Selected evidence changed during analysis; no state was saved.",
                        "malformed_storage": "Opportunity state storage is malformed; no state was saved.",
                    }
                    raise ManagedJobError(exc.code, safe_messages.get(exc.code, "Overview creation failed safely; no state was saved.")) from exc
                except (ValidationError, ValueError) as exc:
                    # exc.args are opaque source_type/source_id identifiers or Pydantic field
                    # paths -- never evidence content -- safe to log for diagnostics.
                    logger.warning("Opportunity overview candidate rejected post-generation: %s", exc)
                    raise ManagedJobError(
                        "invalid_model_output", "Claude returned an invalid canonical-state candidate; no state was saved."
                    ) from exc

            sig = (
                "opportunity_state_create", identity["safe_account"], identity["safe_opp"],
                manifest_hash, 1, UPDATER_VERSION,
            )
            job_id, warning = await self._job_service.launch_managed(
                kind="opportunity_state_create",
                account=identity["safe_account"],
                opp_slug=identity["safe_opp"],
                opportunity=identity["opportunity"]["name"],
                sig=sig,
                safe_metadata={
                    "evidence_manifest_hash": manifest_hash,
                    "evidence_count": len(manifest),
                },
                runner=runner,
            )
            payload: dict[str, Any] = {"job_id": job_id, "reused": False}
            if warning:
                payload["persistence_warning"] = warning
            return payload

    def get_create_job(self, account: str, opp_slug: str, job_id: str) -> dict[str, Any]:
        job = self._job_service.get_job(job_id)
        if (
            not job
            or job.get("kind") != "opportunity_state_create"
            or job.get("account") != account
            or job.get("opp_slug") != opp_slug
        ):
            raise OpportunityStateCreateError(404, "Unknown overview creation job.", code="unknown_job")
        allowed = (
            "kind", "status", "ok", "started_at", "finished_at", "evidence_manifest_hash",
            "evidence_count", "result_revision", "result_version_id", "error_code", "error_message",
            "persistence_warning",
        )
        return {"job_id": job_id, **{key: job[key] for key in allowed if key in job}}
