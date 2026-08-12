# SE Skills — Productionalization

This document is the entry point for the managed-hosted-beta program. It links to the product definition, architecture, data model, security model, and roadmap.

## Current state

The SE Skills Suite today is a local-first Claude Code skill suite plus an optional FastAPI + vanilla-JS web hub.

- **Skills:** `skills/<skill>/SKILL.md` files invoked by Claude Code through `SkillRuntimeService`. The default permission mode for interactive use is `acceptEdits`; reviewed shell skills listed in `SHELL_BYPASS_ALLOWLIST` and declaring `shell=True` can be granted `--permission-mode bypassPermissions`. This broad local permission behavior is not carried into the hosted runtime.
- **Local workspace:** customer data, transcripts, and outputs live in a configurable filesystem workspace (default `~/airbyte-work/01-customers/`).
- **Webapp:** FastAPI (`webapp/app.py`) serves a static SPA from `webapp/static/`, with routes in `webapp/routes/` and services in `webapp/services/`.
- **Job execution:** in-process `asyncio.create_subprocess_exec` running `claude -p` with a 10-minute timeout; job snapshots stored in `<workspace>/.state/jobs.json` for restart recovery.
- **Persistence:** customer outputs are Markdown files with optional `.md.json` sidecars; review feedback is `.md.feedback.jsonl`.
- **Validation:** `eval/` deterministic manifest-based framework plus `webapp/output_schema.py` Pydantic sidecar validation.
- **Source coverage and anti-hallucination guardrails** are enforced through prompt discipline and deterministic tests, not a separate runtime.

## Hosted-beta objective

Build a secure, Airbyte-managed beta for a single Airbyte organization. The beta must preserve the skill fidelity, source transparency, output validation, auditability, and useful local workflows that make the suite valuable, without simply lifting the local app onto a public server.

First complete hosted workflow:

> Sign in → select/create account → upload transcript → run post-call asynchronously → review validated output → correct/approve → export.

## Key gaps between local and hosted

| Gap | Local today | Hosted requirement |
|---|---|---|
| Identity/tenancy | none; app runs as the local OS user | organization-scoped auth and membership (Supabase Auth is the working hypothesis) |
| Data ownership | `.owner` files on local filesystem; account owned by member id | every tenant-scoped record has explicit `org_id` ownership plus `created_by`/`assigned_to` metadata |
| Storage | local filesystem under `~/airbyte-work` | private organization-scoped object storage; no dependence on persistent local server filesystem |
| Job durability | `jobs.json` snapshot in local workspace | durable Postgres job ledger (`public.jobs`/`public.job_attempts`) with `FOR UPDATE SKIP LOCKED` row claiming, expiring lease tokens, heartbeats, and a separate worker process; survives worker/API restart. Redis is not used. |
| Agent runtime | `claude -p` with the user's local tools, MCPs, repos, and network | isolated ephemeral workspaces with allowlisted files, tools, credentials, and network destinations (Slice 5) |
| Permissions | `SkillRuntimeService` selects a permission profile per skill; default is `acceptEdits`, and reviewed shell skills in `SHELL_BYPASS_ALLOWLIST` can receive `bypassPermissions` | no unrestricted shell, `bypassPermissions`, arbitrary Git, browser/computer automation, local-repo access, Live Transcribe, or arbitrary outbound network in the hosted runtime |
| Cross-org access | N/A | must be impossible at both DB/RLS and application layers; cross-organization access is a merge blocker |
| Audit/provenance | minimal (job snapshots, output mtimes) | durable audit log of who uploaded, ran, reviewed, and exported what |

## Architecture principles

1. **Organization-first, not user-first.** Accounts and opportunities belong to the organization. Users access them through memberships/roles. Assignment is metadata, not the tenancy boundary.
2. **FastAPI + SPA on one origin initially.** The existing vanilla-JS SPA and FastAPI backend continue to be served from the same origin; no beta requirement to move the SPA to Vercel.
3. **Supabase is the working hypothesis for Auth, Postgres, and private Storage.** It is a hypothesis, not a committed operational decision, until the beta infrastructure is provisioned and approved.
4. **No persistent local server filesystem state for production durability.** Customer transcripts, outputs, job state, and audit logs live in managed Postgres and object storage.
5. **Durable job ledger, queue, and workers.** Long-running skill execution is decoupled from the web/API process. Slice 4 implements a Postgres-backed queue with `FOR UPDATE SKIP LOCKED` row claiming, expiring lease tokens, worker heartbeats, `recover_expired_leases`, bounded retries with backoff, cancellation, and a separate polling worker process. Redis is intentionally not used for the beta workload.
6. **Hosted runtime is a multi-step agent, not a single LLM request.** It must preserve source/file discovery, full transcript reads, tool use, prior context, self-checks, source coverage, and artifact generation.
7. **Agent isolation by default.** Hosted jobs run in isolated ephemeral workspaces. Files, tools, credentials, and network destinations are allowlisted.
8. **Local-only capabilities remain local.** Unrestricted shell, `bypassPermissions`, arbitrary Git, browser/computer automation, local repository access, Live Transcribe, and arbitrary outbound network access remain local-only until separately approved.
9. **Defense in depth for organization isolation.** RLS policies plus application-layer authorization; private storage; merge-blocking invariants.

## Explicit hosted/local boundary

### Hosted (planned)

- Sign in / org membership
- Organization-scoped accounts, opportunities, transcripts, outputs, jobs
- Asynchronous post-call skill job enqueueing and durable status tracking via a Postgres queue and separate worker process (Slice 4 uses a deterministic `EchoExecutor`; actual skill execution is Slice 5)
- Review, correction, approval, and export of generated outputs
- Audit log and provenance

### Local (preserved)

- Full Claude Code skill suite with local `~/.claude/skills/` symlinks
- `claude -p` with local permission profiles: `acceptEdits` by default, `bypassPermissions` for reviewed shell skills, with local tools, MCPs, repos, and network
- Live Transcribe with local audio capture
- Connector feasibility using local `airbyte` / `airbyte-platform` repos
- `pov-gsheet` Chrome automation and Google Drive workflows
- Unrestricted shell, git, browser automation, and arbitrary network
- The local webapp can continue to run side-by-side with the hosted beta for users who need those capabilities

## Documents

- [Product definition](./docs/PRODUCT.md)
- [Architecture](./docs/ARCHITECTURE.md)
- [Data model](./docs/DATA_MODEL.md)
- [Security model](./docs/SECURITY.md)
- [Productionalization roadmap](./docs/ROADMAP.md)
