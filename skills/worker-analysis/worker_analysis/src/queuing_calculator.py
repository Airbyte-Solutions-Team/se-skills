"""
Queuing-aware worker estimation for time-window-based sizing.

Instead of sizing for peak concurrency (all syncs at once), this module
calculates the minimum workers needed to drain a batch of syncs within a
completion window. This is appropriate for:

- Scheduled batches with a completion deadline (e.g., "all syncs done by 12:30 AM")
- API-orchestrated pipelines that trigger syncs in sequence
- Any workload where jobs queue and drain rather than burst simultaneously

The mental model: workers are checkout lanes, and the completion window is
closing time. You don't need a lane for every shopper — just enough lanes
to clear the line before the doors close.
"""

from dataclasses import dataclass, field
from math import ceil, floor
from typing import Dict, List, Optional, Any


@dataclass
class QueuingEstimateInput:
    """Input parameters for a queuing-aware worker estimation.

    Attributes:
        total_syncs: Total number of syncs to complete in the window.
        critical_syncs: Number of syncs that MUST land in the window.
            If None, defaults to total_syncs (all are critical).
        avg_sync_duration_minutes: Average sync duration in minutes.
        p90_sync_duration_minutes: 90th percentile sync duration.
            Used for worst-case analysis. If None, defaults to avg * 1.5.
        completion_window_minutes: The window in which all critical syncs
            must complete (e.g., 30 for a 30-minute window).
        dw_per_sync: Data workers consumed by each running sync.
            Default 0.5 for database connectors (1 src + 1 dst + 1 orch = 3 CPU / 8 = 0.375,
            but the platform rounds to ~0.5 per the published docs).
        connector_type: "database" or "api" — determines slots per worker.
        queue_poll_interval_seconds: Seconds between queue admission polls.
            Default 60 (platform checks every ~60s for next job to admit).
    """
    total_syncs: int
    critical_syncs: Optional[int] = None
    avg_sync_duration_minutes: float = 5.0
    p90_sync_duration_minutes: Optional[float] = None
    completion_window_minutes: float = 30.0
    dw_per_sync: float = 0.5
    connector_type: str = "database"
    queue_poll_interval_seconds: float = 60.0

    def __post_init__(self):
        if self.critical_syncs is None:
            self.critical_syncs = self.total_syncs
        if self.p90_sync_duration_minutes is None:
            self.p90_sync_duration_minutes = self.avg_sync_duration_minutes * 1.5


@dataclass
class QueuingEstimateResult:
    """Result of a queuing-aware worker estimation.

    Attributes:
        recommended_minimum_workers: The floor — fewest workers that clear
            the batch in-window at average sync duration.
        recommended_with_headroom: Workers with margin for retries,
            slow nights, and growth.
        concurrent_slots_minimum: How many syncs run at once with minimum workers.
        concurrent_slots_headroom: How many syncs run at once with headroom workers.
        waves_minimum: Number of sequential waves needed at minimum workers.
        waves_headroom: Number of sequential waves needed with headroom.
        estimated_drain_time_minimum: Minutes to drain the batch at minimum.
        estimated_drain_time_headroom: Minutes to drain the batch with headroom.
        completion_window_minutes: The target window.
        margin_minutes_minimum: Time left over in the window at minimum.
        margin_minutes_headroom: Time left over in the window with headroom.
        scenarios: List of all calculated scenarios for reporting.
    """
    recommended_minimum_workers: int
    recommended_with_headroom: int
    concurrent_slots_minimum: int
    concurrent_slots_headroom: int
    waves_minimum: int
    waves_headroom: int
    estimated_drain_time_minimum: float
    estimated_drain_time_headroom: float
    completion_window_minutes: float
    margin_minutes_minimum: float
    margin_minutes_headroom: float
    scenarios: List[Dict[str, Any]] = field(default_factory=list)
    input_params: Optional[Dict[str, Any]] = None


def calculate_drain_time(
    total_syncs: int,
    concurrent_slots: int,
    avg_sync_duration_minutes: float,
    queue_poll_interval_minutes: float = 1.0,
) -> float:
    """Calculate how long it takes to drain a batch with a given concurrency.

    Uses a continuous-flow model: the platform admits the next sync as soon
    as a slot opens (not strict batch waves). This matches observed behavior
    where syncs have varying durations and the queue drains smoothly.

    The formula is: (total_syncs / concurrent_slots) * avg_duration, with
    a small overhead factor for queue admission latency between waves.

    Args:
        total_syncs: Total syncs to drain.
        concurrent_slots: How many syncs run at once.
        avg_sync_duration_minutes: Average time per sync.
        queue_poll_interval_minutes: Overhead per wave transition (platform poll).

    Returns:
        Total drain time in minutes.
    """
    if concurrent_slots <= 0:
        return float('inf')
    if total_syncs <= 0:
        return 0.0

    waves = ceil(total_syncs / concurrent_slots)
    # Continuous flow: total work / parallelism, plus minor overhead per
    # wave boundary for queue admission latency.
    continuous_drain = (total_syncs * avg_sync_duration_minutes) / concurrent_slots
    # Add a fraction of poll overhead — not full gap per wave since syncs
    # stagger their completions naturally.
    overhead = (waves - 1) * queue_poll_interval_minutes * 0.5
    return continuous_drain + overhead


def workers_to_concurrent_slots(
    workers: int,
    connector_type: str = "database",
) -> int:
    """Convert worker count to number of concurrent sync slots.

    Per the universal worker model:
    - Database connectors: 2 concurrent syncs per worker
    - API connectors: 5 concurrent syncs per worker

    Args:
        workers: Number of data workers.
        connector_type: "database" or "api".

    Returns:
        Number of concurrent sync slots.
    """
    if connector_type == "api":
        return workers * 5
    return workers * 2


def concurrent_slots_to_workers(
    slots: int,
    connector_type: str = "database",
) -> int:
    """Convert concurrent slots needed to worker count (rounded up).

    Args:
        slots: Number of concurrent sync slots needed.
        connector_type: "database" or "api".

    Returns:
        Number of data workers required.
    """
    if connector_type == "api":
        return ceil(slots / 5)
    return ceil(slots / 2)


def estimate_workers_for_window(
    params: QueuingEstimateInput,
) -> QueuingEstimateResult:
    """Calculate minimum workers needed to drain a batch within a time window.

    This is the core queuing estimation function. It finds the smallest worker
    count where the batch clears before the deadline, then adds headroom.

    Args:
        params: QueuingEstimateInput with all parameters.

    Returns:
        QueuingEstimateResult with recommendation and scenario analysis.
    """
    queue_poll_minutes = params.queue_poll_interval_seconds / 60.0
    critical = params.critical_syncs

    # Build scenario table: for each worker count 1..max, calculate drain time
    scenarios = []
    min_workers = None
    headroom_workers = None

    # Upper bound: enough workers to run everything at once (no queuing needed)
    max_workers_to_check = concurrent_slots_to_workers(
        critical, params.connector_type
    ) + 2  # +2 for safety

    for w in range(1, max_workers_to_check + 1):
        slots = workers_to_concurrent_slots(w, params.connector_type)
        waves = ceil(critical / slots)

        # Average case
        drain_avg = calculate_drain_time(
            critical, slots, params.avg_sync_duration_minutes, queue_poll_minutes
        )
        # Worst case (P90 duration)
        drain_p90 = calculate_drain_time(
            critical, slots, params.p90_sync_duration_minutes, queue_poll_minutes
        )

        fits_avg = drain_avg <= params.completion_window_minutes
        fits_p90 = drain_p90 <= params.completion_window_minutes
        margin_avg = params.completion_window_minutes - drain_avg
        margin_p90 = params.completion_window_minutes - drain_p90

        scenario = {
            "workers": w,
            "concurrent_slots": slots,
            "waves": waves,
            "drain_time_avg_minutes": round(drain_avg, 1),
            "drain_time_p90_minutes": round(drain_p90, 1),
            "fits_in_window_avg": fits_avg,
            "fits_in_window_p90": fits_p90,
            "margin_avg_minutes": round(margin_avg, 1),
            "margin_p90_minutes": round(margin_p90, 1),
        }
        scenarios.append(scenario)

        # Find minimum: first worker count that fits at average duration
        if min_workers is None and fits_avg:
            min_workers = w

        # Find headroom: first worker count that fits at P90 duration
        if headroom_workers is None and fits_p90:
            headroom_workers = w

    # Edge case: if nothing fits, recommend max checked
    if min_workers is None:
        min_workers = max_workers_to_check
    if headroom_workers is None:
        headroom_workers = max_workers_to_check

    # If headroom equals minimum, add one more for real margin
    if headroom_workers <= min_workers:
        headroom_workers = min_workers + 1

    # Get details for recommended values
    min_slots = workers_to_concurrent_slots(min_workers, params.connector_type)
    min_waves = ceil(critical / min_slots)
    min_drain = calculate_drain_time(
        critical, min_slots, params.avg_sync_duration_minutes, queue_poll_minutes
    )

    head_slots = workers_to_concurrent_slots(headroom_workers, params.connector_type)
    head_waves = ceil(critical / head_slots)
    head_drain = calculate_drain_time(
        critical, head_slots, params.avg_sync_duration_minutes, queue_poll_minutes
    )

    return QueuingEstimateResult(
        recommended_minimum_workers=min_workers,
        recommended_with_headroom=headroom_workers,
        concurrent_slots_minimum=min_slots,
        concurrent_slots_headroom=head_slots,
        waves_minimum=min_waves,
        waves_headroom=head_waves,
        estimated_drain_time_minimum=round(min_drain, 1),
        estimated_drain_time_headroom=round(head_drain, 1),
        completion_window_minutes=params.completion_window_minutes,
        margin_minutes_minimum=round(params.completion_window_minutes - min_drain, 1),
        margin_minutes_headroom=round(params.completion_window_minutes - head_drain, 1),
        scenarios=scenarios,
        input_params={
            "total_syncs": params.total_syncs,
            "critical_syncs": params.critical_syncs,
            "avg_sync_duration_minutes": params.avg_sync_duration_minutes,
            "p90_sync_duration_minutes": params.p90_sync_duration_minutes,
            "completion_window_minutes": params.completion_window_minutes,
            "connector_type": params.connector_type,
            "queue_poll_interval_seconds": params.queue_poll_interval_seconds,
        },
    )
