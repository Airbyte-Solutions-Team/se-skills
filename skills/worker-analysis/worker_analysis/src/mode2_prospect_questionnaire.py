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

try:
    from src import config
except ImportError:
    import config

from questionnaire_calculator import analyze_questionnaire
from worker_calculator import WorkerCalculator


def estimate_from_questionnaire(
    total_connections: int,
    db_file_percent: float,
    api_percent: float,
    sub_hourly_percent: float,
    hourly_percent: float,
    daily_percent: float,
    sync_duration_minutes: float = None,
    maintenance_window_hours: float = None,
    freshness_minutes: float = 60.0,
    environments: int = 2,
    growth_connections: int = None,
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
        freshness_minutes: [OPTIONAL] Target freshness window for daily batch (minutes)
        environments: [OPTIONAL] Number of environments (default 2: prod + staging)
        growth_connections: [OPTIONAL] Future connection target for growth sizing

    Returns:
        Dictionary with deterministic estimation results including all seven
        sizing views required for a complete questionnaire answer.
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
    if freshness_minutes:
        print(f"  • Freshness Window: {freshness_minutes} minutes")
    if environments:
        print(f"  • Environments: {environments}")
    if growth_connections:
        print(f"  • Growth Target: {growth_connections} connections")

    sizing = analyze_questionnaire(
        total_connections=total_connections,
        api_percent=api_percent,
        db_percent=db_file_percent,
        sub_hourly_percent=sub_hourly_percent,
        hourly_percent=hourly_percent,
        daily_percent=daily_percent,
        sync_duration_minutes=sync_duration_minutes,
        maintenance_window_hours=maintenance_window_hours,
        freshness_minutes=freshness_minutes,
        environments=environments,
        growth_connections=growth_connections,
    )

    print("\n" + "=" * 80)
    print("📊 DETERMINISTIC SIZING SUMMARY")
    print("=" * 80 + "\n")

    print("| Sizing view | Workers |")
    print("|---|---|")
    print(f"| Steady-state requirement | {sizing['steady_state_workers']} |")
    print(f"| Peak-window drain requirement | {sizing['peak_window_drain_workers']} |")
    print(f"| Worst-case simultaneous or clustered burst | {sizing['worst_case_burst_workers']} |")
    print(f"| Production-only requirement | {sizing['production_only_workers']} |")
    print(f"| Combined production and staging requirement | {sizing['combined_prod_staging_workers']} |")
    print(f"| Future-growth requirement | {sizing['future_growth_workers']} |")
    print(f"| Recommended contract or deployment capacity | {sizing['recommended_contract_or_deployment_workers']} |")

    print("\n" + "=" * 80)
    print("📋 CONNECTION MATRIX")
    print("=" * 80 + "\n")

    matrix = sizing["connection_matrix"]
    print("| Type | Sub-hourly | Hourly | Daily | Total |")
    print("|---|---|---|---|---|")
    print(
        f"| API | {matrix['sub_hourly']['api']} | "
        f"{matrix['hourly']['api']} | {matrix['daily']['api']} | "
        f"{matrix['api_connections']} |"
    )
    print(
        f"| DB/File | {matrix['sub_hourly']['db']} | "
        f"{matrix['hourly']['db']} | {matrix['daily']['db']} | "
        f"{matrix['db_connections']} |"
    )
    print(
        f"| Total | {matrix['sub_hourly']['total']} | "
        f"{matrix['hourly']['total']} | {matrix['daily']['total']} | "
        f"{sum(matrix[b]['total'] for b in ('sub_hourly', 'hourly', 'daily'))} |"
    )

    print("\n" + "=" * 80)
    print("🎯 ESTIMATED WORKERS REQUIRED: "
          f"{sizing['recommended_contract_or_deployment_workers']} Data Workers")
    print("=" * 80 + "\n")

    print("⚠️  NOTE: This is a statistical ESTIMATE based on expected concurrency.")
    print("   Once the customer is on the platform, analyze actual job history")
    print("   using Mode 1 (job overlap analysis) for accurate worker requirements.\n")

    # Add maintenance window insights if provided
    if maintenance_window_hours:
        print("=" * 80)
        print("💡 MAINTENANCE WINDOW INSIGHTS")
        print("=" * 80 + "\n")

        infrequent_count = matrix['daily']['total']
        if sync_duration_minutes:
            syncs_per_window = maintenance_window_hours * 60 / sync_duration_minutes
            # Use combined API + DB daily slots at the steady-state per-worker level
            daily_slots_per_worker = (
                config.API_CONNECTIONS_PER_WORKER + config.DB_CONNECTIONS_PER_WORKER
            )
            max_in_window = syncs_per_window * daily_slots_per_worker

            print(f"With a {maintenance_window_hours}h maintenance window:")
            print(f"  • Can handle ~{int(max_in_window)} infrequent syncs")
            print(f"  • Current daily syncs: {infrequent_count}")
            if infrequent_count <= max_in_window:
                print(f"  ✅ All infrequent syncs fit comfortably in window")
            else:
                print(f"  ⚠️  May need longer window or parallel processing")
        print()

    # Also include the legacy WorkerCalculator steady-state output so callers that
    # expect the previous result shape continue to work.
    legacy_estimate = WorkerCalculator().calculate_from_estimate(
        total_connections=total_connections,
        api_percent=api_percent,
        db_percent=db_file_percent,
        sub_hourly_percent=sub_hourly_percent,
        hourly_percent=hourly_percent,
        daily_percent=daily_percent,
        sync_duration_minutes=sync_duration_minutes,
    )

    return {
        "sizing": sizing,
        "legacy_estimate": legacy_estimate,
    }


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
        maintenance_window_hours=4,
    )

    # Save result
    output_file = "prospect_estimate.json"
    with open(output_file, 'w') as f:
        json.dump({
            'mode': '2A_questionnaire',
            'timestamp': datetime.utcnow().isoformat(),
            'result': result,
        }, f, indent=2, default=str)

    print(f"📁 Results saved to: {output_file}")
