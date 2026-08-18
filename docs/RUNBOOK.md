# Hosted worker runbook

This runbook describes the repository-defined foundation. Provider-specific
commands and ownership are intentionally left to 5B2B2.

## First installation

1. Obtain the approved provider/account/project, region, VPC, DNS/certificate,
   secret manager, registry, observability backend, budget, and operational
   owner.
2. Review `deploy/pins.json`, the role defaults, and the generated Ansible
   plan.
3. Provision `/etc/se-skills/worker.env` as root-owned, group-readable
   configuration with mode `0640`; never put its values in bootstrap output.
4. Deliver the application payload and approved rootfs under `/opt/se-skills`.
5. Run the role in check mode, then apply it only during the approved
   maintenance window.
6. Run host preflight and the gated live smoke before accepting beta traffic.

The role has not been applied to a real host in this repository. Its localhost
check correctly fails closed when the OS contract is not Ubuntu 24.04.

## Validation and lifecycle

Validate pins, required environment names, image/rootfs digests, firewall
addresses, and the worker runtime before starting. The systemd unit runs the
virtualenv interpreter with `HOSTED_MODE=1`, `HOSTED_ENV=production`, and
`--runtime post-call-runsc`. Start, stop, and restart through systemd; do not
run a second worker manually against the same queue.

Readiness means host preflight passes, the service is active, the worker can
claim and heartbeat a test lease, the proxy can reach its approved upstream,
and the live smoke completes without customer content in evidence.

## Operations

- **Queue backlog and leases:** inspect queue depth, oldest queued age, running
  attempts, heartbeat age, retry count, and expired-lease recovery. Do not log
  lease tokens.
- **Sandbox failures:** classify the fixed runtime category, inspect the
  redacted runsc exit class and cleanup status, then retry only through the
  queue policy.
- **Proxy failures/timeouts:** distinguish upstream timeout, worker cancellation,
  capability rejection, and provider response errors. A proxy-owned deadline is
  authoritative over a sandbox `model_error`.
- **Token and cost monitoring:** use aggregated token usage and cost fields
  from the trusted proxy ledger. Alert thresholds are a 5B2B2 Product Owner
  input.
- **Tombstone cleanup:** the worker polls the durable cleanup queue. Check
  tombstone age, cleanup failures, and private object deletion without
  printing object paths.
- **Disk/cgroup pressure:** inspect free-space, memory, PID, CPU, and file-size
  failures. Stop intake before the host reaches exhaustion.

## Rotation, rollback, and crashes

Rotate secrets in the approved secret manager, replace `worker.env` with
correct ownership/mode, validate, and restart. Roll back by selecting a
previously approved image/rootfs digest and restarting; never roll back to a
mutable tag.

If the worker crashes, do not manually mutate the job ledger. Postgres lease
expiry and `recover_expired_leases` requeue or dead-letter attempts. If a
sandbox remains, the cleanup timer removes only verified-dead state.

For containment, stop the worker, remove it from queue intake, preserve
redacted logs and derived timestamps, rotate affected credentials, and
coordinate provider/network blocks. Customer-data cleanup uses the private
Storage and database deletion workflows; do not copy transcript or output
content into incident tickets.

## Observability contract

The current code has structured/redacted log categories and trusted token/cost
metadata, but it does not yet expose a production metrics registry. The
following contract is the target for 5B2B2; no metric names are claimed as
implemented by this slice.

Proposed low-cardinality metrics are:

- `se_skills_worker_jobs_total{outcome,runtime}`
- `se_skills_worker_job_duration_seconds{outcome,runtime}`
- `se_skills_worker_queue_depth`
- `se_skills_worker_lease_recoveries_total`
- `se_skills_worker_sandbox_failures_total{category}`
- `se_skills_worker_proxy_requests_total{outcome}`
- `se_skills_worker_proxy_timeouts_total`
- `se_skills_worker_tokens_total{direction,model}`
- `se_skills_worker_cost_total{model}`
- `se_skills_worker_tombstone_cleanup_total{outcome}`

Allowed labels are fixed categories, runtime, direction, and approved model
identifier. Forbidden labels and fields include transcript text, prompts,
responses, capability or lease tokens, Storage paths/object keys, customer
titles, emails, account names, raw URLs containing credentials, and secret
values. Structured logs should contain only event category, outcome, runtime,
model, bounded duration, token counts, cost, attempt number, and redacted
error category. Tests must reject forbidden values when the metrics/logging
surface is implemented.
