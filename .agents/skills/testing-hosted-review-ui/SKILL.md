---
name: testing-hosted-review-ui
description: Stand up the SE Skills hosted-mode webapp locally with synthetic Postgres, Storage, auth, and a seeded post-call output so a real browser can exercise hosted review, correction, approval, and export behavior. Use for HOSTED_MODE=1 acceptance testing when a plain `cd webapp && uv run app.py` would stop at sign-in or when browser tests need durable DB state plus the in-process MemoryStorageBackend.
---

# Browser-test the hosted review UI

Hosted mode needs Postgres, migrations, a Storage backend, and a signed JWT. The fastest reliable recipe is to boot the app **in-process** after seeding because `hosted.storage.MemoryStorageBackend` lives in server memory; a separately started server cannot see objects seeded from another process.

## Recipe

1. Sync the repository environment from the repo root with the same Python you will use to run the server:

   ```bash
   uv sync --python 3.11 --extra dev --extra hosted
   ```

   If PDF text extraction is useful for a one-off assertion, install `pypdf` into that test environment; do not make it a production dependency just for browser acceptance.

2. Start plain Postgres:

   ```bash
   docker run -d --name se-pg \
     -e POSTGRES_PASSWORD=test \
     -e POSTGRES_USER=test \
     -e POSTGRES_DB=test \
     -p 55432:5432 postgres:15
   ```

3. From `webapp/`, run the bundled `hosted_dev_server.py` with the repo venv Python. It:
   - creates the same minimal Supabase Storage/auth shim used by `eval/tests/conftest.py`;
   - applies all hosted migrations present in the checkout;
   - installs `MemoryStorageBackend`;
   - seeds a synthetic org, user, membership, account, transcript, successful `post-call` job, and validated output from `eval/fixtures/outputs/post-call-full.md`;
   - injects bounded hostile Markdown so XSS behavior can be checked;
   - mints a local HS256 JWT and writes sign-in/review URLs to `/tmp/hosted_dev_urls.txt`;
   - serves uvicorn on `127.0.0.1:8787`.

4. Sign in without DevTools by opening the printed URL shaped like:

   ```text
   http://127.0.0.1:8787/#access_token=<jwt>&token_type=bearer
   ```

   `initHosted()` in `webapp/static/app.js` parses the fragment and stores the token in localStorage. Then open the printed review URL under `#/hosted/accounts/<account_id>/outputs/<output_id>`.

5. Keep the seeded email in `BETA_ALLOWED_EMAILS`.

6. Cleanup after the acceptance pass:

   ```bash
   lsof -ti:8787 | xargs -r kill
   docker rm -f se-pg
   ```

## Review-page facts

- Version rail buttons switch versions; correction and approval apply only to the current version.
- Correction submit requires a non-empty change summary and Markdown that still satisfies the `post-call` contract, including required sections such as `## Source Coverage`.
- Slice 6B1 export controls are gated by `approved && isCurrent`. The page should explain whether the current version still needs approval or whether the reviewer is viewing a historical version.
- Expected downloads are deterministic, customer-name-free filenames such as `output-<output_id>-v<ordinal>.md` / `.pdf` when the export implementation under test uses that contract.
- Assert Markdown bytes against the seeded artifact. For PDF, assert the `%PDF-` header and material extracted text rather than pixel-perfect rendering.
- Export audit evidence is in `public.audit_events` with action `output_export`; `output_version_id` is `NULL` for generated V0 and the version UUID for a corrected version.

## Browser acceptance sequence

Exercise the user-visible lifecycle in order:

1. Open generated V0 and confirm the document renders with no executed hostile content.
2. Approve V0 and confirm export becomes available when Slice 6B1 is present.
3. Export Markdown and PDF; verify returned/downloaded bytes and audit rows.
4. Submit a valid correction and confirm the new version becomes current and export becomes unavailable until approval.
5. Approve the correction and export again; verify the audit event names the exact corrected version.
6. Submit an invalid correction and confirm it is rejected without adding a version.
7. Switch to a historical version and confirm correction/approval/export controls stay constrained to the current-version rules.
8. Repeat at a narrow viewport and confirm the header, version rail, document, activity, and actions remain usable.

## Gotchas

- Injecting `<img src=x onerror=...>` can legitimately produce `GET /x` 404 console noise because the image source is intentionally invalid. Treat the important assertions as: the `onerror` attribute is stripped, `javascript:` URLs are inert, and no injected script executes.
- CDP/browser-console evaluation may be unavailable in some environments. If so, verify console state in the browser rather than claiming automated console evidence.
- Chrome's minimum physical window width may be around 500 CSS px. To exercise narrower responsive breakpoints, shrink the window and use browser zoom; `Ctrl+=` is often more reliable than `Ctrl++`.
- Keep one browser window focused while driving clicks; extra windows can steal automation focus.
- The script uses only synthetic data and fixed local test credentials. Never replace the seed with real customer data or production credentials.

## Devin secrets needed

None. All seeded data and JWT secrets are synthetic and local to the test environment.
