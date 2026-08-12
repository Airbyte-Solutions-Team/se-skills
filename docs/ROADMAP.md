# SE Skills — Productionalization Roadmap

This is the source of truth for productionalization slices and progress. Each slice has a product outcome, scope, dependencies, acceptance criteria, non-goals, and a simple status: **Proposed**, **Ready**, **In Progress**, **Complete**.

## Status model

- `Proposed` — identified, not ready to start
- `Ready` — scoped, dependencies and acceptance criteria are known
- `In Progress` — implementation is underway
- `Complete` — merged, with a reference to the implementation PR

---

## Slice 1: Architecture baseline

**Status:** `Complete` (PR #39)

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

**Status:** `Complete` (PR #40)

**Product outcome:** Users can sign in, belong to the single Airbyte beta organization, and create/list accounts scoped to that organization.

**Scope:**
- Supabase Auth with Google OAuth for Airbyte users (Product Owner-approved for the beta). Access is invite/pre-provisioned-membership only; an `@airbyte.com` email may be an onboarding check, but email domain is not the authorization boundary.
- `organizations`, `memberships`, `accounts`, and `opportunities` tables with `org_id`, `created_by`, `assigned_to`, timestamps, uniqueness, and relationship constraints.
- Postgres RLS with `app_user` (`NOBYPASSRLS`). The request context is a signed tenant token (`user_id:hmac(user_id, secret)`) set with transaction-scoped `SET LOCAL app.context_token`. The secret lives in an `app_private` schema that `app_user` cannot read; `SECURITY DEFINER` functions owned by `app_admin` verify the HMAC and active membership for every RLS check. The web process never holds a `BYPASSRLS` admin pool.
- Least-privilege `app_user` grants: explicit `SELECT/INSERT/UPDATE/DELETE` on `accounts` and `opportunities`; `SELECT` on `users`, `organizations`, and `memberships`; `EXECUTE` on the membership resolver functions; no broad or default privileges. Default `PUBLIC` execute on `SECURITY DEFINER` functions is revoked and only `app_user` is granted explicit execute.
- FastAPI authentication and organization-scoped authorization helpers that resolve organization context from the authenticated user's active membership and never trust a browser-supplied `org_id` or `user_id`.
- Organization-scoped `accounts` list/create APIs and the `opportunities` data layer required by Slice 2.
- Minimal SPA sign-in, signed-out, loading, error, account-list, and account-create states. The hosted SPA does not call local skill/execution endpoints.
- `HOSTED_MODE=1` opt-in with fail-closed startup: local filesystem, integration, transcription, skill, and shell routes are not registered in hosted mode.

**Dependencies:** Slice 1.

**Acceptance criteria:**
- Authenticated members can list and create accounts in the Airbyte organization.
- Unauthenticated, inactive-membership, and non-member requests are rejected.
- A supplied or spoofed `org_id` cannot influence authorization.
- `assigned_to` is metadata and does not change organization visibility.
- Direct database access under the authenticated role cannot read or mutate another organization's rows.
- Cross-organization opportunity/account relationships fail at the database boundary.
- Relevant API authorization failures are covered by tests.
- Existing deterministic evaluations and local workflow tests continue to pass.
- No local filesystem state is required for sign-in or account CRUD.

**Non-goals:**
- No skill execution.
- No transcript upload.
- No worker or queue.
- No Salesforce/Gong/Google OAuth.
- No migration of local accounts.

---

## Slice 3: Transcript upload and private storage

**Status:** `Complete` (PR #41)

**Product outcome:** SEs can upload a transcript for an account/opportunity and have it stored privately, organization-scoped, and referenced by the API.

**Scope:**
- Private object storage using Supabase Storage (Product Owner-approved for the beta). The `transcripts` bucket is private and RLS policies target a dedicated `app_storage` Postgres role. FastAPI signs a short-lived `app_storage` JWT with the server-side `SUPABASE_JWT_SECRET` for each Storage REST call; the browser-visible `authenticated` role has no `storage.objects` privileges.
- `transcripts` table with `org_id`, `account_id`, optional `opportunity_id`, server-generated `storage_path`, `original_filename`, `size_bytes`, `mime_type`, `uploaded_by`, and timestamps. Composite foreign keys enforce same-organization relationships.
- FastAPI upload/list/download/delete endpoints that stream through the API, validate content independently of browser filename/MIME, and sign a short-lived `app_storage` JWT for Supabase Storage REST calls (public `SUPABASE_ANON_KEY` only as the `apikey` project identifier; no service-role key or browser `authenticated` JWT in normal request paths).
- SPA account/opportunity selection, upload progress/list, download, and delete states.
- `TRANSCRIPT_MAX_BYTES` default of 10 MiB; accepted `.txt`, `.md`, `.vtt`, `.srt`; rejection of HTML, archives, executables, binary files, invalid UTF-8, and NUL bytes.

**Dependencies:** Slice 2.

**Acceptance criteria:**
- Authenticated active members can upload, list, download, and delete transcripts for an account in their organization.
- Uploading against an opportunity succeeds only when the opportunity belongs to the selected account and organization.
- Unauthenticated, inactive-membership, and non-member requests are rejected.
- Browser-supplied or spoofed `org_id`, `user_id`, storage path, `account_id`, or `opportunity_id` cannot bypass authorization.
- Cross-organization transcript/account and transcript/opportunity relationships fail at the database boundary.
- Direct `app_user` database access cannot read or mutate another organization's transcript metadata.
- Direct Storage requests using a user token cannot read, write, replace, list, or delete another organization's objects; anonymous access fails.
- The API never uses or exposes a service-role credential during normal transcript operations.
- Invalid types, oversized files, invalid UTF-8, NUL-containing content, unsafe filenames, and empty files are rejected safely.
- Upload and deletion partial-failure behavior is covered by tests.
- Hosted mode exposes no local filesystem, skill, shell, transcription, or integration routes.
- Existing Slice 2 hosted tests, non-hosted tests, and deterministic evaluations continue to pass.
- `docs/ARCHITECTURE.md`, `docs/SECURITY.md`, `docs/DATA_MODEL.md`, and `webapp/README.md` reflect the approved provider and trust boundary.

**Non-goals:**
- No transcript parsing, chunking, summarization, or transcription.
- No PDF, DOCX, audio, video, image, ZIP, or arbitrary document uploads.
- No Live Transcribe, Gong, Salesforce, or Google imports.
- No account deletion or retention-policy implementation.
- No jobs, workers, queues, agent runtime, skill execution, or model calls.
- No public object URLs or permanent signed URLs.
- No production deployment or real customer-data migration.
- No broad frontend redesign.

---

## Slice 4: Durable asynchronous jobs

**Status:** `Complete` (draft PR)

**Product outcome:** An authenticated member can select an uploaded transcript, enqueue a durable `post-call` job, a separate worker claims and runs it, and the member sees the terminal job status. All state is persisted in Postgres; no job state depends on process memory or local filesystem.

**Scope:**
- `jobs` and `job_attempts` tables with explicit `org_id`, composite foreign keys to `accounts`, `opportunities`, and `transcripts` that enforce same-organization ownership, `requester_id`, `cancelled_by`, `skill`, `skill_version`, `model`, `runtime_version`, `payload`, `input_refs`, `source_manifest`, `result_output_id` (NULL until Slice 5), `validation_status`, `token_usage`, `cost`, `attempts`, `max_attempts`, `timeout_at`, `cancel_requested_at`, `cancelled_at`, `next_attempt_after`, `dead_lettered`, and redacted `error`.
- Postgres-backed queue using transactional `FOR UPDATE SKIP LOCKED` row claiming, expiring leases, heartbeats, and a `recover_expired_leases()` function. Redis is not used.
- Dedicated least-privilege `app_worker` Postgres role (`NOBYPASSRLS`) that can only execute the queue functions; no direct tenant table access.
- `SECURITY DEFINER` queue functions (`enqueue_job`, `claim_next_job`, `worker_heartbeat`, `complete_job`, `fail_job`, `cancel_job`, `recover_expired_leases`) owned by `app_admin`, with empty `search_path`, revoked `PUBLIC` execute, and explicit transition validation.
- Separate polling worker entry point (`scripts/run_hosted_worker.py`) and `webapp/hosted/worker.py` `Worker` class with graceful shutdown.
- Deterministic `EchoExecutor` that proves claim, heartbeat, completion, failure, retry, timeout, cancellation, and crash recovery without executing a skill, model, shell command, container, or sandbox.
- Organization-scoped enqueue (`POST /api/hosted/accounts/{account_id}/jobs`), list (`GET /api/hosted/accounts/{account_id}/jobs`), detail (`GET /api/hosted/jobs/{job_id}`), and cancel (`POST /api/hosted/jobs/{job_id}/cancel`) APIs.
- Minimal SPA job-status and enqueue UI on the hosted transcript list.
- Updated `docs/ARCHITECTURE.md`, `docs/DATA_MODEL.md`, `docs/SECURITY.md`, `docs/ROADMAP.md`, `PRODUCTIONALIZATION.md`, and `webapp/README.md` with queue, lease, worker-role, retry, cancellation, timeout, idempotency, and recovery design; distinguishes Slice 4's deterministic executor from Slice 5's unresolved sandbox and hosted agent-runtime decisions.

**Dependencies:** Slice 2, Slice 3.

**Acceptance criteria:**
- Authenticated active members can enqueue a job for an accessible transcript and receive a durable `job_id`.
- Unauthenticated, inactive-member, non-member, spoofed-org, and cross-organization requests fail.
- Same-organization mismatched `account`/`opportunity`/`transcript` relationships fail at the API and database boundaries.
- Direct `app_user` access cannot read or mutate another organization's jobs or attempts.
- Two concurrent workers cannot claim the same attempt; claim and attempt creation are atomic.
- A worker can heartbeat and complete only with the current lease token; a stale worker cannot mutate a job after lease expiry and reclamation.
- API or worker restart does not lose queued/running job state.
- Expired leases are recovered deterministically; retries are bounded, backoff is persisted, attempt history is append-only, and exhausted jobs are visibly `dead_lettered` without inventing a new primary status.
- Queued and running cancellation behavior is covered; `timeout` is distinct from `failure`.
- Illegal transitions and mutation of terminal jobs fail at the database boundary.
- Idempotent enqueue replay returns the same job; conflicting reuse returns a safe `409` conflict.
- Job payloads and persisted/logged errors contain no transcript body or secrets.
- The worker identity can invoke only its required queue operations and cannot assume migration/admin roles or directly access unrelated tenant data.
- Existing Slice 2 and Slice 3 hosted tests, all non-hosted tests, deterministic evaluations, and local workflows continue to pass.

**Non-goals:**
- No actual post-call skill execution.
- No model-provider or agent-runtime integration.
- No output generation, output Storage bucket, or output review.
- No sandbox, container orchestration, unrestricted subprocess, shell, Git, browser automation, or outbound network.
- No Redis or externally hosted queue.
- No production deployment, paid infrastructure provisioning, or real customer-data migration.
- No Salesforce, Gong, Google, or other integration work.
- No broad SPA rewrite.

---

## Slice 5: Isolated hosted post-call runtime

**Status:** `Proposed`

**Product outcome:** A `post-call` skill can run asynchronously in an isolated sandbox and produce a validated Markdown output from an uploaded transcript.

**Scope:**
- Sandbox technology decision and integration.
- Agent runtime that preserves multi-step behavior: source/file discovery, full transcript reads, tool use, prior context, self-checks, source coverage, artifact generation.
- `post-call` skill packaging and allowlist for files, tools, network, credentials.
- Define and test the production validation contract for `post-call` (decision-critical structure and Source Coverage requirements necessary to present the output as validated; `webapp/output_schema.py` does not currently define a post-call schema).
- Output sidecar generation and validation.
- Worker integration: claim job, run sandbox, write output, update job.

**Dependencies:** Slice 3, Slice 4.

**Acceptance criteria:**
- `post-call` runs from an uploaded transcript and produces Markdown + sidecar.
- Sandbox has no host filesystem access except mounted transcript and read-only reference data.
- Sandbox network egress is allowlisted.
- No unrestricted shell, `bypassPermissions`, Git, browser automation, or local repo access.
- Output includes required sections (`At a Glance`, `Source Coverage`, etc.).
- The `post-call` validation contract is documented, implemented, and tested before the slice is marked complete.
- Validation status is recorded and surfaced in the UI.
- `outputs` to `jobs`/`accounts`/`opportunities` preserve `org_id` and are enforced by the database; cross-org references fail at the DB boundary and in application authorization tests.

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
- Exports use the same `nh3` allowlist as local to avoid unsanitized HTML; secrets are redacted, and customer content only appears where the artifact is intended to contain it.
- `reviews` and `output_versions` to `outputs` preserve `org_id` and are enforced by the database; cross-org references fail at the DB boundary and in application authorization tests.
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
