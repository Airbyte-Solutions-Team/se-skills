// Command Center (local, single-user pilot) — Today, Portfolio, Actions,
// Changes and the Unprocessed Sources queue. Every view renders from the
// bounded aggregate read APIs in routes/command_center.py; nothing here calls
// a provider or model. Shares app.js globals (api, esc, view, setCrumbs,
// emptyBox, showToast, relTime); loaded before app.js, used only from route().
/* global api, esc, view, setCrumbs, emptyBox, showToast */

const CC_BASE = "#/command-center";
const CC_TABS = [
  ["today", "Today"],
  ["portfolio", "Portfolio"],
  ["actions", "Actions"],
  ["changes", "Changes"],
  ["sources", "Sources"],
];
const CC_PAGE = 25;

const ccLabel = (v) => String(v ?? "unknown").replaceAll("_", " ");
const ccWhen = (iso) => {
  if (!iso) return "unknown";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? esc(String(iso)) : d.toLocaleString();
};
const ccDay = (iso) => (iso ? esc(String(iso).slice(0, 10)) : "no date");

function ccTone(status) {
  return ({
    open: "info", proposed: "warn", blocked: "error", completed: "ok", dismissed: "neutral",
    processed: "ok", failed: "error", processing: "info", queued: "info",
    awaiting_association: "warn", awaiting_content: "warn", discovered: "warn", superseded: "neutral",
    associated: "ok", unassociated: "warn", ambiguous: "warn",
    processed_latest_import: "ok", source_pending: "warn", overview_behind: "warn",
    unavailable: "error", no_sources: "neutral",
    current: "ok", missing: "neutral", stale: "warn", unknown: "neutral", known: "ok", partial: "warn",
  })[status] || "neutral";
}
const ccBadge = (status, extra = "") =>
  `<span class="cc-badge cc-badge--${ccTone(status)}${extra ? " " + extra : ""}">${esc(ccLabel(status))}</span>`;

function ccQuery(params) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== null && v !== undefined && v !== "" && v !== false) q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `?${s}` : "";
}

function ccReadHash() {
  const [path, query = ""] = location.hash.slice(1).split("?");
  const parts = path.split("/").filter(Boolean); // ["command-center", tab, arg?]
  return { tab: parts[1] || "today", arg: parts[2] ? decodeURIComponent(parts[2]) : null, params: new URLSearchParams(query) };
}

function ccGo(tab, params = {}, arg = null) {
  const path = `${CC_BASE}/${tab}${arg ? "/" + encodeURIComponent(arg) : ""}`;
  location.hash = path + ccQuery(params);
}

function ccShell(tab, title, sub, body) {
  setCrumbs([{ label: "Command Center", href: `${CC_BASE}/today` }, { label: title }]);
  view.innerHTML = `
    <div class="row cc-head">
      <div><h1>Command Center</h1><p class="sub">${esc(sub)}</p></div>
      <span class="cc-pilot-note" title="Local single-user pilot: pages read persisted local records only.">Local pilot · manual Granola import · no live sync</span>
    </div>
    <nav class="tabs cc-tabs" role="tablist" aria-label="Command Center views">
      ${CC_TABS.map(([id, label]) => `<a class="tab${id === tab ? " active" : ""}" role="tab" aria-selected="${id === tab}" href="${CC_BASE}/${id}">${label}</a>`).join("")}
    </nav>
    <section id="cc-body" class="cc-body" aria-live="polite">${body}</section>`;
  return document.getElementById("cc-body");
}

function ccLoading() {
  return `<div class="cc-loading" role="status">Loading…</div>`;
}

function ccError(e, retryHref) {
  return emptyBox({
    icon: "!", title: "Couldn't load this view",
    body: e?.message || String(e),
    actions: retryHref ? `<a class="ghost small" href="${esc(retryHref)}">Retry</a>` : "",
  });
}

function ccPager(page, onPage) {
  const { total, offset, limit, next_offset } = page;
  if (!total) return "";
  const from = offset + 1;
  const to = Math.min(offset + limit, total);
  const prev = offset > 0 ? Math.max(0, offset - limit) : null;
  return `<div class="cc-pager" role="navigation" aria-label="Pagination">
    <span class="muted">Showing ${from}–${to} of ${total}</span>
    <span class="cc-pager-buttons">
      <button class="ghost small" type="button" data-cc-page="${prev ?? ""}" ${prev === null ? "disabled" : ""}>‹ Prev</button>
      <button class="ghost small" type="button" data-cc-page="${next_offset ?? ""}" ${next_offset === null ? "disabled" : ""}>Next ›</button>
    </span>
  </div>`;
}

function ccWirePager(root, params, tab, arg = null) {
  root.querySelectorAll("[data-cc-page]").forEach((b) => {
    b.addEventListener("click", () => {
      if (b.disabled) return;
      ccGo(tab, { ...params, offset: b.dataset.ccPage || 0 }, arg);
    });
  });
}

function ccOppLink(item) {
  return `<a class="cc-opp" href="${esc(item.opportunity_link)}">${esc(item.account)} · ${esc(item.opportunity_name || item.opportunity_slug)}</a>`;
}

function ccFilterBar(fields, params) {
  // fields: [{name,label,options:[[value,label]]}] — `options` null → text input.
  return `<form class="cc-filters" id="cc-filters" aria-label="Filters">
    ${fields.map((f) => f.options
      ? `<label>${esc(f.label)} <select name="${f.name}">
          <option value="">Any</option>
          ${f.options.map(([v, l]) => `<option value="${esc(v)}"${params.get(f.name) === v ? " selected" : ""}>${esc(l)}</option>`).join("")}
        </select></label>`
      : f.checkbox
        ? `<label class="cc-check"><input type="checkbox" name="${f.name}"${params.get(f.name) === "true" ? " checked" : ""}/> ${esc(f.label)}</label>`
        : `<label>${esc(f.label)} <input type="text" name="${f.name}" value="${esc(params.get(f.name) || "")}" placeholder="${esc(f.placeholder || "")}" pattern="[A-Za-z0-9._-]{1,120}"/></label>`
    ).join("")}
    <button class="primary small" type="submit">Apply</button>
    <button class="ghost small" type="reset" id="cc-filters-reset">Clear</button>
  </form>`;
}

function ccWireFilters(root, tab, fixed = {}) {
  const form = root.querySelector("#cc-filters");
  if (!form) return;
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const data = new FormData(form);
    const params = { ...fixed };
    for (const [k, v] of data.entries()) params[k] = v === "on" ? "true" : v;
    ccGo(tab, params);
  });
  form.querySelector("#cc-filters-reset").addEventListener("click", (e) => { e.preventDefault(); ccGo(tab, fixed); });
}

// ── Today ────────────────────────────────────────────────────────────────

const CC_KIND_LABEL = {
  overdue_action: "Overdue", due_action: "Due soon", proposal_review: "Proposal to review",
  confirmed_blocker: "Confirmed blocker", association_review: "Association needed",
  source_unavailable: "Source unavailable", processing_failed: "Processing failed",
  reconciliation_failed: "Reconciliation failed", content_missing: "Content missing",
};
const ccKindTone = (k) => ({
  overdue_action: "error", confirmed_blocker: "error", processing_failed: "error", reconciliation_failed: "error",
  source_unavailable: "error", due_action: "warn", proposal_review: "warn", association_review: "warn", content_missing: "warn",
})[k] || "neutral";

async function ccPageToday(params) {
  const offset = Number(params.get("offset") || 0);
  const body = ccShell("today", "Today", "What needs a human today, from persisted local records only.", ccLoading());
  let data;
  try { data = await api(`/api/command-center/today${ccQuery({ limit: CC_PAGE, offset })}`); }
  catch (e) { body.innerHTML = ccError(e, `${CC_BASE}/today`); return; }

  const counts = data.counts_by_kind || {};
  const summary = Object.entries(CC_KIND_LABEL)
    .filter(([k]) => counts[k])
    .map(([k, label]) => `<span class="cc-count cc-count--${ccKindTone(k)}">${counts[k]} ${esc(label)}</span>`).join("");

  const list = data.attention.length ? `<ul class="cc-attention" aria-label="Attention items">${data.attention.map((it) => `
    <li class="cc-att cc-att--${ccKindTone(it.kind)}">
      <div class="cc-att-head">
        <span class="cc-badge cc-badge--${ccKindTone(it.kind)}">${esc(CC_KIND_LABEL[it.kind] || ccLabel(it.kind))}</span>
        ${ccOppLink(it)}
        ${it.when ? `<span class="muted cc-att-when">${ccDay(it.when)}</span>` : ""}
      </div>
      <div class="cc-att-title">${esc(it.title)}</div>
      <div class="cc-att-reason">${esc(it.reason)}</div>
      <div class="cc-att-foot">
        <span class="muted">Next: ${esc(it.next_step)}</span>
        ${it.link ? `<a class="ghost small" href="${esc(it.link)}">Open</a>` : ""}
      </div>
    </li>`).join("")}</ul>`
    : emptyBox({
      icon: "✓", title: "Nothing needs attention",
      body: data.opportunity_count
        ? "No overdue or due actions, proposals, blockers, or source problems are recorded locally."
        : "No local opportunities yet. Import a meeting under Sources or create an Opportunity Overview first.",
      actions: `<a class="ghost small" href="${CC_BASE}/sources">Go to Sources</a>`,
    });

  const recent = (data.recent_changes || []).length
    ? `<ul class="cc-recent">${data.recent_changes.map((c) => `
        <li><span class="muted">${ccWhen(c.applied_at)}</span> ${ccBadge(c.change_type)} ${ccOppLink(c)}
          ${c.source_link ? `<a class="cc-mini" href="${esc(c.source_link)}">source</a>` : ""}</li>`).join("")}</ul>`
    : `<p class="muted">No changes recorded yet.</p>`;

  body.innerHTML = `
    <div class="cc-summary">${summary || `<span class="muted">${data.opportunity_count} local opportunit${data.opportunity_count === 1 ? "y" : "ies"} · nothing flagged</span>`}</div>
    ${list}
    ${ccPager(data, "today")}
    <h2 class="cc-h2">Recent changes</h2>
    ${recent}
    <p class="muted cc-foot-note">Confirmed blockers appear only when a persisted, evidence-backed blocker exists in an Opportunity Overview; inferred risks are never shown as blockers.</p>`;
  ccWirePager(body, {}, "today");
}

// ── Portfolio ────────────────────────────────────────────────────────────

function ccFreshness(f) {
  const counts = f.source_counts || {};
  const detail = [
    `${counts.total ?? 0} source(s)`,
    counts.stale ? `${counts.stale} stale` : "",
    counts.pending ? `${counts.pending} pending` : "",
    counts.failed ? `${counts.failed} failed` : "",
    counts.withheld ? `${counts.withheld} withheld` : "",
  ].filter(Boolean).join(" · ");
  return `<div class="cc-fresh">
    ${ccBadge(f.state)} <span>${esc(f.label)}</span>
    <div class="muted cc-fresh-detail">${esc(detail)}${f.last_manual_import_at ? ` · last manual import ${ccWhen(f.last_manual_import_at)}` : ""}${f.latest_processed_meeting_at ? ` · latest processed meeting ${ccDay(f.latest_processed_meeting_at)}` : ""}</div>
    <div class="muted cc-fresh-detail">Granola: ${esc(ccLabel(f.connector.mode))} · health ${esc(ccLabel(f.connector.health))}</div>
  </div>`;
}

async function ccPagePortfolio(params) {
  const filters = { account: params.get("account") || "", attention_only: params.get("attention_only") === "true" };
  const offset = Number(params.get("offset") || 0);
  const body = ccShell("portfolio", "Portfolio", "Every locally known opportunity, with honest per-source freshness.", ccLoading());
  let data;
  try { data = await api(`/api/command-center/portfolio${ccQuery({ ...filters, limit: CC_PAGE, offset })}`); }
  catch (e) { body.innerHTML = ccError(e, `${CC_BASE}/portfolio`); return; }

  const accountOpts = (data.accounts || []).map((a) => [a, a]);
  const cards = data.opportunities.length ? `<div class="cc-cards">${data.opportunities.map((o) => {
    const ac = o.action_counts;
    const ev = o.evaluation;
    const evText = !ev.supported ? "not tracked here" : (ev.overall ? `${ccLabel(ev.overall)}${ev.current_phase ? ` · ${ccLabel(ev.current_phase)}` : ""}` : "no tracker yet");
    return `<article class="card cc-card" aria-label="${esc(o.account)} ${esc(o.opportunity_name || o.opportunity_slug)}">
      <div class="cc-card-head">
        <div><div class="muted">${esc(o.account)}</div><h3><a href="${esc(o.opportunity_link)}">${esc(o.opportunity_name || o.opportunity_slug)}</a></h3></div>
        ${ccBadge(o.overview.status, "cc-badge--lg")}
      </div>
      <dl class="cc-facts">
        <dt>Next step</dt><dd>${o.next_step.value ? esc(o.next_step.value) + ` <span class="muted">(${esc(ccLabel(o.next_step.state))}${o.next_step.confirmation ? ", " + esc(ccLabel(o.next_step.confirmation)) : ""})</span>` : `<span class="muted">${esc(ccLabel(o.next_step.state))}</span>`}</dd>
        <dt>Actions</dt><dd>${ac.open} open · ${ac.blocked} blocked · ${ac.proposed} proposed · ${ac.completed} done${ac.overdue ? ` · <strong class="cc-danger">${ac.overdue} overdue</strong>` : ""}</dd>
        <dt>Waiting on</dt><dd>${o.waiting_on.length ? o.waiting_on.map((p) => `<span class="chip">${esc(p)}</span>`).join("") : `<span class="muted">nobody recorded</span>`}</dd>
        <dt>Blockers</dt><dd>${o.confirmed_blockers.length ? o.confirmed_blockers.map((b) => `<span class="cc-badge cc-badge--error">${esc(b.title)}</span> ${esc(b.reason)}`).join("<br/>") : `<span class="muted">none confirmed</span>`}</dd>
        <dt>Evaluation</dt><dd>${esc(evText)}</dd>
        <dt>Freshness</dt><dd>${ccFreshness(o.freshness)}</dd>
      </dl>
      ${o.attention.length ? `<div class="cc-card-attn">${o.attention.map((a) => `<span class="cc-count cc-count--warn">${esc(a)}</span>`).join("")}</div>` : ""}
      <div class="cc-card-foot">
        <a class="ghost small" href="${esc(o.opportunity_link)}">Overview</a>
        <a class="ghost small" href="${CC_BASE}/actions${ccQuery({ account: o.account, opportunity_slug: o.opportunity_slug })}">Actions</a>
        <a class="ghost small" href="${CC_BASE}/changes${ccQuery({ account: o.account, opportunity_slug: o.opportunity_slug })}">Changes</a>
        <span class="muted cc-card-when">${o.latest_change_at ? "changed " + ccWhen(o.latest_change_at) : "no changes yet"}</span>
      </div>
    </article>`;
  }).join("")}</div>`
    : emptyBox({
      icon: "⊘", title: filters.account || filters.attention_only ? "No opportunities match these filters" : "No local opportunities",
      body: "Opportunities appear here once an Overview exists locally or a meeting source has been associated with one.",
      actions: `<a class="ghost small" href="${CC_BASE}/sources">Go to Sources</a>`,
    });

  body.innerHTML = `
    ${ccFilterBar([
      { name: "account", label: "Account", options: accountOpts },
      { name: "attention_only", label: "Needs attention only", checkbox: true },
    ], params)}
    ${cards}
    ${ccPager(data)}`;
  ccWireFilters(body, "portfolio");
  ccWirePager(body, filters, "portfolio");
}

// ── Actions ──────────────────────────────────────────────────────────────

const CC_STATUSES = ["proposed", "open", "blocked", "completed", "dismissed"];
const CC_PARTIES = ["Airbyte", "Customer", "Engineering", "Security", "Partner", "Unknown"];

function ccActionRow(a, { detail = false } = {}) {
  const due = a.due_date ? `${ccDay(a.due_date)}${a.overdue ? ' <span class="cc-danger">overdue</span>' : a.due_soon ? ' <span class="cc-warn">due soon</span>' : ""}` : '<span class="muted">no due date</span>';
  const prov = a.provenance;
  return `<li class="cc-action${a.retracted ? " cc-action--retracted" : ""}" data-action-id="${esc(a.action_id)}">
    <div class="cc-action-head">
      ${ccBadge(a.status)}${a.retracted ? ccBadge("retracted") : ""}${a.human_touched ? '<span class="cc-badge cc-badge--neutral">human</span>' : ""}
      ${ccOppLink(a)}
      <span class="cc-action-due">${due}</span>
    </div>
    <div class="cc-action-title"><a href="${CC_BASE}/actions/${esc(a.action_id)}">${esc(a.commitment)}</a></div>
    <div class="muted cc-action-meta">
      ${esc(a.party)}${a.owner ? ` · ${esc(a.owner)}` : ""}
      · <a class="cc-mini" href="${esc(prov.source_link)}">${prov.evidence_count} evidence ref(s)${prov.retracted_evidence_count ? `, ${prov.retracted_evidence_count} retracted` : ""}</a>
      ${prov.origin_locator ? ` @ ${esc(prov.origin_locator)}` : ""}
      ${a.possible_duplicate_of ? ` · <span class="cc-warn">possible duplicate</span>` : ""}
      ${a.active_completion_suggestions?.length ? ` · <span class="cc-warn">${a.active_completion_suggestions.length} completion suggestion(s)</span>` : ""}
    </div>
    ${detail ? ccActionDetail(a) : ""}
    <div class="cc-action-controls" role="group" aria-label="Transitions for ${esc(a.commitment)}">
      ${a.allowed_transitions.map((t) => `<button class="ghost small" type="button" data-transition="${esc(t)}">${esc(t)}</button>`).join("")}
      ${a.transitions.some((t) => t.actor === "user") ? `<button class="ghost small" type="button" data-undo="1">Undo last</button>` : ""}
    </div>
  </li>`;
}

function ccActionDetail(a) {
  return `<div class="cc-action-detail">
    ${a.definition_of_done ? `<p><strong>Done when:</strong> ${esc(a.definition_of_done)}</p>` : ""}
    <h4>Evidence</h4>
    <ul class="cc-evidence">${a.effective_evidence.map((e) => `<li><a href="${CC_BASE}/sources/${esc(e.source_id)}">${esc(e.source_id)}</a> r${e.revision}${e.locator ? ` @ ${esc(e.locator)}` : ""}</li>`).join("") || "<li class='muted'>none</li>"}</ul>
    ${a.evidence_retractions.length ? `<p class="muted">${a.evidence_retractions.length} evidence ref(s) retracted after an association correction.</p>` : ""}
    <h4>History</h4>
    <ol class="cc-transitions">${a.transitions.map((t) => `<li>
      <span class="muted">${ccWhen(t.recorded_at)}</span> ${t.from_status ? esc(t.from_status) + " → " : ""}${esc(t.to_status)}
      <span class="muted">by ${esc(t.actor)}${t.undoes_sequence ? ` (undoes #${t.undoes_sequence})` : ""}</span>
      ${t.reason ? `<div class="cc-reason">${esc(t.reason)}</div>` : ""}
    </li>`).join("")}</ol>
    ${a.retraction ? `<p class="cc-warn">Retracted: ${esc(a.retraction.reason || "association corrected")}</p>` : ""}
  </div>`;
}

async function ccMutate(promise, okMessage) {
  try {
    await promise;
    if (okMessage) showToast(okMessage, "ok");
    return true;
  } catch (e) {
    showToast(e.message || String(e), "err");
    return false;
  }
}

function ccPrompt(label, fallback) {
  const v = window.prompt(label, fallback);
  if (v === null) return null;
  const t = v.trim();
  return t || fallback;
}

function ccWireActions(root, rerender) {
  root.querySelectorAll(".cc-action").forEach((li) => {
    const id = li.dataset.actionId;
    li.querySelectorAll("[data-transition]").forEach((b) => b.addEventListener("click", async () => {
      const to = b.dataset.transition;
      const reason = ccPrompt(`Reason for marking this action ${to}:`, `Marked ${to} from Command Center`);
      if (reason === null) return;
      const ok = await ccMutate(api(`/api/command-center/actions/${encodeURIComponent(id)}/transitions`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to_status: to, reason }),
      }), `Action marked ${to}`);
      if (ok) rerender();
    }));
    li.querySelector("[data-undo]")?.addEventListener("click", async () => {
      const reason = ccPrompt("Reason for undoing the last change:", "Undone from Command Center");
      if (reason === null) return;
      const ok = await ccMutate(api(`/api/command-center/actions/${encodeURIComponent(id)}/undo`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }),
      }), "Last transition undone");
      if (ok) rerender();
    });
  });
}

async function ccPageActions(params, actionId) {
  if (actionId) return ccPageActionDetail(actionId);
  const filters = {
    account: params.get("account") || "", opportunity_slug: params.get("opportunity_slug") || "",
    status: params.get("status") || "", party: params.get("party") || "",
    overdue: params.get("overdue") === "true", include_retracted: params.get("include_retracted") === "true",
  };
  const offset = Number(params.get("offset") || 0);
  const body = ccShell("actions", "Actions", "Durable action records across every local opportunity.", ccLoading());
  let data;
  try { data = await api(`/api/command-center/actions${ccQuery({ ...filters, limit: CC_PAGE, offset })}`); }
  catch (e) { body.innerHTML = ccError(e, `${CC_BASE}/actions`); return; }

  const counts = Object.entries(data.counts_by_status || {}).map(([s, n]) => `<span class="cc-count cc-count--${ccTone(s)}">${n} ${esc(s)}</span>`).join("");
  body.innerHTML = `
    ${ccFilterBar([
      { name: "account", label: "Account", placeholder: "Account" },
      { name: "opportunity_slug", label: "Opportunity slug", placeholder: "needs account" },
      { name: "status", label: "Status", options: CC_STATUSES.map((s) => [s, s]) },
      { name: "party", label: "Party", options: CC_PARTIES.map((p) => [p, p]) },
      { name: "overdue", label: "Overdue only", checkbox: true },
      { name: "include_retracted", label: "Include retracted", checkbox: true },
    ], params)}
    <div class="cc-summary">${counts || '<span class="muted">no actions match</span>'}</div>
    ${data.actions.length ? `<ul class="cc-actions" aria-label="Actions">${data.actions.map((a) => ccActionRow(a)).join("")}</ul>` : emptyBox({ icon: "⊘", title: "No actions", body: "Actions are created when a confirmed meeting source is reconciled into an Opportunity Overview." })}
    ${ccPager(data)}`;
  ccWireFilters(body, "actions");
  ccWirePager(body, filters, "actions");
  ccWireActions(body, () => ccPageActions(params));
}

async function ccPageActionDetail(actionId) {
  const body = ccShell("actions", "Action", "One durable action with its evidence and history.", ccLoading());
  let a;
  try { a = await api(`/api/command-center/actions${ccQuery({ include_retracted: true, limit: 200 })}`).then((d) => d.actions.find((x) => x.action_id === actionId)); }
  catch (e) { body.innerHTML = ccError(e, `${CC_BASE}/actions/${actionId}`); return; }
  if (!a) {
    body.innerHTML = emptyBox({ icon: "?", title: "Action not found", body: "It may belong to another workspace or have been removed.", actions: `<a class="ghost small" href="${CC_BASE}/actions">All actions</a>` });
    return;
  }
  body.innerHTML = `<p><a class="cc-mini" href="${CC_BASE}/actions">‹ All actions</a></p><ul class="cc-actions">${ccActionRow(a, { detail: true })}</ul>`;
  ccWireActions(body, () => ccPageActionDetail(actionId));
}

// ── Changes ──────────────────────────────────────────────────────────────

const CC_CHANGE_TYPES = [
  "overview_revision", "action_created", "action_linked", "action_transition", "completion_suggested",
  "possible_duplicate_flagged", "association_corrected", "evidence_retracted", "completion_suggestion_retracted", "overview_reverted",
];

function ccDiff(before, after) {
  const keys = [...new Set([...Object.keys(before || {}), ...Object.keys(after || {})])];
  if (!keys.length) return "";
  return `<dl class="cc-diff">${keys.map((k) => {
    const b = before?.[k]; const a = after?.[k];
    if (JSON.stringify(b) === JSON.stringify(a)) return "";
    return `<dt>${esc(k)}</dt><dd>${b === undefined ? "" : `<s>${esc(JSON.stringify(b))}</s> → `}${esc(JSON.stringify(a ?? null))}</dd>`;
  }).join("")}</dl>`;
}

async function ccPageChanges(params) {
  const filters = {
    account: params.get("account") || "", opportunity_slug: params.get("opportunity_slug") || "",
    change_type: params.get("change_type") || "", actor: params.get("actor") || "",
  };
  const offset = Number(params.get("offset") || 0);
  const body = ccShell("changes", "Changes", "Deterministic change history, newest first.", ccLoading());
  let data;
  try { data = await api(`/api/command-center/changes${ccQuery({ ...filters, limit: CC_PAGE, offset })}`); }
  catch (e) { body.innerHTML = ccError(e, `${CC_BASE}/changes`); return; }

  body.innerHTML = `
    ${ccFilterBar([
      { name: "account", label: "Account", placeholder: "Account" },
      { name: "opportunity_slug", label: "Opportunity slug", placeholder: "needs account" },
      { name: "change_type", label: "Type", options: CC_CHANGE_TYPES.map((t) => [t, ccLabel(t)]) },
      { name: "actor", label: "Actor", options: [["analysis", "analysis"], ["user", "user"], ["system", "system"]] },
    ], params)}
    ${data.changes.length ? `<ol class="cc-changes" aria-label="Changes">${data.changes.map((c) => `
      <li class="cc-change">
        <div class="cc-change-head">
          <span class="muted">${ccWhen(c.applied_at)}</span> ${ccBadge(c.change_type)}
          <span class="muted">by ${esc(c.actor)}</span> ${ccOppLink(c)}
        </div>
        ${ccDiff(c.before, c.after)}
        <div class="cc-change-links">
          ${c.subject_id?.startsWith("act_") ? `<a class="cc-mini" href="${CC_BASE}/actions/${esc(c.subject_id)}">action</a>` : ""}
          ${c.source_link ? `<a class="cc-mini" href="${esc(c.source_link)}">source r${c.source?.revision ?? "?"}${c.source?.locator ? " @ " + esc(c.source.locator) : ""}</a>` : ""}
          <a class="cc-mini" href="${esc(c.opportunity_link)}">overview</a>
        </div>
      </li>`).join("")}</ol>` : emptyBox({ icon: "⊘", title: "No changes", body: "Changes are recorded when an Overview revision, action, or association is created or corrected." })}
    ${ccPager(data)}`;
  ccWireFilters(body, "changes");
  ccWirePager(body, filters, "changes");
}

// ── Sources ──────────────────────────────────────────────────────────────

function ccSourceRow(s) {
  const assoc = s.association;
  const target = assoc.state === "associated" ? `${esc(assoc.account)} · ${esc(assoc.opportunity_slug)}` : `<span class="muted">${esc(ccLabel(assoc.state))}</span>`;
  const stale = s.processing.processed_revision !== null && s.processing.processed_revision < s.latest_revision;
  return `<li class="cc-source">
    <div class="cc-source-head">
      ${ccBadge(s.processing.status)} ${ccBadge(s.availability)}${stale ? ccBadge("stale") : ""}
      <a class="cc-source-id" href="${CC_BASE}/sources/${esc(s.source_id)}">${esc(s.source_id)}</a>
    </div>
    <div class="muted cc-source-meta">
      ${esc(s.provider)} ${esc(s.kind)} · rev ${s.latest_revision} · meeting ${ccDay(s.latest.occurred_at)} · imported ${ccWhen(s.latest.observed_at)} (${esc(ccLabel(s.latest.trigger))})
      ${s.processing.last_error_code ? ` · <span class="cc-danger">error ${esc(s.processing.last_error_code)}</span>` : ""}
    </div>
    <div class="cc-source-assoc">Association: ${target}</div>
  </li>`;
}

async function ccPageSources(params, sourceId) {
  if (sourceId) return ccPageSourceReview(sourceId);
  const queue = params.get("view") !== "all";
  const status = params.get("status") || "";
  const offset = Number(params.get("offset") || 0);
  const body = ccShell("sources", "Sources", "Imported meeting sources: review association, retry, and reconcile.", ccLoading());
  let data;
  try {
    data = queue
      ? await api(`/api/command-center/sources/unprocessed${ccQuery({ limit: CC_PAGE, offset })}`)
      : await api(`/api/command-center/sources${ccQuery({ status, limit: CC_PAGE, offset })}`);
  } catch (e) { body.innerHTML = ccError(e, `${CC_BASE}/sources`); return; }

  const counts = Object.entries(data.counts_by_status || {}).map(([s, n]) => `<span class="cc-count cc-count--${ccTone(s)}">${n} ${esc(ccLabel(s))}</span>`).join("");
  body.innerHTML = `
    <div class="row cc-sources-bar">
      <div class="cc-subtabs" role="tablist">
        <a class="tab${queue ? " active" : ""}" role="tab" aria-selected="${queue}" href="${CC_BASE}/sources">Unprocessed queue</a>
        <a class="tab${queue ? "" : " active"}" role="tab" aria-selected="${!queue}" href="${CC_BASE}/sources?view=all">All sources</a>
      </div>
      <button class="primary small" type="button" id="cc-import-toggle" aria-expanded="false" aria-controls="cc-import">Manual Granola import</button>
    </div>
    <div id="cc-import" class="cc-import hidden">
      <p class="muted">Paste one or more Granola note payloads (a JSON array or a single object) exported by you. This is a manual, user-triggered import: nothing is polled or synced, and no credentials are stored. Transcript text is kept in the local ledger and never shown in aggregate views.</p>
      <label for="cc-import-json" class="muted">Note payload(s)</label>
      <textarea id="cc-import-json" rows="6" spellcheck="false" placeholder='[{"id": "not_…", "title": "…", …}]'></textarea>
      <div class="row-actions">
        <button class="primary small" type="button" id="cc-import-run">Import</button>
        <span id="cc-import-status" class="muted" role="status"></span>
      </div>
    </div>
    <div class="cc-summary">${counts || `<span class="muted">${queue ? "queue is empty" : "no sources"}</span>`}${data.malformed_records ? `<span class="cc-count cc-count--error">${data.malformed_records} malformed record(s) skipped</span>` : ""}</div>
    ${data.sources.length ? `<ul class="cc-sources" aria-label="Sources">${data.sources.map(ccSourceRow).join("")}</ul>`
      : emptyBox({ icon: queue ? "✓" : "⊘", title: queue ? "Nothing waiting" : "No sources imported", body: queue ? "Every imported source is processed, or none has been imported yet." : "Use the manual import above to add a meeting exported from Granola." })}
    ${ccPager(data)}`;
  ccWirePager(body, { view: queue ? "" : "all", status }, "sources");

  const toggle = body.querySelector("#cc-import-toggle");
  const panel = body.querySelector("#cc-import");
  toggle.addEventListener("click", () => {
    const open = panel.classList.toggle("hidden") === false;
    toggle.setAttribute("aria-expanded", String(open));
    if (open) body.querySelector("#cc-import-json").focus();
  });
  body.querySelector("#cc-import-run").addEventListener("click", async () => {
    const statusEl = body.querySelector("#cc-import-status");
    let notes;
    try {
      const parsed = JSON.parse(body.querySelector("#cc-import-json").value);
      notes = Array.isArray(parsed) ? parsed : [parsed];
    } catch { statusEl.textContent = "Not valid JSON."; return; }
    statusEl.textContent = "Importing…";
    try {
      const res = await api("/api/command-center/imports/granola", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ connection_id: "local-manual", notes }),
      });
      const results = res.results || [];
      const summary = results.map((r) => r.outcome || r.status || "ok");
      statusEl.textContent = `Imported ${results.length} note(s): ${summary.join(", ")}`;
      showToast(`Imported ${results.length} note(s)`, "ok");
      ccPageSources(params);
    } catch (e) {
      statusEl.textContent = e.message || String(e);
    }
  });
}

async function ccPageSourceReview(sourceId) {
  const body = ccShell("sources", "Source review", "Association, processing state, runs, and derived actions for one source.", ccLoading());
  let r;
  try { r = await api(`/api/command-center/sources/${encodeURIComponent(sourceId)}/review`); }
  catch (e) { body.innerHTML = ccError(e, `${CC_BASE}/sources/${sourceId}`); return; }
  const s = r.source;
  const assoc = s.association;
  const caps = r.capabilities;
  const base = r.overview_base;
  const rerender = () => ccPageSourceReview(sourceId);

  const candidates = r.candidates.map((c) => `<option value="${esc(c.account)}|${esc(c.opportunity_slug)}"${assoc.account === c.account && assoc.opportunity_slug === c.opportunity_slug ? " selected" : ""}>${esc(c.account)} · ${esc(c.opportunity_name || c.opportunity_slug)}</option>`).join("");

  body.innerHTML = `
    <p><a class="cc-mini" href="${CC_BASE}/sources">‹ Sources</a></p>
    <ul class="cc-sources"><li class="cc-source cc-source--detail">
      <div class="cc-source-head">${ccBadge(s.processing.status)} ${ccBadge(s.availability)} <span class="cc-source-id">${esc(s.source_id)}</span></div>
      <dl class="cc-facts">
        <dt>Provider</dt><dd>${esc(s.provider)} ${esc(s.kind)} · object ${esc(s.provider_object_id)} · connection ${esc(s.connection_id)}</dd>
        <dt>Revisions</dt><dd>${s.revision_count} (latest r${s.latest_revision}, ${esc(s.latest.change)}) · ${s.processing.processed_revision ? `processed r${s.processing.processed_revision}` : "not yet processed"}</dd>
        <dt>Meeting</dt><dd>${ccWhen(s.latest.occurred_at)} · ${s.latest.metrics.attendee_count} attendee(s) · ${s.latest.metrics.transcript_segments} transcript segment(s)</dd>
        <dt>Imported</dt><dd>${ccWhen(s.latest.observed_at)} · ${esc(ccLabel(s.latest.trigger))}</dd>
        <dt>Processing</dt><dd>${esc(s.processing.status)} · ${s.processing.attempts} attempt(s)${s.processing.last_error_code ? ` · <span class="cc-danger">last error ${esc(s.processing.last_error_code)}</span>` : ""}${s.latest.unavailable_reason ? ` · <span class="cc-danger">${esc(s.latest.unavailable_reason)}</span>` : ""}</dd>
      </dl>
    </li></ul>

    <h2 class="cc-h2">Association</h2>
    <p>${ccBadge(assoc.state)} ${assoc.state === "associated" ? `<a href="${esc(base?.opportunity_link || "#")}">${esc(assoc.account)} · ${esc(assoc.opportunity_slug)}</a> <span class="muted">(${esc(assoc.method || "")}, by ${esc(assoc.actor || "")})</span>` : `<span class="muted">${esc(assoc.reason || "")}</span>`}</p>
    ${assoc.candidates?.length ? `<p class="muted">Suggested: ${assoc.candidates.map((c) => `${esc(c.account)} · ${esc(c.opportunity_slug)}`).join(", ")} — suggestions are never auto-applied.</p>` : ""}
    <form id="cc-assoc" class="cc-inline-form">
      <label>Opportunity <select name="target" ${candidates ? "" : "disabled"}>${candidates || '<option value="">No local opportunities yet</option>'}</select></label>
      <label>Reason <input name="reason" type="text" required maxlength="300" placeholder="Why this association is correct" /></label>
      <button class="primary small" type="submit" ${candidates ? "" : "disabled"}>${assoc.state === "associated" ? "Correct association" : "Confirm association"}</button>
      ${caps.clear_association ? `<button class="ghost small" type="button" id="cc-assoc-clear">Clear association</button>` : ""}
    </form>
    <p class="muted">Correcting or clearing an association retracts evidence and actions derived from this source; the change history records it.</p>

    <h2 class="cc-h2">Reconcile into Overview</h2>
    ${base ? `<p>Base: ${ccBadge(base.status)} ${base.revision ? `revision ${base.revision} <code>${esc((base.version_id || "").slice(0, 12))}</code>` : ""} <a class="cc-mini" href="${esc(base.opportunity_link)}">open overview</a></p>` : `<p class="muted">No associated opportunity, so there is no Overview base.</p>`}
    <div class="row-actions">
      <button class="primary small" type="button" id="cc-reconcile" ${caps.reconcile ? "" : "disabled"}>Reconcile against revision ${base?.revision ?? "—"}</button>
      <button class="ghost small" type="button" id="cc-retry" ${caps.retry ? "" : "disabled"}>Retry processing</button>
      <span id="cc-run-status" class="muted" role="status">${caps.reconcile ? "" : esc(caps.reconcile_blocked_reason || "")}</span>
    </div>

    <h2 class="cc-h2">Runs</h2>
    ${r.runs.length ? `<ol class="cc-runs">${r.runs.map((run) => `<li>${ccBadge(run.status)} <span class="muted">${ccWhen(run.started_at || run.created_at)}</span> base r${run.base_revision ?? "?"}${run.error_code ? ` · <span class="cc-danger">${esc(run.error_code)}</span>` : ""}${run.error ? ` · ${esc(run.error)}` : ""}</li>`).join("")}</ol>` : `<p class="muted">No reconciliation runs yet.</p>`}

    <h2 class="cc-h2">Actions derived from this source</h2>
    ${r.derived_actions.length ? `<ul class="cc-actions">${r.derived_actions.map((a) => ccActionRow(a)).join("")}</ul>` : `<p class="muted">None.</p>`}

    <h2 class="cc-h2">Association history</h2>
    <ol class="cc-transitions">${s.association_history.map((h) => `<li><span class="muted">${ccWhen(h.recorded_at)}</span> ${ccBadge(h.state)} ${h.account ? `${esc(h.account)} · ${esc(h.opportunity_slug)}` : ""} <span class="muted">by ${esc(h.actor)}</span>${h.reason ? `<div class="cc-reason">${esc(h.reason)}</div>` : ""}</li>`).join("")}</ol>`;

  body.querySelector("#cc-assoc").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const [account, opportunity_slug] = (form.target.value || "").split("|");
    if (!account) return;
    const ok = await ccMutate(api(`/api/command-center/sources/${encodeURIComponent(sourceId)}/association`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account, opportunity_slug, reason: form.reason.value.trim() }),
    }), "Association confirmed");
    if (ok) rerender();
  });
  body.querySelector("#cc-assoc-clear")?.addEventListener("click", async () => {
    const reason = ccPrompt("Reason for clearing this association:", "Cleared from Command Center");
    if (reason === null) return;
    const ok = await ccMutate(api(`/api/command-center/sources/${encodeURIComponent(sourceId)}/association`, {
      method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }),
    }), "Association cleared");
    if (ok) rerender();
  });
  body.querySelector("#cc-retry").addEventListener("click", async () => {
    const ok = await ccMutate(api(`/api/command-center/sources/${encodeURIComponent(sourceId)}/retry`, { method: "POST" }), "Source queued for retry");
    if (ok) rerender();
  });
  body.querySelector("#cc-reconcile").addEventListener("click", async () => {
    const statusEl = body.querySelector("#cc-run-status");
    statusEl.textContent = "Starting…";
    let job;
    try {
      job = await api(`/api/command-center/sources/${encodeURIComponent(sourceId)}/reconcile`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_version_id: base.version_id, base_revision: base.revision }),
      });
    } catch (err) { statusEl.textContent = err.message || String(err); return; }
    const jobId = job.job_id;
    for (let i = 0; i < 600; i++) {
      await new Promise((res) => setTimeout(res, 1000));
      let st;
      try { st = await api(`/api/command-center/reconciliations/${encodeURIComponent(jobId)}`); }
      catch (err) { statusEl.textContent = err.message || String(err); return; }
      statusEl.textContent = `Reconciliation ${st.status}…`;
      if (st.status !== "running" && st.status !== "queued") break;
    }
    rerender();
  });
}

// ── Router entry ─────────────────────────────────────────────────────────

async function pageCommandCenter() {
  const { tab, arg, params } = ccReadHash();
  if (tab === "today") return ccPageToday(params);
  if (tab === "portfolio") return ccPagePortfolio(params);
  if (tab === "actions") return ccPageActions(params, arg);
  if (tab === "changes") return ccPageChanges(params);
  if (tab === "sources") return ccPageSources(params, arg);
  location.hash = `${CC_BASE}/today`;
}
