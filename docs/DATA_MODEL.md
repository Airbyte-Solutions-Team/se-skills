# SE Skills — Hosted Beta Data Model

## Overview

All durable tenant-scoped records are owned by an organization. Users access those records through memberships. `created_by` and `assigned_to` are metadata, not the tenancy boundary. Row-level security (RLS) policies in Postgres enforce organization isolation; the application layer re-checks the same invariant before returning data.

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

A user can belong to multiple organizations, but the beta UI selects one active organization per session. The API resolves `org_id` from the active membership and never from user-provided input alone.

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
| `skill` | Skill id, e.g. `post-call` |
| `status` | `pending`, `running`, `done`, `error`, `cancelled` |
| `payload` | JSON: prompt, model, runtime config, mounted input ids |
| `result_output_id` (FK to outputs, nullable) | Output produced |
| `attempts` | Retry counter |
| `max_attempts` | Max retries |
| `started_at` / `finished_at` | Timestamps |
| `worker_id` | Worker that claimed the job |
| `runtime_version` | Sandbox/runtime image version |
| `error` | Redacted error summary |
| `created_at` | Timestamp |

RLS on `org_id`. The queue claims jobs by updating `status` from `pending` to `running` with a check that the status was `pending`.

### `outputs`

Generated skill outputs. The Markdown content may be stored in object storage and referenced by `storage_path`, or stored inline if small; the sidecar validation metadata is always in Postgres.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `account_id` (FK) | Account |
| `opportunity_id` (FK, nullable) | Opportunity |
| `job_id` (FK) | Job that produced it |
| `skill` | Skill id |
| `title` | Generated title |
| `content_storage_path` | Private object-storage key for Markdown |
| `sidecar` | JSON: validation status, missing sections, reference freshness, etc. |
| `status` | `unvalidated`, `valid`, `invalid` |
| `review_status` | `none`, `commented`, `corrected`, `approved` |
| `generated_at` | Timestamp |
| `created_by` (FK to users) | Invoker |

RLS on `org_id`.

### `reviews`

Review/correction/approval actions on an output.

| Column | Purpose |
|---|---|
| `id` (PK) | UUID |
| `org_id` (FK, indexed) | Organization owner |
| `output_id` (FK) | Output |
| `user_id` (FK to users) | Reviewer |
| `action` | `approve`, `comment`, `correct` |
| `comment` | Text |
| `corrected_content` | Optional corrected Markdown |
| `created_at` | Timestamp |

RLS on `org_id`.

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
| `metadata` | JSON: IP, user agent, diff, etc. |
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

## Important constraints and indexes

1. **Every tenant-scoped table has `org_id` and a non-nullable foreign-key relationship to `organizations`.**
2. **RLS policies restrict reads/writes to rows where `org_id` equals the active membership's organization.**
3. **Slugs are unique per parent (organization for accounts, account for opportunities).**
4. **Creator and assignee are metadata columns (not RLS predicates).**
5. **User-provided file/storage paths are never used directly; the API maps ids to org-scoped storage keys.**
6. **Job state transitions are atomic (compare-and-set on `status`).**
7. **Audit events record who created, ran, reviewed, and exported every artifact.**

## RLS expectations

- `USING (org_id = current_setting('app.current_org_id')::uuid)` or equivalent policy. The API sets `app.current_org_id` from the user's active membership after validating the JWT.
- Application code still checks `org_id` on every mutation to catch path-traversal or policy misconfigurations.
- Direct database access for analytics/admin is allowed only with an elevated role that bypasses RLS and is audited separately.
