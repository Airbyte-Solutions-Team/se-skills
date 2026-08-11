# SE Skills — Hosted Beta Security Model

## Trust boundaries

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
- Workers are semi-trusted: they run sandboxed code and receive only allowlisted secrets.
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
- The exact mechanism for propagating that trusted organization context into RLS (for example, authenticated JWT/provider claims, a transaction-scoped database context, a provider-native Auth/RLS integration, or another safe DB-enforced mechanism) is a Slice 2 design and testing decision. Application-level `org_id` checks are a separate defense-in-depth layer, not the mechanism that propagates identity into RLS.
- The application layer re-checks `org_id` on every mutation and before returning or forwarding data to the SPA.
- No API route may use a user-provided `org_id` or `user_id` as the authorization predicate.
- Every tenant-scoped parent/child reference (for example, `opportunities` to `accounts`, `transcripts` to `accounts`/`opportunities`, `jobs` to `accounts`/`opportunities`, `outputs` to `jobs`/`accounts`/`opportunities`, `output_versions` to `outputs`, `reviews` to `outputs`/`output_versions`, and `job_attempts` to `jobs`) must preserve organization ownership and is enforced by the database. Cross-organization relationship attempts must fail at the database boundary and in application authorization tests.
- Service-to-service calls (for example, worker to database) use a narrow role that can access only job/output/transcript rows and cannot read auth users or memberships directly.

## Private storage

- Transcripts, outputs, and uploaded documents are stored in private organization-scoped buckets/paths.
- Objects are keyed by `org_id/account_id/opportunity_id/<type>/<uuid>-filename`.
- The SPA never holds long-lived storage credentials. The API either streams file contents or returns short-lived signed URLs.
- Public access is disabled by default. No object may be marked public without explicit review and a separate audit policy.
- Deleted objects move to an org-scoped soft-delete prefix or are purged according to retention policy.

## Secrets and OAuth handling

- **No organization-wide secrets in generic application environment variables.** Salesforce, Gong, Google, and similar integration credentials are stored per organization or per user in an encrypted credential store (Supabase Vault, AWS Secrets Manager, HashiCorp Vault, or equivalent).
- OAuth flows are per user or per organization with explicit consent and scoped scopes.
- Refresh tokens and API keys are encrypted at rest; the agent runtime receives only short-lived, job-scoped credentials.
- The Anthropic/model API key is managed by the platform, not exposed to the sandbox or the SPA.
- Secret values are redacted from logs, subprocess output, and error messages using the same patterns as `webapp/security.py` (authorization headers, tokens, keys, passwords).

## Agent isolation

- Each skill job runs in an isolated, ephemeral sandbox.
- The sandbox has no access to the host filesystem except explicitly mounted, read-only input files and a temporary writable workspace that is destroyed after the job.
- The sandbox receives no host environment variables except an allowlist (for example, `PATH`, `HOME` for a temporary home, model endpoint config).
- The runtime does not have unrestricted shell, `bypassPermissions`, arbitrary Git, browser/computer automation, local repository access, Live Transcribe, or arbitrary outbound network.
- Tool use is mediated and logged. The agent can only call tools that are registered in the runtime config for that job type.

## Tool and network allowlisting

- Network egress is deny-by-default. Each job type declares an allowlist of domains and protocols.
- `post-call` may need the model provider API and the transcript storage endpoint; it does not need general web access.
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
