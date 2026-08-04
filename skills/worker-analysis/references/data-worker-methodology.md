---
guidance_title: When analyzing Data Workers, 1K-C, sync metrics, or CPU resource bumps
guidance_trigger: When calculating Data Workers, 1K-C counts, workspace data volumes, sync metrics, CPU resource overrides, CPU bump impact, or querying Airbyte warehouse sync data
devin_knowledge_id: note-0eb2d966d6874ea19e1a468cb6e3ed79
---

> **SE Suite note:** BigQuery project and dataset names are configured in `.se-config.yaml` under `worker_analysis.bigquery_project` and `worker_analysis.bigquery_dataset`. Substitute `{bigquery_project}.{bigquery_dataset}` in any project-qualified query.

Use the `worker-analysis` skill or CPU-bump playbook for step-by-step execution when available.

BigQuery context:

- Project: `{bigquery_project}`
- Business-layer dataset: `airbyte_warehouse`
- Marts dataset: `airbyte_warehouse_marts`
- MCP server: `cognition-bigquery`
- Important tables: `connection_sync`, `workspace`, `connection`, `connection_by_day`, `workspace_by_day`, `stream_sync_by_attempt`, `connector`, and `organization`.
- `connection_sync` is central for sync job analysis. Useful columns include workspace and connection identifiers, `job_id`, start and end timestamps, row and data-volume metrics, `job_status`, connection status, platform, connector names and versions, and sync frequency category.
- Use `total_volume_rows_committed` and `total_volume_mb_committed` for actual landed data.
- BigQuery `TIMESTAMP_SUB` does not support month intervals; use day intervals such as `INTERVAL 365 DAY`.

1K-C definition:

- A 1K-C is a cloud-only, non-internal-user connection that moves at least 1,000 records per day.
- Query `{bigquery_project}.airbyte_warehouse.workspace_by_day`, summing `qualified_connection` where `is_cloud = TRUE` and `is_internal_user = FALSE`.
- Today's data is usually incomplete until the next dbt run, so use the previous complete day for current counts.
- As of Feb 2026, the count is approximately 13K on weekdays and 12K-13K on weekends.
- Definition confirmed by Matteo Palarchio.

Data Worker calculation:

- Data Workers = sum of CPU requests across all running containers in a workspace during a given hour divided by 8.
- The peak hourly value in a billing period is the advertised Data Worker count.
- Analyze sync concurrency, because staggered cron schedules do not prevent overlap if syncs run for hours.
- Data Worker usage is workspace-wide, not per connection.
- Each sync has source, destination, and orchestrator containers with separate CPU requests.
- The CPU-bump analysis output should include current and default Data Worker impact, sync success rates, duration percentiles, CPU utilization charts, risk assessment, and a phased rollback recommendation.

Resource overrides and defaults:

- Connection-level `resource_requirements` are stored in `stg_airbyte_prod_configapi__connection.resource_requirements`.
- Verify whether a CPU override applies to source and destination only or all containers before calculating impact; confirm with Datadog rather than assuming.
- Common defaults: API syncs are often 0.8 source + 0.3 destination + 0.3 orchestrator CPU request = 1.4 total, while DB syncs are often 1.0 + 1.0 + 1.0 = 3.0 total.
- Historical CPU-bump notes differ on whether connection-level overrides apply to source and destination only or all three containers, so verify actual per-container requests in Datadog with `avg:kubernetes.cpu.requests{ab_connection_id:<ID>} by {kube_container_name}` before calculating impact.
- DB connectors include MySQL, Microsoft SQL Server, Postgres, MongoDB, Oracle, CockroachDB, TiDB, and Db2.
- Check `stg_airbyte_prod_configapi__actor_definition.resource_requirements` for connector-definition-level overrides; most are null.

Sync timing methodology:

- Do not rely on `connection_sync.end_at`; it is often unreliable, null, or equal to `start_at`.
- Use `stream_sync_by_attempt` to derive actual sync start and end times.
- For a job, use `MIN(event_at)` as sync start and `MAX(TIMESTAMP_ADD(event_at, INTERVAL CAST(stream_duration_seconds AS INT64) SECOND))` as sync end.
- Include success-rate and duration percentile analysis over 30- and 90-day windows when assessing risk of reverting resource bumps.

Datadog CPU methodology:

- CPU usage metric: `kubernetes.cpu.usage.total`; divide nanocores by `1e9` for cores.
- CPU request and limit metrics: `kubernetes.cpu.requests` and `kubernetes.cpu.limits` in cores.
- Useful tags include `ab_connection_id` and `kube_container_name`.
- Break down by `{kube_container_name}` or `{ab_connection_id,kube_container_name}`.
- Compare actual usage against request, limit, and default values; a connection is lower risk to revert when actual CPU usage stays well below defaults, syncs are short or I/O-bound, failure rates are near zero, and default-comparable connections in the same workspace succeed.
- Treat risk as medium when sustained usage exceeds default requests but remains below limits or when high-frequency connections have tight SLAs.
- Treat risk as high when sustained usage approaches or exceeds default limits or when failures may be resource-related.
- Use average aggregation and average rollups for sustained usage, not max aggregation. For example: `avg:kubernetes.cpu.usage.total{...} by {ab_connection_id,kube_container_name}.rollup(avg, 86400)`.
- Do not use `max:` aggregation with max rollup for usage comparisons; it captures momentary spikes such as container startup or garbage collection.
- For many connections, batch Datadog queries with `OR` filters in groups of approximately 20 connection IDs.
- Exclude the `init` container from CPU analysis; only source, destination, and orchestrator are relevant.
