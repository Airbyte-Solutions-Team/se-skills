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
from .output_service import OutputService


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

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        account_service: AccountService,
        output_service: OutputService,
    ) -> None:
        self._account_service = account_service
        self._output_service = output_service

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

    async def get_workspace(self, account: str, opp_slug: str) -> dict[str, Any]:
        """Return one local opportunity workspace or a safe 404."""
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

        account_outputs = self._output_service.list_outputs(safe_account)

        return {
            "schema_version": self.SCHEMA_VERSION,
            "account": account_payload,
            "opportunity": opportunity,
            "canonical_state": {
                "available": False,
                "status": "not_created",
            },
            "capabilities": {
                "generate": True,
                "live_transcribe": True,
                "coverage_handoff": True,
                "update_overview": False,
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
