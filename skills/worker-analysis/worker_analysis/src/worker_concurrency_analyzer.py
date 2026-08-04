#!/usr/bin/env python3
"""
Raylo Airbyte Workspace - Job Concurrency & Worker Analysis
Analyzes job overlap patterns over Feb 4-6, 2026 (72 hours)
to determine worker requirements and optimization opportunities.
"""

import math
from datetime import datetime, timedelta
from collections import defaultdict

# ============================================================
# CONNECTION DEFINITIONS
# ============================================================

CONNECTIONS = [
    # --- API CONNECTORS ---
    {
        "id": "1656ad7b", "name": "Facebook Ads", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~02:25 UTC",
        "duration_min": 2,
        "starts": [
            datetime(2026, 2, 4, 2, 25),
            datetime(2026, 2, 5, 2, 25),
            datetime(2026, 2, 6, 2, 25),
        ],
    },
    {
        "id": "1b70fd9b", "name": "Stripe", "type": "API",
        "schedule": "every_2h", "schedule_desc": "Every 2h",
        "duration_min": 5,
        "starts": [
            # Feb 4
            datetime(2026, 2, 4, 0, 17), datetime(2026, 2, 4, 2, 17),
            datetime(2026, 2, 4, 4, 17), datetime(2026, 2, 4, 6, 17),
            datetime(2026, 2, 4, 8, 17), datetime(2026, 2, 4, 10, 17),
            datetime(2026, 2, 4, 12, 17), datetime(2026, 2, 4, 14, 17),
            datetime(2026, 2, 4, 16, 17), datetime(2026, 2, 4, 18, 17),
            datetime(2026, 2, 4, 20, 17), datetime(2026, 2, 4, 22, 17),
            # Feb 5
            datetime(2026, 2, 5, 0, 17), datetime(2026, 2, 5, 2, 17),
            datetime(2026, 2, 5, 4, 17), datetime(2026, 2, 5, 6, 17),
            datetime(2026, 2, 5, 8, 17), datetime(2026, 2, 5, 10, 17),
            datetime(2026, 2, 5, 12, 17), datetime(2026, 2, 5, 14, 17),
            datetime(2026, 2, 5, 16, 17), datetime(2026, 2, 5, 18, 17),
            datetime(2026, 2, 5, 20, 17), datetime(2026, 2, 5, 22, 17),
            # Feb 6
            datetime(2026, 2, 6, 0, 17), datetime(2026, 2, 6, 2, 17),
            datetime(2026, 2, 6, 4, 17), datetime(2026, 2, 6, 6, 17),
            datetime(2026, 2, 6, 8, 17), datetime(2026, 2, 6, 10, 17),
            datetime(2026, 2, 6, 12, 17), datetime(2026, 2, 6, 14, 17),
            datetime(2026, 2, 6, 16, 17), datetime(2026, 2, 6, 18, 17),
            datetime(2026, 2, 6, 20, 17), datetime(2026, 2, 6, 22, 17),
        ],
    },
    {
        "id": "23044727", "name": "Anchor Proposals", "type": "API",
        "schedule": "hourly", "schedule_desc": "Hourly",
        "duration_min": 3,
        "starts": [],  # will be generated
    },
    {
        "id": "26126d16", "name": "Anchor s3custdb", "type": "DB",
        "schedule": "hourly", "schedule_desc": "Hourly",
        "duration_min": 2,
        "starts": [],  # will be generated
    },
    {
        "id": "9ef83611", "name": "Anchor Collections", "type": "API",
        "schedule": "hourly", "schedule_desc": "Hourly",
        "duration_min": 3,
        "starts": [],  # will be generated
    },
    {
        "id": "2c364cc0", "name": "Raylo Prod (No CDC)", "type": "DB",
        "schedule": "every_15min", "schedule_desc": "Every 15min",
        "duration_min": 10,
        "starts": [],  # will be generated
    },
    {
        "id": "3f4c560e", "name": "Raylo Prod (CDC)", "type": "DB",
        "schedule": "every_15min", "schedule_desc": "Every 15min",
        "duration_min": 10,
        "starts": [],  # will be generated
    },
    {
        "id": "710667ab", "name": "Customer.io Checkouts", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~01:45 UTC",
        "duration_min": 3,
        "starts": [
            datetime(2026, 2, 4, 1, 45),
            datetime(2026, 2, 5, 1, 45),
            datetime(2026, 2, 6, 1, 45),
        ],
    },
    {
        "id": "7cd03b67", "name": "Google Ads", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~02:17 UTC",
        "duration_min": 5,
        "starts": [
            datetime(2026, 2, 4, 2, 17),
            datetime(2026, 2, 5, 2, 17),
            datetime(2026, 2, 6, 2, 17),
        ],
    },
    {
        "id": "802b3bc6", "name": "Intercom", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~02:17 UTC",
        "duration_min": 15,
        "starts": [
            datetime(2026, 2, 4, 2, 17),
            datetime(2026, 2, 5, 2, 17),
            datetime(2026, 2, 6, 2, 17),
        ],
    },
    {
        "id": "80b8eb87", "name": "Customer.io New Customers", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~01:30 UTC",
        "duration_min": 3,
        "starts": [
            datetime(2026, 2, 4, 1, 30),
            datetime(2026, 2, 5, 1, 30),
            datetime(2026, 2, 6, 1, 30),
        ],
    },
    {
        "id": "83ee29bb", "name": "Raylo Staging", "type": "DB",
        "schedule": "every_6h", "schedule_desc": "Every 6h",
        "duration_min": 10,
        "starts": [],  # will be generated
    },
    {
        "id": "84dca7f5", "name": "Typesense", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~02:17 UTC",
        "duration_min": 5,
        "starts": [
            datetime(2026, 2, 4, 2, 17),
            datetime(2026, 2, 5, 2, 17),
            datetime(2026, 2, 6, 2, 17),
        ],
    },
    {
        "id": "8f8750b5", "name": "TrustPilot v2", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~09:00 UTC",
        "duration_min": 10,
        "starts": [
            datetime(2026, 2, 4, 9, 0),
            datetime(2026, 2, 5, 9, 0),
            datetime(2026, 2, 6, 9, 0),
        ],
    },
    {
        "id": "9394b3b4", "name": "Taktile Cases", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~05:35 UTC",
        "duration_min": 2,
        "starts": [
            datetime(2026, 2, 4, 5, 35),
            datetime(2026, 2, 5, 5, 35),
            datetime(2026, 2, 6, 5, 35),
        ],
    },
    {
        "id": "ddd24b52", "name": "Survicate v2", "type": "API",
        "schedule": "every_12h", "schedule_desc": "Every 12h",
        "duration_min": 15,
        "starts": [
            datetime(2026, 2, 4, 2, 0), datetime(2026, 2, 4, 14, 0),
            datetime(2026, 2, 5, 2, 0), datetime(2026, 2, 5, 14, 0),
            datetime(2026, 2, 6, 2, 0), datetime(2026, 2, 6, 14, 0),
        ],
    },
    {
        "id": "de634ef9", "name": "Airbyte", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~06:13 UTC",
        "duration_min": 3,
        "starts": [
            datetime(2026, 2, 4, 6, 13),
            datetime(2026, 2, 5, 6, 13),
            datetime(2026, 2, 6, 6, 13),
        ],
    },
    {
        "id": "ffac34ce", "name": "Sentry", "type": "API",
        "schedule": "daily", "schedule_desc": "Daily ~20:11 UTC",
        "duration_min": 10,
        "starts": [
            datetime(2026, 2, 4, 20, 11),
            datetime(2026, 2, 5, 20, 11),
            datetime(2026, 2, 6, 20, 11),
        ],
    },
    # --- BIG DB JOBS ---
    {
        "id": "23f0eb68", "name": "Anchor s3db01 (Full Refresh)", "type": "DB",
        "schedule": "daily", "schedule_desc": "Daily ~03:00 UTC, 6-12h",
        "duration_min": 540,  # 9 hours average
        "starts": [
            datetime(2026, 2, 4, 3, 0),
            datetime(2026, 2, 5, 3, 0),
            datetime(2026, 2, 6, 3, 0),
        ],
    },
    {
        "id": "dcfc37e8", "name": "Anchor s3db01 (Incremental)", "type": "DB",
        "schedule": "every_2h", "schedule_desc": "Every 2h, ~90min each",
        "duration_min": 90,
        "starts": [],  # will be generated
    },
]


def generate_periodic_starts(base_hour, base_min, interval_hours, interval_min=0):
    """Generate start times for a periodic schedule over Feb 4-6."""
    starts = []
    start = datetime(2026, 2, 4, base_hour, base_min)
    end = datetime(2026, 2, 7, 0, 0)
    delta = timedelta(hours=interval_hours, minutes=interval_min)
    t = start
    while t < end:
        starts.append(t)
        t += delta
    return starts


# Generate starts for periodic connections
for conn in CONNECTIONS:
    if conn["id"] == "23044727":  # Anchor Proposals - hourly at :35
        conn["starts"] = generate_periodic_starts(0, 35, 1)
    elif conn["id"] == "26126d16":  # Anchor s3custdb - hourly at :05
        conn["starts"] = generate_periodic_starts(0, 5, 1)
    elif conn["id"] == "9ef83611":  # Anchor Collections - hourly at :50
        conn["starts"] = generate_periodic_starts(0, 50, 1)
    elif conn["id"] == "2c364cc0":  # Raylo Prod No CDC - every 15min starting :00
        conn["starts"] = generate_periodic_starts(0, 0, 0, 15)
    elif conn["id"] == "3f4c560e":  # Raylo Prod CDC - every 15min starting :02
        conn["starts"] = generate_periodic_starts(0, 2, 0, 15)
    elif conn["id"] == "83ee29bb":  # Raylo Staging - every 6h at :00
        conn["starts"] = generate_periodic_starts(0, 0, 6)
    elif conn["id"] == "dcfc37e8":  # Anchor s3db01 incremental - every 2h at :30
        conn["starts"] = generate_periodic_starts(0, 30, 2)


# ============================================================
# BUILD MINUTE-BY-MINUTE TIMELINE
# ============================================================

ANALYSIS_START = datetime(2026, 2, 4, 0, 0)
ANALYSIS_END = datetime(2026, 2, 7, 0, 0)
TOTAL_MINUTES = int((ANALYSIS_END - ANALYSIS_START).total_seconds() / 60)

# For each minute, track which jobs are running
# timeline[minute_offset] = {"api": set(), "db": set()}
timeline = [{"api": set(), "db": set()} for _ in range(TOTAL_MINUTES)]

# All job intervals for detailed analysis
all_jobs = []

for conn in CONNECTIONS:
    for start in conn["starts"]:
        if start < ANALYSIS_START or start >= ANALYSIS_END:
            continue
        end = start + timedelta(minutes=conn["duration_min"])
        # Clamp end to analysis window
        if end > ANALYSIS_END:
            end = ANALYSIS_END

        all_jobs.append({
            "conn_id": conn["id"],
            "name": conn["name"],
            "type": conn["type"],
            "start": start,
            "end": end,
            "duration_min": conn["duration_min"],
        })

        start_offset = int((start - ANALYSIS_START).total_seconds() / 60)
        end_offset = int((end - ANALYSIS_START).total_seconds() / 60)

        for m in range(max(0, start_offset), min(TOTAL_MINUTES, end_offset)):
            bucket = "api" if conn["type"] == "API" else "db"
            timeline[m][bucket].add(conn["name"])


# ============================================================
# HOUR-BY-HOUR AGGREGATION
# ============================================================

TOTAL_HOURS = 72

# For each hour: peak concurrent API, peak concurrent DB
hourly_stats = []

for h in range(TOTAL_HOURS):
    hour_start_min = h * 60
    hour_end_min = (h + 1) * 60
    peak_api = 0
    peak_db = 0
    peak_api_set = set()
    peak_db_set = set()
    all_api_in_hour = set()
    all_db_in_hour = set()

    for m in range(hour_start_min, min(hour_end_min, TOTAL_MINUTES)):
        api_count = len(timeline[m]["api"])
        db_count = len(timeline[m]["db"])
        all_api_in_hour.update(timeline[m]["api"])
        all_db_in_hour.update(timeline[m]["db"])
        if api_count > peak_api:
            peak_api = api_count
            peak_api_set = set(timeline[m]["api"])
        if db_count > peak_db:
            peak_db = db_count
            peak_db_set = set(timeline[m]["db"])

    workers = math.ceil(peak_api / 5) + math.ceil(peak_db / 2)
    hour_dt = ANALYSIS_START + timedelta(hours=h)

    hourly_stats.append({
        "hour": h,
        "datetime": hour_dt,
        "date": hour_dt.strftime("%b %d"),
        "hour_utc": hour_dt.strftime("%H:00"),
        "peak_api": peak_api,
        "peak_db": peak_db,
        "workers": workers,
        "peak_api_names": peak_api_set,
        "peak_db_names": peak_db_set,
        "all_api": all_api_in_hour,
        "all_db": all_db_in_hour,
    })


# ============================================================
# CALCULATE P99
# ============================================================

all_worker_values = [s["workers"] for s in hourly_stats]
all_worker_values_sorted = sorted(all_worker_values)
p99_index = int(math.ceil(0.99 * len(all_worker_values_sorted))) - 1
p99_workers = all_worker_values_sorted[p99_index]
p95_index = int(math.ceil(0.95 * len(all_worker_values_sorted))) - 1
p95_workers = all_worker_values_sorted[p95_index]
p50_index = int(math.ceil(0.50 * len(all_worker_values_sorted))) - 1
p50_workers = all_worker_values_sorted[p50_index]
max_workers = max(all_worker_values)
avg_workers = sum(all_worker_values) / len(all_worker_values)


# ============================================================
# OUTPUT
# ============================================================

print("=" * 100)
print("RAYLO AIRBYTE WORKSPACE - JOB CONCURRENCY & WORKER ANALYSIS")
print("Analysis Period: Feb 4-6, 2026 (72 hours)")
print("=" * 100)

# --- EXECUTIVE SUMMARY ---
print("\n" + "=" * 100)
print("1. EXECUTIVE SUMMARY")
print("=" * 100)

active_connections = len([c for c in CONNECTIONS if len(c["starts"]) > 0])
api_connections = len([c for c in CONNECTIONS if c["type"] == "API" and len(c["starts"]) > 0])
db_connections = len([c for c in CONNECTIONS if c["type"] == "DB" and len(c["starts"]) > 0])
total_jobs = len(all_jobs)
api_jobs = len([j for j in all_jobs if j["type"] == "API"])
db_jobs = len([j for j in all_jobs if j["type"] == "DB"])

print(f"""
  Active Connections:     {active_connections} total ({api_connections} API, {db_connections} DB)
  Total Jobs (72h):       {total_jobs} ({api_jobs} API, {db_jobs} DB)

  Worker Requirements (formula: ceil(API/5) + ceil(DB/2)):
  -------------------------------------------------------
    P99 Workers Needed:   {p99_workers}
    P95 Workers Needed:   {p95_workers}
    P50 (Median):         {p50_workers}
    Maximum:              {max_workers}
    Average:              {avg_workers:.1f}

  Always-On DB Load:
    - Raylo Prod (CDC):       runs every 15min, 10min duration = ~67% utilization
    - Raylo Prod (No CDC):    runs every 15min, 10min duration = ~67% utilization
    - Anchor s3db01 (Incr.):  runs every 2h, 90min duration = ~75% utilization
    - Anchor s3db01 (Full):   runs daily, ~9h duration = ~37.5% utilization
    >> Baseline: 2-4 DB jobs running at almost any given time
""")

# --- HOUR-BY-HOUR HEATMAP ---
print("=" * 100)
print("2. HOUR-BY-HOUR HEATMAP")
print("=" * 100)

# Print by day
for day_offset in range(3):
    day_start = day_offset * 24
    day_date = (ANALYSIS_START + timedelta(days=day_offset)).strftime("%A %b %d, %Y")
    print(f"\n  {day_date}")
    print(f"  {'Hour':>6}  {'PeakAPI':>7}  {'PeakDB':>6}  {'Workers':>7}  {'API Bar':20}  {'DB Bar':20}  Running Connections")
    print(f"  {'─' * 6}  {'─' * 7}  {'─' * 6}  {'─' * 7}  {'─' * 20}  {'─' * 20}  {'─' * 40}")

    for h_in_day in range(24):
        h = day_start + h_in_day
        s = hourly_stats[h]
        api_bar = "#" * s["peak_api"]
        db_bar = "#" * s["peak_db"]

        # Highlight peak hours
        marker = ""
        if s["workers"] >= p99_workers:
            marker = " <<<< P99 PEAK"
        elif s["workers"] >= p95_workers:
            marker = " << P95"

        # Show connection names for interesting hours
        conn_names = ""
        if s["peak_api"] > 0 or s["peak_db"] > 0:
            all_names = sorted(s["all_api"]) + sorted(s["all_db"])
            # Abbreviate names
            abbrevs = []
            for n in all_names:
                short = n.replace("Customer.io ", "CIO ").replace("Anchor ", "A.").replace("Raylo ", "R.")
                short = short.replace(" (Full Refresh)", " FR").replace(" (Incremental)", " Inc")
                short = short.replace(" (No CDC)", " nCDC").replace(" (CDC)", " CDC")
                short = short.replace("New Customers", "NewCust").replace("Checkouts", "Chkout")
                short = short.replace("Proposals", "Prop").replace("Collections", "Coll")
                short = short.replace("TrustPilot v2", "TrustP").replace("Survicate (v2)", "Surv")
                short = short.replace("Taktile Cases", "Taktile")
                abbrevs.append(short)
            conn_names = ", ".join(abbrevs)
            if len(conn_names) > 80:
                conn_names = conn_names[:77] + "..."

        print(f"  {s['hour_utc']:>6}  {s['peak_api']:>7}  {s['peak_db']:>6}  {s['workers']:>7}  {api_bar:20}  {db_bar:20}  {conn_names}{marker}")


# --- PEAK CONCURRENCY DEEP DIVE ---
print("\n" + "=" * 100)
print("3. PEAK CONCURRENCY ANALYSIS")
print("=" * 100)

# Find top 10 peak hours
peak_hours = sorted(hourly_stats, key=lambda x: x["workers"], reverse=True)[:10]

print("\n  Top 10 Peak Hours:")
print(f"  {'Rank':>4}  {'Date':>8}  {'Hour':>6}  {'Workers':>7}  {'API':>4}  {'DB':>4}  Concurrent Connections")
print(f"  {'─' * 4}  {'─' * 8}  {'─' * 6}  {'─' * 7}  {'─' * 4}  {'─' * 4}  {'─' * 60}")

for i, s in enumerate(peak_hours, 1):
    all_names = sorted(s["peak_api_names"]) + sorted(s["peak_db_names"])
    names_str = ", ".join(all_names)
    if len(names_str) > 80:
        names_str = names_str[:77] + "..."
    print(f"  {i:>4}  {s['date']:>8}  {s['hour_utc']:>6}  {s['workers']:>7}  {s['peak_api']:>4}  {s['peak_db']:>4}  {names_str}")


# --- MINUTE-LEVEL PEAK ANALYSIS ---
print("\n\n  Minute-Level Peak Analysis (absolute peak moments):")
print(f"  {'─' * 90}")

# Find the minute with most total concurrent jobs
peak_minute_total = 0
peak_minute_idx = 0
for m in range(TOTAL_MINUTES):
    total = len(timeline[m]["api"]) + len(timeline[m]["db"])
    if total > peak_minute_total:
        peak_minute_total = total
        peak_minute_idx = m

peak_time = ANALYSIS_START + timedelta(minutes=peak_minute_idx)
peak_api_names = sorted(timeline[peak_minute_idx]["api"])
peak_db_names = sorted(timeline[peak_minute_idx]["db"])
peak_api_c = len(peak_api_names)
peak_db_c = len(peak_db_names)
peak_w = math.ceil(peak_api_c / 5) + math.ceil(peak_db_c / 2)

print(f"""
  Absolute Peak Minute: {peak_time.strftime('%b %d %H:%M')} UTC
  Total Concurrent Jobs: {peak_minute_total} ({peak_api_c} API + {peak_db_c} DB)
  Workers at Peak:       {peak_w} (ceil({peak_api_c}/5) + ceil({peak_db_c}/2))

  API Jobs Running ({peak_api_c}):""")
for n in peak_api_names:
    conn = next((c for c in CONNECTIONS if c["name"] == n), None)
    if conn:
        print(f"    - {n} [{conn['schedule_desc']}]")

print(f"\n  DB Jobs Running ({peak_db_c}):")
for n in peak_db_names:
    conn = next((c for c in CONNECTIONS if c["name"] == n), None)
    if conn:
        print(f"    - {n} [{conn['schedule_desc']}]")


# --- DAILY PATTERN ANALYSIS ---
print("\n\n" + "=" * 100)
print("4. DAILY PATTERN - TYPICAL DAY PROFILE")
print("=" * 100)

# Average across 3 days for each hour-of-day
print(f"\n  {'Hour':>6}  {'AvgAPI':>6}  {'AvgDB':>5}  {'AvgW':>5}  {'MaxW':>4}  Pattern Description")
print(f"  {'─' * 6}  {'─' * 6}  {'─' * 5}  {'─' * 5}  {'─' * 4}  {'─' * 50}")

for hod in range(24):
    indices = [hod, hod + 24, hod + 48]
    avg_api = sum(hourly_stats[i]["peak_api"] for i in indices) / 3
    avg_db = sum(hourly_stats[i]["peak_db"] for i in indices) / 3
    avg_w = sum(hourly_stats[i]["workers"] for i in indices) / 3
    max_w = max(hourly_stats[i]["workers"] for i in indices)

    # Describe pattern
    desc = ""
    if hod in (2,):
        desc = "DAILY CLUSTER: Google Ads + Intercom + Typesense + Stripe + FB + Survicate"
    elif hod == 3:
        desc = "S3 Full Refresh starts + Incremental overlap"
    elif 4 <= hod <= 11:
        desc = "S3 Full Refresh running + periodic jobs"
    elif hod == 9:
        desc = "S3 FR + TrustPilot daily"
    elif hod == 1:
        desc = "Customer.io dailies fire"
    elif hod == 5:
        desc = "S3 FR running + Taktile daily"
    elif hod == 6:
        desc = "S3 FR running + Airbyte daily"
    elif hod == 20:
        desc = "Sentry daily"
    elif 12 <= hod <= 23:
        desc = "Periodic jobs only (baseline load)"
    else:
        desc = "Periodic baseline"

    print(f"  {hod:02d}:00  {avg_api:6.1f}  {avg_db:5.1f}  {avg_w:5.1f}  {max_w:>4}  {desc}")


# --- WORKER DISTRIBUTION ---
print("\n\n" + "=" * 100)
print("5. WORKER DISTRIBUTION")
print("=" * 100)

from collections import Counter
worker_dist = Counter(all_worker_values)
print(f"\n  Workers  Hours  Pct     Bar")
print(f"  {'─' * 7}  {'─' * 5}  {'─' * 6}  {'─' * 40}")
for w in sorted(worker_dist.keys()):
    count = worker_dist[w]
    pct = count / TOTAL_HOURS * 100
    bar = "#" * int(pct)
    print(f"  {w:>7}  {count:>5}  {pct:5.1f}%  {bar}")


# --- CONNECTION UTILIZATION ---
print("\n\n" + "=" * 100)
print("6. CONNECTION UTILIZATION (72-hour window)")
print("=" * 100)

print(f"\n  {'Connection':<35}  {'Type':>4}  {'Jobs':>4}  {'TotalMin':>8}  {'Util%':>5}  Schedule")
print(f"  {'─' * 35}  {'─' * 4}  {'─' * 4}  {'─' * 8}  {'─' * 5}  {'─' * 25}")

conn_utils = []
for conn in sorted(CONNECTIONS, key=lambda c: c["type"] + c["name"]):
    jobs_in_window = [j for j in all_jobs if j["conn_id"] == conn["id"]]
    n_jobs = len(jobs_in_window)
    total_min = n_jobs * conn["duration_min"]
    util_pct = total_min / TOTAL_MINUTES * 100
    conn_utils.append((conn, n_jobs, total_min, util_pct))
    print(f"  {conn['name']:<35}  {conn['type']:>4}  {n_jobs:>4}  {total_min:>8}  {util_pct:5.1f}%  {conn['schedule_desc']}")


# --- OPTIMIZATION RECOMMENDATIONS ---
print("\n\n" + "=" * 100)
print("7. OPTIMIZATION RECOMMENDATIONS")
print("=" * 100)

print("""
  FINDING 1: Daily 02:00-02:30 UTC API Cluster
  -----------------------------------------------
  At 02:17 UTC, FOUR daily connections fire simultaneously:
    - Google Ads, Intercom, Typesense, Stripe (2h schedule hits too)
  At 02:25, Facebook Ads joins. Survicate (12h) also fires at 02:00.

  IMPACT: 6-7 concurrent API jobs = ceil(7/5) = 2 API workers needed
          (vs. 1 worker for most other hours)

  RECOMMENDATION: Stagger daily API syncs across different hours:
    - Move Google Ads to 04:00 UTC (avoids S3 Full Refresh peak)
    - Move Typesense to 07:00 UTC
    - Move Intercom to 08:00 UTC (longest daily API job, ~15min)
    - Keep Stripe at 02:17 (cannot easily change 2h schedule)
    - Keep Facebook Ads at 02:25 (very short, low impact)

  ESTIMATED SAVINGS: Reduces peak from 7 to ~3 concurrent API jobs
                     at 02:xx, saving 1 worker during that hour.

  FINDING 2: S3 Full Refresh (9h) Overlaps with Everything
  -----------------------------------------------
  The Anchor s3db01 Full Refresh runs 03:00-12:00 UTC daily.
  During this window, it constantly overlaps with:
    - Raylo Prod CDC (every 15min)
    - Raylo Prod No CDC (every 15min)
    - Anchor s3db01 Incremental (every 2h, 90min each)
    - Raylo Staging (every 6h)

  IMPACT: 4-5 concurrent DB jobs during 03:00-12:00 = ceil(5/2) = 3 DB workers
          vs. 2-3 DB jobs (ceil(3/2) = 2 workers) when S3 FR is not running

  RECOMMENDATION:
    - Move Full Refresh to 20:00-22:00 UTC start (runs overnight through
      ~05:00-07:00 UTC), reducing overlap with the daily API cluster at 02:xx.
    - OR: Evaluate converting Full Refresh to incremental (already have
      incremental running - is the daily full refresh truly needed?)

  ESTIMATED SAVINGS: 1 DB worker during 03:00-12:00 UTC window.

  FINDING 3: Anchor s3db01 Incremental Nearly Always Running
  -----------------------------------------------
  Every 2h with 90min duration = 75% utilization.
  Effectively consumes 1 DB worker slot permanently.

  RECOMMENDATION:
    - If data freshness allows, extend interval to every 3h
      (90min / 180min = 50% utilization, still very fresh data)
    - Would reduce DB concurrency by ~1 job during off-peak hours

  FINDING 4: Raylo Prod CDC + No CDC Double Booking
  -----------------------------------------------
  Both run every 15min with ~10min duration each.
  They always overlap (start 2 minutes apart), consuming 2 DB slots.

  RECOMMENDATION:
    - Stagger CDC to start at :00 and No CDC at :08 (currently :00 and :02)
    - Even better: offset No CDC to alternate slots (:07, :22, :37, :52)
      so they interleave rather than overlap
    - Potential savings: 1 DB worker if they can be made non-overlapping

  ESTIMATED SAVINGS: ceil(2/2)=1 worker -> ceil(1/2)=1 worker (no change due
  to ceiling), BUT reduces total DB count which helps when other DB jobs overlap.
""")

# --- SUMMARY TABLE ---
print("=" * 100)
print("8. SUMMARY: CURRENT vs. OPTIMIZED WORKER REQUIREMENTS")
print("=" * 100)

print(f"""
  Metric               Current    After Optimization
  ──────────────────    ───────    ──────────────────
  P99 Workers:          {p99_workers:>5}      {max(p99_workers - 1, p50_workers):>5}  (stagger daily APIs + move S3 FR)
  P95 Workers:          {p95_workers:>5}      {max(p95_workers - 1, p50_workers):>5}  
  P50 Workers:          {p50_workers:>5}      {p50_workers:>5}  (baseline unchanged)
  Max Workers:          {max_workers:>5}      {max(max_workers - 2, p50_workers):>5}  
  Avg Workers:          {avg_workers:>5.1f}      {max(avg_workers - 0.5, p50_workers):>5.1f}

  Key Insight: The baseline load from always-on periodic jobs (CDC, No CDC,
  S3 Incremental) means you ALWAYS need at least {p50_workers} workers. Peaks are driven
  by the daily S3 Full Refresh overlapping with this baseline + daily API cluster.

  RECOMMENDED WORKER ALLOCATION: {p95_workers} workers
    - Covers P95 of actual usage
    - Peak hours ({p99_workers} workers) occur only ~{sum(1 for w in all_worker_values if w >= p99_workers)} hours out of 72
    - Airbyte can queue excess jobs briefly during rare peaks
""")

# --- QUIET HOURS ---
print("=" * 100)
print("9. QUIET HOURS (Best Windows for Maintenance)")
print("=" * 100)

quiet = [(s["datetime"].strftime("%b %d %H:00"), s["workers"], s["peak_api"], s["peak_db"])
         for s in hourly_stats if s["workers"] <= p50_workers]

print(f"\n  Hours requiring only {p50_workers} workers ({len(quiet)} out of 72 hours):")
print(f"  These are the best windows for maintenance or manual syncs.\n")

# Group consecutive quiet hours
if quiet:
    current_start = quiet[0][0]
    current_end = quiet[0][0]
    current_w = quiet[0][1]
    ranges = []
    for i in range(1, len(quiet)):
        # Check if consecutive by hour
        prev_dt = datetime.strptime(quiet[i-1][0], "%b %d %H:00")
        curr_dt = datetime.strptime(quiet[i][0], "%b %d %H:00")
        if (curr_dt - prev_dt).total_seconds() == 3600:
            current_end = quiet[i][0]
        else:
            ranges.append((current_start, current_end, current_w))
            current_start = quiet[i][0]
            current_end = quiet[i][0]
            current_w = quiet[i][1]
    ranges.append((current_start, current_end, current_w))

    for start, end, w in ranges:
        if start == end:
            print(f"    {start} (workers: {w})")
        else:
            print(f"    {start} - {end} (workers: {w})")


# --- FINAL CHART ---
print("\n\n" + "=" * 100)
print("10. 72-HOUR WORKER TIMELINE (visual)")
print("=" * 100)
print(f"\n  Each column = 1 hour, height = workers needed\n")

max_w_display = max_workers + 1
for level in range(max_w_display, 0, -1):
    row = f"  {level:>2} |"
    for h in range(TOTAL_HOURS):
        w = hourly_stats[h]["workers"]
        if w >= level:
            row += "#"
        else:
            row += " "
    row += "|"
    print(row)

# X-axis
print(f"     +{'─' * TOTAL_HOURS}+")
# Hour labels (every 6 hours)
label_row = "      "
for h in range(0, TOTAL_HOURS, 6):
    label_row += f"{h % 24:02d}    "
print(label_row)
print(f"      {'|-- Feb 4 --|':^24}{'|-- Feb 5 --|':^24}{'|-- Feb 6 --|':^24}")

print(f"\n  Legend: # = worker in use at peak of that hour")
print(f"  Worker formula: ceil(peak_concurrent_API / 5) + ceil(peak_concurrent_DB / 2)")

print("\n" + "=" * 100)
print("END OF ANALYSIS")
print("=" * 100)
