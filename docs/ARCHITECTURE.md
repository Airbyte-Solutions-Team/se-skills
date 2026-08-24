# SE Skills — Hosted Beta Architecture

## Current architecture

- `webapp/app.py` is a FastAPI composition root. It constructs `OutputService`, `JobService`, `AccountService`, `OverviewService`, `AskService`, `TranscriptionService`, `SkillRuntimeService`, and `SalesforceIntegration`; mounts `webapp/static/`; and registers routes.
- The frontend is a vanilla-JS SPA (`static/index.html`, `app.js`, `style.css`) served by FastAPI's `StaticFiles`.
- Business logic lives in `webapp/services/` and `webapp/routes/`. The current data layer is the local filesystem under `~/airbyte-work/01-customers/` plus `<workspace>/.state/` snapshots.
- Skills are Markdown prompt files under `skills/<skill>/SKILL.md`. The webapp invokes them through `SkillRuntimeService`, which selects a permission profile per skill. The default mode is `acceptEdits`; reviewed shell skills listed in `SHELL_BYPASS_ALLOWLIST` and declaring `shell=True` can receive `--permission-mode bypassPermissions`. This broad local permission model is not carried into the hosted runtime.
- Outputs are Markdown files with `.md.json` sidecars; review feedback is `.md.feedback.jsonl`.
- Persistence is best-effort JSON snapshots; job state survives an API restart but not a filesystem loss.
- `eval/` provides deterministic manifest-based evaluation.

## Target logical architecture

```
┌─────────────┐     HTTPS     ┌─────────────────────────────────────┐
│   Browser   │──────────────▶│  FastAPI + static SPA (same origin) │
└─────────────┘               └──────────────┬──────────────────────┘
                                             │
        ┌────────────────────────────────────┼────────────────────┐
        │                                    │                    │
        ▼                                    ▼                    ▼
 ┌─────────────┐                   ┌─────────────────┐    ┌────────────────┐
 │ Supabase    │                   │ Job ledger /    │    │ Object Storage │
 │ Auth + DB   │◀─────────────────▶│ queue (Postgres)│    │ (private org-  │
 │ + Storage   │                   │                 │    │ scoped buckets)│
 └─────────────┘                   └────────┬────────┘    └────────────────┘
                                            │
                                            ▼
                                  ┌──────────────────┐
                                  │  Worker pool     │
                                  │ (isolated        │
                                  │  ephemeral       │
                                  │  runtimes)       │
                                  └──────────────────┘
```

This is a logical view. Supabase remains the beta operational hypothesis; the
worker and runsc implementation are now present in the repository. The
deployment foundation now also loads root-owned image evidence, hashes the
materialized rootfs, uses durable worker-owned state paths, and verifies the
live ordered firewall table. Provider selection and real host provisioning
remain 5B2B2.

## FastAPI/SPA boundary

- The FastAPI backend and the vanilla-JS SPA stay on the same origin for the beta. The browser obtains a session from Supabase Auth; the SPA sends the JWT or session cookie with API requests.
- FastAPI validates the session, resolves the user's organization from an active membership, and applies organization-scoped authorization on every route. The beta supports a single Airbyte organization; there is no org switcher.
- Hosted persistence and auth are opt-in via an explicit `HOSTED_MODE=1` environment variable. When `HOSTED_MODE` is unset, the app continues to run the existing local filesystem workflows and does not require `asyncpg`/`pyjwt`.
- No server-side rendering; the SPA calls JSON/REST endpoints.

The worker deployment foundation is a separate hardened Ubuntu 24.04 x86_64
host contract. It does not expose a public application port and is configured
by the Ansible package under `deploy/ansible/`.

## Auth, database, and storage boundaries

- **Identity:** Supabase Auth with Google OAuth for Airbyte users (Product Owner-approved for the Slice 2 beta). Access is invite/pre-provisioned-membership only; an `@airbyte.com` email may be an onboarding check, but email domain is not the authorization boundary. Supabase Auth holds the canonical user id; the application maintains a `public.users` mirror and `memberships` links users to organizations with roles.
- **Relational data:** Postgres managed by Supabase (approved for the beta). All tenant-scoped tables carry `org_id`. Row-level security policies enforce that a query can only touch rows whose `org_id` matches a user's active membership. The API establishes organization context from an active membership resolved after JWT validation and never uses a user-supplied `org_id` as authoritative.
- **RLS context mechanism:** Normal requests use a least-privilege `app_user` Postgres role (`NOBYPASSRLS`) and a transaction-scoped signed tenant context token. The application verifies the Supabase JWT, computes `user_id:hmac(user_id, secret)` using a shared secret stored in the database under an `app_private` schema that `app_user` cannot read, and passes the token to Postgres via `SET LOCAL app.context_token`. `SECURITY DEFINER` functions owned by `app_admin` verify the HMAC against the private secret, read the active membership, and return a boolean for RLS checks. Because `app_user` cannot access the secret, it cannot forge a token for another user or organization even though it can set the GUC. This keeps the trust boundary inside the database connection and makes tenant isolation testable without a live Supabase Auth server.
- **Private storage:** Supabase Storage private buckets (Product Owner-approved for Slice 3). The `transcripts` bucket name is fixed and the migration forces it private, aborting if privacy cannot be verified. Objects are stored under `org_id/account_id/{opportunity_id/}transcripts/{transcript_id}` prefixes generated exclusively by the FastAPI server. Storage RLS policies target a dedicated `app_storage` Postgres role and call `public.is_active_org_member_storage`, an `app_admin`-owned `SECURITY DEFINER` function, so the browser-visible `authenticated` role has no `storage.objects` privileges and never reads `public.memberships` directly; anonymous, inactive, and cross-organization requests are denied. The Supabase `authenticator` role is granted membership in `app_storage` so it can switch roles based on the JWT claim; `app_storage` is `NOINHERIT` so the claim is required. For each Storage REST call, FastAPI signs a short-lived JWT (`role: "app_storage"`, `sub: user_id`) with the server-side `SUPABASE_JWT_SECRET` and sends it in the `Authorization` header; the public `SUPABASE_ANON_KEY` is only used as the `apikey` project identifier. The API streams chunked uploads and downloads through FastAPI/httpx; it does not use a Supabase service-role key or database admin credential in normal request paths. The SPA never receives Storage credentials or a service-role key. The beta uses the legacy extractable HS256 `SUPABASE_JWT_SECRET`; moving to a non-extractable signing model is an explicit pre-production decision.
- **No local filesystem durability:** the worker and API processes do not depend on persistent local disk for customer data, job state, or outputs.

## Web, API, and worker responsibilities

### Web/API process

- Authentication and session handling.
- Organization resolution and authorization.
- CRUD for accounts, opportunities, transcripts, jobs, and generated outputs (list/detail/content).
- Enqueueing skill jobs (`POST /api/hosted/accounts/{account_id}/jobs`) and returning a `job_id`.
- Listing an account's jobs (`GET /api/hosted/accounts/{account_id}/jobs`) and fetching job detail (`GET /api/hosted/jobs/{job_id}`).
- Requesting job cancellation (`POST /api/hosted/jobs/{job_id}/cancel`).
- Serving the static SPA and rendered output content.
- Hosted output review (Slice 6A): review state and per-version content
  (`GET .../outputs/{output_id}/review`,
  `GET .../outputs/{output_id}/versions/{version_ref}/content`), correction
  preview, and the comment/correction/approval mutations
  (`POST .../comments`, `POST .../corrections`, `POST .../approvals`).
- Audit logging of user-facing actions.
- Hosted export of an approved output (Slice 6B1):
  `POST .../outputs/{output_id}/exports` returns the exact approved current
  version as Markdown or PDF. The browser sends only a format and an idempotency
  key; the organization, actor, current version, approval state, and private
  Storage path come from `public.authorize_output_export` under an output-row
  lock, and the authorized version is immutable, so no lock is held across the
  Storage read or the render. Markdown is byte-for-byte the reviewed artifact;
  the PDF is derived from those same bytes through the shared `md_render` +
  `nh3` allowlist and an in-process ReportLab renderer with bounded document
  limits — no headless Chrome and no subprocess in the hosted path. Nothing is
  persisted and no signed or public URL is issued.
- Export rendering (PDF, internal HTML) from stored output content.

### Job ledger / queue (Slice 4)

- `public.jobs` is the durable job ledger. Primary statuses are `queued`, `running`, `success`, `failure`, `cancelled`, and `timeout`. `retry-wait` and dead-letter are scheduling metadata, not additional primary statuses.
- `public.job_attempts` is an append-only table. Each claim creates a row with `attempt_number`, `worker_id`, a random `lease_token`, `started_at`, `heartbeat_at`, and `timeout_at`. The attempt row later records the actual executor `runtime_version` and `model`, which may differ from the requested values stored on `public.jobs`.
- Every job and attempt has an explicit non-null `org_id`. Composite foreign keys on `(transcript_id, account_id, org_id)`, `(opportunity_id, account_id, org_id)`, and `(account_id, org_id)` enforce that all inputs belong to the same organization.
- `payload`, `input_refs`, and `source_manifest` contain only stable IDs and non-sensitive lineage metadata. They never contain transcript bodies, secrets, bearer tokens, storage credentials, or browser-supplied paths.
- Claiming is transactional: `claim_next_job` selects one eligible `queued` job with `FOR UPDATE SKIP LOCKED`, transitions it to `running`, creates an attempt row, increments `attempts`, sets `worker_id`, `started_at`, and `timeout_at`, and returns the attempt metadata including the lease token. Only one worker can win the row lock.
- `worker_heartbeat` locks the job row, then the attempt row, updates `heartbeat_at` and `timeout_at` for the current attempt and lease token, and returns whether `cancel_requested_at` has been set.
- `complete_job` and `fail_job` require the current attempt number and lease token, lock the job row first and then the attempt row, verify the job is `running` and the lease token is current, and then either record `success`/`failure`/requeue or finalize as `cancelled`. A stale token cannot mutate a reclaimed job. `complete_job` persists the actual executor `runtime_version` and `model` on the attempt row. `fail_job` checks `cancel_requested_at` before writing the attempt outcome; if cancellation was requested it records `outcome = 'cancelled'` on the current attempt while preserving diagnostics, and never emits retry/dead-letter metadata.
- `recover_expired_leases` scans `running` rows whose `timeout_at` has passed, locks the job row, locks the open attempt row, marks the abandoned attempt with a `timeout` outcome, and either requeues the job with bounded backoff when attempts remain or moves it to terminal `failure` with `dead_lettered = true`.
- `request_job_cancellation` locks the job row and uses status-predicated updates. A queued job becomes `cancelled` immediately; a running job sets `cancel_requested_at` and is finalized by the worker on its next heartbeat or completion attempt.
- Idempotent enqueue: `enqueue_job` accepts an optional `idempotency_key`. It inserts atomically with `ON CONFLICT (org_id, idempotency_key) DO NOTHING` and then compares the stored request fingerprint (account, transcript, opportunity, skill, skill_version, model, runtime_version, max_attempts, payload, input_refs, source_manifest). Identical requests return the original job; a conflicting reuse raises a safe `409`-style error.
- Delivery is at-least-once; result commits are idempotent within the current attempt. No job state lives in process memory or on local disk.
- Redis is intentionally not used. Postgres row locks, leases, and recovery functions are sufficient for the beta workload and avoid an additional infrastructure dependency.

### Workers

- A separate worker process (`scripts/run_hosted_worker.py`) polls `claim_next_job` and runs an injected executor.
- The worker enforces an immutable wall-clock execution deadline around `executor.execute` (using `asyncio.wait_for`) that is independent of the heartbeat lease. If the deadline expires, the worker cancels the executor and records a terminal `timeout`. If a cancellation request is observed, the heartbeat monitor cancels the executor and the worker records `cancelled`.
- Heartbeat failures are distinct from user cancellation. If `worker_heartbeat` fails, the worker stops the current attempt and leaves it for `recover_expired_leases`; it does not call `cancel_job` or mark the job as user-cancelled.
- Slice 4 ships a deterministic `EchoExecutor` that returns synthetic metadata and a `null` `output_id`. It proves the queue lifecycle without invoking a skill, model, shell command, container, or sandbox.
- Slice 5A defines the executable runtime contract in `webapp/hosted/runtime_contract.py` and keeps `EchoExecutor` working. Slice 5B1 adds `webapp/hosted/post_call_orchestrator.py` and `PostCallExecutor`, which materialize authorized inputs, invoke an injected `SkillRuntime`, validate the artifact with `output_schema.parse_output`, and persist valid outputs to private org-scoped Storage and Postgres.
- Slice 5B2A adds `webapp/hosted/runsc_executor.py` and `webapp/hosted/model_proxy.py`, replacing the in-process fake runtime with a per-attempt gVisor `runsc` sandbox and a worker-side Anthropic Messages API proxy. `scripts/run_hosted_worker.py` supports `--runtime {echo,post-call-runsc}` and fails closed when `runsc`, the pinned image, model-proxy configuration, Anthropic key, or isolation prerequisites are missing.
- Report completion, failure, or retry to the job ledger, including the actual executor `runtime_version` and `model` on each attempt.

#### Post-call orchestrator (Slice 5B1)

- `PostCallOrchestrator` resolves the job's transcript and prior-context references from trusted DB state and the signed `source_manifest`, rejects missing/cross-org/cross-account/mismatched-opportunity/duplicated/aliased/unlisted inputs, and fetches transcript bytes through the Storage backend (`transcripts` bucket).
- It creates host-generated input and output directories beneath the provisioned
  `/var/lib/se-skills/workspaces` root, materializes only manifest-listed inputs
  read-only, and invokes an injected `SkillRuntime`. No transcript bodies,
  credentials, signed URLs, JWTs, DB URLs, or browser-supplied paths enter the
  runtime.
- It reads the candidate `output.md` and `sidecar.json` outside the runtime, validates them with `output_schema.parse_output`, and persists only valid artifacts: an `outputs` row with a deterministic `id` derived from `job_id:attempt_number` and a private Storage object at `org_id/account_id/transcript_id/output_id/output.md` in the `outputs` bucket.
- On any failure, temporary directories are removed. Validation failures produce a fixed, redacted `output_error` category and create no Storage object or `outputs` row. If Storage upload succeeds but the `outputs` row cannot be created, the Storage object is deleted; if the row insert succeeds but Storage upload fails, the unvalidated row is rolled back by the transaction. Retries reuse the same deterministic `output_id` without duplicating evidence.
- `complete_job` receives the authoritative `result_output_id` and `validation_status`; `fail_job` receives a redacted error category and `validation_status`.

## Agent-runtime isolation model (resolved in ADR-005 for Slice 5B)

The hosted runtime is not a single-shot `messages.create()` call. It is a manual multi-step Anthropic Messages API typed-tool loop that preserves the behavior the local skills rely on. Slice 5A resolved the runtime/sandbox decision in ADR-005 and encoded the contract in `webapp/hosted/runtime_contract.py`, `webapp/output_schema.py`, and `webapp/hosted/agent_loop_harness.py`. Slice 5B1 implements the trusted worker-side orchestration (`webapp/hosted/post_call_orchestrator.py`) and output persistence boundary. Slice 5B2A implements the sandbox executor (`webapp/hosted/runsc_executor.py`) and worker-side model proxy (`webapp/hosted/model_proxy.py`). The contract a 5B2A sandbox must satisfy is:

- **Source/file discovery:** the runtime can list and read the files the job owns (transcripts, prior outputs, reference data).
- **Full transcript reads:** transcripts are loaded entirely into context; no arbitrary truncation that would break source-coverage claims.
- **Tool use:** the agent can call a fixed set of approved tools (for example, read file, list directory, search text, call an allowlisted HTTP endpoint). Tool calls are logged.
- **Prior context:** the worker can pass the account/opportunity history, recent outputs, and uploaded transcript context into the runtime.
- **Self-checks:** the runtime can verify required output sections and source coverage before finalizing.
- **Source coverage and artifact generation:** the output Markdown must still include `At a Glance`, `Source Coverage`, and skill-specific required sections; sidecar validation is run after generation.

The runtime is executed inside an isolated gVisor-backed `runsc` container running the manual typed-tool loop:

- One ephemeral sandbox per attempt.
- No access to the host filesystem except explicitly mounted allowlisted paths (read-only transcript and prior context, writable temporary output workspace).
- No environment variables from the host except a short allowlist.
- No unrestricted shell; no `bypassPermissions`; no arbitrary Git; no browser/computer automation; no local repository access; no Live Transcribe; no arbitrary outbound network.
- Network egress is deny-by-default; the sandbox reaches only a per-job worker-side model proxy over a Unix domain socket (`runsc --network=none`). The proxy validates a short-lived, job-scoped capability token bound to the job ID, attempt number, lease token, authorized model, execution deadline, endpoint, and API version. The proxy adds the Anthropic API key and forwards the request; the sandbox never sees the key.
- The container image is rebuilt from a pinned distroless base image with non-root UID/GID; ephemeral data and the container bundle are destroyed after the attempt completes.
- The worker validates the Markdown and sidecar outside the sandbox before writing anything to Storage or the `outputs` row.

## Hosted vs local runtime distinction

| Capability | Hosted runtime | Local runtime |
|---|---|---|
| Identity | Supabase Auth / org membership | OS user / Claude Code user |
| Skill invocation | Worker sandbox with a manual Anthropic Messages API typed-tool loop and worker-side model proxy (Slice 5B) | `claude -p` with `acceptEdits` default; reviewed shell skills can use `bypassPermissions` |
| File access | Mounted allowlisted files only | Full local workspace, `~/.claude/skills/`, repos |
| Network | Allowlist only | Host network |
| MCPs/tools | Approved, audited subset | User's full `~/.claude.json` MCP config |
| Git / shell | Not available | Available |
| Browser/computer automation | Not available | Available (`pov-gsheet`, etc.) |
| Live Transcribe | Not available | Local audio capture |
| Source-code repo reasoning | Limited to bundled or fetched reference data | Full local `airbyte` / `airbyte-platform` clones |

## Failure and retry flow

1. **API enqueue:** `POST /api/hosted/accounts/{account_id}/jobs` calls `public.enqueue_job`, verifies the caller's active membership, validates the transcript/opportunity/account organization chain, and inserts a `queued` record. It returns `job_id`, `job_status`, and `job_created_at`.
2. **Worker claim:** `claim_next_job` selects the oldest eligible `queued` job with `FOR UPDATE SKIP LOCKED`, transitions it to `running`, creates an append-only `job_attempts` row with a random `lease_token`, and sets `timeout_at`. The row lock guarantees only one worker owns the attempt.
3. **Heartbeat:** the worker calls `worker_heartbeat` to extend `timeout_at` while it runs the executor; `worker_heartbeat` also returns whether `cancel_requested_at` is set.
4. **Slice 4 execution / Slice 5A contract:** the worker runs the deterministic `EchoExecutor` under an immutable wall-clock deadline. The executor returns synthetic metadata and a `null` `output_id`. No skill, model, shell, container, or sandbox is invoked. Slice 5A added the `SkillRuntime` protocol (`webapp/hosted/runtime_contract.py`) and the `post-call` validation contract (`webapp/output_schema.py`).
5. **Success:** `complete_job` locks the job then the attempt, verifies the current attempt/lease token, persists the actual executor `runtime_version` and `model` on the attempt, and sets the job to `success` with `finished_at`, `token_usage`, and `cost`.
6. **Retryable failure:** `fail_job` checks `cancel_requested_at` first. If set, it finalizes the current attempt as `cancelled` and the job as `cancelled` without retry/dead-letter metadata. Otherwise it records the attempt outcome (`failure` or `timeout`) and either requeues the job with a persisted backoff when `attempts < max_attempts` or moves it to terminal `failure` with `dead_lettered = true`.
7. **Timeout:** a `running` job whose `timeout_at` expires is recovered by `recover_expired_leases`, which records the abandoned attempt as `timeout` and either requeues or dead-letters the job. The final status is `timeout`, distinct from `failure`.
8. **Cancellation:** `request_job_cancellation` sets `cancel_requested_at`. A queued job becomes `cancelled` immediately; a running job is finalized by the worker on the next heartbeat or completion attempt. `fail_job` and `complete_job` both honor a pending cancellation by finalizing the current attempt as `cancelled`; `cancel_job` is used for a clean worker-side cancellation.
9. **API/worker crash:** jobs in `running` without a heartbeat are recovered by `recover_expired_leases`; the ledger is the source of truth. The invariant is at-least-once recovery, not exactly-once execution; `complete_job`/`fail_job` are scoped to the current attempt and lease token to keep result commits idempotent where practical.

## Clearly identified unresolved decisions

These decisions are intentionally deferred to the implementation slices and must be resolved before the corresponding code is merged:

- **Worker framework:** containerized workers (for example, Fly Machines, ECS Fargate, Kubernetes Jobs) vs. a process pool on a VM. The sandbox technology depends on this choice.
- **Sandbox technology:** resolved to gVisor-backed `runsc` containers for the beta, with Firecracker as a future higher-isolation alternative.
- **Supabase commitment:** whether Supabase Auth, Postgres, and Storage are approved as the operational backend or are replaced by another Airbyte-standard provider.
- **Credential storage:** whether to use Supabase Vault, AWS Secrets Manager, HashiCorp Vault, or another encrypted store for OAuth tokens and integration credentials.
- **Salesforce/Gong/Google integrations:** whether the beta includes these integrations and, if so, how user-consented OAuth credentials are stored and scoped.
- **Observability:** logging, metrics, and tracing backend for workers and sandbox.
- **Output persistence boundary for Slice 5B1:** implemented in `webapp/hosted/post_call_orchestrator.py`. The worker resolves transcript and prior context, materializes read-only files into a job-scoped workspace, invokes the runtime, validates `output.md` and `sidecar.json` outside the sandbox, writes valid artifacts to private org-scoped Storage, creates the `outputs` row, and calls `complete_job` with `result_output_id`. Invalid artifacts are distinguished from execution failures. Cancellation and Storage/metadata failure cleanup is implemented; the gVisor/runsc sandbox and live Anthropic proxy remain Slice 5B2.
