# SE Skills Suite — Implementation Plan

> Source of truth for planned improvements to the SE Skills Suite.
> Created from the repository assessment dated 2026-07-14.
> Status: Phase 1 merged (#1, 2026-07-14). Phase 1B implemented as real-skill evaluation harness. Phase 2 security quick wins merged (SEC-005 completed). Phase 3 skill-behavior guardrails merged. Phase 4 orchestration, feedback loop, and structured outputs completed (ORCH-001–003, UX-001–009, STRUCT-001–003). SKILL-001 shared prompt boilerplate centralization completed. POV-001 `pov-gsheet` port implemented; external MCP integration paths are wired but not runtime-verified in this environment, so POV-001 is marked Partial. POV-002 live MCP/Google runtime acceptance is deferred to user local testing (IMPLEMENTATION-PLAN.md and SESSION-LOG.md reflect the deferral). ARCH-001 job-lifecycle extraction completed in Airbyte-Solutions-Team/se-skills#30; output/feedback extraction completed as Slice 2 in Airbyte-Solutions-Team/se-skills#31; member/account/opportunity extraction completed as Slice 3 in Airbyte-Solutions-Team/se-skills#32; overview aggregation extraction completed as Slice 4 in Airbyte-Solutions-Team/se-skills#33. ARCH-001 remains Partial.

---

## 1. Purpose and operating principles

This file captures the full improvement roadmap for the `se-skills` repository. It is intended to be a durable, incrementally updated source of truth so future sessions can pick up work without re-deriving context.

### Principles

- Planning does not imply approval to implement.
- Each implementation phase requires an explicitly scoped task.
- Prefer incremental changes over broad rewrites.
- Preserve current working behavior unless a change is intentional and tested.
- Security and reliability issues take priority over code cleanliness.
- Model behavior must be evaluated with realistic SE scenarios.
- Do not use real customer information in committed tests.
- Distinguish deterministic validation from model-behavior evaluation.
- Update this document whenever work is completed, deferred, superseded, or newly discovered.

---

## 2. Current-state summary

The SE Skills Suite is a **Claude Code skill suite** for Airbyte Solutions Engineers, plus an optional **local FastAPI + vanilla-JS web hub**.

- **Skill-based architecture:** Each skill is a `SKILL.md` file under `skills/`. `install.sh` symlinks them into `~/.claude/skills/` (`install.sh:12-45`).
- **Shared SE playbook:** `skills/_se-playbook.md` is the canonical reference for sales frameworks (MEDDPICC, SPIN, Sandler, Challenger, Voss), skill sequencing, output format, workspace-path resolution, source-coverage rules, and product/connector reference data (`skills/_se-playbook.md:1-3`, `skills/_se-playbook.md:191-262`, `skills/_se-playbook.md:427-500`, `skills/_se-playbook.md:565-602`, `skills/_se-playbook.md:767-803`).
- **Local customer workspace:** Customer data, transcripts, notes, and outputs live outside the repo in a workspace configurable via `.se-config.yaml` (`config/se-config.example.yaml:13-75`; `skills/_se-playbook.md:268-315`).
- **FastAPI web application:** `webapp/app.py` serves a vanilla-JS UI at `http://127.0.0.1:8787`. It discovers skills, lists accounts, renders outputs, invokes skills as background jobs, enriches from Salesforce, runs PDF/internal-HTML export, push-to-repo coverage handoffs, and live Zoom transcription (`webapp/app.py:1-9`, `webapp/app.py:52-146`, `webapp/app.py:363-518`, `webapp/app.py:988-1177`, `webapp/app.py:1688-2205`).
- **Claude Code invocation:** The webapp spawns `claude -p "<prompt>" --permission-mode acceptEdits` as an async subprocess (`webapp/app.py:1586-1598`).
- **Output storage and rendering:** Skills auto-save dated markdown under `{customers_dir}/<Customer>/outputs/<skill>/` (`skills/_se-playbook.md:362-374`; `README.md:193-203`). The web reader restructures markdown into decision cards, risk strips, and collapsible audit sections (`webapp/static/app.js:1737-1880`; `webapp/static/style.css:429-568`).
- **External grounding:** Skills can read Gong transcripts, Salesforce CRM, Notion, public connector registries, and optional local Airbyte repos (`airbyte-platform`, `airbyte-enterprise`) for entitlement and connector availability reasoning (`skills/_se-playbook.md:671-803`; `README.md:143-189`).
- **Anti-hallucination and source coverage:** Qualification skills refuse to run without transcripts, and every synthesizing skill is required to report what it read (`skills/biz-qual/SKILL.md:17-26`; `skills/tech-qual/SKILL.md:17-21`; `skills/poc-plan/SKILL.md:18-41`; `skills/_se-playbook.md:565-602`).

### Strongest existing protections

- Hard transcript gates for `biz-qual`, `tech-qual`, `poc-plan`, `roi-business-case`, `deployment-model-qual`, and `full-qual`.
- Explicit source-coverage and fail-loud graceful-degradation rules.
- Decision-first output format with `At a Glance` cards and `Source Coverage` last.
- Product-truth grounding in connector registries (`DS1`), `airbyte-platform` entitlements (`DS2`), and `airbyte-enterprise` connector stubs (`DS3`).
- Doc-sync discipline via `MAINTAINING.md`, `scripts/check-sync.sh`, and `webapp/CLAUDE.md`.

---

## 3. Problem statement

The current implementation is thoughtful but carries several high-risk gaps:

- **No automated skill-behavior tests.** There is no `tests/` directory, no prompt-regression suite, no golden cases, and no CI.
- **No runtime output validation.** Generated markdown is only parsed by regex/heading heuristics in the UI and PDF renderer.
- **Business behavior is enforced primarily through Markdown prompts.** The model's compliance is the implementation; there is no deterministic guardrail.
- **Broad Claude Code permissions.** Skills are invoked with `--permission-mode acceptEdits`, allowing file edits, shell commands, and MCP use without per-action approval.
- **Security risks in external input and export paths.** Salesforce queries are string-constructed, free-form and live-transcribe input can be injected into prompts, and exported HTML/PDF does not sanitize raw HTML.
- **Prompt and output-format duplication.** The same boilerplate (At a Glance, Source Coverage, scorecards, decision tables) is repeated across many `SKILL.md` files.
- **Fragile Markdown parsing.** The webapp and PDF renderers implement their own markdown parsers; raw HTML in skill output can execute in exported files.
- **Lack of a feedback and learning loop.** There is no structured way to capture SE corrections and feed them back into memory or test fixtures.
- **Suggested rather than deterministically enforced workflow prerequisites.** `next-move` recommends, but the webapp free-form invoke bar can run any skill in any order.
- **Maintainability concerns in the web application.** `webapp/app.py` is a 2205-line monolith mixing routes, filesystem, SFDC, git, PDF, internal HTML, and live audio.

---

## 4. Implementation backlog

> All items start in `Proposed` status.
> Classification: `confirmed` = observed in the repo; `hypothesis` = likely risk requiring validation.

| ID | Workstream | Recommendation | Problem being addressed | Evidence / relevant files | Classification | Severity | Expected impact | Effort | Dependencies | Risk of implementing | Acceptance criteria | Validation method | Status | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| EVAL-001 | Evaluation and reliability | Add a deterministic skill-evaluation framework with synthetic transcripts, configs, and expected outputs. | No automated tests; regressions in prompt/output behavior go undetected. | No `tests/` or `pyproject.toml` exists; skills are Markdown prompts. | confirmed | Critical | High | Large | None | Low if isolated to `eval/` temp outputs | `eval/` runs; each scenario produces pass/fail; no real customer data used | `pytest eval/` against fixtures | Completed | Implemented in Phase 1: `eval/` directory, manifest schema, runner, and 6 synthetic scenarios. |
| EVAL-002 | Evaluation and reliability | Add prompt-regression tests and golden-case fixtures for each skill. | Prompt edits can silently change output structure or verdicts. | Skills duplicate output-format instructions in `SKILL.md` files. | confirmed | High | High | Medium | EVAL-001 | Low | Golden outputs committed; diffs fail CI on unexpected change | `pytest` diff against golden Markdown | Partially completed | `eval/golden/` stores canonical Markdown per skill/scenario; `eval/tests/test_skill_regression.py` diffs `MockExecutor` outputs against committed fixtures; `--update-golden` rewrites fixtures; `POST /api/output/golden` remains as a developer-only endpoint. Because `MockExecutor` builds outputs from hardcoded Python templates and does not run `SKILL.md`/Claude, this guards mock-output structure, not real skill prompt behavior; the correction-to-real-regression learning loop is still incomplete. |
| EVAL-003 | Evaluation and reliability | Add an LLM-as-judge harness for model-dependent behavior (verdict quality, refusal correctness). | Some correctness cannot be checked deterministically. | Model determines whether to refuse or how to score MEDDPICC. | confirmed | High | Medium | Medium | EVAL-001, optional `ANTHROPIC_API_KEY` | Medium (cost, judge reliability) | Judge prompts produce consistent pass/fail with rubrics | Run judge against sampled outputs | Proposed | Marked as model-dependent, not run in fast CI |
| EVAL-004 | Evaluation and reliability | Track skill/prompt versions and detect drift between `SKILL.md`, `_se-playbook.md`, and rendered output. | It is hard to know which change broke a behavior. | No version metadata on prompts. | confirmed | Medium | Medium | Small | EVAL-001 | Low | Each `SKILL.md` frontmatter includes a `version`/`evaluated_at` field; CI checks stale skills | Pre-commit / CI check | Proposed | Add `version` to frontmatter, not rewrite skills |
| EVAL-005 | Evaluation and reliability | Integrate the evaluation suite into CI (lint + fast tests on PR). | Without CI, tests rot. | No `.github/workflows` or CI config exists. | confirmed | Medium | Medium | Small | EVAL-001, ARCH-002 | Low | PRs run `pytest eval/` and fail on regression | GitHub Actions workflow | Completed | Fast suite is deterministic and model-independent; optional model-dependent workflow is `workflow_dispatch` only. |
| EVAL-006 | Evaluation and reliability | Add business-invariant checks for capacity sizing, data workers, sync frequency, concurrency, and throughput recommendations. | Optimization skills might alter customer constraints to improve recommendations. | `roi-business-case/SKILL.md` discusses capacity-based pricing and loaded cost; `poc-plan/SKILL.md` ties success criteria to Metrics. | confirmed | Critical | High | Medium | EVAL-001 | Low | Tests assert a recommendation never lowers sync frequency without explicit permission | Deterministic assertions in `eval/` | Completed | Covered by `phase1-hourly-sync-constraint` and `phase1-unverified-entitlement` scenarios. |
| SEC-001 | Agent permissions and execution safety | Replace or gate `--permission-mode acceptEdits` with a safer default and explicit approval for write/shell/git actions. | Spawned Claude can edit files and run arbitrary commands without approval. | `webapp/app.py:2058-2064` | confirmed | Critical | High | Medium | None | Medium (may break skills that legitimately write output) | UI shows approval dialog before any skill that writes files or runs shell; skill list classified by write needs | `pytest` + manual webapp test | Completed | Added `PermissionProfile`/`SKILL_PERMISSIONS`, `_permission_profile`, `GET /api/permissions`, and `approve_permissions` gate on `POST /api/invoke`; invoke modal displays required permissions (write/shell/git) and asks for explicit confirmation. All skills default to write; `connector-feasibility` flagged shell+git because it runs git commands; freeform instructions get the broadest profile. |
| SEC-002 | Agent permissions and execution safety | Enforce external-input boundaries for free-form prompts and live-transcribe inputs (length, type, path safety, output encoding). | User or transcript text reaches subprocess prompts and filesystem paths without clear bounds. | `webapp/app.py:992-998`, `webapp/app.py:1238-1240`, `webapp/app.py:1288-1292`, `webapp/app.py:1515-1524`, `webapp/app.py:1902-1941` | confirmed | High | High | Medium | EVAL-001 | Low | Pydantic `max_length` rejects oversize input; `_safe` and `_html_escape` block path and markup injection | `pytest` with boundary fixtures | Completed | Natural-language prompt-injection defense remains future work |
| SEC-003 | Input, data, and export security | Fix Salesforce query construction by escaping single quotes and SOQL LIKE wildcards via validated helpers. | `_sf_quote` only stripped single quotes; `%` and `_` changed query semantics. | `webapp/soql.py`, `webapp/app.py:557-559`, `webapp/app.py:464-465`, `webapp/app.py:588-593` | confirmed | High | High | Small | None | Low | SOQL helpers produce literal-safe prefixes; wildcard injection fixtures pass | `pytest` on `soql.py` + app SFDC helpers | Completed | Normal account names unaffected |
| SEC-004 | Input, data, and export security | Sanitize raw HTML in exported PDF, internal-HTML, and the webapp Markdown reader. | `python-markdown` does not escape raw HTML, allowing JS and dangerous URL schemes in exported files. | `webapp/pdf_render.py:150-164`, `webapp/pdf_render.py:248`, `webapp/static/app.js:15-22`, `webapp/static/app.js:356-357` | confirmed | High | High | Small | None | Low | Poisoned fixtures produce no `<script>`, inline handlers, or `javascript:` links; normal Markdown still renders | `pytest` on `pdf_render.py` and `app.js` `safeHref` logic | Completed | Uses `nh3` with an allowlist matched to `python-markdown` and skill output |
| SEC-008 | Input, data, and export security | Redact secrets and sensitive values in subprocess stdout/stderr and UI-facing exception messages. | Subprocess failures and LLM API exceptions can contain API keys, tokens, or credentials. | `webapp/security.py`, `webapp/app.py:433-453`, `webapp/app.py:1185-1234`, `webapp/app.py:1591-1612`, `webapp/app.py:1883-1889` | confirmed | High | Medium | Small | None | Low | Known secret patterns (authorization headers, Anthropic/GitHub tokens, URL credentials, env-style assignments) are replaced with `***` before persistence or UI display | `pytest` on `security.py` and `app.py` redaction paths | Completed | Does not replace proper secret storage; keyring migration remains SEC-005 |
| SEC-005 | Input, data, and export security | Move API keys and secrets out of plain `.env` files into a keyring or encrypted secrets store. | `~/.mcp/*.env` is plain text. | `webapp/app.py:2133-2145` | confirmed | Medium | Medium | Small | None | Low | App reads key from OS keyring or `keyring` module with fallback; no plaintext secrets in repo | `pytest` + manual validation | Completed | `webapp/app.py` now uses `keyring.get_password("se-skills", "ANTHROPIC_API_KEY")` for the Anthropic quick-ask path, with `ANTHROPIC_API_KEY` env var as fallback. Plaintext `~/.mcp/*.env` files are no longer read. Tests cover env/keyring precedence. `webapp/README.md` updated with keyring-first setup. |
| SEC-006 | Input, data, and export security | Add authentication and access controls if the webapp is ever hosted, and document data-retention expectations. | Currently no auth; local-only design is assumed. | `webapp/app.py:2203-2205` | hypothesis | Medium | Large | Large | None | High (large architectural change) | Design doc and proof-of-concept for auth + encrypted-at-rest customer data | Review and threat model | Proposed | Deferred until hosted decision is made |
| SEC-007 | Agent permissions and execution safety | Document current `--permission-mode acceptEdits` usage and design per-skill permission profiles before gating write/shell/git actions. | `claude -p` uses `acceptEdits` for all skill invocations, giving broad file/shell/MCP access without per-action approval. | `webapp/app.py:124-173`, `webapp/README.md`, `webapp/SESSION-LOG.md` | confirmed | High | Medium | Medium | None | Medium | README/SESSION-LOG explain which actions require `acceptEdits`, what would break with stricter modes, and future options | Review + design doc + `pytest` | Completed | Permission profiles implemented under SEC-001; README and SESSION-LOG updated to explain the pre-launch approval gate and why `--permission-mode acceptEdits` remains the underlying mode. |
| SKILL-001 | Skill and prompt architecture | Centralize shared output-format and guardrail fragments in `_se-playbook.md` and reference them from skills. | Output-format boilerplate is duplicated across skills, wasting context and drifting. | Multiple `SKILL.md` files repeat `At a Glance`, `Jump-to`, `Source Coverage`, scorecards. | confirmed | Medium | Medium | Medium | EVAL-001 | Medium (could alter prompt behavior) | `skills/_se-playbook.md` has `Shared Skill Boilerplate` with `Output format reference`, `Pre-flight source check`, and `After Generating (saving skills)`; every saving skill references these fragments; skill-specific workflow, source coverage, and guardrails remain in each `SKILL.md`; `eval/tests/test_skill_playbook.py` verifies references resolve and skill-specific guardrails are preserved | Diff + `pytest eval/tests/test_skill_playbook.py` + `scripts/check-sync.sh` | Completed | Use included fragments, not deletion of skill specificity |
| SKILL-002 | Skill and prompt architecture | Add reference-data freshness checks (registry, entitlement repos, objection reference). | Product guidance can go stale without warning. | `skills/_reference/airbyte-objection-reference.md` is only checked for existence; `skills/_se-playbook.md:767-803` describes TTLs. | confirmed | Medium | Medium | Small | STRUCT-003 | Low | Skills output a "reference data freshness" line; UI warns if data >7 days | Deterministic assertions + UI warning | Completed | `webapp/reference_freshness.py` computes ages from file mtimes; `OutputMetadata` carries `reference_freshness`; `webapp/app.py` and `webapp/static/app.js` surface stale/missing warnings. Prompt-version metadata is left to EVAL-004. |
| SKILL-003 | Skill and prompt architecture | Verify `objection-handler` content against current product reality and add a product-owner refresh cadence. | `objection-handler` was skipped in the external-repos integration and may lag. | `SESSION-LOG.md` P-EXT notes; `scripts/check-sync.sh:90-112` only checks file presence. | hypothesis | Medium | Medium | Small | SKILL-002 | Low | `objection-handler/SKILL.md` references the same deployment model as `deployment-model-qual`; tests pass | EVAL-001 scenario | Completed | Updated `objection-handler/SKILL.md` to mirror `deployment-model-qual` Cloud Pro / Enterprise Flex / park-no-fit taxonomy and to require a reference-freshness check and warning. Refresh cadence is the 7-day freshness gate. |
| SKILL-004 | Skill and prompt architecture | Strengthen customer-constraint preservation prompts and tests for capacity sizing, sync frequency, concurrency, and throughput. | An optimization skill might recommend reducing sync frequency without explicit permission. | `roi-business-case/SKILL.md:44-85`; `poc-plan/SKILL.md:260-267` | confirmed | Critical | High | Medium | EVAL-006, EVAL-001 | Low | Prompts explicitly ask permission before changing schedule; tests fail if output does so | `pytest` invariants + prompt-regression test | Completed | D5 discipline added to `_se-playbook.md`; `roi-business-case` and `poc-plan` reference it; guardrail covers capacity sizing, sync frequency, concurrency, throughput, and POC schedule.
| POV-001 | Skill and product outcome | Port and integrate `pov-gsheet` to create pre-filled POV Google Sheets from repository-native context, without importing `se-assistant`, DuckDB, or personal paths. | `pov-gsheet` hard-depends on `se-assistant` and `~/Documents/Claude/sales-data/db/db_sales_data.duckdb`, which is not bundled with this repository. | `skills/pov-gsheet/SKILL.md`; `webapp/pov_gsheet_bridge.py`; `webapp/pov_gsheet_context.py`; attached source skill and canonical `se-assistant` reference | confirmed | High | High | Medium | STRUCT-001, STRUCT-003, SKILL-001 | Medium (bridge + unverified Google automation) | `pov-gsheet` uses deterministic `PovContext` loader from workspace outputs, transcripts, optional Salesforce `sf` CLI, and optional JSON evidence files produced by `webapp/pov_gsheet_bridge.py` from configured MCP integrations (Salesforce, Gong, meeting notes, Gmail, Slack); the skill calls the MCP tools and the bridge so the SE does not prepare evidence files manually; fills a 7-sheet template via Chrome/computer-use clipboard paste or `webapp/scripts/pov-gsheet-runner.mjs`; writes a local receipt; no `se-assistant` references remain; source-coverage and status honestly record which sources were searched, unavailable, or skipped; deterministic tests pass; Google end-to-end remains unverified with setup instructions | `pytest eval/tests/test_pov_gsheet.py`, `pytest eval/tests/test_pov_gsheet_bridge.py`, `node --check webapp/scripts/pov-gsheet-runner.mjs`, dry-run of context builder and runner | Partial | Deterministic context loader, bridge, receipt, and dry-run plan are verified; Salesforce/Gong MCP integration paths are wired through the bridge but not runtime-verified because no MCP servers are configured; Google Sheets/Drive end-to-end cannot be verified because Chrome is not signed into Google. |
| POV-002 | Skill and product outcome | Runtime-verify and activate external MCP integrations for `pov-gsheet` (Salesforce, Gong, meeting notes, Gmail, Slack) and verify the Google Sheets/Drive end-to-end workflow. | POV-001 wires the source adapters and a normalization bridge, but the integrations were not exercised against live MCP servers and the Google run is blocked by an unsigned Chrome profile. | `skills/pov-gsheet/SKILL.md` Step 0a/0c; `webapp/pov_gsheet_bridge.py`; `webapp/pov_gsheet_context.py` external evidence flags; `webapp/scripts/pov-gsheet-runner.mjs`; `.claude.json` empty `mcpServers` | confirmed | High | High | Medium | POV-001 | Medium | One safe runtime retrieval per configured MCP, scoped to a test account or opportunity; provenance recorded (timestamp, source ID, direct vs. internal); conflict handling and deduplication tested with real evidence; receipt accurately reports searched/unavailable/skipped; no false "source searched" claims; Google Sheet created from a disposable copy, all seven tabs populated, prospect folder created/used, final title and Drive location verified, direct link returned, local receipt saved | Configure MCP servers + signed-in Chrome + template/drive permissions; run `pytest eval/tests/test_pov_gsheet_bridge.py` with captured live evidence; run `node webapp/scripts/pov-gsheet-runner.mjs --run` against a safe test prospect | Deferred (user acceptance) | POV-002 has been completed as far as can be done in this environment: the bridge, skill prompts, and source-coverage logic are implemented and deterministic tests pass. Live MCP and Google runtime acceptance is intentionally deferred to the user's local environment per the 2026-07-14 direction: "I will test the Salesforce, Gong, Google Sheets, and Drive workflow later in my local environment and use Claude Code to address any operational issues." Do not resume POV-002 work until the user asks for it. |
| STRUCT-001 | Structured outputs and rendering | Define Pydantic schemas for high-risk skill outputs (`biz-qual`, `tech-qual`, `deployment-model-qual`, `poc-plan`, `connector-feasibility`). | No runtime validation that generated markdown contains required sections. | `webapp/output_schema.py`; `webapp/static/app.js:1737-1880` regex-parses headings. | confirmed | High | High | Medium | EVAL-001 | Medium (schemas may need iteration) | Schemas validate required sections and key/value types | Unit tests on schema validation | Completed | `webapp/output_schema.py` defines `SkillOutputSchema` and `OutputMetadata`; validates title, date, source coverage, and decision-critical sections for the five skills. |
| STRUCT-002 | Structured outputs and rendering | Unify Markdown parsing: one safe, shared renderer for webapp, PDF, and internal HTML. | Multiple parsers with different HTML-escaping behavior. | `webapp/static/app.js:412-472`; `webapp/md_render.py`; `webapp/pdf_render.py`; `webapp/internal_html.py` | confirmed | High | Medium | Medium | None | Medium (may change rendering slightly) | All three renderers produce identical, safe HTML for the same Markdown fixture | Snapshot tests | Completed | `webapp/md_render.py` is the single shared renderer; PDF, internal HTML, and `POST /api/output/render` all use it; `app.js` fetches the renderer and applies presentation-only CSS classes; SSE streams emit pre-rendered `html`. |
| STRUCT-003 | Structured outputs and rendering | Add optional sidecar metadata (`<output>.json`) alongside Markdown outputs for programmatic access. | Downstream skills and UI currently re-parse Markdown. | `skills/_se-playbook.md:362-374` defines output paths; `webapp/output_schema.py` writes sidecars. | hypothesis | Medium | Medium | Medium | STRUCT-001 | Medium | Each saving skill writes a small JSON sidecar; UI can read it but still supports legacy `.md` only | `pytest` + UI smoke test | Completed | `webapp/app.py` writes `.md.json` sidecars after each run; `list_outputs` and `GET /api/output/meta` expose `valid` / `validation_errors`; legacy `.md` files remain the source of truth. |
| ORCH-001 | Workflow orchestration | Introduce a deterministic prerequisite checker / planner that enforces skill sequencing before invocation. | Workflow order is suggested, not enforced; free-form invoke can run anything. | `skills/_se-playbook.md:191-262`; `webapp/app.py:1530-1545` (freeform prompt) | confirmed | Medium | High | Large | EVAL-001, STRUCT-001 | High (changes UX and behavior) | Planner returns `ready`/`missing` list; UI blocks or warns before running out-of-order skill | Unit tests + manual webapp test | Completed | Implemented `webapp/orchestrator.py` with `GET /api/plan` and `POST /api/invoke` override; UI uses `invokeWithPlan` confirm; override always allowed. |
| ORCH-002 | Workflow orchestration | Handle `full-qual` partial failure atomically (report which child failed, do not leave stale state). | `full-qual` runs `biz-qual` then `tech-qual` with no combined rollback. | `skills/full-qual/SKILL.md:49-73` | confirmed | Medium | Low | Small | EVAL-001 | Low | If `biz-qual` or `tech-qual` refuses, `full-qual` output clearly says which and why | `pytest` + mock suite | Completed | Partial-failure section added; manifest `phase1-full-qual-partial-failure.yaml` passes |
| ORCH-003 | Workflow orchestration | Enforce `next-move` evidence requirements and surface missing data instead of overconfident recommendations. | `next-move` shallow read contract may miss nuance. | `skills/next-move/SKILL.md:64-72` | confirmed | Medium | Medium | Medium | EVAL-001 | Low | `next-move` never recommends a skill whose prerequisites are missing without flagging | `pytest` + mock suite | Completed | Missing-prerequisite flag section added; manifest `phase1-next-move-missing-prereq.yaml` passes |
| ARCH-001 | Application architecture and maintainability | Refactor `webapp/app.py` into modules (routes, services, sfdc, git, render, transcribe). | 2083-line monolith still mixing routes, service logic, and integration helpers. | `webapp/app.py` | confirmed | Medium | High | Medium | None | Medium (risk of regressions) | Job lifecycle extracted in Airbyte-Solutions-Team/se-skills#30; output and feedback lifecycle extracted as Slice 2 in Airbyte-Solutions-Team/se-skills#31; member/account/opportunity domain extracted as Slice 3 in Airbyte-Solutions-Team/se-skills#32; overview aggregation extracted as Slice 4 in Airbyte-Solutions-Team/se-skills#33; all route URLs, request/response shapes, status codes, folder conventions, display names/slugs, sorting/filtering, empty states, output summaries, job-activity indicators, and overview response shape preserved | `pytest eval/tests/test_account_service.py`, `pytest eval/tests/test_accounts_routes.py`, smoke tests, manual UI run | Partial (job + output/feedback + member/account/opportunity subsystems) | Airbyte-Solutions-Team/se-skills#30 completed the job-lifecycle slice. Airbyte-Solutions-Team/se-skills#31 completed the output/feedback slice. Airbyte-Solutions-Team/se-skills#32 completed the member/account/opportunity slice. Airbyte-Solutions-Team/se-skills#33 completes the overview aggregation slice: `webapp/services/overview_service.py` owns summary counts, activity classification, attention/recent rules, sorting/ranking, maximum-item limits, href formatting, status mapping, account/opportunity rollups, and safe empty-state fallback; `webapp/routes/overview.py` registers the thin `GET /api/overview` route; `JobService.overview_jobs()` provides a full job snapshot (including `stderr`) for overview consumption while `list_jobs` continues to strip `sig`/`stdout`/`stderr` for public `/api/jobs` consumers. ARCH-001 remains Partial. Remaining slices: external integration boundaries (Salesforce/Gong/Google Sheets), live transcription and Ask workflows, startup/config cleanup, and future ARCH-002/003 dev-tooling. |
| ARCH-002 | Application architecture and maintainability | Add a declared dependency manifest (`pyproject.toml` or `requirements-dev.txt`) and dev tools. | No `pyproject.toml`, `requirements.txt`, or `package.json` exists. | Repo root | confirmed | Medium | Medium | Small | None | Low | `pip install -e .[dev]` or `uv sync --dev` works; lint + test commands defined | CI / local install | Proposed | Keep PEP 723 inline deps in `app.py` for runtime |
| ARCH-003 | Application architecture and maintainability | Add type checking (`pyright` or `mypy`) and lint configuration. | `webapp/app.py` lacks type annotations in many places. | `webapp/app.py` | confirmed | Low | Medium | Small | ARCH-002 | Low | Type checker passes on refactored modules | CI check | Proposed | Start with new modules |
| ARCH-004 | Application architecture and maintainability | Make jobs and live-transcribe session *records* durable across webapp restarts (processes cannot be resumed after a server stop). | `JOBS` and `LiveSession` are in-memory only. | `webapp/app.py:1527-1528`, `webapp/app.py:1697-1698`, `webapp/LIVE-TRANSCRIBE.md:44-49` | confirmed | Medium | Medium | Medium | None | Medium | Jobs and live sessions persisted to disk; recovered jobs marked lost with a re-run message; recovered sessions offered as read-only Save; persistence failures log and surface UI warnings | Unit tests + manual restart | Completed | `webapp/persistence.py` added; `JOBS`/`SESSIONS` load/save integrated in `app.py`; session files deleted on explicit stop; write failures return `False` and do not crash the app |
| ARCH-005 | Application architecture and maintainability | Abstract model provider/version and allow per-skill model configuration. | Model name `claude-sonnet-4-6` is hardcoded. | `webapp/app.py:1337`, `webapp/app.py:2117` | confirmed | Medium | Medium | Small | None | Low | `.se-config.yaml` supports `models:` block; quick/deep asks use configured model | Config parse tests | Completed | `_model_for(use)` reads `.se-config.yaml` `models:`; `_run_job` passes `--model` to `claude -p`; quick/live asks use `_model_for` |
| ARCH-006 | Application architecture and maintainability | Add structured logging, metrics, and an audit trail of skill invocations and file writes. | No observability into what the agent actually did. | `webapp/app.py` `_run_job` | confirmed | Medium | High | Small | None | Low | Every skill invocation logs command, duration, files touched, MCP calls, and errors | `pytest` log capture | Proposed | Write logs outside customer workspace |
| ARCH-007 | Application architecture and maintainability | Refresh skill discovery at runtime when `skills/` changes. | `discover_skills()` runs once at import. | `webapp/app.py:116-146` | confirmed | Low | Low | Small | None | Low | File watcher or `/api/reload` endpoint updates skill list without restart | Unit test | Completed | `POST /api/reload` refreshes `SKILLS`/`SKILL_IDS`; invoke modal has a ↻ reload button |
| UX-001 | UX and learning loop | Add output review/approval/correction workflow and persist corrections to memory or golden fixtures. | SEs have no structured way to give feedback on generated docs. | `webapp/static/app.js` output reader; `skills/_se-playbook.md:605-616` Memory Check | confirmed | Medium | High | Medium | EVAL-001 | Medium | Compact, expandable review panel in the output reader; Approve/Comment/Correct actions with color-coded history; feedback persisted in `.md.feedback.jsonl` sidecars | `pytest` + UI smoke test | Completed | Output review panel with approve/comment/correct feedback persisted as `.md.feedback.jsonl` sidecars. Form is hidden until an action is selected; existing entries are summarized by latest status and kept visually secondary. The **Promote to golden fixture** UI action was removed because the regression test runs `MockExecutor`, not `SKILL.md`/Claude, so a promoted fixture would not make real skill runs follow the correction; `POST /api/output/golden` remains for developer-managed mock baselines. |
| UX-002 | UX and learning loop | Add semantic deal-assessment diff/trend view so an SE can see what materially changed between two assessments without reading raw Markdown. | Dated files exist but UI groups by day with no comparison; the existing modal is a raw line diff. | `webapp/static/app.js:openDealDiffModal`, `webapp/app.py:api_output_diff`, `webapp/output_schema.py:semantic_diff` | confirmed | Low | Medium | Medium | UX-001 | Low | Semantic summary (sections changed, risks added/removed, actions changed), section-by-section before/after, item-level risk/action lists, expandable raw Markdown diff fallback, improved output selector with date and safe legacy handling | `pytest` + UI smoke test (changed At a Glance, risk added/removed/changed, blocker/close/action changes, mostly unchanged, legacy/missing metadata, non-deal-assessment output, long sections, narrow viewport) | Completed | `POST /api/output/diff` now returns both a deterministic semantic comparison (using `OutputMetadata` sidecars with Markdown fallback) and the raw line diff. The modal defaults to semantic view, keeps unchanged sections secondary, and exposes the raw diff under "Raw Markdown diff". Selector shows generated date, prevents self-comparison, and degrades safely for legacy outputs without sidecars. |
| UX-003 | UX and learning loop | Integrate action items with calendar/task tools (optional). | Action items live only in Markdown. | `skills/post-call/SKILL.md` Action Items | hypothesis | Low | Large | Large | None | High | Design doc for Salesforce task / calendar integration | Review | Proposed | Deferred; not a daily blocker |
| UX-004 | UX and learning loop | Improve discoverability of `roi-business-case` and `mutual-close-plan` in the webapp (they were previously ungrouped / hidden in "Anytime" despite logical late-workflow placement). | UI tiering may cause SEs to miss them. | `webapp/app.py:87-113` `SKILL_PRESENTATION`; `webapp/static/app.js:openInvoke` | confirmed | Low | Low | Small | None | Low | Add a "Late-stage" tier or contextual prompt after `poc-plan`; selected skill in the invoke modal shows its tier/step badge. | `pytest` + UI review | Completed | `TIER_LATE` added with steps 8-9; tests in `eval/tests/test_webapp_skill_tiers.py`. In the invoke modal the selected skill displays a tier/step badge next to the picker, so the SE sees where the skill belongs even after the dropdown closes. |
| UX-005 | UX and learning loop | Improve live-transcribe session recovery and add per-speaker labels. | Session is lost on restart; speaker diarization is absent. | `webapp/app.py:1797-2205`; `webapp/LIVE-TRANSCRIBE.md` | confirmed | Low | Medium | Medium | ARCH-004 | Medium | Persisted sessions recovered on startup as read-only; editable mic/call labels in setup, embedded in saved transcripts, and styled by speaker | Unit tests + manual test | Completed | Two-channel manual labels implemented; per-person diarization remains out of scope |
| UX-006 | UX and learning loop | Improve output-reader information hierarchy, status clarity, and action discoverability. | Generated output pages have stacked warning banners, an undifferentiated action bar, a feedback panel that dominates the document, and dense/undifferentiated section styling. | `webapp/static/app.js:openOutput`, `webapp/static/app.js:loadFeedbackPanel`, `webapp/static/app.js:renderOutputGroups`; `webapp/static/style.css` reader styles | confirmed | Medium | High | Medium | UX-001, UX-002, STRUCT-002 | Medium | Unified `Document status` bar replaces validation + reference banners; primary/secondary/destructive action hierarchy; compact expandable feedback; redundant Date/Skill meta suppressed; decision/risk sections styled as callout cards; responsive output-reader behavior preserved | `pytest` + UI smoke test (desktop, narrow laptop, small viewport) | Completed | Implemented as a cohesive output-reader batch. Details and screenshots in `ux-assessment.md`. Invokes no backend or skill changes beyond the existing `validation_status` / `reference_freshness_at_generation` / `reference_changed_since_generation` metadata. |
| UX-007 | UX and learning loop | Improve skill-invocation modal clarity and account/opportunity navigation on narrow screens. | Invoke modal is dense (long descriptions push the Run action below the fold), prerequisite/permission disclosures can be hidden or look like unrelated warnings, and account/opportunity tables clip/overflow on narrow laptop and small viewports. | `webapp/static/app.js:openInvoke`, `webapp/static/style.css` modal + responsive table styles, `webapp/static/index.html` modal markup | confirmed | Medium | High | Medium | UX-004, ORCH-001 | Medium | Compact skill summary with expandable details; calm prerequisite disclosure; visible expected-permissions disclosure; tier badge; sticky Run/Cancel; account and opportunity tables hide lower-priority columns progressively on narrow viewports | `pytest` + manual webapp test (desktop, narrow laptop, small viewport; invoke with normal/missing-prereq/broad-perm skills) | Completed | Implemented as a cohesive navigation-and-invocation batch. Details in `ux-assessment.md`. No backend or skill changes; only presentation and responsive CSS. |
| UX-008 | UX and learning loop | Improve account and opportunity list readability, activity context, and empty states. | Account and opportunity lists still feel like dense data tables: every field has equal weight, there is no signal of running or failed work, and empty states give no next action. | `webapp/static/app.js` `pageMember`, `pageAccount`, `oppRow`; `webapp/static/style.css` account/opp list styles; `webapp/app.py` `_run_job` job timestamp | confirmed | Medium | High | Medium | UX-007 | Low | Two-line row hierarchy with name + activity, stage + close date, outputs, owner; muted secondary meta; running/failed job status visible on rows; actionable empty states for no accounts, no opps, no outputs; responsive columns hide progressively | `pytest` + manual webapp test (account list, long names, multi-opp account, no outputs, running/failed jobs, narrow/mobile) | Completed | Adds `finished_at` to in-memory job records so the UI can surface latest run state without a new endpoint. Small presentation-only backend change; no skill or data-model changes. See `ux-assessment.md` problem F for supporting research. |
| UX-009 | UX and learning loop | Add a calm operational overview to the team landing page and member directory. | The main landing page is just a card grid of names; an SE or manager cannot see where work is happening, what failed, what needs review, or where to click next. | `webapp/static/app.js` `pageMembers`; `webapp/static/style.css` overview styles; `webapp/app.py` new `/api/overview` aggregation | confirmed | Medium | High | Medium | UX-008 | Low | Summary counts (members, accounts, opportunities, outputs, running, failed, needs attention); attention-needed list (long-running, failed/interrupted, outputs needing attention with review and validation states separated, stale activity); compact recent activity; member directory cards with objective workload info; preserved Team → Member → Account → Opportunity → Output navigation; empty/quiet states; responsive desktop/narrow/mobile | `pytest` + manual webapp test (multiple members, running jobs, failed/interrupted, recent outputs, outputs needing attention, no attention, no recent activity, empty/no accounts, long names, navigation, app restart persistence) | Completed | New `/api/overview` endpoint aggregates existing filesystem + job state in one pass. Adds `started_at` to job records for long-running detection; recovered interrupted jobs keep `finished_at`. Review workflow state (from `.feedback.jsonl`) is kept distinct from validation state (from `.md.json`); malformed or missing sidecars/feedback degrade safely and do not crash the endpoint. No risk/health scoring or composite labels — only objective signals. Closeout regression pass fixed unvalidated outputs being counted as validation attention. |

### 4.1 Deferred UX ideas

The UI/UX improvement phase is complete. The following ideas were considered during the original UX assessment but are deferred as future backlog rather than active work:

- **Follow-up chat redesign:** Make the ask-bar on an output feel like a guided next-step workflow (suggest the right next skill, surface prerequisites, preserve transcript context). This would extend UX-006/UX-007 but is not required to complete the current UX phase.
- **Richer per-member workload visualization:** Cards currently show objective counts. Future work could surface a compact “where is this person stuck” signal, but only when a reliable, non-inferred rule exists.
- **Deep real-time reference-freshness dashboard:** The landing page intentionally does not count source-changed outputs as attention. A dedicated freshness view could be added later if source-change detection matures.
- **Calendar/task integration for action items:** Tracked as UX-003 (Proposed / deferred) and remains a large, integration-heavy lift.
- **Per-speaker live-transcribe diarization:** Tracked as part of UX-005; two-channel labels are implemented, true speaker separation is out of scope for the current phase.

These remain ideas; no active backlog items are created for them unless a future phase explicitly scopes them.

---

## 5. Workstreams

### A. Evaluation and reliability

Purpose: establish a fast, deterministic test harness and a slower model-dependent evaluation harness so every future change is validated against realistic SE behavior.

Backlog IDs: **EVAL-001, EVAL-002, EVAL-003, EVAL-004, EVAL-005, EVAL-006**

Key themes:
- Synthetic SE test fixtures (`eval/fixtures/`)
- Business-invariant checks (capacity sizing, sync frequency, concurrency, throughput)
- Required-section validation for markdown outputs
- Prompt-regression tests and golden cases
- Model-dependent integration tests with an LLM-as-judge
- Skill and prompt version tracking
- CI integration
- Failure reporting

### B. Agent permissions and execution safety

Purpose: ensure the agent cannot be tricked into performing unwanted actions, and that every destructive action requires explicit approval.

Backlog IDs: **SEC-001, SEC-002**

Key themes:
- Review of `--permission-mode acceptEdits` (`webapp/app.py:1586-1598`)
- Skill-specific write requirements
- Read-only vs. write-capable execution
- Approval gates in the web UI
- Shell and git action boundaries
- Transcript and prompt-injection risks
- Auditability of agent actions

### C. Input, data, and export security

Purpose: sanitize and secure all places where external or user data enters the system or leaves it as an export.

Backlog IDs: **SEC-003, SEC-004, SEC-005, SEC-006**

Key themes:
- Salesforce query construction and wildcard escaping
- Free-form prompt sanitization
- Live-transcript input handling
- HTML and Markdown export sanitization
- API key storage (`webapp/app.py:2133-2145`)
- Customer-data handling
- External model calls and data retention
- Authentication if the app is hosted

### D. Skill and prompt architecture

Purpose: reduce duplication, centralize shared guidance, and keep prompts aligned with current product reality.

Backlog IDs: **SKILL-001, SKILL-002, SKILL-003, SKILL-004**

Key themes:
- Shared instruction centralization in `_se-playbook.md`
- Prompt duplication reduction
- Business guardrails and customer-constraint preservation
- Facts vs. assumptions discipline
- Unsupported product claims
- Missing-input behavior
- Skill dependencies and reference-data freshness
- Prompt versioning

### E. Structured outputs and rendering

Purpose: make generated artifacts machine-readable, validate them at runtime, and render them safely and consistently.

Backlog IDs: **STRUCT-001, STRUCT-002, STRUCT-003**

Key themes:
- Evaluation manifests for test scenarios
- Optional sidecar metadata
- Pydantic or JSON schemas for high-risk skills
- Markdown compatibility and safe shared rendering
- Runtime validation of required sections
- UI dependence on headings and regex parsing

### F. Workflow orchestration

Purpose: move from suggestions to deterministic enforcement of skill prerequisites and safe failure modes.

Backlog IDs: **ORCH-001, ORCH-002, ORCH-003**

Key themes:
- Deterministic prerequisite checks
- Router behavior and `next-move` evidence requirements
- Sequential vs. parallel skill execution
- Cross-skill contradiction detection
- `full-qual` partial-failure handling
- Workflow states and human approval points

### G. Application architecture and maintainability

Purpose: make the webapp maintainable, observable, and portable.

Backlog IDs: **ARCH-001, ARCH-002, ARCH-003, ARCH-004, ARCH-005, ARCH-006, ARCH-007**

Key themes:
- Breaking up `webapp/app.py`
- Dependency declaration
- Type checking and lint
- Error handling
- Durable jobs and sessions
- Model configuration and provider abstraction
- Logging, metrics, and audit trails
- Skill discovery refresh

### H. UX and learning loop

Purpose: close the loop between generated output and corrected, reusable knowledge.

Backlog IDs: **UX-001, UX-002, UX-003, UX-004, UX-005, UX-006, UX-007, UX-008, UX-009**

Key themes:
- Output review and approval
- Inline corrections
- Feedback capture and golden-case generation
- Version comparison and deal-health trend views
- Action-item integrations
- Source traceability
- User corrections becoming evaluation cases
- Output-reader status, hierarchy, and visual clarity

---

## 6. Prioritization framework

Work should be prioritized by these factors, in roughly this order:

1. **Customer or deal impact** — Could this cause an incorrect customer-facing recommendation?
2. **Security exposure** — Does this expose customer data, credentials, or allow unauthorized actions?
3. **Likelihood of incorrect recommendations** — How often is this code path hit, and how wrong can the output be?
4. **Ability to detect regressions** — Will the evaluation framework catch future drift?
5. **Implementation complexity** — How much risk and time does the change introduce?
6. **Risk of breaking existing behavior** — Are we changing a public interface or a prompt contract?
7. **Dependency on unfinished architecture** — Does this block on a refactor or a schema?
8. **Value to daily SE workflows** — Does this save time or reduce friction for the SE?

**Explicit rule:** Code cleanliness alone should not outrank behavior, security, or reliability. Refactoring `app.py` is valuable, but it should happen after there are tests to guard it.

---

## 7. Phased roadmap

### Phase 0: Baseline and documentation

Status: **In progress** (this plan).

Entry criteria:
- Assessment completed.
- Implementation plan created.

Exit criteria:
- `IMPLEMENTATION-PLAN.md` committed to the repository.
- Highest-risk skills identified: `connector-feasibility`, `deployment-model-qual`, `tech-qual`, `poc-plan`, `next-move`.
- Initial business invariants documented (see EVAL-006).
- Skills classified by write/shell/git needs for SEC-001.

### Phase 1: Safe, lightweight evaluation foundation

**Scope:** Build an `eval/` framework that can detect the highest-risk behaviors without requiring a full refactor, Pydantic migration, or hosted infrastructure.

Target skills:
- `connector-feasibility`
- `deployment-model-qual`
- `tech-qual`
- `poc-plan`
- `next-move`

Also cover repository logic involving:
- Capacity sizing
- Data workers
- Optimization
- Sync frequency
- Concurrency
- Throughput

Initial framework detects:
- Changing a customer requirement without permission
- Reducing sync frequency to improve a primary sizing recommendation
- Presenting assumptions as facts
- Claiming unsupported connector or product capabilities
- Recommending a deployment model without sufficient evidence
- Ignoring missing inputs
- Producing unsupported next actions
- Omitting source coverage or decision rationale

**Do not require:**
- A full application refactor
- Conversion of every skill to Pydantic
- A complete LLM-as-judge platform
- Hosted infrastructure
- A full CI pipeline
- Rewriting every skill prompt

Entry criteria:
- Phase 0 complete.
- Explicit go-ahead to begin Phase 1.

Exit criteria:
- `eval/` directory exists with fixtures, manifests, and deterministic tests.
- Six synthetic scenarios run and produce pass/fail results.
- Baseline report documents which tests pass and which fail against current skills.
- No real customer data is used.
- Framework supports adding new scenarios without deep Python knowledge.
- Model-dependent tests are optional and clearly separated.

### Phase 2: Security quick wins and execution boundaries

Status: **Completed**.

Scope:
- SEC-002: sanitize free-form and live-transcribe inputs.
- SEC-003: fix Salesforce query construction.
- SEC-004: sanitize exported HTML/PDF.
- SEC-001 analysis: classify each skill by write/shell/git needs and add UI approval gates.
- SEC-005: move API key storage to a keyring.

Entry criteria:
- Phase 1 baseline established.
- Deterministic tests can catch regressions in affected paths.

Exit criteria:
- All five security quick wins (SEC-001–005) merged with regression tests.
- `acceptEdits` decision documented and gated.
- API key storage uses keyring; plaintext `~/.mcp/*.env` files are no longer read.

### Phase 3: Skill-behavior guardrails (narrow, 2026-07-14)

In-scope skills: `connector-feasibility`, `deployment-model-qual`, `tech-qual`, `poc-plan`, `next-move`, and the capacity/cost portions of `roi-business-case`.

Completed guardrails:
- **Preserve customer requirements** as the baseline; alternatives must be labeled with trade-offs and never silently substituted.
- `connector-feasibility`: explicit five-dimension distinction (availability · supported sync · auth/network · full use-case · native vs workaround).
- `deployment-model-qual`: recommendation separates technical fit, verified entitlement/packaging, security preference, commercial dependency, and still-to-validate items.
- `tech-qual`: no `🟢 Strong` fit when a critical requirement is unknown/unverified; four-bucket classification (confirmed fit · solvable risk · critical blocker · open validation item).
- `poc-plan`: preserve customer success criteria; separate minimum viable POC scope, optional stretch, production requirements, and POC-specific simplifications.
- `next-move`: already-completed action guard + low-confidence → gather evidence; recommend downstream actions, not repeat existing qualifications.
- `roi-business-case`: requested operating model is the primary scenario; label optimization alternatives; show missing inputs that materially affect the result; avoid false precision.

Evaluation coverage:
- Five targeted deterministic scenarios added/updated: hourly sync baseline, connector-CDC unverified, POC difficult criterion, tech-qual missing critical requirement, next-move no-repeat.
- No new generalized semantic-evaluation platform, JSON/Pydantic conversion, or webapp changes.

Exit criteria:
- Targeted skills include concise guardrail instructions in `SKILL.md`.
- Deterministic evaluation manifests exercise each core behavior.
- `uv run --extra dev pytest eval/ -v` and the mock suite pass.

Deferred from Phase 3:
- SKILL-001 (centralize shared fragments), SKILL-002 (reference-data freshness warnings), SKILL-003 (`objection-handler` verification), STRUCT-001/002/003 — moved to Phase 4 / backlog unless a future task narrows them.

### Phase 4: Orchestration and feedback loop

Status: **Completed** (ORCH-001–003 and UX-001/002/004/006–009 delivered; correction-to-golden learning loop remains mock-only because `MockExecutor` does not run `SKILL.md`/Claude; real golden regression is tracked under EVAL-002).

Scope:
- ORCH-001: deterministic prerequisite checker / planner.
- ORCH-002: `full-qual` partial-failure handling. *(Completed — `full-qual` now reports child skill status and the mock suite has a partial-failure scenario.)*
- ORCH-003: `next-move` evidence requirements. *(Completed — missing-prerequisite flag added and `phase1-next-move-missing-prereq.yaml` passes.)*
- UX-001: output review, corrections, and golden-case capture. *(Completed — output review/feedback panel with sidecar JSONL persistence and a **Promote to golden fixture** action; `eval/golden/` regression baselines are maintained by `test_skill_regression.py`.)*
- UX-002: semantic deal-assessment diff/trend view. *(Completed — semantic comparison modal on `deal-assessment` outputs via `POST /api/output/diff`, with section-level summary, risk/action item diff, and raw Markdown fallback.)*
- UX-004: better `roi-business-case` / `mutual-close-plan` discoverability. *(Completed — `TIER_LATE` added in `webapp/app.py` with `pytest` coverage.)*

Entry criteria:
- Phase 3 complete.
- Output schemas can drive UI behavior.

Exit criteria:
- UI blocks or warns on out-of-order skill invocation.
- SEs can correct outputs and corrections are persisted; mock golden-case baselines are maintained by `test_skill_regression.py`. A real-skill correction-to-regression loop remains future work (EVAL-002).

### Phase 5: Architectural improvements

Scope:
- ARCH-001: refactor `app.py`.
- ARCH-002: dependency manifest.
- ARCH-003: type checking.
- ARCH-004: durable jobs and sessions.
- ARCH-005: model-provider abstraction.
- ARCH-006: logging, metrics, and audit trails.
- ARCH-007: runtime skill discovery refresh.
- UX-005: live-transcribe session recovery and speaker labels.
- UX-003 (optional): calendar/task integration.
- SEC-006 (if applicable): hosted auth and encryption.

Entry criteria:
- Phase 4 complete.
- Test coverage makes refactor safe.

Exit criteria:
- `app.py` is modular.
- CI runs lint, type check, and deterministic tests.
- Durable sessions survive webapp restart.

---

## 8. Phase 1 implementation specification

### 8.1 Proposed directory structure

```
eval/
├── README.md                 # How to run the suite and add a scenario
├── conftest.py               # Pytest fixtures and shared helpers
├── runner.py                 # Skill invocation wrapper
├── manifests/                # YAML evaluation manifests
│   ├── phase1/
│   │   ├── hourly-sync-constraint.yaml
│   │   ├── unverified-connector.yaml
│   │   ├── missing-technical-input.yaml
│   │   ├── sfdc-transcript-conflict.yaml
│   │   ├── next-move-low-evidence.yaml
│   │   └── unverified-entitlement.yaml
├── fixtures/                 # Synthetic inputs
│   ├── transcripts/
│   │   ├── acme-2026-07-01-hourly.txt
│   │   ├── acme-2026-07-01-no-tech.txt
│   │   ├── acme-2026-07-01-conflict.txt
│   │   └── ...
│   ├── config/
│   │   └── synthetic-se-config.yaml
│   ├── outputs/                # Optional pre-existing downstream docs
│   │   └── .gitkeep
│   └── expected/               # Golden or partial-expected artifacts (optional)
│       └── .gitkeep
├── tests/
│   ├── test_manifests.py       # Deterministic tests for all manifests
│   └── test_invariants.py      # Cross-skill invariant checks
└── results/                    # Generated by test runs, gitignored
    └── .gitkeep
```

### 8.2 Fixture format

**Transcript fixture (`fixtures/transcripts/*.txt`):**
A plain-text Gong-style transcript. Speaker labels are lines ending in a colon, e.g.:

```
John (Acme): We need hourly refresh for our sales pipeline.
Gary (Airbyte): What volume are we talking about?
John (Acme): About 2M rows per day, mostly during business hours.
```

**Config fixture (`fixtures/config/synthetic-se-config.yaml`):**
A minimal `.se-config.yaml` that points `workspace_root` to a temp directory created per test run:

```yaml
workspace_root: "{tmp_dir}"
name: "Synthetic SE"
email: "se-test@airbyte.io"
slack_handle: "@se-test"
role: "Solutions Engineer"
aliases: ["SE Test"]
salesforce:
  enabled: false
```

The runner substitutes `{tmp_dir}` with a unique temporary directory and copies fixtures into it.

### 8.3 Evaluation manifest format

Each manifest is a YAML file with this schema:

```yaml
manifest_version: "1.0"
id: "phase1-hourly-sync-constraint"
title: "Capacity sizing must not reduce sync frequency without permission"
skills_under_test:
  - connector-feasibility
  - deployment-model-qual
  - tech-qual
  - poc-plan
  - next-move
tags: ["capacity", "sync-frequency", "constraint-preservation"]
fixtures:
  transcripts:
    - source: "fixtures/transcripts/acme-2026-07-01-hourly.txt"
      target: "_transcripts/Acme-07.01.26.txt"
  existing_outputs: []
  config: "fixtures/config/synthetic-se-config.yaml"
environment:
  reference_data: {}  # explicitly empty for unverified-connector/entitlement
  salesforce: disabled
customer_constraints:
  - "Hourly synchronization is required for the sales pipeline."
  - "2M rows per day, business-hours skew."
available_evidence:
  - "One transcript with explicit hourly requirement."
  - "No prior biz-qual or tech-qual."
required_behavior:
  - "Any capacity or data-worker estimate must assume hourly sync."
  - "If data is insufficient, output must ask for more inputs, not optimize by lowering frequency."
  - "Source Coverage section must be present and list the transcript."
forbidden_behavior:
  - "Recommending daily/weekly sync frequency to reduce data workers without explicit customer permission."
  - "Presenting a capacity number as customer-confirmed when it is interpolated."
expected_sections:
  - "At a Glance"
  - "Source Coverage"
  - "Data Volume & Scale"
deterministic_assertions:
  - name: "refuses to lower sync frequency"
    target: "markdown"
    check: "not contains_any_case_insensitive(output, ['recommend lowering to daily', 'recommend daily sync', 'reduce frequency to daily'])"
    severity: "blocker"
  - name: "reports source coverage"
    target: "markdown"
    check: "contains_case_insensitive(output, 'Source Coverage')"
    severity: "blocker"
  - name: "does not hallucinate entitlement"
    target: "markdown"
    check: "not contains_any_case_insensitive(output, ['verified against EntitlementDefinitions.kt', 'confirmed by airbyte-platform']) when reference_data.repos.airbyte_platform is absent"
    severity: "major"
model_judge:
  enabled: true
  criteria:
    - "Does the recommendation respect the hourly constraint?"
    - "Does it clearly label assumptions that are not customer-stated?"
    - "Is the tone appropriate for a pre-sales SE?"
  threshold: "all"
failure_severity: "blocker"  # blocker / major / minor
notes: "If the skill refuses because prerequisites are missing, that is a valid pass if the refusal is correct."
```

The exact assertion language can be small Python helper functions (`eval/assertions.py`).

### 8.4 Deterministic assertion format

Assertions are plain Python functions in `eval/assertions.py` and invoked from `test_manifests.py`:

```python
def contains_case_insensitive(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()

def contains_any_case_insensitive(text: str, needles: list[str]) -> bool:
    return any(contains_case_insensitive(text, n) for n in needles)

def has_section(markdown: str, heading: str) -> bool:
    return bool(re.search(rf"^#+\s+{re.escape(heading)}\s*$", markdown, re.MULTILINE | re.IGNORECASE))

def has_all_sections(markdown: str, headings: list[str]) -> bool:
    return all(has_section(markdown, h) for h in headings)

def section_contains(markdown: str, heading: str, phrase: str) -> bool:
    # naive section extractor for deterministic tests
    ...
```

These are intentionally robust-but-simple: they only check Markdown structure and phrase presence, not semantic quality.

### 8.5 Optional model-invocation mode

Each manifest has a `model_judge.enabled` flag.

- When `false`, the test runs only deterministic assertions.
- When `true`, the test also sends the generated output and the manifest criteria to a lightweight judge prompt. The judge returns a JSON `{"pass": bool, "reason": str}`.
- Model-judge tests are marked with `@pytest.mark.model_dependent` and skipped unless `--run-model-judge` is passed.

This keeps the fast CI deterministic and makes the slower, more expensive evaluation optional.

### 8.6 Temporary output handling

- The runner creates a new temporary directory for each test (`tmp_path` in pytest).
- It copies fixtures into `{tmp}/customers/Acme/`, `{tmp}/_transcripts/`, etc., according to the manifest.
- It sets `SE_WORKSPACE={tmp}` for the skill invocation.
- It captures all files written under `{tmp}/customers/Acme/outputs/`.
- After the test, outputs are copied to `eval/results/{manifest_id}/` only if the test fails or if `KEEP_RESULTS=1` is set.
- Real customer workspaces (`~/.se-skills`, `~/airbyte-work`) are never touched.

### 8.7 Pass/fail reporting

- `pytest` standard output.
- A `eval/results/phase1-report.json` with per-manifest status, failure reasons, and which assertions failed.
- Failed model-judge results include the judge's reasoning.

### 8.8 Severity levels

| Level | Meaning | Action |
|---|---|---|
| `blocker` | Output is unsafe or materially wrong; skill should refuse or be fixed before use. | Fail the build. |
| `major` | Important guidance violated; may still be usable with explicit warnings. | Block merge unless documented. |
| `minor` | Style, formatting, or low-impact issue. | Track, fix opportunistically. |

### 8.9 Prompt and skill version capture

- Each `SKILL.md` frontmatter gains a `version:` field (semver or ISO date) when it is first modified.
- `eval/runner.py` reads `version` from `SKILL.md` and writes it into the result report.
- A CI/pre-commit check warns if `version` is missing from a changed skill.
- This is lightweight and does not require prompt-version registries yet.

### 8.10 How an SE can add a test without deep Python knowledge

1. Create a new transcript fixture in `eval/fixtures/transcripts/`.
2. Create a new YAML manifest in `eval/manifests/phase1/` copying an existing one.
3. Fill in `customer_constraints`, `available_evidence`, `required_behavior`, `forbidden_behavior`, `expected_sections`.
4. If needed, ask a Python-savvy teammate to add a new assertion helper.
5. Run `pytest eval/tests/test_manifests.py::test_manifest -k <manifest_id>`.

### 8.11 Separation of model-dependent tests

- Deterministic tests: `pytest eval/ -m "not model_dependent"` (default).
- Model-judge tests: `pytest eval/ -m model_dependent --run-model-judge` (requires `ANTHROPIC_API_KEY`).
- A nightly or manual job can run model-judge tests; fast CI runs only deterministic tests.

### 8.12 Phase 1 synthetic scenarios

#### Scenario 1: Hourly sync requirement, lower frequency would reduce capacity

- **ID:** `phase1-hourly-sync-constraint`
- **Skills:** `tech-qual`, `poc-plan`, `next-move`
- **Customer constraints:** Hourly sync is required; 2M rows/day business-hours skew.
- **Available evidence:** One transcript with the hourly requirement; no prior biz-qual.
- **Required behavior:** Capacity/data-worker estimate must assume hourly. If insufficient, ask for more inputs.
- **Forbidden behavior:** Recommending a lower sync frequency to reduce data workers without explicit permission.
- **Expected sections:** `At a Glance`, `Data Volume & Scale`, `Source Coverage`.
- **Failure severity:** `blocker`.

#### Scenario 2: Connector cannot be verified against product sources

- **ID:** `phase1-unverified-connector`
- **Skills:** `connector-feasibility`, `tech-qual`
- **Customer constraints:** Customer wants to move data from `source-foo-bar`.
- **Available evidence:** Transcript names the system; registry and `airbyte-enterprise` are unavailable.
- **Required behavior:** State "connector not found / cannot verify", cap confidence, list unavailable sources in `Source Coverage`.
- **Forbidden behavior:** Claiming the connector exists, is certified, or is available on Cloud/Flex.
- **Expected sections:** `At a Glance`, `Data Sources & Destinations`, `Source Coverage`.
- **Failure severity:** `blocker`.

#### Scenario 3: Required technical input is absent

- **ID:** `phase1-missing-technical-input`
- **Skills:** `tech-qual`
- **Customer constraints:** None technical.
- **Available evidence:** One business-only transcript with no deployment, volume, or source/dest discussion.
- **Required behavior:** Refuse to generate a tech-qual doc and recommend `prep-call` or a technical discovery call.
- **Forbidden behavior:** Generating a technical qualification from thin air.
- **Expected output:** Refusal message, not a saved `tech-qual-*.md`.
- **Failure severity:** `blocker`.

#### Scenario 4: Salesforce information conflicts with the customer transcript

- **ID:** `phase1-sfdc-transcript-conflict`
- **Skills:** `deal-assessment`, `next-move` (if implemented), `biz-qual`
- **Customer constraints:** None.
- **Available evidence:** SFDC stage is `Closed-Won` with a close date next week; transcript shows the customer is still evaluating and has not identified an economic buyer.
- **Required behavior:** Flag the SFDC-vs-reality mismatch prominently; trust the transcript; lower confidence.
- **Forbidden behavior:** Using the SFDC stage as ground truth without flagging the conflict.
- **Expected sections:** `At a Glance`, `Confidence & Assumptions`, `Source Coverage`.
- **Failure severity:** `major`.

#### Scenario 5: `next-move` lacks sufficient evidence for a definitive recommendation

- **ID:** `phase1-next-move-low-evidence`
- **Skills:** `next-move`
- **Customer constraints:** None.
- **Available evidence:** One 14-day-old transcript; no biz-qual/tech-qual/deployment-qual.
- **Required behavior:** Acknowledge shallow read contract, recommend low-risk next step (`prep-call`, `account-refresher`, or `biz-qual` gathering), low confidence.
- **Forbidden behavior:** Recommending `poc-plan` or `roi-business-case` with high confidence.
- **Expected sections:** `At a Glance`, `Current read`, `Ranked Next Moves`, `Source Coverage`.
- **Failure severity:** `major`.

#### Scenario 6: Deployment recommendation depends on an unverified entitlement

- **ID:** `phase1-unverified-entitlement`
- **Skills:** `deployment-model-qual`, `tech-qual`, `poc-plan`
- **Customer constraints:** Hard requirement for customer-managed KMS / BYOK.
- **Available evidence:** Transcript states BYOK requirement; `airbyte-platform` repo not available.
- **Required behavior:** If BYOK is hard, verdict is `park` / no fit. If repo is unavailable, mark entitlement claim as "verify with team" and cap confidence.
- **Forbidden behavior:** Asserting a `Flex` or `Cloud` fit that contradicts BYOK; asserting an entitlement from memory.
- **Expected sections:** `At a Glance`, `Security & Compliance`, `Source Coverage`.
- **Failure severity:** `blocker`.

---

## 9. Phase 1B: real skill-output evaluation (implemented)

Phase 1B extends the Phase 1 framework so existing manifests can be executed against the actual skills in an isolated, synthetic workspace.

### 9.1 Delivered

- `eval/runner.py` CLI: `list`, `run`, `run-suite` subcommands.
- `WorkspaceBuilder` creates a temporary customer workspace with synthetic transcripts and `.se-config.yaml`.
- `_ClaudeHome` prepares an isolated `$HOME`, copies `skills/` into it, stubs external tools (`sf`, `gh`, `curl`, `wget`), and redirects `XDG_*` paths.
- `ClaudeExecutor` invokes `claude -p --bare --permission-mode acceptEdits --disallowed-tools ...` with `SE_WORKSPACE` set to the temp workspace.
- Categorized reporting: invocation, structural, business-invariants, semantic, warnings.
- Failed outputs are preserved in `eval/results/` and temp workspaces are cleaned by default (`--retain-workspace` to keep).
- Optional semantic evaluator scaffold uses `claude` as a judge with manifest rubrics, returning machine-readable results.
- Business-invariant checks strengthened for sync-frequency preservation, constraint removal, alternative scenarios, unverified connectors/entitlements, missing-input handling, SFDC conflict, and `next-move` evidence.

### 9.2 Execution modes

1. Deterministic: `uv run --extra dev pytest eval/ -v`
2. Single live scenario: `uv run python -m eval.runner run --manifest eval/manifests/phase1/hourly-sync-constraint.yaml --executor claude`
3. Suite: `uv run python -m eval.runner run-suite --manifest-dir eval/manifests/phase1 --executor claude`

### 9.3 Safety constraints

- Temp `HOME` and `SE_WORKSPACE`; real `~/.se-skills` / `~/airbyte-work` are never used.
- External CLI tools are stubbed; dangerous `git`/network commands are disallowed.
- `salesforce.enabled: false` in synthetic config.
- Skills are copied into the temp home, not symlinked, to avoid sandbox escape.
- Generated outputs go to `eval/results/` (gitignored); nothing is committed.

### 9.4 Acceptance criteria

- [x] `pytest eval/` still passes with no model access.
- [x] A single Phase 1 scenario runs end-to-end against the real `claude` CLI.
- [x] The hourly-sync-constraint scenario passes against real skill output.
- [x] Report separates structural, business-invariant, semantic, and invocation failures.
- [x] No real customer workspaces or data are touched.

### 9.5 Limitations and next work

- Semantic evaluator is a scaffold; it returns structured JSON but has not been run against all scenarios.
- `poc-plan` initially paused on missing `biz-qual`/`deployment-qual`; the runner now instructs the skill to skip missing upstream docs and produce with flags.
- Some manifest expected-section names were refined to match actual skill headings (`Scope` instead of `POC Scope`).
- `skills/roi-business-case/SKILL.md` already covers capacity-based pricing and loaded cost; it is not in the Phase 1B skill list but should be added to the Phase 2 evaluation suite if capacity sizing becomes a priority.
- Phase 1B does not implement golden-output regression diffs, CI, prompt versioning, or webapp changes — those remain Phase 2/Proposed.

---

## 10. Definition of done

A backlog item is complete only when:

- Acceptance criteria are met.
- Relevant tests are added and passing.
- No known regression to existing behavior is introduced.
- Documentation is updated (this plan, `README.md`, `MAINTAINING.md`, or skill `## Changelog` as appropriate).
- Security impact is reviewed for items touching input, output, or execution paths.
- This implementation plan is updated with status and any remaining limitations.
- For model-dependent changes, a small number of representative SE scenarios are validated and recorded in `eval/golden/` or the judge results.

A phase is complete when:

- All backlog items assigned to the phase are `Completed` or explicitly `Deferred`.
- A phase report is added to `CHANGELOG` of this plan.
- Exit criteria for the phase are met.
- The next phase has been approved before work begins.

---

## 10. Decision log

Record durable architecture and product decisions here. Only record decisions supported by the existing repository or explicit direction.

| Date | Decision | Context | Alternatives considered | Rationale | Consequences | Related IDs |
|---|---|---|---|---|---|---|
| 2026-07-14 | Use a local-only, Markdown-prompt skill architecture for the SE workflow. | Existing repo is a Claude Code skill suite with `SKILL.md` files and `install.sh` symlinks. | Rewrite as Python agent framework or hosted service. | The existing design is lightweight, fits the current team's workflow, and preserves human-readable prompts. | Slower to enforce behavior deterministically; requires strong testing. | ARCH-001, ARCH-005 |
| 2026-07-14 | Keep the FastAPI webapp as a thin wrapper, not the source of truth for skill logic. | `webapp/app.py` spawns `claude -p` and renders Markdown. | Move logic into the webapp. | Skills must also work headlessly in Claude Code; webapp is optional UI. | Webapp should not re-implement business rules; validation belongs in `eval/` and schemas. | STRUCT-001, ORCH-001 |
| 2026-07-14 | Build an `eval/` directory with synthetic fixtures as the first implementation. | No tests exist; highest risk is incorrect skill behavior. | Start with refactoring or Pydantic schemas. | You cannot refactor safely without tests; evaluation also informs schema design. | Phase 1 is test-only, no production code changes except minimal runner helpers. | EVAL-001, EVAL-006 |
| 2026-07-14 | Do not convert every skill to JSON/Pydantic outputs immediately. | Skills are currently Markdown-first. | Mandate sidecar JSON for all skills now. | Markdown is the primary SE-readable artifact; sidecars should be optional until proven valuable. | Structured validation starts with parsers and schemas, not a forced output migration. | STRUCT-001, STRUCT-003 |
| 2026-07-14 | Defer hosted authentication and encryption until a hosting decision is made. | App is local-only today. | Add auth now. | Scope control; local data handling is the current operating model. | A future hosted version will require a dedicated security phase. | SEC-006 |

---

## 11. Change log

| Date | Change | Author | Notes |
|---|---|---|---|
| 2026-07-14 | Created `IMPLEMENTATION-PLAN.md` | Devin | Phase 0 complete; all backlog items `Proposed`. |
| 2026-07-14 | Completed Phase 1: `eval/` framework, manifest schema, runner, 6 synthetic scenarios, and baseline report | Devin | All deterministic tests pass; model-dependent and real-skill invocation are optional. `skills/` and `webapp/` were not modified. |
| 2026-07-14 | Hardened Phase 1B runner and prerequisite handling | Devin | Added `eval/tests/test_runner.py`; `execution.prerequisite_mode` and `classification`; synthetic upstream fixtures for `hourly-sync-constraint`; fail-closed workspace isolation; explicit-override reporting. `skills/` and `webapp/` were not modified. |
| 2026-07-14 | Added deterministic CI for `eval/` and optional model-dependent workflow | Devin | Added `.github/workflows/eval-deterministic.yml` (PR/push to `main`) and `.github/workflows/eval-model-dependent.yml` (`workflow_dispatch` only). No Anthropic credentials or secrets in fast CI. `skills/` and `webapp/` were not modified. |
| 2026-07-14 | Completed Phase 1B: real-skill evaluation runner, isolated `claude` execution, categorized reporting, semantic evaluator scaffold | Devin | Hourly-sync-constraint and next-move-low-evidence validated against real `claude` output in isolated temp workspaces. `sfdc-transcript-conflict.yaml` gitignore fix staged. `skills/` and `webapp/` were not modified. |
| 2026-07-14 | Completed Phase 2 security quick wins (SOQL helpers, HTML/PDF sanitization, secret redaction, input boundaries, Claude permission review) | Devin | `webapp/soql.py`, `webapp/security.py`, `webapp/pdf_render.py`, `webapp/static/app.js`, `webapp/app.py` input models and redaction wiring. Tests in `eval/tests/test_webapp_*.py`. 78 passed, 1 skipped. `webapp/` docs and `app.js?v=` cache-bust updated. |
| 2026-07-14 | Closed UI/UX improvement phase and Phase 4 orchestration/feedback loop | Devin | Marked UX-001/002/004/006/007/008/009 and ORCH-001/002/003 Completed; fixed unvalidated output attention regression; recorded deferred UX ideas. |
| 2026-07-14 | Completed SEC-005: moved Anthropic API key from plaintext `~/.mcp/*.env` to OS keyring | Devin | `webapp/app.py` uses `keyring.get_password("se-skills", "ANTHROPIC_API_KEY")` with `ANTHROPIC_API_KEY` env fallback; `keyring` added to deps; tests in `eval/tests/test_webapp_security.py`; `webapp/README.md` updated. |
