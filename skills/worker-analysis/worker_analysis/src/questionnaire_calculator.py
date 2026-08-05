"""
Deterministic questionnaire sizing for the worker-analysis skill.

This module replaces runtime/model-dependent burst arithmetic with a reproducible
set of sizing views. It is always called when a questionnaire has enough inputs
to size a workload. The resulting numbers are passed into the model as structured
evidence; the model explains them but does not decide whether to compute them.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Dict, Optional

try:
    from src import config
except ImportError:
    import config

from worker_calculator import WorkerCalculator


def _connection_matrix(
    total_connections: int,
    api_percent: float,
    db_percent: float,
    sub_hourly_percent: float,
    hourly_percent: float,
    daily_percent: float,
) -> Dict[str, Any]:
    """Return a reconciled matrix of API/DB connections by frequency bucket."""
    freq_totals = config.split_proportionally(
        total_connections,
        {
            "sub_hourly": sub_hourly_percent,
            "hourly": hourly_percent,
            "daily": daily_percent,
        },
    )

    matrix: Dict[str, Any] = {}
    for bucket, bucket_total in freq_totals.items():
        split = config.split_proportionally(
            bucket_total, {"api": api_percent, "db": db_percent}
        )
        matrix[bucket] = {
            "total": bucket_total,
            "api": split["api"],
            "db": split["db"],
        }

    api_total = sum(matrix[b]["api"] for b in matrix)
    db_total = sum(matrix[b]["db"] for b in matrix)

    return {
        "api_connections": api_total,
        "db_connections": db_total,
        "sub_hourly": matrix["sub_hourly"],
        "hourly": matrix["hourly"],
        "daily": matrix["daily"],
    }


def _workers_from_concurrency(api_concurrent: float, db_concurrent: float) -> int:
    """Apply the universal worker model."""
    return ceil(api_concurrent / config.API_CONNECTIONS_PER_WORKER) + ceil(
        db_concurrent / config.DB_CONNECTIONS_PER_WORKER
    )


def _steady_state_workers(
    matrix: Dict[str, Any],
    sync_duration_minutes: float,
) -> Dict[str, Any]:
    """Expected concurrency assuming schedules are spread over each interval."""
    api = (
        matrix["sub_hourly"]["api"] * sync_duration_minutes / config.SYNC_INTERVALS["sub_hourly"]
        + matrix["hourly"]["api"] * sync_duration_minutes / config.SYNC_INTERVALS["hourly"]
        + matrix["daily"]["api"] * sync_duration_minutes / config.SYNC_INTERVALS["daily"]
    )
    db = (
        matrix["sub_hourly"]["db"] * sync_duration_minutes / config.SYNC_INTERVALS["sub_hourly"]
        + matrix["hourly"]["db"] * sync_duration_minutes / config.SYNC_INTERVALS["hourly"]
        + matrix["daily"]["db"] * sync_duration_minutes / config.SYNC_INTERVALS["daily"]
    )

    return {
        "api_concurrent": round(api, 2),
        "db_concurrent": round(db, 2),
        "workers": _workers_from_concurrency(api, db),
    }


def _worst_case_burst_workers(
    matrix: Dict[str, Any],
    sync_duration_minutes: float,
) -> Dict[str, Any]:
    """Worst-case where every daily sync fires at the same moment.

    Sub-hourly and hourly steady-state concurrency continues to run while all
    daily (or less frequent) connections in the matrix start simultaneously.
    """
    api = (
        matrix["sub_hourly"]["api"] * sync_duration_minutes / config.SYNC_INTERVALS["sub_hourly"]
        + matrix["hourly"]["api"] * sync_duration_minutes / config.SYNC_INTERVALS["hourly"]
        + matrix["daily"]["api"]
    )
    db = (
        matrix["sub_hourly"]["db"] * sync_duration_minutes / config.SYNC_INTERVALS["sub_hourly"]
        + matrix["hourly"]["db"] * sync_duration_minutes / config.SYNC_INTERVALS["hourly"]
        + matrix["daily"]["db"]
    )

    return {
        "api_concurrent": round(api, 2),
        "db_concurrent": round(db, 2),
        "workers": _workers_from_concurrency(api, db),
    }


def _peak_window_drain_workers(
    matrix: Dict[str, Any],
    sync_duration_minutes: float,
    freshness_minutes: float,
) -> Dict[str, Any]:
    """Minimum workers needed to drain the daily batch within the freshness window.

    API and DB batches are sized independently because a worker slot is type-
    specific in the universal model, then summed.
    """
    def _drain_for_type(count: int, slots_per_worker: int) -> int:
        if count <= 0 or freshness_minutes <= 0:
            return 0
        # continuous-flow drain: total sync-minutes / available slots
        required_slots = (count * sync_duration_minutes) / freshness_minutes
        slots = ceil(required_slots)
        if slots <= 0:
            return 0
        return ceil(slots / slots_per_worker)

    api_drain = _drain_for_type(
        matrix["daily"]["api"], config.API_CONNECTIONS_PER_WORKER
    )
    db_drain = _drain_for_type(
        matrix["daily"]["db"], config.DB_CONNECTIONS_PER_WORKER
    )

    return {
        "api_workers": api_drain,
        "db_workers": db_drain,
        "workers": api_drain + db_drain,
        "window_minutes": freshness_minutes,
    }


def _staging_steady_workers(
    total_connections: int,
    api_percent: float,
    db_percent: float,
    sync_duration_minutes: float,
) -> int:
    """Model a staging environment as daily-only with the same connector mix."""
    split = config.split_proportionally(
        total_connections, {"api": api_percent, "db": db_percent}
    )
    api = split["api"] * sync_duration_minutes / config.SYNC_INTERVALS["daily"]
    db = split["db"] * sync_duration_minutes / config.SYNC_INTERVALS["daily"]
    return _workers_from_concurrency(api, db)


def _future_growth_workers(
    growth_connections: Optional[int],
    api_percent: float,
    db_percent: float,
    sub_hourly_percent: float,
    hourly_percent: float,
    daily_percent: float,
    sync_duration_minutes: float,
    environments: int,
) -> Optional[Dict[str, Any]]:
    """Combined prod + staging at the growth target, if one was supplied."""
    if growth_connections is None or growth_connections <= 0:
        return None

    matrix = _connection_matrix(
        growth_connections,
        api_percent,
        db_percent,
        sub_hourly_percent,
        hourly_percent,
        daily_percent,
    )
    prod = _steady_state_workers(matrix, sync_duration_minutes)
    extra_envs = max(0, environments - 1)
    staging = _staging_steady_workers(
        growth_connections, api_percent, db_percent, sync_duration_minutes
    )

    return {
        "connections": growth_connections,
        "prod_workers": prod["workers"],
        "combined_workers": prod["workers"] + (extra_envs * staging),
    }


def _headroom_workers(
    combined_steady: int,
    worst_case_burst: int,
    has_daily: bool,
    environments: int,
) -> int:
    """Headroom above the combined steady-state floor.

    A non-zero burst delta means schedules are not staggered. We add enough
    workers to absorb a portion of that delta (capped at 2) and at least one
    worker when daily syncs or multiple environments create operational risk.
    """
    burst_delta = max(0, worst_case_burst - combined_steady)
    burst_headroom = min(2, burst_delta)
    base_headroom = 1 if (has_daily or environments > 1) else 0
    return max(base_headroom, burst_headroom)


def analyze_questionnaire(
    total_connections: int,
    api_percent: float,
    db_percent: float,
    sub_hourly_percent: float,
    hourly_percent: float,
    daily_percent: float,
    sync_duration_minutes: Optional[float] = None,
    maintenance_window_hours: Optional[float] = None,
    freshness_minutes: float = 60.0,
    environments: int = 2,
    growth_connections: Optional[int] = None,
) -> Dict[str, Any]:
    """Return the deterministic sizing views for a completed questionnaire.

    The seven required views are:
      1. Steady-state requirement
      2. Peak-window drain requirement
      3. Worst-case simultaneous or clustered burst requirement
      4. Production-only requirement
      5. Combined production and staging requirement
      6. Future-growth requirement
      7. Recommended contract or deployment capacity

    All numbers are computed from the inputs; none are invented by the model.
    """
    if sync_duration_minutes is None or sync_duration_minutes <= 0:
        sync_duration_minutes = config.DEFAULT_SYNC_DURATION_MINUTES

    # If the caller supplied a freshness SLA, use it; otherwise fall back to a
    # 1-hour reporting window, which is the most common cadence-preservation
    # target. The legacy maintenance_window_hours argument is preserved for
    # backwards compatibility but is not used as the binding drain window.
    effective_freshness = freshness_minutes or 60.0

    matrix = _connection_matrix(
        total_connections,
        api_percent,
        db_percent,
        sub_hourly_percent,
        hourly_percent,
        daily_percent,
    )

    steady = _steady_state_workers(matrix, sync_duration_minutes)
    burst = _worst_case_burst_workers(matrix, sync_duration_minutes)
    drain = _peak_window_drain_workers(
        matrix, sync_duration_minutes, effective_freshness
    )

    extra_envs = max(0, environments - 1)
    staging_steady = _staging_steady_workers(
        total_connections, api_percent, db_percent, sync_duration_minutes
    )

    production_only = steady["workers"]
    combined = production_only + (extra_envs * staging_steady)

    future = _future_growth_workers(
        growth_connections,
        api_percent,
        db_percent,
        sub_hourly_percent,
        hourly_percent,
        daily_percent,
        sync_duration_minutes,
        environments,
    )
    future_combined = future["combined_workers"] if future else combined

    has_daily = matrix["daily"]["total"] > 0
    headroom = _headroom_workers(combined, burst["workers"], has_daily, environments)
    recommended = max(combined + headroom, future_combined)

    return {
        "connection_matrix": matrix,
        "steady_state_workers": production_only,
        "steady_state_concurrency": {
            "api_concurrent": steady["api_concurrent"],
            "db_concurrent": steady["db_concurrent"],
        },
        "peak_window_drain_workers": drain["workers"],
        "peak_window_drain_breakdown": drain,
        "worst_case_burst_workers": burst["workers"],
        "worst_case_burst_concurrency": {
            "api_concurrent": burst["api_concurrent"],
            "db_concurrent": burst["db_concurrent"],
        },
        "production_only_workers": production_only,
        "combined_prod_staging_workers": combined,
        "staging_steady_workers": staging_steady,
        "environments": environments,
        "future_growth": future,
        "future_growth_workers": future_combined,
        "recommended_contract_or_deployment_workers": recommended,
        "recommended_basis": {
            "combined_steady": combined,
            "headroom": headroom,
            "future_combined": future_combined,
        },
        "calculation_basis": {
            "api_connections_per_worker": config.API_CONNECTIONS_PER_WORKER,
            "db_connections_per_worker": config.DB_CONNECTIONS_PER_WORKER,
            "sync_duration_minutes": sync_duration_minutes,
            "freshness_minutes": effective_freshness,
            "sub_hourly_interval_minutes": config.SYNC_INTERVALS["sub_hourly"],
            "hourly_interval_minutes": config.SYNC_INTERVALS["hourly"],
            "daily_interval_minutes": config.SYNC_INTERVALS["daily"],
        },
    }
