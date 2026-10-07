# <Prospect> - Connector Builder Demo Working Session
**Date**: <date> | **Time**: <time> | **Duration**: <duration>

> Status banner: when was this last verified against the live Builder UI? Is there a tested
> fallback draft in the workspace, and what is it called?

---

## Context

- <Prospect>'s core technical question, in one sentence (this drives the whole session):
  > <question>
- Why we build against a stand-in API instead of their real environment.
- **The stand-in: <API>** (`<base url>`). Why it was chosen (not in catalog / matching protocol shape / free credentials / writable for the live moment).

### How <API> maps to <Prospect>'s pattern

| <Prospect> concept | <API> equivalent | Builder feature demonstrated |
|---|---|---|
| | | |

### The key insight (the answer to their core question)

<one paragraph: which Builder primitive answers their question and why>

---

## ⚠️ UI Changes and Gotchas (verify each rehearsal)

Start from the current list in the skill's `references/ui-gotchas.md`
(AI Assistant skip screen; no global base URL; auto-created auth input - keep the default;
Secret toggle breaks testing values; paste Jinja, never type; "Current Parent Key Value Identifier";
copy-stream toggle; capped test preview; password-manager interference; Inputs-page testing values).
Re-verify during rehearsal, prune what's fixed, add what's new.

---

## Feature coverage

| Feature they asked for | Where it's demonstrated | Live or reference |
|---|---|---|
| | | |

---

## Agenda at a Glance

| Time | Step | Topic | Feature shown |
|---|---|---|---|
| | | | |

---

## <API> Quick Reference

| Detail | Value |
|---|---|
| Endpoints | |
| Auth | |
| Pagination | |
| Response shape | |
| Rate limit | |
| Docs | |

---

## Pre-Session Checklist

- [ ] Workspace + Builder access
- [ ] Fallback draft (tested green) open in a second tab
- [ ] This doc open for paste-ready values
- [ ] Credentials/token ready (marked internal; regenerate after)
- [ ] Password-manager extension disabled for screenshare
- [ ] Dry run done end to end (including one Jinja paste)
- [ ] (Optional) Destination configured for live sync

> 🔑 Token (internal - regenerate after session): `<token>`

---

## STEP 1: Create Connector + First Stream Shell + Auth

1. Builder → New custom connector → Start from scratch → "Skip and start manually"
2. Rename connector (top-left chip) → `<name>`
3. Name the auto-created stream; set full API Endpoint URL
4. Authenticator → check → pick type → keep default config reference (auto-creates the input)
5. Inputs → gear → Secret OFF → Save changes
6. Inputs → enter testing value → Done → wait for "Draft saved"

Talking points: <auth story; open the Authenticator dropdown to show the full range>

---

## STEP 2: Parent Stream - <discovery + pagination>

1. Record Selector → Field Path: <value or empty>
2. Primary Key → Single Key → `<key>`
3. Paginator → strategy, page size, inject params
4. Test

Expected result (fill in after rehearsal): records count, columns, Request tab URL.

---

## STEP 3: Child Stream - <the headline>

1. + New stream → "Use an existing stream configuration" → copy from parent
2. PASTE URL: `<url with {{ stream_partition.<field> }}>`
3. Partition Router → Parent Stream / Parent Key / Current Parent Key Value Identifier
4. Test → narrate the partition selector + "Other Requests" panel

### 🎯 The money moment
<exact script for the live wow, rehearsed: create data → re-test → it appears with no config change>

---

## STEP 4+: <additional levels / features>

---

## STEP N-1: Capability tour for features the stand-in can't show

<reference manifest walkthrough: OAuth2, incremental, etc.>

---

## STEP N: Publish, Connect & Sync

1. (Optional) Flip Secret/Required back ON, last step only
2. Publish → Create Source → Test Connection → Create Connection → select streams → Sync Now

---

## Mapping back to <Prospect>'s real stack

| What we built | What it proves for their stack |
|---|---|
| | |

### The answer to their question
> <one sentence>

---

## Live Test Evidence (date)

| Item | Result |
|---|---|
| | |

---

## Next Steps (Post-Session)

| Step | When | Owner |
|---|---|---|
| Regenerate demo token | Right after | |
| Delete sandbox demo records | Right after | |
| | | |

---

## Source Coverage

| Source | Checked | What it contributed |
|---|---|---|
| Prior skill outputs (tech-qual, connector-feasibility, post-call, prep-call) | | |
| Transcripts / Gong | | |
| Salesforce opportunity fields | | |
| Stand-in API docs | | |
| Rehearsal in the Builder (date) | | |
