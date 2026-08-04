#!/usr/bin/env python3
"""
Job Overlap Analyzer - Core Module for Worker Calculation

This module analyzes ACTUAL job overlaps to calculate peak concurrent workers.
It replaces the incorrect method of counting total connections.

CORRECT METHOD (this module):
    1. For each job, record start_time and end_time
    2. For each minute, count how many API and DB jobs are running simultaneously
    3. Find peak concurrent API and DB jobs per hour
    4. Workers = (Peak Concurrent API / 5) + (Peak Concurrent DB / 2)
    5. P99 Workers = 99th percentile of hourly workers

WRONG METHOD (old approach):
    - Count total connections, divide by capacity factor
    - Assumes all connections run simultaneously
    - Grossly overestimates worker requirements

Usage:
    from src.job_overlap_analyzer import (
        analyze_job_overlaps,
        calculate_peak_workers_from_jobs,
    )

    # From job data
    result = analyze_job_overlaps(jobs, connections)

    # Get P99 workers
    p99_workers, peak_hour = calculate_peak_workers_from_jobs(jobs, connections)
"""

from datetime import datetime, timedelta
from collections import defaultdict
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
import math

# Import config for connector classification
try:
    from src.config import (
        DB_CONNECTIONS_PER_WORKER,
        API_CONNECTIONS_PER_WORKER,
        DATABASE_CONNECTORS,
        API_CONNECTORS,
    )
except ImportError:
    try:
        from config import (
            DB_CONNECTIONS_PER_WORKER,
            API_CONNECTIONS_PER_WORKER,
            DATABASE_CONNECTORS,
            API_CONNECTORS,
        )
    except ImportError:
        # Fallback defaults
        DB_CONNECTIONS_PER_WORKER = 2
        API_CONNECTIONS_PER_WORKER = 5
        DATABASE_CONNECTORS = [
            'postgres', 'mysql', 'mssql', 'oracle', 'snowflake', 'bigquery',
            'redshift', 'mongodb', 'mariadb', 'cockroach', 'supabase', 's3',
            'gcs', 'azure-blob', 'sftp', 'ftp', 'file'
        ]
        API_CONNECTORS = [
            'stripe', 'salesforce', 'hubspot', 'shopify', 'google-sheets',
            'github', 'slack', 'zendesk', 'intercom', 'mixpanel', 'amplitude'
        ]


@dataclass
class JobInterval:
    """Represents a job's running interval."""
    connection_id: str
    connection_name: str
    start_time: datetime
    end_time: datetime
    duration_minutes: float
    connection_type: str  # 'API' or 'DATABASE'


@dataclass
class HourlyConcurrency:
    """Concurrency stats for a single hour."""
    hour: int
    api_peak: int = 0
    db_peak: int = 0
    total_peak: int = 0
    api_jobs_started: int = 0
    db_jobs_started: int = 0
    workers_needed: float = 0.0


@dataclass
class OverlapAnalysisResult:
    """Result of job overlap analysis."""
    # Analysis metadata
    analysis_date: datetime = field(default_factory=datetime.now)
    analysis_period_days: int = 10

    # Connection totals (for reference, NOT used for worker calc)
    total_connections: int = 0
    api_connections: int = 0
    db_connections: int = 0

    # ACTUAL concurrency (used for worker calculation)
    peak_concurrent_api: int = 0
    peak_concurrent_db: int = 0
    peak_total_concurrent: int = 0
    peak_hour: int = 0

    # Worker calculation
    p99_workers: float = 0.0
    avg_workers: float = 0.0
    max_workers: float = 0.0

    # Hourly breakdown
    hourly_stats: Dict[int, HourlyConcurrency] = field(default_factory=dict)

    # Job details
    total_jobs_analyzed: int = 0
    job_intervals: List[JobInterval] = field(default_factory=list)


def classify_connector(source_name: str) -> str:
    """
    Classify a connector as 'DATABASE' or 'API' based on its name.

    Args:
        source_name: The connector or source name

    Returns:
        'DATABASE' or 'API'
    """
    name_lower = (source_name or "").lower()

    # Check for database patterns
    for db_pattern in DATABASE_CONNECTORS:
        if db_pattern in name_lower:
            return "DATABASE"

    # Check for API patterns
    for api_pattern in API_CONNECTORS:
        if api_pattern in name_lower:
            return "API"

    # Default to API for unknown (lighter weight)
    return "API"


def parse_iso_datetime(dt_string: str) -> Optional[datetime]:
    """Parse ISO datetime string to datetime object."""
    if not dt_string:
        return None
    try:
        dt_string = dt_string.replace('Z', '+00:00')
        if '.' in dt_string:
            parts = dt_string.split('.')
            if '+' in parts[1]:
                micro, tz = parts[1].split('+')
                parts[1] = micro[:6] + '+' + tz
            elif len(parts[1]) > 6:
                parts[1] = parts[1][:6]
            dt_string = '.'.join(parts)
        return datetime.fromisoformat(dt_string.replace('+00:00', ''))
    except (ValueError, AttributeError):
        return None


def calculate_workers(api_concurrent: int, db_concurrent: int) -> float:
    """
    Calculate workers needed for given concurrent job counts.

    Formula: (Concurrent API / 5) + (Concurrent DB / 2)

    Args:
        api_concurrent: Number of concurrent API jobs
        db_concurrent: Number of concurrent database jobs

    Returns:
        Workers needed (float, not rounded)
    """
    return (api_concurrent / API_CONNECTIONS_PER_WORKER) + (db_concurrent / DB_CONNECTIONS_PER_WORKER)


def build_job_intervals(
    jobs: List[Dict[str, Any]],
    connections: Optional[Dict[str, Dict[str, Any]]] = None,
    default_duration_minutes: float = 5.0
) -> List[JobInterval]:
    """
    Build a list of JobInterval objects from raw job data.

    Args:
        jobs: List of job dicts with keys:
            - connection_id: str
            - start_time or start: str (ISO datetime)
            - end_time or end: str (ISO datetime, optional)
            - duration or duration_minutes: float (optional, used if end_time not provided)
        connections: Optional dict mapping connection_id to connection info with 'type' key
        default_duration_minutes: Default duration if not specified (default: 5 min)

    Returns:
        List of JobInterval objects
    """
    intervals = []

    for job in jobs:
        conn_id = job.get('connection_id') or job.get('connectionId') or ''
        conn_name = job.get('connection_name') or job.get('name') or conn_id

        # Parse start time
        start_str = job.get('start_time') or job.get('start') or job.get('startTime')
        start_time = parse_iso_datetime(start_str) if isinstance(start_str, str) else start_str

        if not start_time:
            continue

        # Parse end time or calculate from duration
        end_str = job.get('end_time') or job.get('end') or job.get('endTime')
        end_time = parse_iso_datetime(end_str) if isinstance(end_str, str) else end_str

        if not end_time:
            duration = job.get('duration') or job.get('duration_minutes') or default_duration_minutes
            end_time = start_time + timedelta(minutes=float(duration))

        duration_minutes = (end_time - start_time).total_seconds() / 60

        # Determine connection type
        if connections and conn_id in connections:
            conn_type = connections[conn_id].get('type', 'API')
        else:
            # Try to classify from job data
            source_type = job.get('source_type') or job.get('sourceType') or ''
            conn_type = classify_connector(source_type or conn_name)

        intervals.append(JobInterval(
            connection_id=conn_id,
            connection_name=conn_name,
            start_time=start_time,
            end_time=end_time,
            duration_minutes=duration_minutes,
            connection_type=conn_type
        ))

    return intervals


def analyze_concurrency_by_hour(
    job_intervals: List[JobInterval],
    analysis_date: Optional[datetime] = None
) -> Dict[int, HourlyConcurrency]:
    """
    Analyze job concurrency by hour.

    For each hour (0-23), finds the peak number of concurrent API and DB jobs
    by checking every minute within that hour.

    Args:
        job_intervals: List of JobInterval objects
        analysis_date: Date to analyze (default: most recent date in jobs)

    Returns:
        Dict mapping hour (0-23) to HourlyConcurrency stats
    """
    # Determine analysis date
    if analysis_date is None and job_intervals:
        analysis_date = max(interval.start_time for interval in job_intervals).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif analysis_date is None:
        analysis_date = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    # Initialize hourly stats
    hourly_stats = {}
    for hour in range(24):
        hourly_stats[hour] = HourlyConcurrency(hour=hour)

    # For each hour, analyze every minute to find peak concurrency
    for hour in range(24):
        max_api = 0
        max_db = 0
        api_jobs_started = 0
        db_jobs_started = 0

        # Check every minute in this hour
        for minute in range(60):
            check_time = analysis_date.replace(hour=hour, minute=minute, second=0)

            api_concurrent = 0
            db_concurrent = 0

            for interval in job_intervals:
                # Check if job is running at this time
                if interval.start_time <= check_time < interval.end_time:
                    if interval.connection_type == 'API':
                        api_concurrent += 1
                    else:
                        db_concurrent += 1

            max_api = max(max_api, api_concurrent)
            max_db = max(max_db, db_concurrent)

        # Count jobs that started in this hour (for reference)
        for interval in job_intervals:
            if (interval.start_time.date() == analysis_date.date() and
                interval.start_time.hour == hour):
                if interval.connection_type == 'API':
                    api_jobs_started += 1
                else:
                    db_jobs_started += 1

        # Calculate workers for this hour's peak
        workers = calculate_workers(max_api, max_db)

        hourly_stats[hour] = HourlyConcurrency(
            hour=hour,
            api_peak=max_api,
            db_peak=max_db,
            total_peak=max_api + max_db,
            api_jobs_started=api_jobs_started,
            db_jobs_started=db_jobs_started,
            workers_needed=workers
        )

    return hourly_stats


def calculate_p99_workers(hourly_stats: Dict[int, HourlyConcurrency]) -> Tuple[float, int, float, float]:
    """
    Calculate P99, average, and max workers from hourly stats.

    Args:
        hourly_stats: Dict mapping hour to HourlyConcurrency

    Returns:
        Tuple of (p99_workers, peak_hour, avg_workers, max_workers)
    """
    if not hourly_stats:
        return 0.0, 0, 0.0, 0.0

    workers_list = [(h, stats.workers_needed) for h, stats in hourly_stats.items()]
    workers_list.sort(key=lambda x: x[1], reverse=True)

    # P99 for 24 hours is essentially the peak (top 1%)
    p99_workers = workers_list[0][1]
    peak_hour = workers_list[0][0]

    # Average and max
    all_workers = [w for _, w in workers_list]
    avg_workers = sum(all_workers) / len(all_workers)
    max_workers = max(all_workers)

    return p99_workers, peak_hour, avg_workers, max_workers


def analyze_job_overlaps(
    jobs: List[Dict[str, Any]],
    connections: Optional[Dict[str, Dict[str, Any]]] = None,
    analysis_date: Optional[datetime] = None,
    analysis_period_days: int = 10,
    default_duration_minutes: float = 5.0
) -> OverlapAnalysisResult:
    """
    Analyze job overlaps to calculate peak concurrent workers.

    This is the main entry point for job overlap analysis.

    Args:
        jobs: List of job dicts (see build_job_intervals for format)
        connections: Optional dict mapping connection_id to connection info
        analysis_date: Date to analyze (default: most recent date in jobs)
        analysis_period_days: Analysis period for metadata
        default_duration_minutes: Default job duration if not specified

    Returns:
        OverlapAnalysisResult with all analysis data
    """
    result = OverlapAnalysisResult(
        analysis_period_days=analysis_period_days
    )

    if not jobs:
        return result

    # Build job intervals
    job_intervals = build_job_intervals(jobs, connections, default_duration_minutes)
    result.job_intervals = job_intervals
    result.total_jobs_analyzed = len(job_intervals)

    # Count connection types (for reference only)
    if connections:
        result.total_connections = len(connections)
        result.api_connections = sum(1 for c in connections.values() if c.get('type') == 'API')
        result.db_connections = sum(1 for c in connections.values() if c.get('type') == 'DATABASE')

    # Analyze hourly concurrency
    result.hourly_stats = analyze_concurrency_by_hour(job_intervals, analysis_date)

    # Calculate P99 workers
    p99, peak_hour, avg, max_w = calculate_p99_workers(result.hourly_stats)
    result.p99_workers = p99
    result.peak_hour = peak_hour
    result.avg_workers = avg
    result.max_workers = max_w

    # Get peak concurrency details
    if peak_hour in result.hourly_stats:
        peak_stats = result.hourly_stats[peak_hour]
        result.peak_concurrent_api = peak_stats.api_peak
        result.peak_concurrent_db = peak_stats.db_peak
        result.peak_total_concurrent = peak_stats.total_peak

    return result


def calculate_peak_workers_from_jobs(
    jobs: List[Dict[str, Any]],
    connections: Optional[Dict[str, Dict[str, Any]]] = None
) -> Tuple[float, int]:
    """
    Convenience function to calculate P99 workers from job data.

    Args:
        jobs: List of job dicts
        connections: Optional connection info dict

    Returns:
        Tuple of (p99_workers, peak_hour)
    """
    result = analyze_job_overlaps(jobs, connections)
    return result.p99_workers, result.peak_hour


def format_hourly_summary(hourly_stats: Dict[int, HourlyConcurrency], contracted_workers: int = 1) -> str:
    """
    Format hourly stats as a readable summary string.

    Args:
        hourly_stats: Dict from analyze_concurrency_by_hour
        contracted_workers: Contracted worker count for comparison

    Returns:
        Formatted string summary
    """
    lines = [
        f"{'Hour':<8} {'API Peak':<10} {'DB Peak':<10} {'Total':<10} {'Workers':<10} {'Status':<12}",
        "-" * 60
    ]

    for hour in range(24):
        stats = hourly_stats.get(hour, HourlyConcurrency(hour=hour))
        status = ""
        if stats.workers_needed > contracted_workers:
            status = "OVER"
        elif stats.workers_needed >= contracted_workers * 0.85:
            status = "NEAR CAP"
        elif stats.workers_needed > 0:
            status = "OK"

        lines.append(
            f"{hour:02d}:00    {stats.api_peak:<10} {stats.db_peak:<10} "
            f"{stats.total_peak:<10} {stats.workers_needed:<10.1f} {status:<12}"
        )

    return "\n".join(lines)


# Alias for backwards compatibility
def analyze_workspace_job_overlaps(
    org_id: str,
    jobs_data: List[Dict],
    connections_data: List[Dict],
    contracted_workers: int = 1
) -> Dict[str, Any]:
    """
    Analyze workspace job overlaps and return results dict.

    This function provides a dict-based interface for compatibility
    with existing code.

    Args:
        org_id: Organization ID (for metadata)
        jobs_data: List of job dicts from Airbyte API
        connections_data: List of connection dicts from Airbyte API
        contracted_workers: Number of contracted workers

    Returns:
        Dict with analysis results
    """
    # Build connections dict
    connections = {}
    for conn in connections_data:
        conn_id = conn.get('connectionId') or conn.get('id', '')
        conn_name = conn.get('name', '')
        source_type = conn.get('source', {}).get('sourceName', '') if isinstance(conn.get('source'), dict) else ''

        connections[conn_id] = {
            'name': conn_name,
            'type': classify_connector(source_type or conn_name),
            'status': conn.get('status', 'unknown')
        }

    # Analyze job overlaps
    result = analyze_job_overlaps(jobs_data, connections)

    # Calculate utilization
    utilization = (result.p99_workers / contracted_workers * 100) if contracted_workers > 0 else 0

    return {
        'org_id': org_id,
        'analysis_method': 'job_overlap',
        'total_connections': result.total_connections,
        'api_connections': result.api_connections,
        'db_connections': result.db_connections,
        'total_jobs_analyzed': result.total_jobs_analyzed,
        'peak_hour': result.peak_hour,
        'peak_concurrent_api': result.peak_concurrent_api,
        'peak_concurrent_db': result.peak_concurrent_db,
        'peak_total_concurrent': result.peak_total_concurrent,
        'p99_workers': result.p99_workers,
        'avg_workers': result.avg_workers,
        'max_workers': result.max_workers,
        'contracted_workers': contracted_workers,
        'utilization_pct': utilization,
        'hourly_stats': {
            h: {
                'api_peak': s.api_peak,
                'db_peak': s.db_peak,
                'total_peak': s.total_peak,
                'workers_needed': s.workers_needed
            }
            for h, s in result.hourly_stats.items()
        }
    }
