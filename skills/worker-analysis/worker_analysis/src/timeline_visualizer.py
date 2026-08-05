"""
Worker Utilization Timeline Visualizer

Generates hour-by-hour heatmap views of worker utilization across days.
Helps identify usage patterns and optimization opportunities.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Tuple
from collections import defaultdict
import math


def calculate_hourly_utilization(jobs: List[Dict]) -> Dict:
    """
    Calculate worker utilization for each hour across all days in the dataset.

    Args:
        jobs: List of job dictionaries with startTime, endTime

    Returns:
        Dictionary with hourly utilization data
    """
    if not jobs:
        return {}

    # Parse job times
    job_events = []
    for job in jobs:
        try:
            start = datetime.fromisoformat(job['startTime'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(job['endTime'].replace('Z', '+00:00'))
            job_events.append({
                'start': start,
                'end': end,
                'connector_type': job.get('connector_type', 'DATABASE')
            })
        except (KeyError, ValueError):
            continue

    if not job_events:
        return {}

    # Find date range
    all_times = [j['start'] for j in job_events] + [j['end'] for j in job_events]
    min_date = min(all_times).replace(hour=0, minute=0, second=0, microsecond=0)
    max_date = max(all_times).replace(hour=0, minute=0, second=0, microsecond=0)

    # Initialize hourly utilization matrix
    # Structure: {date_str: {hour: workers_used}}
    daily_hourly = defaultdict(lambda: defaultdict(float))

    # For each day
    current_date = min_date
    while current_date <= max_date:
        # For each hour of the day
        for hour in range(24):
            hour_start = current_date.replace(hour=hour)
            hour_end = hour_start + timedelta(hours=1)

            # Find all jobs running during this hour
            concurrent_api = 0
            concurrent_db = 0

            for job in job_events:
                # Check if job overlaps with this hour
                if job['start'] < hour_end and job['end'] > hour_start:
                    if job['connector_type'] == 'API':
                        concurrent_api += 1
                    else:  # DATABASE or UNKNOWN counted as DB
                        concurrent_db += 1

            # Calculate workers needed for this hour
            workers_needed = calculate_workers_from_concurrent(
                concurrent_api, concurrent_db
            )

            date_key = current_date.strftime('%Y-%m-%d')
            daily_hourly[date_key][hour] = workers_needed

        current_date += timedelta(days=1)

    return {
        'daily_hourly': dict(daily_hourly),
        'date_range': {
            'start': min_date.strftime('%Y-%m-%d'),
            'end': max_date.strftime('%Y-%m-%d')
        }
    }


def calculate_workers_from_concurrent(concurrent_api: int, concurrent_db: int) -> float:
    """
    Calculate workers needed from concurrent job counts.

    Formula: (API / 5) + (DB / 2) = workers used

    Args:
        concurrent_api: Number of concurrent API jobs
        concurrent_db: Number of concurrent DB/file jobs

    Returns:
        Workers needed (can be fractional)
    """
    # Capacity formula: (API / 5) + (DB / 2) = workers used
    api_workers = concurrent_api / 5.0
    db_workers = concurrent_db / 2.0

    return api_workers + db_workers


def generate_heatmap_view(utilization_data: Dict, max_days: int = 5) -> str:
    """
    Generate ASCII heatmap visualization of worker utilization.

    Args:
        utilization_data: Output from calculate_hourly_utilization()
        max_days: Maximum number of days to display (default: 5 for readability)

    Returns:
        Formatted heatmap string
    """
    daily_hourly = utilization_data.get('daily_hourly', {})
    if not daily_hourly:
        return "No utilization data available."

    # Sort dates - show last N days, but note full data range
    all_dates = sorted(daily_hourly.keys())
    dates = all_dates[-max_days:]  # Last N days for visualization

    # Build heatmap
    lines = []
    lines.append("Worker Utilization Heatmap (Last 5 Days)")
    lines.append("=" * 80)

    date_range = utilization_data.get('date_range', {})
    total_days = len(all_dates)
    lines.append(f"Full Data Period: {date_range.get('start', 'N/A')} to {date_range.get('end', 'N/A')} ({total_days} days)")
    lines.append(f"Showing: Last {len(dates)} days for readability")
    lines.append("")

    # Header with hours
    header = "Date       │ " + " ".join(f"{h:02d}" for h in range(0, 24, 2))
    lines.append(header)
    lines.append("─" * len(header))

    # Each date row
    max_workers = 0
    for date in dates:
        hourly = daily_hourly[date]
        max_workers = max(max_workers, max(hourly.values()) if hourly else 0)

    for date in dates:
        hourly = daily_hourly[date]

        # Format date (just Mon DD)
        date_obj = datetime.strptime(date, '%Y-%m-%d')
        date_label = date_obj.strftime('%b %d')

        # Build visualization for each 2-hour block
        viz_chars = []
        for h in range(0, 24, 2):
            # Average workers for this 2-hour block
            workers_h1 = hourly.get(h, 0)
            workers_h2 = hourly.get(h + 1, 0)
            avg_workers = (workers_h1 + workers_h2) / 2.0

            # Choose character based on utilization
            if avg_workers < 0.5:
                viz_chars.append('░░')  # Very low
            elif avg_workers < 1.0:
                viz_chars.append('▒▒')  # Low
            elif avg_workers < 1.5:
                viz_chars.append('▓▓')  # Medium
            elif avg_workers < 2.0:
                viz_chars.append('██')  # High
            else:
                viz_chars.append('██')  # Very high

        # Daily peak
        daily_peak = max(hourly.values()) if hourly else 0

        row = f"{date_label}  │ {' '.join(viz_chars)}  Peak: {daily_peak:.1f}"
        lines.append(row)

    # Legend
    lines.append("")
    lines.append("Legend:")
    lines.append("  ░░ <0.5 workers   ▒▒ 0.5-1.0 workers   ▓▓ 1.0-1.5 workers   ██ 1.5+ workers")
    lines.append("")

    # Peak hours analysis
    peak_hours_count = defaultdict(int)
    for date in dates:
        hourly = daily_hourly[date]
        if not hourly:
            continue
        max_val = max(hourly.values())
        for hour, workers in hourly.items():
            if workers >= max_val * 0.9:  # Within 90% of peak
                peak_hours_count[hour] += 1

    if peak_hours_count:
        lines.append("Frequent Peak Hours (UTC):")
        top_peak_hours = sorted(peak_hours_count.items(), key=lambda x: x[1], reverse=True)[:5]
        for hour, count in top_peak_hours:
            percentage = (count / len(dates)) * 100
            lines.append(f"  {hour:02d}:00 - {hour+1:02d}:00: Peak on {count}/{len(dates)} days ({percentage:.0f}%)")

    return "\n".join(lines)


def generate_hourly_summary(utilization_data: Dict) -> str:
    """
    Generate summary statistics by hour of day.

    Args:
        utilization_data: Output from calculate_hourly_utilization()

    Returns:
        Formatted summary string
    """
    daily_hourly = utilization_data.get('daily_hourly', {})
    if not daily_hourly:
        return "No utilization data available."

    # Aggregate by hour across all days
    hourly_avg = defaultdict(list)
    for date, hourly in daily_hourly.items():
        for hour, workers in hourly.items():
            hourly_avg[hour].append(workers)

    # Calculate averages
    lines = []
    lines.append("Average Worker Utilization by Hour (UTC)")
    lines.append("=" * 60)

    for hour in range(24):
        if hour in hourly_avg:
            values = hourly_avg[hour]
            avg = sum(values) / len(values)
            max_val = max(values)

            # Bar visualization
            bar_length = int(avg * 10)
            bar = '█' * bar_length + '░' * (20 - bar_length)

            lines.append(f"{hour:02d}:00 │ {bar} {avg:.2f} avg (max: {max_val:.1f})")

    return "\n".join(lines)


def identify_optimization_windows(utilization_data: Dict, target_workers: float = 1.0) -> str:
    """
    Identify time windows where utilization is consistently low.
    These are good times to schedule additional syncs.

    Args:
        utilization_data: Output from calculate_hourly_utilization()
        target_workers: Target worker allocation

    Returns:
        Formatted recommendations
    """
    daily_hourly = utilization_data.get('daily_hourly', {})
    if not daily_hourly:
        return "No utilization data available."

    # Find average utilization by hour
    hourly_avg = defaultdict(list)
    for date, hourly in daily_hourly.items():
        for hour, workers in hourly.items():
            hourly_avg[hour].append(workers)

    hourly_avg_values = {
        hour: sum(values) / len(values)
        for hour, values in hourly_avg.items()
    }

    # Find low-utilization windows
    low_util_hours = [
        (hour, avg)
        for hour, avg in hourly_avg_values.items()
        if avg < target_workers * 0.6  # Less than 60% of target
    ]
    low_util_hours.sort(key=lambda x: x[1])  # Sort by utilization

    lines = []
    lines.append("Optimization Windows - Low Utilization Hours")
    lines.append("=" * 60)
    lines.append(f"Target Allocation: {target_workers:.1f} workers")
    lines.append("")

    if low_util_hours:
        lines.append("Best times to schedule additional syncs (UTC):")
        for hour, avg in low_util_hours[:10]:  # Top 10
            capacity = target_workers - avg
            lines.append(f"  {hour:02d}:00 - {hour+1:02d}:00: {avg:.2f} avg workers ({capacity:.2f} capacity available)")
    else:
        lines.append("No significant low-utilization windows found.")
        lines.append("Consider increasing worker allocation or optimizing existing schedules.")

    return "\n".join(lines)


def calculate_optimization_potential(utilization_data: Dict, target_workers: float = 1.0) -> str:
    """
    Calculate and display optimization potential by spreading out jobs.

    Shows current vs potential optimized worker usage while maintaining freshness.

    Args:
        utilization_data: Output from calculate_hourly_utilization()
        target_workers: Target worker allocation

    Returns:
        Formatted comparison
    """
    daily_hourly = utilization_data.get('daily_hourly', {})
    if not daily_hourly:
        return "No utilization data available."

    # Calculate current metrics
    all_hourly_values = []
    for date, hourly in daily_hourly.items():
        all_hourly_values.extend(hourly.values())

    if not all_hourly_values:
        return "No utilization data available."

    current_avg = sum(all_hourly_values) / len(all_hourly_values)
    current_peak = max(all_hourly_values)
    current_p99 = sorted(all_hourly_values)[int(len(all_hourly_values) * 0.99)] if len(all_hourly_values) > 0 else 0

    # Estimate optimized state (spreading jobs evenly across low-util hours)
    # This is a theoretical best-case
    total_worker_hours = sum(all_hourly_values)
    hours_count = len(all_hourly_values)
    optimized_avg = total_worker_hours / hours_count  # Same as current avg
    optimized_peak = optimized_avg * 1.3  # Assume we can flatten to ~30% above average

    lines = []
    lines.append("Optimization Potential Analysis")
    lines.append("=" * 60)
    lines.append("")
    lines.append("**Current State** (without optimization):")
    lines.append(f"  • Average workers/hour: {current_avg:.2f}")
    lines.append(f"  • Peak workers (99th percentile): {current_p99:.2f}")
    lines.append(f"  • Maximum workers observed: {current_peak:.2f}")
    lines.append(f"  • Required allocation: {math.ceil(current_p99)} workers")
    lines.append("")
    lines.append("**Optimized State** (spreading jobs while maintaining freshness):")
    lines.append(f"  • Average workers/hour: {optimized_avg:.2f} (same)")
    lines.append(f"  • Target peak workers: {optimized_peak:.2f}")
    lines.append(f"  • Estimated allocation: {math.ceil(optimized_peak)} workers")
    lines.append("")

    workers_saved = math.ceil(current_p99) - math.ceil(optimized_peak)
    if workers_saved > 0:
        reduction_pct = (workers_saved / math.ceil(current_p99)) * 100
        lines.append(f"✅ **Potential Improvement**: Reduce from {math.ceil(current_p99)} → {math.ceil(optimized_peak)} workers")
        lines.append(f"   ({workers_saved} worker reduction = {reduction_pct:.0f}% improvement)")
        lines.append("")
        lines.append("**How to achieve this**:")
        lines.append("  1. Identify connections running during peak hours")
        lines.append("  2. Reschedule to low-utilization windows (maintaining sync frequency)")
        lines.append("  3. Spread out sub-hourly syncs across the hour")
        lines.append("  4. Use Mode 3 (Optimize) for specific recommendations")
    else:
        lines.append("✅ **Current State**: Already well-optimized!")
        lines.append("   Worker usage is evenly distributed across hours.")
        lines.append("   No significant improvement possible without changing sync frequencies.")

    return "\n".join(lines)


def generate_timeline_report(jobs: List[Dict], max_days: int = 5) -> str:
    """
    Generate complete timeline visualization report.

    Args:
        jobs: List of job dictionaries
        max_days: Maximum days to display (default: 5 for readability)

    Returns:
        Complete formatted report
    """
    utilization_data = calculate_hourly_utilization(jobs)

    sections = []
    sections.append(generate_heatmap_view(utilization_data, max_days))
    sections.append("\n\n")
    sections.append(generate_hourly_summary(utilization_data))
    sections.append("\n\n")
    sections.append(calculate_optimization_potential(utilization_data))
    sections.append("\n\n")
    sections.append(identify_optimization_windows(utilization_data))

    return "\n".join(sections)
