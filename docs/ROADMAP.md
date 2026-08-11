# SE Skills — Productionalization Roadmap

This is the source of truth for productionalization slices and progress. Each slice has a product outcome, scope, dependencies, acceptance criteria, non-goals, and a simple status: **Proposed**, **Ready**, **In Progress**, **Complete**.

## Status model

- `Proposed` — identified, not ready to start
- `Ready` — scoped, dependencies and acceptance criteria are known
- `In Progress` — implementation is underway
- `Complete` — merged, with a reference to the implementation PR

---

## Slice 1: Architecture baseline

**Status:** `In Progress` (this PR)

**Product outcome:** A shared, reviewed architecture and security contract for the hosted beta, so future slices have a source of truth and do not re-litigate decisions.

**Scope:**
- `PRODUCTIONALIZATION.md`, `docs/PRODUCT.md`, `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md`, `docs/SECURITY.md`, `docs/ROADMAP.md`
- Reconcile `IMPLEMENTATION-PLAN.md` and update `README.md` with links.
- No infrastructure, migrations, auth, workers, or runtime code.

**Dependencies:** None.

**Acceptance criteria:**
- All architecture and security docs are merged to `main`.
- The product, architecture, data model, security, and roadmap docs are internally consistent.
- `git diff --check` passes.
- `README.md` links to `PRODUCTIONALIZATION.md`.

**Non-goals:**
- No Supabase project creation.
- No database migrations.
- No authentication code.
- No Docker/deployment configuration.
- No queue implementation.
- No worker implementation.
- No agent SDK/runtime implementation.
- No frontend rewrite.
- No OAuth implementation.
- No migration of existing customer data.
- No changes to skill behavior.

---

## Slice 2: Auth, organization, and data foundation

**Status:** `Ready`

**Product outcome:** Users can sign in, belong to an organization, and create/list accounts scoped to that organization.

**Scope:**
- Identity provider integration (Supabase Auth is the working hypothesis).
- `organizations` and `memberships` tables; active-organization resolution.
- `accounts` and `opportunities` tables with `org_id`, `created_by`, `assigned_to`, and RLS policies.
- FastAPI authentication middleware and organization-scoped authorization helpers.
- SPA login flow and org switcher.

**Dependencies:** Slice 1.

**Acceptance criteria:**
- A user can sign in and see only their organization's accounts.
- Creating an account sets `org_id` and `created_by`.
- `assigned_to` is metadata and does not change visibility.
- Direct SQL queries with a different `org_id` return no rows due to RLS.
- No local filesystem state is required for sign-in or account CRUD.

**Non-goals:**
- No skill execution.
- No transcript upload.
- No worker or queue.
- No Salesforce/Gong/Google OAuth.
- No migration of local accounts.

---

## Slice 3: Transcript upload and private storage

**Status:** `Proposed`

**Product outcome:** SEs can upload a transcript for an account/opportunity and have it stored privately, organization-scoped, and referenced by the API.

**Scope:**
- Private object storage integration (Supabase Storage is the working hypothesis).
- `transcripts` table with `org_id`, `account_id`, `opportunity_id`, `storage_path`, `uploaded_by`.
- Upload API and SPA UI; signed-URL or streaming download.
- Validation: file type, size limits, safe filename handling.

**Dependencies:** Slice 2.

**Acceptance criteria:**
- Uploaded transcripts are stored under an org-scoped path.
- The SPA cannot access storage without the API.
- Listing transcripts for an account only returns transcripts in the active organization.
- Deleting an account also removes or marks transcripts for deletion.

**Non-goals:**
- No transcript parsing or transcription.
- No skill execution.
- No live audio capture.
- No Gong/Salesforce import.

---

## Slice 4: Durable asynchronous jobs

**Status:** `Proposed`

**Product outcome:** The API can enqueue a skill job, a worker can claim and run it, and job state survives API/worker restarts without local filesystem state.

**Scope:**
- `jobs` table (job ledger) with `status`, `payload`, `attempts`, `max_attempts`, `started_at`, `finished_at`, `error`, `worker_id`.
- Queue mechanism (Postgres-backed advisory-lock or lightweight queue; Redis not required unless workload need is demonstrated).
- Worker harness that polls/claims jobs and runs an ephemeral container/process.
- Retry and dead-letter behavior.
- API endpoints to create, list, and get job status.

**Dependencies:** Slice 2 (data model), Slice 3 (optional, but jobs need at least account context).

**Acceptance criteria:**
- Enqueuing a job returns a durable `job_id`.
- A worker can claim a job exactly once.
- A worker crash leaves the job in a recoverable state.
- Retry counters and dead-letter state are persisted in Postgres.
- No job state lives only in a local file or in-memory dict.

**Non-goals:**
- No actual skill runtime.
- No sandbox isolation (a no-op or echo worker is acceptable).
- No model provider integration.

---

## Slice 5: Isolated hosted post-call runtime

**Status:** `Proposed`

**Product outcome:** A `post-call` skill can run asynchronously in an isolated sandbox and produce a validated Markdown output from an uploaded transcript.

**Scope:**
- Sandbox technology decision and integration.
- Agent runtime that preserves multi-step behavior: source/file discovery, full transcript reads, tool use, prior context, self-checks, source coverage, artifact generation.
- `post-call` skill packaging and allowlist for files, tools, network, credentials.
- Output sidecar generation and validation (`OutputMetadata` equivalent in Postgres/object storage).
- Worker integration: claim job, run sandbox, write output, update job.

**Dependencies:** Slice 3, Slice 4.

**Acceptance criteria:**
- `post-call` runs from an uploaded transcript and produces Markdown + sidecar.
- Sandbox has no host filesystem access except mounted transcript and read-only reference data.
- Sandbox network egress is allowlisted.
- No unrestricted shell, `bypassPermissions`, Git, browser automation, or local repo access.
- Output includes required sections (`At a Glance`, `Source Coverage`, etc.).
- Validation status is recorded and surfaced in the UI.

**Non-goals:**
- Not all skills.
- No Live Transcribe.
- No browser/computer automation.
- No local `airbyte` / `airbyte-platform` repo access.
- No `pov-gsheet` Google automation.

---

## Slice 6: Review, export, and beta readiness

**Status:** `Proposed`

**Product outcome:** SEs can review, correct, approve, and export the hosted `post-call` output, completing the first end-to-end beta workflow.

**Scope:**
- Review/correction UI and API (`reviews` table).
- Output validation status and reference-freshness surfacing.
- PDF and MD export; optional internal HTML export.
- Audit logging (`audit_events` table).
- Basic admin/org settings (members, roles, data retention view).
- Onboarding and beta runbook.

**Dependencies:** Slice 3, Slice 5.

**Acceptance criteria:**
- End-to-end workflow: sign in → create account → upload transcript → run `post-call` → review → correct/approve → export.
- Review/correction history is persisted and visible.
- Audit events cover upload, run, review, export.
- Exports are sanitized (same `nh3` allowlist as local).
- Beta launch checklist (security review, observability, runbook) is complete.

**Non-goals:**
- No Salesforce/Gong/Google integrations.
- No full suite of skills.
- No local-to-hosted data migration.
- No generic SaaS billing or multi-org signup.

---

## Future (post-beta)

- Additional hosted skills (`biz-qual`, `tech-qual`, `connector-feasibility`, etc.) after each is reviewed for sandbox allowlists.
- Integration connections with user-consented OAuth (Salesforce, Gong, Google).
- Live Transcribe only if a separately approved hosted audio architecture is defined.
- Multi-organization SaaS considerations, if any.
