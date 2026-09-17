from __future__ import annotations

import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.opportunity_state import router
from eval.tests.test_opportunity_state_create_service import _harness


def _client(tmp_path):
    service, state, jobs, executor, transcript, evidence_id = _harness(tmp_path)
    app = FastAPI()
    app.state.opportunity_state_create_service = service
    app.state.opportunity_state_service = state
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
    for value in (evidence_id[:-1] + "x", "../Acme-09.17.26.txt", "C:/raw/customer.txt"):
        response = client.post(
            "/api/accounts/Acme/opportunities/synthetic-opportunity/overview/create",
            json={"transcript_ids": [value]},
        )
        assert response.status_code == 400
