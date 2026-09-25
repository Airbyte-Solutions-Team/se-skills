"""Synthetic tests for the Command Center aggregate read model (PR D).

Fixtures and the deterministic fake executor only; no Granola transport, no
Claude runtime, no credentials, no customer content.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routes.command_center import router
from services.command_center_read_service import CommandCenterReadService

from eval.tests.test_command_center_operations_service import (
    ACCOUNT,
    OPP,
    SENTINELS,
    FailingExecutor,
    Harness,
    _recommendation,
    fixture,
)
from eval.tests.test_opportunity_state_create_service import _wait

OPP2 = "second-opportunity"
# Fixture note commits "Airbyte SE ... by October 1, 2026"; the read clock sits after that so it is overdue.
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
SANDBOX_LINE = "Customer Synthetic Buyer will confirm sandbox access by October 15, 2026."


class ReadHarness(Harness):
    def __init__(self, tmp_path) -> None:
        super().__init__(tmp_path)
        self.workspace.resolve_identity = self._resolve_identity
        self.tech_eval: dict[tuple[str, str], dict] = {}
        self.reads = CommandCenterReadService(
            customers_dir=self.customers, ledger=self.ledger, operations=self.ops, state_service=self.state,
            tech_eval_summary=lambda a, o: self.tech_eval.get((a, o)), clock=lambda: NOW,
        )
        self.app = FastAPI()
        self.app.state.evidence_ledger_service = self.ledger
        self.app.state.command_center_operations_service = self.ops
        self.app.state.command_center_read_service = self.reads
        self.app.state.granola_adapter = self.adapter
        self.app.state.job_service = self.jobs
        self.app.state.opportunity_workspace_service = self.workspace
        self.app.include_router(router)
        self.client = TestClient(self.app)

    async def _resolve_identity(self, account: str, opp_slug: str) -> dict:
        if account != ACCOUNT or opp_slug not in (OPP, OPP2):
            raise AssertionError("unexpected identity")
        opportunity = {**self.workspace.opportunity, "slug": opp_slug, "name": opp_slug.replace("-", " ").title()}
        return {
            "safe_account": account,
            "safe_opp": opp_slug,
            "account": {"name": account},
            "opportunity": opportunity,
            "opportunity_outputs": [],
        }

    async def bootstrap_second(self) -> None:
        started = await self.create.start_create(ACCOUNT, OPP2, [self.first_id])
        assert (await _wait(self.jobs, started["job_id"]))["ok"] is True

    async def reconcile_for(self, source_id: str, opp: str) -> dict:
        current = self.state.read_current(ACCOUNT, opp)
        started = await self.ops.start_reconciliation(
            source_id, base_version_id=current.version_id, base_revision=current.revision
        )
        return started if started.get("reused") else await _wait(self.jobs, started["job_id"])

    def import_variant(self, note_id: str, *, extra_line: str | None = None) -> str:
        payload = fixture("note_synthetic_v1.json")
        payload["id"] = note_id
        if extra_line:
            segment = json.loads(json.dumps(payload["transcript"][-1]))
            segment["text"] = extra_line
            payload["transcript"].append(segment)
        meeting = self.adapter.normalize(payload, connection_id="local-manual")
        return self.ledger.import_meetings([meeting])["results"][0]["source_id"]

    def get(self, path: str, **params) -> dict:
        response = self.client.get(path, params={k: v for k, v in params.items() if v is not None})
        assert response.status_code == 200, response.text
        return response.json()


async def _two_opportunities(tmp_path) -> tuple[ReadHarness, str, str]:
    """Two distinct opportunities under one account, each with its own processed source."""
    h = ReadHarness(tmp_path)
    await h.bootstrap_overview()
    await h.bootstrap_second()
    first = h.import_note("note_synthetic_v1.json")
    second = h.import_variant("not_SYNTH000000002", extra_line=SANDBOX_LINE)
    assert first != second
    await h.confirm(first, ACCOUNT, OPP)
    await h.confirm(second, ACCOUNT, OPP2)
    # source-verified owner+date+commitment -> open (overdue at NOW); model-only -> proposed
    h.set_recs(first, 1, lambda eid: [
        _recommendation(eid, key="send-arch", action="Send architecture diagram", owner="Airbyte SE", due="2026-10-01"),
        _recommendation(eid, key="security-q", action="Share security questionnaire", owner="Airbyte SE", due=None),
    ])
    assert (await h.reconcile_for(first, OPP))["ok"] is True
    # source-verified customer commitment -> open, due within a week; unowned -> proposed
    h.set_recs(second, 1, lambda eid: [
        _recommendation(eid, key="sandbox", action="Confirm sandbox access", owner="Customer Synthetic Buyer", due="2026-10-15"),
        _recommendation(eid, key="scope", action="Decide pilot scope", owner=None, due=None),
    ])
    assert (await h.reconcile_for(second, OPP2))["ok"] is True
    return h, first, second


@pytest.mark.asyncio
async def test_portfolio_keeps_two_opportunities_under_one_account_distinct(tmp_path) -> None:
    h, first, second = await _two_opportunities(tmp_path)
    portfolio = h.get("/api/command-center/portfolio")
    assert portfolio["total"] == 2
    rows = {row["opportunity_slug"]: row for row in portfolio["opportunities"]}
    assert set(rows) == {OPP, OPP2}
    assert all(row["account"] == ACCOUNT for row in rows.values())
    assert rows[OPP]["opportunity_link"] == f"#/opp/{ACCOUNT}/{OPP}/{OPP}"
    assert rows[OPP]["action_counts"]["overdue"] == 1
    assert rows[OPP2]["action_counts"]["overdue"] == 0
    assert rows[OPP2]["waiting_on"] == ["Customer"]
    assert rows[OPP]["waiting_on"] == []
    for row in rows.values():
        assert row["overview"]["status"] == "current"
        assert row["freshness"]["source_counts"]["total"] == 1
        assert row["freshness"]["connector"] == {
            "provider": "granola", "mode": "manual_import", "unattended_discovery": False,
            "health": "not_monitored",
            "note": "Sources arrive only when a user imports them; there is no connection check or sync.",
        }
        assert row["evaluation"] == {"supported": True, "overall": None, "current_phase": None}
        assert row["confirmed_blockers"] == []
    # account filter and attention filter are honoured; unknown account is empty, not an error
    assert h.get("/api/command-center/portfolio", account=ACCOUNT)["total"] == 2
    assert h.get("/api/command-center/portfolio", account="Other")["total"] == 0
    assert rows[OPP]["attention"] == ["1 overdue action(s)", "1 proposal(s) to review"]
    assert rows[OPP2]["attention"] == ["1 proposal(s) to review"]
    assert h.get("/api/command-center/portfolio", attention_only=True)["total"] == 2
    h.tech_eval[(ACCOUNT, OPP2)] = {"overall": "in_progress", "current_phase": "discovery"}
    row = next(r for r in h.get("/api/command-center/portfolio")["opportunities"] if r["opportunity_slug"] == OPP2)
    assert row["evaluation"] == {"supported": True, "overall": "in_progress", "current_phase": "discovery"}


@pytest.mark.asyncio
async def test_global_actions_filters_and_paging_are_scoped(tmp_path) -> None:
    h, first, second = await _two_opportunities(tmp_path)
    everything = h.get("/api/command-center/actions")
    # 2 recs from the bootstrap candidate are not durable actions; 4 from the two reconciliations are
    assert everything["total"] == 4
    assert everything["counts_by_status"] == {"open": 2, "proposed": 2}
    assert everything["actions"][0]["overdue"] is True
    assert everything["actions"][0]["opportunity_slug"] == OPP
    assert everything["actions"][0]["provenance"]["origin_source_id"] == first
    assert everything["actions"][0]["provenance"]["source_link"] == f"#/command-center/sources/{first}"
    assert everything["actions"][0]["allowed_transitions"] == ["blocked", "completed", "dismissed"]

    by_opp = h.get("/api/command-center/actions", account=ACCOUNT, opportunity_slug=OPP2)
    assert by_opp["total"] == 2 and {a["opportunity_slug"] for a in by_opp["actions"]} == {OPP2}
    assert h.client.get("/api/command-center/actions", params={"opportunity_slug": OPP2}).status_code == 400
    assert h.get("/api/command-center/actions", party="Customer")["total"] == 1
    assert h.get("/api/command-center/actions", status="proposed")["total"] == 2
    assert h.get("/api/command-center/actions", overdue=True)["total"] == 1
    assert h.client.get("/api/command-center/actions", params={"status": "bogus"}).status_code == 422
    assert h.client.get("/api/command-center/actions", params={"limit": 0}).status_code == 422
    assert h.client.get("/api/command-center/actions", params={"limit": 999}).status_code == 422

    page1 = h.get("/api/command-center/actions", limit=3)
    page2 = h.get("/api/command-center/actions", limit=3, offset=page1["next_offset"])
    assert len(page1["actions"]) == 3 and len(page2["actions"]) == 1 and page2["next_offset"] is None
    ids = [a["action_id"] for a in page1["actions"] + page2["actions"]]
    assert len(set(ids)) == 4


@pytest.mark.asyncio
async def test_today_lists_overdue_due_proposals_and_reasons_with_links(tmp_path) -> None:
    h, first, second = await _two_opportunities(tmp_path)
    today = h.get("/api/command-center/today")
    kinds = [item["kind"] for item in today["attention"]]
    assert kinds == ["overdue_action", "due_action", "proposal_review", "proposal_review"]
    overdue = today["attention"][0]
    assert overdue["reason"] == "Due 2026-10-01, 8 day(s) ago; still open."
    assert overdue["link"].startswith("#/command-center/actions/act_")
    assert overdue["opportunity_link"] == f"#/opp/{ACCOUNT}/{OPP}/{OPP}"
    assert today["attention"][1]["opportunity_slug"] == OPP2
    assert today["counts_by_kind"] == {"overdue_action": 1, "due_action": 1, "proposal_review": 2}
    assert today["opportunity_count"] == 2
    assert today["recent_changes"] and all("opportunity_link" in c for c in today["recent_changes"])
    # No inferred blocker: the synthetic Overview has no active_blocker stakeholder
    assert "confirmed_blocker" not in kinds
    assert h.get("/api/command-center/today", limit=1)["next_offset"] == 1


@pytest.mark.asyncio
async def test_completion_is_reflected_across_views_and_undo_restores(tmp_path) -> None:
    h, first, second = await _two_opportunities(tmp_path)
    overdue = h.get("/api/command-center/actions", overdue=True)["actions"][0]
    done = h.client.post(
        f"/api/command-center/actions/{overdue['action_id']}/transitions",
        json={"to_status": "completed", "reason": "Sent in synthetic test"},
    )
    assert done.status_code == 200
    assert h.get("/api/command-center/today")["counts_by_kind"].get("overdue_action") is None
    assert h.get("/api/command-center/actions", overdue=True)["total"] == 0
    row = next(r for r in h.get("/api/command-center/portfolio")["opportunities"] if r["opportunity_slug"] == OPP)
    assert row["action_counts"]["completed"] == 1 and row["action_counts"]["overdue"] == 0
    changes = h.get("/api/command-center/changes", change_type="action_transition")
    assert changes["total"] == 1 and changes["changes"][0]["actor"] == "user"
    assert changes["changes"][0]["subject_id"] == overdue["action_id"]

    undone = h.client.post(f"/api/command-center/actions/{overdue['action_id']}/undo", json={"reason": "oops"})
    assert undone.status_code == 200
    assert h.get("/api/command-center/today")["counts_by_kind"]["overdue_action"] == 1
    assert h.get("/api/command-center/changes", change_type="action_transition")["total"] == 2


@pytest.mark.asyncio
async def test_changes_filters_and_paging(tmp_path) -> None:
    h, first, second = await _two_opportunities(tmp_path)
    everything = h.get("/api/command-center/changes")
    assert everything["total"] >= 6
    assert everything["changes"] == sorted(everything["changes"], key=lambda c: c["applied_at"], reverse=True)
    for_second = h.get("/api/command-center/changes", account=ACCOUNT, opportunity_slug=OPP2)
    assert for_second["total"] > 0 and {c["opportunity_slug"] for c in for_second["changes"]} == {OPP2}
    assert h.get("/api/command-center/changes", actor="user")["total"] == 0
    overview = h.get("/api/command-center/changes", change_type="overview_revision")
    assert overview["total"] == 2 and all(c["source_link"] for c in overview["changes"])
    page = h.get("/api/command-center/changes", limit=2)
    assert len(page["changes"]) == 2 and page["next_offset"] == 2
    assert h.client.get("/api/command-center/changes", params={"opportunity_slug": OPP}).status_code == 400


@pytest.mark.asyncio
async def test_source_states_stale_failed_unassociated_and_review_payload(tmp_path) -> None:
    h, first, second = await _two_opportunities(tmp_path)
    # stale: an edited revision arrives after processing
    edited = h.import_note("note_synthetic_v2_edited.json")
    assert edited == first
    row = next(r for r in h.get("/api/command-center/portfolio")["opportunities"] if r["opportunity_slug"] == OPP)
    assert row["freshness"]["state"] == "source_pending"
    assert row["freshness"]["source_counts"]["stale"] == 1 or row["freshness"]["source_counts"]["pending"] == 1
    other = next(r for r in h.get("/api/command-center/portfolio")["opportunities"] if r["opportunity_slug"] == OPP2)
    assert other["freshness"]["state"] == "processed_latest_import"

    # failed reconciliation of the edited revision: the accepted Overview stays; Today explains and offers retry
    h.ops._executor = FailingExecutor("timeout")
    failed = await h.reconcile_for(first, OPP)
    assert failed["ok"] is False
    assert h.state.read_current(ACCOUNT, OPP).revision == 2
    today = h.get("/api/command-center/today")
    failure = next(i for i in today["attention"] if i["kind"] == "reconciliation_failure")
    assert failure["source_id"] == first and failure["next_step"] == "Retry"
    assert "timeout" in failure["reason"] and "synthetic failure" not in json.dumps(today)
    review = h.get(f"/api/command-center/sources/{first}/review")
    assert review["capabilities"]["retry"] is True
    assert review["overview_base"]["status"] == "current" and review["overview_base"]["revision"] == 2
    assert review["capabilities"]["reconcile"] is True
    assert [r["status"] for r in review["runs"]] == ["succeeded", "failed"]
    row = next(r for r in h.get("/api/command-center/portfolio")["opportunities"] if r["opportunity_slug"] == OPP)
    assert row["freshness"]["state"] == "failed"
    assert {c["opportunity_slug"] for c in review["candidates"]} == {OPP, OPP2}
    assert len(review["derived_actions"]) == 2

    # unassociated import: association_review, no derivation, reconcile blocked with a reason
    third = h.import_variant("not_SYNTH000000003")
    today = h.get("/api/command-center/today")
    pending = next(i for i in today["attention"] if i["kind"] == "association_review")
    assert pending["source_id"] == third and pending["opportunity_link"] is None
    review = h.get(f"/api/command-center/sources/{third}/review")
    assert review["overview_base"] is None
    assert review["capabilities"]["reconcile"] is False
    assert review["capabilities"]["reconcile_blocked_reason"] == "Confirm an opportunity association first."
    assert h.client.get("/api/command-center/sources/src_nope/review").status_code == 404

    # Aggregate responses never carry meeting text
    for path in ("/api/command-center/today", "/api/command-center/portfolio", "/api/command-center/actions",
                 "/api/command-center/changes", f"/api/command-center/sources/{first}/review",
                 "/api/command-center/sources/unprocessed"):
        blob = json.dumps(h.get(path))
        for sentinel in SENTINELS:
            assert sentinel not in blob, path


@pytest.mark.asyncio
async def test_empty_workspace_reads_are_empty_not_errors(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    assert h.get("/api/command-center/today") == {
        "total": 0, "offset": 0, "limit": 50, "next_offset": None, "attention": [], "counts_by_kind": {},
        "recent_changes": [], "as_of": NOW.isoformat(), "opportunity_count": 0,
    }
    assert h.get("/api/command-center/portfolio")["opportunities"] == []
    assert h.get("/api/command-center/actions")["total"] == 0
    assert h.get("/api/command-center/changes")["total"] == 0
    assert h.get("/api/command-center/opportunities")["total"] == 0


@pytest.mark.asyncio
async def test_scope_isolation_between_workspaces(tmp_path) -> None:
    h, first, second = await _two_opportunities(tmp_path)
    other = ReadHarness(tmp_path / "other")
    assert other.get("/api/command-center/today")["total"] == 0
    assert other.get("/api/command-center/portfolio")["total"] == 0
    assert other.get("/api/command-center/actions")["total"] == 0
    assert other.client.get(f"/api/command-center/sources/{first}/review").status_code == 404
