#!/usr/bin/env python3
"""
Mode 2B: Analyze OSS/Cloud Export

Analyzes the JSON export from OSS/Cloud users and provides worker estimation.

IMPORTANT: This module now supports TWO analysis methods:

1. JOB OVERLAP ANALYSIS (preferred, more accurate):
   - If the export contains job_history data with start/end times
   - Calculates ACTUAL concurrent jobs at each minute
   - Workers = (Peak Concurrent API / 5) + (Peak Concurrent DB / 2)

2. ESTIMATION (fallback, less accurate):
   - If the export only has connection counts and schedules
   - Uses statistical modeling based on sync frequencies
   - May overestimate worker needs
"""

import json
import sys
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from worker_calculator import WorkerCalculator

# Import job overlap analyzer for accurate calculations
try:
    from job_overlap_analyzer import (
        analyze_job_overlaps,
        calculate_peak_workers_from_jobs,
        format_hourly_summary,
        classify_connector,
    )
    HAS_JOB_OVERLAP_ANALYZER = True
except ImportError:
    HAS_JOB_OVERLAP_ANALYZER = False


def analyze_with_job_overlap(
    jobs: List[Dict],
    connections: Dict[str, Dict],
    workspace_id: str = "Unknown"
) -> Dict[str, Any]:
    """
    Analyze job overlaps to calculate ACTUAL peak concurrent workers.

    This is the CORRECT method - it looks at when jobs actually run,
    not just how many connections exist.

    Args:
        jobs: List of job dicts with start_time, end_time/duration, connection_id
        connections: Dict mapping connection_id to connection info with 'type'
        workspace_id: Workspace ID for metadata

    Returns:
        Analysis result dict
    """
    if not HAS_JOB_OVERLAP_ANALYZER:
        print("⚠️  Job overlap analyzer not available, falling back to estimation")
        return None

    print("\n" + "=" * 80)
    print("📊 ANALYZING JOB OVERLAPS (Accurate Method)")
    print("=" * 80 + "\n")

    # Run analysis
    result = analyze_job_overlaps(jobs, connections)

    print(f"Jobs Analyzed: {result.total_jobs_analyzed}")
    print(f"Analysis Date: {result.analysis_date.strftime('%Y-%m-%d')}")
    print()

    # Show hourly breakdown
    print("Hourly Concurrency Analysis (Peak concurrent jobs per hour):")
    print("-" * 60)
    print(format_hourly_summary(result.hourly_stats, contracted_workers=1))
    print()

    print("=" * 80)
    print("🎯 WORKER REQUIREMENTS (Based on Actual Job Overlaps)")
    print("=" * 80 + "\n")

    print(f"P99 Workers Needed: {result.p99_workers:.1f}")
    print(f"Peak Hour: {result.peak_hour:02d}:00 UTC")
    print(f"  - Peak Concurrent API: {result.peak_concurrent_api}")
    print(f"  - Peak Concurrent DB: {result.peak_concurrent_db}")
    print(f"  - Total Concurrent: {result.peak_total_concurrent}")
    print()
    print("Calculation:")
    print(f"  ({result.peak_concurrent_api} API / 5) + ({result.peak_concurrent_db} DB / 2)")
    print(f"  = {result.peak_concurrent_api/5:.1f} + {result.peak_concurrent_db/2:.1f}")
    print(f"  = {result.p99_workers:.1f} workers")
    print()
    print("✅ Analysis complete - based on ACTUAL job start/end times\n")

    return {
        'mode': '2B_job_overlap_analysis',
        'timestamp': datetime.utcnow().isoformat(),
        'workspace_id': workspace_id,
        'workers_required': result.p99_workers,
        'workers_required_rounded': int(result.p99_workers) + (1 if result.p99_workers % 1 > 0 else 0),
        'analysis_method': 'job_overlap',
        'data_source': 'actual_job_times',
        'confidence': 'high',
        'peak_hour': result.peak_hour,
        'peak_concurrent_api': result.peak_concurrent_api,
        'peak_concurrent_db': result.peak_concurrent_db,
        'peak_total_concurrent': result.peak_total_concurrent,
        'total_jobs_analyzed': result.total_jobs_analyzed,
        'hourly_stats': {
            h: {
                'api_peak': s.api_peak,
                'db_peak': s.db_peak,
                'workers_needed': s.workers_needed
            }
            for h, s in result.hourly_stats.items()
        }
    }


def analyze_cloud_export(data: dict) -> dict:
    """
    Analyze export from Airbyte Cloud workspace.

    If the export contains job_history data, uses job overlap analysis (accurate).
    Otherwise, uses the pre-calculated values from the export.

    Args:
        data: Export data from Cloud workspace

    Returns:
        Analysis result
    """
    print("\n" + "=" * 80)
    print("📊 MODE 2B: CLOUD WORKSPACE ANALYSIS")
    print("=" * 80 + "\n")

    workspace_id = data.get('workspace_id', 'Unknown')
    print(f"Workspace: {workspace_id}")
    print(f"Export Date: {data.get('export_timestamp', 'Unknown')}")
    print(f"Total Connections: {data['total_connections']}")
    print(f"API Connections: {data.get('api_connections', 0)}")
    print(f"DB/File Connections: {data.get('db_file_connections', 0)}")

    # Check if export contains job history for accurate analysis
    job_history = data.get('job_history', [])
    connections = data.get('connections', {})

    if job_history and HAS_JOB_OVERLAP_ANALYZER:
        print("\n📈 Job history found - using job overlap analysis for accuracy")
        result = analyze_with_job_overlap(job_history, connections, workspace_id)
        if result:
            return result

    # Fallback to pre-calculated values (if available)
    print("\n" + "=" * 80)
    print("🎯 WORKER REQUIREMENTS")
    print("=" * 80 + "\n")

    workers_required = data.get('workers_required', 0)
    peak = data.get('peak_concurrency', {})

    print(f"Workers Required: {workers_required} Enterprise Data Workers")
    print(f"Peak API Concurrent: {peak.get('api', 0)}")
    print(f"Peak DB/File Concurrent: {peak.get('db', 0)}")

    print("\n✅ Analysis complete - based on actual usage data from last 5 jobs per connection\n")

    return {
        'mode': '2B_cloud_workspace',
        'timestamp': datetime.utcnow().isoformat(),
        'workspace_id': workspace_id,
        'workers_required': workers_required,
        'data_source': 'actual_usage',
        'confidence': 'high'
    }


def analyze_oss_export(data: dict) -> dict:
    """
    Analyze export from OSS Airbyte instance.

    If the export contains job_history data, uses job overlap analysis (accurate).
    Otherwise, falls back to estimation based on connection counts and schedules.

    Args:
        data: Export data from OSS instance

    Returns:
        Analysis result
    """
    print("\n" + "=" * 80)
    print("📊 MODE 2B: OSS WORKSPACE ANALYSIS")
    print("=" * 80 + "\n")

    print(f"Export Date: {data.get('export_timestamp', 'Unknown')}")
    print(f"Total Connections: {data['total_connections']}")

    # Check if export contains job history for accurate analysis
    job_history = data.get('job_history', [])
    connections_list = data.get('connections', [])

    if job_history and HAS_JOB_OVERLAP_ANALYZER:
        print("\n📈 Job history found - using job overlap analysis for accuracy")

        # Build connections dict from list
        connections = {}
        for conn in connections_list:
            conn_id = conn.get('connection_id') or conn.get('connectionId') or conn.get('id', '')
            source_type = conn.get('source_type') or conn.get('sourceType') or ''
            conn_name = conn.get('name', '')

            # Classify connector
            if conn.get('type'):
                conn_type = conn.get('type').upper()
                if conn_type not in ['API', 'DATABASE']:
                    conn_type = classify_connector(source_type or conn_name)
            else:
                conn_type = classify_connector(source_type or conn_name)

            connections[conn_id] = {
                'name': conn_name,
                'type': conn_type,
                'source_type': source_type
            }

        result = analyze_with_job_overlap(job_history, connections, data.get('workspace_id', 'OSS'))
        if result:
            return result

    # Fallback: Check schedule breakdown for estimation
    schedule_breakdown = data.get('schedule_breakdown', {})

    print(f"\nSchedule Breakdown:")
    for schedule_type, count in schedule_breakdown.items():
        print(f"  • {schedule_type}: {count}")

    # Check if manual classification is needed
    if data.get('needs_manual_classification'):
        print("\n" + "=" * 80)
        print("⚠️  MANUAL CLASSIFICATION REQUIRED")
        print("=" * 80 + "\n")

        print("OSS export detected. To complete estimation, please provide:")
        print("  1. How many connections are Database/File connectors? (number)")
        print("  2. How many connections are API connectors? (number)")
        print("  3. Average sync duration in minutes (estimate)")
        print()
        print("⚠️  NOTE: This uses estimation, which may overestimate worker needs.")
        print("   For accurate results, include job_history in your export with")
        print("   start_time and end_time/duration for each job.")

        # For interactive mode
        try:
            db_count = int(input("\nDatabase/File connections: "))
            api_count = int(input("API connections: "))
            sync_duration = float(input("Average sync duration (minutes): "))

            total = data['total_connections']
            db_percent = (db_count / total) * 100
            api_percent = (api_count / total) * 100

            # Map schedule types to frequency percentages
            sub_hourly = schedule_breakdown.get('sub_hourly', 0)
            hourly = schedule_breakdown.get('hourly', 0) + schedule_breakdown.get('cron', 0)
            daily = schedule_breakdown.get('daily', 0) + schedule_breakdown.get('manual', 0)

            sub_hourly_percent = (sub_hourly / total) * 100
            hourly_percent = (hourly / total) * 100
            daily_percent = (daily / total) * 100

            # Calculate using worker calculator
            calculator = WorkerCalculator()
            result = calculator.calculate_from_estimate(
                total_connections=total,
                api_percent=api_percent,
                db_percent=db_percent,
                sub_hourly_percent=sub_hourly_percent,
                hourly_percent=hourly_percent,
                daily_percent=daily_percent,
                sync_duration_minutes=sync_duration
            )

            print("\n" + "=" * 80)
            print("🎯 ESTIMATED WORKERS REQUIRED (Less Accurate)")
            print("=" * 80 + "\n")

            print(f"Workers: {result['workers_required']} Enterprise Data Workers")
            print(f"Expected Peak API Concurrent: {result['expected_concurrency']['api_concurrent']}")
            print(f"Expected Peak DB Concurrent: {result['expected_concurrency']['db_concurrent']}")
            print()
            print("⚠️  This is an ESTIMATE based on statistical modeling.")
            print("   For accurate results, re-export with job_history data.")

            print("\n✅ Estimation complete\n")

            return {
                'mode': '2B_oss_workspace',
                'timestamp': datetime.utcnow().isoformat(),
                'workers_required': result['workers_required'],
                'data_source': 'oss_export_with_classification',
                'analysis_method': 'estimation',
                'confidence': 'medium',
                'note': 'Estimate based on connection counts, not actual job overlaps',
                'full_result': result
            }

        except (ValueError, KeyboardInterrupt):
            print("\n❌ Input cancelled or invalid")
            return None

    return None


def load_and_analyze_export(json_file: str) -> dict:
    """
    Load and analyze workspace export JSON.

    Args:
        json_file: Path to export JSON file

    Returns:
        Analysis result
    """
    print(f"Loading export: {json_file}")

    with open(json_file, 'r') as f:
        data = json.load(f)

    source = data.get('source', 'unknown')

    if source == 'airbyte_cloud':
        return analyze_cloud_export(data)
    elif source == 'airbyte_oss':
        return analyze_oss_export(data)
    else:
        print(f"❌ Unknown export source: {source}")
        return None


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 mode2_analyze_oss_export.py <export_file.json>")
        print("\nExample:")
        print("  python3 mode2_analyze_oss_export.py airbyte_workspace_export.json")
        sys.exit(1)

    json_file = sys.argv[1]

    result = load_and_analyze_export(json_file)

    if result:
        # Save analysis result
        output_file = f"analysis_{json_file}"
        with open(output_file, 'w') as f:
            json.dump(result, f, indent=2, default=str)

        print(f"📁 Analysis saved to: {output_file}")


if __name__ == "__main__":
    main()
