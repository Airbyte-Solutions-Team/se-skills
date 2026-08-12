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

This is a logical view. Concrete technology choices (Supabase, worker framework, sandbox implementation) are working hypotheses and will be decided per roadmap slice.

## FastAPI/SPA boundary

- The FastAPI backend and the vanilla-JS SPA stay on the same origin for the beta. The browser obtains a session from Supabase Auth; the SPA sends the JWT or session cookie with API requests.
- FastAPI validates the session, resolves the user's organization from an active membership, and applies organization-scoped authorization on every route. The beta supports a single Airbyte organization; there is no org switcher.
- Hosted persistence and auth are opt-in via an explicit `HOSTED_MODE=1` environment variable. When `HOSTED_MODE` is unset, the app continues to run the existing local filesystem workflows and does not require `asyncpg`/`pyjwt`.
- No server-side rendering; the SPA calls JSON/REST endpoints.

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
- CRUD for accounts, opportunities, transcripts, outputs, reviews, and jobs.
- Enqueueing skill jobs and returning a job id.
- Serving the static SPA and rendered output content.
- Audit logging of user-facing actions.
- Export rendering (PDF, internal HTML) from stored output content.

### Job ledger / queue

- Persist job records with `status`, `requester_id`, `org_id`, `account_id`, `opportunity_id`, `skill`, `skill_version`, `model`, `runtime_version`, `payload` (input references and runtime config, not raw transcript bodies or secrets), `source_manifest`, `result_output_id`, `validation_status`, `token_usage`/`cost`, `attempts`, `max_attempts`, `started_at`, `finished_at`, `timeout_at`, `cancelled_at`, `error` (redacted), `worker_id`.
- Provide at-least-once delivery to workers (not exactly-once execution); support bounded retry with backoff and a dead-letter / poison-pill queue. `retry-wait` and dead-letter are queue/scheduling concepts, not primary job statuses.
- Survive API and worker restarts without data loss.
- Do not require Redis unless a concrete workload need (for example, a high-throughput event stream or scheduled job fan-out) justifies it; a Postgres-backed queue is acceptable for the initial beta workload.

### Workers

- Poll the job ledger or consume events.
- Create an isolated sandbox for each job.
- Mount only the allowlisted transcript/output files and read-only reference data.
- Run the skill runtime with allowlisted tools, credentials, and network destinations.
- Write the generated Markdown and `.md.json` sidecar back to object storage and update the job/output record.
- Report completion, failure, or retry.

## Agent-runtime isolation model

The hosted runtime is not "load a `SKILL.md` and call `messages.create()` once." It is a multi-step agent environment that preserves the behavior the local skills rely on:

- **Source/file discovery:** the runtime can list and read the files the job owns (transcripts, prior outputs, reference data).
- **Full transcript reads:** transcripts are loaded entirely into context; no arbitrary truncation that would break source-coverage claims.
- **Tool use:** the agent can call a fixed set of approved tools (for example, read file, list directory, search text, call an allowlisted HTTP endpoint). Tool calls are logged.
- **Prior context:** the worker can pass the account/opportunity history, recent outputs, and uploaded transcript context into the runtime.
- **Self-checks:** the runtime can verify required output sections and source coverage before finalizing.
- **Source coverage and artifact generation:** the output Markdown must still include `At a Glance`, `Source Coverage`, and skill-specific required sections; sidecar validation is run after generation.

The runtime is executed inside an isolated sandbox:

- One sandbox per job.
- No access to the host filesystem except explicitly mounted allowlisted paths.
- No environment variables from the host except a short allowlist.
- No unrestricted shell; no `bypassPermissions`; no arbitrary Git; no browser/computer automation; no local repository access; no Live Transcribe; no arbitrary outbound network.
- Network egress is deny-by-default and allowlisted per job type.
- The sandbox image is rebuilt from a known base; ephemeral data is destroyed after the job completes.

## Hosted vs local runtime distinction

| Capability | Hosted runtime | Local runtime |
|---|---|---|
| Identity | Supabase Auth / org membership | OS user / Claude Code user |
| Skill invocation | Worker sandbox with allowlisted tools | `claude -p` with `acceptEdits` default; reviewed shell skills can use `bypassPermissions` |
| File access | Mounted allowlisted files only | Full local workspace, `~/.claude/skills/`, repos |
| Network | Allowlist only | Host network |
| MCPs/tools | Approved, audited subset | User's full `~/.claude.json` MCP config |
| Git / shell | Not available | Available |
| Browser/computer automation | Not available | Available (`pov-gsheet`, etc.) |
| Live Transcribe | Not available | Local audio capture |
| Source-code repo reasoning | Limited to bundled or fetched reference data | Full local `airbyte` / `airbyte-platform` clones |

## Failure and retry flow

1. **API enqueue:** `POST /api/jobs` inserts a `queued` job record and returns `job_id`.
2. **Worker claim:** a worker atomically claims a `queued` job by transitioning it to `running` with a `worker_id` and `started_at`. A compare-and-set on `status` ensures only one worker owns an attempt.
3. **Sandbox run:** the worker creates the sandbox, mounts the allowlisted inputs by reference, and runs the skill runtime.
4. **Success:** the worker writes the output and sidecar, updates the job to `success` with `result_output_id`, and records `finished_at`.
5. **Retryable failure:** transient errors (sandbox timeout, model rate limit, storage write failure) increment `attempts` and return the job to `queued`/`retry-wait` with a backoff. `retry-wait` is a queue/scheduling concept, not a primary job status. After `max_attempts` the job moves to `failure` or a dead-letter queue.
6. **Permanent failure:** the job moves to `failure`; `stderr`/logs are redacted and persisted; the user sees a failure state with guidance.
7. **Cancellation/timeout:** a `running` job can be cancelled by the user or by a timeout, moving to `cancelled` or `timeout` respectively.
8. **API/worker crash:** jobs in `running` without a heartbeat become claimable again or are marked lost; the ledger is the source of truth. The intended invariant is at-least-once recovery, not exactly-once execution; workers must make result persistence idempotent where practical.

## Clearly identified unresolved decisions

These decisions are intentionally deferred to the implementation slices and must be resolved before the corresponding code is merged:

- **Queue implementation:** Postgres-backed advisory locks vs. a lightweight message queue. Redis is not required unless workload analysis shows a need.
- **Worker framework:** containerized workers (for example, Fly Machines, ECS Fargate, Kubernetes Jobs) vs. a process pool on a VM. The sandbox technology depends on this choice.
- **Sandbox technology:** gVisor, Firecracker, unprivileged containers, or another isolation layer.
- **Model provider and runtime:** whether the hosted runtime continues to use Claude Code/`claude` CLI or switches to Anthropic API/Agent SDK/Bedrock/etc.
- **Supabase commitment:** whether Supabase Auth, Postgres, and Storage are approved as the operational backend or are replaced by another Airbyte-standard provider.
- **Credential storage:** whether to use Supabase Vault, AWS Secrets Manager, HashiCorp Vault, or another encrypted store for OAuth tokens and integration credentials.
- **Salesforce/Gong/Google integrations:** whether the beta includes these integrations and, if so, how user-consented OAuth credentials are stored and scoped.
- **Observability:** logging, metrics, and tracing backend for workers and sandbox.
