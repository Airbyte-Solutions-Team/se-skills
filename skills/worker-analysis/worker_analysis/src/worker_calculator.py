"""
Worker calculation logic for Airbyte Data Workers.

Supports two modes:
1. Measured: Calculate from actual concurrency data
2. Estimate: Calculate from projected usage parameters
"""

from typing import Dict, List, Optional, Any
from math import ceil
import numpy as np
try:
    from src import config
except ImportError:
    import config


class WorkerCalculator:
    """Calculate Airbyte Data Worker requirements.

    All plans use the same worker model with COMBINABLE capacity:
    - 5 API connections per worker
    - 2 DB/File connections per worker
    - Formula: (API ÷ 5) + (DB ÷ 2) = workers used
    """

    def __init__(self):
        """Initialize calculator with universal worker model."""
        self.model = config.get_worker_model()
        self.db_connections_per_worker = config.DB_CONNECTIONS_PER_WORKER
        self.api_connections_per_worker = config.API_CONNECTIONS_PER_WORKER

    # -------------------------------------------------------------------------
    # Measured Mode - Calculate from Actual Data
    # -------------------------------------------------------------------------

    def calculate_from_measured_data(
        self,
        api_concurrent_samples: List[float],
        db_concurrent_samples: List[float]
    ) -> Dict[str, Any]:
        """
        Calculate workers from measured concurrency samples.

        Uses 99th percentile of observed concurrency as the planning number.

        Args:
            api_concurrent_samples: List of concurrent API connection counts
            db_concurrent_samples: List of concurrent DB connection counts

        Returns:
            Dictionary with calculation details and result
        """
        # Calculate 99th percentile (peak planning number)
        p99_api = self._calculate_percentile(api_concurrent_samples, config.PEAK_PERCENTILE)
        p99_db = self._calculate_percentile(db_concurrent_samples, config.PEAK_PERCENTILE)

        # Also calculate max for reference
        max_api = max(api_concurrent_samples) if api_concurrent_samples else 0
        max_db = max(db_concurrent_samples) if db_concurrent_samples else 0

        # Calculate capacity units
        api_capacity = p99_api / self.api_connections_per_worker
        db_capacity = p99_db / self.db_connections_per_worker
        total_capacity = api_capacity + db_capacity

        # Round up to get workers needed
        workers_required = ceil(api_capacity) + ceil(db_capacity)

        return {
            "mode": "measured",
            "worker_type": self.model["worker_type"],
            "concurrency_analysis": {
                "p99_concurrent_api": round(p99_api, 2),
                "p99_concurrent_db": round(p99_db, 2),
                "max_concurrent_api": max_api,
                "max_concurrent_db": max_db,
                "samples_analyzed": {
                    "api": len(api_concurrent_samples),
                    "db": len(db_concurrent_samples)
                }
            },
            "capacity_calculation": {
                "api_capacity_units": round(api_capacity, 2),
                "db_capacity_units": round(db_capacity, 2),
                "total_capacity_units": round(total_capacity, 2)
            },
            "workers_required": workers_required,
            "calculation_details": {
                "api_connections_per_worker": self.api_connections_per_worker,
                "db_connections_per_worker": self.db_connections_per_worker,
                "percentile_used": config.PEAK_PERCENTILE
            }
        }

    def _calculate_percentile(self, samples: List[float], percentile: int) -> float:
        """
        Calculate percentile from samples.

        Args:
            samples: List of values
            percentile: Percentile to calculate (0-100)

        Returns:
            Percentile value
        """
        if not samples:
            return 0.0

        return float(np.percentile(samples, percentile))

    # -------------------------------------------------------------------------
    # Estimate Mode - Calculate from Projected Parameters
    # -------------------------------------------------------------------------

    def calculate_from_estimate(
        self,
        total_connections: int,
        api_percent: float,
        db_percent: float,
        sub_hourly_percent: float,
        hourly_percent: float,
        daily_percent: float,
        sync_duration_minutes: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Calculate workers from estimated usage parameters.

        Uses statistical modeling based on sync frequencies and durations.

        Args:
            total_connections: Total number of connections
            api_percent: Percentage of API connections (0-100)
            db_percent: Percentage of DB connections (0-100)
            sub_hourly_percent: Percentage running sub-hourly (0-100)
            hourly_percent: Percentage running hourly (0-100)
            daily_percent: Percentage running daily (0-100)
            sync_duration_minutes: Average sync duration (defaults to config value)

        Returns:
            Dictionary with calculation details and result
        """
        # Validate inputs
        self._validate_estimate_inputs(
            total_connections, api_percent, db_percent,
            sub_hourly_percent, hourly_percent, daily_percent
        )

        # Use default sync duration if not provided
        if sync_duration_minutes is None:
            sync_duration_minutes = config.DEFAULT_SYNC_DURATION_MINUTES

        # Calculate connection counts by type and frequency using a proportional
        # distribution that preserves the integer totals.
        type_split = config.split_proportionally(
            total_connections, {"api": api_percent, "db": db_percent}
        )
        api_count = type_split["api"]
        db_count = type_split["db"]

        freq_split = config.split_proportionally(
            total_connections,
            {
                "sub_hourly": sub_hourly_percent,
                "hourly": hourly_percent,
                "daily": daily_percent,
            },
        )
        sub_hourly_count = freq_split["sub_hourly"]
        hourly_count = freq_split["hourly"]
        daily_count = freq_split["daily"]

        # Distribute each frequency bucket across connector types.
        sub_type = config.split_proportionally(
            sub_hourly_count, {"api": api_percent, "db": db_percent}
        )
        hourly_type = config.split_proportionally(
            hourly_count, {"api": api_percent, "db": db_percent}
        )
        daily_type = config.split_proportionally(
            daily_count, {"api": api_percent, "db": db_percent}
        )

        api_sub = sub_type["api"]
        api_hourly = hourly_type["api"]
        api_daily = daily_type["api"]
        db_sub = sub_type["db"]
        db_hourly = hourly_type["db"]
        db_daily = daily_type["db"]

        # Calculate expected concurrent syncs for each category
        # Proportion of time running = sync_duration / interval
        api_concurrent = (
            api_sub * (sync_duration_minutes / config.SYNC_INTERVALS["sub_hourly"]) +
            api_hourly * (sync_duration_minutes / config.SYNC_INTERVALS["hourly"]) +
            api_daily * (sync_duration_minutes / config.SYNC_INTERVALS["daily"])
        )

        db_concurrent = (
            db_sub * (sync_duration_minutes / config.SYNC_INTERVALS["sub_hourly"]) +
            db_hourly * (sync_duration_minutes / config.SYNC_INTERVALS["hourly"]) +
            db_daily * (sync_duration_minutes / config.SYNC_INTERVALS["daily"])
        )

        # Calculate capacity units
        api_capacity = api_concurrent / self.api_connections_per_worker
        db_capacity = db_concurrent / self.db_connections_per_worker
        total_capacity = api_capacity + db_capacity

        # Round up to get workers needed
        workers_required = ceil(api_capacity) + ceil(db_capacity)

        return {
            "mode": "estimate",
            "worker_type": self.model["worker_type"],
            "input_parameters": {
                "total_connections": total_connections,
                "api_percent": api_percent,
                "db_percent": db_percent,
                "sub_hourly_percent": sub_hourly_percent,
                "hourly_percent": hourly_percent,
                "daily_percent": daily_percent,
                "sync_duration_minutes": sync_duration_minutes
            },
            "connection_breakdown": {
                "api_connections": api_count,
                "db_connections": db_count,
                "by_frequency": {
                    "sub_hourly": sub_hourly_count,
                    "hourly": hourly_count,
                    "daily": daily_count
                }
            },
            "expected_concurrency": {
                "api_concurrent": round(api_concurrent, 2),
                "db_concurrent": round(db_concurrent, 2),
                "total_concurrent": round(api_concurrent + db_concurrent, 2)
            },
            "capacity_calculation": {
                "api_capacity_units": round(api_capacity, 2),
                "db_capacity_units": round(db_capacity, 2),
                "total_capacity_units": round(total_capacity, 2)
            },
            "workers_required": workers_required,
            "calculation_details": {
                "api_connections_per_worker": self.api_connections_per_worker,
                "db_connections_per_worker": self.db_connections_per_worker,
                "model": "statistical"
            }
        }

    def _validate_estimate_inputs(
        self,
        total_connections: int,
        api_percent: float,
        db_percent: float,
        sub_hourly_percent: float,
        hourly_percent: float,
        daily_percent: float
    ) -> None:
        """Validate estimate inputs."""
        if total_connections <= 0:
            raise ValueError("total_connections must be positive")

        # Validate percentages
        config.validate_percentage(api_percent, "api_percent")
        config.validate_percentage(db_percent, "db_percent")
        config.validate_percentage(sub_hourly_percent, "sub_hourly_percent")
        config.validate_percentage(hourly_percent, "hourly_percent")
        config.validate_percentage(daily_percent, "daily_percent")

        # Check that type percentages sum to ~100
        type_sum = api_percent + db_percent
        if abs(type_sum - 100) > 1:  # Allow 1% tolerance
            raise ValueError(
                f"API and DB percentages must sum to 100 (got {type_sum})"
            )

        # Check that frequency percentages sum to ~100
        freq_sum = sub_hourly_percent + hourly_percent + daily_percent
        if abs(freq_sum - 100) > 1:  # Allow 1% tolerance
            raise ValueError(
                f"Frequency percentages must sum to 100 (got {freq_sum})"
            )

# --------------------------------------------------------------------------
# Convenience Functions
# --------------------------------------------------------------------------

def calculate_workers_measured(
    api_concurrent_samples: List[float],
    db_concurrent_samples: List[float],
) -> Dict[str, Any]:
    """
    Calculate workers from measured concurrency data.

    Formula: (API / 5) + (DB / 2) = workers used.

    Args:
        api_concurrent_samples: List of concurrent API connection counts
        db_concurrent_samples: List of concurrent DB connection counts

    Returns:
        Calculation result dictionary
    """
    calculator = WorkerCalculator()
    return calculator.calculate_from_measured_data(
        api_concurrent_samples,
        db_concurrent_samples
    )


def calculate_workers_estimate(
    total_connections: int,
    api_percent: float,
    db_percent: float,
    sub_hourly_percent: float,
    hourly_percent: float,
    daily_percent: float,
    sync_duration_minutes: Optional[float] = None
) -> Dict[str, Any]:
    """
    Calculate workers from estimated parameters.

    Formula: (API / 5) + (DB / 2) = workers used.

    Args:
        total_connections: Total number of connections
        api_percent: Percentage of API connections
        db_percent: Percentage of DB connections
        sub_hourly_percent: Percentage running sub-hourly
        hourly_percent: Percentage running hourly
        daily_percent: Percentage running daily
        sync_duration_minutes: Optional average sync duration

    Returns:
        Calculation result dictionary
    """
    calculator = WorkerCalculator()
    return calculator.calculate_from_estimate(
        total_connections,
        api_percent,
        db_percent,
        sub_hourly_percent,
        hourly_percent,
        daily_percent,
        sync_duration_minutes
    )
