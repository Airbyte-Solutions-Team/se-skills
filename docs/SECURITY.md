# SE Skills — Hosted Beta Security Model

## Trust boundaries

The hosted worker adds a dedicated host boundary: an untrusted per-attempt
runsc sandbox, a trusted worker-owned model proxy over a Unix socket, and a
separate FastAPI/API service. The host firewall is defense in depth; proxy
mediation and TLS hostname verification remain primary controls for changing
provider addresses. See `docs/HOST_CONTRACT.md` and
`docs/NETWORK_POLICY.md`.

```
[Browser / SPA] ──TLS──▶ [FastAPI API] ──TLS/mTLS──▶ [Supabase Auth + Postgres + Storage]
                              │
                              ▼
                       [Job ledger + queue]
                              │
                              ▼
                       [Worker sandbox pool]
                              │
                              ▼
                    [Allowlisted external APIs]
```

- The browser is untrusted.
- The SPA is static and served from the same origin as the API.
- The FastAPI process is trusted to enforce authentication and authorization.
- Supabase (or another operational provider chosen and approved for the beta) provides identity, relational data, and object storage.
- Workers are semi-trusted: they resolve authorized inputs and manage sandbox lifecycle. The sandbox runtime is untrusted and receives no Postgres credentials, Storage credentials, model keys, or browser-supplied paths.
- External APIs (model provider, Gong, Salesforce, etc.) are third-party boundaries.

## Organization isolation

- The organization is the tenancy boundary. Every tenant-scoped row has `org_id`.
- The managed beta supports one Airbyte organization. The conceptual schema may remain extensible to multiple organizations in the future, but the beta does not include an org switcher.
- Accounts and opportunities belong to the organization, not to the user.
- `created_by` and `assigned_to` are metadata, not access controls.
- Cross-organization access is a merge blocker. Any change that could allow one organization to read or modify another's data must be rejected.

## Database and RLS + application authorization

- Postgres row-level security (RLS) policies enforce that all queries for a tenant-scoped table only return rows whose `org_id` matches a membership the authenticated user has for that organization.
- The API establishes a trusted organization context only after validating the user's JWT and resolving an active membership. User-supplied `org_id` values are never authoritative.
- **Selected RLS context mechanism:** normal requests use a least-privilege `app_user` role (`NOBYPASSRLS`). At the start of each tenant-scoped transaction the API sets a signed tenant context token via `SET LOCAL app.context_token`. The token is `user_id:hmac(user_id, secret)`, where the secret is stored in an `app_private` schema that `app_user` cannot access. `SECURITY DEFINER` functions owned by `app_admin` verify the HMAC and active membership; RLS policies call these functions to compare each row's `org_id` to the token user's active organization. `SET LOCAL` makes the token transaction-scoped, so it cannot leak to other requests that reuse the same pool connection. `app_admin` is the migration role and owns the membership-resolution functions; the runtime never connects as `app_admin`.
- The application layer re-checks `org_id` on every mutation and before returning or forwarding data to the SPA.
- No API route may use a user-provided `org_id` or `user_id` as the authorization predicate.
- Every tenant-scoped parent/child reference (for example, `opportunities` to `accounts`, `transcripts` to `accounts`/`opportunities`, `jobs` to `accounts`/`opportunities`, `outputs` to `jobs`/`accounts`/`opportunities`, `output_versions` to `outputs`, `reviews` to `outputs`/`output_versions`, and `job_attempts` to `jobs`) must preserve organization ownership and is enforced by the database. Cross-organization relationship attempts must fail at the database boundary and in application authorization tests.
- Service-to-service calls (for example, worker to database) use a dedicated least-privilege `app_worker` Postgres role (`NOBYPASSRLS`). `app_worker` is not granted `SELECT`/`INSERT`/`UPDATE`/`DELETE` on tenant tables, membership data, transcript contents, or Storage objects; it can only `EXECUTE` the queue functions (`enqueue_job`, `claim_next_job`, `worker_heartbeat`, `complete_job`, `fail_job`, `cancel_job`, `recover_expired_leases`) needed to claim, heartbeat, complete, fail, cancel, and recover jobs. Queue functions are `SECURITY DEFINER` owned by `app_admin`, use a fixed empty `search_path`, fully qualified object references, revoked `PUBLIC` execute, and explicit transition validation. `app_worker` cannot assume `app_admin`, migration credentials, or Supabase service-role privileges.
- `app_user` cannot read or mutate another organization's jobs or attempts; `app_worker` cannot read unrelated tenant data. Direct table access by `app_worker` is denied by `NOBYPASSRLS` and missing table privileges.
- Errors stored or logged by the job API and worker are categorized and redacted. `payload`, `input_refs`, and `source_manifest` contain only stable IDs and non-sensitive lineage metadata; transcript bodies, bearer tokens, storage credentials, signed URLs, and raw filenames are never persisted or logged.

## Private storage

- Transcripts, outputs, and uploaded documents are stored in private organization-scoped buckets/paths.
- The `transcripts` and `outputs` buckets are private and fixed. The migrations force each private and abort if privacy cannot be verified. Storage RLS policies target a dedicated `app_storage` Postgres role and call `public.is_active_org_member_storage`, an `app_admin`-owned `SECURITY DEFINER` function that checks active membership. The browser-visible `authenticated` role is granted no privileges on `storage.objects`; it cannot read `public.memberships` directly. Access is allowed only when the object's first path segment (`storage.foldername(name)[1]`) matches an org the user is an active member of.
- Transcript object paths are generated by the FastAPI server, not the browser: `org_id/account_id/{opportunity_id/}transcripts/{transcript_id}`. Output object paths are generated by the worker: `org_id/account_id/transcript_id/output_id/output.md`. The original filename is stored only as metadata.
- The SPA never holds storage credentials. For each Storage REST call, FastAPI signs a short-lived JWT (`role: "app_storage"`, `sub: user_id`) with the server-side `SUPABASE_JWT_SECRET` and sends it in the `Authorization` header; the public `SUPABASE_ANON_KEY` is only used as the `apikey` project identifier. It does not use a Supabase service-role key, an unrestricted database credential, or the browser's `authenticated` JWT in normal transcript operations. The Supabase `authenticator` role is granted membership in `app_storage` so it can switch roles based on the JWT claim; `app_storage` is `NOINHERIT` so the claim is required.
- FastAPI streams upload bytes and download bytes through the API without writing customer content to local disk. Downloads are returned with `Content-Disposition: attachment` and `X-Content-Type-Options: nosniff`.
- Upload validation is independent of browser-provided filename or MIME type: accepted text formats are `.txt`, `.md`, `.vtt`, and `.srt`; content must be valid UTF-8, contain no NUL bytes, and be under `TRANSCRIPT_MAX_BYTES` (default 10 MiB). HTML, archives, executables, binary files, and invalid UTF-8 are rejected.
- Transcripts and uploaded content are never parsed, summarized, rendered, or executed by the upload handler. Transcript bodies, bearer tokens, storage credentials, signed URLs, and raw filenames are never logged.
- Public access is disabled by default. No object may be marked public without explicit review and a separate audit policy.
- The beta `app_storage` JWT relies on the legacy extractable HS256 `SUPABASE_JWT_SECRET`. This is an explicit beta deployment constraint; moving to a non-extractable signing model (for example, a Supabase Edge Function or Vault-issued token) must be a pre-production decision and migration.
- Transcript metadata in `public.transcripts` is protected by the same membership-bound RLS as accounts and opportunities and by composite foreign keys that enforce same-organization relationships with `accounts` and `opportunities`.
- Deleting a transcript first removes or marks the private object, then deletes the metadata; if metadata deletion fails after object deletion, the API returns a safe failure. Account/opportunity deletion is not implemented in this slice, and existing foreign keys prevent deleting an account or opportunity while it would orphan transcript metadata.
- Deleted objects move to an org-scoped soft-delete prefix or are purged according to retention policy.

## Secrets and OAuth handling

- **No organization-wide secrets in generic application environment variables.** Salesforce, Gong, Google, and similar integration credentials are stored per organization or per user in an encrypted credential store (Supabase Vault, AWS Secrets Manager, HashiCorp Vault, or equivalent).
- OAuth flows are per user or per organization with explicit consent and scoped scopes.
- Refresh tokens and API keys are encrypted at rest; the agent runtime receives only short-lived, job-scoped credentials.
- The Anthropic/model API key is managed by the platform, not exposed to the sandbox or the SPA. Slice 5A encodes this in `webapp/hosted/runtime_contract.py`: model calls originate from the worker or a worker-side model proxy, not from the sandbox directly.
- Secret values are redacted from logs, subprocess output, and error messages using the same patterns as `webapp/security.py` (authorization headers, tokens, keys, passwords).

## Agent isolation

- Each skill job runs in an isolated, ephemeral sandbox.
- The sandbox has no access to the host filesystem except explicitly mounted, read-only input files and a temporary writable workspace that is destroyed after the job.
- The sandbox receives no host environment variables except an allowlist (for example, `PATH`, `HOME` for a temporary home, model endpoint config).
- The runtime does not have unrestricted shell, `bypassPermissions`, arbitrary Git, browser/computer automation, local repository access, Live Transcribe, or arbitrary outbound network.
- Network egress is deny-by-default. In `runsc --network=none` mode the sandbox reaches only the per-job worker-side model proxy over a Unix domain socket; direct Anthropic, DNS, metadata, database, Storage, and arbitrary localhost access are blocked.
- Tool use is mediated and logged. The agent can only call tools that are registered in the runtime config for that job type.

## Runtime contract and output validation (Slices 5A and 5B2A)

- The `SkillRuntime` protocol in `webapp/hosted/runtime_contract.py` is provider-neutral. A durable job carries only stable identifiers; transcript bodies, bearer tokens, signed URLs, DB credentials, Storage credentials, and arbitrary browser-supplied paths are never passed through the job payload.
- `Allowlist` rejects generic tools such as `Bash`, `Shell`, `Exec`, `Git`, `Browser`, `Chrome`, `Http`, `McpDiscover`, and `BypassPermissions` at contract construction time. Each tool input is validated against a strict Pydantic model at dispatch. Only the Anthropic `tool_use` stop reason authorizes tool dispatch; `end_turn`/`stop_sequence` terminate a turn only when no tools are present, and every other stop reason (including unknown future values and `None`) fails closed as `model_error`.
- `InputManifest` carries an explicit `transcript_ref` and a closed set of `prior_context_refs`; the runtime resolves only those filenames in the read-only input workspace and never reads unlisted files.
- `RuntimeJob` requires `requested_model` at construction, carries an immutable execution deadline, and receives a `CancellationToken`; the harness races every in-flight model request against both cancellation and the deadline.
- `NetworkDestination` is a closed Pydantic model (`extra="forbid"`) that accepts only `http`/`https` hostnames.
- `RuntimeResult` separates validated output artifacts from categorized, redacted failures. `RedactedFailure` carries only a closed `FailureCategory` and a fixed generic message derived by the worker; arbitrary model-supplied failure text is rejected at the contract boundary.
- `report_failure` is a controlled model-report tool: its input schema exposes only a closed `model_reported_failure` category, and the runtime maps it to the host-owned `model_error` category. The model cannot report lifecycle categories such as `cancelled`, `timeout`, or `configuration_error`.
- `output_schema.parse_output` validates the generated Markdown and sidecar deterministically, without an LLM. For `post-call`, it requires a title, date, At a Glance decision fields, Key Takeaways, Action Items, Next Step, Source Coverage with concrete read/total counts, and rejects unfilled template placeholders. Conditional sections such as Sources & Destinations, Technical Notes, MEDDPICC Quick Pass, and Coaching Observations are not required when their triggering evidence is absent.
- Only the worker persists validated artifacts to Storage and the `outputs` row; the sandbox has no direct access to either.
- The sandbox model proxy (`webapp/hosted/model_proxy.py`) issues short-lived capability tokens bound to `job_id`, `attempt_number`, `lease_token`, `model`, `execution_deadline`, endpoint, and API version. The proxy overrides the model with the worker-authorized value, strips sandbox-supplied `x-api-key`/`anthropic-version`/`x-forwarded-for` headers, adds the real Anthropic key, and forwards only the Anthropic Messages API `v1/messages` route. Expired, replayed, malformed, cross-job, cross-attempt, wrong-model, and wrong-route requests are rejected.

## Tool and network allowlisting

- Network egress is deny-by-default. Each job type declares an allowlist of domains and protocols; in `runsc --network=none` mode this is realized as a single Unix domain socket to the per-job worker-side model proxy.
- `post-call` reaches only the model proxy endpoint; it does not need general web access, DNS, metadata services, database sockets, or storage endpoints.
- DNS resolution is restricted if possible; IP-based egress is blocked.
- No tool or MCP may be called unless it is in the approved list for the skill and the job's network allowlist permits it.
- Local-only tools (browser automation, `gh`/`git`, `sf` CLI, audio capture, arbitrary MCP servers) are not available in the hosted runtime.

## Logging and redaction

- All user, transcript, and output access is logged to the `audit_events` table.
- Worker and sandbox logs are shipped to the chosen observability backend.
- Secret patterns are redacted before persistence or display, following `webapp/security.py`.
- Transcripts and generated outputs are not logged verbatim except in encrypted debug streams accessible only to administrators.
- Failed jobs log a redacted error summary, not raw stack traces or model responses.

## Customer-data handling

- Customer transcripts, notes, and outputs are organization-scoped and encrypted at rest in storage.
- Data retention and deletion policies are defined per organization.
- When an account or organization is deleted, all associated objects and outputs are removed or moved to a hold prefix according to policy.
- Exports to internal.airbyte.ai or PDF use the same `md_render.py` + `nh3` sanitization used locally; no raw unsanitized HTML is emitted.
- The hosted runtime does not retain customer data between jobs; ephemeral workspace is destroyed.

## Explicit merge-blocking security invariants

The following invariants must be enforced by code review and CI. A PR that violates any of them is not merged:

1. **Every tenant-scoped table has a non-nullable `org_id` column and an RLS policy.**
2. **No API route may bypass organization authorization.**
3. **No service may use a user-provided `org_id` or `user_id` as the authorization predicate.**
4. **No storage object may be publicly accessible by default.**
5. **No worker may receive host filesystem or environment secrets outside an allowlist.**
6. **No hosted job may use unrestricted shell, `bypassPermissions`, arbitrary Git, browser/computer automation, local repository access, Live Transcribe, or arbitrary outbound network.**
7. **No organization-wide integration secrets may be stored in generic application environment variables.**
8. **Cross-organization access is a merge blocker.** Any test, route, or query that returns data from another organization must fail CI.
9. **Secrets must never be exported; exports are authorized and organization-scoped. Customer content must not leak into logs, errors, or unrelated organizations, and exports preserve only the information the artifact is intended to contain. Applicable retention and data-handling rules still apply.**
10. **Live Transcribe and other local-only capabilities remain local-only until separately approved.**
11. **Transcript and output contents are never logged.**
12. **The sandbox receives only allowlisted tools, network destinations, and read-only input mounts; it cannot access Postgres, Supabase Storage credentials, or the model key.**
