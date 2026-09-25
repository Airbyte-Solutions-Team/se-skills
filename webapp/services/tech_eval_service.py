"""Local persistence and deterministic summaries for Tech Eval readiness.

The tracker is intentionally separate from immutable canonical opportunity
state. It contains only a fixed checklist and attributable manual edits; no
model output or generated artifact is accepted by this service.
"""
from __future__ import annotations

import json
import os
import stat
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from services.path_utils import resolve_within


PHASES = ("plan", "prepare", "execute", "validate", "close")
STATUSES = ("not_started", "in_progress", "blocked", "done", "not_applicable")
OVERALL_LABELS = {
    "not_started": "Not started",
    "in_progress": "In progress",
    "ready_with_risks": "Ready with risks",
    "blocked": "Blocked",
    "complete": "Complete",
}
_MAX_TRACKER_BYTES = 250_000
_EDITABLE_FIELDS = frozenset(("status", "owner", "note"))


# Stable ids are part of the persisted contract. Keep the template compact and
# deterministic so every local opportunity starts from the same operating plan.
TECH_EVAL_TEMPLATE: tuple[dict[str, str], ...] = (
    {"id": "evaluation_objective_use_cases", "phase": "plan", "type": "required", "label": "Evaluation objective and prioritized use cases"},
    {"id": "technical_champion", "phase": "plan", "type": "recommended", "label": "Technical champion"},
    {"id": "agreed_scope_dates", "phase": "plan", "type": "gate", "label": "Agreed evaluation scope and dates"},
    {"id": "customer_success_criteria", "phase": "plan", "type": "gate", "label": "Customer-approved success criteria"},
    {"id": "checkin_cadence", "phase": "plan", "type": "recommended", "label": "Evaluation check-in cadence"},
    {"id": "source_destination_access", "phase": "prepare", "type": "gate", "label": "Source and destination access"},
    {"id": "network_security_path", "phase": "prepare", "type": "gate", "label": "Network and security path"},
    {"id": "environment_provisioning", "phase": "prepare", "type": "required", "label": "Environment provisioning"},
    {"id": "responsibilities_ownership", "phase": "prepare", "type": "required", "label": "Responsibilities and ownership"},
    {"id": "core_replication_validation", "phase": "execute", "type": "required", "label": "Core replication validation"},
    {"id": "incremental_cdc_validation", "phase": "execute", "type": "required", "label": "Incremental or CDC validation when applicable"},
    {"id": "connector_gap_validation", "phase": "execute", "type": "optional", "label": "Connector-gap validation"},
    {"id": "scale_performance_validation", "phase": "validate", "type": "required", "label": "Scale and performance validation"},
    {"id": "failure_recovery_testing", "phase": "validate", "type": "recommended", "label": "Failure and recovery testing"},
    {"id": "monitoring_operational_readiness", "phase": "validate", "type": "required", "label": "Monitoring and operational readiness"},
    {"id": "customer_acceptance", "phase": "close", "type": "required", "label": "Customer acceptance"},
    {"id": "technical_win_confirmation", "phase": "close", "type": "required", "label": "Technical Win confirmation"},
    {"id": "production_architecture", "phase": "close", "type": "required", "label": "Production architecture"},
    {"id": "final_readout_handoff", "phase": "close", "type": "required", "label": "Final readout and handoff"},
)


class TechEvalError(Exception):
    """Domain error with an HTTP-compatible status and stable code."""

    def __init__(self, status_code: int, detail: str, *, code: str = "tech_eval_error") -> None:
        self.status_code = status_code
        self.detail = detail
        self.code = code
        super().__init__(detail)


class TechEvalService:
    """Persist one manually maintained checklist per exact local opportunity."""

    SCHEMA_VERSION = 1
    TRACKER_DIR_NAME = ".tech-eval-readiness"
    TRACKER_FILE_NAME = "tracker.json"

    def __init__(
        self,
        customers_dir: Path,
        *,
        safe_name: Callable[[str], str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.customers_dir = Path(customers_dir)
        self._safe_name = safe_name
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def _now(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _paths(self, account: str, opp_slug: str, *, create: bool) -> tuple[str, str, Path, Path]:
        safe_account = self._safe_name(account)
        safe_opp = self._safe_name(opp_slug)
        try:
            account_dir = resolve_within(self.customers_dir, safe_account)
            opportunity_dir = resolve_within(account_dir, Path("opportunities") / safe_opp)
            tracker_dir = resolve_within(opportunity_dir, self.TRACKER_DIR_NAME)
            tracker_path = resolve_within(tracker_dir, self.TRACKER_FILE_NAME)
        except ValueError as exc:
            raise TechEvalError(400, "Invalid tracker path.", code="invalid_path") from exc
        if not account_dir.is_dir():
            raise TechEvalError(404, "Unknown account.", code="unknown_account")
        for path in (opportunity_dir, tracker_dir):
            if path.exists() and path.is_symlink():
                raise TechEvalError(409, "Tech Eval tracker storage is unsafe.", code="unsafe_storage")
        if create:
            tracker_dir.mkdir(parents=True, exist_ok=True)
        return safe_account, safe_opp, tracker_dir, tracker_path

    @staticmethod
    def _atomic_write(path: Path, value: Mapping[str, Any]) -> None:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
            if info.st_size <= 0 or info.st_size > _MAX_TRACKER_BYTES:
                raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
            value = json.loads(path.read_text(encoding="utf-8"))
        except TechEvalError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage") from exc
        if not isinstance(value, dict):
            raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
        return value

    def _new_tracker(self, account: str, opp_slug: str) -> dict[str, Any]:
        now = self._now()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "identity": {"account": account, "opportunity_slug": opp_slug},
            "created_at": now,
            "updated_at": now,
            "last_change_source": None,
            "manual_change_count": 0,
            "items": [
                {
                    **definition,
                    "status": "not_started",
                    "owner": None,
                    "note": None,
                    "last_updated": None,
                    "update_source": None,
                }
                for definition in TECH_EVAL_TEMPLATE
            ],
        }

    @staticmethod
    def _validate_loaded(tracker: dict[str, Any], account: str, opp_slug: str) -> None:
        identity = tracker.get("identity")
        items = tracker.get("items")
        if (
            tracker.get("schema_version") != 1
            or identity != {"account": account, "opportunity_slug": opp_slug}
            or not isinstance(items, list)
            or len(items) != len(TECH_EVAL_TEMPLATE)
            or not isinstance(tracker.get("manual_change_count"), int)
            or tracker.get("last_change_source") not in (None, "manual")
        ):
            raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
        for item, definition in zip(items, TECH_EVAL_TEMPLATE, strict=True):
            if not isinstance(item, dict) or any(item.get(key) != definition[key] for key in ("id", "phase", "type", "label")):
                raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
            if item.get("status") not in STATUSES:
                raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
            if item.get("owner") is not None and not isinstance(item.get("owner"), str):
                raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
            if item.get("note") is not None and not isinstance(item.get("note"), str):
                raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
            if item.get("last_updated") is not None and not isinstance(item.get("last_updated"), str):
                raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")
            if item.get("update_source") not in (None, "manual"):
                raise TechEvalError(409, "Tech Eval tracker storage is malformed.", code="malformed_storage")

    @staticmethod
    def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
        applicable = [item for item in items if item["status"] != "not_applicable"]
        completed = [item for item in applicable if item["status"] == "done"]
        gates = [item for item in applicable if item["type"] == "gate" and item["status"] != "done"]
        explicitly_blocked = [item for item in applicable if item["status"] == "blocked"]
        core = [item for item in applicable if item["type"] in ("gate", "required")]
        started = any(item["status"] != "not_started" for item in items)

        if not started:
            overall = "not_started"
        elif explicitly_blocked:
            overall = "blocked"
        elif len(completed) == len(applicable):
            overall = "complete"
        elif all(item["status"] == "done" for item in core):
            overall = "ready_with_risks"
        else:
            overall = "in_progress"

        current_phase = "close"
        for phase in PHASES:
            unfinished_core = [
                item for item in core if item["phase"] == phase and item["status"] != "done"
            ]
            if unfinished_core:
                current_phase = phase
                break

        if not applicable or len(completed) == len(applicable):
            remaining = "All applicable readiness items are complete."
        elif gates:
            first = next((item for item in gates if item["status"] == "blocked"), gates[0])
            prefix = "Blocked gate" if first["status"] == "blocked" else "Next gate"
            if len(gates) > 1:
                other_count = len(gates) - 1
                remaining = (
                    f"{prefix}: {first['label']}; {other_count} other "
                    f"gate{'s' if other_count != 1 else ''} remain."
                )
            else:
                remaining = f"{prefix}: {first['label']} remains."
        else:
            required = [item for item in core if item["status"] != "done"]
            if required:
                remaining = f"Next required item: {required[0]['label']}."
            else:
                risk_items = len(applicable) - len(completed)
                remaining = f"Core readiness is complete; {risk_items} recommended or optional item{'s' if risk_items != 1 else ''} remain."

        phase_summaries = []
        for phase in PHASES:
            phase_items = [item for item in items if item["phase"] == phase and item["status"] != "not_applicable"]
            phase_summaries.append({
                "phase": phase,
                "completed": sum(item["status"] == "done" for item in phase_items),
                "total_applicable": len(phase_items),
                "blocking_gates": sum(item["type"] == "gate" and item["status"] != "done" for item in phase_items),
            })

        return {
            "current_phase": current_phase,
            "overall_state": overall,
            "overall_label": OVERALL_LABELS[overall],
            "completed": len(completed),
            "total_applicable": len(applicable),
            "blocking_gates": len(gates),
            "remaining": remaining,
            "phases": phase_summaries,
        }

    def _with_summary(self, tracker: dict[str, Any]) -> dict[str, Any]:
        return {**tracker, "summary": self._summary(tracker["items"])}

    def peek_summary(self, account: str, opp_slug: str) -> dict[str, Any] | None:
        """Summary of an existing tracker, or None when none was ever created; never writes."""
        with self._lock:
            try:
                safe_account, safe_opp, _tracker_dir, path = self._paths(account, opp_slug, create=False)
            except TechEvalError:
                return None
            if not path.exists():
                return None
            try:
                tracker = self._read_json(path)
                self._validate_loaded(tracker, safe_account, safe_opp)
            except TechEvalError:
                return None
            return self._summary(tracker["items"])

    def get_tracker(self, account: str, opp_slug: str) -> dict[str, Any]:
        """Load the scoped tracker, creating the deterministic template once."""
        with self._lock:
            safe_account, safe_opp, _tracker_dir, path = self._paths(account, opp_slug, create=True)
            if path.exists():
                tracker = self._read_json(path)
                self._validate_loaded(tracker, safe_account, safe_opp)
            else:
                tracker = self._new_tracker(safe_account, safe_opp)
                self._atomic_write(path, tracker)
            return self._with_summary(tracker)

    def update_item(
        self,
        account: str,
        opp_slug: str,
        item_id: str,
        changes: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Apply an allowlisted manual edit and atomically persist the tracker."""
        if not changes or not set(changes).issubset(_EDITABLE_FIELDS):
            raise TechEvalError(400, "A status, owner, or note change is required.", code="invalid_request")
        if "status" in changes and changes["status"] not in STATUSES:
            raise TechEvalError(400, "Invalid Tech Eval item status.", code="invalid_status")
        for key, limit in (("owner", 120), ("note", 500)):
            if key in changes and changes[key] is not None:
                if not isinstance(changes[key], str) or len(changes[key]) > limit:
                    raise TechEvalError(400, f"Invalid {key}.", code="invalid_request")

        with self._lock:
            safe_account, safe_opp, _tracker_dir, path = self._paths(account, opp_slug, create=True)
            if path.exists():
                tracker = self._read_json(path)
                self._validate_loaded(tracker, safe_account, safe_opp)
            else:
                tracker = self._new_tracker(safe_account, safe_opp)
            item = next((candidate for candidate in tracker["items"] if candidate["id"] == item_id), None)
            if item is None:
                raise TechEvalError(404, "Unknown Tech Eval checklist item.", code="unknown_item")

            for key, value in changes.items():
                if key in ("owner", "note") and isinstance(value, str):
                    value = value.strip() or None
                item[key] = value
            now = self._now()
            item["last_updated"] = now
            item["update_source"] = "manual"
            tracker["updated_at"] = now
            tracker["last_change_source"] = "manual"
            tracker["manual_change_count"] += 1
            self._atomic_write(path, tracker)
            return self._with_summary(tracker)
