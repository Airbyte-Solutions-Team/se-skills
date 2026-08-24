# SE Skills — Hosted Beta Product Definition

## Target user

Airbyte Solutions Engineers who are members of the beta organization.

## Initial managed-beta scope

- One Airbyte organization.
- Accounts and opportunities belong to the organization; users access them through memberships/roles.
- Assignment (owner) is recorded but is not the tenancy boundary.
- First end-to-end workflow: sign in → select/create account → upload transcript → run `post-call` asynchronously → review validated output → correct/approve → export.
- `post-call` is the first hosted skill; other skills remain local-only until their runtime requirements are approved.

## Primary hosted workflow

1. **Sign in.** User authenticates via the beta identity provider (Supabase Auth is the working hypothesis).
2. **Select or create account.** User sees accounts scoped to the organization. Creating an account creates the organization-owned record.
3. **Upload transcript.** User uploads a transcript file for an account/opportunity. The file is stored in organization-scoped private storage; metadata is saved in Postgres.
4. **Run `post-call` asynchronously.** User invokes the hosted `post-call` skill from the SPA. The API enqueues a job. A worker picks up the job, runs the skill in an isolated sandbox, and writes the validated output to private storage.
5. **Review validated output.** The output reader shows the generated Markdown, validation status, source coverage, and reference-freshness warnings.
6. **Correct / approve.** The SE adds a comment, correction, or approval; feedback is persisted and linked to the output.
7. **Export.** Once the current version is approved, the SE downloads it as Markdown or PDF. Export always targets the approved current version, so a later correction withdraws export until that correction is approved.

## Product boundaries

- In scope for the beta: auth, organization membership, accounts, opportunities, transcript upload/storage, async post-call execution, output review/correction, PDF/MD/HTML export, and audit logging.
- The SPA and FastAPI remain on the same origin initially.
- Data lives in Supabase Postgres + private Storage (working hypothesis); no persistent local server filesystem state.
- The hosted runtime is isolated and allowlisted; it does not depend on local tools, MCPs, repos, or network.

## Non-goals

- Generic multi-tenant SaaS. The beta optimizes for a single Airbyte organization.
- Moving the SPA to Vercel for beta.
- Requiring Redis simply because jobs are asynchronous.
- Running all skills in the hosted runtime in the first beta. Only `post-call` is in scope; other skills remain local.
- Migrating real existing customer data from local workspaces as an early step.
- Live Transcribe in the hosted runtime.
- Browser/computer automation, unrestricted shell, arbitrary Git, or local repository access in the hosted runtime.
- Storing organization-wide integration secrets (Salesforce/Gong/Google) in generic application environment variables as the long-term credential model.
