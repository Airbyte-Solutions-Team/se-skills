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
from opportunity_state import EvidenceReference, EvidenceSourceType, OpportunityRisk, RiskClassification, RiskSeverity
from routes.command_center import router
from services.command_center_read_service import CommandCenterReadService
from services.command_center_operations_service import evidence_id_for
from services.opportunity_state_executor import FakeCanonicalStateExecutor

from eval.tests.test_command_center_operations_service import (
    ACCOUNT,
    OPP,
    SENTINELS,
    FailingExecutor,
    Harness,
    _recommendation,
    _candidate_with,
    fixture,
)
from eval.tests.test_opportunity_state_create_service import _candidate_for, _wait

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


def _risk(evidence_id: str, *, key: str, severity: RiskSeverity, refs: str = "transcript") -> OpportunityRisk:
    citations = (
        [EvidenceReference(source_type=EvidenceSourceType.TRANSCRIPT, source_id=evidence_id, locator="00:00:05")]
        if refs == "transcript" else
        [EvidenceReference(source_type=EvidenceSourceType.OPPORTUNITY_METADATA,
                           source_id="opportunity-metadata-v1")] if refs == "metadata" else []
    )
    return OpportunityRisk(
        key=key, title=f"Synthetic {key}", description=f"Synthetic reason for {key}.",
        severity=severity, classification=RiskClassification.IMPLEMENTATION_RISK,
        evidence_refs=citations,
    )


async def _reconcile_risks(h: ReadHarness, source_id: str, opp: str, risks: list[OpportunityRisk]) -> FakeCanonicalStateExecutor:
    revision = h.ledger.get_source(source_id)["latest_revision"]
    candidate = _candidate_with(h.first_id, evidence_id_for(source_id, revision), [])
    executor = FakeCanonicalStateExecutor(candidate.model_copy(update={"risks": risks}), cli_version="2.1.272")
    h.ops._executor = executor
    assert (await h.reconcile_for(source_id, opp))["ok"] is True
    return executor


@pytest.mark.asyncio
async def test_portfolio_and_today_show_scoped_potential_risks_with_detail_and_freshness(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    await h.bootstrap_overview()
    await h.bootstrap_second()
    first = h.import_note("note_synthetic_v1.json")
    second = h.import_variant("not_SYNTH000000002", extra_line=SANDBOX_LINE)
    await h.confirm(first, ACCOUNT, OPP)
    await h.confirm(second, ACCOUNT, OPP2)
    executor = await _reconcile_risks(h, first, OPP, [
        _risk(evidence_id_for(first, 1), key="security-review", severity=RiskSeverity.HIGH),
    ])
    await _reconcile_risks(h, second, OPP2, [
        _risk(evidence_id_for(second, 1), key="pilot-scope", severity=RiskSeverity.MEDIUM),
    ])
    calls = len(executor.requests)
    rows = {row["opportunity_slug"]: row for row in h.get("/api/command-center/portfolio")["opportunities"]}
    assert [r["key"] for r in rows[OPP]["risks"]] == ["security-review"]
    assert [r["key"] for r in rows[OPP2]["risks"]] == ["pilot-scope"]
    risk = rows[OPP]["risks"][0]
    assert risk["severity"] == "high" and risk["status"] == "potential"
    assert risk["reason"] == "Synthetic reason for security-review."
    assert risk["link"] == f"#/opp/{ACCOUNT}/{OPP}/{OPP}/risk/security-review"
    assert rows[OPP]["risks_link"] == f"#/opp/{ACCOUNT}/{OPP}/{OPP}/risks"
    assert risk["evidence_label"] == "1 transcript citation(s) · 00:00:05"
    assert risk["evidence_refs"][0]["locator"] == "00:00:05"
    assert rows[OPP]["attention"] == ["1 potential high/critical risk(s) to review"]
    today = h.get("/api/command-center/today")
    reviews = [item for item in today["attention"] if item["kind"] == "risk_review"]
    assert len(reviews) == 1 and reviews[0]["opportunity_slug"] == OPP
    assert reviews[0]["link"] == risk["link"] and reviews[0]["risk"] == risk
    assert reviews[0]["freshness"]["overview_revision"] == 2
    assert reviews[0]["freshness"]["state"] == "processed_latest_import"
    assert len(executor.requests) == calls  # list reads do not analyse again


@pytest.mark.asyncio
async def test_risk_revision_replaces_old_conclusion_and_weak_evidence_stays_potential(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    await h.bootstrap_overview()
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    await _reconcile_risks(h, source_id, OPP, [
        _risk(evidence_id_for(source_id, 1), key="model-hypothesis", severity=RiskSeverity.CRITICAL,
              refs="none").model_copy(update={"classification": RiskClassification.CRITICAL_BLOCKER}),
        _risk(evidence_id_for(source_id, 1), key="metadata-hypothesis", severity=RiskSeverity.HIGH, refs="metadata"),
    ])
    reviews = [item for item in h.reads.today()["attention"] if item["kind"] == "risk_review"]
    assert len(reviews) == 2
    assert all(item["risk"]["status"] == "potential" for item in reviews)
    assert any("No cited evidence" in item["risk"]["evidence_label"] for item in reviews)
    assert any("metadata only" in item["risk"]["evidence_label"].lower() for item in reviews)
    assert h.reads.today()["counts_by_kind"].get("confirmed_blocker") is None

    edited = h.adapter.normalize(fixture("note_synthetic_v2_edited.json"), connection_id="local-manual")
    assert h.ledger.import_meetings([edited])["results"][0]["source_id"] == source_id
    await _reconcile_risks(h, source_id, OPP, [
        _risk(evidence_id_for(source_id, 2), key="new-constraint", severity=RiskSeverity.HIGH),
    ])
    row = h.reads.portfolio()["opportunities"][0]
    assert row["overview"]["revision"] == 3
    assert [risk["key"] for risk in row["risks"]] == ["new-constraint"]
    assert [item["risk"]["key"] for item in h.reads.today()["attention"] if item["kind"] == "risk_review"] == ["new-constraint"]
    followup = h.import_variant("not_SYNTH000000010", extra_line="Synthetic follow-up resolves the constraint.")
    await h.confirm(followup)
    await _reconcile_risks(h, followup, OPP, [])
    assert h.reads.portfolio()["opportunities"][0]["risks"] == []
    assert not any(item["kind"] == "risk_review" for item in h.reads.today()["attention"])


@pytest.mark.asyncio
async def test_unavailable_source_and_withheld_overview_hide_risk_conclusions(tmp_path, monkeypatch) -> None:
    h = ReadHarness(tmp_path)
    await h.bootstrap_overview()
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    await _reconcile_risks(h, source_id, OPP, [
        _risk(evidence_id_for(source_id, 1), key="availability-risk", severity=RiskSeverity.HIGH),
    ])
    assert h.reads.portfolio()["opportunities"][0]["risks"]
    unavailable = h.adapter.normalize({"id": "not_SYNTH000000001", "error_code": "UNAUTHORIZED"},
                                      connection_id="local-manual")
    h.ledger.import_meetings([unavailable])
    row = h.reads.portfolio()["opportunities"][0]
    assert row["freshness"]["state"] == "unavailable" and row["risks"] == []
    assert row["confirmed_blockers"] == []
    assert not any(item["kind"] == "risk_review" for item in h.reads.today()["attention"])
    monkeypatch.setattr(h.state, "_forgotten_ids", lambda: {source_id})
    row = h.reads.portfolio()["opportunities"][0]
    assert row["overview"]["status"] == "withheld" and row["risks"] == []


@pytest.mark.asyncio
async def test_corrected_first_overview_removes_wrong_opportunity_risk(tmp_path) -> None:
    h = ReadHarness(tmp_path)
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    eid = evidence_id_for(source_id, 1)

    async def create_for(opp: str, key: str) -> None:
        candidate = _candidate_for(eid).model_copy(update={
            "risks": [_risk(eid, key=key, severity=RiskSeverity.HIGH)],
        })
        h.ops._executor = FakeCanonicalStateExecutor(candidate, cli_version="2.1.272")
        source = h.ledger.get_source(source_id)
        started = await h.ops.start_first_overview(
            source_id, account=ACCOUNT, opp_slug=opp, revision=source["latest_revision"],
            association_sequence=source["association"]["sequence"],
        )
        assert (await _wait(h.jobs, started["job_id"]))["ok"] is True

    await create_for(OPP, "wrong-association")
    assert h.reads.portfolio()["opportunities"][0]["risks"][0]["key"] == "wrong-association"
    corrected = await h.confirm(source_id, ACCOUNT, OPP2)
    assert corrected["overview_reverted"]["status"] == "retired"
    old = next(row for row in h.reads.portfolio()["opportunities"] if row["opportunity_slug"] == OPP)
    assert old["overview"]["status"] == "retired" and old["risks"] == []
    assert not any(item["kind"] == "risk_review" for item in h.reads.today()["attention"])
    await create_for(OPP2, "correct-association")
    rows = {row["opportunity_slug"]: row for row in h.reads.portfolio()["opportunities"]}
    assert rows[OPP]["risks"] == []
    assert [risk["key"] for risk in rows[OPP2]["risks"]] == ["correct-association"]
    reviews = [item for item in h.reads.today()["attention"] if item["kind"] == "risk_review"]
    assert len(reviews) == 1 and reviews[0]["opportunity_slug"] == OPP2


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
            "note": "Sources arrive through user-initiated intake; there is no unattended discovery or sync.",
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
async def test_three_opportunity_pilot_walkthrough_with_local_gap_risk_and_stale_source(tmp_path) -> None:
    h, meeting, stale_source = await _two_opportunities(tmp_path)
    # A third, locally saved opportunity can predate every import and Overview.
    (h.customers / "Other" / "opportunities" / "new-pilot").mkdir(parents=True)
    assert h.import_note("note_synthetic_v2_edited.json") == meeting
    await _reconcile_risks(h, meeting, OPP, [
        _risk(evidence_id_for(meeting, 2), key="security-review", severity=RiskSeverity.HIGH),
    ])
    assert h.import_variant("not_SYNTH000000002", extra_line="Synthetic later update.") == stale_source

    portfolio = h.get("/api/command-center/portfolio")
    assert portfolio["total"] == 3
    assert portfolio["coverage"]["complete"] is False
    assert portfolio["coverage"]["scope"] == "locally_known"
    rows = {(r["account"], r["opportunity_slug"]): r for r in portfolio["opportunities"]}
    assert set(rows) == {(ACCOUNT, OPP), (ACCOUNT, OPP2), ("Other", "new-pilot")}
    assert rows[("Other", "new-pilot")]["overview"]["status"] == "not_created"
    assert rows[("Other", "new-pilot")]["freshness"]["state"] == "no_sources"
    assert rows[("Other", "new-pilot")]["next_action"] is None
    assert rows[(ACCOUNT, OPP2)]["freshness"]["state"] == "source_pending"
    assert rows[(ACCOUNT, OPP2)]["freshness"]["source_link"] == f"#/command-center/sources/{stale_source}"
    assert rows[(ACCOUNT, OPP2)]["waiting_on"] == ["Customer"]
    assert rows[(ACCOUNT, OPP)]["next_action"]["overdue"] is True
    assert rows[(ACCOUNT, OPP)]["risks"][0]["status"] == "potential"

    today = h.get("/api/command-center/today")
    assert today["opportunity_count"] == 3 and today["coverage"] == portfolio["coverage"]
    pending = next(i for i in today["attention"] if i["kind"] == "source_pending")
    assert pending["source_id"] == stale_source
    assert pending["link"] == rows[(ACCOUNT, OPP2)]["freshness"]["source_link"]
    risk = next(i for i in today["attention"] if i["kind"] == "risk_review")
    assert risk["link"] == rows[(ACCOUNT, OPP)]["risks"][0]["link"]
    assert h.get(f"/api/command-center/sources/{stale_source}/review")["source"]["latest_revision"] == 2

    action = rows[(ACCOUNT, OPP)]["next_action"]
    assert h.get(f"/api/command-center/actions/{action['action_id']}")["overdue"] is True
    completed = h.client.post(
        f"/api/command-center/actions/{action['action_id']}/transitions",
        json={"to_status": "completed", "reason": "Synthetic walkthrough completion"},
    )
    assert completed.status_code == 200
    assert not any(i.get("action_id") == action["action_id"] for i in h.get("/api/command-center/today")["attention"])
    after = next(r for r in h.get("/api/command-center/portfolio")["opportunities"] if r["opportunity_slug"] == OPP)
    assert after["action_counts"]["overdue"] == 0
    assert after["action_counts"]["completed"] == 1
    assert h.get(f"/api/command-center/actions/{action['action_id']}")["status"] == "completed"
    changes = h.get("/api/command-center/changes", change_type="action_transition")["changes"]
    assert changes[0]["subject_id"] == action["action_id"] and changes[0]["after"]["status"] == "completed"

    corrected = h.client.post(
        f"/api/command-center/actions/{action['action_id']}/undo",
        json={"reason": "Synthetic correction: still outstanding"},
    )
    assert corrected.status_code == 200
    assert any(i.get("action_id") == action["action_id"] for i in h.get("/api/command-center/today")["attention"])
    assert h.get("/api/command-center/actions", overdue=True)["total"] == 1
    assert next(r for r in h.get("/api/command-center/portfolio")["opportunities"] if r["opportunity_slug"] == OPP)["action_counts"]["overdue"] == 1
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
        "crm_only": [], "crm_only_count": 0,
        "coverage": h.reads.portfolio()["coverage"],
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


@pytest.mark.asyncio
async def test_action_detail_is_reachable_by_id_beyond_the_list_page_bound(tmp_path) -> None:
    """Today/Changes/Portfolio deep-link actions by stable ID; detail must not depend on a global list page."""
    h, first, second = await _two_opportunities(tmp_path)
    template = h.ops._load_action(h.get("/api/command-center/actions", overdue=True)["actions"][0]["action_id"])
    for index in range(210):
        clone = template.model_copy(update={
            "action_id": f"act_{index:032x}",
            "observation_key": f"{index:016x}",
            "commitment": f"Synthetic filler action {index}",
        })
        h.ops._write_record(h.ops._action_path(clone.action_id, create=True), clone.model_dump(mode="json"))
    listing = h.get("/api/command-center/actions", limit=200, include_retracted=True)
    assert listing["total"] > 200 and listing["next_offset"] == 200
    last_id = f"act_{209:032x}"
    assert all(a["action_id"] != last_id for a in listing["actions"])  # not on the first page of 200
    detail = h.get(f"/api/command-center/actions/{last_id}")
    second_page = h.get("/api/command-center/actions", limit=200, offset=200, include_retracted=True)["actions"]
    row = next(a for a in second_page if a["action_id"] == last_id)
    for key in ("overdue", "due_soon", "opportunity_link", "provenance", "allowed_transitions", "status"):
        assert detail[key] == row[key], key
    assert detail["overdue"] is True and "completed" in detail["allowed_transitions"]
    done = h.client.post(
        f"/api/command-center/actions/{last_id}/transitions",
        json={"to_status": "completed", "reason": "Synthetic completion"},
    )
    assert done.status_code == 200
    after = h.get(f"/api/command-center/actions/{last_id}")
    assert after["status"] == "completed" and after["overdue"] is False and after["allowed_transitions"] == ["open"]
    assert h.client.get("/api/command-center/actions/act_" + "f" * 32).status_code == 404
    assert h.client.get("/api/command-center/actions/nope").status_code == 404


@pytest.mark.asyncio
async def test_ui_association_path_confirm_reconcile_correct_and_clear(tmp_path) -> None:
    """The exact request sequence the source-review page issues: candidates -> PUT association -> reconcile -> correct -> DELETE."""
    h = ReadHarness(tmp_path)
    await h.bootstrap_overview()
    await h.bootstrap_second()
    source_id = h.import_variant("not_SYNTH000000009", extra_line=SANDBOX_LINE)

    today = h.get("/api/command-center/today")
    item = next(i for i in today["attention"] if i["kind"] == "association_review")
    assert item["account"] is None and item["opportunity_link"] is None and item["source_id"] == source_id
    assert today["counts_by_kind"] == {"association_review": 1}

    candidates = h.get("/api/command-center/opportunities")["opportunities"]
    assert {(c["account"], c["opportunity_slug"]) for c in candidates} == {(ACCOUNT, OPP), (ACCOUNT, OPP2)}
    review = h.get(f"/api/command-center/sources/{source_id}/review")
    assert review["capabilities"]["reconcile"] is False and review["overview_base"] is None
    assert h.client.post(f"/api/command-center/sources/{source_id}/association", json={}).status_code == 405

    confirmed = h.client.put(
        f"/api/command-center/sources/{source_id}/association",
        json={"account": ACCOUNT, "opportunity_slug": OPP, "reason": "Chosen in synthetic UI test"},
    )
    assert confirmed.status_code == 200, confirmed.text
    review = h.get(f"/api/command-center/sources/{source_id}/review")
    base = review["overview_base"]
    assert review["capabilities"]["reconcile"] is True and base["status"] == "current"
    assert base["opportunity_link"] == f"#/opp/{ACCOUNT}/{OPP}/{OPP}"
    assert h.get("/api/command-center/today")["counts_by_kind"] == {"source_pending": 1}

    h.set_recs(source_id, 1, lambda eid: [
        _recommendation(eid, key="sandbox", action="Confirm sandbox access", owner="Customer Synthetic Buyer", due="2026-10-15"),
    ])
    started = h.client.post(
        f"/api/command-center/sources/{source_id}/reconcile",
        json={"base_version_id": base["version_id"], "base_revision": base["revision"]},
    )
    assert started.status_code == 202, started.text
    assert (await _wait(h.jobs, started.json()["job_id"]))["ok"] is True
    job = h.get(f"/api/command-center/reconciliations/{started.json()['job_id']}")
    assert job["ok"] is True
    review = h.get(f"/api/command-center/sources/{source_id}/review")
    assert review["source"]["processing"]["status"] == "processed"
    assert [a["opportunity_slug"] for a in review["derived_actions"]] == [OPP]
    assert h.get("/api/command-center/today")["counts_by_kind"] == {"due_action": 1}

    corrected = h.client.put(
        f"/api/command-center/sources/{source_id}/association",
        json={"account": ACCOUNT, "opportunity_slug": OPP2, "reason": "Wrong opportunity"},
    )
    assert corrected.status_code == 200, corrected.text
    review = h.get(f"/api/command-center/sources/{source_id}/review")
    assert review["source"]["association"]["opportunity_slug"] == OPP2
    assert review["overview_base"]["opportunity_link"] == f"#/opp/{ACCOUNT}/{OPP2}/{OPP2}"
    assert all(a["retracted"] for a in review["derived_actions"])
    assert h.get("/api/command-center/today")["counts_by_kind"] == {"source_pending": 1}
    assert h.get("/api/command-center/changes", change_type="association_corrected")["total"] == 1

    cleared = h.client.request(
        "DELETE", f"/api/command-center/sources/{source_id}/association", json={"reason": "Not ours"}
    )
    assert cleared.status_code == 200, cleared.text
    review = h.get(f"/api/command-center/sources/{source_id}/review")
    assert review["source"]["association"]["state"] != "associated" and review["overview_base"] is None
    assert review["capabilities"]["reconcile"] is False
    assert h.get("/api/command-center/today")["counts_by_kind"] == {"association_review": 1}
    states = [x["state"] for x in review["source"]["association_history"]]
    assert states[-1] != "associated" and states.count("associated") == 2
