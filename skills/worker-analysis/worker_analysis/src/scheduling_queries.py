#!/usr/bin/env python3
"""
Scheduling Queries for Connection-Level Analysis

This module provides Metabase queries to analyze connection timing patterns
for scheduling optimization recommendations.

Key Features:
- Query connection job history with timing data
- Identify which connections run during peak hours
- Get connection-level scheduling information (cron expressions)

Usage:
    from src.scheduling_queries import (
        build_connection_hourly_query,
        build_peak_connections_query,
        parse_connection_hourly_result,
    )

    # Get query for a customer
    query = build_connection_hourly_query("<Customer>", days=30)
"""

from typing import Any, Dict, List, Optional

from se_config import get_config_value


def _billing_dataset() -> str:
    return get_config_value("bigquery_dataset", "airbyte_warehouse")


def _worker_table() -> str:
    return f"{get_config_value('bigquery_reporting_dataset', 'airbyte_warehouse_reporting')}.node_calculator_raw"


def build_connection_hourly_query(account_name: str, days: int = 30) -> str:
    """
    Build a SQL query to get hourly job patterns per connection.

    This identifies which connections run at which hours, helping to
    understand which connections contribute to peak hour usage.

    Args:
        account_name: Account name in Metabase (e.g., "<Customer>")
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    return f"""
SELECT
    cs.connection_name,
    cs.connection_id,
    cs.source_connector_name,
    cs.destination_connector_name,
    EXTRACT(HOUR FROM cs.start_at) as start_hour_utc,
    COUNT(*) as job_count,
    ROUND(AVG(cs.total_duration_seconds / 60.0), 1) as avg_duration_minutes,
    ROUND(MAX(cs.total_duration_seconds / 60.0), 1) as max_duration_minutes,
    MAX(cs.schedule_type) as schedule_type,
    MAX(cs.cron_expression_cloud) as cron_expression
FROM {_billing_dataset()}.cloud_connection_sync cs
JOIN {_billing_dataset()}.organization o ON cs.organization_id = o.organization_id
JOIN {_billing_dataset()}.account a ON o.account_id = a.account_id
WHERE LOWER(a.account_name_masked) = LOWER('{account_name}')
  AND cs.start_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
  AND cs.job_status = 'succeeded'
GROUP BY
    cs.connection_name,
    cs.connection_id,
    cs.source_connector_name,
    cs.destination_connector_name,
    start_hour_utc
ORDER BY job_count DESC
"""


def build_connection_summary_query(account_name: str, days: int = 30) -> str:
    """
    Build a SQL query to get connection summary with typical run hours.

    This aggregates connection data to show the most common run hour
    for each connection, along with scheduling details.

    Args:
        account_name: Account name in Metabase
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    return f"""
WITH connection_hours AS (
    SELECT
        cs.connection_name,
        cs.connection_id,
        cs.source_connector_name,
        cs.destination_connector_name,
        EXTRACT(HOUR FROM cs.start_at) as start_hour_utc,
        COUNT(*) as hour_job_count,
        ROUND(AVG(cs.total_duration_seconds / 60.0), 1) as avg_duration_minutes
    FROM {_billing_dataset()}.cloud_connection_sync cs
    JOIN {_billing_dataset()}.organization o ON cs.organization_id = o.organization_id
    JOIN {_billing_dataset()}.account a ON o.account_id = a.account_id
    WHERE LOWER(a.account_name_masked) = LOWER('{account_name}')
      AND cs.start_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
      AND cs.job_status = 'succeeded'
    GROUP BY 1, 2, 3, 4, 5
),
connection_totals AS (
    SELECT
        connection_id,
        SUM(hour_job_count) as total_jobs,
        MAX(schedule_type) as schedule_type,
        MAX(cron_expression_cloud) as cron_expression
    FROM {_billing_dataset()}.cloud_connection_sync cs
    JOIN {_billing_dataset()}.organization o ON cs.organization_id = o.organization_id
    JOIN {_billing_dataset()}.account a ON o.account_id = a.account_id
    WHERE LOWER(a.account_name_masked) = LOWER('{account_name}')
      AND cs.start_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
      AND cs.job_status = 'succeeded'
    GROUP BY connection_id
),
ranked_hours AS (
    SELECT
        ch.*,
        ct.total_jobs,
        ct.schedule_type,
        ct.cron_expression,
        ROW_NUMBER() OVER (PARTITION BY ch.connection_id ORDER BY ch.hour_job_count DESC) as rn
    FROM connection_hours ch
    JOIN connection_totals ct ON ch.connection_id = ct.connection_id
)
SELECT
    connection_name,
    connection_id,
    source_connector_name,
    destination_connector_name,
    start_hour_utc as typical_run_hour,
    hour_job_count as runs_at_typical_hour,
    total_jobs,
    avg_duration_minutes,
    schedule_type,
    cron_expression
FROM ranked_hours
WHERE rn = 1
ORDER BY total_jobs DESC
"""


def build_peak_connections_query(
    account_name: str,
    peak_hours: List[int],
    days: int = 30
) -> str:
    """
    Build a SQL query to identify connections running during peak hours.

    Args:
        account_name: Account name in Metabase
        peak_hours: List of peak hour integers (0-23 UTC)
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    peak_hours_str = ", ".join(str(h) for h in peak_hours)

    return f"""
WITH connection_peak_jobs AS (
    SELECT
        cs.connection_name,
        cs.connection_id,
        cs.source_connector_name,
        cs.destination_connector_name,
        EXTRACT(HOUR FROM cs.start_at) as start_hour_utc,
        COUNT(*) as job_count,
        ROUND(AVG(cs.total_duration_seconds / 60.0), 1) as avg_duration_minutes,
        MAX(cs.schedule_type) as schedule_type,
        MAX(cs.cron_expression_cloud) as cron_expression
    FROM {_billing_dataset()}.cloud_connection_sync cs
    JOIN {_billing_dataset()}.organization o ON cs.organization_id = o.organization_id
    JOIN {_billing_dataset()}.account a ON o.account_id = a.account_id
    WHERE LOWER(a.account_name_masked) = LOWER('{account_name}')
      AND cs.start_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
      AND cs.job_status = 'succeeded'
      AND EXTRACT(HOUR FROM cs.start_at) IN ({peak_hours_str})
    GROUP BY 1, 2, 3, 4, 5
),
connection_totals AS (
    SELECT
        connection_id,
        SUM(job_count) as total_peak_jobs
    FROM connection_peak_jobs
    GROUP BY connection_id
)
SELECT
    cpj.connection_name,
    cpj.connection_id,
    cpj.source_connector_name,
    cpj.destination_connector_name,
    cpj.start_hour_utc as peak_hour,
    cpj.job_count as jobs_at_peak_hour,
    ct.total_peak_jobs,
    cpj.avg_duration_minutes,
    cpj.schedule_type,
    cpj.cron_expression
FROM connection_peak_jobs cpj
JOIN connection_totals ct ON cpj.connection_id = ct.connection_id
ORDER BY ct.total_peak_jobs DESC, cpj.job_count DESC
"""


def build_hourly_worker_connection_overlap_query(account_name: str, days: int = 30) -> str:
    """
    Build a SQL query to understand worker-connection overlap by hour.

    This helps estimate how many workers are used by connections at each hour.

    Args:
        account_name: Account name in Metabase
        days: Number of days to analyze

    Returns:
        SQL query string
    """
    return f"""
WITH hourly_connection_activity AS (
    SELECT
        EXTRACT(HOUR FROM cs.start_at) as hour_utc,
        COUNT(DISTINCT cs.connection_id) as active_connections,
        COUNT(*) as total_jobs,
        ROUND(SUM(cs.total_duration_seconds / 60.0), 0) as total_job_minutes
    FROM {_billing_dataset()}.cloud_connection_sync cs
    JOIN {_billing_dataset()}.organization o ON cs.organization_id = o.organization_id
    JOIN {_billing_dataset()}.account a ON o.account_id = a.account_id
    WHERE LOWER(a.account_name_masked) = LOWER('{account_name}')
      AND cs.start_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
      AND cs.job_status = 'succeeded'
    GROUP BY hour_utc
),
hourly_workers AS (
    SELECT
        EXTRACT(HOUR FROM minute_timestamp) as hour_utc,
        ROUND(AVG(total_nodes), 2) as avg_workers,
        MAX(total_nodes) as max_workers,
        APPROX_QUANTILES(total_nodes, 100)[OFFSET(99)] as p99_workers
    FROM `{_worker_table()}`
    WHERE account_name = '{account_name}'
      AND minute_timestamp >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    GROUP BY hour_utc
)
SELECT
    hw.hour_utc,
    hw.avg_workers,
    hw.max_workers,
    hw.p99_workers,
    COALESCE(hca.active_connections, 0) as active_connections,
    COALESCE(hca.total_jobs, 0) as total_jobs,
    COALESCE(hca.total_job_minutes, 0) as total_job_minutes
FROM hourly_workers hw
LEFT JOIN hourly_connection_activity hca ON hw.hour_utc = hca.hour_utc
ORDER BY hw.hour_utc
"""


# Result parsing functions

def parse_connection_hourly_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse connection hourly query results.

    Args:
        result: Raw Metabase query result

    Returns:
        List of connection hour records
    """
    if "error" in result:
        return []

    rows = result.get("data", {}).get("rows", [])
    cols = result.get("data", {}).get("cols", [])
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(cols)]

    records = []
    for row in rows:
        data = dict(zip(col_names, row))
        records.append({
            "connection_name": data.get("connection_name", "Unknown"),
            "connection_id": data.get("connection_id"),
            "source": data.get("source_connector_name", ""),
            "destination": data.get("destination_connector_name", ""),
            "start_hour_utc": int(data.get("start_hour_utc", 0)),
            "job_count": int(data.get("job_count", 0)),
            "avg_duration_minutes": float(data.get("avg_duration_minutes", 0)),
            "max_duration_minutes": float(data.get("max_duration_minutes", 0)),
            "schedule_type": data.get("schedule_type"),
            "cron_expression": data.get("cron_expression"),
        })

    return records


def parse_connection_summary_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse connection summary query results.

    Args:
        result: Raw Metabase query result

    Returns:
        List of connection summary records
    """
    if "error" in result:
        return []

    rows = result.get("data", {}).get("rows", [])
    cols = result.get("data", {}).get("cols", [])
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(cols)]

    records = []
    for row in rows:
        data = dict(zip(col_names, row))
        records.append({
            "connection_name": data.get("connection_name", "Unknown"),
            "connection_id": data.get("connection_id"),
            "source": data.get("source_connector_name", ""),
            "destination": data.get("destination_connector_name", ""),
            "typical_run_hour": int(data.get("typical_run_hour", 0)),
            "runs_at_typical_hour": int(data.get("runs_at_typical_hour", 0)),
            "total_jobs": int(data.get("total_jobs", 0)),
            "avg_duration_minutes": float(data.get("avg_duration_minutes", 0)),
            "schedule_type": data.get("schedule_type"),
            "cron_expression": data.get("cron_expression"),
        })

    return records


def parse_peak_connections_result(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Parse peak connections query results.

    Args:
        result: Raw Metabase query result

    Returns:
        List of peak connection records
    """
    if "error" in result:
        return []

    rows = result.get("data", {}).get("rows", [])
    cols = result.get("data", {}).get("cols", [])
    col_names = [c.get("name", f"col_{i}") for i, c in enumerate(cols)]

    records = []
    for row in rows:
        data = dict(zip(col_names, row))
        records.append({
            "connection_name": data.get("connection_name", "Unknown"),
            "connection_id": data.get("connection_id"),
            "source": data.get("source_connector_name", ""),
            "destination": data.get("destination_connector_name", ""),
            "peak_hour": int(data.get("peak_hour", 0)),
            "jobs_at_peak_hour": int(data.get("jobs_at_peak_hour", 0)),
            "total_peak_jobs": int(data.get("total_peak_jobs", 0)),
            "avg_duration_minutes": float(data.get("avg_duration_minutes", 0)),
            "schedule_type": data.get("schedule_type"),
            "cron_expression": data.get("cron_expression"),
        })

    return records


def parse_hourly_overlap_result(result: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    """
    Parse hourly worker-connection overlap results.

    Args:
        result: Raw Metabase query result

    Returns:
        Dictionary mapping hour to overlap metrics
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
            "avg_workers": float(data.get("avg_workers", 0)),
            "max_workers": float(data.get("max_workers", 0)),
            "p99_workers": float(data.get("p99_workers", 0)),
            "active_connections": int(data.get("active_connections", 0)),
            "total_jobs": int(data.get("total_jobs", 0)),
            "total_job_minutes": float(data.get("total_job_minutes", 0)),
        }

    return hourly


# Analysis helper functions

def identify_peak_hours(hourly_data: Dict[int, Dict[str, float]], top_n: int = 3) -> List[int]:
    """
    Identify the top N peak hours by P99 worker usage.

    Args:
        hourly_data: Hour -> metrics mapping from parse_metabase_hourly_result
        top_n: Number of peak hours to identify

    Returns:
        List of peak hour integers (0-23)
    """
    if not hourly_data:
        return []

    # Sort hours by P99 workers descending
    sorted_hours = sorted(
        hourly_data.items(),
        key=lambda x: x[1].get("p99_workers", 0),
        reverse=True
    )

    return [hour for hour, _ in sorted_hours[:top_n]]


def identify_quiet_hours(hourly_data: Dict[int, Dict[str, float]], bottom_n: int = 5) -> List[int]:
    """
    Identify the bottom N quiet hours by P99 worker usage.

    Args:
        hourly_data: Hour -> metrics mapping from parse_metabase_hourly_result
        bottom_n: Number of quiet hours to identify

    Returns:
        List of quiet hour integers (0-23), sorted by lowest usage first
    """
    if not hourly_data:
        return []

    # Sort hours by P99 workers ascending
    sorted_hours = sorted(
        hourly_data.items(),
        key=lambda x: x[1].get("p99_workers", 0)
    )

    return [hour for hour, _ in sorted_hours[:bottom_n]]


def categorize_hours(
    hourly_data: Dict[int, Dict[str, float]]
) -> Dict[str, List[int]]:
    """
    Categorize all 24 hours into peak, normal, and quiet categories.

    Args:
        hourly_data: Hour -> metrics mapping

    Returns:
        Dictionary with 'peak', 'normal', and 'quiet' hour lists
    """
    if not hourly_data:
        return {"peak": [], "normal": [], "quiet": []}

    # Calculate P99 thresholds
    p99_values = [h.get("p99_workers", 0) for h in hourly_data.values()]
    if not p99_values:
        return {"peak": [], "normal": [], "quiet": []}

    max_p99 = max(p99_values)
    min_p99 = min(p99_values)
    range_p99 = max_p99 - min_p99

    # Define thresholds (top 25% = peak, bottom 25% = quiet)
    peak_threshold = min_p99 + (range_p99 * 0.75) if range_p99 > 0 else max_p99
    quiet_threshold = min_p99 + (range_p99 * 0.25) if range_p99 > 0 else min_p99

    categories = {"peak": [], "normal": [], "quiet": []}

    for hour, metrics in hourly_data.items():
        p99 = metrics.get("p99_workers", 0)
        if p99 >= peak_threshold:
            categories["peak"].append(hour)
        elif p99 <= quiet_threshold:
            categories["quiet"].append(hour)
        else:
            categories["normal"].append(hour)

    # Sort each category
    for key in categories:
        categories[key].sort()

    return categories


def aggregate_connections_by_hour(
    connection_records: List[Dict[str, Any]]
) -> Dict[int, List[Dict[str, Any]]]:
    """
    Aggregate connection records by their typical run hour.

    Args:
        connection_records: List of connection records from parse functions

    Returns:
        Dictionary mapping hour to list of connections
    """
    by_hour = {}
    for record in connection_records:
        hour = record.get("typical_run_hour", record.get("start_hour_utc", 0))
        if hour not in by_hour:
            by_hour[hour] = []
        by_hour[hour].append(record)

    return by_hour


# Query registry for MCP server
SCHEDULING_QUERIES = {
    "connection_hourly": build_connection_hourly_query,
    "connection_summary": build_connection_summary_query,
    "peak_connections": build_peak_connections_query,
    "hourly_overlap": build_hourly_worker_connection_overlap_query,
}
