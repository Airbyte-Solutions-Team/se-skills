# SE Skills — Hosted Beta Data Model

## Overview

All durable tenant-scoped records are owned by an organization. Users access those records through memberships. `created_by` and `assigned_to` are metadata, not the tenancy boundary. The beta supports one Airbyte organization, though the schema is extensible to multiple organizations in the future. Row-level security (RLS) policies in Postgres enforce organization isolation; the application layer re-checks the same invariant before returning data.

## Conceptual entities

### `organizations`

A single Airbyte organization is the beta tenant boundary.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `slug` | URL-friendly unique identifier |
| `name` | Display name |
| `created_at` / `updated_at` | Timestamps |

### `users` (identity provider)

Managed by the identity provider (Supabase Auth working hypothesis). The application references `users.id` in `created_by`/`assigned_to` columns but does not own the user table.

### `memberships`

Many-to-many link between users and organizations, with a role.

| Column | Purpose |
|---|---|
| `user_id` (FK to users) | Identity |
| `org_id` (FK to organizations) | Organization |
| `role` | e.g. `admin`, `se`, `viewer` |
| `joined_at` | When the user joined the org |
| PK | `(user_id, org_id)` |

A user can belong to multiple organizations in the conceptual schema, but the beta supports a single Airbyte organization. The API resolves `org_id` from the user's active membership for that organization and never from user-provided input alone.

### `accounts`

A customer account. Belongs to an organization.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `name` | Display name |
| `slug` | URL-friendly, unique per `org_id` |
| `owner_id` (FK to users, nullable) | Assigned SE (metadata, not tenancy) |
| `created_by` (FK to users) | Who created the account |
| `sfdc_name` | Optional Salesforce account name |
| `archived` | Boolean |
| `created_at` / `updated_at` | Timestamps |

Constraints: `UNIQUE(org_id, slug)`. RLS: `org_id` must equal the user's active organization.

### `opportunities`

A sales opportunity under an account. Belongs to the organization (not just the account) so RLS can be simple and direct.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `account_id` (FK) | Parent account |
| `name` | Display name |
| `slug` | URL-friendly, unique per `account_id` |
| `stage` | Sales stage (string) |
| `amount` | Numeric |
| `close_date` | Date |
| `type` | Opportunity type |
| `is_closed` | Boolean |
| `ae` | Assigned AE name/email |
| `created_by` / `assigned_to` (FK to users, nullable) | Metadata |
| `created_at` / `updated_at` | Timestamps |

Constraints: `UNIQUE(account_id, slug)` (which also implies per-org because account is per-org). RLS on `org_id`.

### `transcripts` (and other documents)

Uploaded customer artifacts. The object bytes live in Supabase Storage private buckets; Postgres keeps metadata.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID (also embedded in the generated storage path) |
| `org_id` (FK, indexed) | Organization owner |
| `account_id` (FK) | Account |
| `opportunity_id` (FK, nullable) | Opportunity |
| `storage_path` | Server-generated private object-storage key (`org_id/account_id/{opportunity_id/}transcripts/{transcript_id}`) |
| `original_filename` | Original filename, kept only as metadata |
| `size_bytes` | File size |
| `mime_type` | MIME type (derived from original filename) |
| `uploaded_by` (FK to users) | Uploader |
| `tombstoned_at` | Timestamp; set when a deletion is accepted and the row becomes invisible to the organization |
| `delete_requested_by` (FK to users) | Member whose request tombstoned the row |
| `cleanup_state` | `none`, `pending`, `complete`; `complete` is the only proof the private object is gone |
| `cleanup_completed_at` | Timestamp of the reconciled Storage delete (nullable) |
| `cleanup_attempts` | Bounded reconciliation attempt counter |
| `cleanup_claimed_by` / `cleanup_claimed_at` | Lease held by the worker currently reconciling this tombstone (nullable) |
| `cleanup_last_error` | Truncated failure category from the last attempt (nullable) |
| `created_at` | Timestamp |
| `updated_at` | Timestamp |

Constraints:
- `CHECK (cleanup_state IN ('none', 'pending', 'complete'))`
- `CHECK ((tombstoned_at IS NULL AND cleanup_state = 'none' AND cleanup_completed_at IS NULL) OR (tombstoned_at IS NOT NULL AND cleanup_state IN ('pending', 'complete')))` — cleanup state cannot exist without a tombstone, and a live transcript can never look reconciled
- `FOREIGN KEY (account_id, org_id) REFERENCES accounts(id, org_id)`
- `FOREIGN KEY (opportunity_id, account_id, org_id) REFERENCES opportunities(id, account_id, org_id)`
- `FOREIGN KEY (uploaded_by, org_id) REFERENCES memberships(user_id, org_id)`
- `org_id` references `public.organizations`
- Indexes on `org_id`, `(account_id, org_id)`, `(opportunity_id, org_id)`, and `uploaded_by`

RLS on `org_id` using the same signed tenant-context token as accounts and opportunities. Storage object access uses a dedicated `app_storage` Postgres role: FastAPI signs a short-lived JWT (`role: "app_storage"`, `sub: user_id`) with the server-side `SUPABASE_JWT_SECRET` for Supabase Storage REST calls, and the browser-visible `authenticated` role has no `storage.objects` privileges. The API streams file contents through FastAPI; the SPA never holds storage credentials. Uploads are validated for size (`TRANSCRIPT_MAX_BYTES`, default 10 MiB), allowed text extensions (`.txt`, `.md`, `.vtt`, `.srt`), valid UTF-8, no NUL bytes, and disallowed HTML/executable/binary signatures before any object or metadata is created.

**Slice 6B2A1 deletion lifecycle (implemented).** Deletion is a durable tombstone plus asynchronous Storage reconciliation, because Storage and Postgres share no transaction and the previous delete-object-then-delete-row order could leave a listable row with no object and no evidence:

1. `public.request_transcript_deletion(context_token, account_id, opportunity_id, transcript_id)` (`app_admin`-owned `SECURITY DEFINER`, `SET search_path = ''`) derives the actor from the signed tenant context and the organization from the locked transcript row, and returns `SE020` (indistinguishable for missing, cross-organization, and wrong-account/opportunity requests) or `SE021` when a `queued` or `running` job still references the transcript. Deletion never implicitly cancels a job.
2. Accepted requests set `tombstoned_at`, `delete_requested_by`, and `cleanup_state = 'pending'` and record exactly one `transcript_delete` audit event in the same transaction. A replayed or concurrent duplicate request returns `already_deleted` and appends no second event.
3. The `SELECT`/`INSERT` policies for `app_user` carry `tombstoned_at IS NULL`, and `app_user` holds no `UPDATE`/`DELETE` on the table, so a tombstoned row is invisible to lists, downloads, corrections, and new runs the moment the tombstone commits. `public.enqueue_job` rejects a tombstoned transcript at the database boundary, so the delete/enqueue race has one safe winner.
4. A worker claims one tombstone with `public.claim_next_transcript_cleanup(worker_id, lease_seconds, max_attempts)` (`FOR UPDATE SKIP LOCKED`, oldest first, bounded attempts, expired leases reclaimable) and receives only the transcript id, organization, trusted `storage_path`, and attempt count.
5. It deletes that one object with the `app_storage_maintenance` identity, then calls `public.finalize_transcript_cleanup(...)`, which marks `complete` only when the caller still owns the claim. A missing object counts as reconciled; any ambiguous Storage failure calls `public.release_transcript_cleanup(...)`, which keeps the row `pending`, clears the lease, and stores a truncated error, so a restarted worker retries it.
6. The row itself is retained as the hidden provenance anchor for terminal jobs, outputs, reviews, and export evidence. A privileged physical delete is guarded by `app_private.assert_transcript_delete_is_reconciled()`, which raises `SE022` unless the row is tombstoned and `complete`, and emits no user audit event. Retention/purge policy for reconciled tombstones is Slice 6B2B.

### `jobs`

Durable job ledger for asynchronous skill execution.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner; every job and attempt has explicit non-null `org_id` |
| `account_id` (FK) | Account |
| `opportunity_id` (FK, nullable) | Opportunity |
| `transcript_id` (FK) | Uploaded transcript input; composite FK `(transcript_id, account_id, org_id)` enforces same-organization ownership |
| `requester_id` (FK to users) | User who invoked the job; derived from the authenticated session |
| `skill` | Skill id, e.g. `post-call` |
| `skill_version` | Skill/prompt version or content hash |
| `model` | Model selected for the run |
| `runtime_version` | Sandbox/runtime image version |
| `status` | Primary lifecycle: `queued`, `running`, `success`, `failure`, `cancelled`, `timeout` |
| `payload` | JSON: runtime config, input references, and source/evidence manifest. Must not contain raw transcript bodies or secrets. |
| `input_refs` | Stable ids of transcripts/documents/prior outputs mounted for the run |
| `source_manifest` | JSON: references, versions, and lineage needed to reconstruct the run context |
| `result_output_id` (FK to outputs, nullable) | Output produced by a successful run |
| `validation_status` | `unvalidated`, `valid`, `invalid` (status reported by the worker after sidecar validation) |
| `token_usage` | JSON: input/output token counts (where available) |
| `cost` | Estimated cost if available |
| `attempts` | Counter of execution attempts |
| `max_attempts` | Maximum allowed attempts |
| `started_at` / `finished_at` | Timestamps |
| `timeout_at` | Deadline after which a running attempt is considered timed out |
| `cancel_requested_at` | Set when a running job is requested to cancel |
| `next_attempt_after` | Earliest time a requeued job is eligible for the next claim; used for bounded backoff |
| `dead_lettered` | True when `max_attempts` has been exhausted |
| `cancelled_at` / `cancelled_by` | Cancellation timestamp and actor |
| `worker_id` | Worker that claimed the current attempt |
| `error` | Redacted failure summary (no raw stack traces or secrets) |
| `created_at` | Timestamp |
| `updated_at` | Timestamp |

RLS on `org_id`. The queue claims a job by atomically transitioning `status` from `queued` to `running` with `FOR UPDATE SKIP LOCKED` in a single transaction that also creates the first `job_attempts` row, increments `attempts`, assigns `worker_id`, and sets `timeout_at`. The intended invariant is an atomic/exclusive claim per attempt, at-least-once recovery, bounded retries, and no silent job loss on worker/API crashes.

Primary job statuses: `queued`, `running`, `success`, `failure`, `cancelled`, `timeout`. `retry-wait` and dead-letter are scheduling metadata, not primary job statuses. The initial hosted job accepts exactly one uploaded transcript belonging to the selected account/opportunity.

### `job_attempts`

Append-only attempt history for a job. `jobs` remains the aggregate ledger; `job_attempts` records each execution attempt.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK to organizations, indexed) | Organization owner; must match the parent `jobs.org_id` |
| `job_id` (FK) | Parent job; composite FK `(job_id, org_id)` enforces same-organization parent |
| `attempt_number` | Integer, starting at 1 |
| `worker_id` | Worker/runtime that ran this attempt |
| `runtime_version` | Actual sandbox/runtime image version recorded for this attempt |
| `model` | Actual model recorded for this attempt |
| `lease_token` | Heartbeat/lease token for claim liveness (nullable) |
| `started_at` / `finished_at` | Timestamps |
| `heartbeat_at` | Last worker heartbeat/lease renewal |
| `outcome` | `success`, `failure`, `timeout`, `cancelled` |
| `error_category` | Categorized failure reason (for example, `timeout`, `model_rate_limit`, `storage_write`, `sandbox`) |
| `error` | Redacted failure summary |
| `token_usage` | JSON: input/output token counts for this attempt |
| `cost` | Estimated cost for this attempt |
| `created_at` | Timestamp |

RLS on `org_id` with an invariant that `job_attempts.org_id` equals the parent `jobs.org_id` at the database boundary (enforced by the composite FK). Each attempt is immutable once recorded. `jobs.attempts` is a derived counter, not the source of truth for retry history.

### `outputs`

A generated skill output. The original Markdown and sidecar are immutable once written; corrections are recorded as new `output_versions` and approvals reference a specific version.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID; deterministic per `job_id` + `attempt_number` to make retries idempotent |
| `org_id` (FK, indexed) | Organization owner |
| `account_id` (FK) | Account |
| `opportunity_id` (FK, nullable) | Opportunity |
| `job_id` (FK) | Job that produced it; composite FK `(job_id, account_id, org_id)` enforces same-organization ownership |
| `transcript_id` (FK) | Transcript input; composite FK `(transcript_id, account_id, org_id)` enforces same-organization ownership |
| `requester_id` (FK to memberships) | User who invoked the job |
| `skill` | Skill id |
| `skill_version` | Skill/prompt version or content hash used |
| `model` | Model used |
| `title` | Generated title |
| `content_storage_path` | Private org-scoped object-storage key for the immutable generated Markdown |
| `sidecar` | JSON: validation status, validation errors, source coverage, reference freshness, etc. |
| `validation_status` | `unvalidated`, `valid`, `invalid` |
| `tombstoned_at` | Timestamp; set when a staged/cancelled output is hidden pending Storage-object deletion |
| `cleanup_claimed_by` | Worker id that has claimed this tombstone for cleanup (nullable) |
| `cleanup_claimed_at` | Timestamp of the cleanup claim (nullable) |
| `generated_at` | Timestamp |

RLS on `org_id` with the predicate `validation_status = 'valid' AND tombstoned_at IS NULL`. The generated Markdown and sidecar are immutable; any correction creates a new `output_versions` row.

**Slice 5B1 persistence boundary (implemented):**
1. The worker resolves the authorized transcript and approved prior-context references for the job against trusted DB state.
2. It materializes only manifest-listed inputs into a host-generated, job-scoped temporary workspace; no transcript bodies, credentials, signed URLs, or browser-supplied paths enter the runtime.
3. The injected `SkillRuntime` writes `output.md` and `sidecar.json` to a temporary output area.
4. The worker reads both files outside the sandbox and runs `output_schema.parse_output` to validate them; the worker's result is authoritative and the sidecar cannot assert `validation_status`.
5. Only validated artifacts create an `outputs` row and a private org-scoped Storage object at `content_storage_path`.
6. `complete_job` receives the authoritative `result_output_id`.
7. If validation fails, the attempt is finalized with a fixed, redacted `output_error` category; no Storage object or `outputs` row is created, so invalid output is distinguishable from execution failure.
8. Compensating cleanup: if Storage write succeeds but the `outputs` row insert fails, the Storage object is deleted. If the `outputs` row insert succeeds but Storage fails, the unvalidated row is rolled back by the transaction. Retries of the same attempt reuse the deterministic `output_id` without duplicating evidence.
9. Cancellation/timeout/heartbeat loss triggers a staged-output cleanup: the `outputs` row is tombstoned, the Storage object is deleted, and only then is the row deleted. A Storage-delete or DB-delete failure leaves a hidden tombstone record so the customer content remains tracked and can be retried, and the job is finalized as a redacted `cleanup_error`. Tombstone cleanup is independently retryable after the job/attempt finalizes: `claim_tombstoned_output(...)`/`finalize_tombstone_delete(...)` form a lease-bound claim/delete/finalize path that does not require the original attempt to still be running. The worker poll loop also calls `claim_next_tombstoned_output(...)` so retries are discovered automatically without an external caller knowing the output id.

### `reviews`

Review/correction/approval actions on an output. `output_version_id` is null when the action applies to the immutable original generated `outputs` row; it references an `output_versions` row when the action applies to a correction. Approvals always reference a specific reviewed version (original or corrected); corrections create a new `output_versions` row rather than overwriting the original output.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `output_id` (FK) | Output reviewed |
| `output_version_id` (FK, nullable) | Specific `output_versions` row being reviewed/approved; `null` means the immutable original `outputs` row |
| `previous_version_id` (FK, nullable) | Previous `output_versions` row this correction follows; `null` for original-output comments/approvals or first corrections |
| `user_id` (FK to users) | Reviewer |
| `action` | `approve`, `comment`, `correct` |
| `comment` | Text |
| `request_id` (unique per output/action) | Idempotency key supplied by the caller so a retried comment/correction/approval cannot create a second row |
| `created_at` | Timestamp |

RLS on `org_id` for reads. `app_user` has no direct `INSERT`/`UPDATE`/`DELETE`:
rows are written only by the narrow `SECURITY DEFINER` functions in migration
`009`, which derive `user_id` from the signed tenant context.

### `output_versions`

Append-only versions of an output. A correction creates a new version with a corrected Markdown/sidecar; the original `outputs` row remains unchanged and serves as the generation evidence.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `output_id` (FK) | Parent output |
| `previous_version_id` (FK, nullable) | Previous version in the chain |
| `created_by` (FK to users) | User who made the correction, derived server-side from the signed tenant context |
| `content_storage_path` | Private object-storage key for this version's Markdown |
| `sidecar` | JSON: validation status, source coverage, etc. for this version |
| `change_summary` | Brief description of what changed |
| `request_id` (unique per output) | Idempotency key for the correction request |
| `created_at` | Timestamp |

RLS on `org_id` for reads; writes go only through the migration `009`
`SECURITY DEFINER` functions. Partial unique indexes enforce a single root
(`previous_version_id IS NULL`) and a single child per version, so the chain is
linear and a branched chain fails closed. The parent `outputs` row is locked
`FOR UPDATE` inside those functions, which is the serialization point for
concurrent corrections on the same base version.

The original `outputs.content_storage_path` and `outputs.sidecar` are immutable and represent the generated evidence (version 0). The first correction references the original `output_id` with `previous_version_id` null; subsequent corrections reference the prior `output_versions` row. An `approve` action may have `output_version_id` null (approving the original) or reference a specific corrected version. The final reviewed state is reconstructable from the original output and the chain of `output_versions` and `reviews`.

### `audit_events`

Provenance and audit trail.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `user_id` (FK to users, nullable) | Actor |
| `action` | `create`, `read`, `update`, `delete`, `run`, `export`, `approve`, `login`, etc. |
| `entity_type` | Table/entity name |
| `entity_id` | Affected record id |
| `request_id` | Request/correlation id |
| `metadata` | JSON: identifiers only (output id, version id, previous version id, review id, request id, action). Never Markdown, comment bodies, transcript text, prompts, model responses, account names, Storage paths, signed URLs, raw sidecars, or credentials |
| `created_at` | Timestamp |

RLS on `org_id` for reads only. Rows are written exclusively by the review
`SECURITY DEFINER` functions in the same transaction as the review evidence they
describe, so the trail is append-only and cannot be forged or rewritten by
`app_user`. Slice 6A writes `output.comment`, `output.correct`, and
`output.approve` events; Slice 6B1 adds `output_export`, whose metadata is the
organization, actor, output id, exact exported version id (null for the
generated version), request id, format, and timestamp — never the exported
bytes, the filename, or the Storage path. An export event exists only if the
returned artifact was produced, and replaying the same request id for the same
output, version, and format does not append a second row. There is no
audit-admin UI.

Slice 6B2A extends the same boundary to the remaining user actions:
`transcript_upload` and `transcript_delete` (written by `app_admin`-owned
database code on `public.transcripts`, so metadata and evidence share one
transaction) and `job_run_requested` / `job_cancel_requested` (written inside
`public.enqueue_job` and `public.request_job_cancellation` on the branch that
changes durable job state). Their metadata is server-side identifiers only —
account, opportunity, transcript, job — so every recorded value is a UUID and no
client-supplied string can reach the audit log. `jobs.skill` is excluded on
purpose: the hosted API still accepts it as unconstrained client text, and audit
metadata must not carry a browser-controlled value. There is no filename,
Storage path, transcript content, source manifest, payload, or idempotency key,
and `request_id` stays null because these actions carry no UUID request id. A
partial unique index on `(org_id, action, entity_id)` for those four actions is
the database-side backstop against duplicate evidence from a retry or a
concurrent request. Worker lifecycle stays out of this table: `jobs` and
`job_attempts` remain the operational lifecycle and provenance ledger, and the
job actions record the authenticated user's request rather than each attempt.

Slice 6B2A1 moves `transcript_delete` from an `AFTER DELETE` trigger to
`public.request_transcript_deletion`, which records it in the same transaction
that writes the tombstone. The action therefore means "the organization asked
for this transcript to be deleted and it is no longer visible", not "the private
object is gone": `transcripts.cleanup_state = 'complete'` is the only proof of
physical reconciliation. Reconciliation is background maintenance, not a user
action, so it appends nothing — one accepted deletion is exactly one event, and
a privileged hard delete of an already reconciled row records nothing rather
than re-attributing it to the original requester.

### `output_correction_uploads`

A correction reserves its version id and server-generated Storage path before the
private object is uploaded, so a Storage-success/DB-failure path always leaves
durable, recoverable evidence instead of an untracked object.

| Column | Purpose |
|---|---|
| `id` (PK) | Reservation id |
| `org_id` / `output_id` / `base_version_id` | Trusted context derived server-side |
| `reserved_version_id` | Version id the commit will use |
| `content_storage_path` | Server-generated private key for this version |
| `payload_hash` | SHA-256 of replacement Markdown plus change summary, bound to `request_id` for idempotency |
| `state` | `pending`, `committed`, `aborted`, `orphaned` |
| `cleanup_attempts` / `cleanup_claimed_by` / `cleanup_claimed_at` | Lease-bound retryable cleanup of an orphaned object |

Not readable or writable by `app_user` or `app_worker`; only the review functions
and the lease-bound `claim_orphaned_correction_upload(...)` /
`finalize_correction_cleanup(...)` path touch it. The claim is a lease, so two
workers cannot concurrently delete the same private object, and it returns only
the reservation id, Storage path, owning `org_id`, and attempt count the worker
needs. The org id rather than the correction author is deliberate: the worker
deletes the private object with the `app_storage_maintenance` Storage identity,
whose token carries the organization plus the exact claimed Storage path and is
authorized only for that one object in the `outputs` bucket, so cleanup does not
depend on the author still being an active member and cannot touch any other
output. The same maintenance identity
is used for the pre-existing generated-output tombstone cleanup; that lifecycle
is otherwise unchanged.

Two states are cleanup work. `orphaned` means the request knew the object existed
and could not delete it. `pending` means the request never reached commit or
abandon — a dead process, or an upload whose outcome is unknown because the write
can be accepted while the response is lost. A `pending` reservation becomes
eligible only once it is older than a bounded grace period (30 minutes), and
claiming it flips it to `orphaned` in the same locked transaction; since
`commit_output_correction` accepts only `pending`, a late commit for a reconciled
reservation fails instead of pointing a version at a deleted object. `committed`
reservations are never eligible. The worker runs this in the same maintenance
cycle as generated-output tombstone cleanup, so a restart rediscovers any
reservation whose lease has expired.

### `integration_connections` (future-facing)

User- or organization-scoped integration credentials (Salesforce, Gong, Google, etc.). This table is future-facing for the beta and should not be populated with organization-wide secrets in generic environment variables.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `type` | `salesforce`, `gong`, `google`, etc. |
| `scope` | `user` or `org` |
| `encrypted_credentials` | Encrypted token blob |
| `created_by` (FK to users) | Who authorized it |
| `created_at` / `updated_at` / `last_used_at` | Timestamps |

RLS on `org_id` plus scope checks. Rotation and refresh are handled by a credential service, not the agent runtime.

## Job payload and audit metadata

- The `jobs.payload` stores runtime configuration and stable input references (`input_refs`, `source_manifest`), not raw transcript bodies or secrets. `input_refs` are resolved only to same-organization records through DB-enforced/authorized lookup paths.
- Each job records `requester_id`, `org_id`, `account_id`, `opportunity_id`, `skill`, `skill_version`, `model`, `runtime_version`, aggregate `token_usage`/`cost`, `attempts`, timeout/cancellation information, redacted failure information, and the resulting `result_output_id`. Detailed per-attempt retry history lives in `job_attempts`.
- The job ledger plus the source/evidence manifest must be sufficient to reconstruct the run context without re-executing the model.

## Output immutability and review versioning

- The original generated `outputs` row and its Markdown/sidecar are immutable. It serves as the generation evidence (version 0).
- User corrections create new `output_versions` rows. Each version references its parent `output_id` and, for a correction chain, `previous_version_id`. The first correction may have `previous_version_id` null because it follows the original `outputs` row.
- An `approve` action may have `output_version_id` null (approving the original generated output) or reference a specific corrected `output_versions` row.
- Export targets the chain leaf only: `public.authorize_output_export` locks the `outputs` row, resolves the current version, and requires an `approve` row for exactly that version, so an approval of the generated version or of any superseded version never authorizes an export of a later one. Comments do not affect approval. Historical versions stay readable but are never export targets.
- The final reviewed state can be reconstructed from the original `outputs` row and the chain of `output_versions` and `reviews`.
- Correcting an output must never overwrite the original generation evidence.

## Important constraints and indexes

1. **Every tenant-scoped table has `org_id` and a non-nullable foreign-key relationship to `organizations`.**
2. **RLS policies restrict reads/writes to rows where the authenticated user has an active membership proving membership in that row's `org_id`.**
3. **Slugs are unique per parent (organization for accounts, account for opportunities).**
4. **Creator and assignee are metadata columns (not RLS predicates).**
5. **User-provided file/storage paths are never used directly; the API maps ids to org-scoped storage keys.**
6. **Job state transitions are atomic (compare-and-set on `status`).**
7. **Job payloads must not contain raw transcript bodies or secrets; they reference inputs by stable ids.**
8. **Audit events record who created, ran, reviewed, corrected, approved, and exported every artifact.**
9. **Generated outputs and sidecars are immutable; corrections are append-only/versioned.**
10. **Every tenant-scoped parent/child reference preserves organization ownership and is enforced by the database (for example, composite organization-aware foreign keys, triggers, or another DB-enforced mechanism selected in the implementation slice). Application-layer `org_id` checks are a required additional defense, not a substitute for the database enforcement.**
11. **`job_attempts` has a non-nullable `org_id` indexed FK to `organizations`; every `job_attempts.org_id` must equal the parent `jobs.org_id` and is enforced at the database layer.**

## RLS expectations

- An authenticated user may access a tenant-scoped row only when an active membership proves they belong to that row's organization. The API resolves the user's organization from their active membership and never trusts a user-supplied `org_id`.
- The exact RLS policy implementation (for example, authenticated JWT/provider claims, a transaction-scoped database context, a provider-native Auth/RLS integration, or another safe DB-enforced mechanism) is a Slice 2 implementation decision and must be designed and tested before it is committed.
- Application code re-checks `org_id` on every mutation to catch path-traversal or policy misconfigurations.
- Direct database access for analytics/admin is allowed only with an elevated role that bypasses RLS and is audited separately.