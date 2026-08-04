---
name: worker-analysis
description: >
  Analyze Airbyte Data Worker usage, estimate worker needs for prospects, or optimize sync schedules.
  Use when someone asks "how many workers do I need", "analyze worker usage", "worker capacity",
  "reduce worker usage", or "queue risk".
---

# Airbyte Worker Analysis

Analyze, estimate, and optimize Airbyte Data Worker usage.

## SE Suite Configuration

Before running workspace or Metabase analysis, read `.se-config.yaml` and use the `worker_analysis` block:

```yaml
worker_analysis:
  bigquery_project: "<your-bigquery-project>"   # default: airbyte-data-prod
  bigquery_dataset: "<your-dataset>"             # default: airbyte_warehouse
  metabase_database_id: 2                         # default billing database
  datadog_dashboard_url: "<optional-dashboard>"
  airbyte_cloud_client_id: "<client-id>"
  airbyte_cloud_client_secret: "<client-secret>"
```

All SQL examples below use `{bigquery_project}.{bigquery_dataset}` for project-qualified tables and `{bigquery_dataset}` for dataset-only tables. Substitute from config before running queries. If the config block is missing, ask the user for these values rather than guessing.

## Billing Formula (Ground Truth)

The platform computes data workers from CPU resource requests, NOT from connection counts or connector types:

**Formula: (maxSourceCPU + maxDestinationCPU + maxOrchestratorCPU) / 8 = Data Workers**

Each sync job tracks three CPU components: source connector CPU, destination connector CPU, and orchestrator CPU. These are summed into hourly buckets and the maximum within each bucket determines the data workers for that hour. There is no distinction between API and DB connectors in the billing formula.

Source: Airbyte platform `DataWorkerUsage` billing logic (internal Kotlin source in the `DataWorkerUsage` entity).

### How Hourly Bucketing Works

1. When a sync job starts, its CPU requirements (source, destination, orchestrator) are added to the current hourly bucket
2. When a sync job completes, those CPU values are subtracted from the current hourly bucket
3. Each hourly bucket tracks both current and max CPU values per component
4. Billing uses the max values: `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`

### Connection-Level CPU Overrides (`cpu_request`)

Some connections have a `cpu_request` override that increases CPU allocated per sync. Critical rules:

- The `cpu_request` override **generally applies to all 3 containers** (source, destination, AND orchestrator)
- **Exceptions:** MySQL and Oracle connectors — the orchestrator retains its default (1.0) regardless of override
- For most connectors (Postgres, MSSQL, Salesforce, S3, SAP HANA, ServiceNow, etc.): `cpu_request=N` → N on all 3 containers
- Database connectors (MySQL, Postgres, MSSQL, MongoDB, etc.): default = 1.0 (src) + 1.0 (dst) + 1.0 (orch) = 3.0 total
- API connectors (Salesforce, HubSpot, Stripe, etc.): default = 0.8 (src) + 0.3 (dst) + 0.3 (orch) = 1.4 total

**CPU per sync with overrides:**

- `cpu_request=2.0` (most connectors): 2.0 (src) + 2.0 (dst) + 2.0 (orch) = **6.0 total**
- `cpu_request=3.0` (most connectors): 3.0 (src) + 3.0 (dst) + 3.0 (orch) = **9.0 total**
- `cpu_request=2.0` (MySQL/Oracle): 2.0 (src) + 2.0 (dst) + 1.0 (orch) = **5.0 total**
- `cpu_request=3.0` (MySQL/Oracle): 3.0 (src) + 3.0 (dst) + 1.0 (orch) = **7.0 total**

**ALWAYS verify actual per-container values via Datadog — do not assume:** `avg:kubernetes.cpu.requests{ab_connection_id:<ID>} by {kube_container_name}`

To identify connections with CPU overrides, query BigQuery:

```sql
SELECT c.connection_id, c.connection_name, c.cpu_request,
       c.source_connector_name, c.destination_connector_name
FROM `{bigquery_project}.{bigquery_dataset}.connection` c
WHERE c.organization_id = '<ORG_ID>'
  AND c.connection_status = 'active'
  AND c.cpu_request IS NOT NULL
ORDER BY c.cpu_request DESC
```

For a full CPU bump impact analysis (DW comparison, risk assessment, charts), use the `references/analyze-cpu-bumps.md` playbook in this skill directory.

### Known Bug Fix: CPU Leak at Hour Boundaries (March 2026)

A 2026 platform bug fix resolved a case where the hourly-bucket UPDATE targeted the current hour while the SELECT could match the previous hour, causing CPU values to be permanently "leaked" into the carry-forward system. Use the documented `bucket_start <= :bucketStart` / `DATE_TRUNC('hour', :bucketStart)` behavior to explain why hour-boundary leaks can occur.

The fix checks the affected row count and falls back to creating a new bucket when 0 rows are updated. If analyzing historical data for a customer that shows a persistent phantom CPU floor, this bug may be the cause. A separate SQL migration was planned to reset accumulated phantom values for affected organizations.

## Estimation Model (For OSS and Prospects)

When CPU-level billing data is not available (OSS instances, prospects), use the API/DB concurrency estimation model as a heuristic:

**Estimation Formula: ceil(Concurrent API jobs / 5) + ceil(Concurrent DB jobs / 2) = Estimated Workers**

This is an approximation that does NOT match the billing formula exactly. It is useful for rough sizing but should always be caveated as an estimate. Once a customer is on Cloud, Metabase billing data is the source of truth.

## What You Can Do

Tell me what you need and I'll figure out the right approach:

- **"Analyze [customer name]'s worker usage"** — I'll query Metabase billing data and/or the Airbyte Cloud API
- **"Analyze workspace [ID]"** — I'll fetch connections and job history directly from the Cloud API
- **"Here's an OSS export [JSON]"** — I'll parse it and estimate workers from job overlaps
- **"Estimate workers for a prospect with 50 connections"** — I'll run a statistical estimate
- **"How can [customer] reduce their worker usage?"** — I'll identify peak hours and suggest reschedules
- **"Generate a report for [customer]"** — I'll create **two** PDF reports: a customer-facing executive report and an internal detailed report
- **"What's the queue risk for [customer]?"** — I'll run a queue risk simulation using historical hourly data
- **"Will [customer] have queueing issues under enforcement?"** — Same as above, with enforcement context
- **"[Customer] is being enforced, analyze their usage"** — Full analysis with enforcement-aware optimization and reporting
- **"Generate an enforcement report for [customer]"** — Two PDFs with enforcement analysis page included in the internal report

## Capacity Enforcement Mode

Airbyte now enforces data worker limits — syncs queue when an org's usage meets or exceeds committed capacity. This changes analysis from billing-focused to reliability-focused.

### How Enforcement Works

- **Feature flag:** `platform.enforce-data-worker-capacity` (org-scoped, LaunchDarkly)
- **Admission check:** `current_data_workers + required_data_workers <= committed_data_workers`
- **Required DW per job:** `(source_cpu + destination_cpu + orchestrator_cpu) / 8` (fallback: src=1.0, dest=1.0, orch=0.5 → 0.3125 DW)
- **Queued jobs do NOT reserve capacity** — they poll every minute
- **On-demand bypass:** connections with `onDemandEnabled=true` skip the queue
- **Cancellation:** manual syncs cancel after 8 hours in queue; scheduled syncs cancel at next scheduled run

### When to Activate Enforcement-Aware Analysis

Activate enforcement mode when ANY of these conditions are met:

1. **User explicitly asks** — e.g., "analyze with enforcement", "queue risk for [customer]", "will [customer] have queueing issues?"
2. **Customer matches an enforcement rollout cohort** — use the internal enforcement rollout tracker or the user's explicit declaration

When enforcement mode is active:
- Queue Risk Analysis is included (see "Queue Risk Analysis" section below)
- Optimization recommendations are reframed around preventing queueing (see "Enforcement-Aware Optimization" section)
- Internal reports include an enforcement analysis page (see "Internal Report: Enforcement Analysis Page" section)

### Enforcement Rollout Phase Lists

Customer-specific rollout cohort lists are maintained in the internal Airbyte enforcement rollout tracker and are not reproduced here. When the LaunchDarkly MCP auth is fixed, auto-detection should replace manual lookup. Until then, if a customer isn't on a known list but the user says enforcement is active, trust the user.

### Known Gaps (v1 Limitations)

| Gap | Status | Handling |
|---|---|---|
| `data_worker_usage_reservation` table | Not deployed to prod yet | Skip. Future enhancement when available. |
| Enforcement flag auto-detection | LaunchDarkly MCP auth broken; no API in any MCP | Use explicit user declaration or phase list lookup. |
| On-demand capacity breakdown | No accessible data source | Skip for v1. Future enhancement. |
| `get_data_worker_availability` endpoint | Not in any MCP | Skip. |
| `contracted_data_workers` from Stigg | Backfill in progress, Salesforce is current source | Use existing field as-is; may lag contract changes. |

## How It Works

### For Existing Customers (Metabase-First)

When you have a customer name or organization ID:

1. **Query Metabase** (authoritative billing data) using `mcp__metabase__execute_query` with `database_id=2`:

   **Account info:**
   ```sql
   SELECT account_name_masked, organization_id, account_owner, salesforce_arr, account_type
   FROM airbyte_warehouse.account
   WHERE organization_id = '{org_id}'
   ```

   **Daily worker usage (billing data):**
   ```sql
   SELECT worker_usage_date, contracted_data_workers, max_data_workers_used, workspaces
   FROM airbyte_warehouse.organization_data_worker_usage_daily
   WHERE organization_id = '{org_id}'
   ORDER BY worker_usage_date DESC
   LIMIT 30
   ```

   **Hourly worker patterns (last 7 days):**
   ```sql
   SELECT worker_usage_day_hour, data_workers_used,
          source_data_workers, destination_data_workers,
          orchestrator_data_workers, workspace_name_masked
   FROM airbyte_warehouse.workspace_data_worker_usage_hourly
   WHERE organization_id = '{org_id}'
   AND worker_usage_day_hour >= DATE_SUB(CURRENT_DATE(), INTERVAL 7 DAY)
   ORDER BY worker_usage_day_hour DESC
   LIMIT 200
   ```

2. If Metabase returns data, use `max_data_workers_used` as ground truth (this is the billing meter)
3. If no Metabase data, fall back to API job overlap analysis using the estimation model

### For Workspace Analysis (API-Based)

When you have a workspace ID:

1. Fetch connections using `mcp__pyairbyte__list_deployed_cloud_connections`
2. For each connection, fetch job history with `mcp__pyairbyte__list_cloud_sync_jobs`
3. Classify each connector as API or DATABASE (file connectors like S3, GCS, SFTP count as DATABASE)
4. For UNKNOWN connectors, use `mcp__pyairbyte__get_connector_info` to classify
5. Calculate peak concurrent jobs at each minute using job start/end times
6. Apply estimation formula: `ceil(Peak Concurrent API / 5) + ceil(Peak Concurrent DB / 2)`
7. Use P99 (99th percentile) of hourly workers as the estimated billing metric
8. **Caveat**: This is an estimation. The actual billing formula uses CPU values, not connection type counts. Results may differ from actual billing.

### For OSS Exports (JSON Input)

When the user provides a JSON export from an OSS instance, it will come from one of two export scripts. Identify the format and analyze accordingly:

**Enhanced format** (has `jobs` array with individual job details — preferred for accuracy):
```json
[
  {
    "name": "Salesforce -> Snowflake",
    "connectionId": "abc-123",
    "job_count": 20,
    "avg_hours": 1.02,
    "pattern": "1.0h interval",
    "last_run": "2026-03-08T12:00:00+00:00",
    "avg_duration_seconds": 342.5,
    "jobs": [
      {
        "job_id": "job-1",
        "start_time": "2026-03-08T12:00:00+00:00",
        "end_time": "2026-03-08T12:05:42+00:00",
        "duration_seconds": 342,
        "status": "succeeded"
      }
    ]
  }
]
```

**Legacy format** (summary only, no individual job details):
```json
[
  {
    "name": "Postgres -> BigQuery",
    "connectionId": "def-456",
    "job_count": 15,
    "avg_hours": 24.0,
    "pattern": "Daily",
    "last_run": "2026-03-07T02:00:00+00:00",
    "avg_duration_seconds": 180.0
  }
]
```

**Analysis approach by format:**

1. **If `jobs` array is present** (enhanced format): Run job overlap analysis for worker estimation
   - For each connection, extract all job `start_time` and `end_time` pairs
   - Build a timeline of all jobs across all connections
   - At each minute, count concurrent API and DB jobs
   - Apply estimation formula: `ceil(Peak Concurrent API / 5) + ceil(Peak Concurrent DB / 2)`
   - Use P99 of hourly worker values as the billing estimate
   - **Caveat**: This uses the estimation model, not the actual CPU-based billing formula

2. **If no `jobs` array** (legacy format): Use statistical estimation with the empirical duration table
   - Map `pattern` field to frequency bucket: "Multiple runs/hour" -> sub-hourly, "Xh interval" -> use X, "Daily" -> 24h, "Weekly" -> 168h
   - Use `avg_duration_seconds` from the export if available (more accurate than fleet defaults)
   - Fall back to empirical duration defaults from the table above if `avg_duration_seconds` is null
   - Calculate concurrency per connection: `avg_duration_seconds / (avg_hours * 3600)`
   - Sum concurrent jobs by type, apply formula
   - **Warn the user** that this is a statistical estimate — the enhanced export script provides more accurate results

3. **Classify connectors as API or DATABASE** from the connection `name` field:
   - Common API sources: Salesforce, HubSpot, Stripe, Zendesk, Jira, GitHub, Slack, Google Ads, Facebook Marketing, Shopify, Intercom, Marketo
   - Common DB sources: Postgres, MySQL, MSSQL, Oracle, MongoDB, CockroachDB, TiDB, ClickHouse
   - File sources (S3, GCS, SFTP, Azure Blob) count as DATABASE
   - If unclear from the name, default to API (conservative — API has the lower concurrency divisor of 5)

### For Prospect Estimation

When estimating for a new prospect, ask:

1. Total number of connections expected
2. Percentage that are Database/File vs API connectors
3. Percentage running sub-hourly / hourly / daily
4. (Optional) Average sync duration in minutes
5. (Optional) Maintenance window hours

Then estimate peak concurrency using the empirical sync duration defaults below, and apply the estimation formula.

**Empirical Sync Duration Defaults** (based on Airbyte Cloud data, last 30 days, median/avg/P90 in minutes):

| Connector Type | Frequency | Median | Avg | P90 |
|---|---|---|---|---|
| API | Hourly | 2.9 | 5.5 | 10.2 |
| API | Every 6h | 3.4 | 11.5 | 18.9 |
| API | Every 12h | 3.3 | 17.3 | 19.9 |
| API | Every 24h | 3.5 | 15.8 | 24.5 |
| Database | Hourly | 1.7 | 3.3 | 6.8 |
| Database | Every 6h | 2.1 | 4.5 | 11.0 |
| Database | Every 12h | 3.0 | 6.5 | 14.0 |
| Database | Every 24h | 2.1 | 4.5 | 5.8 |

**How to estimate concurrency**: For each frequency group, calculate `connections * (avg_duration / schedule_interval)`. This gives the expected number of concurrent jobs at any point. Use avg duration for moderate estimates or P90 for conservative estimates.

**Example**: 45 hourly API connections -> 45 * (5.5 / 60) = ~4.1 concurrent API jobs. 30 hourly DB connections -> 30 * (3.3 / 60) = ~1.7 concurrent DB jobs. Workers = ceil(4.1 / 5) + ceil(1.7 / 2) = 1 + 1 = **2 workers**.

Always caveat that this is an estimate — once on the platform, actual CPU-based billing data is more accurate.

### Queue Risk Analysis (Enforcement)

This is the core enforcement capability. It uses existing BigQuery hourly data to simulate enforcement impact.

**When to run:** Enforcement mode is active (user requested or customer matches a phase list).

**Step 1 — Query hourly overflow data (last 30 days):**

```sql
SELECT
  worker_usage_day_hour,
  SUM(data_workers_used) AS data_workers_used,
  MAX(contracted_data_workers) AS contracted_data_workers,
  SUM(data_workers_used) - MAX(contracted_data_workers) AS overflow_workers
FROM airbyte_warehouse.workspace_data_worker_usage_hourly
WHERE organization_id = '{org_id}'
AND worker_usage_day_hour >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY worker_usage_day_hour
ORDER BY worker_usage_day_hour
```

**Step 2 — Query daily exceedance count:**

```sql
SELECT
  COUNT(*) AS total_days,
  COUNT(CASE WHEN max_data_workers_used >= contracted_data_workers THEN 1 END) AS days_exceeded,
  MAX(max_data_workers_used) AS peak_max_workers,
  MAX(contracted_data_workers) AS contracted_workers
FROM airbyte_warehouse.organization_data_worker_usage_daily
WHERE organization_id = '{org_id}'
AND worker_usage_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 30 DAY)
```

**Step 3 — Calculate metrics:**

- **Queue Risk Score:** `(hours where data_workers_used >= contracted_data_workers) / total_hours × 100`
  - < 5% = Low risk (occasional spikes, unlikely to cause sustained queueing)
  - 5–20% = Moderate risk (regular peak-hour queueing likely)
  - > 20% = High risk (frequent queueing, optimization or upgrade needed)
- **Peak Overflow:** max(`data_workers_used - contracted_data_workers`) — how far over the limit usage goes at worst
- **Phase Classification:** days exceeded → Phase 1 (0), Phase 2 (1–14), Phase 3 (15–29), Phase 4 (30)
- **Queue Risk by Hour of Day:** aggregate overflow hours by hour-of-day (0–23 UTC) to identify which hours carry the most queue risk
- **Estimated Queue Impact:** for hours where usage exceeds capacity, any new sync arriving would queue. Estimate queued syncs as: connections scheduled during overflow hours × (overflow_minutes / 60)

**Step 4 — Identify connections at highest queue risk:**

Using Coral MCP tools (`list_deployed_cloud_connections`, `describe_cloud_connection`), cross-reference:
- Which connections are scheduled to run during the highest-overflow hours
- Sort by: (a) overlap with overflow hours, then (b) estimated DW cost per sync (use default 0.3125 DW if actual CPU unknown)

**Caveats to always include in queue risk output:**
- "This analysis uses historical hourly data rebuilt twice daily. It simulates enforcement impact but does not reflect real-time admission decisions."
- "Actual queueing behavior depends on live CPU reservations, which may differ from hourly aggregated data."
- "The contracted_data_workers value is currently sourced from Salesforce and may not reflect very recent contract changes."

### Workload Queue Analysis (Conditional/Supplementary)

If `workload_queue` data is available in Metabase (database_id=72), you can query actual queue events for post-enforcement impact analysis.

**Note:** The `workload_queue` table does not have an `organization_id` column. You MUST join through `workload_id` → `jobs` table to scope results to the target org. If the join is not possible, clearly label all results as **platform-wide metrics (not customer-specific)** in the internal report.

**Preferred query (org-scoped via join):**

```sql
SELECT
  wq.workload_id,
  wq.created_at AS queued_at,
  wq.acked_at AS dequeued_at,
  EXTRACT(EPOCH FROM (wq.acked_at - wq.created_at)) AS queue_duration_seconds,
  wq.dataplane_group,
  wq.priority
FROM workload_queue wq
JOIN jobs j ON j.id = wq.workload_id
JOIN connection c ON c.id = j.scope_id
JOIN workspace w ON w.id = c.workspace_id
WHERE w.organization_id = '{org_id}'
AND wq.created_at >= NOW() - INTERVAL '30 days'
ORDER BY wq.created_at DESC
LIMIT 500
```

**Fallback query (platform-wide, if join fails):**

```sql
SELECT
  wq.workload_id,
  wq.created_at AS queued_at,
  wq.acked_at AS dequeued_at,
  EXTRACT(EPOCH FROM (wq.acked_at - wq.created_at)) AS queue_duration_seconds,
  wq.dataplane_group,
  wq.priority
FROM workload_queue wq
WHERE wq.created_at >= NOW() - INTERVAL '30 days'
ORDER BY wq.created_at DESC
LIMIT 500
-- WARNING: This query returns ALL orgs. Label results as platform-wide in the report.
```

**Important:** This table is newly synced and may have gaps. It has not been confirmed whether it specifically tracks capacity enforcement queue events vs. general workload scheduling. Use results as supplementary evidence alongside the hourly simulation, not as the sole source of queue analysis.

If using the fallback query, the "Actual Queue Events" section on the Enforcement Analysis page MUST be labeled: "Platform-wide queue metrics (not scoped to this organization)".

## Critical Rules

### Use Metabase Billing Data When Available

For Cloud customers, Metabase data reflects the actual CPU-based billing formula and is always the source of truth. Only use the API/DB estimation model when CPU-based data is unavailable (OSS, prospects).

### Use Job Overlap Analysis for Estimation, NOT Connection Counts

**WRONG** (overestimates): Count total API + DB connections, apply formula
- Example: 34 API + 27 DB = ceil(34/5) + ceil(27/2) = 21 workers

**CORRECT** (better estimate): Find peak CONCURRENT jobs, apply formula
- Example: Peak at 00:00 UTC = 6 API + 11 DB running simultaneously
- Calculation: ceil(6/5) + ceil(11/2) = 2 + 6 = 8 workers

### Applicable Plans
- Data worker usage is tracked for: **Pro, Flex (Enterprise Flex), and SME (Self Managed Enterprise)** plans
- All applicable plans use the same billing formula
- Do NOT ask for plan type — there is only one model

### Job Duration Data
- Use the Airbyte API `GET /jobs/{jobId}` endpoint which returns `start_time` and `duration` fields
- Do NOT estimate durations from bytes_synced or log timestamps

## Customer Name Lookup

Known customer-to-org-ID mappings are maintained in the worker-toolkit repository. If you can't find a customer, search Metabase:

```sql
SELECT organization_id, account_name_masked
FROM airbyte_warehouse.account
WHERE LOWER(account_name_masked) LIKE LOWER('%{search_term}%')
LIMIT 10
```

## Optimization Recommendations

When asked to optimize worker usage, the approach depends on whether enforcement mode is active.

### Non-Enforced (Default Behavior)

When enforcement mode is NOT active, use the existing cost-focused approach:

1. Identify peak hours from Metabase hourly data or API job overlap analysis
2. Find which connections run during peak hours
3. Sort connections by duration (longest first)
4. Suggest minimum number of reschedules to get peak below contracted workers
5. Generate specific Quartz cron expressions for recommended schedules
6. Show before/after comparison
- Frame as: "Rescheduling these connections could reduce peak from X to Y workers"

### Enforced or Enforcement-Imminent

When enforcement mode IS active, reframe optimization recommendations around **preventing sync queueing** rather than reducing cost:

1. **Lead with queue impact:** "During hours HH:00–HH:00 UTC, usage exceeds your contracted N workers by X.X. Syncs arriving during these windows will queue until capacity is available."
2. **Quantify:** "In the last 30 days, approximately N hours (X% of total) showed usage above contracted capacity."
3. **Recommend:** "Staggering these connections to off-peak hours would bring peak usage below contracted capacity, preventing queueing."
4. **Priority:** Sort by **queue risk** (connections scheduled during highest-overflow hours first), then by estimated DW cost per sync
5. Still generate Quartz cron expressions for internal reports
6. Still use natural language only for customer-facing reports

### Generating Specific Schedule Recommendations

When producing scheduling recommendations, do the following:

1. **Analyze the actual sync schedule** from connection data — identify which connections fire at the same time
2. **Identify peak concurrency windows** — find the top 3-5 hours where the most workers are consumed
3. **Generate specific cron expressions** that would spread load more evenly:
   - Identify connections that can be safely moved (not part of a data dependency chain)
   - Suggest new start times that avoid the peak windows
   - Provide Quartz cron syntax (e.g., `0 15 3 * * ?` for 3:15 AM daily)
4. **Show before/after comparison**: current peak worker usage vs. projected usage with recommended schedule
5. **For customer-facing reports**: Express recommendations in natural language only (e.g., "Consider moving Connection X from 2:00 PM to 6:00 AM to reduce peak usage by ~0.3 workers"). No cron syntax, no internal details.
6. **For internal reports**: Include the full cron expressions, per-connection impact analysis, and before/after concurrency charts

---

## Report Generation

When asked to generate a report, **always produce two separate PDF files**:

1. **Customer-Facing Executive Report** (3-4 pages) — safe to share externally
2. **Internal Detailed Report** (8 pages) — for Airbyte internal use only

Both reports are generated on every run. The customer report is the one to share with the customer. The internal report stays within the Airbyte team.

### Data Sanitization (CRITICAL)

Before generating the customer-facing report, ALL data MUST pass through the `DataSanitizer`. This is a hard requirement that prevents internal data leakage regardless of runtime environment.

#### Customer-Facing Allowlist

Only these data fields may appear in customer-facing reports:

```python
CUSTOMER_ALLOWED_FIELDS = {
    "customer_name",
    "report_date",
    "analysis_period",
    "deployment_type",
    "contracted_workers",
    "p99_usage",
    "average_usage",
    "peak_usage",
    "headroom",
    "usage_status",               # green/yellow/red
    "hourly_usage_timeline",      # aggregated hourly data points (total workers only)
    "peak_activity_hours",
    "low_activity_hours",
    "daily_averages",             # day-of-week averages
    "connection_count",
    "total_worker_usage",         # per-connection total only -- NEVER component breakdown
    "optimization_suggestions",   # natural language only -- no cron, no formulas
    "growth_recommendations",
}
```

#### Customer-Facing Blocklist

If ANY of these patterns appear in customer report output data, **raise an error** (do not silently pass). This surfaces the issue during development/testing rather than silently producing a bad report.

```python
CUSTOMER_BLOCKED_PATTERNS = [
    r"maxSrcCPU|maxDstCPU|maxOrchCPU|maxSourceCPU|maxDestCPU|maxOrchestratorCPU",
    r"ceil\(",
    r"source.{0,5}worker|destination.{0,5}worker|orchestrator.{0,5}worker",
    r"src.{0,5}cpu|dst.{0,5}cpu|orch.{0,5}cpu",
    r"0\.33.{0,10}worker",
    r"estimation.{0,5}model",
    r"confidence.{0,5}interval",
    r"metabase",
    r"workspace.{0,3}id",
    r"billing.{0,5}formula",
    r"cpu.{0,5}formula",
    r"internal.{0,5}calculation",
    r"granola|se.toolkit",
    r"organization_id",
    r"heuristic",
    r"concurrent.{0,5}(api|db)",
    r"enforc(e[d]?|ing|ement)",
    r"queue[d]?|queueing|queuing",
    r"admission",
    r"reservation",
    r"\bon[\s._-]?demand",
    r"capacity.*(limit|gate|block|cap\b)",
    r"rollout.*(phase|cohort)",
    r"launch.?darkly|stigg",
    r"workload_queue",
]
```

#### DataSanitizer Implementation

When building the report generation script, include this sanitizer class. It MUST be used to filter all data before it reaches the customer report generator.

```python
import re


class DataSanitizer:
    """Enforces allowlist/blocklist for customer-facing reports.

    Raises ValueError if blocked content is detected -- this surfaces
    the issue during development/testing rather than silently producing
    a bad report.
    """

    ALLOWED_FIELDS = {
        "customer_name", "report_date", "analysis_period", "deployment_type",
        "contracted_workers", "p99_usage", "average_usage", "peak_usage",
        "headroom", "usage_status", "hourly_usage_timeline",
        "peak_activity_hours", "low_activity_hours", "daily_averages",
        "connection_count", "total_worker_usage", "optimization_suggestions",
        "growth_recommendations",
    }

    BLOCKED_PATTERNS = [
        re.compile(p, re.IGNORECASE) for p in [
            r"maxSrcCPU|maxDstCPU|maxOrchCPU|maxSourceCPU|maxDestCPU|maxOrchestratorCPU",
            r"ceil\(",
            r"source.{0,5}worker|destination.{0,5}worker|orchestrator.{0,5}worker",
            r"src.{0,5}cpu|dst.{0,5}cpu|orch.{0,5}cpu",
            r"0\.33.{0,10}worker",
            r"estimation.{0,5}model",
            r"confidence.{0,5}interval",
            r"metabase",
            r"workspace.{0,3}id",
            r"billing.{0,5}formula",
            r"cpu.{0,5}formula",
            r"internal.{0,5}calculation",
            r"granola|se.toolkit",
            r"organization_id",
            r"heuristic",
            r"concurrent.{0,5}(api|db)",
            r"enforc(e[d]?|ing|ement)",
            r"queue[d]?|queueing|queuing",
            r"admission",
            r"reservation",
            r"\bon[\s._-]?demand",
            r"capacity.*(limit|gate|block|cap\b)",
            r"rollout.*(phase|cohort)",
            r"launch.?darkly|stigg",
            r"workload_queue",
        ]
    ]

    @classmethod
    def sanitize(cls, data: dict) -> dict:
        """Filter data to allowed fields and scan for blocked patterns.

        Args:
            data: Raw data dictionary from the analysis pipeline.

        Returns:
            Sanitized data dictionary containing only allowlisted fields.

        Raises:
            ValueError: If any blocked pattern is detected in the data values.
        """
        # Step 1: Filter to allowlisted keys only
        sanitized = {k: v for k, v in data.items() if k in cls.ALLOWED_FIELDS}

        # Step 2: Scan all string values for blocked patterns
        cls._scan_for_blocked_content(sanitized)

        return sanitized

    @classmethod
    def _scan_for_blocked_content(cls, data: dict, path: str = "") -> None:
        """Recursively scan all string values for blocked patterns."""
        for key, value in data.items():
            current_path = f"{path}.{key}" if path else key
            if isinstance(value, str):
                for pattern in cls.BLOCKED_PATTERNS:
                    match = pattern.search(value)
                    if match:
                        raise ValueError(
                            f"BLOCKED CONTENT in customer report at '{current_path}': "
                            f"matched pattern '{pattern.pattern}' -> '{match.group()}'. "
                            f"This data must not appear in customer-facing reports."
                        )
            elif isinstance(value, dict):
                cls._scan_for_blocked_content(value, current_path)
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, str):
                        for pattern in cls.BLOCKED_PATTERNS:
                            match = pattern.search(item)
                            if match:
                                raise ValueError(
                                    f"BLOCKED CONTENT in customer report at "
                                    f"'{current_path}[{i}]': matched pattern "
                                    f"'{pattern.pattern}' -> '{match.group()}'."
                                )
                    elif isinstance(item, dict):
                        cls._scan_for_blocked_content(item, f"{current_path}[{i}]")
                    elif isinstance(item, list):
                        for j, sub_item in enumerate(item):
                            if isinstance(sub_item, str):
                                for pattern in cls.BLOCKED_PATTERNS:
                                    match = pattern.search(sub_item)
                                    if match:
                                        raise ValueError(
                                            f"BLOCKED CONTENT in customer report at "
                                            f"'{current_path}[{i}][{j}]': matched pattern "
                                            f"'{pattern.pattern}' -> '{match.group()}'."
                                        )
                            elif isinstance(sub_item, dict):
                                cls._scan_for_blocked_content(sub_item, f"{current_path}[{i}][{j}]")
```

### Data Source Routing

- **Cloud customers (Pro, Flex, SME)**: Use Metabase MCP as the source of truth for billing and worker data
- **OSS / prospect customers**: Use exported JSON data (enhanced format with `jobs` array preferred) with the estimation model

### Data Source Restrictions for Reports

**CRITICAL**: Reports must ONLY use data from these sources:
- Metabase MCP queries (billing data)
- Airbyte Cloud API (connection and job data)
- User-provided JSON exports (OSS data)

Reports must NEVER pull data from:
- SE toolkit or similar MCPs
- Granola notes or meeting transcription tools
- Any personal or team-specific MCP servers
- Any source not explicitly listed above

If additional context is needed, ask the user to provide it explicitly rather than pulling from connected tools.

### Report Types

Classify utilization using **both** P99 and average usage to avoid contradictory language:

- **Over-utilized** (P99 >= 85% of contracted **AND** average >= 60% of contracted): Scheduling optimization reports with reschedule recommendations. The customer is genuinely approaching capacity.
- **Spiky but healthy** (P99 >= 85% of contracted **BUT** average < 60% of contracted): Growth headroom reports. Occasional spikes touch the ceiling but sustained usage is well within capacity. This is common for small contracts (1-2 workers) and does NOT indicate a capacity problem.
- **Under-utilized** (P99 < 85% of contracted): Capacity analysis reports showing growth headroom.
- **Non-Metabase customers**: API-based analysis reports for Cloud Sales Assist, Free tier, trials

**Important**: Never combine "At Capacity" / "0.00 headroom" language with "substantial room for growth" / "no optimization needed" in the same report. Pick one narrative based on the classification above and apply it consistently across all sections.

---

## Customer-Facing Executive Report (3-4 Pages)

This is the report to share with customers. It contains ONLY high-level, aggregated data. No internal formulas, no component breakdowns, no estimation methodology.

### Page 1: Cover

- Title: "Airbyte Worker Utilization Report"
- Customer name (large, bold)
- Report date and analysis period
- Deployment type: "Airbyte Cloud" (never show plan tier details)
- Footer: "Prepared by Airbyte Solutions Engineering"
- Airbyte branding: Use brand blue `#615EFF` accent line at top of page

### Page 2: Executive Summary

**Key Metrics Table** (4 columns, single row of values):

| Metric | Description |
|--------|-------------|
| Contracted Workers | Number of data workers in the customer's plan |
| P99 Usage | 99th percentile of hourly worker usage over the analysis period |
| Average Usage | Mean hourly worker usage |
| Peak Usage | Maximum hourly worker usage observed |

**Usage Status Indicator**: Display a colored badge based on the utilization classification:
- Green (`#10B981`): Under-utilized OR spiky-but-healthy (average < 60% of contracted). Label: "Healthy" or "Within Capacity"
- Yellow (`#F59E0B`): P99 70-85% of contracted AND average >= 60% (approaching capacity). Label: "Approaching Capacity"
- Red (`#EF4444`): P99 >= 85% of contracted AND average >= 60% (genuinely at or over capacity). Label: "At Capacity"

**Available Headroom**: Show `contracted_workers - average_usage` as the primary headroom metric (reflects sustained capacity), with a note that peak (P99) usage reaches X.X workers. Do NOT show `contracted_workers - p99_usage` as the sole headroom number, as this overstates capacity pressure when average usage is low.

**Key Finding Callout Box**: A 2-3 sentence summary of the most important finding. Examples:
- "Your current usage averages X.X workers with a P99 of Y.Y, leaving Z.Z workers of headroom against your contracted N workers."
- "Peak usage reaches X.X workers during hours HH:00-HH:00 UTC. Spreading syncs more evenly could reduce peak by ~Y.Y workers."

### Page 3: Usage Patterns

**24-Hour Worker Usage Timeline**: A bar chart showing average and max worker usage for each hour of the day (0-23 UTC). This is the key visualization — it shows customers when their worker consumption peaks.

- X-axis: Hour of day (00-23 UTC), labels at every hour
- Y-axis: Data Workers
- Two bar series: Average Workers (brand blue `#615EFF`) and Max Workers (light blue `#A5B4FC`)
- Horizontal reference line at contracted worker level (dashed red `#EF4444`)
- Legend in upper right corner

**Peak Activity Hours Table**: Top 3-5 hours with highest average worker usage

| Hour (UTC) | Avg Workers | Max Workers |
|------------|-------------|-------------|
| 00:00 | 4.2 | 6.1 |

**Low Activity Hours Table**: 3-5 hours with lowest average worker usage (shows optimization windows)

**Average Usage by Day of Week Table**: Mon-Sun average worker usage

### Page 4: Recommendations

Content depends on the utilization classification (see Report Types above):

**If over-utilized (P99 >= 85% AND average >= 60%)**:
- "Optimization Opportunities" heading
- Natural-language suggestions for spreading sync load (NO cron expressions, NO formulas)
- Example: "Consider rescheduling the syncs currently running at 2:00 PM UTC to the 6:00 AM UTC window, which has X.X workers of available capacity."
- Growth warning if trending upward

**If spiky-but-healthy (P99 >= 85% BUT average < 60%)**:
- "Growth Headroom" heading
- Explain that occasional peak spikes are normal operating behavior and do not indicate a capacity constraint
- How many additional connections could be supported before sustained (average) usage approaches capacity
- Scenario table: current, +25%, +50%, +100% connection growth with projected **average** workers (not P99)
- Statement about healthy headroom and no optimization needed
- Do NOT include language suggesting the customer will need additional workers for moderate growth

**If under-utilized (P99 < 85%)**:
- "Growth Headroom" heading
- How many additional connections could be supported before reaching capacity
- Scenario table: current, +25%, +50%, +100% connection growth with projected workers
- Statement about healthy headroom

**If enforcement mode is active — add "Sync Scheduling & Reliability" subsection:**
- Natural language description of peak usage windows and how spreading syncs improves reliability
- Example: "Your highest usage occurs between 00:00–03:00 UTC. Distributing syncs across lower-usage windows (06:00–12:00 UTC) would provide more consistent sync performance and reduce the likelihood of delays during peak periods."
- Do NOT mention: enforcement, queueing, admission checks, capacity limits as blocking mechanisms, feature flags, reservations
- Frame entirely as: performance, reliability, consistency

**Always include**: "For questions about this report, contact your Airbyte Solutions Engineer."

### Customer Report Content Rules (ENFORCED BY SANITIZER)

The following MUST NOT appear anywhere in the customer-facing report. The `DataSanitizer` enforces this automatically:

- CPU formulas or references (`maxSourceCPU`, `maxDestCPU`, `maxOrchestratorCPU`, `/8`)
- Source/destination/orchestrator worker breakdown (customers see total workers only)
- `ceil()` formulas or any estimation model details
- Metabase, workspace IDs, organization IDs
- Confidence intervals or methodology sections
- Internal calculation details
- Any reference to the billing formula internals
- Cron expressions or Quartz syntax
- The words "estimation", "heuristic", or "confidence interval" in any context
- References to Granola, SE toolkit, or other internal tools
- Enforcement, enforcing, enforced, or enforcement-related language
- Queueing, queuing, queued, or queue-related language (in the context of sync blocking)
- Admission checks, capacity gates, or blocking mechanisms
- Reservations, on-demand capacity, or capacity limit references
- Rollout phases, LaunchDarkly, Stigg, or feature flag references
- workload_queue, data_worker_usage_reservation, or internal table names

---

## Internal Detailed Report (8–9 Pages)

This report is for Airbyte internal use only. It contains the full analysis detail including formulas, methodology, and component breakdowns. When enforcement mode is active, an additional Enforcement Analysis page is included (9 pages total).

### Page 1: Cover

- Same layout as customer cover page
- **"INTERNAL USE ONLY"** watermark diagonally across the page (45-degree rotation, large gray text, alpha 0.15)
- Additional fields: Organization ID, Workspace ID(s), Analysis period, Data source (Metabase / API / OSS Export)

### Page 2: Executive Summary

- Same metric cards as customer report but with additional detail
- Key Findings box with full estimation details (if applicable)
- Note about data source and methodology used

### Page 3: Worker Calculation Model

- Full CPU billing formula with explanation: `(maxSourceCPU + maxDestCPU + maxOrchestratorCPU) / 8`
- Table with Component / Formula / Description columns
- If using estimation model: explain the API/DB heuristic (`ceil(DB/2) + ceil(API/5)`)
- Example calculation with actual customer numbers

### Page 4: Current Utilization Analysis

- **Connection Overview Table** with all columns:

| Connection | Type | Env | Jobs/7d | Avg Dur | Rows/7d | MB/7d |
|------------|------|-----|---------|---------|---------|-------|

- Full connection inventory with source/destination info

### Page 5: Concurrency Timeline

Three charts stacked vertically with `hspace=0.55` between subplots:
1. **Estimated Worker Usage (Hourly Peak)** — line chart over the analysis period
2. **Peak Concurrent Jobs (Hourly)** — area chart showing DB and API separately
3. **Worker Usage by Hour of Day (UTC)** — bar chart (avg + max) with contracted line

### Page 6: Worker Calculation Breakdown

- **Stats Table**: Peak Workers, P99, P95, Median, Average, Peak Concurrent DB, Peak Concurrent API, Peak Hour (UTC), Total Jobs
- **Worker Time Distribution Pie Chart**: Shows percentage of time at each worker level (max 6 slices, group <5% into "Other", use leader lines with 8pt font)
- **Concurrency Analysis Narrative**: Text describing the patterns observed

### Page 7: Enforcement Analysis (Conditional — Only When Enforcement Mode Is Active)

This page is only included when enforcement mode is active. If enforcement is not active, skip this page and continue with Growth & Capacity Planning as Page 7.

1. **Enforcement Status Box:**
   - Enforcement active: Yes / No / Unknown
   - Source: User declared / Phase list match / API detected
   - Phase: 1–4 (with definition)
   - Days over capacity in last 30

2. **Queue Risk Score Card:**
   - Score: X% of hours over capacity (with Low/Moderate/High label)
   - Peak overflow: X.X workers above contracted limit
   - Total overflow hours in last 30 days

3. **Queue Risk by Hour of Day Chart:**
   - Bar chart (same style as existing 24-hour timeline)
   - X-axis: hours 0–23 UTC
   - Y-axis: number of days (out of 30) where that hour exceeded capacity
   - Color: red bars for hours with >50% exceedance rate, amber for >20%, green for <=20%
   - Horizontal reference line at 50% threshold (15 days) to delineate moderate vs. high exceedance

4. **Connections at Highest Queue Risk Table:**

   | Connection | Schedule | Overlap Hours | Est. DW/Sync | Queue Risk |
   |---|---|---|---|---|
   | Salesforce → Snowflake | Every 1h | 00–03 UTC | 0.31 | High |

5. **Actual Queue Events** (conditional — only if workload_queue data is available):
   - Total queue events in last 30 days
   - Average queue duration
   - Longest queue event
   - Note: "Data from workload_queue table — reliability of this data is being validated."

6. **Live Enforcement Telemetry** (conditional — only if Datadog MCP is available):

   Query the following Datadog metrics using the Datadog MCP `get_datadog_metric` tool. Use `from: "now-7d"` for all queries. If the Datadog MCP is unavailable or returns errors, skip this subsection entirely.

   - **Capacity Wait Timeouts** (last 7 days):
     - Query: `sum:airbyte.temporal_workflow_failure{env:prod,failure_cause:capacity_wait_exceeded,workspace_id:{workspace_id}}.as_count()` for each workspace in the org
     - Display: total timeout count, trend (increasing/decreasing/stable)
   - **Reservation Recording Health:**
     - Success query: `sum:airbyte.data_worker_usage_recorded{env:prod,success:true,organization_id:{org_id}}.as_count()`
     - Failure query: `sum:airbyte.data_worker_usage_recorded{env:prod,success:false,organization_id:{org_id}}.as_count()`
     - Display: success rate percentage, failure count
   - **Entitlement Retrieval Health:**
     - Success query: `sum:airbyte.entitlement_retrieval{env:prod,success:true,organization_id:{org_id}}.as_count()`
     - Failure query: `sum:airbyte.entitlement_retrieval{env:prod,success:false,organization_id:{org_id}}.as_count()`
     - Display: success rate percentage, failure count
   - **Dashboard Link:** Include the configured `{datadog_dashboard_url}` for deep-dive investigation if one is set in `.se-config.yaml`; otherwise omit the link.
   - Note: "Live telemetry from Datadog — covers the last 7 days of real-time enforcement events."

### Page 8: Growth & Capacity Planning

- **Growth Scenario Analysis Bar Chart**: Current, +25%, +50%, +100% connection growth with white background bounding boxes for bar labels
- **Growth Scenarios Table**: Scenario / Connections / Est. P99 Workers / Basis
- **Runtime Headroom Analysis**: Average sync duration vs schedule interval, available headroom per cycle

### Page 9: Scheduling & Methodology

- **Optimized Scheduling Recommendations**: Specific cron expressions with Quartz syntax, per-connection impact, before/after comparison chart showing projected peak reduction
- **Data Dependency Sequencing Table**: Which connections depend on others
- **Confidence & Methodology Table**:

| Dimension | Confidence | Rationale |
|-----------|------------|-----------|
| Connection Inventory | High | All N connections retrieved via API |
| Job History | High | N jobs with duration data from last 7 days |
| Connector Classification | High/Medium | Confirmed as DB/API based on connector type |
| Concurrency Analysis | Medium | Based on job overlap at minute granularity |
| Worker Estimation | Medium | Uses heuristic model; actual billing uses CPU formula |

- **Methodology Box**: Description of data sources, analysis period, and caveats

---

## PDF Formatting Standards (Both Reports)

Follow these formatting rules to ensure all text is readable with no overlaps.

### Color Palette

| Element | Color | Hex |
|---------|-------|-----|
| Table headers (background) | Dark Navy | `#1E1B4B` |
| Table header text | White | `#FFFFFF` |
| Section headers | Brand Blue | `#615EFF` |
| Metric card accent (left border) | Brand Blue | `#615EFF` |
| Metric card background | White | `#FFFFFF` |
| Chart primary series | Brand Blue | `#615EFF` |
| Chart secondary series | Light Blue | `#A5B4FC` |
| Positive indicators | Green | `#10B981` |
| Warning indicators | Amber | `#F59E0B` |
| Critical indicators | Red | `#EF4444` |
| Callout box background | Light Lavender | `#EDE9FE` |
| Callout box border | Brand Blue | `#615EFF` |
| Body text | Dark Gray | `#1F2937` |
| Watermark text (internal) | Light Gray | `#D1D5DB` at alpha 0.15 |

### Tables

- Use `Paragraph` objects with word wrapping for any column that may contain long text
- Set `wordWrap: 'CJK'` in table styles to enable text wrapping within cells
- Target total table width of 6.5 inches (fits within standard margins on letter-size page)
- **Header row**: Dark navy background (`#1E1B4B`) with white text (`#FFFFFF`), bold
- **Alternating row shading**: White and light gray (`#F9FAFB`) for readability
- **Border**: 0.5pt light gray (`#E5E7EB`) grid lines
- Column width guidelines (adjust proportionally to fit 6.5 inches total):
  - Name/Connection columns: 2.0-2.2 inches
  - Short value columns (Type, Jobs, Confidence): 0.5-0.9 inches
  - Description/Rationale columns: 2.3-3.4 inches (use Paragraph wrapping)

### Charts

- **24-Hour Usage Timeline (customer report page 3)**: Grouped bar chart with hour labels 00-23. Use `matplotlib` with brand blue and light blue bars. Add horizontal dashed red line for contracted workers. Label axes clearly. Set figure size to (8, 4) for proper proportions.
- **Concurrency timeline (internal report page 5)**: Use `matplotlib.dates.HourLocator(interval=2)` and `DateFormatter("%m/%d %H:%M")` for x-axis labels. Rotate labels 45 degrees with `ha="right"` alignment. Set `hspace=0.55` between subplots for breathing room.
- **Hourly schedule chart**: Position annotation boxes above the bars (not overlapping them). Use `xytext` parameter to offset annotations vertically above data points.
- **Growth headroom chart**: Use `ax.annotate()` with white background bounding boxes (`bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor="none", alpha=0.8)`) for bar labels to prevent overlap.
- **Pie chart (internal report page 6)**: Use leader lines (`autopct` with `pctdistance=0.75`), 8pt font, and `startangle=90`. If any slice is < 5%, group into "Other". Limit to 6 slices maximum to prevent label overlap.
- All charts: Save at 150 DPI with `bbox_inches="tight"`. Use `plt.tight_layout()` before saving.

### Metric Cards (Executive Summary — Both Reports)

- **White background** with a colored left border accent (4pt wide, brand blue `#615EFF`)
- Large bold number (18pt, dark gray `#1F2937`)
- Smaller label beneath (9pt, medium gray `#6B7280`)
- Minimum 6pt spacing between number and label
- 4 cards in a row, evenly spaced across page width
- Subtle 1pt border (`#E5E7EB`) for card outline

### General Layout

- Page size: US Letter (8.5 x 11 inches)
- Page margins: 0.75 inches on all sides
- Section headers: 14pt bold in brand blue (`#615EFF`)
- Body text: 10pt `Helvetica` in dark gray (`#1F2937`)
- Callout boxes: Light lavender background (`#EDE9FE`) with brand blue left border (3pt)
- Page numbers: Bottom center, 8pt gray
- Footer on every page:
  - Customer report: "Confidential — Airbyte Solutions Engineering"
  - Internal report: "INTERNAL USE ONLY — Airbyte Solutions Engineering"

### INTERNAL USE ONLY Watermark (Internal Report)

On every page of the internal report, render a diagonal watermark:
```python
canvas.saveState()
canvas.setFont("Helvetica-Bold", 60)
canvas.setFillColor(colors.Color(0.82, 0.82, 0.82, alpha=0.15))
canvas.translate(4.25 * inch, 5.5 * inch)
canvas.rotate(45)
canvas.drawCentredString(0, 0, "INTERNAL USE ONLY")
canvas.restoreState()
```

---

## Report Generation Workflow

When generating reports, follow this exact sequence:

### Step 1: Gather Data

Use the appropriate data source (Metabase, API, or OSS export) as described in the "How It Works" section above. Collect all raw data into a single data dictionary.

### Step 2: Prepare Report Data

From the raw data, build two data dictionaries:

**`internal_data`**: Contains ALL fields — full detail for the internal report. No filtering applied.

**`customer_data`**: Pass the raw data through `DataSanitizer.sanitize()` to produce the filtered, safe version. If the sanitizer raises a `ValueError`, **FIX the data before proceeding** — do not skip the sanitizer or catch-and-ignore the error.

### Step 3: Generate Both PDFs

Generate both reports using ReportLab and matplotlib, following the page structures and formatting standards defined above:

1. **Customer report**: `{CustomerName}_Worker_Report_{YYYYMMDD}.pdf`
   - Use ONLY the `customer_data` dict from the sanitizer
   - Follow the 3-4 page customer report structure
   - Apply all formatting standards (navy headers, white metric cards, etc.)
2. **Internal report**: `{CustomerName}_Worker_Report_Internal_{YYYYMMDD}.pdf`
   - Use the full `internal_data` dict
   - Follow the 8–9 page internal report structure (9 pages when enforcement mode is active)
   - Apply all formatting standards plus the "INTERNAL USE ONLY" watermark on every page

### Step 4: Deliver

- Share the customer report file with the user, noting it is safe to forward to the customer
- Share the internal report file with the user, noting it is for internal use only
- Summarize key findings in a message

### Worker Calculation in Reports

For Cloud customers with Metabase data, the billing formula is:
`(maxSourceCPU + maxDestinationCPU + maxOrchestratorCPU) / 8 = Data Workers`

For OSS/prospect estimation reports, the sweep-line algorithm for estimating workers from job data:

1. For each connection's jobs, create START and END events with timestamps
2. Classify each connection as DB or API based on source/destination connector names
3. Process events chronologically, tracking concurrent DB and API counts separately
4. At each event, compute estimated workers: `ceil(concurrent_db / 2) + ceil(concurrent_api / 5)`
5. Track the peak workers value across all events

**Important**: The estimation model assumes each worker handles 2 DB connections OR 5 API connections concurrently. The estimation formula is `ceil(db/2) + ceil(api/5)`. This is a heuristic; actual billing uses the CPU-based formula above.

## Supporting Code

The Python analysis modules are located at `~/.claude/skills/worker-analysis/worker_analysis/` (or the equivalent `skills/worker-analysis/worker_analysis/` in the se-skills repo). Run them with `python3 ~/.claude/skills/worker-analysis/scripts/run_worker_analysis.py <mode>`.

This includes:
- Worker calculation engine
- Job overlap analyzer
- Connector classifier (API vs DATABASE)
- Metabase query builders
- PDF report generators (ReportLab + matplotlib)
- Airbyte Cloud API client
