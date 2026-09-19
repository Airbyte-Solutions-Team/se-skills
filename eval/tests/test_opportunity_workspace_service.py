"""Focused tests for the local Opportunity Workspace aggregate."""
from __future__ import annotations

from typing import Any

import pytest

from services.account_service import AccountError
from services.opportunity_workspace_service import OpportunityWorkspaceService


class FakeAccountService:
    def __init__(self, opportunities: list[dict[str, Any]]) -> None:
        self.opportunities = opportunities

    def safe_name(self, value: str) -> str:
        if ".." in value or "/" in value or "\\" in value:
            raise AccountError(400, "Invalid name")
        return value

    def get_account(self, account: str) -> dict[str, Any]:
        if account != "Acme":
            raise AccountError(404, "Unknown account")
        return {"name": "Acme", "owner": "gary"}

    def member_by_id(self, member_id: str) -> dict[str, Any] | None:
        return {"id": "gary", "name": "Gary Yang"} if member_id == "gary" else None

    async def list_opportunities(self, account: str) -> list[dict[str, Any]]:
        return self.opportunities


class FakeOutputService:
    def __init__(
        self,
        opportunity_outputs: list[dict[str, Any]] | None = None,
        account_outputs: list[dict[str, Any]] | None = None,
    ) -> None:
        self.opportunity_outputs = opportunity_outputs or []
        self.account_outputs = account_outputs or []

    def list_outputs(self, account: str, opp: str | None = None) -> list[dict[str, Any]]:
        return list(self.opportunity_outputs if opp else self.account_outputs)


def _opportunity() -> dict[str, Any]:
    return {
        "name": "Acme Expansion",
        "slug": "acme-expansion",
        "stage": "Tech Eval",
        "stage_num": "S3",
        "amount": 75000,
        "close_date": "2026-10-31",
        "type": "New Business",
        "is_closed": False,
        "ae": "Alex",
        "sfdc_url": "https://example.invalid/opportunity",
    }


@pytest.mark.asyncio
async def test_workspace_uses_authoritative_opportunity_and_groups_history() -> None:
    outputs = [
        {
            "skill": "tech-qual",
            "filename": "tech-qual-new.md",
            "path": "Acme/opportunities/acme-expansion/outputs/tech-qual/tech-qual-new.md",
            "modified": "2026-09-15 12:00",
            "review_status": "approved",
        },
        {
            "skill": "post-call",
            "filename": "post-call.md",
            "path": "Acme/opportunities/acme-expansion/outputs/post-call/post-call.md",
            "modified": "2026-09-14 12:00",
            "review_status": "awaiting review",
        },
        {
            "skill": "tech-qual",
            "filename": "tech-qual-old.md",
            "path": "Acme/opportunities/acme-expansion/outputs/tech-qual/tech-qual-old.md",
            "modified": "2026-09-10 12:00",
            "review_status": "corrected",
        },
    ]
    account_outputs = [
        {
            "skill": "account-refresher",
            "filename": "account-refresher.md",
            "path": "Acme/outputs/account-refresher/account-refresher.md",
            "modified": "2026-09-13 12:00",
        }
    ]
    service = OpportunityWorkspaceService(
        account_service=FakeAccountService([_opportunity()]),
        output_service=FakeOutputService(outputs, account_outputs),
    )

    workspace = await service.get_workspace("Acme", "acme-expansion")

    assert workspace["schema_version"] == 3
    assert workspace["account"] == {
        "name": "Acme",
        "owner_id": "gary",
        "owner_name": "Gary Yang",
    }
    assert workspace["opportunity"]["name"] == "Acme Expansion"
    assert workspace["opportunity"]["stage"] == "Tech Eval"
    assert workspace["opportunity"]["metadata_source"] == "account_service"
    assert workspace["canonical_state"] == {
        "available": False,
        "status": "not_created",
    }
    assert workspace["tech_eval"] is None

    groups = workspace["outputs"]["opportunity"]["groups"]
    assert [group["skill"] for group in groups] == ["tech-qual", "post-call"]
    assert groups[0]["latest"]["filename"] == "tech-qual-new.md"
    assert groups[0]["generation_count"] == 2
    assert groups[0]["history_count"] == 1
    assert [item["filename"] for item in groups[0]["generations"]] == [
        "tech-qual-new.md",
        "tech-qual-old.md",
    ]

    assert workspace["outputs"]["account"]["total"] == 1
    assert workspace["outputs"]["account"]["groups"][0]["skill"] == "account-refresher"
    assert workspace["capabilities"]["update_overview"] is False


@pytest.mark.asyncio
async def test_workspace_uses_honest_local_fallback_when_outputs_outlive_sfdc() -> None:
    service = OpportunityWorkspaceService(
        account_service=FakeAccountService([]),
        output_service=FakeOutputService(
            [{"skill": "post-call", "filename": "post-call.md", "path": "safe.md"}]
        ),
    )

    workspace = await service.get_workspace("Acme", "legacy-renewal")

    assert workspace["opportunity"]["name"] == "Legacy Renewal"
    assert workspace["opportunity"]["metadata_source"] == "local_outputs"
    assert workspace["opportunity"]["metadata_complete"] is False
    assert workspace["opportunity"]["stage"] is None


@pytest.mark.asyncio
async def test_workspace_rejects_unknown_opportunity_without_local_outputs() -> None:
    service = OpportunityWorkspaceService(
        account_service=FakeAccountService([]),
        output_service=FakeOutputService(),
    )

    with pytest.raises(AccountError) as exc:
        await service.get_workspace("Acme", "missing")

    assert exc.value.status_code == 404
    assert exc.value.detail == "Unknown opportunity"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "opportunities",
    [
        [None, "invalid", 42],
        [{"slug": "legacy-renewal"}, {"slug": "legacy-renewal", "name": "  "}],
    ],
)
async def test_workspace_ignores_malformed_opportunity_metadata(
    opportunities: list[Any],
) -> None:
    service = OpportunityWorkspaceService(
        account_service=FakeAccountService(opportunities),
        output_service=FakeOutputService(
            [{"skill": "post-call", "filename": "post-call.md", "path": "safe.md"}]
        ),
    )

    workspace = await service.get_workspace("Acme", "legacy-renewal")

    assert workspace["opportunity"]["name"] == "Legacy Renewal"
    assert workspace["opportunity"]["metadata_source"] == "local_outputs"
    assert workspace["opportunity"]["metadata_complete"] is False


@pytest.mark.asyncio
async def test_workspace_rejects_unsafe_opportunity_name() -> None:
    service = OpportunityWorkspaceService(
        account_service=FakeAccountService([_opportunity()]),
        output_service=FakeOutputService(),
    )

    with pytest.raises(AccountError) as exc:
        await service.get_workspace("Acme", "../escape")

    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_workspace_enriches_canonical_state_create_job_and_evidence_summary() -> None:
    class FakeState:
        def inspect_current(self, account, opp_slug):
            return {
                "available": True,
                "status": "current",
                "metadata": {"revision": 1},
                "current": {"revision": 1, "state": {"brief": {}}},
            }

    class FakeJobs:
        def latest_job(self, **kwargs):
            return {
                "kind": "opportunity_state_create",
                "status": "done",
                "ok": True,
                "started_at": 1.0,
                "finished_at": 2.0,
                "result_revision": 1,
                "stdout": "must not leak",
                "stderr": "must not leak",
            }

    class FakeTranscripts:
        def list_evidence_transcripts(self, account):
            return [
                {"id": "opaque-1", "display_name": "one.txt", "size_bytes": 10},
                {"id": "opaque-2", "display_name": "two.txt", "size_bytes": 20},
            ]

    service = OpportunityWorkspaceService(
        account_service=FakeAccountService([_opportunity()]),
        output_service=FakeOutputService(),
        state_service=FakeState(),
        job_service=FakeJobs(),
        transcription_service=FakeTranscripts(),
    )
    workspace = await service.get_workspace("Acme", "acme-expansion")
    assert workspace["canonical_state"]["available"] is True
    assert workspace["canonical_state"]["current"]["revision"] == 1
    assert workspace["canonical_state"]["create_job"]["result_revision"] == 1
    assert "stdout" not in workspace["canonical_state"]["create_job"]
    assert workspace["eligible_evidence"] == {"count": 2, "total_bytes": 30}
    assert workspace["capabilities"]["create_overview"] is False
