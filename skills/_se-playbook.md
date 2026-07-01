# SE Playbook — Frameworks & Tactics

This is the canonical reference for SE craft. All SE skills in `~/.claude/skills/` (prep-call, post-call, biz-qual, tech-qual, poc-plan, deal-assessment, connector-feasibility, deployment-model-qual, follow-up-email, objection-handler, internal-prep, next-move, account-refresher) reference this document. When a skill fires, it should read the relevant section here and apply the tactics.

**Not a skill itself** — the leading underscore prevents auto-trigger. Read it on demand from other skills.

---

## 1. MEDDPICC — Qualification Backbone

**Use to:** score deal health, find missing intel, decide whether to invest more time.
**Stage:** Mid-discovery → proposal. Run as a deal-review rubric every 2 weeks.

**The letters:**
- **M**etrics — quantified business value
- **E**conomic Buyer — person who can write the check
- **D**ecision Criteria — what they're evaluating against
- **D**ecision Process — how the decision actually gets made
- **P**aper Process — legal/procurement/security steps
- **I**dentify Pain — the real cost of the current state
- **C**hampion — internal seller who pushes for you
- **C**ompetition — alternatives, including build-vs-buy

**Tactical moves:**

- **Metrics — anchor in their own numbers.** Don't ask "what are your KPIs"; ask "If we cut your data engineering team's pipeline maintenance time by 50%, what does that unlock — dollars or headcount?" Get a number on a slide. Echo it back in every future meeting.
- **Economic Buyer — confirm by behavior, not title.** Common trap: treating a director as the EB. Ask the champion: "Walk me through the last time your team bought a tool over $200K — who signed?" If they hesitate, you haven't found the EB.
- **Paper Process — offer a hypothesis, don't ask cold.** "What's your paper process?" gets shrugs. Try: "Typically at a company your size we see InfoSec review, then a 30-day legal redline cycle, then procurement. Who runs each on your side?"
- **Champion — test, don't assume.** A real champion (a) takes meetings on short notice, (b) shares internal context unprompted, (c) gives you bad news. If they only relay messages, they're a coach, not a champion.
- **Competition — ask who they've ruled out.** "Who else is on your shortlist — and who did you take off the list and why?" The "off the list" answer tells you their real decision criteria.

---

## 2. SPIN Selling — The Discovery Engine

**Use to:** structure discovery so the customer pitches your product back to you.
**Stage:** Discovery and pre-demo. SPIN's job is to make the demo land.

**The four question types:**
- **S**ituation — context (use sparingly; do your homework first)
- **P**roblem — pain points
- **I**mplication — cost/consequence of the problem
- **N**eed-Payoff — value of solving it

**Tactical moves:**

- **Skip Situation when you can.** Read the 10-K, docs, LinkedIn first. "Tell me about your data stack" burns trust. Open with a hypothesis: "From your job posts you're hiring three data engineers — I'm guessing pipeline maintenance is eating your existing team. Fair?"
- **The Problem → Implication move (the one most SEs skip).** When customer says "reporting is slow," weak reps demo. SPIN move: stack implications. "How long does it take?" → "10 hours/week." → "That's 520 hours/year. At a loaded data engineer rate, $80K of capacity. What would your team ship instead with that time back?"
- **Pre-stage three Implication questions per known problem.** For data infra: cost of stale dashboards, cost of analyst hours rebuilding broken pipelines, cost of a missed compliance audit. Land at least one before demoing.
- **Need-Payoff in the buyer's language.** Don't say "imagine if you could." Ask: "If you didn't have to maintain custom Salesforce-to-Snowflake code anymore, what would your team work on?" The buyer pitches your product back to you.

---

## 3. Sandler — Disqualification and Control

**Use to:** earn the right to ask hard questions, force real answers, kill bad deals fast.
**Stage:** Early qualification, anytime a deal is drifting.

**Tactical moves:**

- **Upfront contract on every call.** Open: "We've got 45 minutes. I want to ask about your current pipeline pain and your evaluation criteria. You'll probably want to see how we handle CDC and schema evolution. At the end we should be able to decide together whether a POC makes sense — or whether this isn't a fit. Fair?" Pre-licenses you to ask hard questions and hear "no."
- **Pain funnel — go three layers deep.** "Pipelines break" is surface. Funnel: Tell me more → For example? → How long has this been a problem? → What have you tried? → What did that cost you? → How do you feel about it personally? The last question is where the budget lives.
- **Negative reverse selling on stalled deals.** When a champion goes dark: "Based on the last three reschedules, it sounds like this probably isn't a priority right now — should I close the file?" Forces a real answer.
- **No spilling candy in the lobby.** Don't show your best feature in the first 10 minutes because they asked. Trade: "Happy to show you schema evolution — first, can you tell me how often your source schemas change today and what breaks when they do?"
- **Reversing — answer questions with questions.** Customer: "Do you support OAuth?" Weak: "Yes, here's how." Sandler: "We do — what's driving the question? Is there a specific auth requirement from your security team?" Reveals the real evaluation criterion.

---

## 4. Challenger Sale — Teach, Tailor, Take Control

**Use to:** earn second meetings, expand deals, lead with insight not feature.
**Stage:** First substantive meeting (Reframe), exec readout (Rational Drowning).

**Commercial Teaching structure:** Warmer → Reframe → Rational Drowning → Emotional Impact → A New Way → Your Solution

**Tactical moves:**

- **Lead with a Reframe, not a discovery question.** "Most data teams think their cost problem is warehouse spend. Data shows 60-70% of actual cost is engineering time maintaining custom connectors that break every quarter — invisible because it's salary, not SaaS." Counterintuitive, data-backed, reframes what they thought they were buying.
- **Rational Drowning — three pieces of evidence, not one.** Benchmark from peer customers, an industry data point, and a cost model they can fill in. The point isn't to convince — it's to make their current path feel riskier than change.
- **Tailor per persona, same deal.** CDO: speed-to-insight + team productivity. CFO: predictable per-connector cost vs. unpredictable maintenance. Security: SOC2, deployment models, data residency. Three different one-pagers, not one.
- **Take Control = constructive tension on next steps.** When customer says "send pricing and we'll get back to you": "I can send pricing, but deals that go to pricing before InfoSec review stall for 6 weeks. Can we line up the security call first?" You're protecting their timeline.
- **Challenge vs. listen.** Listen during pain discovery. Challenge when they state a belief about their business you have data to refute. Never challenge feelings — only assumptions.

---

## 5. Chris Voss / Never Split the Difference — Objections & Tough Moments

**Use to:** defuse objections, surface real blockers, handle negotiations.
**Stage:** Objection handling, mid-cycle stalls, security/legal pushback, close.

**Tactical moves:**

- **Mirror to draw out objections.** Customer: "We're worried about vendor lock-in." SE: "Vendor lock-in?" (silence). They keep talking and tell you what they actually mean — often something specific like "we got burned by Informatica's pricing model" — which you can now address.
- **Label, don't argue.** Instead of "that's not true, we're open source," try: "It seems like you've been burned before by a vendor who promised flexibility and didn't deliver." Customer relaxes; you can have the real conversation.
- **Calibrated questions to shift the burden.** When pushed on price: "How am I supposed to get my product team to discount 30% when we've already shown 5x ROI in the model you reviewed?" Forces them to either justify or back off.
- **Get to "no" early.** "Is now a bad time to talk pricing?" feels backwards but works — people protect "no" more than they grant "yes." Real "no" = stable platform; fake "yes" = stalled deal.
- **Accusations Audit before a hard meeting.** Open with: "You're probably thinking we're going to push you to a bigger contract, that we haven't fixed the issues from last quarter, and that this call is more for us than for you." Pre-empting their objections defuses them. Deliver in the slow, low **late-night FM DJ voice** — signals calm authority.

---

## When to Use Which Framework

| Stage | Primary framework | Why |
|---|---|---|
| Pre-call research | Challenger (Reframe prep) | Need a point of view before you arrive |
| First discovery | SPIN + Sandler upfront contract | Earn the right to ask hard questions, then ask them |
| Qualification | MEDDPICC | Score the deal honestly; find the gaps |
| Demo / technical deep-dive | SPIN Need-Payoff + Challenger Tailor | Every feature ties back to stated pain, per persona |
| POC | MEDDPICC (Decision Criteria, Competition) + Sandler upfront contract per milestone | Define "success" in writing before kickoff |
| Objections / mid-cycle stall | Voss + Sandler negative reverse | Surface the real blocker |
| Negotiation / close | Voss (calibrated questions, accusations audit) + MEDDPICC (Paper Process) | Control tone; remove paper-process surprises |

---

## Question Taxonomy

Different question types do different work. Mixing them up is a top SE mistake.

- **Discovery questions** — open-ended, exploratory. Goal: understand the world. "Walk me through how you currently move data from Salesforce to your warehouse."
- **Diagnostic questions** — narrower, pain-finding. Goal: surface specific cost. "When that pipeline broke last month, who got paged and how long was it down?"
- **Confirmation questions** — closed, restate-and-verify. Goal: make sure you heard right and bank a commitment. "So if we can deliver Workday + NetSuite + Snowflake on a 60-day POC with sub-15-min latency, that hits your bar for going to procurement?"
- **Closing questions** — directive, force a next step. Goal: move the deal. "What needs to be true between now and Friday for you to commit to the POC scope?"

If every question you ask is discovery, you're not a salesperson — you're a research assistant.

---

## Top SE Anti-Patterns to Avoid

- **Happy ears.** Hearing "this looks great" and forecasting Commit. Cure: write down the next concrete step with a date and a name. If you can't, the deal isn't where you think it is.
- **Feature dumping in the demo.** Demo is not a tour. Distill discovery into 3-4 use cases tied to stated pain. Everything else lives in a follow-up doc.
- **Solution-pitching before earning it.** If you've shown product before the customer can articulate their own problem in their own words, you've skipped SPIN's Implication step. Deal will stall at procurement.
- **Weak MEDDPICC questions.** "What's your decision process?" gets shrugs. Offer a hypothesis based on similar deals and let them correct you.
- **Never getting to "no."** A pipeline of "maybes" is a pipeline of stalled deals. Use Sandler negative reverse and Voss calibrated questions.
- **Weak next-steps.** "I'll follow up next week" is not a next step. "Tuesday 2pm, 30 min, you + your security lead + our solutions architect, agenda: encryption-at-rest and network egress" is a next step. End every call this way.
- **Not tailoring to persona.** Same deck for CDO and CFO. #1 expansion killer in enterprise deals.

---

## Skill Sequencing Rules

The SE skills are designed to compose in a specific order. Skipping ahead produces low-value output.

### Hard prerequisite: call data before qualification

**`biz-qual` and `deal-assessment` require at least one customer transcript.** They synthesize what the customer said — without customer voice, you're producing hypotheses dressed up as analysis.

For a brand-new prospect with **zero transcripts**:
- ✅ Run `prep-call` to plan the first call
- ❌ Do NOT run `biz-qual` (no MEDDPICC data to score against)
- ❌ Do NOT run `deal-assessment` (no health to assess)

After the first call:
- Save the transcript to `01-customers/_transcripts/<Customer>-MM.DD.YY.txt`
- THEN `biz-qual` and `deal-assessment` become meaningful

### Standard workflow order

```
NEW PROSPECT
  ↓
[ AE does initial business discovery call — happens in Gong ]
  ↓
prep-call (default mode: SE prepping for tech call,
            inherits AE's discovery, goes deeper)
  ↓
[ SE tech call happens, transcript saved ]
  ↓
post-call (summarize the call)
  ↓
biz-qual (MEDDPICC scoring with real data)
  ↓
deployment-model-qual (gate before tech)
  ↓
tech-qual + connector-feasibility
  ↓
poc-plan
  ↓
[ ongoing: deal-assessment every 2 weeks, follow-up-email and objection-handler as needed ]
```

**Cold prep mode (rare):** If no AE call exists yet, prep-call runs on pure web research. Output explicitly flags "cold-prep mode" so Gary knows the AE handoff hasn't happened.

### Internal vs. customer-facing skills

The customer-facing chain above produces docs for or about the customer. Separately, **`internal-prep`** handles internal meetings (AE syncs, forecasts, exec readouts, deal reviews). It reads the same artifacts but produces differently-structured output for Gary's Airbyte colleagues, not for customers.

`internal-prep` is invoked independently — it doesn't fit linearly into the customer-facing chain. Use whenever an internal meeting is upcoming.

### Why this ordering

- A `biz-qual` with no customer voice is just SE hypotheses. Worse than no doc — it can mislead later decisions.
- A `deal-assessment` without qualification data is wishful forecasting.
- `prep-call` is intentionally the only skill that runs on pure research, because its job is to make the first call valuable — not to make claims about the deal.

### When skills should refuse to run

| Skill | Refuse if |
|---|---|
| `biz-qual` | 0 transcripts (local or Gong) |
| `deal-assessment` | 0 transcripts AND 0 prior qualification docs |
| `tech-qual` | 0 transcripts containing technical discovery |
| `poc-plan` | 0 transcripts AND no biz-qual / tech-qual |
| `deployment-model-qual` | 0 transcripts (the 5 questions need customer answers) |

In each case, the right output is: explain why the skill can't run, and recommend the upstream skill that should run first.

---

## SE Identity (Multi-User Support)

Skills are designed to be shared across an SE team. To identify which SE is running them, all skills should read the config file:

**Path:** `~/airbyte-work/.se-config.yaml`

**Contains:** `name`, `email`, `slack_handle`, `role`, `aliases` (nicknames for transcript matching), `ae_pairings` (typical AE collaborators).

### Usage rules

- **Replace `[SE name]` placeholder** in any output with the config's `name` field
- **Email signatures** in `follow-up-email` use the config's `name`
- **Audience attribution** (was the SE on this call?) — see "Call Attribution" section below
- **File ownership** — saved outputs should reference the SE who ran the skill

### If the config doesn't exist
Fall back to asking the user: "I don't see `~/airbyte-work/.se-config.yaml`. Who are you running this as? (name + role)". Suggest they create the config so future skills don't ask again.

---

## Call Attribution (Was the SE on This Call?)

For skills that process transcripts (`post-call`, especially the coaching layer), the audience for coaching observations depends on who was actually on the call.

### How to detect

1. Read the SE's `name` + `aliases` from `~/airbyte-work/.se-config.yaml`
2. Scan the transcript for any of those names appearing as a speaker label or mentioned in introductions
3. Cross-reference with the `ae_pairings` list — if an AE name appears but the SE name doesn't, this is an AE-led call

### Implications

| Result | Coaching audience | Output framing |
|--------|-------------------|----------------|
| SE name found in transcript | The SE was on the call | Coaching is for the SE personally — "what to do differently next time" |
| Only AE name found, no SE | AE-led call (typical for first discovery) | Coaching is "context to share with the AE" or "things to address in your next call" — not a direct critique of the SE |
| Neither found | Unknown attribution | Flag as "attribution unclear — coaching frame omitted; review with care" |

### Why this matters

If Gary's `post-call` skill critiques the discovery on an AE-led call as if Gary did the discovery, the output is wrong — Gary wasn't there. Coaching observations need the right audience or they're noise.

---

## Output Persistence (Auto-Save)

Skills that produce working artifacts (call prep, qualification docs, deal assessments, POC plans, summaries) should **auto-save outputs by default** rather than asking each time.

### Exemptions

**`next-move` is exempt.** Router output is ephemeral — it's a "what to do right now" diagnostic that goes stale within hours of being acted on. Saving it just creates noise in `outputs/workflow-status/` with no future reader. Router default = chat output only; saves only on explicit user request.

All other SE skills follow the auto-save rule.

### Folder structure

All customer-specific outputs are saved under:
```
~/airbyte-work/01-customers/<Customer>/outputs/<skill-name>/
```

Standard subfolders (created on first save):
- `call-prep/` — prep-call outputs
- `post-call/` — post-call summaries
- `biz-qual/` — MEDDPICC scorecards
- `tech-qual/` — technical qualification
- `deployment-qual/` — deployment-model verdicts
- `connector-feasibility/` — coverage analyses
- `poc-plan/` — POC plans
- `deal-assessment/` — deal health assessments
- `emails/` — drafted follow-up emails
- `workflow-status/` — router outputs (when saved)

Manual artifacts (technical docs, guides, raw notes) go in `~/airbyte-work/01-customers/<Customer>/raw/`.
Transcripts continue to live in `~/airbyte-work/01-customers/_transcripts/`.

### Filename format

**`<skill-type>-<YYYY-MM-DD>-<Descriptor>[-vN].md`**

Examples:
- `call-prep-2026-05-28-Tech-Discovery.md`
- `post-call-2026-05-28-Discovery.md`
- `deal-assessment-2026-05-28.md`
- `deal-assessment-2026-05-28-v2.md` (when re-run same day)
- `biz-qual-2026-05-28-Post-Tech-Call.md`

Rules:
- Date prefix (`YYYY-MM-DD`) so files sort chronologically — **always keep this numeric format in filenames** (do NOT use "June 11" in a filename; it breaks chronological sort)
- **Descriptor is Title Case**, hyphen-separated, no spaces. Capitalize the first letter of each word; keep short articles/conjunctions/prepositions lowercase (`a`, `an`, `the`, `and`, `or`, `of`, `for`, `to`, `vs`, `w`). E.g. `Pro-Upsell`, `Tech-Discovery`, `Migration-of-the-Pipeline`.
- **Keep the descriptor to a single concept** — prefer `Pro-Upsell` or `Expansion` over awkward two-topic combos like `Intro-Expansion`. Pick the call/output's core substance.
- Version suffix (`-v2`, `-v3`) only when explicitly re-running with new context the same day

### Date format inside documents

In the **document body** (titles, headers, and prose — anything the user reads), write dates in long form: **`June 11, 2026`**, not `2026-06-11`.
- This applies to every skill's output (the H1/title line especially).
- Exception: leave dates that are *data fields* in their native format when that's clearer — e.g. an SFDC `CloseDate`, a filename reference, or a transcript ID. The rule targets human-facing title/header/prose dates, not raw data values.

### Suppress auto-save

User can pass `--no-save` to any skill invocation to suppress file write. Output still goes to chat.

### Multi-version handling

When a same-day file already exists with the same descriptor, append `-v2`, `-v3`, etc. Don't overwrite — let the user compare versions and clean up manually.

---

## Output Document Format

Every saving skill produces a markdown document that is read both in the raw `.md` and in the Solutions Team Hub web app (which renders an auto-generated index sidebar, colored callouts, and key-number highlighting). Follow this shared structure so outputs are **sectionalized, scannable, and consistent**.

### Top-of-document structure (in this order)

```markdown
# <Customer> — <Skill Title>: <one-line verdict/descriptor>
**Date:** June 18, 2026 · **Stage:** <stage> · **<key>:** <value>   ← ONE line, `·`-separated, long-form date

### At a Glance
- **Verdict:** <one line — wrap the headline figure in ==…==>
- **<Label>:** <value> · **<Label>:** ==<key number>== · **<Label>:** <value>
- **Top <blocker/risk/gap>:** <one line>

**Jump to:** [At a Glance](#at-a-glance) · [<Section>](#slug) · … · [Source Coverage](#source-coverage)   ← Source Coverage is LAST

## <First body section>   ← lead with the decision content, not the audit trail
### <subsection>
…

## Source Coverage   ← the LAST content section (audit trail; progressive disclosure)
…(anti-hallucination block — see Source Coverage Transparency)…
```

**Source Coverage goes at the BOTTOM.** It is audit/evidence, not the lead — the reader wants the answer first, the trail last. Put the one-line "Source confidence" summary in the At-a-Glance decision card; place the full file list as the final content section (the web app also collapses audit sections, so low placement + collapse both defer it). This applies to **all** saving skills. (Older docs that still put it near the top render fine — no need to retrofit.)

- **The meta line under the H1 is ONE line.** Put the 2–4 most-scannable facts (date, stage, deal size, SE) on a single line joined by ` · `. **Never stack multiple `**Label:**` lines as separate paragraphs** — in markdown, adjacent lines with no blank line between them collapse into one flowing paragraph, and the web app renders that as an unreadable run-on blob. Everything else (attendees, contacts, meeting type, prerequisites, durations) belongs in the **At a Glance** list below, NOT in the header. If a fact needs its own row, make it an At-a-Glance bullet — a list item, not a loose paragraph.
- **At a Glance** is a short labeled key/value list (3–6 lines) — the single most decision-relevant facts. It is NOT a table or a card; just bold `**Label:**` pairs, each as its own `- ` bullet (list items render as discrete rows; loose lines do not). Don't repeat the header's facts here — the meta line and At-a-Glance are complementary, not duplicative.
- **Jump to** is a one-line list of links to the document's `##` sections. The web app also auto-builds a sidebar from the headings, but the inline Jump-to keeps the raw `.md` navigable. Anchor slugs are lowercase, non-alphanumeric → `-` (e.g. `## Fit Verdict` → `#fit-verdict`).

### Decision-First Layout (analytical skills)

The **analytical skills** — `tech-qual`, `biz-qual`, `deal-assessment`, `deployment-model-qual`, `connector-feasibility`, `poc-plan` — produce reports a busy SE or leader reads to make a call. Structure them so the doc answers, **in this order**: *(1) Should we proceed? (2) Why? (3) What could block us? (4) What do we do next? (5) What's the evidence?* The reader should get the answer in ~10 seconds and only descend for detail. This is a layer ON TOP of the top-of-document structure above — it does not replace each skill's signature sections, it standardizes the **head** and the **high-value tables**.

**1. Decision Card.** The `At a Glance` block for an analytical skill IS the decision card — lead with the judgment, not metadata. Use these labels (adapt wording per skill; omit lines that don't apply):
```markdown
### At a Glance
- **Verdict / Fit:** <🟢/🟡/🔴 pill + 3–6 word headline>
- **Recommended motion:** <the one next move — e.g. "Proceed to CDK workshop">
- **Primary risk:** <the single biggest thing that could blow itup — one line>
- **Confidence:** <low / medium / medium-high / high — and what it's pending on>
- **Next gate:** <the concrete checkpoint that resolves the risk>
- **Source confidence:** <one line — N transcripts + SFDC + docs; "see Source Coverage">
```
Keep it 4–6 lines. The headline figure still gets `==…==` (one, maybe two — the verdict band or coverage count, not every line).

**2. Scorecard.** Each analytical skill has a status table (tech-qual Fit Summary, biz-qual MEDDPICC, connector-feasibility Fit Verdict, deployment-qual Five Questions). Standardize it to a scannable shape with a **"Why it matters"** column so the reader sees significance, not just a color:
```markdown
| Area | Status | Why it matters |
|------|--------|----------------|
| <dimension> | 🟢 / 🟡 / 🔴 / ⬜ | <one short line — the consequence, not a restatement> |
```
Status legend stays 🟢 strong/viable · 🟡 needs validation/caveat · 🔴 weak/blocker · ⬜ unknown.

**3. Facts vs. judgment vs. recommendation.** Make clear what is **proven** (stated in a source — cite it), what is **inferred** (your read — say so), and what is **recommended** (your advice). Don't blend them in one undifferentiated paragraph. This is a principle, not a rigid 3-section split — most skills already separate evidence (Source Coverage, scorecard) from judgment (verdict, risks) from action (next steps). When you state something the customer did NOT say, label it: *"(inferred — not stated)"*. Ties to the anti-hallucination rules below.

**4. Open Questions / Next Actions as decision tables.** These high-value sections are tables, not loose checklists, so the reader sees ownership and impact at a glance:
```markdown
## Open Questions / Questions Still Needed
| Open Question | Owner | Needed By | Why it matters | Status |
|---------------|-------|-----------|----------------|--------|
| <question> | <name or **TBD**> | <date/gate or **TBD**> | <decision it unblocks> | Open |

## Recommended Next Actions
| # | Next Action | Goal | Success criteria | Fallback | Owner |
|---|-------------|------|------------------|----------|-------|
| 1 | <action> | <what it proves> | <what "done" looks like> | <plan B> | <name or **TBD**> |
```
**Owner / Needed-By guardrail:** if the source does not state an owner or a due date, render **`TBD`** (or `—`) — **never invent a name or a date.** Where the data exists, use it and cite the source. A fabricated owner is worse than a blank one. This is a hard rule (see anti-hallucination below).

**5. Progressive disclosure.** `Source Coverage` is audit material — keep the one-line "Source confidence" summary up in the Decision Card and put the detailed file list in a `## Source Coverage` section that is the **last content section** of the doc (see the top-of-document structure above). The web app collapses audit sections (Source Coverage, Activity, MEDDPICC, Coaching) by default AND bottom placement keeps them out of the way — both defer the trail. Don't open the report with a wall of file paths.

**6. Translate jargon in user-facing prose.** In the narrative/decision sections, write skill and artifact names as prose — "no prior deployment qualification exists" not "no `deployment-qual` exists". Reserve raw tokens (`connector-feasibility`, filenames, IDs) for `Source Coverage` and audit lines. Inline `code` formatting in the main narrative makes the doc read developer-heavy.

### Heading rule (required for the index to work)

- **Every top-level section is an H2 (`##`).** The web app sidebar and the Jump-to index only anchor headings. Never skip from H1 straight to H3 for a primary section.
- Use **H3 (`###`)** for sub-parts. `At a Glance` is an H3 (it sits under the title block, not a navigable section).
- Don't go deeper than H3 in output.

### Callouts (verdicts, risks, blockers, questions)

Use GitHub-style admonition syntax on a blockquote. The web app renders each as a colored box; in raw markdown it reads as a labeled quote. Reserve callouts for genuinely decision-relevant moments — a verdict, a real risk, a blocker, or the key questions to ask. **Don't wrap ordinary prose in callouts.**

```markdown
> [!verdict] <title>     ← green — go / viable / fully-validated / probability ≥60%
> body line(s)…

> [!risk] <title>        ← amber — caution / needs-confirmation / SFDC-vs-reality / band 20–60%
> [!blocker] <title>     ← red — no-go / deal-killer / hard blocker / band <20%
> [!info] <title>        ← blue — questions to ask / neutral key context
```

- First `>` line is `[!type]` + an optional bold title; following `>` lines are the body.
- A plain `> quote` with no `[!type]` marker stays an ordinary blockquote — unchanged.

### Key-number emphasis

Wrap genuinely decision-relevant figures in `==…==` — the web app renders them bold + highlighted.

```markdown
Probability **==40–60%==**, deal size ==$88K==, latency ==13h → 15min==, ==31 days silent==.
```

- **Cap: 3–6 highlights per document.** Highlight the deal amount, the before→after figure, the probability band, days-silent, close date, coverage count — the numbers a reader scans for. **If everything is highlighted, nothing stands out.** Do not highlight every number, label, or date.
- **Highlight numbers and short tokens, NOT sentences.** `==…==` is for a figure or a 1–4 word token (`==$45K==`, `==40–60%==`, `==1 data worker==`). Never wrap a whole clause or sentence — a highlighted phrase reads as a loud marker swipe, not emphasis. If a sentence is important, lead with it or use a callout; don't paint it.

### Exemptions

- **`follow-up-email` is fully exempt** — it's a customer-facing email, not a report. No At-a-Glance, no Jump-to, no TOC headings, no callouts, no Decision-First layout. Keep its existing email structure.
- **`next-move`, `objection-handler`, and `account-refresher` are light-touch** — already short and scannable. They may use a callout for the recommended next move / severity, and account-refresher's "10-Second Version" already serves as a decision card, but the full Decision-First layout (scorecard, decision tables) is optional, not required.
- **`internal-prep` adopts the Decision Card concept lightly** — its four sub-templates (ae-sync, forecast, exec-readout, deal-review) already have tight At-a-Glance blocks; align their labels to the decision-card spirit (lead with the judgment/ask) but they keep their own per-type section structure.
- **The six analytical skills** (`tech-qual`, `biz-qual`, `deal-assessment`, `deployment-model-qual`, `connector-feasibility`, `poc-plan`) **fully adopt** the Decision-First Layout above.

### Backward compatibility

The web app degrades gracefully: documents predating this contract still get an auto-generated sidebar from whatever headings they have; absent callouts/`==…==` simply render as normal text. No need to retrofit old outputs.

---

## Source Coverage Transparency (Anti-Hallucination)

When a skill processes large source material (transcripts, multi-doc synthesis), it must report what it actually read.

### The rule

**Every skill that synthesizes from external sources should include a "Source Coverage" section near the top of its output**, stating:
- Which files were read
- For long transcripts: line count read / total line count
- For multi-doc synthesis: list of files actually opened
- Any source that was inventoried but not read in full (e.g., "Inventoried 4 transcripts; read 03.31 and 04.01 in full, summarized older two")

### Why this matters

The model can produce plausible-looking summaries from partial reads, and the user can't tell. Forcing explicit source coverage:
1. Creates self-pressure to actually read sources
2. Lets the user verify the work
3. Makes hallucination risk visible

### Example

> **Source Coverage:**
> - Read in full: `Acme-04.01.26.txt` (566 lines), `Acme-03.31.26.txt` (1247 lines)
> - Skimmed for key topics: `Acme-03.25.26.txt` (489 lines)
> - Inventoried but not read: `Acme 03.24.26.rtf` (RTF — content extracted as plaintext)
> - Local notes read: `2026-03-11_ssl_certificate_error_diagnostic.md`
> - Memory: `project_acme_flex_ca_cert.md` (last updated 04.09.26)

If a skill claims to do thorough work but reads only part of a source, this section will reveal it.

---

## Memory Check (Active Project Context)

Before synthesizing for a customer, check the persistent memory directory at `~/.claude/projects/-Users-gary-yang-airbyte-work/memory/` for any project memories matching this customer. These often hold critical context not in transcripts:
- Active blockers (e.g., the Acme 403 secret storage issue)
- Stakeholder dynamics
- Decisions made between calls
- Status of pending Airbyte-side actions

Read `MEMORY.md` for the index, then read any customer-relevant memory files in full. Treat memories as point-in-time observations — if a memory is >30 days old, verify against current transcripts and notes before asserting as fact.

When a memory contains a critical claim that affects the skill's output (e.g., "POC paused pending X"), cite it inline by filename and date.

---

## Source Freshness Check (Gong Fallback)

Before synthesizing across transcripts and notes, confirm the local sources are current. The default behavior depends on the skill — see table below. Always follow Gary's CLAUDE.md Gong workflow when pulling:

1. Check `~/airbyte-work/01-customers/_transcripts/` first — never call Gong if the call is already local
2. Use `search_calls` with date + account filters — never bulk-pull
3. Fetch via `gong://calls/{callId}/transcript`
4. **Save the pulled transcript immediately** to `_transcripts/<Customer-Name>-MM.DD.YY.txt` before using it
5. Note in the output which sources were freshly pulled vs. pre-existing — Gary should know what's new

### Per-skill defaults

The most recent call is crucial context for almost every SE skill. The defaults reflect that.

| Skill | Default behavior |
|---|---|
| `prep-call` | **Always check Gong for the most recent call on this account.** The typical SE invocation is prepping for a tech call AFTER the AE did business discovery 1-14 days ago — that AE call is in Gong and is the primary input. **Lookback: 7 days first, expand to 14 days if empty.** If still no AE call found, declare "cold-prep mode" explicitly in output. For existing customers with local folders, also check local `_transcripts/` first. Pull the *most recent* call only, save it, and read it before generating prep. |
| `post-call` | Check Gong if the user references a specific call that isn't local. |
| `deal-assessment`, `biz-qual`, `tech-qual`, `deployment-model-qual`, `poc-plan`, `connector-feasibility`, `follow-up-email` | Check Gong if the most-recent local transcript is more than **14 days old**, OR if the user signals "check for new activity." Pull the most recent call only. |
| `objection-handler` | If the user provides a customer name (not just an abstract objection), check both local transcripts and Gong (14-day rule). Otherwise skip — abstract objections don't need customer call history. |

### When you don't find a match
If you can't find a Gong call for the customer, say so explicitly in the output — don't silently fall back to stale local data. "Checked Gong, no calls found in the last X days" is a finding worth reporting.

### Don't re-pull Gong if a recent pull happened (session dedupe)

Within a single workflow session, multiple skills may each independently want to check Gong for the same customer. To avoid redundant API calls:

**Before calling Gong, run this exact check and report the result in the output:**
```bash
find ~/airbyte-work/01-customers/_transcripts/ -iname "<Customer>*" -mmin -30
```

### Required: log the dedupe check result

Every skill that hits Gong must include this line in its output (in the Source Coverage section):

> **Session dedupe check:** Ran `find _transcripts/ -iname "Contoso*" -mmin -30` → [found `Contoso-2026-05-28.txt` saved 4 min ago, using it / no recent files, proceeding with Gong query]

This makes the dedupe visible and lets the user verify it's working.

### Decision logic

- **If recent file found** (≤ 30 min old): use it as the Gong result, skip the API call. Log "Skipping Gong — using transcript saved X min ago."
- **If no recent file**: proceed with the normal Gong check per the skill's lookback rule.

### Why mtime, not filename date

The filename embeds the *call date*; mtime captures the *save date*. A Gong call from April pulled today has filename `Customer-04.15.26.txt` but mtime = today. Dedupe needs save-time, not call-time.

---

## Salesforce Enrichment (Shared Machinery)

Several skills enrich their analysis with Salesforce CRM data. This section is the canonical "how" — individual skills reference it and add only their skill-specific field list.

### Connection

- **Tool:** `mcp__salesforce__run_soql_query` (read-only SOQL)
- **Org alias + query directory:** read from `~/airbyte-work/.se-config.yaml` under `salesforce:` (`org_alias`, `query_directory`). Default org alias `airbyte-prod`; default directory `~/airbyte-work`.
- **Enabled flag:** if `salesforce.enabled: false` in the config, or the config/org isn't available, **skip SFDC enrichment entirely and run the skill exactly as it would without CRM data.** SFDC is additive, never required. Note in output: "Salesforce enrichment: skipped (not configured)."

### Finding the right Opportunity (matching rule)

A customer often has multiple opportunities (renewals, prior losses, the active deal). To pick "the deal":

1. Query all opportunities for the account: `WHERE Account.Name LIKE '%<Customer>%'`
2. **Selection priority:**
   - Most recent **open** opportunity (`IsClosed = false`), **excluding renewals** (`Type != 'Renewal'`)
   - If the only open opp is a renewal, use it (a renewal IS the deal in that case)
   - If no open opps, use the most recently closed opp for context
3. Everything else on the account is **account history/arc** — not "the deal" but context.

Why exclude renewals by default: a renewal is a different sales motion (the AM owns it, the relationship exists, the qualification is different). When an SE is pulled in, it's usually for new/expansion business. A renewal only becomes "the deal" if it's the only thing open.

### Account Arc (for deal-assessment + biz-qual only)

These two skills read the **full account history**, not just the active opp:
- All opportunities: name, stage, amount, close date, type — the relationship arc
- **Prior losses:** `Closed_Reason__c`, `Closed_Lost_Detail__c`, `Stage_Before_Closed_Lost__c` — "have we lost here before, and why?"
- **Existing ARR:** sum of closed-won amounts — is this expansion on a paying account, or net-new?
- Why it matters: a $200K opp on an account already paying $88K/yr is a different conversation than a $200K net-new logo. You never evaluate an opp in isolation.

Lighter skills (router, tech-qual, connector-feasibility, internal-prep) pull only the active opp to stay fast.

### The holistic read — what CRM data tells an SE

Don't read SFDC field-by-field. Read it for three kinds of signal:

1. **Truth vs. story.** SFDC is the AE's narrative; transcripts + your read are ground truth. **The gap is the signal.** Examples to flag:
   - Stage ahead of technical reality (`StageName` = Negotiation but `SE_Engaged__c` = false, no POV)
   - `Probability__c` = 80% but `Days_Since_Last_Activity__c` = 45 → happy-ears forecasting
   - `Champion__c` names someone who never appears in any transcript → untested champion
   - `Why_buy_now__c` claims urgency no transcript confirms → manufactured forcing function

2. **Trajectory / velocity.** Deals are motion. Read the stage-date series (`Stage_0_Date__c` … `Stage_6_Date__c`, `Stage_Time_X_Y__c`, `Last_Stage_Change_Date__c`). A deal that moved 1→2 in 4 days but has sat in Stage 4 for 60 days is stuck — that's a finding.

3. **The "why" trio + closed-lost intel.** `Why_buy_anything__c` / `Why_buy_now__c` / `Why_buy_from_Airbyte__c` map to Driver/Urgency/Decision-Criteria. For prior lost opps, `Closed_Reason__c` + `Closed_Lost_Detail__c` tell you why you lost before.

### Mismatch-flagging posture: ASSERTIVE

When SFDC and reality (transcripts, local artifacts, your read) disagree, **call it out as an explicit finding every time** — don't bury it or soften it. Treat the CRM as a hypothesis to test, not as truth. A dedicated "⚠️ SFDC vs. Reality" callout in the output is the right pattern. This is the single highest-value thing SFDC enrichment provides — the Acme Flex deal showed exactly why (SFDC said Closed/Lost; local artifacts hadn't caught up).

### Field Reference (Airbyte SFDC Opportunity custom fields)

Verified live fields (as of 2026-05-28). Note two DEPRECATED fields to avoid.

**MEDDPICC:**
- `Economic_Buyer__c`, `Decision_Maker__c` → Economic Buyer
- `Champion__c` → Champion
- `Identify_Pain__c` → Identify Pain
- `Decision_Process__c` → Decision Process
- `Primary_Competitor__c`, `Gong__MainCompetitors__c`, `Fivetran_competitive__c`, `Fivetran_Renewal_Date__c` → Competition
- `Required_features_functionality__c` → Decision Criteria (use this, NOT deprecated `Decision_Criteria__c`)

**Driver / Urgency:**
- `Why_buy_anything__c`, `Why_buy_now__c`, `Why_buy_from_Airbyte__c`

**Technical:**
- `Most_important_sources__c`, `Most_Important_Destinations__c`
- `No_of_Databases__c`, `No_of_API_Sources__c`, `Monthly_Data_Volume__c`, `Refresh_Frequency__c`
- `Use_case_description__c`, `Airbyte_Use_Case__c` (use these, NOT deprecated `Primary_Use_Case__c`)
- `Region__c`, `Billing_Country__c` → data residency
- `Support_SLA__c`, `Contracted_Data_Workers__c`

**Deal health / SE:**
- `SE_Engaged__c`, `SE_Name__c` → call attribution + is-SE-on-deal
- `SE_Deal_Risks__c` → SE-flagged risks (read; future write-back candidate)
- `At_risk__c`, `Days_Since_Last_Activity__c`, `Last_Stage_Change_Date__c`
- `Next_Step_Date__c`, `Next_Step_History__c`
- `POV_Created__c`, `POV_Completed__c`

**Forecast:**
- `Probability__c`, `Forecast_Value__c`, `Weighted_ACV__c`, `Opportunity_ACV__c`, `ARR__c`

**Trajectory:**
- `Stage_0_Date__c` … `Stage_6_Date__c`, `Stage_Time_1_2__c` … `Stage_Time_6_Closed_Won__c`, `Stage_Number__c`, `Latest_Stage_Reached__c`

**Standard fields:** `StageName`, `Amount`, `CloseDate`, `Type`, `Owner.Name`, `LeadSource`

**Avoid (DEPRECATED):** `Decision_Criteria__c`, `Primary_Use_Case__c`

### Source Coverage

When a skill uses SFDC, the Source Coverage section must report: the opp(s) queried (Id + Name), which were treated as "the deal" vs. "account arc," and that the data is AE-entered (treat as hypothesis, not verified truth).

---

## Cross-Transcript Analysis

When synthesizing across multiple transcripts, notes, or memory records for a customer (e.g., during deal-assessment, post-call referencing prior calls, prep-call reviewing history, biz-qual scoring MEDDPICC over time):

1. **Read all available source material.** Don't skip earlier transcripts or skim only the most recent. Context lives in the trajectory, not just the snapshot.

2. **Weight by recency.** The most recent transcript reflects current state. Older transcripts provide context, evolution, and patterns. When statements conflict, the recent one wins unless there's a reason to trust the older one (e.g., a more senior stakeholder said it).

3. **Cross-reference by (speaker, topic, date).** The same topic appearing across multiple calls is signal, not noise. Track who said what and when. Stakeholder authority matters as much as recency — the technical decision-maker's view on architecture overrides the commercial sponsor's view on the same topic.

4. **Call out contradictions explicitly** in a dedicated section. Classify each as one of three types:
   - **Evolution** — they learned more and updated their position (healthy signal; no concern)
   - **Stakeholder split** — different people in the org disagree (this is often where the deal is won or lost — find the fault line and address it)
   - **Walking it back** — same person, softer commitment over time (deal-decay signal; deserves a direct callout and a forcing-function next step)

5. **Flag topics that went quiet.** Themes that appeared in early transcripts and stopped appearing in recent ones. Two possibilities, both worth surfacing:
   - **Resolved** — they got the answer they needed and moved on (healthy)
   - **Abandoned** — they gave up pushing because they lost interest or routed around us (unhealthy; possible champion fatigue or competitor surfacing)

   When you can't tell which it is from the source material, name it as an open question for the next call.

6. **When in doubt, trust the most recent statement from the most senior stakeholder on that topic.** Recency and authority compound — they're not independent axes.

7. **Cite sources inline.** When making a claim, reference the transcript date and speaker. Not "the customer is concerned about X" but "Jordan raised X on 04.01, having mentioned it in passing 03.25 — the concern is sharpening, not fading."

---

## Output Mode (Brief vs. Full)

Every SE skill produces a full-length output by default — comprehensive, table-heavy, multi-section. This is right for the first time a doc is created, or when a customer's status warrants depth.

For known accounts, quick refreshes, or back-to-back use, a tight version is more useful than a 200-line working doc.

### How brief mode triggers
- User says `--brief`, `quick`, `short version`, `1-pager`, `summary mode`, or similar
- User explicitly references a prior full version: "give me the short version of last week's Acme assessment"

### Unified brief-mode rule (applies to all SE skills)

**Brief mode keeps the top 1-3 decision-relevant sections only.** What counts as "decision-relevant" depends on the skill — each skill's body specifies which sections survive.

Common rules across all skills:
- **No coaching layer** (Coaching Observations, "What Could Have Gone Better" sections always cut)
- **No supporting/explanatory tables** that exist to teach (e.g., the band-definitions table in deal-assessment is cut; the verdict band stays)
- **No "what NOT to do" or "anti-patterns" sections** in output
- **Source citations stay** — brief ≠ vague. Every claim still cites a transcript date/speaker
- **Quantitative claims stay** — never strip numbers to save lines
- **Length follows from content, not a hard cap.** A `deployment-model-qual` brief is genuinely 5 lines (verdict + rationale + next action). A `deal-assessment` brief is genuinely ~25 lines because deal health is intrinsically more complex. Don't force a uniform line count.

### Default mode
Full unless brief is requested.

---

## Deferred Future Skills

Skills considered but not built yet. Add when the need is concrete enough to justify the scaffold.

### `renewal-assessment` / `expansion-readiness`
Different MEDDPICC profile from net-new sales:
- Champion is an existing user (track engagement, not introduction)
- EB is known (focus on expansion budget, not approval)
- Decision Process is shorter (no formal RFP)
- New signals matter: usage trends, support tickets, exec sponsorship continuity, NPS-style health
- Risk signals: declining usage, churned advocates, competitor sniffing at renewal

If/when built, this should be a separate skill (not a mode flag on biz-qual/deal-assessment) because the deal dynamics are genuinely different.

---

## Airbyte-Specific Application Notes

- **Deployment-model qualification first.** Per Gary's CLAUDE.md: confirm Cloud SaaS / Self-Managed / Hybrid early. Air-gap, data residency, BYOK, or VPC isolation requirements mean Cloud isn't viable — requalify to Self-Managed Enterprise or park. Don't waste cycles.
- **The connector-count reframe (Challenger).** Customers compare Airbyte to Fivetran on connector count. Real reframe: it's not the count, it's coverage of *your* stack + how the long tail gets built when something's missing. Pivot to manifest-only builder + custom CDK story.
- **The "build it ourselves" objection (TCO move).** Engage seriously. Stack implications: connector count over 3 years, schema-drift handling, on-call burden, opportunity cost of engineering time. Often this is the strongest reframe opportunity in a deal.
- **OSS skepticism reframe.** "Open source = support risk" is the misconception. Cloud Pro is fully managed with SLAs; OSS is the trust-building distribution model, not the support model.

---

## Changelog

- **2026-07-01** — **Source Coverage moved to the BOTTOM** of the doc (was right after At-a-Glance) — it's the audit trail, not the lead. Top-of-document structure + progressive-disclosure note updated; applied to all four decision-first-ordered skills (connector-feasibility, prep-call, post-call, tech-qual). Also **per-skill decision-first section reordering**: connector-feasibility (Fit Verdict before Use Case); prep-call (Company Snapshot + Why Airbyte to the top, before AE-learned/where-we-left-off); post-call (Key Takeaways → Deal Health → New Objections → Action Items up top, Attendees + Source Coverage at bottom); tech-qual (verdict-then-architecture already good, only Source Coverage moved down). Pairs with web-app renderer: checkbox affordance (no more square-bullet ☐), constraint/info cards, calmer bold, Inter + slate palette.
- **2026-06-25** — Added the **Decision-First Layout** sub-contract to Output Document Format: analytical skills (tech-qual, biz-qual, deal-assessment, deployment-model-qual, connector-feasibility, poc-plan) lead with a **Decision Card** (verdict/motion/primary-risk/confidence/next-gate), a standardized **Scorecard** with a "Why it matters" column, **facts-vs-judgment-vs-recommendation** labeling, and **Open-Questions / Next-Actions decision tables** (Owner / Needed-By / Why / Status — render `TBD` where unstated, never invent). Plus progressive disclosure (Source Coverage stays low) and jargon-translation in user-facing prose. Reinforced `==…==` = numbers/short-tokens-only (no sentence highlighting). Extended exemptions (account-refresher light-touch; internal-prep light adoption). Pairs with web-app render fixes (softer highlight, wider tables, checkbox bullets).

- **2026-06-18** — Added the Output Document Format contract: standard top-of-doc structure (H1 title → At a Glance → Jump-to index → Source Coverage → H2 body sections), H2-per-section rule for the auto-index, GitHub-style callouts (`[!verdict]`/`[!risk]`/`[!blocker]`/`[!info]`), `==key==` number emphasis (3–6 cap), and exemptions (follow-up-email full; next-move + objection-handler light-touch). Pairs with the web app's TOC sidebar + callout/highlight rendering.

- **2026-05-28** — Added Salesforce Enrichment (Shared Machinery) section: connection via sf-mcp + .se-config.yaml, opp-matching rule (most-recent-open, exclude renewals unless only-open), account-arc rule (deal-assessment + biz-qual only), the holistic three-signal read (truth-vs-story, trajectory, why-trio), ASSERTIVE mismatch-flagging posture, verified Airbyte SFDC field reference (incl. 2 deprecated fields to avoid).

- **2026-05-28** — Added SE Identity (multi-user .se-config.yaml), Call Attribution rule (post-call audience framing), Output Persistence (auto-save defaults + folder structure), Source Coverage Transparency (anti-hallucination), session-dedupe logging requirement. Migrated existing customer folders to outputs/ + raw/ structure.

- **2026-05-27** — Skill Sequencing Rules added — hard prerequisites for qualification skills, refusal table, standard workflow ordering with AE→SE handoff. prep-call lookback differentiated (7d→14d for new prospects, default for typical case). Output Mode (Brief vs Full) section.
- **2026-05-27** — Cross-Transcript Analysis section added (Evolution / Stakeholder split / Walking-it-back classification; topics-that-went-quiet; speaker × recency × authority).
- **2026-05-27** — Source Freshness Check (Gong Fallback) section added — per-skill defaults table.
- **2026-05-27** — Memory Check (Active Project Context) section added.
- **2026-05-27** — Initial creation — MEDDPICC, SPIN, Sandler, Challenger, Voss frameworks + when-to-use-which table + question taxonomy + anti-patterns + Airbyte-specific application notes.
