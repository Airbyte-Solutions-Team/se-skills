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

Validate pins, required environment names, the root-owned sandbox manifest and
all referenced evidence, the deployed rootfs digest, live firewall addresses,
and the worker runtime before starting. The systemd unit runs the
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
expiry and `recover_expired_leases` requeue or dead-letter attempts. The
worker's terminal path invokes broker-owned `finalize`; the stale sweeper
reuses that same reconciliation primitive with the root-configured reclaim
age.

The worker's root-owned Python runsc broker receives typed stdin requests and
authors the OCI config. It seals input/output/proxy workspaces before binding
them, and list/finalize/stale cleanup all use the same sudo boundary. The
supported list contract is `runsc list --format=text`; an unparseable `ID`
header is a fail-closed cleanup error. Broker failures expose only fixed
redacted error classes. Worker workspaces are created beneath
`/var/lib/se-skills/workspaces` (root-owned, group-owned by `se-worker`, mode
0730), then atomically moved into root-owned staging. Input and job metadata
are sandbox-readable but read-only; output is mode 0770/0660 for the worker
and sandbox group only. The broker computes the CPU rlimit from the job's
validated execution deadline. Broker exit 65 and 66 become explicit,
redacted cleanup failures rather than undocumented generic errors.

The broker is standard-library-only and runs as `/usr/bin/python3 -I`; it
does not trust the worker environment or user-site imports. Preflight executes
that isolated interpreter and validates every actual `sys.path` component,
plus the broker script and config, as root-owned and non-group/world-writable.
The host Python 3.12 contract is separate from the sandbox image's Python 3.11
runtime.

Admission limits are root-owned in `/etc/se-skills/runsc-broker.json`: 1 MiB
stdin, a five-second read deadline, 8 KiB strings, 256-item collections,
16-level nesting, four concurrent operations, 30-second broker operations,
and a 15-minute maximum attempt horizon. Expired and far-future deadlines
fail closed.

Crash custody is recorded in the fsynced root-owned
`/var/lib/se-skills/runsc-journal`. The record is updated through sealing and
is removed only after state, bundle, staging, workspace, and worker paths are
reconciled. `finally` is only a fast path. Broker-owned `finalize` is the only
terminal release operation, and the stale sweeper invokes the same
reconciliation primitive using the root-configured reclaim age. The
broker-owned runsc process group records PID/PGID/start-time identity and is
terminated or escalated only while ownership remains verifiable. Input, proxy
sockets, and `job.json` are always destroyed. Output is restored only for a
durably successful terminal record; all other partial output is discarded.

Image promotion is manual and protected. Build the pinned image, run SBOM and
vulnerability scans, push it, record the registry manifest digest, generate
and attest digest-bound SBOM/provenance, and sign that digest. Install the
resulting root-owned manifest and evidence files before starting the worker.
From the repository root, run `python -m scripts.install_sandbox_evidence
--bundle-dir <release-bundle>
--manifest <build-manifest> --output-manifest
/etc/se-skills/sandbox-manifest.json` as root with the downloaded release
bundle; it copies the named SBOM and provenance into
`/etc/se-skills/evidence/<digest-hex>/` and writes the host manifest with
absolute paths. Extract the rootfs archive as root with
`--same-owner --numeric-owner`.
The release builder itself runs as the normal workflow user and hashes the
Docker-export tar stream without extracting it. For local workflow validation,
bootstrap the pinned actionlint binary with
`./scripts/install-actionlint.sh`, then run
`ACTIONLINT_BIN="$PWD/.tools/actionlint" ./scripts/check-workflows.sh`; the
check fails closed when actionlint is unavailable. The pull-request workflow
performs the checksum verification and runs this command over every
`.github/workflows/*.yml`.
The release workflow is `workflow_dispatch`-only, requires
`BUILD_SANDBOX_IMAGE`, uses the protected `sandbox-release` environment, and
rejects signing from refs other than `main` or an approved immutable `v*` tag
whose commit is reachable from `origin/main`.
When dispatching it, provide the image input as
`<owner>/<repo>/se-skills-sandbox`; the workflow rejects image paths outside
the running repository namespace.
The current Linux-amd64 tool pins are Syft 1.50.0, Grype 0.116.1, and Cosign
3.1.3; only releases public for at least seven days are eligible for these
pins. Their checksums live in `deploy/pins.json` and the workflow verifies
each download before execution. Production preflight compares
`SANDBOX_IMAGE_DIGEST` with the manifest and repository pin, verifies every
evidence file and parent directory, and hashes the deployed rootfs tree.
The Ansible role validates and applies the firewall transaction before the
systemd task enables or starts the worker.

Before promotion, provision the controlled forward proxy selected by the
Product Owner. Configure `ANTHROPIC_EGRESS_PROXY_URL` and the role's proxy
address/port. The proxy must permit only `CONNECT`/TLS to
`api.anthropic.com:443`, resolve DNS itself, authenticate the worker, enforce
bounded timeouts and concurrency, redact audit logs, and omit request/response
bodies. Direct Anthropic egress and maintained Anthropic CIDRs are disabled.

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
