"""
Metabase Billing Meter Data Integration

Queries the official billing meter tables for worker usage data:
- organization_data_worker_usage_daily: Daily peak workers (P99 billing)
- workspace_data_worker_usage_hourly: Hourly worker patterns
- account: Account metadata (name, ARR, owner)
- organization: Organization metadata (plan, lifecycle, connections)

These tables are the AUTHORITATIVE source for worker billing.
The data includes source + destination + orchestrator pod costs.

Usage:
    Called via mcp__metabase__execute_query(database_id=2, query=...)
    from the Claude Code agent executing the /workers command.
"""

from typing import Dict, List, Any, Optional

from se_config import get_config_value


# BigQuery - Business Tables (Metabase database ID)
METABASE_DATABASE_ID = 2


def _billing_dataset() -> str:
    return get_config_value("bigquery_dataset", "airbyte_warehouse")


# =============================================================================
# QUERY BUILDERS
# =============================================================================

def build_daily_usage_query(org_id: str, days: int = 30) -> str:
    """
    Query organization_data_worker_usage_daily for P99 billing data.

    Returns daily peak workers for the last N days.
    This is the ground truth for worker billing.
    """
    return f"""SELECT worker_usage_date, contracted_data_workers, max_data_workers_used, workspaces
FROM {_billing_dataset()}.organization_data_worker_usage_daily
WHERE organization_id = '{org_id}'
ORDER BY worker_usage_date DESC
LIMIT {days}"""


def build_hourly_pattern_query(org_id: str, days: int = 7) -> str:
    """
    Query workspace_data_worker_usage_hourly for peak hour analysis.

    Returns hourly worker usage broken down by source/destination/orchestrator.
    Use this to identify peak hours for optimization.
    """
    return f"""SELECT worker_usage_day_hour, data_workers_used,
       source_data_workers, destination_data_workers,
       orchestrator_data_workers, workspace_name_masked
FROM {_billing_dataset()}.workspace_data_worker_usage_hourly
WHERE organization_id = '{org_id}'
AND worker_usage_day_hour >= DATE_SUB(CURRENT_DATE(), INTERVAL {days} DAY)
ORDER BY worker_usage_day_hour DESC
LIMIT 200"""


def build_account_info_query(org_id: str) -> str:
    """
    Query account table for customer metadata.

    Returns account name, owner, ARR, and type.
    """
    return f"""SELECT account_name_masked, organization_id, account_owner,
       salesforce_arr, account_type, account_segment
FROM {_billing_dataset()}.account
WHERE organization_id = '{org_id}'
LIMIT 5"""


def build_org_info_query(org_id: str) -> str:
    """
    Query organization table for org metadata.

    Returns plan name, lifecycle stage, connection counts.
    """
    return f"""SELECT organization_id, organization_name_masked,
       customer_lifecycle_stage, customer_product_type,
       orb_plan_name, subscription_status,
       num_active_connections, num_workspaces_in_org,
       first_sync_at, last_sync_at, is_active
FROM {_billing_dataset()}.organization
WHERE organization_id = '{org_id}'
LIMIT 1"""


# =============================================================================
# RESULT PARSERS
# =============================================================================

def parse_daily_results(rows: List[List]) -> Dict[str, Any]:
    """
    Parse daily usage query results.

    Args:
        rows: List of [worker_usage_date, contracted_data_workers, max_data_workers_used, workspaces]

    Returns:
        Dict with p99, avg, max, contracted, and daily_peaks list
    """
    if not rows:
        return {"has_data": False}

    daily_peaks = []
    contracted = None

    for row in rows:
        date_str = str(row[0])[:10] if row[0] else "unknown"
        contracted_val = row[1]
        max_workers = float(row[2]) if row[2] else 0
        workspaces = int(row[3]) if row[3] else 0

        if contracted_val is not None:
            contracted = float(contracted_val)

        daily_peaks.append({
            "date": date_str,
            "max_workers": max_workers,
            "workspaces": workspaces,
        })

    max_vals = [p["max_workers"] for p in daily_peaks if p["max_workers"] > 0]

    if not max_vals:
        return {"has_data": False}

    sorted_vals = sorted(max_vals)
    n = len(sorted_vals)
    p99 = sorted_vals[min(int(n * 0.99), n - 1)]
    avg = sum(max_vals) / len(max_vals)
    peak = max(max_vals)

    return {
        "has_data": True,
        "p99_workers": round(p99, 2),
        "avg_workers": round(avg, 2),
        "max_workers": round(peak, 2),
        "min_workers": round(min(max_vals), 2),
        "contracted_workers": contracted,
        "days_analyzed": len(daily_peaks),
        "daily_peaks": daily_peaks,
    }


def parse_hourly_results(rows: List[List]) -> Dict[str, Any]:
    """
    Parse hourly pattern query results.

    Args:
        rows: List of [worker_usage_day_hour, data_workers_used, source, dest, orchestrator, workspace]

    Returns:
        Dict with peak_hour, quiet_hours, hourly breakdown
    """
    if not rows:
        return {"has_data": False}

    from collections import defaultdict

    # Aggregate by hour-of-day across all days
    hourly_totals = defaultdict(list)
    hourly_peak = defaultdict(float)

    for row in rows:
        hour_str = str(row[0])
        workers = float(row[1]) if row[1] else 0

        # Extract hour from timestamp
        try:
            hour = int(hour_str[11:13])
        except (ValueError, IndexError):
            continue

        hourly_totals[hour].append(workers)
        hourly_peak[hour] = max(hourly_peak[hour], workers)

    # Build hourly summary
    hourly_summary = {}
    for hour in range(24):
        vals = hourly_totals.get(hour, [0])
        hourly_summary[hour] = {
            "avg_workers": round(sum(vals) / len(vals), 3) if vals else 0,
            "max_workers": round(hourly_peak.get(hour, 0), 3),
        }

    # Find peak and quiet hours
    peak_hour = max(range(24), key=lambda h: hourly_summary[h]["max_workers"])
    quiet_hours = [h for h in range(24) if hourly_summary[h]["max_workers"] == 0]

    return {
        "has_data": True,
        "peak_hour": peak_hour,
        "peak_workers": hourly_summary[peak_hour]["max_workers"],
        "quiet_hours": quiet_hours,
        "hourly_summary": hourly_summary,
    }


def parse_account_results(rows: List[List]) -> Dict[str, Any]:
    """Parse account info query results."""
    if not rows:
        return {"has_data": False}

    row = rows[0]
    return {
        "has_data": True,
        "account_name": row[0],
        "organization_id": row[1],
        "account_owner": row[2],
        "salesforce_arr": float(row[3]) if row[3] else 0,
        "account_type": row[4],
        "account_segment": row[5],
    }


def parse_org_results(rows: List[List]) -> Dict[str, Any]:
    """Parse organization info query results."""
    if not rows:
        return {"has_data": False}

    row = rows[0]
    return {
        "has_data": True,
        "organization_id": row[0],
        "organization_name": row[1],
        "lifecycle_stage": row[2],
        "product_type": row[3],
        "orb_plan_name": row[4],
        "subscription_status": row[5],
        "num_active_connections": int(row[6]) if row[6] else 0,
        "num_workspaces": int(row[7]) if row[7] else 0,
        "first_sync": str(row[8])[:10] if row[8] else None,
        "last_sync": str(row[9])[:10] if row[9] else None,
        "is_active": row[10],
    }
