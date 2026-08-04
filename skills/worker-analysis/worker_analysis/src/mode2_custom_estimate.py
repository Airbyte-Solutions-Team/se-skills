#!/usr/bin/env python3
"""
Mode 2C: Custom/General Estimate

Quick ballpark estimate for pricing discussions.
Takes minimal input and provides general worker estimate with ranges.
"""

import json
import sys
import os
from datetime import datetime

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'src'))

from worker_calculator import WorkerCalculator


def quick_estimate(
    description: str,
    connection_count_range: str,
    connector_mix: str = "mixed",
    sync_frequency: str = "hourly",
) -> dict:
    """
    Generate quick ballpark estimate from high-level description.

    Args:
        description: Brief description of use case
        connection_count_range: "small" (1-25), "medium" (25-100), "large" (100-500), "xlarge" (500+)
        connector_mix: "mostly_api", "mostly_db", "mixed"
        sync_frequency: "realtime" (sub-hourly), "frequent" (hourly), "daily", "mixed"

    Returns:
        Dictionary with estimate range and recommendations
    """
    print("\n" + "=" * 80)
    print("💡 MODE 2C: CUSTOM QUICK ESTIMATE")
    print("=" * 80 + "\n")

    print("Input:")
    print(f"  • Use Case: {description}")
    print(f"  • Connection Range: {connection_count_range}")
    print(f"  • Connector Mix: {connector_mix}")
    print(f"  • Sync Frequency: {sync_frequency}")
    print(f"  • Worker Model: Universal (API/5 + DB/2)")

    # Map ranges to actual numbers (low, mid, high)
    connection_ranges = {
        "small": (5, 15, 25),
        "medium": (25, 60, 100),
        "large": (100, 250, 500),
        "xlarge": (500, 750, 1000)
    }

    # Map connector mix to percentages (api%, db%)
    connector_mixes = {
        "mostly_api": (70, 30),
        "mostly_db": (30, 70),
        "mixed": (50, 50)
    }

    # Map frequency to percentages (sub-hourly%, hourly%, daily%)
    frequency_mixes = {
        "realtime": (60, 30, 10),
        "frequent": (20, 50, 30),
        "daily": (5, 20, 75),
        "mixed": (20, 30, 50)
    }

    conn_low, conn_mid, conn_high = connection_ranges.get(
        connection_count_range, (10, 50, 100)
    )
    api_pct, db_pct = connector_mixes.get(connector_mix, (50, 50))
    sub_hourly_pct, hourly_pct, daily_pct = frequency_mixes.get(
        sync_frequency, (20, 30, 50)
    )

    calculator = WorkerCalculator()

    # Calculate low, mid, high estimates
    estimates = {}

    for label, conn_count in [("low", conn_low), ("mid", conn_mid), ("high", conn_high)]:
        result = calculator.calculate_from_estimate(
            total_connections=conn_count,
            api_percent=api_pct,
            db_percent=db_pct,
            sub_hourly_percent=sub_hourly_pct,
            hourly_percent=hourly_pct,
            daily_percent=daily_pct
        )
        estimates[label] = {
            "connections": conn_count,
            "workers": result['workers_required'],
            "peak_concurrent": result['expected_concurrency']['total_concurrent']
        }

    # Display results
    print("\n" + "=" * 80)
    print("📊 ESTIMATED WORKER RANGE")
    print("=" * 80 + "\n")

    print(f"Conservative (Low):  {estimates['low']['connections']} connections  → {estimates['low']['workers']} workers")
    print(f"Expected (Mid):      {estimates['mid']['connections']} connections  → {estimates['mid']['workers']} workers")
    print(f"High-Growth (High):  {estimates['high']['connections']} connections  → {estimates['high']['workers']} workers")

    print("\n" + "=" * 80)
    print("💰 PRICING GUIDANCE")
    print("=" * 80 + "\n")

    mid_workers = estimates['mid']['workers']
    high_workers = estimates['high']['workers']

    print(f"Recommend quoting: {mid_workers}-{high_workers} workers")
    print(f"  • Start with {mid_workers} workers")
    print(f"  • Include provision to scale to {high_workers} workers")
    print(f"  • Re-assess at 60/90 days based on actual usage")

    # Recommendations
    print("\n" + "=" * 80)
    print("💡 RECOMMENDATIONS")
    print("=" * 80 + "\n")

    if sync_frequency == "realtime":
        print("⚠️  Realtime syncs detected:")
        print("  • Consider CDC for database sources")
        print("  • Monitor for API rate limits")
        print("  • May need optimization after onboarding")

    if connection_count_range in ["large", "xlarge"]:
        print("⚠️  Large deployment detected:")
        print("  • Recommend phased rollout")
        print("  • Plan optimization review after first 100 connections")
        print("  • Consider dedicated support engagement")

    if connector_mix == "mostly_db":
        print("ℹ️  Database-heavy workload:")
        print("  • Each worker handles 2 concurrent DB syncs")
        print("  • Consider scheduling to avoid peak overlap")

    print()

    return {
        'mode': '2C_custom_estimate',
        'timestamp': datetime.utcnow().isoformat(),
        'description': description,
        'input': {
            'connection_range': connection_count_range,
            'connector_mix': connector_mix,
            'sync_frequency': sync_frequency,
            'worker_model': 'universal'
        },
        'estimates': estimates,
        'recommendation': {
            'quote_range': f"{mid_workers}-{high_workers} workers",
            'start_with': mid_workers,
            'scale_to': high_workers
        }
    }


def interactive_estimate():
    """
    Interactive mode for custom estimates.
    """
    print("\n" + "=" * 80)
    print("💡 CUSTOM QUICK ESTIMATE - INTERACTIVE MODE")
    print("=" * 80 + "\n")

    print("Let's gather some high-level information...\n")

    description = input("Brief description of use case: ")

    print("\nConnection count range:")
    print("  1. Small (1-25 connections)")
    print("  2. Medium (25-100 connections)")
    print("  3. Large (100-500 connections)")
    print("  4. X-Large (500+ connections)")
    range_choice = input("Choice [1-4]: ")

    range_map = {"1": "small", "2": "medium", "3": "large", "4": "xlarge"}
    connection_range = range_map.get(range_choice, "medium")

    print("\nConnector mix:")
    print("  1. Mostly API (Salesforce, HubSpot, Stripe, etc.)")
    print("  2. Mostly Database/Files (Postgres, MySQL, S3, etc.)")
    print("  3. Mixed")
    mix_choice = input("Choice [1-3]: ")

    mix_map = {"1": "mostly_api", "2": "mostly_db", "3": "mixed"}
    connector_mix = mix_map.get(mix_choice, "mixed")

    print("\nSync frequency:")
    print("  1. Real-time (sub-hourly)")
    print("  2. Frequent (hourly)")
    print("  3. Daily")
    print("  4. Mixed")
    freq_choice = input("Choice [1-4]: ")

    freq_map = {"1": "realtime", "2": "frequent", "3": "daily", "4": "mixed"}
    sync_frequency = freq_map.get(freq_choice, "mixed")

    return quick_estimate(
        description=description,
        connection_count_range=connection_range,
        connector_mix=connector_mix,
        sync_frequency=sync_frequency,
    )


def main():
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--interactive":
        result = interactive_estimate()
    else:
        # Example: E-commerce company
        result = quick_estimate(
            description="E-commerce company syncing product data, orders, and analytics",
            connection_count_range="medium",
            connector_mix="mostly_api",
            sync_frequency="frequent",
        )

    # Save result
    output_file = "custom_estimate.json"
    with open(output_file, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    print(f"📁 Estimate saved to: {output_file}")


if __name__ == "__main__":
    main()
