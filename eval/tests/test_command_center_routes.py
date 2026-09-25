"""HTTP boundary tests for the local-only Command Center evidence routes."""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.granola import ManualGranolaImportAdapter
from routes.command_center import router
from services.account_service import AccountError
from services.command_center_operations_service import CommandCenterOperationsService, evidence_id_for
from services.evidence_ledger_service import EvidenceLedgerService
from services.job_service import JobService
from services.opportunity_state_executor import FakeCanonicalStateExecutor
from eval.tests.test_command_center_operations_service import ACCOUNT, OPP, DEFAULT_RECS, _candidate_with
from eval.tests.test_opportunity_state_create_service import _harness, _wait


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "command_center" / "granola"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ResolvedWorkspace:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def resolve_identity(self, account: str, opp_slug: str) -> dict:
        self.calls.append((account, opp_slug))
        if account == "missing":
            raise AccountError(404, "Unknown account")
        return {
            "safe_account": "Acme",
            "safe_opp": "expansion",
            "opportunity": {"sfdc_id": "006SYNTHETIC00001", "sfdc_account_id": None},
        }


class RejectingStateService:
    """Association routes only read the current Overview (to repair it on correction); nothing else."""

    def read_current(self, account: str, opp_slug: str):
        return None

    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected Overview state access: {name}")


class UnusedExecutor:
    async def execute(self, request):
        raise AssertionError("the executor must not run for association routes")


def _client(tmp_path) -> tuple[TestClient, ResolvedWorkspace]:
    (tmp_path / "customers").mkdir()
    app = FastAPI()
    workspace = ResolvedWorkspace()
    ledger = EvidenceLedgerService(tmp_path / "customers")
    app.state.opportunity_workspace_service = workspace
    app.state.evidence_ledger_service = ledger
    app.state.granola_adapter = ManualGranolaImportAdapter()
    app.state.job_service = JobService(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    app.state.command_center_operations_service = CommandCenterOperationsService(
        ledger=ledger,
        workspace_service=workspace,
        state_service=RejectingStateService(),
        job_service=app.state.job_service,
        executor=UnusedExecutor(),
    )
    app.include_router(router)
    return TestClient(app), workspace


def test_adapter_listing_is_manual_only(tmp_path) -> None:
    client, _ = _client(tmp_path)
    adapters = client.get("/api/command-center/adapters").json()["adapters"]
    assert adapters == [{
        "provider": "granola",
        "transport": "manual_import",
        "mode": "manual_import",
        "unattended_discovery": False,
        "requires_credentials": False,
        "label": "Manual Granola import (user-selected notes)",
        "payload_contracts": ["granola-rest-note-v1", "granola-mcp-meeting-v1"],
    }]


def test_import_association_and_unprocessed_flow(tmp_path) -> None:
    client, workspace = _client(tmp_path)
    imported = client.post(
        "/api/command-center/imports/granola",
        json={"notes": [fixture("note_synthetic_v1.json"), fixture("note_malformed.json")]},
    )
    assert imported.status_code == 201
    body = imported.json()
    assert body["trigger"] == "manual_import"
    assert body["unattended_discovery"] is False
    assert body["adapter"]["transport"] == "manual_import"
    assert body["rejected"] == [{
        "index": 1, "code": "malformed_payload",
        "detail": "Note payload does not match the documented Granola note shape.",
    }]
    assert "Synthetic line" not in imported.text
    source_id = body["results"][0]["source_id"]

    queue = client.get("/api/command-center/sources/unprocessed").json()
    assert queue["total"] == 1
    assert queue["counts_by_status"] == {"awaiting_association": 1}
    assert queue["sources"][0]["latest"]["trigger"] == "manual_import"

    proposed = client.post(
        f"/api/command-center/sources/{source_id}/association/proposals",
        json={"reason": "Attendee domain matched two opportunities", "candidates": [
            {"account": "Acme", "opportunity_slug": "expansion", "method": "domain", "reason": "domain"},
            {"account": "Acme", "opportunity_slug": "renewal", "method": "domain", "reason": "domain"},
        ]},
    )
    assert proposed.status_code == 200
    assert proposed.json()["association"]["state"] == "proposed"

    confirmed = client.put(
        f"/api/command-center/sources/{source_id}/association",
        json={"account": "untrusted", "opportunity_slug": "untrusted", "reason": "Confirmed by SE"},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["association"]["account"] == "Acme"
    assert confirmed.json()["association"]["crm_opportunity_id"] == "006SYNTHETIC00001"
    assert confirmed.json()["processing"]["status"] == "queued"
    assert workspace.calls == [("untrusted", "untrusted")]

    assert client.get("/api/command-center/sources?status=queued").json()["total"] == 1
    assert client.get("/api/command-center/sources?status=bogus").status_code == 422

    detail = client.get(f"/api/command-center/sources/{source_id}").json()
    assert [item["state"] for item in detail["association_history"]] == [
        "unassociated", "proposed", "associated"
    ]

    missing = client.put(
        f"/api/command-center/sources/{source_id}/association",
        json={"account": "missing", "opportunity_slug": "x", "reason": "nope"},
    )
    assert missing.status_code == 404

    cleared = client.request(
        "DELETE", f"/api/command-center/sources/{source_id}/association", json={"reason": "Internal sync"}
    )
    assert cleared.status_code == 200
    assert cleared.json()["association"]["state"] == "unassociated"

    assert client.post(f"/api/command-center/sources/{source_id}/retry").status_code == 200
    assert client.get("/api/command-center/sources/src_" + "0" * 32).status_code == 404
    assert client.get("/api/command-center/sources/../etc").status_code == 404


def test_import_rejects_all_malformed_and_bounds(tmp_path) -> None:
    client, _ = _client(tmp_path)
    rejected = client.post("/api/command-center/imports/granola", json={"notes": [fixture("note_malformed.json")]})
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["rejected"][0]["code"] == "malformed_payload"
    assert client.post("/api/command-center/imports/granola", json={"notes": []}).status_code == 422
    assert client.post(
        "/api/command-center/imports/granola", json={"notes": [fixture("note_synthetic_v1.json")] * 26}
    ).status_code == 422
    assert client.post(
        "/api/command-center/imports/granola",
        json={"notes": [fixture("note_synthetic_v1.json")], "api_key": "x"},
    ).status_code == 422
    assert client.post(
        "/api/command-center/imports/granola",
        json={"notes": [fixture("note_synthetic_v1.json")], "connection_id": "bad id/../x"},
    ).status_code == 422
    assert client.get("/api/command-center/sources").json()["total"] == 0


def _poll(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        body = client.get(f"/api/command-center/reconciliations/{job_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("reconciliation did not finish")


def test_reconcile_actions_and_changes_over_http(tmp_path) -> None:
    create, state, jobs, _executor, _path, first_id = _harness(tmp_path)
    import asyncio

    async def bootstrap() -> None:
        started = await create.start_create(ACCOUNT, OPP, [first_id])
        assert (await _wait(jobs, started["job_id"]))["ok"] is True

    asyncio.run(bootstrap())
    customers = tmp_path / "customers"
    ledger = EvidenceLedgerService(customers)
    adapter = ManualGranolaImportAdapter()
    source_id = ledger.import_meetings(
        [adapter.normalize(fixture("note_synthetic_v1.json"), connection_id="local-manual")]
    )["results"][0]["source_id"]
    evidence_id = evidence_id_for(source_id, 1)
    executor = FakeCanonicalStateExecutor(_candidate_with(first_id, evidence_id, DEFAULT_RECS(evidence_id)))

    app = FastAPI()
    app.state.opportunity_workspace_service = create._workspace_service
    app.state.evidence_ledger_service = ledger
    app.state.granola_adapter = adapter
    app.state.job_service = jobs
    app.state.command_center_operations_service = CommandCenterOperationsService(
        ledger=ledger, workspace_service=create._workspace_service, state_service=state,
        job_service=jobs, executor=executor,
    )
    app.include_router(router)

    with TestClient(app) as client:
        current = state.read_current(ACCOUNT, OPP)
        body = {"base_version_id": current.version_id, "base_revision": current.revision}
        unassociated = client.post(f"/api/command-center/sources/{source_id}/reconcile", json=body)
        assert unassociated.status_code == 409
        assert unassociated.json()["detail"] == "Confirm the opportunity association before reconciling."

        confirmed = client.put(
            f"/api/command-center/sources/{source_id}/association",
            json={"account": ACCOUNT, "opportunity_slug": OPP, "reason": "Confirmed by SE"},
        )
        assert confirmed.status_code == 200 and confirmed.json()["retracted_actions"] == []

        assert client.post(
            f"/api/command-center/sources/{source_id}/reconcile",
            json={"base_version_id": "0" * 32, "base_revision": 1},
        ).status_code == 409

        accepted = client.post(f"/api/command-center/sources/{source_id}/reconcile", json=body)
        assert accepted.status_code == 202, accepted.text
        job = _poll(client, accepted.json()["job_id"])
        assert job["ok"] is True and job["result_revision"] == 2
        assert "sig" not in job and "Synthetic line" not in json.dumps(job)

        assert client.get("/api/command-center/reconciliations/nope").status_code == 404
        runs = client.get(f"/api/command-center/sources/{source_id}/runs").json()["runs"]
        assert [r["status"] for r in runs] == ["succeeded"]

        actions = client.get(f"/api/command-center/opportunities/{ACCOUNT}/{OPP}/actions").json()
        assert actions["total"] == 2
        assert client.get(
            f"/api/command-center/opportunities/{ACCOUNT}/{OPP}/actions?status=proposed"
        ).json()["total"] == 1
        assert client.get(
            f"/api/command-center/opportunities/{ACCOUNT}/{OPP}/actions?status=bogus"
        ).status_code == 422
        proposed = next(a for a in actions["actions"] if a["status"] == "proposed")
        action_id = proposed["action_id"]

        moved = client.post(
            f"/api/command-center/actions/{action_id}/transitions",
            json={"to_status": "open", "reason": "Accepted", "owner": "Customer lead", "due_date": "2026-11-01"},
        )
        assert moved.status_code == 200
        assert moved.json()["status"] == "open" and moved.json()["party"] == "Customer"
        bad = client.post(
            f"/api/command-center/actions/{action_id}/transitions",
            json={"to_status": "proposed", "reason": "backwards"},
        )
        assert bad.status_code == 409
        undone = client.post(f"/api/command-center/actions/{action_id}/undo", json={"reason": "Oops"})
        assert undone.status_code == 200 and undone.json()["status"] == "proposed"
        assert undone.json()["owner"] is None
        assert client.get(f"/api/command-center/actions/{action_id}").json()["transitions"][-1]["undoes_sequence"] == 2
        assert client.get("/api/command-center/actions/act_" + "0" * 32).status_code == 404
        assert client.get("/api/command-center/actions/../../etc").status_code in {404, 422}

        changes = client.get(f"/api/command-center/opportunities/{ACCOUNT}/{OPP}/changes?limit=2").json()
        assert changes["total"] == 5 and len(changes["changes"]) == 2
        assert changes["changes"][0]["change_type"] == "action_transition"
        assert "Synthetic line" not in json.dumps(changes)
        assert client.get(f"/api/command-center/opportunities/{ACCOUNT}/{OPP}/changes?limit=0").status_code == 422
        assert client.get("/api/command-center/opportunities/Nope/none/actions").json()["total"] == 0
