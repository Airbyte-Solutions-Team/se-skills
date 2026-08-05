#!/usr/bin/env python3
"""
Metabase Worker Data Integration

This module provides direct access to worker usage data from Metabase,
using the node_calculator_raw table in BigQuery. This replaces the need
for manual CSV exports.

Key Feature: Uses P99 (99th percentile) as the primary billing metric,
which matches Airbyte's actual billing calculation. This allows ~7.5 hours
of burst usage per month that doesn't count against contracted workers.

Usage:
    from src.metabase_worker_data import get_worker_data, get_worker_data_detailed

    # Get summary for a customer
    data = get_worker_data("<Customer>", days=30)

    # Get detailed minute-by-minute data
    detailed = get_worker_data_detailed("<Customer>", days=30)
"""

import os
import sys
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from se_config import get_config_value


def _worker_table() -> str:
    return f"{get_config_value('bigquery_reporting_dataset', 'airbyte_warehouse_reporting')}.node_calculator_raw"

# Try to import MCP tools - these are available when running in Claude Code context
try:
    # When running as part of the MCP ecosystem, we can use the Metabase MCP tools
    HAS_MCP_CONTEXT = True
except ImportError:
    HAS_MCP_CONTEXT = False


def _execute_metabase_query(query: str, database_id: int = 2) -> Dict[str, Any]:
    """
    Execute a SQL query against Metabase.

    This function is designed to be called from within a Claude Code context
    where the Metabase MCP is available.

    Args:
        query: SQL query to execute
        database_id: Metabase database ID (2 = BigQuery - Business Tables)

    Returns:
        Query results as a dictionary
    """
    # This will be replaced by actual MCP call in the MCP server context
    raise NotImplementedError(
        "Direct Metabase queries require the Metabase MCP context. "
        "Use the mcp__metabase__execute_query tool instead."
    )


def build_worker_summary_query(account_name: str, days: int = 30) -> str:
    """
    Build a SQL query to get worker usage summary for an account.

    Returns P99 as the primary billing metric.

    Args:
        account_name: Account name in Metabase (e.g., "<Customer>")
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    return f"""
SELECT
    account_name,
    MIN(minute_timestamp) as period_start,
    MAX(minute_timestamp) as period_end,
    COUNT(*) as total_minutes,
    MAX(contracted_workers) as contracted_workers,
    MAX(total_nodes) as peak_workers,
    ROUND(AVG(total_nodes), 2) as avg_workers,
    APPROX_QUANTILES(total_nodes, 100)[OFFSET(99)] as p99_workers,
    APPROX_QUANTILES(total_nodes, 100)[OFFSET(95)] as p95_workers,
    APPROX_QUANTILES(total_nodes, 100)[OFFSET(50)] as median_workers,
    MIN(total_nodes) as min_workers
FROM `{_worker_table()}`
WHERE account_name = '{account_name}'
  AND minute_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
GROUP BY account_name
"""


def build_worker_hourly_query(account_name: str, days: int = 30) -> str:
    """
    Build a SQL query to get hourly worker usage patterns.

    Args:
        account_name: Account name in Metabase
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    return f"""
SELECT
    EXTRACT(HOUR FROM minute_timestamp) as hour_utc,
    ROUND(AVG(total_nodes), 2) as avg_workers,
    MAX(total_nodes) as max_workers,
    APPROX_QUANTILES(total_nodes, 100)[OFFSET(99)] as p99_workers,
    COUNT(*) as sample_count
FROM `{_worker_table()}`
WHERE account_name = '{account_name}'
  AND minute_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
GROUP BY hour_utc
ORDER BY hour_utc
"""


def build_worker_daily_query(account_name: str, days: int = 30) -> str:
    """
    Build a SQL query to get daily worker usage patterns.

    Args:
        account_name: Account name in Metabase
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    return f"""
SELECT
    FORMAT_TIMESTAMP('%A', minute_timestamp) as day_of_week,
    EXTRACT(DAYOFWEEK FROM minute_timestamp) as day_num,
    ROUND(AVG(total_nodes), 2) as avg_workers,
    MAX(total_nodes) as max_workers,
    APPROX_QUANTILES(total_nodes, 100)[OFFSET(99)] as p99_workers
FROM `{_worker_table()}`
WHERE account_name = '{account_name}'
  AND minute_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
GROUP BY day_of_week, day_num
ORDER BY day_num
"""


def build_worker_timeseries_query(account_name: str, days: int = 30) -> str:
    """
    Build a SQL query to get minute-by-minute worker data.

    Args:
        account_name: Account name in Metabase
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    return f"""
SELECT
    minute_timestamp as timestamp,
    total_nodes as workers,
    contracted_workers
FROM `{_worker_table()}`
WHERE account_name = '{account_name}'
  AND minute_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
ORDER BY minute_timestamp
"""


def build_over_limit_query(account_name: str, days: int = 30) -> str:
    """
    Build a SQL query to find periods where workers exceeded contracted limit.

    Args:
        account_name: Account name in Metabase
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    return f"""
SELECT
    minute_timestamp as timestamp,
    total_nodes as workers,
    contracted_workers,
    total_nodes - contracted_workers as overage
FROM `{_worker_table()}`
WHERE account_name = '{account_name}'
  AND minute_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
  AND total_nodes > contracted_workers
ORDER BY minute_timestamp
"""


def parse_metabase_summary_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse Metabase query results into a standardized summary format.

    Args:
        result: Raw Metabase query result

    Returns:
        Parsed summary dictionary with P99 as primary billing metric
    """
    if "error" in result:
        return {"error": result["error"]}

    rows = result.get("data", {}).get("rows", [])
    cols = result.get("data", {}).get("cols", [])

    if not rows:
        return {"error": "No data returned from query"}

    # Build column name mapping
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(cols)]

    row = rows[0]
    data = dict(zip(col_names, row))

    # Extract and calculate key metrics
    contracted = float(data.get("contracted_workers", 0))
    p99 = float(data.get("p99_workers", 0))
    peak = float(data.get("peak_workers", 0))
    avg = float(data.get("avg_workers", 0))

    return {
        "account_name": data.get("account_name"),
        "period_start": data.get("period_start"),
        "period_end": data.get("period_end"),
        "total_minutes": int(data.get("total_minutes", 0)),
        "contracted_workers": int(contracted),

        # P99 is the PRIMARY billing metric
        "billing_workers": round(p99, 2),  # What Airbyte charges for
        "p99_workers": round(p99, 2),
        "p95_workers": round(float(data.get("p95_workers", 0)), 2),
        "median_workers": round(float(data.get("median_workers", 0)), 2),

        # Peak is for reference only (not billing)
        "peak_workers": round(peak, 2),
        "avg_workers": round(avg, 2),
        "min_workers": round(float(data.get("min_workers", 0)), 2),

        # Utilization based on P99 (billing metric)
        "billing_utilization_pct": round((p99 / contracted * 100) if contracted > 0 else 0, 1),
        "peak_utilization_pct": round((peak / contracted * 100) if contracted > 0 else 0, 1),
        "avg_utilization_pct": round((avg / contracted * 100) if contracted > 0 else 0, 1),

        # Headroom based on billing metric
        "billing_headroom": round(contracted - p99, 2),
        "peak_headroom": round(contracted - peak, 2),

        # Capacity assessment
        "capacity_status": get_capacity_status(p99, contracted),
    }


def get_capacity_status(p99_workers: float, contracted: int) -> str:
    """
    Determine capacity status based on P99 utilization.

    Args:
        p99_workers: 99th percentile worker usage
        contracted: Contracted worker count

    Returns:
        Status string
    """
    if contracted == 0:
        return "unknown"

    utilization = (p99_workers / contracted) * 100

    if utilization > 100:
        return "over_capacity"
    elif utilization > 85:
        return "near_capacity"
    elif utilization > 60:
        return "healthy"
    elif utilization > 30:
        return "under_utilized"
    else:
        return "significantly_under_utilized"


def parse_metabase_hourly_result(result: Dict[str, Any]) -> Dict[int, Dict[str, float]]:
    """
    Parse hourly usage pattern results.

    Args:
        result: Raw Metabase query result

    Returns:
        Dictionary mapping hour (0-23) to usage stats
    """
    if "error" in result:
        return {}

    rows = result.get("data", {}).get("rows", [])
    cols = result.get("data", {}).get("cols", [])
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(cols)]

    hourly = {}
    for row in rows:
        data = dict(zip(col_names, row))
        hour = int(data.get("hour_utc", 0))
        hourly[hour] = {
            "avg_workers": round(float(data.get("avg_workers", 0)), 2),
            "max_workers": round(float(data.get("max_workers", 0)), 2),
            "p99_workers": round(float(data.get("p99_workers", 0)), 2),
        }

    return hourly


def parse_metabase_daily_result(result: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """
    Parse daily usage pattern results.

    Args:
        result: Raw Metabase query result

    Returns:
        Dictionary mapping day name to usage stats
    """
    if "error" in result:
        return {}

    rows = result.get("data", {}).get("rows", [])
    cols = result.get("data", {}).get("cols", [])
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(cols)]

    daily = {}
    for row in rows:
        data = dict(zip(col_names, row))
        day = data.get("day_of_week", "Unknown")
        daily[day] = {
            "avg_workers": round(float(data.get("avg_workers", 0)), 2),
            "max_workers": round(float(data.get("max_workers", 0)), 2),
            "p99_workers": round(float(data.get("p99_workers", 0)), 2),
        }

    return daily


def generate_utilization_assessment(summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate a detailed utilization assessment based on P99 billing metric.

    Args:
        summary: Parsed summary from parse_metabase_summary_result

    Returns:
        Assessment with recommendations
    """
    if "error" in summary:
        return {"error": summary["error"]}

    contracted = summary.get("contracted_workers", 0)
    p99 = summary.get("p99_workers", 0)
    peak = summary.get("peak_workers", 0)
    billing_util = summary.get("billing_utilization_pct", 0)
    status = summary.get("capacity_status", "unknown")

    assessment = {
        "status": status,
        "billing_metric": "P99 (99th percentile)",
        "billing_workers": p99,
        "billing_utilization_pct": billing_util,
        "contracted_workers": contracted,
    }

    # Generate recommendations based on status
    if status == "over_capacity":
        assessment["recommendation"] = "increase_workers"
        assessment["recommended_workers"] = max(contracted + 1, int(p99) + 1)
        assessment["message"] = (
            f"P99 usage ({p99:.1f}) exceeds contracted capacity ({contracted}). "
            f"Recommend increasing to {assessment['recommended_workers']} workers."
        )
        assessment["urgency"] = "high"

    elif status == "near_capacity":
        assessment["recommendation"] = "monitor_closely"
        assessment["recommended_workers"] = contracted
        assessment["message"] = (
            f"Operating at {billing_util:.0f}% of capacity (P99: {p99:.1f}/{contracted}). "
            "Monitor usage trends and consider adding capacity if growth expected."
        )
        assessment["urgency"] = "medium"

    elif status == "healthy":
        assessment["recommendation"] = "maintain"
        assessment["recommended_workers"] = contracted
        assessment["message"] = (
            f"Healthy utilization at {billing_util:.0f}% (P99: {p99:.1f}/{contracted}). "
            "Current allocation is appropriate."
        )
        assessment["urgency"] = "none"

    elif status == "under_utilized":
        # Only recommend reduction if significantly under-utilized
        assessment["recommendation"] = "review_allocation"
        potential_reduction = contracted - max(1, int(p99) + 1)
        assessment["potential_reduction"] = max(0, potential_reduction)
        assessment["message"] = (
            f"Under-utilized at {billing_util:.0f}% (P99: {p99:.1f}/{contracted}). "
            f"Could potentially reduce by {potential_reduction} worker(s)."
        )
        assessment["urgency"] = "low"

    else:  # significantly_under_utilized
        assessment["recommendation"] = "reduce_workers"
        recommended = max(1, int(p99) + 1)
        assessment["recommended_workers"] = recommended
        assessment["potential_reduction"] = contracted - recommended
        assessment["message"] = (
            f"Significantly under-utilized at {billing_util:.0f}% (P99: {p99:.1f}/{contracted}). "
            f"Consider reducing to {recommended} workers to optimize costs."
        )
        assessment["urgency"] = "low"

    # Add context about P99 vs peak
    if p99 > 0 and peak > p99 * 1.3:  # Peak is 30%+ higher than P99
        assessment["burst_note"] = (
            f"Peak usage ({peak:.1f}) is {((peak/p99)-1)*100:.0f}% higher than P99. "
            "These bursts are within Airbyte's ~7.5 hour allowance and don't affect billing."
        )

    return assessment


# Queries available for MCP server to use
AVAILABLE_QUERIES = {
    "summary": build_worker_summary_query,
    "hourly": build_worker_hourly_query,
    "daily": build_worker_daily_query,
    "timeseries": build_worker_timeseries_query,
    "over_limit": build_over_limit_query,
}
