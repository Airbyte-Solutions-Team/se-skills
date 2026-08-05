#!/usr/bin/env python3
"""
Fast optimization analysis - optimized for speed.

Key optimizations:
1. Pre-filter jobs by date range
2. Analyze only most recent 3 peak days
3. Skip detailed overlap calculations - focus on peak concurrency only
4. More efficient event-based concurrency tracking
"""

import json
from datetime import datetime, timedelta
from collections import defaultdict

def parse_timestamp(ts_str):
    """Parse ISO timestamp string."""
    return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))

def analyze_peak_concurrency_fast(results, target_date):
    """
    Fast analysis of peak concurrency on a specific day.

    Optimized to skip detailed overlap calculations and focus on:
    - Peak concurrent jobs
    - Which connections were involved
    - Peak time
    """
    workspaces = results['workspaces']

    # Target day boundaries
    target_dt = datetime.fromisoformat(target_date).replace(tzinfo=None)
    day_start = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    # Pre-filter: collect only jobs that might overlap with this day
    day_jobs = []

    for workspace in workspaces:
        for conn in workspace['connections']:
            jobs = conn.get('jobs', [])
            if not jobs:
                continue

            conn_name = conn['name']

            # Quick filter: check if any job could be on this day
            for job in jobs:
                start_time = parse_timestamp(job['startTime']).replace(tzinfo=None)

                # Quick reject: job started after day ended
                if start_time >= day_end:
                    continue

                end_time = parse_timestamp(job['endTime']).replace(tzinfo=None)

                # Quick reject: job ended before day started
                if end_time <= day_start:
                    continue

                # This job overlaps with our target day
                day_jobs.append({
                    'connection_name': conn_name,
                    'start': start_time,
                    'end': end_time,
                    'duration_minutes': job['duration'] / 60,
                    'job_id': job.get('jobId', 'unknown')
                })

    if not day_jobs:
        return None

    # Calculate peak concurrency using efficient event-based algorithm
    events = []
    for job in day_jobs:
        events.append({'time': job['start'], 'delta': 1, 'name': job['connection_name']})
        events.append({'time': job['end'], 'delta': -1, 'name': job['connection_name']})

    events.sort(key=lambda x: (x['time'], -x['delta']))  # Process starts before ends at same time

    max_concurrent = 0
    peak_time = None
    peak_connections = []
    current_concurrent = 0
    active_connections = []

    for event in events:
        current_concurrent += event['delta']

        if event['delta'] > 0:
            active_connections.append(event['name'])
        else:
            if event['name'] in active_connections:
                active_connections.remove(event['name'])

        if current_concurrent > max_concurrent:
            max_concurrent = current_concurrent
            peak_time = event['time']
            peak_connections = active_connections.copy()

    return {
        'date': target_date,
        'total_jobs': len(day_jobs),
        'peak_concurrent': max_concurrent,
        'peak_time': peak_time.isoformat() if peak_time else None,
        'peak_connections': peak_connections
    }

def generate_fast_recommendations(peak_days_analysis, current_workers):
    """
    Generate fast optimization recommendations.

    Simplified logic:
    - If peak > 2: need to reschedule
    - If peak == 2: already optimal for 1 worker
    - If peak < 2: over-allocated
    """
    recommendations = []

    for analysis in peak_days_analysis:
        if not analysis:
            continue

        peak = analysis['peak_concurrent']
        date = analysis['date']

        if peak > 2:
            # Need optimization
            rec = {
                'date': date,
                'peak_concurrent': peak,
                'peak_time': analysis['peak_time'],
                'status': 'needs_optimization',
                'connections_at_peak': analysis['peak_connections'],
                'recommendation': f'Reschedule 1-2 connections to reduce peak from {peak} to 2 concurrent'
            }
        elif peak == 2:
            # Optimal for 1 worker (can handle 2 DB connections)
            rec = {
                'date': date,
                'peak_concurrent': peak,
                'peak_time': analysis['peak_time'],
                'status': 'optimal',
                'connections_at_peak': analysis['peak_connections'],
                'recommendation': 'Already optimized - peak matches 1 worker capacity'
            }
        else:
            # Under-utilized
            rec = {
                'date': date,
                'peak_concurrent': peak,
                'peak_time': analysis['peak_time'],
                'status': 'under_utilized',
                'connections_at_peak': analysis['peak_connections'],
                'recommendation': 'Could potentially schedule more connections during this period'
            }

        recommendations.append(rec)

    return recommendations

def calculate_fast_impact(peak_days_analysis, current_workers):
    """
    Fast calculation of optimization impact.
    """
    if not peak_days_analysis:
        return {
            'current_workers': current_workers,
            'optimal_workers': current_workers,
            'can_optimize': False,
            'message': 'Insufficient data for optimization'
        }

    # Find max peak across all analyzed days
    max_peak = max(a['peak_concurrent'] for a in peak_days_analysis if a)

    # Calculate optimal workers needed
    # 1 Enterprise Data Worker = 2 concurrent DB connections
    optimal_workers = max(1, (max_peak + 1) // 2)

    # Check if any days need optimization
    days_need_optimization = sum(1 for a in peak_days_analysis if a and a['peak_concurrent'] > 2)

    return {
        'current_workers': current_workers,
        'max_peak_concurrent': max_peak,
        'optimal_workers': optimal_workers,
        'worker_reduction': max(0, current_workers - optimal_workers),
        'days_needing_optimization': days_need_optimization,
        'can_optimize': days_need_optimization > 0 or current_workers > optimal_workers,
        'cost_savings_percent': ((current_workers - optimal_workers) / current_workers * 100) if current_workers > 0 else 0
    }

if __name__ == "__main__":
    print("This module provides fast optimization functions.")
    print("Import and use: analyze_peak_concurrency_fast(), generate_fast_recommendations(), calculate_fast_impact()")
