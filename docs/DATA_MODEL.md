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

Uploaded customer artifacts. Stored as objects in private storage; Postgres keeps metadata.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `account_id` (FK) | Account |
| `opportunity_id` (FK, nullable) | Opportunity |
| `type` | `transcript`, `note`, `document` |
| `filename` | Original filename |
| `storage_path` | Private object-storage key (org-scoped) |
| `size_bytes` | File size |
| `mime_type` | MIME type |
| `uploaded_by` (FK to users) | Uploader |
| `created_at` | Timestamp |

RLS on `org_id`. The API streams or signs URLs for downloads; the SPA does not hold storage credentials.

### `jobs`

Durable job ledger for asynchronous skill execution.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `account_id` (FK) | Account |
| `opportunity_id` (FK, nullable) | Opportunity |
| `requester_id` (FK to users) | User who invoked the job |
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
| `cancelled_at` / `cancelled_by` | Cancellation timestamp and actor |
| `worker_id` | Worker that claimed the attempt |
| `error` | Redacted failure summary (no raw stack traces or secrets) |
| `created_at` | Timestamp |

RLS on `org_id`. The queue claims a job by atomically transitioning `status` from `queued` to `running` with a compare-and-set. The intended invariant is an atomic/exclusive claim per attempt, at-least-once recovery, bounded retries, and no silent job loss on worker/API crashes.

Primary job statuses: `queued`, `running`, `success`, `failure`, `cancelled`, `timeout`. `retry-wait` and dead-letter are queue/scheduling concepts, not primary job statuses.

### `job_attempts`

Append-only attempt history for a job. `jobs` remains the aggregate ledger; `job_attempts` records each execution attempt.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK to organizations, indexed) | Organization owner; must match the parent `jobs.org_id` |
| `job_id` (FK) | Parent job |
| `attempt_number` | Integer, starting at 1 |
| `worker_id` | Worker/runtime that ran this attempt |
| `runtime_version` | Sandbox/runtime image version for this attempt |
| `lease_token` | Heartbeat/lease token for claim liveness (nullable) |
| `started_at` / `finished_at` | Timestamps |
| `heartbeat_at` | Last worker heartbeat/lease renewal |
| `outcome` | `success`, `failure`, `timeout`, `cancelled` |
| `error_category` | Categorized failure reason (for example, `timeout`, `model_rate_limit`, `storage_write`, `sandbox`) |
| `error` | Redacted failure summary |
| `token_usage` | JSON: input/output token counts for this attempt |
| `cost` | Estimated cost for this attempt |
| `created_at` | Timestamp |

RLS on `org_id` with an invariant that `job_attempts.org_id` equals the parent `jobs.org_id`. Each attempt is immutable once recorded. `jobs.attempts` is a derived counter, not the source of truth for retry history.

### `outputs`

A generated skill output. The original Markdown and sidecar are immutable once written; corrections are recorded as new `output_versions` and approvals reference a specific version.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `account_id` (FK) | Account |
| `opportunity_id` (FK, nullable) | Opportunity |
| `job_id` (FK) | Job that produced it |
| `skill` | Skill id |
| `skill_version` | Skill/prompt version or content hash used |
| `model` | Model used |
| `title` | Generated title |
| `content_storage_path` | Private object-storage key for the immutable generated Markdown |
| `sidecar` | JSON: validation status, missing sections, source coverage, reference freshness, etc. |
| `status` | `unvalidated`, `valid`, `invalid` |
| `generated_at` | Timestamp |
| `created_by` (FK to users) | Invoker |

RLS on `org_id`. The generated content and sidecar must never be overwritten; any correction creates a new `output_versions` row.

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
| `created_at` | Timestamp |

RLS on `org_id`.

### `output_versions`

Append-only versions of an output. A correction creates a new version with a corrected Markdown/sidecar; the original `outputs` row remains unchanged and serves as the generation evidence.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `output_id` (FK) | Parent output |
| `previous_version_id` (FK, nullable) | Previous version in the chain |
| `corrected_by` (FK to users) | User who made the correction |
| `content_storage_path` | Private object-storage key for this version's Markdown |
| `sidecar` | JSON: validation status, source coverage, etc. for this version |
| `change_summary` | Brief description of what changed |
| `created_at` | Timestamp |

RLS on `org_id`. The original `outputs.content_storage_path` and `outputs.sidecar` are immutable and represent the generated evidence (version 0). The first correction references the original `output_id` with `previous_version_id` null; subsequent corrections reference the prior `output_versions` row. An `approve` action may have `output_version_id` null (approving the original) or reference a specific corrected version. The final reviewed state is reconstructable from the original output and the chain of `output_versions` and `reviews`.

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
| `metadata` | JSON: IP, user agent, diff, etc.; must not contain secrets or raw customer content |
| `created_at` | Timestamp |

RLS on `org_id`. Audit events may be append-only or protected by admin policy.

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
