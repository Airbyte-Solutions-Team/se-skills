"""Local Opportunity Workspace read model.

Combines server-authoritative account/opportunity metadata with saved output
artifacts for the local webapp. This service deliberately does not synthesize an
opportunity brief or canonical state from generated Markdown.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from .account_service import AccountError, AccountService
from .job_service import JobService
from .opportunity_state_service import OpportunityStateService
from .output_service import OutputService
from .tech_eval_service import TechEvalService
from .transcription_service import TranscriptionService


_OPPORTUNITY_FIELDS = (
    "name",
    "slug",
    "stage",
    "stage_num",
    "amount",
    "close_date",
    "type",
    "is_closed",
    "ae",
    "sfdc_url",
)


class OpportunityWorkspaceService:
    """Build the local opportunity workspace payload from existing data only."""

    SCHEMA_VERSION = 3

    def __init__(
        self,
        *,
        account_service: AccountService,
        output_service: OutputService,
        state_service: OpportunityStateService | None = None,
        job_service: JobService | None = None,
        transcription_service: TranscriptionService | None = None,
        tech_eval_service: TechEvalService | None = None,
        update_service: Any | None = None,
    ) -> None:
        self._account_service = account_service
        self._output_service = output_service
        self._state_service = state_service
        self._job_service = job_service
        self._transcription_service = transcription_service
        self._tech_eval_service = tech_eval_service
        self._update_service = update_service

    def set_update_service(self, update_service: Any) -> None:
        """Complete the local composition cycle after both services exist."""
        self._update_service = update_service

    @staticmethod
    def _group_outputs(outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group newest-first artifacts by skill without collapsing history."""
        by_skill: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        for output in outputs:
            skill = str(output.get("skill") or "unknown")
            by_skill.setdefault(skill, []).append(output)

        return [
            {
                "skill": skill,
                "latest": generations[0],
                "generation_count": len(generations),
                "history_count": max(0, len(generations) - 1),
                "generations": generations,
            }
            for skill, generations in by_skill.items()
        ]

    @staticmethod
    def _fallback_opportunity(opp_slug: str) -> dict[str, Any]:
        """Return honest metadata when local artifacts outlive SFDC availability."""
        display_name = opp_slug.replace("-", " ").replace("_", " ").strip().title()
        return {
            "name": display_name or "Opportunity",
            "slug": opp_slug,
            "stage": None,
            "stage_num": None,
            "amount": None,
            "close_date": None,
            "type": None,
            "is_closed": None,
            "ae": None,
            "sfdc_url": None,
            "metadata_source": "local_outputs",
            "metadata_complete": False,
        }

    async def resolve_identity(self, account: str, opp_slug: str) -> dict[str, Any]:
        """Resolve trusted account/opportunity identity and local output scope."""
        safe_account = self._account_service.safe_name(account)
        safe_opp = self._account_service.safe_name(opp_slug)
        account_meta = self._account_service.get_account(safe_account)

        opportunity_outputs = self._output_service.list_outputs(safe_account, safe_opp)
        opportunities = await self._account_service.list_opportunities(safe_account)
        # Salesforce is an external metadata boundary. Ignore malformed rows
        # rather than letting one non-mapping value or a missing display name
        # turn the whole workspace into a 500 or a blank authoritative header.
        matched = next(
            (
                o
                for o in opportunities
                if isinstance(o, Mapping)
                and o.get("slug") == safe_opp
                and isinstance(o.get("name"), str)
                and bool(o["name"].strip())
            ),
            None,
        )

        if matched is None:
            if not opportunity_outputs:
                raise AccountError(404, "Unknown opportunity")
            opportunity = self._fallback_opportunity(safe_opp)
        else:
            opportunity = {field: matched.get(field) for field in _OPPORTUNITY_FIELDS}
            opportunity["slug"] = safe_opp
            opportunity["metadata_source"] = "account_service"
            opportunity["metadata_complete"] = True

        owner_id = account_meta.get("owner")
        owner = self._account_service.member_by_id(owner_id) if owner_id else None
        account_payload = {
            "name": account_meta["name"],
            "owner_id": owner_id,
            "owner_name": owner.get("name") if owner else None,
        }

        return {
            "safe_account": safe_account,
            "safe_opp": safe_opp,
            "account": account_payload,
            "opportunity": opportunity,
            "opportunity_outputs": opportunity_outputs,
        }

    @staticmethod
    def _safe_state_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        allowed = (
            "job_id", "kind", "status", "ok", "started_at", "finished_at",
            "evidence_manifest_hash", "evidence_count", "result_revision",
            "result_version_id", "error_code", "error_message", "persistence_warning",
            "base_version_id", "base_revision", "authorized_delta_hash",
            "selected_transcript_count",
        )
        return {key: job[key] for key in allowed if key in job}

    async def get_workspace(self, account: str, opp_slug: str) -> dict[str, Any]:
        """Return one local opportunity workspace or a safe 404."""
        identity = await self.resolve_identity(account, opp_slug)
        safe_account = identity["safe_account"]
        safe_opp = identity["safe_opp"]
        account_payload = identity["account"]
        opportunity = identity["opportunity"]
        opportunity_outputs = identity["opportunity_outputs"]

        tech_eval = (
            self._tech_eval_service.get_tracker(safe_account, safe_opp)
            if self._tech_eval_service is not None
            else None
        )

        account_outputs = self._output_service.list_outputs(safe_account)

        canonical_state = (
            self._state_service.inspect_current(safe_account, safe_opp)
            if self._state_service is not None
            else {"available": False, "status": "not_created"}
        )
        create_job = None
        if self._job_service is not None:
            create_job = self._safe_state_job(self._job_service.latest_job(
                kind="opportunity_state_create", account=safe_account, opp_slug=safe_opp
            ))
        eligible_summary = {"count": 0, "total_bytes": 0}
        if self._transcription_service is not None:
            eligible = self._transcription_service.list_evidence_transcripts(safe_account)
            eligible_summary = {
                "count": len(eligible),
                "total_bytes": sum(item["size_bytes"] for item in eligible),
            }
            canonical_state["create_job"] = create_job
        if canonical_state["status"] == "current" and self._update_service is not None:
            canonical_state["freshness"] = await self._update_service.get_readiness(safe_account, safe_opp)
            if self._job_service is not None:
                canonical_state["update_job"] = self._safe_state_job(self._job_service.latest_job(
                    kind="opportunity_state_update", account=safe_account, opp_slug=safe_opp
                ))

        return {
            "schema_version": self.SCHEMA_VERSION,
            "account": account_payload,
            "opportunity": opportunity,
            "canonical_state": canonical_state,
            "tech_eval": tech_eval,
            "eligible_evidence": eligible_summary,
            "capabilities": {
                "generate": True,
                "live_transcribe": True,
                "coverage_handoff": True,
                "update_overview": canonical_state["status"] == "current",
                "create_overview": canonical_state["status"] == "not_created",
                "local_only": True,
            },
            "outputs": {
                "opportunity": {
                    "total": len(opportunity_outputs),
                    "groups": self._group_outputs(opportunity_outputs),
                },
                "account": {
                    "total": len(account_outputs),
                    "groups": self._group_outputs(account_outputs),
                },
            },
        }
