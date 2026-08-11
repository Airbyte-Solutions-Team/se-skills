# SE Skills — Hosted Beta Architecture

## Current architecture

- `webapp/app.py` is a FastAPI composition root. It constructs `OutputService`, `JobService`, `AccountService`, `OverviewService`, `AskService`, `TranscriptionService`, `SkillRuntimeService`, and `SalesforceIntegration`; mounts `webapp/static/`; and registers routes.
- The frontend is a vanilla-JS SPA (`static/index.html`, `app.js`, `style.css`) served by FastAPI's `StaticFiles`.
- Business logic lives in `webapp/services/` and `webapp/routes/`. The current data layer is the local filesystem under `~/airbyte-work/01-customers/` plus `<workspace>/.state/` snapshots.
- Skills are Markdown prompt files under `skills/<skill>/SKILL.md`. The webapp invokes them with `claude -p ... --permission-mode acceptEdits` from `~/airbyte-work`.
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
- FastAPI validates the session, resolves the user's current organization from membership, and applies organization-scoped authorization on every route.
- No server-side rendering; the SPA calls JSON/REST endpoints.

## Auth, database, and storage boundaries

- **Identity:** Supabase Auth (working hypothesis). Users authenticate with organization credentials or SSO. `auth.users` holds identity; `memberships` links users to organizations with roles.
- **Relational data:** Postgres managed by Supabase (working hypothesis). All tenant-scoped tables carry `org_id`. Row-level security policies enforce that a query can only touch rows whose `org_id` matches the user's active membership.
- **Private storage:** Supabase Storage private buckets (working hypothesis). Objects are stored under organization-scoped prefixes. The API generates short-lived signed URLs or streams objects through the API so the SPA never holds broad storage credentials.
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

- Persist job records with `status`, `payload`, `result_output_id`, `attempts`, `max_attempts`, `started_at`, `finished_at`, `error`, `runtime_version`, `worker_id`.
- Provide at-least-once delivery to workers; support retry with backoff; support dead-letter / poison-pill handling.
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
| Skill invocation | Worker sandbox with allowlisted tools | `claude -p --permission-mode acceptEdits` |
| File access | Mounted allowlisted files only | Full local workspace, `~/.claude/skills/`, repos |
| Network | Allowlist only | Host network |
| MCPs/tools | Approved, audited subset | User's full `~/.claude.json` MCP config |
| Git / shell | Not available | Available |
| Browser/computer automation | Not available | Available (`pov-gsheet`, etc.) |
| Live Transcribe | Not available | Local audio capture |
| Source-code repo reasoning | Limited to bundled or fetched reference data | Full local `airbyte` / `airbyte-platform` clones |

## Failure and retry flow

1. **API enqueue:** `POST /api/jobs` inserts a `pending` job record and returns `job_id`.
2. **Worker claim:** a worker marks the job `running` with a `worker_id` and `started_at` using a compare-and-set on status.
3. **Sandbox run:** the worker creates the sandbox, mounts inputs, and runs the skill runtime.
4. **Success:** the worker writes the output and sidecar, updates the job to `done` with `result_output_id`, and records `finished_at`.
5. **Retryable failure:** transient errors (sandbox timeout, model rate limit, storage write failure) increment `attempts` and return the job to `pending`/`retry` with a backoff. After `max_attempts` the job moves to `error`/`dead-letter`.
6. **Permanent failure:** the job moves to `error`; `stderr`/logs are redacted and persisted; the user sees a failure state with guidance.
7. **API/worker crash:** jobs in `running` without a heartbeat become claimable again or are marked lost; the ledger is the source of truth.

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
