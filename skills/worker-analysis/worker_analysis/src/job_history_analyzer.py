#!/usr/bin/env python3
"""
Job History Analyzer for Scheduling Optimization Reports

Fetches job history from Airbyte Cloud API and analyzes actual sync patterns.
Uses caching to avoid redundant API calls - data is saved per customer.

Key Features:
- Caches job history data per customer (data/job_history/{customer}/)
- ~1 week of history is sufficient if intervals are consistent
- Analyzes actual sync patterns for externally orchestrated syncs (Airflow/Dagster)
- Provides comprehensive sync statistics (frequency, duration, source types)

Usage:
    # RECOMMENDED: Use cache-aware function
    from src.job_history_cache import get_or_fetch_job_history
    results = get_or_fetch_job_history("CustomerName", organization_id="...")

    # Direct API call (no caching):
    from src.job_history_analyzer import analyze_customer_job_history
    results = analyze_customer_job_history(organization_id="28b60a28-...")

    # Results include:
    # - total_pipelines
    # - source_type_breakdown (api_sources, database_sources, unknown_sources)
    # - sync_frequency_breakdown (sub_hourly, hourly, daily)
    # - sync_duration_stats (avg, median, p90)
    # - connections (detailed per-connection data)
"""

import os
import re
import time
import requests
import statistics
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict

# Import credentials helper
try:
    from src.credentials import get_airbyte_credentials, ensure_credentials_loaded
    from src.airbyte_cloud_data import get_workspace_ids_for_organization
except ImportError:
    try:
        from credentials import get_airbyte_credentials, ensure_credentials_loaded
        from airbyte_cloud_data import get_workspace_ids_for_organization
    except ImportError:
        def get_workspace_ids_for_organization(org_id):
            return []

# API Configuration
TOKEN_URL = "https://api.airbyte.com/v1/applications/token"
CONNECTIONS_URL = "https://api.airbyte.com/v1/connections"
SOURCES_URL = "https://api.airbyte.com/v1/sources"
WORKSPACES_URL = "https://api.airbyte.com/v1/workspaces"
JOBS_URL = "https://api.airbyte.com/v1/jobs"


def get_access_token(client_id: str, client_secret: str) -> str:
    """Fetch access token from Airbyte Cloud API."""
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials"
    }
    r = requests.post(
        TOKEN_URL,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=payload,
        timeout=30
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise ValueError("No access_token in response")
    return token


def fetch_job_history_for_connection(
    token: str,
    connection_id: str,
    limit: int = 100,
    max_retries: int = 3
) -> List[Dict]:
    """
    Fetch job history for a single connection.

    Args:
        token: Airbyte API access token
        connection_id: Connection ID to fetch jobs for
        limit: Number of recent jobs to fetch (default 100 = ~10 days for daily syncs)
        max_retries: Max retry attempts on failure

    Returns:
        List of job records
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    params = {
        "connectionId": connection_id,
        "limit": limit,
        "orderBy": "createdAt|DESC"
    }

    for attempt in range(max_retries):
        try:
            r = requests.get(JOBS_URL, headers=headers, params=params, timeout=60)
            r.raise_for_status()
            return r.json().get("data", [])
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                time.sleep(wait_time)
                continue
            return []
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:  # Rate limited
                wait_time = 5 * (attempt + 1)
                time.sleep(wait_time)
                continue
            return []
        except Exception:
            return []

    return []


def parse_timestamp(timestamp_str: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp to datetime object."""
    if not timestamp_str:
        return None
    try:
        return datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
    except Exception:
        return None


def parse_duration_to_seconds(duration_str: str) -> Optional[float]:
    """Parse ISO 8601 duration (PT2M4S) to seconds."""
    if not duration_str or not duration_str.startswith('PT'):
        return None

    try:
        import re
        pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?'
        match = re.match(pattern, duration_str)
        if not match:
            return None

        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = float(match.group(3) or 0)

        return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return None


def process_jobs(jobs: List[Dict]) -> List[Dict]:
    """
    Process raw job data to extract timing information.

    Returns list of dicts with:
    - start_time: datetime
    - end_time: datetime
    - duration_seconds: float
    - hour_utc: int (0-23)
    - status: str
    """
    processed = []

    for job in jobs:
        start_str = job.get("startTime") or job.get("startedAt") or job.get("createdAt")
        if not start_str:
            continue

        start_time = parse_timestamp(start_str)
        if not start_time:
            continue

        status = job.get("status", "unknown")

        # Get duration
        duration = None
        end_time = None

        if job.get("duration"):
            duration = parse_duration_to_seconds(job.get("duration"))

        # Calculate from end time if no duration
        if not duration and status in ["succeeded", "failed", "cancelled"]:
            end_str = job.get("lastUpdatedAt") or job.get("endedAt")
            if end_str:
                end_time = parse_timestamp(end_str)
                if end_time:
                    duration = (end_time - start_time).total_seconds()

        if not duration or duration <= 0:
            continue

        if not end_time:
            end_time = start_time + timedelta(seconds=duration)

        processed.append({
            "start_time": start_time,
            "end_time": end_time,
            "duration_seconds": duration,
            "hour_utc": start_time.hour,
            "status": status,
            "job_id": job.get("jobId"),
        })

    return processed


def analyze_job_patterns(
    jobs: List[Dict],
    days_to_analyze: int = 10
) -> Dict[str, Any]:
    """
    Analyze job patterns for a single connection.

    Returns:
        Dict with pattern analysis including typical run hours, frequency, etc.
    """
    if not jobs:
        return {"has_data": False}

    # Filter to recent jobs
    cutoff = datetime.now(jobs[0]["start_time"].tzinfo) - timedelta(days=days_to_analyze)
    recent_jobs = [j for j in jobs if j["start_time"] > cutoff]

    if not recent_jobs:
        return {"has_data": False}

    # Count jobs per hour
    hour_counts = defaultdict(int)
    for job in recent_jobs:
        hour_counts[job["hour_utc"]] += 1

    # Find most common run hours
    sorted_hours = sorted(hour_counts.items(), key=lambda x: x[1], reverse=True)
    typical_hours = [h for h, _ in sorted_hours[:3]]

    # Calculate average duration
    durations = [j["duration_seconds"] for j in recent_jobs]
    avg_duration = sum(durations) / len(durations) if durations else 0

    # Calculate sync frequency (average hours between syncs) with consistency check
    intervals = []
    if len(recent_jobs) >= 2:
        sorted_jobs = sorted(recent_jobs, key=lambda x: x["start_time"])
        for i in range(1, len(sorted_jobs)):
            delta = (sorted_jobs[i]["start_time"] - sorted_jobs[i-1]["start_time"]).total_seconds() / 3600
            if delta < 168:  # Ignore gaps > 1 week
                intervals.append(delta)

    avg_interval_hours = sum(intervals) / len(intervals) if intervals else None

    # Calculate interval consistency (coefficient of variation)
    # Low CV = consistent intervals, high CV = variable intervals
    interval_cv = None
    is_consistent = True
    if len(intervals) >= 2 and avg_interval_hours and avg_interval_hours > 0:
        import statistics
        stdev = statistics.stdev(intervals) if len(intervals) > 1 else 0
        interval_cv = stdev / avg_interval_hours  # CV = stdev / mean
        # CV < 0.3 means intervals are within ~30% of mean = consistent
        is_consistent = interval_cv < 0.3

    return {
        "has_data": True,
        "job_count": len(recent_jobs),
        "days_of_data": days_to_analyze,
        "typical_run_hours": typical_hours,
        "hour_distribution": dict(hour_counts),
        "avg_duration_seconds": avg_duration,
        "avg_duration_minutes": avg_duration / 60,
        "avg_interval_hours": avg_interval_hours,
        "interval_cv": interval_cv,  # Coefficient of variation
        "is_consistent": is_consistent,  # True if intervals are reliable
    }


def analyze_peak_hour_overlaps(
    connection_jobs: Dict[str, List[Dict]],
    peak_hours: List[int],
    days_to_analyze: int = 10
) -> Dict[str, Any]:
    """
    Analyze which connections actually run during peak hours and overlap.

    Args:
        connection_jobs: Dict mapping connection_id -> list of processed jobs
        peak_hours: List of peak hours (0-23 UTC)
        days_to_analyze: Number of days to look back

    Returns:
        Dict with overlap analysis and confidence metrics
    """
    # Build timeline of all jobs during peak hours
    peak_hour_jobs = []
    connections_at_peak = defaultdict(int)

    cutoff = datetime.now() - timedelta(days=days_to_analyze)

    for conn_id, jobs in connection_jobs.items():
        for job in jobs:
            start = job["start_time"]
            end = job["end_time"]

            # Skip old jobs
            if start.replace(tzinfo=None) < cutoff.replace(tzinfo=None):
                continue

            # Check if job overlaps with any peak hour
            job_hours = set()
            current = start
            while current < end:
                job_hours.add(current.hour)
                current += timedelta(hours=1)

            if any(h in peak_hours for h in job_hours):
                peak_hour_jobs.append({
                    "connection_id": conn_id,
                    "start_time": start,
                    "end_time": end,
                    "hours_covered": job_hours,
                })
                connections_at_peak[conn_id] += 1

    if not peak_hour_jobs:
        return {
            "has_overlaps": False,
            "peak_connections": [],
            "overlap_events": 0,
            "confidence_score": 0,
        }

    # Sort by start time
    peak_hour_jobs.sort(key=lambda x: x["start_time"])

    # Find actual overlaps (jobs running at the same time)
    overlap_events = []
    for i, job1 in enumerate(peak_hour_jobs):
        for job2 in peak_hour_jobs[i+1:]:
            # Check if jobs overlap in time
            if job1["end_time"] > job2["start_time"] and job1["start_time"] < job2["end_time"]:
                overlap_events.append({
                    "conn1": job1["connection_id"],
                    "conn2": job2["connection_id"],
                    "overlap_start": max(job1["start_time"], job2["start_time"]),
                    "overlap_end": min(job1["end_time"], job2["end_time"]),
                })

    # Find max concurrent at any point
    events = []
    for job in peak_hour_jobs:
        events.append({"time": job["start_time"], "type": "start", "conn": job["connection_id"]})
        events.append({"time": job["end_time"], "type": "end", "conn": job["connection_id"]})

    events.sort(key=lambda x: (x["time"], x["type"] == "start"))  # End before start at same time

    max_concurrent = 0
    current_concurrent = 0
    max_concurrent_connections = set()
    active = set()

    for event in events:
        if event["type"] == "start":
            active.add(event["conn"])
            current_concurrent = len(active)
            if current_concurrent > max_concurrent:
                max_concurrent = current_concurrent
                max_concurrent_connections = active.copy()
        else:
            active.discard(event["conn"])
            current_concurrent = len(active)

    # Calculate confidence score (0-100)
    # Based on: data quantity, pattern consistency, overlap clarity
    total_jobs = sum(len(jobs) for jobs in connection_jobs.values())
    data_quality_score = min(total_jobs / 50 * 40, 40)  # Up to 40 points for 50+ jobs
    overlap_clarity_score = min(len(overlap_events) / 10 * 30, 30)  # Up to 30 points
    connection_coverage_score = min(len(connections_at_peak) / 5 * 30, 30)  # Up to 30 points

    confidence_score = data_quality_score + overlap_clarity_score + connection_coverage_score

    return {
        "has_overlaps": len(overlap_events) > 0,
        "peak_connections": list(connections_at_peak.keys()),
        "peak_connection_counts": dict(connections_at_peak),
        "overlap_events": len(overlap_events),
        "max_concurrent_during_peak": max_concurrent,
        "max_concurrent_connections": list(max_concurrent_connections),
        "total_peak_hour_jobs": len(peak_hour_jobs),
        "confidence_score": round(confidence_score),
    }


def calculate_rescheduling_confidence(
    connection_jobs: Dict[str, List[Dict]],
    peak_hours: List[int],
    quiet_hours: List[int],
    connections_to_reschedule: List[str],
    current_p99: float,
    days_to_analyze: int = 10
) -> Dict[str, Any]:
    """
    Calculate confidence level for rescheduling recommendations based on actual job data.

    Args:
        connection_jobs: Dict mapping connection_id -> list of processed jobs
        peak_hours: List of peak hours (0-23 UTC)
        quiet_hours: List of quiet hours (0-23 UTC)
        connections_to_reschedule: List of connection IDs recommended for rescheduling
        current_p99: Current P99 worker usage
        days_to_analyze: Days of history to analyze

    Returns:
        Dict with confidence analysis and estimated impact
    """
    if not connection_jobs:
        return {
            "confidence": "low",
            "confidence_score": 0,
            "reason": "No job history data available",
            "estimated_reduction": 0,
            "data_quality": "none",
        }

    # Analyze peak hour overlaps
    overlap_analysis = analyze_peak_hour_overlaps(
        connection_jobs, peak_hours, days_to_analyze
    )

    # Count how many of our recommended connections actually run during peak
    recommended_at_peak = [
        c for c in connections_to_reschedule
        if c in overlap_analysis["peak_connections"]
    ]

    # Calculate metrics
    total_jobs = sum(len(jobs) for jobs in connection_jobs.values())
    connections_with_data = len([c for c in connection_jobs if len(connection_jobs[c]) > 0])

    # Determine data quality
    if total_jobs >= 100 and connections_with_data >= 5:
        data_quality = "high"
    elif total_jobs >= 50 and connections_with_data >= 3:
        data_quality = "medium"
    elif total_jobs >= 20:
        data_quality = "low"
    else:
        data_quality = "insufficient"

    # Calculate confidence based on multiple factors
    confidence_score = overlap_analysis["confidence_score"]

    # Adjust based on how many recommended connections are verified at peak
    if connections_to_reschedule:
        verification_rate = len(recommended_at_peak) / len(connections_to_reschedule)
        confidence_score = confidence_score * (0.5 + 0.5 * verification_rate)

    # Determine confidence level
    if confidence_score >= 70 and data_quality in ["high", "medium"]:
        confidence = "high"
    elif confidence_score >= 50 and data_quality != "insufficient":
        confidence = "medium"
    elif confidence_score >= 30:
        confidence = "low-medium"
    else:
        confidence = "low"

    # Estimate reduction based on actual overlap data
    if overlap_analysis["has_overlaps"] and len(recommended_at_peak) > 0:
        # Rough estimate: each rescheduled connection at peak reduces concurrent by ~1
        concurrent_reduction = min(len(recommended_at_peak), overlap_analysis["max_concurrent_during_peak"] - 1)
        # Convert concurrent jobs to workers (rough: 2-5 concurrent = 1 worker depending on type)
        estimated_worker_reduction = concurrent_reduction * 0.4  # Conservative estimate
        estimated_new_p99 = max(current_p99 - estimated_worker_reduction, 0.5)
    else:
        estimated_worker_reduction = 0
        estimated_new_p99 = current_p99

    return {
        "confidence": confidence,
        "confidence_score": round(confidence_score),
        "data_quality": data_quality,
        "total_jobs_analyzed": total_jobs,
        "connections_with_data": connections_with_data,
        "days_of_data": days_to_analyze,
        "peak_hour_overlaps": overlap_analysis["overlap_events"],
        "max_concurrent_at_peak": overlap_analysis["max_concurrent_during_peak"],
        "recommended_connections_verified": len(recommended_at_peak),
        "recommended_connections_total": len(connections_to_reschedule),
        "estimated_worker_reduction": round(estimated_worker_reduction, 1),
        "estimated_new_p99": round(estimated_new_p99, 1),
        "current_p99": current_p99,
        "reduction_percentage": round(
            (current_p99 - estimated_new_p99) / current_p99 * 100
            if current_p99 > 0 else 0
        ),
        "reason": _generate_confidence_reason(
            confidence, data_quality, overlap_analysis, len(recommended_at_peak)
        ),
    }


def _generate_confidence_reason(
    confidence: str,
    data_quality: str,
    overlap_analysis: Dict,
    verified_count: int
) -> str:
    """Generate human-readable reason for confidence level."""
    reasons = []

    if data_quality == "high":
        reasons.append("sufficient job history data")
    elif data_quality == "medium":
        reasons.append("moderate job history data")
    elif data_quality == "low":
        reasons.append("limited job history data")
    else:
        reasons.append("insufficient job history data")

    if overlap_analysis["has_overlaps"]:
        reasons.append(f"{overlap_analysis['overlap_events']} verified overlap events")
    else:
        reasons.append("no overlapping jobs detected during peak hours")

    if verified_count > 0:
        reasons.append(f"{verified_count} recommended connections confirmed at peak")

    return "; ".join(reasons).capitalize() + "."


def fetch_and_analyze_job_history(
    connections: List[Dict[str, Any]],
    peak_hours: List[int],
    quiet_hours: List[int],
    current_p99: float,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    jobs_per_connection: int = 20,  # ~1 week is sufficient if consistent
    days_to_analyze: int = 7,
) -> Dict[str, Any]:
    """
    Main entry point: fetch job history and analyze for confidence.

    Args:
        connections: List of connection dicts with connection_id field
        peak_hours: List of peak hours (0-23 UTC)
        quiet_hours: List of quiet hours (0-23 UTC)
        current_p99: Current P99 worker usage
        client_id: Airbyte API client ID (or from env)
        client_secret: Airbyte API client secret (or from env)
        jobs_per_connection: Jobs to fetch per connection
        days_to_analyze: Days of history to analyze

    Returns:
        Dict with analysis results and confidence
    """
    # Get credentials
    client_id = client_id or os.environ.get("AIRBYTE_CLIENT_ID")
    client_secret = client_secret or os.environ.get("AIRBYTE_CLIENT_SECRET")

    if not client_id or not client_secret:
        return {
            "success": False,
            "error": "Missing Airbyte API credentials",
            "confidence": "low",
            "confidence_score": 0,
        }

    try:
        token = get_access_token(client_id, client_secret)
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to authenticate: {str(e)}",
            "confidence": "low",
            "confidence_score": 0,
        }

    # Fetch job history for each connection
    connection_jobs = {}
    connections_to_reschedule = []

    for conn in connections:
        conn_id = conn.get("connection_id") or conn.get("connectionId")
        if not conn_id:
            continue

        # Track which connections are recommended for rescheduling
        if conn.get("recommended_for_rescheduling"):
            connections_to_reschedule.append(conn_id)

        # Fetch jobs
        raw_jobs = fetch_job_history_for_connection(token, conn_id, limit=jobs_per_connection)

        if raw_jobs:
            processed = process_jobs(raw_jobs)
            if processed:
                connection_jobs[conn_id] = processed

        time.sleep(0.1)  # Rate limiting

    if not connection_jobs:
        return {
            "success": False,
            "error": "No job history retrieved",
            "confidence": "low",
            "confidence_score": 0,
        }

    # Calculate confidence
    confidence_result = calculate_rescheduling_confidence(
        connection_jobs=connection_jobs,
        peak_hours=peak_hours,
        quiet_hours=quiet_hours,
        connections_to_reschedule=connections_to_reschedule,
        current_p99=current_p99,
        days_to_analyze=days_to_analyze,
    )

    # Add per-connection pattern analysis
    connection_patterns = {}
    for conn_id, jobs in connection_jobs.items():
        connection_patterns[conn_id] = analyze_job_patterns(jobs, days_to_analyze)

    return {
        "success": True,
        "connections_analyzed": len(connection_jobs),
        "total_jobs": sum(len(j) for j in connection_jobs.values()),
        "connection_patterns": connection_patterns,
        **confidence_result,
    }


# =============================================================================
# Comprehensive Customer Analysis Functions
# =============================================================================

# Known API connector patterns
API_CONNECTOR_PATTERNS = [
    r'salesforce', r'hubspot', r'stripe', r'shopify', r'zendesk', r'slack',
    r'intercom', r'mailchimp', r'jira', r'asana', r'trello', r'notion',
    r'airtable', r'github', r'gitlab', r'google-ads', r'facebook', r'instagram',
    r'linkedin', r'twitter', r'tiktok', r'snapchat', r'pinterest', r'bing-ads',
    r'amazon-ads', r'google-analytics', r'mixpanel', r'amplitude', r'segment',
    r'braze', r'marketo', r'pardot', r'eloqua', r'freshsales', r'pipedrive',
    r'close', r'copper', r'freshdesk', r'zoho', r'quickbooks', r'xero',
    r'netsuite', r'workday', r'oracle-netsuite', r'twilio', r'sendgrid',
    r'typeform', r'surveymonkey', r'calendly', r'zoom', r'webflow', r'chargebee',
    r'recurly', r'zuora', r'plaid', r'yotpo', r'recharge', r'gorgias',
    r'klaviyo', r'attentive', r'iterable', r'customer\.io', r'appsflyer',
    r'adjust', r'branch', r'firebase', r'onesignal', r'braintree', r'paypal',
    r'square', r'lever', r'greenhouse', r'workable', r'bamboohr', r'gusto',
    r'rippling', r'okta', r'auth0', r'monday', r'clickup', r'wrike',
    r'smartsheet', r'datadog', r'pagerduty', r'opsgenie', r'statuspage',
    r'rest-api', r'http', r'webhook', r'graphql',
]

# Known Database/File connector patterns
DB_CONNECTOR_PATTERNS = [
    r'postgres', r'mysql', r'mssql', r'sql-server', r'oracle', r'mongodb',
    r'dynamodb', r'redis', r'elasticsearch', r'cassandra', r'couchbase',
    r'mariadb', r'cockroach', r'clickhouse', r'snowflake', r'bigquery',
    r'redshift', r'databricks', r'synapse', r'teradata', r'vertica',
    r's3', r'gcs', r'azure-blob', r'sftp', r'ftp', r'file', r'csv', r'json',
    r'parquet', r'avro', r'excel', r'google-sheets', r'dropbox', r'box',
    r'onedrive', r'sharepoint', r'jdbc', r'odbc', r'kafka', r'kinesis',
    r'pubsub', r'rabbitmq', r'sqs', r'firehose', r'delta-lake', r'iceberg',
]


def classify_connector_type(connector_name: str) -> str:
    """
    Classify a connector as API or DATABASE based on name patterns.

    Args:
        connector_name: Name of the connector (source or destination)

    Returns:
        "API", "DATABASE", or "UNKNOWN"
    """
    if not connector_name:
        return "UNKNOWN"

    name_lower = connector_name.lower()

    # Check API patterns
    for pattern in API_CONNECTOR_PATTERNS:
        if re.search(pattern, name_lower):
            return "API"

    # Check Database patterns
    for pattern in DB_CONNECTOR_PATTERNS:
        if re.search(pattern, name_lower):
            return "DATABASE"

    return "UNKNOWN"


def classify_sync_frequency(
    cron_expression: Optional[str] = None,
    connection_name: Optional[str] = None,
    destination_name: Optional[str] = None,
    avg_interval_hours: Optional[float] = None,
    job_count: int = 0,
    days_of_history: int = 7,
) -> str:
    """
    Classify sync frequency as sub_hourly, hourly, or daily.

    Priority order (most reliable first):
    1. Job history interval (actual measured frequency) - MOST RELIABLE
    2. Connection/destination name hints (for externally orchestrated syncs)
    3. Cron expression (only if schedule is set in Airbyte)

    Args:
        cron_expression: Quartz cron expression (6 parts)
        connection_name: Connection name (may contain frequency hints)
        destination_name: Destination name (may contain frequency hints like "- Hourly")
        avg_interval_hours: Average hours between syncs from job history
        job_count: Number of jobs analyzed (for confidence)
        days_of_history: Days of job history analyzed

    Returns:
        "sub_hourly", "hourly", or "daily"
    """
    # PRIORITY 1: Use job history interval (actual measured frequency)
    # This is the most reliable indicator, especially for externally orchestrated syncs
    # Trust even 2 jobs if we have interval data - more data just adds confidence
    if avg_interval_hours is not None and job_count >= 2:
        if avg_interval_hours < 0.75:  # Less than 45 minutes = sub-hourly
            return "sub_hourly"
        elif avg_interval_hours <= 6:
            # 45 mins to 6 hours = hourly category
            return "hourly"
        else:
            # More than 6 hours = daily category (includes weekly/monthly)
            return "daily"

    # PRIORITY 2: Check connection AND destination names for frequency hints
    # This helps when job history is insufficient or for new connections
    names_to_check = []
    if connection_name:
        names_to_check.append(connection_name.lower())
    if destination_name:
        names_to_check.append(destination_name.lower())

    for name_lower in names_to_check:
        # Sub-hourly patterns
        if any(x in name_lower for x in ['15 min', '15min', '30 min', '30min', 'every 15', 'every 30', '- 15m', '- 30m']):
            return "sub_hourly"
        # Hourly patterns (including "- Hourly" in destination names)
        if any(x in name_lower for x in ['hourly', '- hourly', 'every hour', '1 hour', '2 hour', '3 hour', '6 hour', '1h', '2h', '3h', '6h']):
            return "hourly"
        # Daily patterns
        if any(x in name_lower for x in ['daily', '- daily', 'once a day', 'weekly', 'monthly']):
            return "daily"

    # PRIORITY 3: Try to parse cron expression (only if set in Airbyte)
    if cron_expression:
        parts = cron_expression.split()
        if len(parts) >= 3:
            minute, hour = parts[1], parts[2]

            # Check for sub-hourly patterns
            if '*' in minute and '/' in minute:
                # e.g., "*/15" = every 15 minutes
                interval = minute.split('/')[1]
                if interval.isdigit() and int(interval) < 60:
                    return "sub_hourly"

            # Check for hourly patterns
            if hour == '*':
                return "hourly"
            if '/' in hour:
                # e.g., "*/2" = every 2 hours
                return "hourly"

    # FALLBACK: Use job history even with just 1 job interval
    if avg_interval_hours is not None:
        if avg_interval_hours < 1:
            return "sub_hourly"
        elif avg_interval_hours <= 8:
            return "hourly"

    # Default to daily (safest assumption for billing)
    return "daily"


def fetch_workspaces_for_org(token: str, organization_id: str) -> List[Dict]:
    """
    Fetch all workspaces for an organization.

    Note: The API's organizationId filter should work, but the response may not
    include organizationId in each workspace object. We trust the API filter.
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    params = {
        "organizationId": organization_id,
        "limit": 100
    }

    r = requests.get(WORKSPACES_URL, headers=headers, params=params, timeout=30)
    r.raise_for_status()

    workspaces = r.json().get("data", [])

    # Try to filter by organizationId if present in response, otherwise trust API filter
    filtered = [ws for ws in workspaces if ws.get("organizationId") == organization_id]

    # If filter removed all workspaces but we got results, the API filtered but didn't include orgId
    if not filtered and workspaces:
        # Trust the API filter - it should have filtered by organizationId
        return workspaces

    return filtered


def fetch_all_connections(token: str, workspace_id: str) -> List[Dict]:
    """Fetch all connections for a workspace."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    connections = []
    offset = 0
    limit = 100

    while True:
        params = {
            "workspaceIds": workspace_id,
            "limit": limit,
            "offset": offset
        }

        r = requests.get(CONNECTIONS_URL, headers=headers, params=params, timeout=30)
        r.raise_for_status()

        page = r.json().get("data", [])
        connections.extend(page)

        if len(page) < limit:
            break
        offset += limit
        time.sleep(0.1)

    return connections


def fetch_source_details(token: str, source_id: str) -> Optional[Dict]:
    """Fetch details for a specific source."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    try:
        r = requests.get(f"{SOURCES_URL}/{source_id}", headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None


def analyze_customer_job_history(
    organization_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    jobs_per_connection: int = 20,  # ~1 week of history is sufficient
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Comprehensive job history analysis for a customer.

    This function provides all the data needed for worker analysis:
    - Total number of pipelines
    - API vs Database source breakdown
    - Sync frequency breakdown (sub-hourly, hourly, daily)
    - Average sync duration with statistics

    Args:
        organization_id: Airbyte Cloud organization ID (fetches all workspaces)
        workspace_id: Single workspace ID (alternative to org_id)
        client_id: API client ID (auto-loads from ~/.env if not provided)
        client_secret: API client secret (auto-loads from ~/.env if not provided)
        jobs_per_connection: Number of recent jobs to fetch per connection
        verbose: Print progress messages

    Returns:
        Dictionary with comprehensive analysis results
    """
    # Get credentials
    client_id, client_secret = get_airbyte_credentials(client_id, client_secret)

    # Authenticate
    if verbose:
        print("Authenticating with Airbyte Cloud API...")
    token = get_access_token(client_id, client_secret)

    # Get workspace IDs
    workspace_ids = []
    if organization_id:
        # First, check if we have confirmed workspace mappings for this org
        known_workspace_ids = get_workspace_ids_for_organization(organization_id)
        if known_workspace_ids:
            if verbose:
                print(f"Using confirmed workspace mapping for organization {organization_id[:8]}...")
            workspace_ids = known_workspace_ids
        else:
            # Fall back to API lookup
            if verbose:
                print(f"Fetching workspaces for organization {organization_id[:8]}...")
            workspaces = fetch_workspaces_for_org(token, organization_id)
            workspace_ids = [ws.get("workspaceId") for ws in workspaces if ws.get("workspaceId")]

        if verbose:
            print(f"Found {len(workspace_ids)} workspace(s)")
    elif workspace_id:
        workspace_ids = [workspace_id]
    else:
        raise ValueError("Either organization_id or workspace_id must be provided")

    # Fetch all connections across workspaces
    all_connections = []
    for ws_id in workspace_ids:
        if verbose:
            print(f"Fetching connections for workspace {ws_id[:8]}...")
        connections = fetch_all_connections(token, ws_id)
        for conn in connections:
            conn["_workspace_id"] = ws_id
        all_connections.extend(connections)

    if verbose:
        print(f"Total connections: {len(all_connections)}")

    # Analyze each connection
    connection_data = []
    all_durations = []
    source_types = {"API": 0, "DATABASE": 0, "UNKNOWN": 0}
    sync_frequencies = {"sub_hourly": 0, "hourly": 0, "daily": 0}
    total_jobs_analyzed = 0

    for idx, conn in enumerate(all_connections, 1):
        conn_id = conn.get("connectionId", "")
        conn_name = conn.get("name", "Unknown")

        if verbose and idx % 10 == 0:
            print(f"Processing connection {idx}/{len(all_connections)}...")

        # Get source and destination names from connection name
        source_name = ""
        dest_name = ""
        if " → " in conn_name or " -> " in conn_name:
            parts = conn_name.split("→" if "→" in conn_name else "->")
            source_name = parts[0].strip() if len(parts) > 0 else ""
            dest_name = parts[1].strip() if len(parts) > 1 else ""

        source_type = classify_connector_type(source_name)
        if source_type == "UNKNOWN":
            # Try to get source details
            source_id = conn.get("sourceId")
            if source_id:
                source_details = fetch_source_details(token, source_id)
                if source_details:
                    source_def = source_details.get("sourceType", "")
                    source_type = classify_connector_type(source_def)

        source_types[source_type] += 1

        # Get schedule info
        schedule_data = conn.get("scheduleData", {}) or {}
        cron = schedule_data.get("cron", {}) or {}
        cron_expression = cron.get("cronExpression")

        if not cron_expression:
            schedule = conn.get("schedule", {}) or {}
            cron_expression = schedule.get("cronExpression")

        # Fetch job history
        jobs = fetch_job_history_for_connection(token, conn_id, limit=jobs_per_connection)
        processed_jobs = process_jobs(jobs)
        total_jobs_analyzed += len(processed_jobs)

        # Calculate job statistics
        conn_durations = []
        start_hours = []
        job_intervals = []

        sorted_jobs = sorted(processed_jobs, key=lambda x: x["start_time"])

        for i, job in enumerate(sorted_jobs):
            duration_secs = job.get("duration_seconds", 0)
            if duration_secs > 0:
                conn_durations.append(duration_secs / 60.0)  # Convert to minutes
                all_durations.append(duration_secs / 60.0)

            start_hours.append(job["start_time"].hour)

            # Calculate interval to previous job
            if i > 0:
                prev_job = sorted_jobs[i - 1]
                interval_hours = (job["start_time"] - prev_job["start_time"]).total_seconds() / 3600
                if interval_hours < 168:  # Ignore gaps > 1 week
                    job_intervals.append(interval_hours)

        # Determine sync frequency from job history (most reliable for externally orchestrated syncs)
        avg_interval = None
        if job_intervals:
            avg_interval = sum(job_intervals) / len(job_intervals)

        frequency = classify_sync_frequency(
            cron_expression=cron_expression,
            connection_name=conn_name,
            destination_name=dest_name,  # Check dest name too (e.g., "Datalake Ingestion - Hourly")
            avg_interval_hours=avg_interval,
            job_count=len(processed_jobs),
            days_of_history=jobs_per_connection // 3,  # Rough estimate assuming ~3 jobs/day for daily syncs
        )
        sync_frequencies[frequency] += 1

        # Store connection data
        connection_data.append({
            "connection_id": conn_id,
            "connection_name": conn_name,
            "source_name": source_name,
            "destination_name": dest_name,
            "source_type": source_type,
            "sync_frequency": frequency,
            "cron_expression": cron_expression,
            "jobs_fetched": len(processed_jobs),
            "avg_duration_minutes": sum(conn_durations) / len(conn_durations) if conn_durations else 0,
            "avg_interval_hours": avg_interval,
            "typical_run_hours": list(set(start_hours))[:5] if start_hours else [],
        })

        time.sleep(0.05)  # Rate limiting

    # Calculate overall statistics
    duration_stats = {}
    if all_durations:
        duration_stats = {
            "count": len(all_durations),
            "mean": sum(all_durations) / len(all_durations),
            "median": statistics.median(all_durations),
            "min": min(all_durations),
            "max": max(all_durations),
        }
        if len(all_durations) >= 2:
            duration_stats["stdev"] = statistics.stdev(all_durations)
        # Calculate percentiles
        sorted_durations = sorted(all_durations)
        n = len(sorted_durations)
        duration_stats["p90"] = sorted_durations[int(n * 0.9)] if n > 0 else 0
        duration_stats["p95"] = sorted_durations[int(n * 0.95)] if n > 0 else 0
        duration_stats["p99"] = sorted_durations[int(n * 0.99)] if n > 0 else 0

    return {
        "success": True,
        "analysis_timestamp": datetime.utcnow().isoformat(),
        "organization_id": organization_id,
        "workspace_ids": workspace_ids,

        # Summary statistics
        "total_pipelines": len(all_connections),

        "source_type_breakdown": {
            "api_sources": source_types["API"],
            "database_sources": source_types["DATABASE"],
            "unknown_sources": source_types["UNKNOWN"],
        },

        "sync_frequency_breakdown": {
            "sub_hourly": sync_frequencies["sub_hourly"],
            "hourly": sync_frequencies["hourly"],
            "daily": sync_frequencies["daily"],
        },

        "sync_duration_stats": {
            "avg_sync_duration_minutes": duration_stats.get("mean", 0),
            "median_sync_duration_minutes": duration_stats.get("median", 0),
            "p90_sync_duration_minutes": duration_stats.get("p90", 0),
            "min_sync_duration_minutes": duration_stats.get("min", 0),
            "max_sync_duration_minutes": duration_stats.get("max", 0),
        },

        "job_statistics": {
            "total_jobs_analyzed": total_jobs_analyzed,
            "jobs_per_connection": jobs_per_connection,
            "duration_stats": duration_stats,
        },

        # Detailed connection data
        "connections": connection_data,
    }


def print_customer_summary(results: Dict[str, Any]) -> str:
    """
    Format customer analysis results as a readable summary.

    Args:
        results: Results from analyze_customer_job_history()

    Returns:
        Formatted summary string
    """
    if not results.get("success"):
        return f"Analysis failed: {results.get('error', 'Unknown error')}"

    lines = []
    lines.append("\n" + "=" * 60)
    lines.append("CUSTOMER DATA WORKER ANALYSIS")
    lines.append("=" * 60)

    lines.append(f"\nTotal number of pipelines: {results['total_pipelines']}")

    lines.append("\nNumber of pipelines extracting data from:")
    src = results['source_type_breakdown']
    lines.append(f"  API sources: {src['api_sources']}")
    lines.append(f"  Database/file sources: {src['database_sources']}")
    if src['unknown_sources'] > 0:
        lines.append(f"  Unknown sources: {src['unknown_sources']}")

    lines.append("\nNumber of pipelines running:")
    freq = results['sync_frequency_breakdown']
    lines.append(f"  Sub-hourly (e.g., every 15 min): {freq['sub_hourly']}")
    lines.append(f"  Hourly (or every few hours): {freq['hourly']}")
    lines.append(f"  Daily (or less frequently): {freq['daily']}")

    dur = results['sync_duration_stats']
    lines.append(f"\nAverage sync length: {dur['avg_sync_duration_minutes']:.1f} mins")
    lines.append(f"  Median: {dur['median_sync_duration_minutes']:.1f} mins")
    lines.append(f"  90th percentile: {dur['p90_sync_duration_minutes']:.1f} mins")

    jobs = results['job_statistics']
    lines.append(f"\n(Based on {jobs['total_jobs_analyzed']} job executions)")

    lines.append("\n" + "=" * 60)

    return "\n".join(lines)
