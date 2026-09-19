"""Focused tests for the local Tech Eval readiness tracker."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from services.tech_eval_service import TECH_EVAL_TEMPLATE, TechEvalError, TechEvalService


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


def _safe(value: str) -> str:
    if not value or ".." in value or "/" in value or "\\" in value:
        raise ValueError("unsafe")
    return value


def _service(tmp_path, account: str = "Acme", clock=None) -> TechEvalService:
    (tmp_path / "customers" / account).mkdir(parents=True, exist_ok=True)
    return TechEvalService(tmp_path / "customers", safe_name=_safe, clock=clock or Clock())


def test_template_initialization_is_deterministic_and_gate_aware(tmp_path) -> None:
    service = _service(tmp_path)
    tracker = service.get_tracker("Acme", "expansion")

    assert [item["id"] for item in tracker["items"]] == [item["id"] for item in TECH_EVAL_TEMPLATE]
    assert [phase["phase"] for phase in tracker["summary"]["phases"]] == [
        "plan", "prepare", "execute", "validate", "close"
    ]
    assert tracker["summary"] == {
        "current_phase": "plan",
        "overall_state": "not_started",
        "overall_label": "Not started",
        "completed": 0,
        "total_applicable": len(TECH_EVAL_TEMPLATE),
        "blocking_gates": 4,
        "remaining": "Next gate: Agreed evaluation scope and dates; 3 other gates remain.",
        "phases": tracker["summary"]["phases"],
    }
    assert tracker["last_change_source"] is None
    assert all(item["last_updated"] is None for item in tracker["items"])


def test_status_transitions_and_manual_attribution(tmp_path) -> None:
    clock = Clock()
    service = _service(tmp_path, clock=clock)
    service.get_tracker("Acme", "expansion")

    blocked = service.update_item(
        "Acme", "expansion", "customer_success_criteria",
        {"status": "blocked", "owner": "SE", "note": "Waiting on customer approval"},
    )
    item = next(item for item in blocked["items"] if item["id"] == "customer_success_criteria")
    assert blocked["summary"]["overall_state"] == "blocked"
    assert blocked["summary"]["remaining"].startswith("Blocked gate: Customer-approved success criteria")
    assert item["update_source"] == "manual"
    assert item["last_updated"] == "2026-09-18T12:00:01Z"
    assert blocked["last_change_source"] == "manual"

    service.update_item("Acme", "expansion", "customer_success_criteria", {"status": "done"})
    in_progress = service.update_item(
        "Acme", "expansion", "evaluation_objective_use_cases", {"status": "in_progress"}
    )
    assert in_progress["summary"]["overall_state"] == "in_progress"

    current = in_progress
    for item in current["items"]:
        if item["type"] in ("gate", "required"):
            current = service.update_item("Acme", "expansion", item["id"], {"status": "done"})
    assert current["summary"]["overall_state"] == "ready_with_risks"
    for item in current["items"]:
        if item["status"] != "done":
            current = service.update_item("Acme", "expansion", item["id"], {"status": "done"})
    assert current["summary"]["overall_state"] == "complete"
    assert current["summary"]["completed"] == current["summary"]["total_applicable"]


def test_not_applicable_items_are_excluded_from_counts(tmp_path) -> None:
    service = _service(tmp_path)
    tracker = service.update_item(
        "Acme", "expansion", "incremental_cdc_validation", {"status": "not_applicable"}
    )
    assert tracker["summary"]["total_applicable"] == len(TECH_EVAL_TEMPLATE) - 1
    execute = next(phase for phase in tracker["summary"]["phases"] if phase["phase"] == "execute")
    assert execute["total_applicable"] == 2


def test_exact_account_and_opportunity_scoping(tmp_path) -> None:
    service = _service(tmp_path)
    (tmp_path / "customers" / "Beta").mkdir()
    service.update_item("Acme", "one", "technical_champion", {"owner": "Alice"})
    service.update_item("Acme", "two", "technical_champion", {"owner": "Bob"})
    service.update_item("Beta", "one", "technical_champion", {"owner": "Carol"})

    assert service.get_tracker("Acme", "one")["items"][1]["owner"] == "Alice"
    assert service.get_tracker("Acme", "two")["items"][1]["owner"] == "Bob"
    assert service.get_tracker("Beta", "one")["items"][1]["owner"] == "Carol"


def test_atomic_persistence_and_restart_reload(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path)
    service.update_item(
        "Acme", "expansion", "network_security_path",
        {"status": "in_progress", "note": "PrivateLink review"},
    )
    restarted = TechEvalService(tmp_path / "customers", safe_name=_safe, clock=Clock())
    loaded = restarted.get_tracker("Acme", "expansion")
    item = next(item for item in loaded["items"] if item["id"] == "network_security_path")
    assert item["status"] == "in_progress"
    assert item["note"] == "PrivateLink review"

    def fail_replace(_source, _target):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr("services.tech_eval_service.os.replace", fail_replace)
    with pytest.raises(OSError):
        restarted.update_item("Acme", "expansion", "network_security_path", {"status": "done"})
    reloaded = TechEvalService(tmp_path / "customers", safe_name=_safe).get_tracker("Acme", "expansion")
    item = next(item for item in reloaded["items"] if item["id"] == "network_security_path")
    assert item["status"] == "in_progress"
    tracker_dir = tmp_path / "customers" / "Acme" / "opportunities" / "expansion" / ".tech-eval-readiness"
    assert not list(tracker_dir.glob("*.tmp"))


@pytest.mark.parametrize("status", ["ready", "complete-ish", "", None])
def test_invalid_status_rejected(tmp_path, status) -> None:
    service = _service(tmp_path)
    with pytest.raises(TechEvalError) as exc:
        service.update_item("Acme", "expansion", "technical_champion", {"status": status})
    assert exc.value.code == "invalid_status"


def test_invalid_item_and_request_rejected(tmp_path) -> None:
    service = _service(tmp_path)
    with pytest.raises(TechEvalError) as unknown:
        service.update_item("Acme", "expansion", "made_up", {"status": "done"})
    assert unknown.value.code == "unknown_item"
    with pytest.raises(TechEvalError) as empty:
        service.update_item("Acme", "expansion", "technical_champion", {})
    assert empty.value.code == "invalid_request"
    with pytest.raises((TechEvalError, ValueError)):
        service.get_tracker("Acme", "../escape")
