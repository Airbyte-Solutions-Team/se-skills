from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.opportunity_state import router
from services.opportunity_state_update_service import OpportunityStateUpdateService
from eval.tests.test_opportunity_state_create_service import _harness
from eval.tests.test_opportunity_state_create_service import _candidate_for
from services.opportunity_state_executor import FakeCanonicalStateExecutor


def _client(tmp_path):
    service, state, jobs, executor, transcript, evidence_id = _harness(tmp_path)
    app = FastAPI()
    app.state.opportunity_state_create_service = service
    app.state.opportunity_state_service = state
    app.state.opportunity_state_update_service = OpportunityStateUpdateService(
        workspace_service=service._workspace_service,
        transcription_service=service._transcription_service,
        state_service=state,
        job_service=jobs,
        executor=executor,
    )
    app.include_router(router)
    return TestClient(app), evidence_id


def test_local_overview_routes_create_job_and_current_state(tmp_path) -> None:
    client, evidence_id = _client(tmp_path)
    evidence = client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/evidence"
    )
    assert evidence.status_code == 200
    assert evidence.json()["metadata_included"] is True
    assert evidence.json()["transcripts"][0]["id"] == evidence_id
    assert "path" not in evidence.json()["transcripts"][0]

    started = client.post(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/create",
        json={"transcript_ids": [evidence_id]},
    )
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(100):
        response = client.get(
            f"/api/accounts/Acme/opportunities/synthetic-opportunity/overview/jobs/{job_id}"
        )
        assert response.status_code == 200
        if response.json()["status"] != "running":
            break
        time.sleep(0.01)
    assert response.json()["ok"] is True
    assert "stdout" not in response.json() and "stderr" not in response.json()

    current = client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/state"
    )
    assert current.status_code == 200
    assert current.json()["revision"] == 1
    assert current.json()["state"]["brief"]["customer_objective"]["value"]

    second = client.post(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/create",
        json={"transcript_ids": [evidence_id]},
    )
    assert second.status_code == 409
    assert "Slice 2B" in second.json()["detail"]


def test_routes_reject_no_selection_extra_fields_and_unknown_job(tmp_path) -> None:
    client, evidence_id = _client(tmp_path)
    empty = client.post(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/create",
        json={"transcript_ids": []},
    )
    assert empty.status_code == 422
    extra = client.post(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/create",
        json={"transcript_ids": [evidence_id], "path": "../secret.txt"},
    )
    assert extra.status_code == 422
    unknown = client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/jobs/missing"
    )
    assert unknown.status_code == 404
    missing_state = client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/state"
    )
    assert missing_state.status_code == 404


def test_routes_reject_tampered_and_arbitrary_transcript_identifiers(tmp_path) -> None:
    client, evidence_id = _client(tmp_path)
    replacement = "A" if evidence_id[-1] != "A" else "B"
    for value in (evidence_id[:-1] + replacement, "../Acme-09.17.26.txt", "C:/raw/customer.txt"):
        response = client.post(
            "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/create",
            json={"transcript_ids": [value]},
        )
        assert response.status_code == 400


def test_update_freshness_history_and_scoped_job_routes(tmp_path) -> None:
    client, first_id = _client(tmp_path)
    created = client.post(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/create",
        json={"transcript_ids": [first_id]},
    )
    create_job = created.json()["job_id"]
    for _ in range(100):
        if client.get(
            f"/api/accounts/Acme/opportunities/synthetic-opportunity/overview/jobs/{create_job}"
        ).json()["status"] != "running":
            break
        time.sleep(0.01)

    service = client.app.state.opportunity_state_update_service
    first_path = Path(tmp_path) / "customers" / "_transcripts" / "Acme-09.17.26.txt"
    first_path.with_name("Acme-09.18.26.txt").write_text("second synthetic delta", encoding="utf-8")
    listed = service._transcription_service.list_evidence_transcripts("Acme")
    second_id = next(item["id"] for item in listed if item["id"] != first_id)
    service._executor = FakeCanonicalStateExecutor(_candidate_for(second_id), cli_version="2.1.272")

    freshness = client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/freshness"
    )
    assert freshness.status_code == 200
    assert freshness.json()["new_source_count"] == 1
    assert "path" not in freshness.text.lower()
    assert "second synthetic delta" not in freshness.text

    unknown = client.post(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/update",
        json={
            "base_version_id": freshness.json()["base_version_id"],
            "base_revision": 1,
            "transcript_ids": [second_id],
            "generated_output": "forbidden",
        },
    )
    assert unknown.status_code == 422
    started = client.post(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/update",
        json={
            "base_version_id": freshness.json()["base_version_id"],
            "base_revision": 1,
            "transcript_ids": [second_id],
        },
    )
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    for _ in range(100):
        job = client.get(
            f"/api/accounts/Acme/opportunities/synthetic-opportunity/overview/update/jobs/{job_id}"
        )
        if job.json()["status"] != "running":
            break
        time.sleep(0.01)
    assert job.json()["ok"] is True
    assert "stdout" not in job.text and "stderr" not in job.text

    history = client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/history"
    )
    assert [item["revision"] for item in history.json()["versions"]] == [2, 1]
    historical = client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/history/1"
    )
    assert historical.status_code == 200
    assert historical.json()["revision"] == 1
    assert client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/history/999"
    ).status_code == 404
    assert client.get(
        "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/update/jobs/missing"
    ).status_code == 404
