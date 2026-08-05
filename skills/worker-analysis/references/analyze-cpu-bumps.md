# CPU Bump Analysis for Data Worker Impact

> **SE Suite note:** BigQuery project and dataset names are configured in `.se-config.yaml` under `worker_analysis.bigquery_project` and `worker_analysis.bigquery_dataset`. Example queries below use `airbyte_warehouse` as the dataset; substitute `{bigquery_project}.{bigquery_dataset}` where a project-qualified name is needed.

Analyze connection-level CPU overrides in an Airbyte Cloud workspace, quantify the Data Worker impact, assess risk of reverting to defaults, and produce a Slack-safe report with charts.

## Overview

This playbook produces a 4-part analysis:

1. **Connection inventory** — Identify all bumped vs default connections
2. **Data Worker calculation** — Compare current DW vs hypothetical DW at defaults
3. **Sync timing & success rates** — Quantify operational risk of reverting
4. **CPU usage visualization** — Chart actual usage vs request vs limit vs default

## What's Needed From User

- **Customer name or organization ID** — to identify the workspace
- **Workspace ID** (optional) — if the org has multiple workspaces, specify which one

## Procedure

### Phase 1: Identify Workspace & Connections

1. **Find the organization.** Run this query via the `cognition-bigquery` MCP:

   ```sql
   SELECT o.organization_id, o.organization_name_masked,
          w.workspace_id, w.workspace_name_masked
   FROM `{bigquery_project}.airbyte_warehouse.organization` o
   JOIN `{bigquery_project}.airbyte_warehouse.workspace` w
     ON o.organization_id = w.organization_id
   WHERE LOWER(o.organization_name_masked) LIKE LOWER('%<CUSTOMER>%')
   ```

   Record the `organization_id` and `workspace_id`.

2. **Pull all active connections with resource overrides.** Run via `cognition-bigquery` MCP:

   ```sql
   SELECT c.connection_id, c.connection_name,
          c.source_connector_name, c.destination_connector_name,
          c.cpu_request, c.memory_request,
          c.schedule_type, c.cron_expression_cloud, c.cron_timezone_cloud,
          c.connection_status
   FROM `{bigquery_project}.airbyte_warehouse.connection` c
   WHERE c.organization_id = '<ORG_ID>'
     AND c.connection_status = 'active'
   ORDER BY c.cpu_request DESC NULLS LAST
   ```

3. **Classify connections into two groups:**
   - **Bumped**: connections where `cpu_request IS NOT NULL` (has an override)
   - **Default**: connections where `cpu_request IS NULL`

4. **Post initial summary to Slack** (non-blocking): number of connections, how many bumped, override values found.

### Phase 2: Verify Per-Container CPU via Datadog

This is the most critical step. Do NOT assume CPU values; verify them.

5. **For each bumped connection, query Datadog** using the `datadog` MCP for actual per-container CPU requests:

   ```
   avg:kubernetes.cpu.requests{ab_connection_id:<CONNECTION_ID>} by {kube_container_name}
   ```

   Use a 7-day window. Record the CPU request for each of: `source`, `destination`, `orchestrator`. Filter out `init` containers.

6. **For 2-3 default connections, do the same query** to confirm the baseline defaults.

7. **Apply the CPU override rules (verified from Datadog ground truth):**

   - The `cpu_request` override **generally applies to all 3 containers** (source, destination, AND orchestrator)
   - **Exceptions:** MySQL and Oracle connectors — the orchestrator retains its default (1.0) regardless of override
   - For most connectors (Postgres, MSSQL, Salesforce, S3, SAP HANA, ServiceNow, etc.): `cpu_request=N` → N on all 3 containers
   - Database connectors default: 1.0 / 1.0 / 1.0 = 3.0 total
   - API connectors default: 0.8 / 0.3 / 0.3 = 1.4 total

   **CPU per sync calculation:**

   | Override | Connector Type      | Source | Dest | Orch | Total |
   |----------|---------------------|--------|------|------|-------|
   | 2.0      | Most connectors     | 2.0    | 2.0  | 2.0  | 6.0   |
   | 3.0      | Most connectors     | 3.0    | 3.0  | 3.0  | 9.0   |
   | 2.0      | MySQL/Oracle        | 2.0    | 2.0  | 1.0  | 5.0   |
   | 3.0      | MySQL/Oracle        | 3.0    | 3.0  | 1.0  | 7.0   |
   | None     | Database            | 1.0    | 1.0  | 1.0  | 3.0   |
   | None     | API                 | 0.8    | 0.3  | 0.3  | 1.4   |

   **ALWAYS verify actual per-container values via Datadog — do not assume.** If Datadog values differ from these, use the Datadog values (they are ground truth).

### Phase 3: Data Worker Calculation

8. **Pull actual sync timing (14 days)** via `cognition-bigquery` MCP:

   ```sql
   SELECT ssa.connection_id, ssa.connection_name, ssa.job_id,
          MIN(ssa.event_at) as sync_start,
          MAX(TIMESTAMP_ADD(ssa.event_at,
              INTERVAL CAST(ssa.stream_duration_seconds AS INT64) SECOND)) as sync_end
   FROM `{bigquery_project}.airbyte_warehouse.stream_sync_by_attempt` ssa
   JOIN `{bigquery_project}.airbyte_warehouse.connection` wc
     ON ssa.connection_id = wc.connection_id
   WHERE wc.organization_id = '<ORG_ID>'
     AND ssa.event_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
     AND ssa.attempt_status = 'succeeded'
   GROUP BY ssa.connection_id, ssa.connection_name, ssa.job_id
   ```

   Do NOT use `connection_sync.end_at` -- it is often unreliable.

9. **Compute hourly DW using a sweep-line algorithm:**
   - Create START and END events from the sync_start/sync_end data from step 8
   - For each connection's sync, the START event adds its CPU (from step 7) and the END event subtracts it
   - Process events chronologically, tracking the running total of source CPU, destination CPU, and orchestrator CPU
   - For each UTC hour, record the **maximum** running CPU total that occurred during that hour
   - DW for that hour = max concurrent CPU / 8
   - Peak hourly DW = the advertised DW for billing

   **Important:** Do NOT simply sum all connections that had any sync during the hour — two connections that sync 2:00-2:10 and 2:50-3:00 (no overlap) should NOT be summed. Only connections with actually overlapping sync windows contribute to concurrent CPU.

10. **Compute the "if defaults" scenario:**
    - Replace each bumped connection's CPU with the appropriate default total (3.0 for DB, 1.4 for API)
    - Recompute hourly DW
    - DW savings = current peak DW - default peak DW

11. **Post the DW comparison to Slack** in code block format:
    ```
    Peak hour (N concurrent connections):
      Current (bumped):  X.X CPU = Y.Y DW
      If all defaults:   X.X CPU = Y.Y DW
      DW saved:          Z.Z DW (PP% reduction)
    ```

### Phase 4: Sync Timing & Success Rate Analysis

12. **Query success rates (30-day)** via `cognition-bigquery` MCP:

    ```sql
    SELECT cs.connection_id, cs.connection_name,
           COUNT(*) as total_syncs,
           COUNTIF(cs.job_status = 'succeeded') as succeeded,
           COUNTIF(cs.job_status = 'failed') as failed,
           ROUND(COUNTIF(cs.job_status = 'succeeded') / COUNT(*) * 100, 2) as success_rate_pct
    FROM `{bigquery_project}.airbyte_warehouse.connection_sync` cs
    WHERE cs.connection_id IN ('<bumped_connection_ids>')
      AND cs.start_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
    GROUP BY cs.connection_id, cs.connection_name
    ORDER BY success_rate_pct ASC
    ```

    Also query for default connections for comparison.

13. **Query sync duration percentiles (30-day)** via `cognition-bigquery` MCP:

    ```sql
    WITH sync_durations AS (
      SELECT ssa.connection_id, ssa.connection_name, ssa.job_id,
             TIMESTAMP_DIFF(
               MAX(TIMESTAMP_ADD(ssa.event_at,
                   INTERVAL CAST(ssa.stream_duration_seconds AS INT64) SECOND)),
               MIN(ssa.event_at), SECOND) as duration_seconds
      FROM `{bigquery_project}.airbyte_warehouse.stream_sync_by_attempt` ssa
      WHERE ssa.connection_id IN ('<all_connection_ids>')
        AND ssa.event_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
        AND ssa.attempt_status = 'succeeded'
      GROUP BY ssa.connection_id, ssa.connection_name, ssa.job_id
    )
    SELECT connection_id, connection_name,
           COUNT(*) as syncs,
           APPROX_QUANTILES(duration_seconds, 100)[OFFSET(50)] as p50_sec,
           APPROX_QUANTILES(duration_seconds, 100)[OFFSET(95)] as p95_sec,
           APPROX_QUANTILES(duration_seconds, 100)[OFFSET(99)] as p99_sec,
           MAX(duration_seconds) as max_sec
    FROM sync_durations
    GROUP BY connection_id, connection_name
    ORDER BY p50_sec DESC
    ```

14. **Post sync analysis to Slack** in code blocks showing bumped vs default durations and success rates.

### Phase 5: CPU Usage Visualization

15. **For each bumped connection + 1-2 representative default connections, query Datadog** for actual CPU usage:

    ```
    avg:kubernetes.cpu.usage.total{ab_connection_id:<ID>} by {kube_container_name}
    avg:kubernetes.cpu.requests{ab_connection_id:<ID>} by {kube_container_name}
    avg:kubernetes.cpu.limits{ab_connection_id:<ID>} by {kube_container_name}
    ```

    Use a 7-day window. Convert `usage.total` from nanocores to cores (divide by 1e9).

16. **Generate CPU charts** using matplotlib:

    - **Per-connection chart**: grouped bar chart with 4 bars per container (actual, request, limit, default) for source/destination/orchestrator
    - **Summary chart**: total CPU per sync (actual vs request vs default) for all connections

    Save as PNG files.

17. **Post charts to Slack** as file attachments.

### Phase 6: Risk Assessment & Recommendation

18. **Classify risk** using this framework:

    - **Low risk** (safe to revert): Actual CPU usage well below default request; zero or near-zero failures in 30-90 days; syncs are short and I/O-bound; default connections in same workspace handle comparable workloads
    - **Medium risk** (revert with monitoring): Some connections' actual CPU exceeds the default request but stays below the limit; high-frequency connections with tight SLAs
    - **High risk** (do not revert without investigation): Actual CPU approaches or exceeds the default limit; connections with resource-related failures

19. **If low/medium risk, suggest phased rollback:**
    - Batch 1: High-frequency, low-volume connections (least risk)
    - Batch 2: Lower-frequency, higher-volume connections
    - Monitor each batch for 24 hours before proceeding

20. **Post final risk assessment and recommendation to Slack.**

## Specifications

### DW Formula

`DW = (sum of CPU requests across all running containers in workspace during an hour) / 8`

The peak hourly value across the analysis period is the advertised (billed) DW.

### Sync Timing Source

Always use `stream_sync_by_attempt` with `MIN(event_at)` as sync start and `MAX(event_at + stream_duration_seconds)` as sync end. The `connection_sync.end_at` field is often unreliable.

### Output Format

All Slack messages must be Slack-compatible:
- Use code blocks for structured data (no markdown tables)
- Attach charts as PNG files
- Use `.csv`/`.tsv` for tabular data attachments
- No emojis unless requested

## Common Pitfalls

1. **Wrong defaults**: Database connectors default to 1.0/1.0/1.0 = 3.0, NOT 0.8/0.3/0.3 = 1.4 (which is for API connectors). Always verify via Datadog.
2. **Wrong override scope**: For most connectors, the `cpu_request` override applies to all 3 containers (source, destination, AND orchestrator). **Exceptions:** MySQL and Oracle connectors — the orchestrator retains its default (1.0). Always verify via Datadog.
3. **Unreliable end_at**: Do not use `connection_sync.end_at` for sync timing. Use `stream_sync_by_attempt` instead.
4. **Assuming uniform behavior**: A `cpu_request=2.0` override is 2.0 x 3 = 6.0 for most connectors, but only 2.0 (src) + 2.0 (dst) + 1.0 (orch) = 5.0 for MySQL/Oracle. Always verify via Datadog.
