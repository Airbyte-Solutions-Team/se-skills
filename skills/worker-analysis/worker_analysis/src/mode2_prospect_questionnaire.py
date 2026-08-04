#!/usr/bin/env python3
"""
Mode 2A: New Prospect - Questionnaire-based Estimation

For prospects who have never used Airbyte before.
Uses answers to standardized questionnaire to estimate worker requirements.

IMPORTANT: This uses STATISTICAL ESTIMATION, not actual job overlap analysis.
The estimation may be less accurate than analyzing actual job history.

For existing customers with job history data:
- Use Mode 1 (workspace analysis) which uses JOB OVERLAP ANALYSIS
- Job overlap analysis calculates ACTUAL concurrent jobs at each minute
- This gives accurate worker requirements based on real usage patterns

This estimation mode is appropriate for:
- New prospects who haven't used Airbyte yet
- Quick ballpark estimates before detailed analysis
- Planning discussions before actual deployment
"""

import json
import sys
import os
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from worker_calculator import WorkerCalculator


def estimate_from_questionnaire(
    total_connections: int,
    db_file_percent: float,
    api_percent: float,
    sub_hourly_percent: float,
    hourly_percent: float,
    daily_percent: float,
    sync_duration_minutes: float = None,
    maintenance_window_hours: float = None
) -> dict:
    """
    Estimate worker requirements from prospect questionnaire answers.

    Args:
        total_connections: Total number of connections planned
        db_file_percent: Percentage of Database or File Connections (%)
        api_percent: Percentage of API Connections (%)
        sub_hourly_percent: Percentage running sub-hourly (%)
        hourly_percent: Percentage running hourly (%)
        daily_percent: Percentage running daily or less (%)
        sync_duration_minutes: [OPTIONAL] Most syncs under time (minutes)
        maintenance_window_hours: [OPTIONAL] Maintenance window for infrequent syncs (hours)

    Returns:
        Dictionary with estimation results
    """
    print("\n" + "=" * 80)
    print("📋 MODE 2A: NEW PROSPECT ESTIMATION")
    print("=" * 80 + "\n")

    print("Input Parameters:")
    print(f"  • Total Connections: {total_connections}")
    print(f"  • Database/File Connections: {db_file_percent}%")
    print(f"  • API Connections: {api_percent}%")
    print(f"  • Sub-hourly Syncs: {sub_hourly_percent}%")
    print(f"  • Hourly Syncs: {hourly_percent}%")
    print(f"  • Daily/Less Frequent Syncs: {daily_percent}%")
    print(f"  • Worker Model: Universal (API/5 + DB/2)")

    if sync_duration_minutes:
        print(f"  • Avg Sync Duration: {sync_duration_minutes} minutes")
    if maintenance_window_hours:
        print(f"  • Maintenance Window: {maintenance_window_hours} hours")

    # Calculate using worker calculator
    calculator = WorkerCalculator()

    result = calculator.calculate_from_estimate(
        total_connections=total_connections,
        api_percent=api_percent,
        db_percent=db_file_percent,
        sub_hourly_percent=sub_hourly_percent,
        hourly_percent=hourly_percent,
        daily_percent=daily_percent,
        sync_duration_minutes=sync_duration_minutes
    )

    # Display results
    print("\n" + "=" * 80)
    print("📊 ESTIMATION RESULTS")
    print("=" * 80 + "\n")

    breakdown = result['connection_breakdown']
    concurrency = result['expected_concurrency']

    print(f"Connection Breakdown:")
    print(f"  • API Connections: {breakdown['api_connections']}")
    print(f"  • Database/File Connections: {breakdown['db_connections']}")
    print(f"  • Sub-hourly: {breakdown['by_frequency']['sub_hourly']}")
    print(f"  • Hourly: {breakdown['by_frequency']['hourly']}")
    print(f"  • Daily: {breakdown['by_frequency']['daily']}")

    print(f"\nExpected Peak Concurrency:")
    print(f"  • API Concurrent: {concurrency['api_concurrent']}")
    print(f"  • DB/File Concurrent: {concurrency['db_concurrent']}")
    print(f"  • Total Concurrent: {concurrency['total_concurrent']}")

    print(f"\n🎯 ESTIMATED WORKERS REQUIRED: {result['workers_required']} {result['worker_type']}")
    print()
    print("⚠️  NOTE: This is a statistical ESTIMATE based on expected concurrency.")
    print("   Once the customer is on the platform, analyze actual job history")
    print("   using Mode 1 (job overlap analysis) for accurate worker requirements.\n")

    # Add maintenance window insights if provided
    if maintenance_window_hours:
        print("=" * 80)
        print("💡 MAINTENANCE WINDOW INSIGHTS")
        print("=" * 80 + "\n")

        # Calculate how many connections can run in maintenance window
        infrequent_count = breakdown['by_frequency']['daily']
        if sync_duration_minutes:
            # How many syncs can fit in window
            syncs_per_window = maintenance_window_hours * 60 / sync_duration_minutes
            # If we have 2 workers (4 DB concurrent), how many can we handle
            concurrent_capacity = 2 * result['calculation_details']['db_connections_per_worker']
            max_in_window = syncs_per_window * concurrent_capacity

            print(f"With a {maintenance_window_hours}h maintenance window:")
            print(f"  • Can handle ~{int(max_in_window)} infrequent syncs")
            print(f"  • Current daily syncs: {infrequent_count}")
            if infrequent_count <= max_in_window:
                print(f"  ✅ All infrequent syncs fit comfortably in window")
            else:
                print(f"  ⚠️  May need longer window or parallel processing")
        print()

    return result


if __name__ == "__main__":
    # Example usage
    print("Example: 50 connections, 60% DB/File, 40% API, mixed frequencies\n")

    result = estimate_from_questionnaire(
        total_connections=50,
        db_file_percent=60,
        api_percent=40,
        sub_hourly_percent=20,
        hourly_percent=30,
        daily_percent=50,
        sync_duration_minutes=5,
        maintenance_window_hours=4
    )

    # Save result
    output_file = "prospect_estimate.json"
    with open(output_file, 'w') as f:
        json.dump({
            'mode': '2A_questionnaire',
            'timestamp': datetime.utcnow().isoformat(),
            'result': result
        }, f, indent=2, default=str)

    print(f"📁 Results saved to: {output_file}")
