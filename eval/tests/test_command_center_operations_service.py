"""Synthetic end-to-end tests for Command Center reconciliation (PR C).

Everything here runs against fixtures and the deterministic fake executor; no
Granola transport, no Claude runtime, no credentials, no customer content.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from command_center_operations import ActionRecord, SourceRef, action_id_for
from integrations.granola import ManualGranolaImportAdapter
from opportunity_state import ActionStatus, EvidenceReference, EvidenceSourceType, RecommendedAction
from services.command_center_operations_service import (
    CommandCenterOperationsError,
    CommandCenterOperationsService,
    evidence_id_for,
)
from services.evidence_ledger_service import EvidenceLedgerService
from services.opportunity_state_executor import (
    CanonicalStateExecutionError,
    CanonicalStateExecutionResult,
    FakeCanonicalStateExecutor,
)
from eval.tests.test_opportunity_state_create_service import _candidate_for, _harness, _wait


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "command_center" / "granola"
ACCOUNT, OPP = "Acme", "synthetic-opportunity"
SENTINELS = ("Synthetic line", "Synthetic summary", "buyer@acme-synthetic")


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _recommendation(evidence_id: str, *, key: str, action: str, owner: str | None, due: str | None,
                    status: ActionStatus = ActionStatus.NOT_STARTED) -> RecommendedAction:
    return RecommendedAction(
        key=key, action=action, goal="Synthetic goal.", definition_of_done="Synthetic done.",
        owner=owner, due_date=due, status=status,
        evidence_refs=[EvidenceReference(
            source_type=EvidenceSourceType.TRANSCRIPT, source_id=evidence_id, locator="00:00:05",
        )],
    )


def _candidate_with(first_id: str, evidence_id: str, recommendations: list[RecommendedAction]):
    cand = _candidate_for(first_id)
    return cand.model_copy(update={"recommended_actions": [*cand.recommended_actions, *recommendations]})


DEFAULT_RECS = lambda eid: [  # noqa: E731
    _recommendation(eid, key="send-arch", action="Send architecture diagram", owner="Airbyte SE", due="2026-10-01"),
    _recommendation(eid, key="security-q", action="Share security questionnaire", owner=None, due=None),
]


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.create, self.state, self.jobs, self.executor, self.first_path, self.first_id = _harness(tmp_path)
        self.customers = tmp_path / "customers"
        self.ledger = EvidenceLedgerService(self.customers)
        self.adapter = ManualGranolaImportAdapter()
        self.workspace = self.create._workspace_service
        self.ops = CommandCenterOperationsService(
            ledger=self.ledger, workspace_service=self.workspace, state_service=self.state,
            job_service=self.jobs, executor=self.executor,
        )

    async def bootstrap_overview(self) -> None:
        started = await self.create.start_create(ACCOUNT, OPP, [self.first_id])
        assert (await _wait(self.jobs, started["job_id"]))["ok"] is True

    def import_note(self, name: str) -> str:
        meeting = self.adapter.normalize(fixture(name), connection_id="local-manual")
        return self.ledger.import_meetings([meeting])["results"][0]["source_id"]

    async def confirm(self, source_id: str, account: str = ACCOUNT, opp: str = OPP) -> dict:
        return await self.ops.confirm_association(
            source_id, account=account, opportunity_slug=opp, reason="Synthetic confirmation",
            resolve_identity=self.workspace.resolve_identity,
        )

    def set_recs(self, source_id: str, revision: int, recs=None) -> str:
        eid = evidence_id_for(source_id, revision)
        recs = DEFAULT_RECS(eid) if recs is None else recs(eid)
        self.ops._executor = FakeCanonicalStateExecutor(
            _candidate_with(self.first_id, eid, recs), cli_version="2.1.272"
        )
        return eid

    async def reconcile(self, source_id: str) -> dict:
        current = self.state.read_current(ACCOUNT, OPP)
        started = await self.ops.start_reconciliation(
            source_id, base_version_id=current.version_id, base_revision=current.revision
        )
        if started.get("reused"):
            return started
        return await _wait(self.jobs, started["job_id"])

    def actions(self, **kw) -> list[dict]:
        return self.ops.list_actions(ACCOUNT, OPP, **kw)["actions"]

    def changes(self) -> list[dict]:
        return self.ops.list_changes(ACCOUNT, OPP)["changes"]

    def assert_no_content(self) -> None:
        blobs = [
            json.dumps(self.ops.list_actions(ACCOUNT, OPP, include_retracted=True)),
            json.dumps(self.ops.list_changes(ACCOUNT, OPP)),
            json.dumps({k: v for k, v in self.jobs.jobs.items()}, default=str),
        ]
        for path in (self.customers / ".command-center" / "operations").rglob("*.json"):
            blobs.append(path.read_text(encoding="utf-8"))
        for blob in blobs:
            for sentinel in SENTINELS:
                assert sentinel not in blob


async def _flow(tmp_path: Path) -> tuple[Harness, str]:
    h = Harness(tmp_path)
    await h.bootstrap_overview()
    source_id = h.import_note("note_synthetic_v1.json")
    await h.confirm(source_id)
    h.set_recs(source_id, 1)
    return h, source_id


class FailingExecutor:
    def __init__(self, code: str = "timeout") -> None:
        self.code = code
        self.calls = 0

    async def execute(self, request):
        self.calls += 1
        raise CanonicalStateExecutionError(self.code, "synthetic failure")


# ------------------------------------------------------------------ happy path


@pytest.mark.asyncio
async def test_end_to_end_confirmed_meeting_promotes_overview_and_creates_actions(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    job = await h.reconcile(source_id)
    assert job["ok"] is True, job
    assert job["kind"] == "command_center_reconcile"
    assert job["result_revision"] == 2
    assert len(job["actions_created"]) == 2

    current = h.state.read_current(ACCOUNT, OPP)
    assert current.revision == 2
    assert current.provenance.updater_version
    eid = evidence_id_for(source_id, 1)
    assert any(e.source_id == eid for e in current.evidence_manifest)
    request = h.ops._executor.requests[0]
    assert request.transcripts[0].evidence_id == eid
    assert b"Synthetic line one" in request.transcripts[0].content  # runtime saw private content

    source = h.ledger.get_source(source_id)
    assert source["processing"]["status"] == "processed"
    assert source["processing"]["processed_revision"] == 1

    actions = h.actions()
    by_commitment = {a["commitment"]: a for a in actions}
    explicit = by_commitment["Send architecture diagram"]
    assert explicit["status"] == "open" and explicit["party"] == "Airbyte" and explicit["due_date"] == "2026-10-01"
    vague = by_commitment["Share security questionnaire"]
    assert vague["status"] == "proposed" and vague["party"] == "Unknown"
    for action in actions:
        assert action["origin"]["source_id"] == source_id and action["origin"]["revision"] == 1
        assert action["origin"]["evidence_id"] == eid
        assert action["transitions"][0]["actor"] == "analysis"
        assert action["human_touched"] is False
    assert explicit["action_id"] == action_id_for(
        workspace_id=h.ledger.scope().workspace_id, account=ACCOUNT, opportunity_slug=OPP,
        source_id=source_id, observation_key=explicit["observation_key"],
    )

    types = [c["change_type"] for c in h.changes()]
    assert types.count("overview_revision") == 1
    assert types.count("action_created") == 2
    overview_change = next(c for c in h.changes() if c["change_type"] == "overview_revision")
    assert overview_change["before"]["revision"] == 1 and overview_change["after"]["revision"] == 2
    assert overview_change["source"]["source_id"] == source_id
    runs = h.ops.list_runs(source_id)
    assert [r["status"] for r in runs] == ["succeeded"]
    assert runs[0]["promoted_revision"] == 2 and runs[0]["observation_count"] == 2
    h.assert_no_content()


@pytest.mark.asyncio
async def test_reconcile_requires_confirmed_association_and_existing_overview(tmp_path) -> None:
    h = Harness(tmp_path)
    source_id = h.import_note("note_synthetic_v1.json")
    with pytest.raises(CommandCenterOperationsError) as exc:
        await h.ops.start_reconciliation(source_id, base_version_id="0" * 32, base_revision=1)
    assert exc.value.code == "not_associated"
    await h.confirm(source_id)
    with pytest.raises(CommandCenterOperationsError) as missing:
        await h.ops.start_reconciliation(source_id, base_version_id="0" * 32, base_revision=1)
    assert missing.value.code == "overview_missing"
    assert h.actions() == []
    assert h.ledger.get_source(source_id)["processing"]["status"] == "queued"


# ------------------------------------------------------- idempotency / safety


@pytest.mark.asyncio
async def test_repeat_of_processed_revision_is_refused_and_replay_does_not_duplicate(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    assert (await h.reconcile(source_id))["ok"] is True
    with pytest.raises(CommandCenterOperationsError) as exc:
        await h.reconcile(source_id)
    assert exc.value.code == "already_processed"
    assert h.state.read_current(ACCOUNT, OPP).revision == 2
    assert len(h.actions()) == 2

    # Direct replay of the same (revision, observation key) through the trusted layer: no-op.
    version = h.state.read_current(ACCOUNT, OPP)
    ref = SourceRef(source_id=source_id, revision=1, evidence_id=evidence_id_for(source_id, 1))
    obs = h.ops.derive_observations(version.state, ref)
    applied = h.ops._apply_observations(
        obs, account=ACCOUNT, opp_slug=OPP, version=version, source_ref=ref, occurred_at=version.created_at,
    )
    assert applied == {"actions_created": [], "actions_linked": [], "completion_suggestions": [], "possible_duplicates": []}
    assert len(h.actions()) == 2
    assert [c["change_type"] for c in h.changes()].count("overview_revision") == 1


@pytest.mark.asyncio
async def test_failed_analysis_leaves_accepted_state_intact_and_is_retryable(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    failing = FailingExecutor("timeout")
    h.ops._executor = failing
    job = await h.reconcile(source_id)
    assert job["ok"] is False and job["error_code"] == "timeout"
    assert "synthetic failure" not in json.dumps(job)
    assert h.state.read_current(ACCOUNT, OPP).revision == 1
    assert h.actions() == [] and h.changes() == []
    source = h.ledger.get_source(source_id)
    assert source["processing"]["status"] == "failed" and source["processing"]["retry_eligible"] is True
    assert [r["status"] for r in h.ops.list_runs(source_id)] == ["failed"]

    h.set_recs(source_id, 1)
    job = await h.reconcile(source_id)
    assert job["ok"] is True
    assert h.state.read_current(ACCOUNT, OPP).revision == 2
    assert len(h.actions()) == 2
    assert [r["status"] for r in h.ops.list_runs(source_id)] == ["failed", "succeeded"]


@pytest.mark.asyncio
async def test_invalid_candidate_is_rejected_before_promotion(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    h.set_recs(source_id, 1, lambda _eid: [
        _recommendation("tr_unauthorized_source_id_000000", key="rogue", action="Rogue", owner=None, due=None),
    ])
    job = await h.reconcile(source_id)
    assert job["ok"] is False and job["error_code"] == "invalid_candidate"
    assert h.state.read_current(ACCOUNT, OPP).revision == 1
    assert h.actions() == []


@pytest.mark.asyncio
async def test_stale_base_is_refused_at_start_and_during_run(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    current = h.state.read_current(ACCOUNT, OPP)
    with pytest.raises(CommandCenterOperationsError) as exc:
        await h.ops.start_reconciliation(source_id, base_version_id="f" * 32, base_revision=1)
    assert exc.value.code == "stale_base"
    assert h.ledger.get_source(source_id)["processing"]["status"] == "queued"

    # Base moves while the executor is running: promotion must refuse with CAS.
    import asyncio

    class Racing:
        def __init__(self, inner):
            self.inner = inner
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, request):
            self.started.set()
            await self.release.wait()
            return await self.inner.execute(request)

    racing = Racing(h.ops._executor)
    h.ops._executor = racing
    started = await h.ops.start_reconciliation(
        source_id, base_version_id=current.version_id, base_revision=current.revision
    )
    await racing.started.wait()
    # Another writer promotes revision 2 underneath us via the state service.
    other = h.create._executor.candidate
    from opportunity_state import GenerationProvenance, OpportunityIdentity
    h.state.promote_update(
        identity=OpportunityIdentity(account=ACCOUNT, opportunity_slug=OPP, opportunity_name="Synthetic Opportunity"),
        expected_parent_version_id=current.version_id,
        expected_parent_revision=current.revision,
        evidence_manifest=list(current.evidence_manifest),
        expected_manifest_hash=current.evidence_manifest_hash,
        provenance=GenerationProvenance(updater_version="t", model="t", runtime="t", cli_version="t"),
        candidate=other,
    )
    racing.release.set()
    job = await _wait(h.jobs, started["job_id"])
    assert job["ok"] is False and job["error_code"] == "stale_base"
    after = h.state.read_current(ACCOUNT, OPP)
    assert after.revision == 2 and after.provenance.model == "t"
    assert h.actions() == []
    assert [r["status"] for r in h.ops.list_runs(source_id)] == ["stale_base"]


@pytest.mark.asyncio
async def test_interrupted_processing_is_recovered_on_next_start(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    # Simulate a crash: the ledger says processing but no job owns it.
    h.ledger.mark_processing(source_id)
    assert h.ledger.get_source(source_id)["processing"]["status"] == "processing"
    job = await h.reconcile(source_id)
    assert job["ok"] is True
    statuses = [r["status"] for r in h.ops.list_runs(source_id)]
    assert statuses == ["interrupted", "succeeded"]
    assert h.ledger.get_source(source_id)["processing"]["attempts"] == 2


@pytest.mark.asyncio
async def test_concurrent_start_reuses_running_job_and_blocks_other_overview_work(tmp_path) -> None:
    import asyncio
    from eval.tests.test_opportunity_state_create_service import BlockingExecutor

    h, source_id = await _flow(tmp_path)
    blocking = BlockingExecutor(h.ops._executor.candidate)
    h.ops._executor = blocking
    current = h.state.read_current(ACCOUNT, OPP)
    first = await h.ops.start_reconciliation(source_id, base_version_id=current.version_id, base_revision=1)
    await blocking.started.wait()
    again = await h.ops.start_reconciliation(source_id, base_version_id=current.version_id, base_revision=1)
    assert again == {"job_id": first["job_id"], "reused": True, "source_id": source_id}
    assert h.jobs.active_opportunity_state_job(account=ACCOUNT, opp_slug=OPP) is not None
    blocking.release.set()
    assert (await _wait(h.jobs, first["job_id"]))["ok"] is True
    assert h.jobs.active_opportunity_state_job(account=ACCOUNT, opp_slug=OPP) is None
    await asyncio.sleep(0)


# ------------------------------------------------ human precedence / corrections


@pytest.mark.asyncio
async def test_human_completion_and_corrections_survive_edited_reimport(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    assert (await h.reconcile(source_id))["ok"] is True
    explicit = next(a for a in h.actions() if a["commitment"] == "Send architecture diagram")
    vague = next(a for a in h.actions() if a["commitment"] == "Share security questionnaire")

    completed = h.ops.transition_action(
        explicit["action_id"], to_status="completed", reason="Sent on the call",
    )
    assert completed["status"] == "completed" and completed["human_touched"] is True
    opened = h.ops.transition_action(
        vague["action_id"], to_status="open", reason="Customer owns this", owner="Customer data team",
        due_date="2026-10-15",
    )
    assert opened["status"] == "open" and opened["party"] == "Customer" and opened["due_date"] == "2026-10-15"

    # Edited note arrives: same commitments, analysis again says NOT_STARTED with the old owner.
    edited_id = h.import_note("note_synthetic_v2_edited.json")
    assert edited_id == source_id
    source = h.ledger.get_source(source_id)
    assert source["latest_revision"] == 2 and source["processing"]["status"] == "queued"
    h.set_recs(source_id, 2)
    job = await h.reconcile(source_id)
    assert job["ok"] is True
    assert job["actions_created"] == []
    assert sorted(job["actions_linked"]) == sorted([explicit["action_id"], vague["action_id"]])

    after = {a["action_id"]: a for a in h.actions()}
    assert len(after) == 2
    assert after[explicit["action_id"]]["status"] == "completed"
    assert after[vague["action_id"]]["status"] == "open"
    assert after[vague["action_id"]]["owner"] == "Customer data team"
    assert after[vague["action_id"]]["due_date"] == "2026-10-15"
    assert [e["revision"] for e in after[explicit["action_id"]]["evidence"]] == [1, 2]
    assert h.state.read_current(ACCOUNT, OPP).revision == 3
    types = [c["change_type"] for c in h.changes()]
    assert types.count("action_linked") == 2 and types.count("overview_revision") == 2
    assert h.ledger.get_source(source_id)["processing"]["processed_revision"] == 2
    h.assert_no_content()


@pytest.mark.asyncio
async def test_completion_is_suggested_not_applied(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    assert (await h.reconcile(source_id))["ok"] is True
    explicit = next(a for a in h.actions() if a["commitment"] == "Send architecture diagram")
    h.import_note("note_synthetic_v2_edited.json")
    h.set_recs(source_id, 2, lambda eid: [
        _recommendation(eid, key="send-arch", action="Send architecture diagram", owner="Airbyte SE",
                        due="2026-10-01", status=ActionStatus.DONE),
    ])
    job = await h.reconcile(source_id)
    assert job["ok"] is True
    assert job["completion_suggestions"] == [explicit["action_id"]]
    assert job["actions_created"] == []
    updated = h.ops.get_action(explicit["action_id"])
    assert updated["status"] == "open"
    assert len(updated["completion_suggestions"]) == 1
    assert updated["completion_suggestions"][0]["source"]["revision"] == 2
    assert "completion_suggested" in [c["change_type"] for c in h.changes()]


@pytest.mark.asyncio
async def test_transition_rules_and_undo_keep_history(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    assert (await h.reconcile(source_id))["ok"] is True
    vague = next(a for a in h.actions() if a["status"] == "proposed")
    with pytest.raises(CommandCenterOperationsError) as bad:
        h.ops.transition_action(vague["action_id"], to_status="completed", reason="skip ahead")
    assert bad.value.code == "invalid_transition"
    with pytest.raises(CommandCenterOperationsError) as nothing:
        h.ops.undo_last_transition(vague["action_id"], reason="nothing yet")
    assert nothing.value.code == "nothing_to_undo"

    dismissed = h.ops.transition_action(vague["action_id"], to_status="dismissed", reason="Not relevant")
    assert dismissed["status"] == "dismissed"
    undone = h.ops.undo_last_transition(vague["action_id"], reason="Dismissed by mistake")
    assert undone["status"] == "proposed"
    assert [t["to_status"] for t in undone["transitions"]] == ["proposed", "dismissed", "proposed"]
    assert undone["transitions"][-1]["undoes_sequence"] == 2
    with pytest.raises(CommandCenterOperationsError) as twice:
        h.ops.undo_last_transition(vague["action_id"], reason="again")
    assert twice.value.code == "nothing_to_undo"
    ActionRecord.model_validate(h.ops._load_action(vague["action_id"]).model_dump(mode="json"))
    transitions = [c for c in h.changes() if c["change_type"] == "action_transition"]
    assert len(transitions) == 2
    assert transitions[0]["after"]["undoes_sequence"] == 2  # newest first
    with pytest.raises(CommandCenterOperationsError) as unknown:
        h.ops.get_action("act_" + "0" * 32)
    assert unknown.value.code == "unknown_action"


@pytest.mark.asyncio
async def test_association_correction_retracts_derived_actions_without_deleting(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    (h.customers / "Other").mkdir()
    assert (await h.reconcile(source_id))["ok"] is True
    ids = sorted(a["action_id"] for a in h.actions())

    async def resolve(account: str, opp_slug: str) -> dict:
        return {"safe_account": account, "safe_opp": opp_slug, "opportunity": {"sfdc_id": None, "sfdc_account_id": None}}

    summary = await h.ops.confirm_association(
        source_id, account="Other", opportunity_slug="deal", reason="Wrong opportunity", resolve_identity=resolve,
    )
    assert sorted(summary["retracted_actions"]) == ids
    assert summary["association"]["account"] == "Other"
    assert len(h.ledger.get_source(source_id)["association_history"]) >= 2
    assert h.actions() == []
    retracted = h.actions(include_retracted=True)
    assert len(retracted) == 2
    for action in retracted:
        assert action["retracted"] is True
        assert action["retraction"]["to_account"] == "Other"
        assert action["status"] in {"open", "proposed"}  # status kept
    with pytest.raises(CommandCenterOperationsError) as exc:
        h.ops.transition_action(ids[0], to_status="open", reason="try")
    assert exc.value.code == "retracted"
    assert [c["change_type"] for c in h.changes()].count("association_corrected") == 2
    # Overview for the original opportunity is untouched by the correction.
    assert h.state.read_current(ACCOUNT, OPP).revision == 2

    # Clearing is a no-op for retraction (already retracted), still allowed.
    cleared = h.ops.clear_association(source_id, reason="unsure")
    assert cleared["association"]["state"] == "unassociated"
    assert cleared["retracted_actions"] == []


@pytest.mark.asyncio
async def test_similar_commitment_from_second_meeting_is_flagged_not_merged(tmp_path) -> None:
    h, source_id = await _flow(tmp_path)
    assert (await h.reconcile(source_id))["ok"] is True
    first = next(a for a in h.actions() if a["commitment"] == "Send architecture diagram")

    second = h.import_note("mcp_meeting_synthetic.json")
    assert second != source_id
    await h.confirm(second)
    h.set_recs(second, 1, lambda eid: [
        _recommendation(eid, key="send-arch-2", action="Send  Architecture diagram.", owner="Airbyte SE", due="2026-10-02"),
    ])
    job = await h.reconcile(second)
    assert job["ok"] is True
    assert len(job["actions_created"]) == 1 and job["possible_duplicates"] == job["actions_created"]
    flagged = h.ops.get_action(job["actions_created"][0])
    assert flagged["status"] == "proposed"
    assert flagged["possible_duplicate_of"] == first["action_id"]
    assert flagged["origin"]["source_id"] == second
    assert h.ops.get_action(first["action_id"])["status"] == "open"
    assert "possible_duplicate_flagged" in [c["change_type"] for c in h.changes()]
    assert h.state.read_current(ACCOUNT, OPP).revision == 3


@pytest.mark.asyncio
async def test_edit_between_start_and_run_is_superseded_safely(tmp_path) -> None:
    from services.job_service import ManagedJobError

    h, source_id = await _flow(tmp_path)
    # The start snapshot saw r1; an edited note lands before the job reads content.
    h.ledger.mark_processing(source_id)
    h.import_note("note_synthetic_v2_edited.json")
    assert h.ledger.get_source(source_id)["latest_revision"] == 2
    with pytest.raises(ManagedJobError) as exc:
        await h.ops._reconcile(
            source_id, revision=1, account=ACCOUNT, opp_slug=OPP,
            identity=await h.workspace.resolve_identity(ACCOUNT, OPP),
            base_version_id=h.state.read_current(ACCOUNT, OPP).version_id, base_revision=1,
        )
    assert exc.value.code == "revision_superseded"
    assert h.state.read_current(ACCOUNT, OPP).revision == 1
    assert h.actions() == []
    assert [r["status"] for r in h.ops.list_runs(source_id)] == ["superseded"]
    source = h.ledger.get_source(source_id)
    assert source["processing"]["status"] == "failed" and source["processing"]["retry_eligible"] is True
    # Retrying on the new revision succeeds and creates the actions once.
    h.set_recs(source_id, 2)
    assert (await h.reconcile(source_id))["ok"] is True
    assert len(h.actions()) == 2
    assert h.ledger.get_source(source_id)["processing"]["processed_revision"] == 2


@pytest.mark.asyncio
async def test_workspace_isolation_and_private_permissions(tmp_path) -> None:
    import os
    import stat

    h, source_id = await _flow(tmp_path)
    assert (await h.reconcile(source_id))["ok"] is True
    ops_dir = h.customers / ".command-center" / "operations"
    if os.name == "posix":
        assert stat.S_IMODE(ops_dir.stat().st_mode) == 0o700
        for path in ops_dir.rglob("*.json"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600

    other_customers = tmp_path / "other-customers"
    other_customers.mkdir()
    other_ledger = EvidenceLedgerService(other_customers)
    other_ledger.import_meetings([h.adapter.normalize(fixture("note_synthetic_v1.json"), connection_id="local-manual")])
    other_ops = CommandCenterOperationsService(
        ledger=other_ledger, workspace_service=h.workspace, state_service=h.state,
        job_service=h.jobs, executor=h.ops._executor,
    )
    assert other_ops.list_actions(ACCOUNT, OPP)["total"] == 0
    # A record copied across workspaces is refused as scope mismatch.
    (other_customers / ".command-center" / "operations" / "actions").mkdir(parents=True)
    src = next((ops_dir / "actions").glob("act_*.json"))
    (other_customers / ".command-center" / "operations" / "actions" / src.name).write_bytes(src.read_bytes())
    with pytest.raises(CommandCenterOperationsError) as exc:
        other_ops.get_action(src.stem)
    assert exc.value.code == "scope_mismatch"


def test_snapshot_rendering_is_deterministic_and_bounded() -> None:
    from services.command_center_operations_service import render_snapshot_text

    snapshot = {
        "metadata": {"title": "T", "occurred_at": "2026-09-22T15:00:00Z", "attendees": [{"name": "A"}, {"email": "b@x"}]},
        "content": {"summary_text": "S", "transcript": [{"speaker_label": "me", "text": "hello"}, "junk"]},
    }
    first = render_snapshot_text(snapshot)
    assert first == render_snapshot_text(json.loads(json.dumps(snapshot)))
    assert first.decode() == "# T\nOccurred: 2026-09-22T15:00:00Z\nAttendees: A, b@x\n\n## Summary\nS\n\n## Transcript\n[me] hello\n"
    assert render_snapshot_text({}) == b"\n"


def test_executor_result_type_is_the_existing_runtime_contract() -> None:
    # Guard: reconciliation consumes exactly the existing executor result shape.
    fields = set(CanonicalStateExecutionResult.__dataclass_fields__)
    assert {"candidate", "model", "cli_version", "runtime"} <= fields
