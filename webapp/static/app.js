// SE Skills — local hub frontend. Tiny hash-router, no build step.
const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.headers.get("content-type")?.includes("application/json") ? r.json() : r.text();
};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let outputMeta = {};

// Ask the planner whether a skill invocation is ready. If it is not, show a
// browser confirm dialog with the missing prerequisites and a "Run anyway" path.
// Free-form instructions bypass the planner entirely.
async function invokeWithPlan(payload, alreadyConfirmed = false) {
  if (!payload.freeform && !alreadyConfirmed && payload.skill) {
    try {
      const qs = new URLSearchParams({ account: payload.account, skill: payload.skill });
      if (payload.opp_slug) qs.set("opp_slug", payload.opp_slug);
      const plan = await api(`/api/plan?${qs.toString()}`);
      if (!plan.ready) {
        const lines = ["This skill is missing prerequisites:"].concat(plan.missing.map((m) => "• " + m));
        if (!confirm(lines.join("\n") + "\n\nRun anyway?")) {
          throw new Error("Cancelled");
        }
        return invokeWithPlan({ ...payload, override_prerequisites: true }, true);
      }
    } catch (e) {
      if (e.message === "Cancelled") throw e;
      // Planner unavailable — proceed to invoke so a local/network glitch doesn't
      // block the SE; the backend still validates the skill id.
    }
  }
  const res = await api("/api/invoke", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (res.blocked && res.permissions) {
    const summary = permissionSummary(res.permissions);
    if (!confirm(`This skill requires the following permissions:\n${summary}\n\nProceed?`)) {
      throw new Error("Cancelled");
    }
    return invokeWithPlan({ ...payload, approve_permissions: true }, alreadyConfirmed);
  }
  return res;
}

function permissionSummary(perms) {
  const parts = [];
  if (perms.write) parts.push("writes a file to the customer workspace");
  if (perms.git) parts.push("runs git commands");
  if (perms.shell) parts.push("runs shell commands");
  return parts.length ? "• " + parts.join("\n• ") : "• performs this action";
}

// Only allow a small set of link schemes in the markdown reader. `javascript:`,
// `data:`, `blob:`, and other arbitrary protocols are replaced with `#` so user
// content cannot execute in the browser. Relative paths pass through.
const safeHref = (url) => {
  if (!url) return "#";
  try {
    const proto = new URL(url, "http://x.invalid").protocol;
    if (proto && !["http:", "https:", "mailto:", "tel:"].includes(proto)) return "#";
    return url;
  } catch { return "#"; }
};

// "prep-call" → "PREP CALL", "deal-assessment" → "DEAL ASSESSMENT"
const prettySkill = (id) => (id || "").replace(/[-_]/g, " ").toUpperCase();

// Concise output label from a filename. Filenames are
// "<skill>-YYYY-MM-DD[-Descriptor][-vN].md" — strip the extension, the redundant
// skill prefix (already shown as the bold line), and reformat the date, leaving
// "<Descriptor> · Mon D (vN)". Falls back to just the date when there's no descriptor.
const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function conciseOutputName(filename, skill) {
  let s = (filename || "").replace(/\.[a-z0-9]+$/i, "");        // drop extension
  if (skill && s.startsWith(skill + "-")) s = s.slice(skill.length + 1);  // drop skill prefix
  // Drop any remaining leading tokens before the date (e.g. internal-prep files
  // saved as "ae-sync-YYYY-MM-DD" — the sub-type prefix is redundant noise here).
  const dm = s.match(/(\d{4})-(\d{2})-(\d{2})-?(.*)$/);          // find the date anywhere
  if (!dm) return s.replace(/-/g, " ") || filename;             // no date → best effort
  const [, y, mo, d, rest] = dm;
  const date = `${MONTHS[+mo - 1] || mo} ${+d}`;
  let ver = "";
  let desc = rest;
  const vm = desc.match(/-?v(\d+)$/i);                            // pull trailing -vN
  if (vm) { ver = ` (v${vm[1]})`; desc = desc.slice(0, vm.index); }
  desc = desc.replace(/-/g, " ").trim();
  return desc ? `${desc} · ${date}${ver}` : `${date}${ver}`;
}

// ---- Output download (PDF via browser print, MD as raw file) -------------
// `path` is URI-encoded relative-to-CUSTOMERS_DIR (as stored on .out-item).
async function fetchOutputText(path) {
  return api("/api/output?path=" + encodeURIComponent(decodeURIComponent(path)));
}
function baseName(path) {
  const p = decodeURIComponent(path);
  return p.slice(p.lastIndexOf("/") + 1) || "output.md";
}
// Reconstruct the live follow-up thread as appendable markdown. Only real
// answers (⚡ quick / 🔧 deep) — skill-invocation status cards (⚙️/✓/✕/⚠️) are
// status, not content, so they're skipped. Empty string when there's nothing.
function qaThreadMarkdown() {
  const items = document.querySelectorAll("#doc-qa .qa-item");
  const blocks = [];
  items.forEach((it) => {
    const tag = (it.querySelector(".qa-tag")?.textContent || "").trim();
    if (tag === "⚙️" || tag === "✓" || tag === "✕" || tag === "⚠️") return;
    const q = it.querySelector(".qa-q")?.textContent?.trim();
    // Raw markdown stashed by askThread; fall back to rendered text.
    const a = (it.dataset.answerMd || it.querySelector(".qa-body")?.innerText || "").trim();
    if (q && a) blocks.push(`### Q: ${q}\n\n${a}`);
  });
  if (!blocks.length) return "";
  return `\n\n---\n\n## Follow-up Q&A\n\n${blocks.join("\n\n")}\n`;
}
// Direct download of the raw .md source file. `extraMd` (optional) appends the
// follow-up Q&A thread beneath the document — "Download with Q&A".
async function downloadMd(path, extraMd = "") {
  const text = (await fetchOutputText(path)) + (extraMd || "");
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = baseName(path);
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// Clean PDF export — server-side render (headless Chrome) so page breaks and
// table rows behave and there's no browser print chrome (about:blank/timestamp).
// The browser's window.print() path had none of that control. `path` is already
// encodeURIComponent'd (from data-path). `extraMd` (optional) appends the
// follow-up Q&A thread; when present we POST the markdown rather than GET-by-path.
async function downloadPdf(path, extraMd = "") {
  let res;
  if (extraMd) {
    res = await fetch("/api/output/pdf", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ path: decodeURIComponent(path), append_md: extraMd }),
    });
  } else {
    res = await fetch("/api/output/pdf?path=" + path);
  }
  if (!res.ok) {
    let msg = "PDF export failed.";
    try { msg = (await res.json()).detail || msg; } catch {}
    alert(msg);
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = baseName(path).replace(/\.md$/i, "") + ".pdf";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// Export to a self-contained internal.airbyte.ai (rs-group) HTML page. Same
// design system as coverage-handoff but generic across skills — each H2 section
// renders as a card in the doc's own order. The SE drops the file into the
// internal repo and PRs it (no auto-push). `path` is already encodeURIComponent'd.
async function downloadInternalHtml(path) {
  const res = await fetch("/api/output/internal-html?path=" + path);
  if (!res.ok) {
    let msg = "Internal HTML export failed.";
    try { msg = (await res.json()).detail || msg; } catch {}
    alert(msg);
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = baseName(path).replace(/\.md$/i, "") + ".html";
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// Build a ⬇ download control with a PDF/MD popup menu. `cls` lets callers add
// position modifiers. Wire it up afterward with wireDownloadMenus(root).
// `label` (optional) renders a labeled "Export ▾" trigger instead of the icon-only
// ⬇ — used in the doc-reader header so export reads as a document-level action
// (per UI/UX guidance: don't use icon-only for a primary document action). The
// compact opp-row menus keep the icon-only ⬇ to stay dense.
function downloadMenuHtml(path, cls = "", label = "", includeDelete = true) {
  // The doc reader (dl-menu-doc) also offers "with Q&A" exports that append the
  // follow-up thread. Other menus (opp list rows) have no thread, so they don't.
  const withQa = cls.includes("dl-menu-doc")
    ? `<button class="menu-item dl-pdf-qa">Download PDF with Q&amp;A</button>
       <button class="menu-item dl-md-qa">Download Markdown with Q&amp;A</button>`
    : "";
  const trigger = label
    ? `<button class="dl-btn dl-btn-labeled" title="Options" aria-label="Options">${esc(label)} <span class="dl-caret">▾</span></button>`
    : `<button class="dl-btn dl-btn-dots" title="Options" aria-label="Options">⋮</button>`;
  const deleteItem = includeDelete ? `<button class="menu-item danger dl-del">Delete output</button>` : "";
  return `<span class="dl-menu ${cls}" data-path="${path}">
    ${trigger}
    <div class="dropdown-menu hidden">
      <button class="menu-item dl-pdf">Download PDF</button>
      <button class="menu-item dl-md">Download Markdown (.md)</button>
      <button class="menu-item dl-html">Export to internal HTML</button>
      ${withQa}
      ${deleteItem}
    </div></span>`;
}
// Move an output to _trash/ (recoverable). Resolves true on success.
async function deleteOutput(path) {
  await api("/api/output?path=" + encodeURIComponent(decodeURIComponent(path)), { method: "DELETE" });
  return true;
}
// `onDeleted(path)` (optional) lets the caller react after a delete — opp row
// refreshes the list; the reader navigates back to the opp.
function wireDownloadMenus(root, onDeleted) {
  const closeAll = () => root.querySelectorAll(".dl-menu .dropdown-menu").forEach((m) => m.classList.add("hidden"));
  root.querySelectorAll(".dl-menu").forEach((wrap) => {
    const path = wrap.dataset.path;
    const btn = wrap.querySelector(".dl-btn");
    const menu = wrap.querySelector(".dropdown-menu");
    btn.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      const open = !menu.classList.contains("hidden");
      closeAll();
      // reset a previously-armed Delete each time the menu opens
      const d = wrap.querySelector(".dl-del");
      if (d) { d.classList.remove("armed"); d.disabled = false; d.textContent = "Delete output"; }
      if (!open) menu.classList.remove("hidden");
    };
    wrap.querySelector(".dl-pdf").onclick = (e) => { e.preventDefault(); e.stopPropagation(); closeAll(); downloadPdf(path); };
    wrap.querySelector(".dl-md").onclick = (e) => { e.preventDefault(); e.stopPropagation(); closeAll(); downloadMd(path); };
    wrap.querySelector(".dl-html").onclick = (e) => { e.preventDefault(); e.stopPropagation(); closeAll(); downloadInternalHtml(path); };
    // "with Q&A" exports (doc reader only) — append the live thread. If the
    // thread is empty, fall back to the plain export so the file is never broken.
    wrap.querySelector(".dl-pdf-qa")?.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation(); closeAll(); downloadPdf(path, qaThreadMarkdown());
    });
    wrap.querySelector(".dl-md-qa")?.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation(); closeAll(); downloadMd(path, qaThreadMarkdown());
    });
    // Delete is two-click: first click arms ("Delete — confirm?"), second deletes.
    const del = wrap.querySelector(".dl-del");
    if (del) del.onclick = async (e) => {
      e.preventDefault(); e.stopPropagation();
      if (!del.classList.contains("armed")) {
        del.classList.add("armed"); del.textContent = "Delete — click to confirm";
        return;
      }
      del.textContent = "Deleting…"; del.disabled = true;
      try {
        await deleteOutput(path);
        closeAll();
        if (onDeleted) onDeleted(path);
      } catch (err) {
        del.disabled = false; del.classList.remove("armed");
        del.textContent = "Delete failed — retry";
      }
    };
  });
}
// Close any open download menu on outside click (registered once).
document.addEventListener("click", () => {
  document.querySelectorAll(".dl-menu .dropdown-menu:not(.hidden)").forEach((m) => {
    m.classList.add("hidden");
    const d = m.querySelector(".dl-del");  // disarm a primed Delete on close
    if (d) { d.classList.remove("armed"); d.disabled = false; d.textContent = "Delete output"; }
  });
});

// "2026-06-22 21:20" → "June 22, 2026"
function longDate(ymd) {
  const m = (ymd || "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return ymd || "";
  const months = ["January","February","March","April","May","June","July","August","September","October","November","December"];
  return `${months[+m[2] - 1]} ${+m[3]}, ${m[1]}`;
}

// Slugify a heading into an anchor id (lowercase, strip inline markdown,
// non-alphanumeric → "-"). De-dupe is handled per-render by the caller.
function slugify(s) {
  return (s || "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/==([^=]+)==/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

// Strip inline markdown to plain text (for TOC sidebar labels).
function stripInline(s) {
  return (s || "")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/==([^=]+)==/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/\*([^*]+)\*/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
}

// "ran 8 min ago" from an epoch-seconds timestamp.
function relTime(epochSec) {
  const diff = Math.max(0, Date.now() / 1000 - epochSec);
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} hr ago`;
  return `${Math.floor(diff / 86400)} d ago`;
}

// Format a timestamp (epoch seconds or ISO-ish) as a full date for tooltips.
function fullDate(epochSec) {
  if (!epochSec) return "";
  const d = new Date(epochSec * 1000);
  if (isNaN(d)) return "";
  return d.toLocaleString();
}

// Build per-account / per-opportunity activity summaries from the jobs list.
function activityByAccount(jobs) {
  const map = {};
  for (const j of jobs || []) {
    const a = j.account;
    if (!a) continue;
    if (!map[a]) map[a] = { running: 0, lastRun: null };
    if (j.status === "running") {
      map[a].running += 1;
      continue;
    }
    const t = j.finished_at || 0;
    if (!map[a].lastRun || t > map[a].lastRun.finished_at) {
      map[a].lastRun = { ok: j.ok, finished_at: t, status: j.status, stderr: j.stderr };
    }
  }
  return map;
}

function activityByOpp(jobs, account) {
  const map = {};
  for (const j of jobs || []) {
    if (j.account !== account) continue;
    const key = j.opp_slug || "__account";
    if (!map[key]) map[key] = { running: 0, lastRun: null };
    if (j.status === "running") {
      map[key].running += 1;
      continue;
    }
    const t = j.finished_at || 0;
    if (!map[key].lastRun || t > map[key].lastRun.finished_at) {
      map[key].lastRun = { ok: j.ok, finished_at: t, status: j.status, stderr: j.stderr };
    }
  }
  return map;
}

function renderActivity(activity, size = "small") {
  if (!activity) return "";
  if (activity.running) {
    return `<span class="activity activity--running activity--${size}" title="${activity.running} skill${activity.running > 1 ? "s" : ""} running for this account"><span class="pulse"></span>${activity.running} running</span>`;
  }
  if (activity.lastRun && activity.lastRun.ok === false) {
    const reason = activity.lastRun.stderr ? activity.lastRun.stderr.split("\n")[0].slice(0, 80) : "";
    return `<span class="activity activity--error activity--${size}" title="Last run failed${reason ? ": " + esc(reason) : ""}">${size === "small" ? "●" : "Last run failed"}</span>`;
  }
  return "";
}

// Compact, actionable empty-state box.
function emptyBox({ icon = "⊘", title, body = "", actions = "" }) {
  return `<div class="empty-box">
    <div class="empty-icon">${icon}</div>
    <div class="empty-title">${esc(title)}</div>
    ${body ? `<div class="empty-body">${body}</div>` : ""}
    ${actions ? `<div class="empty-actions">${actions}</div>` : ""}
  </div>`;
}

// De-duplicate persistence-warning toasts across polls and pages.
const _persistWarningsShown = new Set();

function warnPersistence(id, message) {
  if (!message || _persistWarningsShown.has(id)) return;
  _persistWarningsShown.add(id);
  showToast(message, "warn");
}

// Poll a background job until it finishes. `onTick` gets the job snapshot each
// poll; resolves with the final job. Polling is independent of any page — if
// you navigate away and the element is gone, onTick simply no-ops.
async function pollJob(jobId, onTick) {
  while (true) {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`).catch(() => null);
    if (!job) return null;
    if (job.persistence_warning) warnPersistence(`job:${jobId}`, job.persistence_warning);
    if (onTick) await onTick(job);
    if (job.status !== "running") return job;
    await new Promise((r) => setTimeout(r, 2000));
  }
}

// ── Global skill-completion toasts ─────────────────────────────────────────
// A skill run keeps executing server-side even after you leave the opp/chat
// page. `trackJob` owns a page-independent poller: register a running job once
// (from any invoke entry point), and when it finishes a toast slides in top-right
// — "✓ <skill> for <opp> is ready" with an Open button + an X. Session-only
// (in-memory `_tracked` set dedupes; a hard reload forgets in-flight jobs — the
// output still lands on the opp page).
const _tracked = new Set();

// `ctx` = { account, slug, oppName, skill } — everything the toast + deep-link need.
function trackJob(jobId, ctx) {
  if (!jobId || _tracked.has(jobId)) return;
  _tracked.add(jobId);
  pollJob(jobId).then((job) => {
    _tracked.delete(jobId);
    if (!job) return;  // job vanished (server restart) — nothing to notify
    showSkillToast(job, ctx);
  }).catch(() => _tracked.delete(jobId));
}

// Find the newest output file for a finished skill and open it in the reader.
// The job object has no output path, so re-list the opp's outputs and pick the
// most recent one for that skill (falls back to the newest of any skill).
async function openSkillOutput(ctx) {
  const outs = await api(`/api/accounts/${encodeURIComponent(ctx.account)}/outputs?opp=${encodeURIComponent(ctx.slug)}`).catch(() => []);
  if (!outs || !outs.length) { location.hash = `#/opp/${encodeURIComponent(ctx.account)}/${encodeURIComponent(ctx.slug)}/${encodeURIComponent(ctx.oppName)}`; return; }
  const bySkill = outs.filter((o) => !ctx.skill || o.skill === ctx.skill);
  const pick = (bySkill.length ? bySkill : outs).sort((a, b) => (b.mtime || 0) - (a.mtime || 0))[0];
  navOpenOutput(pick.path, `${prettySkill(pick.skill)} — ${pick.filename}`, ctx);
}

// Render one completion toast. Auto-dismisses after ~10s; X dismisses now; Open
// deep-links to the freshly-generated output. Errors get a red toast, no Open.
function showSkillToast(job, ctx) {
  const wrap = document.getElementById("toast-container");
  if (!wrap) return;
  const ok = job.ok;
  const skill = prettySkill(job.skill || ctx.skill || "run");
  const opp = ctx.oppName || job.opportunity || ctx.account || "";
  const el = document.createElement("div");
  el.className = `toast ${ok ? "ok" : "err"}`;
  el.innerHTML =
    `<span class="toast-icon">${ok ? "✓" : "✕"}</span>`
    + `<div class="toast-body">`
    + `<div class="toast-title">${esc(skill)} ${ok ? "is ready" : "failed"}</div>`
    + `<div class="toast-sub">${esc(opp)}</div></div>`
    + (ok ? `<button class="toast-open">Open</button>` : "")
    + `<button class="toast-x" aria-label="Dismiss">✕</button>`;
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));

  let timer = null;
  const close = () => {
    if (timer) clearTimeout(timer);
    el.classList.remove("show");
    setTimeout(() => el.remove(), 250);
  };
  el.querySelector(".toast-x").onclick = close;
  const openBtn = el.querySelector(".toast-open");
  if (openBtn) openBtn.onclick = () => { close(); openSkillOutput(ctx); };
  timer = setTimeout(close, 10000);
}

// Generic message toast — used for inline UI confirmations (e.g. feedback saved).
function showToast(message, kind = "ok") {
  const wrap = document.getElementById("toast-container");
  if (!wrap) return;
  const el = document.createElement("div");
  el.className = `toast ${kind === "err" ? "err" : "ok"}`;
  el.innerHTML =
    `<span class="toast-icon">${kind === "err" ? "✕" : "✓"}</span>`
    + `<div class="toast-body"><div class="toast-title">${esc(message)}</div></div>`
    + `<button class="toast-x" aria-label="Dismiss">✕</button>`;
  wrap.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  const close = () => { el.classList.remove("show"); setTimeout(() => el.remove(), 250); };
  el.querySelector(".toast-x").onclick = close;
  setTimeout(close, 8000);
}

// Shared Markdown -> HTML renderer. The server uses `md_render.py` to produce
// identical, sanitized HTML for the web reader, PDF export, and internal HTML
// export. The browser calls `POST /api/output/render` and then applies the
// presentation-only `.md-*` / `.callout-*` classes the reader CSS expects.

async function renderMarkdown(md) {
  const res = await api("/api/output/render", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ md: md || "" }),
  });
  return res.html;
}

// Add the CSS classes the web reader DOM restructuring expects, plus safe
// link targets. Keep this in sync with `md_render.py` output.
function addMdClasses(html) {
  // Admonitions from the backend are `<div class="admon admon-{type}">`;
  // the reader needs `.callout*` classes for styling and risk extraction.
  html = html.replace(
    /<div class="admon admon-(\w+)"><div class="admon-label">([\s\S]*?)<\/div><div class="admon-body">([\s\S]*?)<\/div><\/div>/g,
    '<div class="admon callout callout-$1 admon-$1"><div class="admon-label callout-title">$2</div><div class="admon-body callout-body">$3</div></div>'
  );
  // Headings: preserve id from the shared renderer and add reader classes.
  html = html.replace(/<h([1-6])([^>]*)>/g, '<h$1 class="md-h md-h$1"$2>');
  // Paragraphs, lists, tables, code blocks, hr.
  html = html.replace(/<p>/g, '<p class="md-p">');
  html = html.replace(/<ul>/g, '<ul class="md-list">');
  html = html.replace(/<ol>/g, '<ol class="md-list">');
  html = html.replace(/<table>/g, '<table class="md-table">');
  html = html.replace(/<pre>/g, '<pre class="md-pre">');
  html = html.replace(/<hr\s*\/?>/g, '<hr class="md-hr" />');
  // Highlights and checkbox lists.
  html = html.replace(/<mark>/g, '<mark class="md-key">');
  html = html.replace(/<li>(☐|☑)\s+([\s\S]*?)<\/li>/g, (m, box, text) => {
    const done = box === '☑';
    return `<li class="md-check"><span class="md-cbox${done ? ' done' : ''}" aria-hidden="true">${done ? '✓' : ''}</span><span class="md-check-text">${text}</span></li>`;
  });
  // Open external (and relative anchor) links in a new tab safely.
  html = html.replace(/<a href="([^"]*)"([^>]*)>/g, (m, href, rest) => {
    if (rest.includes('target=')) return m;
    if (rest.includes('rel=')) return `<a href="${href}"${rest} target="_blank">`;
    return `<a href="${href}"${rest} target="_blank" rel="noopener">`;
  });
  return html;
}

// Markdown -> HTML for the output reader. The optional `toc` array is populated
// from the rendered headings so the sidebar and in-doc links stay in sync.
async function mdToHtml(md, toc) {
  const body = addMdClasses(await renderMarkdown(md));
  if (toc && Array.isArray(toc)) {
    const tmp = document.createElement("div");
    tmp.innerHTML = body;
    toc.length = 0;
    tmp.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach((h) => {
      toc.push({ level: parseInt(h.tagName[1]), text: h.textContent.trim(), id: h.id });
    });
  }
  return body;
}

let SKILLS = [];
let SKILLS_HELP = {}; // id -> rich help entry (description, triggers, prerequisites, output)

// Does this chat message read as a command to RUN a skill (vs. a question about
// the doc)? Returns the resolved skill {id,label} if so, else null. Gated on a
// leading invoke-verb so "does a connector exist for X?" stays a question while
// "run connector feasibility" invokes. Resolves the name against SKILLS so the
// _DEEP_HINTS keywords (connector/feasib/…) can't hijack a genuine invoke.
const _INVOKE_RE = /^\s*(?:please\s+)?(run|generate|create|invoke|kick\s*off|start|do)\s+(?:a|an|the)?\s*(.+?)(?:\s+skill)?\s*$/i;
function detectSkillInvocation(q) {
  const m = (q || "").match(_INVOKE_RE);
  if (!m || !SKILLS.length) return null;
  const phrase = m[2].toLowerCase().replace(/[^a-z0-9\s-]/g, " ").trim();
  if (!phrase) return null;
  const words = phrase.split(/\s+/).filter((w) => w.length >= 3);
  if (!words.length) return null;
  let best = null, bestScore = 0;
  for (const s of SKILLS) {
    const help = SKILLS_HELP[s.id] || {};
    const idWords = s.id.replace(/[-_]/g, " ");
    const hay = [s.id, idWords, s.label, help.description, ...(help.triggers || [])].join(" ").toLowerCase();
    let score = 0;
    for (const w of words) if (hay.includes(w)) score += (s.label.toLowerCase().includes(w) || idWords.includes(w)) ? 2 : 1;
    if (score > bestScore) { bestScore = score; best = s; }
  }
  // Require a real match (≥2 = at least one strong id/label hit) to avoid
  // misfiring on "run the numbers" etc. — falls through to Q&A otherwise.
  return bestScore >= 2 ? best : null;
}

function setCrumbs(parts) {
  crumbs.innerHTML = parts
    .map((p, i) => (p.href ? `<a href="${p.href}">${esc(p.label)}</a>` : `<span>${esc(p.label)}</span>`))
    .join('<span class="sep">/</span>');
}

// Build the leading crumbs for an account: Team → <Owner Name> → Account.
// The owner crumb links back to that member's accounts page. Falls back to
// just Team → Account if the owner can't be resolved.
async function accountCrumbs(account) {
  const crumbsArr = [{ label: "Team", href: "#/" }];
  try {
    const meta = await api(`/api/accounts/${encodeURIComponent(account)}`);
    if (meta.owner) {
      const members = await api("/api/members").catch(() => []);
      const m = members.find((x) => x.id === meta.owner);
      crumbsArr.push({ label: m ? m.name : meta.owner, href: `#/member/${encodeURIComponent(meta.owner)}` });
    }
  } catch { /* no owner / offline — Team → Account is fine */ }
  return crumbsArr;
}

// Format a short label for an overview attention/recent item.
function overviewSkillLabel(skill) {
  return prettySkill(skill || "skill").toLowerCase();
}

function overviewAttentionTitle(item) {
  const skill = overviewSkillLabel(item.skill);
  if (item.type === "failure") return `${skill} failed`;
  if (item.type === "long-running") return `${skill} still running`;
  if (item.type === "running") return `${skill} running`;
  if (item.type === "review") return `${skill} needs review`;
  if (item.type === "stale") return `${esc(item.account)} — no activity`;
  return skill;
}

function overviewAttentionSubtitle(item) {
  const parts = [];
  if (item.opp_name && item.opp_name !== item.account) parts.push(esc(item.opp_name));
  if (item.account) parts.push(esc(item.account));
  if (item.duration_min != null) parts.push(`${item.duration_min} min`);
  if (item.error) parts.push(esc(item.error));
  else if (item.status) parts.push(esc(item.status.replace("unvalidated", "needs review")));
  if (item.when) parts.push(relTime(item.when));
  return parts.join(" · ");
}

function overviewRecentTitle(item) {
  const skill = overviewSkillLabel(item.skill);
  if (item.type === "output") return `Generated ${skill}`;
  if (item.type === "job_started") return `Started ${skill}`;
  if (item.type === "job_done") return `${skill} completed`;
  if (item.type === "job_error") return `${skill} failed`;
  if (item.type === "job_recovered") return `${skill} recovered (interrupted)`;
  return skill;
}

function overviewRecentSubtitle(item) {
  const parts = [];
  if (item.opp_name && item.opp_name !== item.account) parts.push(esc(item.opp_name));
  if (item.account) parts.push(esc(item.account));
  if (item.filename) parts.push(esc(item.filename));
  if (item.when) parts.push(relTime(item.when));
  return parts.join(" · ");
}

function summaryCard(value, label, level = "neutral") {
  return `<div class="summary-item${level ? " summary-item--" + level : ""}">
    <span class="summary-value">${esc(String(value ?? "—"))}</span>
    <span class="summary-label">${esc(label)}</span>
  </div>`;
}

// ---- Page: members --------------------------------------------------------
async function pageMembers() {
  setCrumbs([{ label: "Team" }]);
  const data = await api("/api/overview").catch(() => null);
  const fallbackMembers = await api("/api/members").catch(() => []);
  const members = data?.members || fallbackMembers.map((m) => ({ ...m, account_count: 0, output_count: 0, running_jobs: 0, recent_failures: 0, needs_review: 0, last_activity_ts: 0 }));
  const summary = data?.summary || {};
  const attention = data?.attention || [];
  const recent = data?.recent || [];
  const empty = data?.empty || {};

  view.innerHTML = `
    <div class="row">
      <div><h1>Solutions Team</h1><p class="sub">Operational overview — where work is happening now.</p></div>
      <button class="primary small" id="add-member-btn">+ Add Team Member</button>
    </div>
    <div id="add-member-form" class="add-member-form hidden">
      <input id="m-name" type="text" placeholder="Full name *" />
      <input id="m-role" type="text" placeholder="Title (e.g. Solutions Engineer)" />
      <input id="m-email" type="text" placeholder="Email" />
      <button class="primary small" id="m-save">Add</button>
      <button class="ghost small" id="m-cancel">Cancel</button>
    </div>

    <div class="summary-bar">
      ${summaryCard(summary.members ?? members.length, "Members")}
      ${summaryCard(summary.active_accounts, "Accounts")}
      ${summaryCard(summary.opportunities, "Opportunities")}
      ${summaryCard(summary.outputs, "Outputs")}
      ${summaryCard(summary.running_jobs, "Running", summary.running_jobs ? "info" : "neutral")}
      ${summaryCard(summary.recent_failures, "Failed", summary.recent_failures ? "error" : "neutral")}
      ${summaryCard(summary.needs_review, "Needs review", summary.needs_review ? "warn" : "neutral")}
    </div>

    <section class="overview-section">
      <h2>Needs attention</h2>
      <div id="attention-list" class="attention-list">
        ${empty.attention ? emptyBox({ icon: "✓", title: "Nothing needs attention", body: "No running jobs, recent failures, or outputs awaiting review." }) : attention.map((item) => `
          <a class="attention-item attention-item--${item.level}" href="${esc(item.href)}">
            <span class="attention-dot" aria-hidden="true"></span>
            <span class="attention-main">
              <span class="attention-title">${overviewAttentionTitle(item)}</span>
              <span class="attention-sub">${overviewAttentionSubtitle(item)}</span>
            </span>
            <span class="attention-when" title="${fullDate(item.when)}">${item.when ? relTime(item.when) : ""}</span>
          </a>`).join("")}
      </div>
    </section>

    <section class="overview-section">
      <h2>Recent activity</h2>
      <div id="recent-list" class="recent-list">
        ${empty.recent ? emptyBox({ icon: "⊘", title: "No recent activity", body: "Run a skill or generate an output to see activity here." }) : recent.map((item) => `
          <div class="recent-item">
            <span class="recent-dot recent-dot--${item.type.startsWith("job_error") ? "error" : (item.type === "output" && item.needs_review ? "warn" : (item.type.startsWith("job") ? "info" : "neutral"))}" aria-hidden="true"></span>
            <span class="recent-main">
              <span class="recent-title">${overviewRecentTitle(item)}</span>
              <span class="recent-sub">${overviewRecentSubtitle(item)}</span>
            </span>
            <a class="ghost small" href="${esc(item.href)}">Open</a>
          </div>`).join("")}
      </div>
    </section>

    <section class="overview-section">
      <h2>Team members</h2>
      ${empty.members ? emptyBox({ icon: "⊘", title: "No team members", body: "Add a team member to get started.", actions: `<button class="primary small" id="empty-add-member">Add team member</button>` }) : ""}
      <div class="member-grid" id="member-grid">
        ${members.map((m) => {
          const meta = [m.role, m.email].filter(Boolean).join(" · ");
          const last = m.last_activity_ts ? relTime(m.last_activity_ts) : (m.last_output ? relTime(m.last_output.mtime) : "no activity");
          const lastTitle = m.last_activity_ts ? fullDate(m.last_activity_ts) : (m.last_output ? fullDate(m.last_output.mtime) : "");
          return `
          <a class="member-card" href="#/member/${encodeURIComponent(m.id)}">
            <div class="member-card-main">
              <h3>${esc(m.name)}</h3>
              <div class="meta">${meta ? esc(meta) : "&nbsp;"}</div>
            </div>
            <div class="member-card-stats">
              <span class="stat"><strong>${m.account_count || 0}</strong> accounts</span>
              <span class="stat"><strong>${m.output_count || 0}</strong> outputs</span>
              ${m.running_jobs ? `<span class="stat stat--info"><strong>${m.running_jobs}</strong> running</span>` : ""}
              ${m.recent_failures ? `<span class="stat stat--error"><strong>${m.recent_failures}</strong> failed</span>` : ""}
              ${m.needs_review ? `<span class="stat stat--warn"><strong>${m.needs_review}</strong> review</span>` : ""}
            </div>
            <div class="member-card-activity" title="${esc(lastTitle)}">Last activity ${esc(last)}</div>
          </a>`;
        }).join("")}
      </div>
    </section>

    ${empty.accounts ? `<section class="overview-section">${emptyBox({ icon: "⊘", title: "No accounts yet", body: "Sync from Salesforce or add an account manually to start tracking work.", actions: `<button class="primary small" id="empty-add-account">Add account</button>` })}</section>` : ""}`;

  const form = document.getElementById("add-member-form");
  const addBtn = document.getElementById("add-member-btn");
  const emptyAddMember = document.getElementById("empty-add-member");
  const emptyAddAccount = document.getElementById("empty-add-account");

  const toggleForm = () => {
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) document.getElementById("m-name").focus();
  };
  addBtn.onclick = toggleForm;
  if (emptyAddMember) emptyAddMember.onclick = toggleForm;
  if (emptyAddAccount) emptyAddAccount.onclick = () => location.hash = `#/member/me`;

  document.getElementById("m-cancel").onclick = () => form.classList.add("hidden");
  document.getElementById("m-save").onclick = async () => {
    const name = document.getElementById("m-name").value.trim();
    if (!name) { alert("Name is required"); return; }
    try {
      await api("/api/members", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({
          name,
          role: document.getElementById("m-role").value.trim() || null,
          email: document.getElementById("m-email").value.trim() || null,
        }) });
      pageMembers(); // refresh
    } catch (e) { alert("Could not add member: " + e.message); }
  };
}

// ---- Page: member's accounts ---------------------------------------------
// `tab` is "active" (default), "archived", or "trash"
let _sort = (() => { try { return JSON.parse(localStorage.getItem("se-hub-sort")) || { key: "updated", dir: -1 }; } catch { return { key: "updated", dir: -1 }; } })();

async function pageMember(memberId, tab = "active") {
  const members = await api("/api/members");
  const m = members.find((x) => x.id === memberId) || { id: memberId, name: memberId };
  setCrumbs([{ label: "Team", href: "#/" }, { label: m.name }]);
  const data = await api(`/api/members/${encodeURIComponent(memberId)}/accounts`);
  const active = data.active || [];
  const archived = data.archived || [];
  const trash = await api("/api/trash").catch(() => []);
  let showing = tab === "trash" ? trash : (tab === "archived" ? archived : active);

  // SFDC enrichment fetched up front so it's sortable (small, fast, cached per render)
  let sfdc = {};
  if (tab !== "trash" && showing.length) {
    sfdc = await api("/api/sfdc/stage-amount", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ accounts: showing.map((a) => a.name) }) }).catch(() => ({}));
    showing = showing.map((a) => ({ ...a, _sfdc: sfdc[a.name] || {} }));
  }

  // Activity summary: running jobs and the latest finished job per account.
  const jobs = tab === "trash" ? [] : await api("/api/jobs").catch(() => []);
  const activity = activityByAccount(jobs);

  // ── Sorting ──────────────────────────────────────────────────────────
  const sortVal = (a, key) => {
    switch (key) {
      case "name": return a.name.toLowerCase();
      case "stage": return (a._sfdc?.stage_num ? parseFloat(a._sfdc.stage_num) : -1);
      case "amount": return (a._sfdc?.amount ?? -1);
      case "close": return (a._sfdc?.close_date || "");
      case "type": return (a._sfdc?.type || "").toLowerCase();
      case "ae": return (a._sfdc?.ae || "").toLowerCase();
      case "updated": return a.last_updated_ts || 0;
      case "outputs": return a.output_count || 0;
      case "owner": return (a.owner || "").toLowerCase();
      default: return 0;
    }
  };
  if (tab !== "trash") {
    showing = [...showing].sort((x, y) => {
      const vx = sortVal(x, _sort.key), vy = sortVal(y, _sort.key);
      if (vx < vy) return -1 * _sort.dir;
      if (vx > vy) return 1 * _sort.dir;
      return 0;
    });
  }
  const sortArrow = (key) => _sort.key === key ? (_sort.dir === 1 ? " ▲" : " ▼") : "";

  // ── Row rendering ────────────────────────────────────────────────────
  const fmtAmt = (n) => (n || n === 0) ? "$" + Number(n).toLocaleString() : '<span class="muted">—</span>';
  // Stage cell — render red when the opportunity is closed (won or lost).
  const stageCell = (s) => {
    const txt = s?.stage_num ? esc(s.stage_num) : (s?.stage ? esc(s.stage) : null);
    if (!txt) return '<span class="muted">—</span>';
    return s?.is_closed ? `<span class="stage-closed">${txt}</span>` : txt;
  };
  const typeCell = (s) => s?.type ? esc(s.type) : null;
  const dateCell = (d) => d ? esc(d) : null;
  const aeCell = (s) => s?.ae ? esc(s.ae) : null;

  const accountSub = (a) => {
    const parts = [fmtAmt(a._sfdc?.amount), typeCell(a._sfdc), aeCell(a._sfdc)].filter(Boolean);
    return parts.length ? `<span class="acct-sub">${parts.join(" · ")}</span>` : '';
  };
  const accountUpdated = (a) => {
    if (!a.last_updated_ts) return '<span class="acct-updated muted">—</span>';
    return `<span class="acct-updated" title="${fullDate(a.last_updated_ts)}">${relTime(a.last_updated_ts)}</span>`;
  };

  const acctRow = (a, isArchived) => {
    const act = activity[esc(a.name)];
    const ownerBadge = a.owner === memberId
      ? '<span class="badge owned">you</span>'
      : `<span class="badge">${esc(a.owner || "—")}</span>`;
    return `
    <div class="acct-row${isArchived ? " is-archived" : ""}" data-acct="${esc(a.name)}">
      <div class="acct-row-menu">
        <button class="kebab" aria-label="Account actions">⋮</button>
        <div class="dropdown-menu hidden">
          ${isArchived
            ? `<button class="menu-item unarchive-btn" data-acct="${esc(a.name)}">Unarchive</button>`
            : `<button class="menu-item archive-btn" data-acct="${esc(a.name)}">Archive</button>`}
          <button class="menu-item danger delete-btn" data-acct="${esc(a.name)}">Delete…</button>
        </div>
      </div>
      <label class="acct-check"><input type="checkbox" class="row-check" data-acct="${esc(a.name)}" /></label>
      <a href="#/account/${encodeURIComponent(a.name)}" class="acct-row-main">
        <div class="acct-cell acct-cell-main">
          <span class="acct-name-wrap"><span class="acct-name">${esc(a.name)}</span>${renderActivity(act, "small")}</span>
          ${accountSub(a)}
        </div>
        <div class="acct-cell acct-cell-stage">
          <span class="acct-stage">${stageCell(a._sfdc)}</span>
          <span class="acct-sub">${dateCell(a._sfdc?.close_date) || "<span class=\"muted\">—</span>"}</span>
        </div>
        <div class="acct-cell acct-cell-updated">${accountUpdated(a)}</div>
        <div class="acct-cell acct-cell-outputs"><span class="acct-out-count${a.output_count ? "" : " muted"}">${a.output_count || "—"}</span></div>
        <div class="acct-cell acct-cell-owner">${ownerBadge}${isArchived ? ' <span class="badge">archived</span>' : ""}</div>
      </a>
      <button class="acct-expand" data-acct="${esc(a.name)}" aria-label="Show opportunities" title="Show opportunities">▸</button>
    </div>
    <div class="opp-drawer hidden" data-drawer="${esc(a.name)}"></div>`;
  };

  const trashRow = (t) => `
    <div class="acct-row trash-row">
      <div class="acct-row-menu"><button class="ghost small restore-btn" data-tid="${esc(t.trash_id)}">Restore</button></div>
      <div class="acct-row-main no-link">
        <span class="acct-name">${esc(t.name)}</span>
        <span class="acct-col" style="grid-column: span 4;"><span class="muted">deleted</span> ${esc(t.deleted_at)}</span>
      </div>
    </div>`;

  const renderRow = tab === "trash" ? trashRow : (a) => acctRow(a, tab === "archived");

  const emptyActions = {
    active: `<button class="primary small" id="empty-add">Add account manually</button>`,
    archived: "",
    trash: "",
  };
  const emptyMsg = {
    active: { icon: "⊘", title: "No active accounts", body: "Sync from Salesforce or add an account manually to get started.", actions: emptyActions.active },
    archived: { icon: "⊘", title: "No archived accounts", body: "Archived accounts will appear here.", actions: "" },
    trash: { icon: "🗑", title: "Trash is empty", body: "Deleted accounts can be restored from here.", actions: "" },
  }[tab];

  const hCell = (key, label, cls) => `<span class="acct-cell ${cls} sortable" data-sort="${key}">${label}${sortArrow(key)}</span>`;
  const listHeader = tab === "trash" ? "" : `
    <div class="acct-row acct-head">
      <div class="acct-row-menu"></div>
      <label class="acct-check"><input type="checkbox" id="check-all" /></label>
      <div class="acct-row-main no-link">
        ${hCell("name", "Account", "acct-cell-main")}
        ${hCell("stage", "Stage", "acct-cell-stage")}
        ${hCell("updated", "Updated", "acct-cell-updated")}
        ${hCell("outputs", "Outputs", "acct-cell-outputs")}
        ${hCell("owner", "Owner", "acct-cell-owner")}
      </div>
      <span class="acct-expand-spacer"></span>
    </div>`;

  view.innerHTML = `
    <div class="row">
      <div><h1>${esc(m.name)}</h1><p class="sub">Accounts</p></div>
      <div class="header-actions">
        <div class="sfdc-sync">
          <button class="primary small" id="sync-sfdc" title="Auto-create accounts from open Salesforce opportunities">⟳ Sync from SFDC ▾</button>
          <div class="dropdown-menu sfdc-ae-menu hidden" id="sfdc-ae-menu"></div>
        </div>
        <button class="manual-create-toggle" id="manual-create-toggle">+ add account manually</button>
        <div class="create-box hidden" id="create-box">
          <input id="new-acct" type="text" placeholder="New account name…" />
          <button class="primary small" id="create-acct">+ Create Account</button>
        </div>
      </div>
    </div>
    <div class="tabs">
      <button class="tab ${tab === "active" ? "active" : ""}" data-tab="active">Active (${active.length})</button>
      <button class="tab ${tab === "archived" ? "active" : ""}" data-tab="archived">Archived (${archived.length})</button>
      <button class="tab ${tab === "trash" ? "active" : ""}" data-tab="trash">Trash (${trash.length})</button>
    </div>
    <div id="bulk-bar" class="bulk-bar hidden">
      <span id="bulk-count" class="bulk-count"></span>
      <div class="bulk-actions">
        ${tab === "archived"
          ? `<button class="ghost small bulk-unarchive">Unarchive</button>`
          : `<button class="ghost small bulk-archive">Archive</button>`}
        <button class="ghost small bulk-claim">Make me owner</button>
        <button class="ghost small bulk-coverage-handoff">🤝 Coverage Handoff</button>
        <select class="bulk-transfer-sel"><option value="">Transfer to…</option>${
          members.filter((x) => x.id !== memberId).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
        <button class="danger small bulk-delete">Delete…</button>
      </div>
    </div>
    <div id="bulk-handoff-status" class="handoff-bulk-status hidden"></div>
    <div class="acct-list" id="acct-grid">
      ${showing.length ? listHeader + showing.map(renderRow).join("") : emptyBox(emptyMsg)}
    </div>`;

  view.querySelectorAll(".tab").forEach((t) => { t.onclick = () => pageMember(memberId, t.dataset.tab); });

  // Sortable headers
  view.querySelectorAll(".sortable").forEach((h) => h.onclick = () => {
    const key = h.dataset.sort;
    _sort = (_sort.key === key) ? { key, dir: -_sort.dir } : { key, dir: 1 };
    localStorage.setItem("se-hub-sort", JSON.stringify(_sort));
    pageMember(memberId, tab);
  });

  document.getElementById("manual-create-toggle").onclick = () => {
    const box = document.getElementById("create-box");
    box.classList.toggle("hidden");
    if (!box.classList.contains("hidden")) document.getElementById("new-acct").focus();
  };

  document.getElementById("empty-add")?.addEventListener("click", () => {
    document.getElementById("create-box")?.classList.remove("hidden");
    document.getElementById("new-acct")?.focus();
  });

  document.getElementById("create-acct").onclick = async () => {
    const name = document.getElementById("new-acct").value.trim();
    if (!name) return;
    try {
      await api("/api/accounts", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ name, owner: memberId }) });
      pageMember(memberId, "active");
    } catch (e) { alert("Could not create account: " + e.message); }
  };

  // ── Sync from SFDC: AE picker dropdown + preview/confirm modal ────────
  const aeMenu = document.getElementById("sfdc-ae-menu");
  const syncBtn = document.getElementById("sync-sfdc");
  let _aesLoaded = false;

  const saveSelectedAes = (sel) =>
    api(`/api/members/${encodeURIComponent(memberId)}/sfdc-aes`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ selected: sel }),
    }).catch(() => {});

  const checkedAes = () =>
    Array.from(aeMenu.querySelectorAll(".ae-check:checked")).map((c) => c.value);

  const loadAeMenu = async () => {
    aeMenu.innerHTML = `<div class="ae-menu-loading muted">Loading AEs…</div>`;
    let data;
    try { data = await api(`/api/members/${encodeURIComponent(memberId)}/sfdc-aes`); }
    catch (e) { aeMenu.innerHTML = `<div class="ae-menu-loading muted">SFDC unavailable</div>`; return; }
    const aes = data.aes || [], selected = new Set(data.selected || []);
    const rows = aes.length
      ? aes.map((ae) => `<label class="ae-item" data-ae="${esc(ae.toLowerCase())}"><input type="checkbox" class="ae-check" value="${esc(ae)}" ${selected.has(ae) ? "checked" : ""}/> ${esc(ae)}</label>`).join("")
      : `<div class="ae-menu-loading muted">No AEs found on open opps</div>`;
    aeMenu.innerHTML = `
      <div class="ae-menu-head">Pull accounts for these AEs<br/><span class="muted">(plus opps where you're the SE)</span></div>
      ${aes.length ? `<input type="text" class="ae-search" placeholder="Search AEs…" />` : ""}
      <div class="ae-menu-list">${rows}</div>
      <div class="ae-menu-foot"><button class="primary small" id="ae-pull-btn">Pull accounts</button></div>`;
    aeMenu.querySelectorAll(".ae-check").forEach((c) => c.onchange = () => saveSelectedAes(checkedAes()));
    const search = aeMenu.querySelector(".ae-search");
    if (search) {
      search.oninput = () => {
        const q = search.value.trim().toLowerCase();
        aeMenu.querySelectorAll(".ae-item").forEach((it) =>
          it.classList.toggle("hidden", q && !it.dataset.ae.includes(q)));
      };
      setTimeout(() => search.focus(), 0);
    }
    const pull = document.getElementById("ae-pull-btn");
    if (pull) pull.onclick = () => { aeMenu.classList.add("hidden"); openSfdcPreview(checkedAes()); };
    _aesLoaded = true;
  };

  syncBtn.onclick = async (e) => {
    e.stopPropagation();
    if (aeMenu.classList.contains("hidden")) {
      aeMenu.classList.remove("hidden");
      if (!_aesLoaded) await loadAeMenu();
    } else {
      aeMenu.classList.add("hidden");
    }
  };
  aeMenu.onclick = (e) => e.stopPropagation();

  async function openSfdcPreview(aes) {
    let data;
    try {
      data = await api(`/api/members/${encodeURIComponent(memberId)}/sfdc-accounts`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ aes }),
      });
    } catch (err) { alert("SFDC pull failed: " + err.message); return; }
    const nb = data.new_business || [], rn = data.renewals || [];
    if (!nb.length && !rn.length) { alert("No open opportunities with a future close date found."); return; }
    const datasets = { nb, rn };
    let _ptab = "nb";
    let _psort = { key: "name", dir: 1 };
    const _picked = new Set();  // account names checked, preserved across sort/tab re-renders

    const pSortVal = (a, key) => {
      switch (key) {
        case "name": return (a.name || "").toLowerCase();
        case "amount": return (a.amount ?? -Infinity);
        case "stage": return (a.stage_num ? parseFloat(a.stage_num) : -1);
        case "close": return (a.close_date || "");
        case "ae": return (a.ae || "").toLowerCase();
        default: return 0;
      }
    };
    const sortItems = (items) => [...items].sort((x, y) => {
      const vx = pSortVal(x, _psort.key), vy = pSortVal(y, _psort.key);
      if (vx < vy) return -1 * _psort.dir;
      if (vx > vy) return 1 * _psort.dir;
      return 0;
    });
    const pArrow = (key) => _psort.key === key ? (_psort.dir === 1 ? " ▲" : " ▼") : "";

    const rowsFor = (items) => items.length ? sortItems(items).map((a) => `
      <label class="sfdc-prev-row ${a.exists ? "is-existing" : ""}">
        <input type="checkbox" class="sfdc-prev-check" data-name="${esc(a.name)}" ${a.exists ? "disabled" : (_picked.has(a.name) ? "checked" : "")} />
        <span class="sfdc-prev-name">${esc(a.name)}${a.exists ? ' <span class="badge">already added</span>' : ""}</span>
        <span class="sfdc-prev-col">${fmtAmt(a.amount)}</span>
        <span class="sfdc-prev-col">${stageCell(a)}</span>
        <span class="sfdc-prev-col">${dateCell(a.close_date)}</span>
        <span class="sfdc-prev-col">${aeCell(a)}</span>
      </label>`).join("") : `<div class="empty">None.</div>`;

    const headEl = (key, label) => `<span class="sfdc-prev-hcell sortable" data-psort="${key}">${label}${pArrow(key)}</span>`;
    const prevHead = () => `
      <div class="sfdc-prev-head">
        <label class="sfdc-prev-hcell"><input type="checkbox" class="sfdc-prev-all" /></label>
        <span class="sfdc-prev-hcell sortable" data-psort="name">Account${pArrow("name")}</span>
        ${headEl("amount", "Amount")}
        ${headEl("stage", "Stage")}
        ${headEl("close", "Close Date")}
        ${headEl("ae", "AE")}
      </div>`;

    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.innerHTML = `
      <div class="modal sfdc-modal">
        <div class="modal-head"><h2>Accounts from Salesforce</h2><button class="modal-close">✕</button></div>
        <div class="tabs sfdc-prev-tabs">
          <button class="tab active" data-ptab="nb">New Business (${nb.length})</button>
          <button class="tab" data-ptab="rn">Renewals (${rn.length})</button>
        </div>
        <div class="sfdc-prev-head-wrap"></div>
        <div class="sfdc-prev-list"></div>
        <div class="modal-foot">
          <span class="muted sfdc-prev-hint">Select the accounts to add.</span>
          <button class="ghost small modal-cancel">Cancel</button>
          <button class="primary small" id="sfdc-create-btn">Create selected</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const close = () => overlay.remove();
    overlay.querySelector(".modal-close").onclick = close;
    overlay.querySelector(".modal-cancel").onclick = close;
    overlay.onclick = (e) => { if (e.target === overlay) close(); };

    const headWrap = overlay.querySelector(".sfdc-prev-head-wrap");
    const listEl = overlay.querySelector(".sfdc-prev-list");
    const renderPanel = () => {
      headWrap.innerHTML = prevHead();
      listEl.innerHTML = rowsFor(datasets[_ptab]);
      // track each checkbox into _picked so selection survives sort/tab re-render
      overlay.querySelectorAll(".sfdc-prev-check:not(:disabled)").forEach((c) => c.onchange = () => {
        if (c.checked) _picked.add(c.dataset.name); else _picked.delete(c.dataset.name);
      });
      // select-all toggles only enabled (non-existing) checkboxes on this tab
      const all = overlay.querySelector(".sfdc-prev-all");
      if (all) all.onchange = () =>
        overlay.querySelectorAll(".sfdc-prev-check:not(:disabled)").forEach((c) => {
          c.checked = all.checked;
          if (all.checked) _picked.add(c.dataset.name); else _picked.delete(c.dataset.name);
        });
      overlay.querySelectorAll(".sfdc-prev-hcell.sortable").forEach((h) => h.onclick = () => {
        const key = h.dataset.psort;
        _psort = (_psort.key === key) ? { key, dir: -_psort.dir } : { key, dir: 1 };
        renderPanel();
      });
    };
    renderPanel();

    overlay.querySelectorAll(".sfdc-prev-tabs .tab").forEach((t) => t.onclick = () => {
      overlay.querySelectorAll(".sfdc-prev-tabs .tab").forEach((x) => x.classList.toggle("active", x === t));
      _ptab = t.dataset.ptab;
      renderPanel();
    });
    overlay.querySelector("#sfdc-create-btn").onclick = async () => {
      const names = [..._picked];
      if (!names.length) { alert("Select at least one account."); return; }
      const btn = overlay.querySelector("#sfdc-create-btn");
      const hint = overlay.querySelector(".sfdc-prev-hint");
      if (hint) { hint.classList.remove("sfdc-prev-err"); hint.textContent = "Select the accounts to add."; }
      btn.disabled = true; btn.textContent = "Creating…";
      const resetBtn = () => { btn.disabled = false; btn.textContent = "Create selected"; };
      try {
        // carry the real SFDC account name through so opp lookups match exactly
        // (folder names are lossy — punctuation is stripped for fs-safety).
        const sfdcByFolder = {};
        for (const it of [...nb, ...rn]) sfdcByFolder[it.name] = it.account_name;
        const resp = await api("/api/bulk-create-accounts", { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ accounts: names.map((name) => ({ name, owner: memberId, sfdc_name: sfdcByFolder[name] })) }) });
        // The endpoint returns HTTP 200 even when individual accounts fail
        // (per-account errors are collected in `results`). Surface those instead
        // of silently closing — a swallowed failure used to look like a no-op.
        const results = (resp && resp.results) || [];
        const failed = results.filter((r) => !r.ok);
        if (failed.length) {
          // drop the ones that DID succeed from the selection so a retry only
          // re-attempts the failures, then re-render to reflect what got added.
          results.filter((r) => r.ok).forEach((r) => _picked.delete(r.name));
          renderPanel();
          resetBtn();
          if (hint) {
            hint.classList.add("sfdc-prev-err");
            const lines = failed.map((r) => `${esc(r.name)} — ${esc(r.error || "unknown error")}`).join("<br>");
            hint.innerHTML = `Couldn't add ${failed.length} of ${results.length}:<br>${lines}`;
          }
          return;
        }
        close();
        pageMember(memberId, "active");
      } catch (err) { resetBtn(); alert("Create failed: " + err.message); }
    };
  }

  // ── Multi-select + bulk bar ──────────────────────────────────────────
  const bulkBar = document.getElementById("bulk-bar");
  const checks = () => Array.from(view.querySelectorAll(".row-check"));
  const selected = () => checks().filter((c) => c.checked).map((c) => c.dataset.acct);
  const refreshBulk = () => {
    const sel = selected();
    if (sel.length) { bulkBar.classList.remove("hidden"); document.getElementById("bulk-count").textContent = `${sel.length} selected`; }
    else bulkBar.classList.add("hidden");
  };
  checks().forEach((c) => c.onchange = refreshBulk);
  const checkAll = document.getElementById("check-all");
  if (checkAll) checkAll.onchange = () => { checks().forEach((c) => (c.checked = checkAll.checked)); refreshBulk(); };

  const runBulk = async (action, owner) => {
    const accounts = selected();
    if (!accounts.length) return;
    if (action === "delete" && !confirm(`Move ${accounts.length} account(s) to trash? They can be restored.`)) return;
    await api(`/api/bulk/${action}`, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ accounts, owner }) });
    pageMember(memberId, tab);
  };
  bulkBar.querySelector(".bulk-archive")?.addEventListener("click", () => runBulk("archive"));
  bulkBar.querySelector(".bulk-unarchive")?.addEventListener("click", () => runBulk("unarchive"));
  bulkBar.querySelector(".bulk-claim")?.addEventListener("click", () => runBulk("set-owner", memberId));
  bulkBar.querySelector(".bulk-delete")?.addEventListener("click", () => runBulk("delete"));
  bulkBar.querySelector(".bulk-transfer-sel")?.addEventListener("change", (e) => {
    if (e.target.value) runBulk("set-owner", e.target.value);
  });

  // Bulk coverage handoff: one shared modal, then one HTML page per account —
  // targeting each account's new-business opp (renewal-only accounts skipped).
  bulkBar.querySelector(".bulk-coverage-handoff")?.addEventListener("click", () => {
    const accounts = selected();
    if (!accounts.length) return;
    openHandoffModal(`${accounts.length} account${accounts.length > 1 ? "s" : ""}`, async (extraText) => {
      const box = document.getElementById("bulk-handoff-status");
      box.classList.remove("hidden");
      box.innerHTML = `<div class="hb-head">Generating coverage handoffs…</div>`;
      const skipped = [];
      // Resolve each account's new-business opp first (sequential — light SFDC reads).
      let started = 0;
      for (const acct of accounts) {
        const opp = await pickNewBusinessOpp(acct).catch(() => null);
        if (!opp) { skipped.push(acct); continue; }
        try {
          const jobId = await invokeCoverageHandoff(acct, opp.name, opp.slug, extraText);
          trackJob(jobId, { account: acct, slug: opp.slug, oppName: opp.name, skill: "coverage-handoff" });
          renderHandoffRow(box, { acct, oppName: opp.name, oppSlug: opp.slug, jobId });
          started++;
        } catch (e) {
          const rowEl = document.createElement("div");
          rowEl.className = "status err";
          rowEl.innerHTML = `<span class="run-head">✕ ${esc(acct)} — ${esc(e.message)}</span>`;
          box.appendChild(rowEl);
        }
      }
      if (skipped.length) {
        const s = document.createElement("div");
        s.className = "status";
        s.innerHTML = `<span class="run-head">ⓘ Skipped (renewal-only / no open new-business opp): ${esc(skipped.join(", "))}</span>`;
        box.appendChild(s);
      }
      box.querySelector(".hb-head").textContent = `Coverage handoffs: ${started} generating${skipped.length ? `, ${skipped.length} skipped` : ""}. Runs continue in the background if you leave.`;
    });
  });

  // Re-attach to any coverage-handoff jobs still running for this member's
  // accounts (fired earlier, then navigated away). The jobs never stopped
  // server-side; this just rebuilds the status box so you see progress again.
  (async () => {
    const box = document.getElementById("bulk-handoff-status");
    if (!box) return;
    const visible = new Set([...(data.active || []), ...(data.archived || [])].map((a) => a.name));
    const all = await api("/api/jobs").catch(() => []);
    const running = (all || []).filter((j) =>
      j.skill === "coverage-handoff" && j.status === "running" && visible.has(j.account));
    if (!running.length) return;
    box.classList.remove("hidden");
    box.innerHTML = `<div class="hb-head">Coverage handoffs in progress (${running.length}) — resumed.</div>`;
    running.forEach((j) => renderHandoffRow(box, {
      acct: j.account, oppName: j.opportunity || j.account, oppSlug: j.opp_slug, jobId: j.job_id,
    }));
  })();

  // ⋮ kebab menus
  const closeMenus = () => view.querySelectorAll(".dropdown-menu").forEach((mn) => mn.classList.add("hidden"));
  view.querySelectorAll(".kebab").forEach((b) => {
    b.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      const menu = b.nextElementSibling;
      const open = !menu.classList.contains("hidden");
      closeMenus(); if (!open) menu.classList.remove("hidden");
    };
  });

  const stop = (e) => { e.preventDefault(); e.stopPropagation(); };
  view.querySelectorAll(".archive-btn").forEach((b) => b.onclick = async (e) => {
    stop(e); await api(`/api/accounts/${encodeURIComponent(b.dataset.acct)}/archive`, { method: "POST" });
    pageMember(memberId, "active");
  });
  view.querySelectorAll(".unarchive-btn").forEach((b) => b.onclick = async (e) => {
    stop(e); await api(`/api/accounts/${encodeURIComponent(b.dataset.acct)}/unarchive`, { method: "POST" });
    pageMember(memberId, "archived");
  });
  view.querySelectorAll(".delete-btn").forEach((b) => b.onclick = (e) => {
    stop(e);
    const acct = b.dataset.acct;
    if (b.dataset.armed !== "1") {
      b.dataset.armed = "1"; b.textContent = `Confirm delete “${acct}”`; b.classList.add("armed");
      setTimeout(() => { if (b.dataset.armed === "1") { b.dataset.armed = "0"; b.textContent = "Delete…"; b.classList.remove("armed"); } }, 4000);
      return;
    }
    api(`/api/accounts/${encodeURIComponent(acct)}`, { method: "DELETE" })
      .then(() => pageMember(memberId, "active")).catch((err) => alert("Delete failed: " + err.message));
  });
  view.querySelectorAll(".restore-btn").forEach((b) => b.onclick = async (e) => {
    stop(e);
    try { await api(`/api/trash/${encodeURIComponent(b.dataset.tid)}/restore`, { method: "POST" }); pageMember(memberId, "trash"); }
    catch (err) { alert("Restore failed: " + err.message); }
  });

  // ── Expand row → inline opportunities (most recent → least recent) ──────
  view.querySelectorAll(".acct-expand").forEach((btn) => {
    btn.onclick = async (e) => {
      e.preventDefault(); e.stopPropagation();
      const acct = btn.dataset.acct;
      const drawer = view.querySelector(`.opp-drawer[data-drawer="${CSS.escape(acct)}"]`);
      const isOpen = !drawer.classList.contains("hidden");
      if (isOpen) { drawer.classList.add("hidden"); btn.classList.remove("open"); return; }
      btn.classList.add("open");
      drawer.classList.remove("hidden");
      if (!drawer.dataset.loaded) {
        drawer.innerHTML = `<div class="opp-drawer-loading">Loading opportunities…</div>`;
        const [opps, jobs] = await Promise.all([
          api(`/api/accounts/${encodeURIComponent(acct)}/opportunities`).catch(() => []),
          api(`/api/jobs?account=${encodeURIComponent(acct)}`).catch(() => []),
        ]);
        drawer.dataset.loaded = "1";
        const activityByOpp = activityByOpp(jobs, acct);
        drawer.innerHTML = opps.length
          ? `<div class="opp-list">${oppHeaderRow()}${opps.map((o) => oppRow(acct, o, activityByOpp[o.slug])).join("")}</div>`
          : `<div class="opp-drawer-loading muted">No opportunities found.</div>`;
      }
    };
  });
}

// Shared opportunity header row (used in the expand drawer and the account page)
function oppHeaderRow() {
  return `
    <div class="opp-row opp-row-head no-link">
      <span class="opp-cell opp-cell-main">Opportunity</span>
      <span class="opp-cell opp-cell-stage">Stage</span>
      <span class="opp-cell opp-cell-status">Status</span>
      <span class="opp-cell opp-cell-outputs">Outputs</span>
    </div>`;
}

// Shared opportunity row (used in the expand drawer and the account page)
function oppRow(account, o, activity = null) {
  const fmtAmt = (n) => (n || n === 0) ? "$" + Number(n).toLocaleString() : null;
  const stage = o.stage_num ? esc(o.stage_num) : (o.stage ? esc(o.stage) : '<span class="muted">—</span>');
  const statusBadge = o.is_closed === false ? '<span class="badge owned">open</span>'
    : (o.is_closed ? '<span class="badge badge-closed">closed</span>' : "");
  const subParts = [o.type ? esc(o.type) : null, o.close_date ? esc(o.close_date) : null, fmtAmt(o.amount)].filter(Boolean);
  const sub = subParts.length ? `<span class="opp-row-sub">${subParts.join(" · ")}</span>` : '';
  return `
    <a class="opp-row" href="#/opp/${encodeURIComponent(account)}/${encodeURIComponent(o.slug)}/${encodeURIComponent(o.name)}">
      <div class="opp-cell opp-cell-main">
        <span class="opp-name-wrap"><span class="opp-row-name">${esc(o.name)}</span>${renderActivity(activity, "small")}</span>
        ${sub}
      </div>
      <div class="opp-cell opp-cell-stage"><span class="opp-stage">${stage}</span></div>
      <div class="opp-cell opp-cell-status">${statusBadge || '<span class="muted">—</span>'}</div>
      <div class="opp-cell opp-cell-outputs"><span class="opp-out-count${o.output_count ? "" : " muted"}">${o.output_count || "—"}</span></div>
    </a>`;
}

// ---- Page: account → list of opportunities -------------------------------
async function pageAccount(account) {
  setCrumbs([...(await accountCrumbs(account)), { label: account }]);
  view.innerHTML = `<div class="row"><div><h1>${esc(account)}</h1><p class="sub">Opportunities — pick one to view outputs &amp; run skills</p></div></div>
    <div class="empty" id="opps-loading">Loading opportunities from Salesforce…</div>`;
  const [opps, jobs] = await Promise.all([
    api(`/api/accounts/${encodeURIComponent(account)}/opportunities`).catch(() => []),
    api(`/api/jobs?account=${encodeURIComponent(account)}`).catch(() => []),
  ]);
  const activityByOppSlug = activityByOpp(jobs, account);

  const empty = emptyBox({
    icon: "⊘",
    title: "No opportunities found",
    body: "Salesforce may be unavailable or this account has no open opportunities. Invoke a skill on this account and it will use a General opportunity bucket.",
  });

  view.innerHTML = `
    <div class="row"><div><h1>${esc(account)}</h1><p class="sub">Opportunities — pick one to view outputs &amp; run skills</p></div></div>
    <div class="opp-list">
      ${opps.length ? oppHeaderRow() + opps.map((o) => oppRow(account, o, activityByOppSlug[o.slug])).join("") : empty}
    </div>`;
}

// Render the Generated Outputs list grouped by day, with pretty skill names.
function renderOutputGroups(outputs) {
  const groups = {};  // "YYYY-MM-DD" -> [items]  (outputs already newest-first)
  for (const o of outputs) {
    const day = (o.modified || "").slice(0, 10);
    (groups[day] = groups[day] || []).push(o);
  }
  return Object.keys(groups).sort().reverse().map((day) => `
    <div class="out-group">
      <div class="out-group-date">${esc(longDate(day))}</div>
      <div class="out-group-items">
        ${groups[day].map((o) => {
          const isHtml = o.ext === "html";
          const atGen = o.reference_freshness_at_generation;
          const changed = o.reference_changed_since_generation || [];
          const legacy = atGen == null;
          const staleAtGen = !legacy && atGen.some((r) => !r.fresh);
          const changedSince = !legacy && changed.length > 0;
          const refParts = [];
          if (staleAtGen) {
            const stale = atGen.filter((r) => !r.fresh);
            refParts.push("Reference data was stale/missing when generated: " + stale.map((r) => `${r.label}${r.age_days != null ? " (" + r.age_days + " days old)" : r.status === "missing" ? " (missing)" : ""}`).join(", "));
          }
          if (changedSince) {
            refParts.push("Reference data has changed since generation: " + changed.map((c) => `${c.label}${c.new_date ? " (now " + c.new_date + ")" : ""}`).join(", "));
          }
          const refWarn = staleAtGen || changedSince;
          const refTitle = refParts.join(" | ");
          const status = o.validation_status || (o.valid === false ? "invalid" : "unvalidated");
          const invalid = status === "invalid";
          const uncertain = status === "unvalidated" && !refWarn;
          const warn = invalid || refWarn;
          const validationTitle = invalid ? esc(o.validation_errors.slice(0, 2).join(" ")) : "";
          const warnTitle = [validationTitle, refTitle].filter(Boolean).join(validationTitle && refTitle ? " | " : "");
          let statusBadge = "";
          if (invalid) {
            statusBadge = `<span class="out-status out-status--error" title="${warnTitle}">Incomplete</span>`;
          } else if (refWarn) {
            const label = changedSince ? "Source changed" : "Stale source";
            statusBadge = `<span class="out-status out-status--warn" title="${warnTitle}">${esc(label)}</span>`;
          } else if (uncertain) {
            statusBadge = `<span class="out-status out-status--info" title="${warnTitle}">Needs review</span>`;
          }
          return `
          <div class="out-item${isHtml ? " is-html" : ""}" data-path="${encodeURIComponent(o.path)}" data-ext="${esc(o.ext || "md")}" data-title="${esc(prettySkill(o.skill))} — ${esc(o.filename)}">
            <div><div class="skill">${esc(prettySkill(o.skill))}${isHtml ? ' <span class="badge">HTML</span>' : ""}</div><div class="when" title="${esc(o.filename)}">${esc(conciseOutputName(o.filename, o.skill))}</div></div>
            <div class="out-item-right">
              ${statusBadge}
              <span class="when">${esc((o.modified || "").slice(11))} UTC</span>
              ${isHtml ? `<button class="ghost small out-view" data-path="${encodeURIComponent(o.path)}">View</button><button class="ghost small out-copy-path" data-path="${encodeURIComponent(o.path)}">Copy repo path</button><button class="ghost small out-push-repo" data-path="${encodeURIComponent(o.path)}">Push to repo</button>` : ""}
              ${downloadMenuHtml(encodeURIComponent(o.path), "dl-menu-row")}
            </div>
          </div>`; }).join("")}
      </div>
    </div>`).join("");
}

// ---- Coverage Handoff modal (PTO) ----------------------------------------
// Slim form: who's-out / covering SE (freeform) + coverage window. Everything
// else (players, meetings, open items) the skill derives from transcripts+SFDC.
// Serializes to a text block and hands it to `onGenerate(extraText)`.
// `titleSuffix` labels the modal header (an opp name, or e.g. "3 accounts").
async function openHandoffModal(titleSuffix, onGenerate) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal handoff-modal">
      <div class="modal-head"><h2>Coverage Handoff — ${esc(titleSuffix)}</h2><button class="modal-close">✕</button></div>
      <div class="handoff-form">
        <div class="hf-grid">
          <label>SE out<input id="hf-out" type="text" placeholder="Owner SE" /></label>
          <label>Covering SE<input id="hf-cover" type="text" placeholder="Covering SE" /></label>
          <label>Coverage start<input id="hf-start" type="date" /></label>
          <label>Coverage end<input id="hf-end" type="date" /></label>
        </div>
      </div>
      <div class="modal-foot">
        <span class="muted" style="margin-right:auto;font-size:12px;">Players, meetings &amp; deal facts are pulled from transcripts + Salesforce automatically.</span>
        <button class="ghost small modal-cancel">Cancel</button>
        <button class="primary small" id="hf-generate">Generate handoff</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.querySelector(".modal-close").onclick = close;
  overlay.querySelector(".modal-cancel").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };

  // default SE out = the current member's name if we're under a member page
  const memberSlug = (location.hash.match(/#\/member\/([^/]+)/) || [])[1];
  if (memberSlug) {
    api("/api/members").then((members) => {
      const me = (members || []).find((m) => m.id === memberSlug);
      const outEl = overlay.querySelector("#hf-out");
      if (me && outEl && !outEl.value) outEl.value = me.name;
    }).catch(() => {});
  }
  overlay.querySelector("#hf-out").focus();

  overlay.querySelector("#hf-generate").onclick = () => {
    const v = (sel) => (overlay.querySelector(sel)?.value || "").trim();
    const extra = [
      "PTO COVERAGE CONTEXT (use verbatim for the coverage banner):",
      `- SE out: ${v("#hf-out") || "Not specified"}`,
      `- Covering SE: ${v("#hf-cover") || "Not specified"}`,
      `- Coverage window: ${v("#hf-start") || "?"} to ${v("#hf-end") || "?"}`,
    ].join("\n");
    close();
    onGenerate(extra);
  };
}

// Pick the opportunity to hand off for an account: the best OPEN, non-renewal
// (new-business) opp. Returns {name, slug} or null (renewal-only / no open opp).
async function pickNewBusinessOpp(account) {
  const opps = await api(`/api/accounts/${encodeURIComponent(account)}/opportunities`).catch(() => []);
  // Only OPEN, non-renewal opps qualify. No open new-business opp → skip (return null).
  const newBiz = opps.filter((o) => (o.is_closed === false || o.is_closed === null) && (o.type || "") !== "Renewal");
  const pick = newBiz[0];  // endpoint returns close_date DESC; first is the active one
  return pick ? { name: pick.name, slug: pick.slug } : null;
}

// Fire a coverage-handoff run for one account+opp. Returns the job id (or throws).
async function invokeCoverageHandoff(account, oppName, oppSlug, extraText) {
  const payload = { account, opportunity: oppName, opp_slug: oppSlug, skill: "coverage-handoff", extra: extraText };
  const res = await invokeWithPlan(payload);
  return res.job_id;
}

// Append one bulk-handoff status row and attach a page-independent poller that
// flips it to ✓ (with a View link) / ✕ when the job finishes. Used both when
// firing fresh jobs and when re-attaching to still-running jobs after you
// navigate back to the member page. The job keeps running server-side either way.
function renderHandoffRow(box, { acct, oppName, oppSlug, jobId }) {
  const rowEl = document.createElement("div");
  rowEl.className = "status running";
  rowEl.innerHTML = `<span class="run-head"><span class="spinner"></span>${esc(acct)} — ${esc(oppName)}…</span>`;
  box.appendChild(rowEl);
  pollJob(jobId, async (job) => {
    if (job.status === "running") return;
    if (job.ok) {
      const outs = await api(`/api/accounts/${encodeURIComponent(acct)}/outputs?opp=${encodeURIComponent(oppSlug)}`).catch(() => []);
      const html = outs.find((o) => o.skill === "coverage-handoff" && o.ext === "html");
      rowEl.className = "status ok";
      rowEl.innerHTML = `<span class="run-head">✓ ${esc(acct)} — ${esc(oppName)}</span>` +
        (html ? ` <button class="ghost small" data-path="${encodeURIComponent(html.path)}">View</button>` : "");
      const vb = rowEl.querySelector("button[data-path]");
      if (vb) vb.onclick = () => window.open("/api/output/html?path=" + vb.dataset.path, "_blank");
    } else {
      rowEl.className = "status err";
      rowEl.innerHTML = `<span class="run-head">✕ ${esc(acct)} — finished with an error</span>`;
    }
  });
  return rowEl;
}

// Wire output rows: HTML outputs open in a new tab + expose View / Copy-repo-path;
// markdown outputs open the in-app viewer (navOpenOutput). Shared by the initial
// render and refreshOutputs so behavior stays consistent.
function wireOutItems(container, ctx) {
  container.querySelectorAll(".out-item").forEach((el) => {
    const isHtml = el.dataset.ext === "html";
    if (isHtml) {
      el.onclick = (e) => {
        if (e.target.closest("button, .dl-menu")) return;  // let buttons handle themselves
        window.open("/api/output/html?path=" + el.dataset.path, "_blank");
      };
    } else {
      el.onclick = () => navOpenOutput(el.dataset.path, el.dataset.title, ctx);
    }
  });
  container.querySelectorAll(".out-view").forEach((b) => b.onclick = (e) => {
    e.stopPropagation(); window.open("/api/output/html?path=" + b.dataset.path, "_blank");
  });
  container.querySelectorAll(".out-copy-path").forEach((b) => b.onclick = async (e) => {
    e.stopPropagation();
    try {
      const rp = await api(`/api/output/repo-path?account=${encodeURIComponent(ctx.account)}&member=${encodeURIComponent(ctx.memberName || "")}`);
      await navigator.clipboard.writeText(rp.full);
      const old = b.textContent; b.textContent = "Copied ✓";
      setTimeout(() => { b.textContent = old; }, 1500);
    } catch (err) { alert("Repo path: could not copy — " + err.message); }
  });
  container.querySelectorAll(".out-push-repo").forEach((b) => b.onclick = async (e) => {
    e.stopPropagation();
    // First: is there already an OPEN PR for this account? (merged/closed don't count)
    const old = b.textContent; b.disabled = true;
    b.innerHTML = `<span class="spinner"></span> Checking…`;
    let existing = null;
    try {
      const st = await api(`/api/output/push-status?account=${encodeURIComponent(ctx.account)}`);
      existing = st.open_pr || null;
    } catch { /* best-effort — fall through to a normal push */ }
    b.disabled = false; b.textContent = old;

    const ok = await confirmPush(ctx.account, existing);
    if (!ok) return;
    b.disabled = true;
    b.innerHTML = `<span class="spinner"></span> Pushing…`;
    try {
      const res = await api("/api/output/push-to-repo", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({
          path: decodeURIComponent(b.dataset.path),
          account: ctx.account,
          member: ctx.memberName || "",
        }),
      });
      showPushResult(ctx.account, res);
    } catch (err) {
      showPushError(ctx.account, err.message);
    } finally {
      b.disabled = false; b.textContent = old;
    }
  });
}

// Confirm modal before opening a PR against the internal repo. If `existing` is
// an open PR ({number, url}) for this account, show the warning variant so the
// user doesn't unknowingly open a duplicate — they can open the existing PR,
// push anyway (a new PR), or cancel. (Merged/closed PRs aren't passed here, so
// re-pushing after merge/close falls through to the normal confirm.)
function confirmPush(account, existing) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    const warn = existing && existing.number;
    overlay.innerHTML = `
      <div class="modal push-modal">
        <div class="modal-head"><h2>${warn ? "PR already open" : "Push handoff to internal.airbyte.ai"}</h2><button class="modal-close">✕</button></div>
        <div class="push-body">
          ${warn ? `
            <p class="push-warn">⚠ <strong>${esc(account)}</strong> already has an open pull request:
              <a class="push-result" href="${esc(existing.url)}" target="_blank" rel="noopener">PR #${esc(String(existing.number))}</a>.</p>
            <p>Pushing again opens a <strong>second</strong> PR for the same handoff. Update the existing one instead, or push anyway if you meant to.</p>
          ` : `
            <p>Push the <strong>${esc(account)}</strong> coverage handoff to <code>internal.airbyte.ai</code>? This branches off <code>origin/main</code> and opens a pull request.</p>
          `}
        </div>
        <div class="modal-foot">
          ${warn ? `<a class="ghost small" href="${esc(existing.url)}" target="_blank" rel="noopener" style="margin-right:auto;text-decoration:none;">Open PR #${esc(String(existing.number))}</a>` : ""}
          <button class="ghost small modal-cancel">Cancel</button>
          <button class="${warn ? "danger" : "primary"} small" id="push-go">${warn ? "Push anyway" : "Push &amp; open PR"}</button>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    const done = (v) => { overlay.remove(); resolve(v); };
    overlay.querySelector(".modal-close").onclick = () => done(false);
    overlay.querySelector(".modal-cancel").onclick = () => done(false);
    overlay.onclick = (e) => { if (e.target === overlay) done(false); };
    overlay.querySelector("#push-go").onclick = () => done(true);
  });
}

function showPushResult(account, res) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal push-modal">
      <div class="modal-head"><h2>Pushed ✓</h2><button class="modal-close">✕</button></div>
      <div class="push-body">
        <p><strong>${esc(account)}</strong> handoff pushed to <code>${esc(res.target || "")}</code>.</p>
        ${res.pr_url ? `<p><a class="push-result" href="${esc(res.pr_url)}" target="_blank" rel="noopener">${esc(res.pr_url)}</a></p>` : `<p class="muted">Branch <code>${esc(res.branch || "")}</code> pushed (no PR URL returned).</p>`}
      </div>
      <div class="modal-foot"><button class="primary small modal-cancel">Done</button></div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.querySelector(".modal-close").onclick = close;
  overlay.querySelector(".modal-cancel").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
}

function showPushError(account, message) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal push-modal">
      <div class="modal-head"><h2>Push failed</h2><button class="modal-close">✕</button></div>
      <div class="push-body">
        <p>Could not push the <strong>${esc(account)}</strong> handoff:</p>
        <pre class="push-err">${esc(message)}</pre>
      </div>
      <div class="modal-foot"><button class="primary small modal-cancel">Close</button></div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.querySelector(".modal-close").onclick = close;
  overlay.querySelector(".modal-cancel").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };
}

// ---- Page: opportunity (outputs + invoke) --------------------------------
async function pageOpportunity(account, slug, oppName) {
  setCrumbs([...(await accountCrumbs(account)), { label: account, href: `#/account/${encodeURIComponent(account)}` }, { label: oppName }]);
  const outputs = await api(`/api/accounts/${encodeURIComponent(account)}/outputs?opp=${encodeURIComponent(slug)}`);
  outputMeta = Object.fromEntries(outputs.map((o) => [o.path, { valid: o.valid, validation_status: o.validation_status || (o.valid === false ? "invalid" : "unvalidated"), validation_errors: o.validation_errors || [], missing_sections: o.missing_sections || [], reference_freshness_at_generation: o.reference_freshness_at_generation, reference_changed_since_generation: o.reference_changed_since_generation || [] }]));
  // Resolve the owning member's display name (for the handoff repo-path). Owner
  // is a member id on the account; map to its name. Empty is fine (endpoint
  // falls back to a placeholder slug).
  let _memberName = "";
  try {
    const [meta, members] = await Promise.all([
      api(`/api/accounts/${encodeURIComponent(account)}`).catch(() => ({})),
      api("/api/members").catch(() => []),
    ]);
    _memberName = (members.find((m) => m.id === meta.owner)?.name) || "";
  } catch { /* best-effort */ }
  view.innerHTML = `
    <div class="row">
      <div><h1>${esc(oppName)}</h1><p class="sub">${esc(account)} · outputs &amp; skills</p></div>
      <div class="row-actions">
        <a class="primary live-btn" href="#/live/${encodeURIComponent(account)}/${encodeURIComponent(slug)}/${encodeURIComponent(oppName)}">🎙 Live Transcribe</a>
        <button class="ghost" id="handoff-btn" title="Generate a PTO coverage handoff for a covering SE">🤝 Coverage Handoff</button>
        <button class="primary" id="invoke-btn">⚡ Invoke Skill</button>
      </div>
    </div>
    <div class="freebar command-bar">
      <span class="command-bar-icon">⚡</span>
      <div class="freebar-input-wrap">
        <input id="opp-free" type="text" autocomplete="off" placeholder="Run a skill or type an instruction (e.g. “deal assessment focused on the security objection”)…" />
        <div id="free-suggest" class="free-suggest hidden"></div>
      </div>
      <button class="primary small" id="opp-free-run">Run</button>
    </div>
    <div id="freebar-status" class="status hidden"></div>
    <h2>Generated outputs</h2>
    <div class="outputs" id="outputs">
      ${outputs.length ? renderOutputGroups(outputs) : emptyBox({ icon: "⊘", title: "No outputs yet", body: "Invoke a skill to generate the first output for this opportunity.", actions: `<button class="primary small" id="empty-invoke">Invoke Skill</button>` })}
    </div>`;
  wireOutItems(view, { account, slug, oppName, memberName: _memberName });
  // onDeleted runs on user click (after refreshOutputs is defined below) → safe.
  wireDownloadMenus(view, () => refreshOutputs());
  document.getElementById("invoke-btn").onclick = () => openInvoke(account, { slug, name: oppName });
  document.getElementById("empty-invoke")?.addEventListener("click", () => openInvoke(account, { slug, name: oppName }));

  // Free-text instruction bar — runs the agent without picking a named skill
  const freeInput = document.getElementById("opp-free");
  const freeBtn = document.getElementById("opp-free-run");
  const fStatus = document.getElementById("freebar-status");
  fStatus.className = "status-stack";  // container of per-job rows (was a single line)

  // Re-fetch the Generated Outputs list (after a run produces a new file).
  const refreshOutputs = async () => {
    const outs = await api(`/api/accounts/${encodeURIComponent(account)}/outputs?opp=${encodeURIComponent(slug)}`).catch(() => []);
    outputMeta = Object.fromEntries(outs.map((o) => [o.path, { valid: o.valid, validation_errors: o.validation_errors || [], missing_sections: o.missing_sections || [], reference_freshness_at_generation: o.reference_freshness_at_generation, reference_changed_since_generation: o.reference_changed_since_generation || [] }]));
    const el = document.getElementById("outputs");
    if (!el) return;
    el.innerHTML = outs.length ? renderOutputGroups(outs) : emptyBox({ icon: "⊘", title: "No outputs yet", body: "Invoke a skill to generate the first output for this opportunity.", actions: `<button class="primary small empty-invoke">Invoke Skill</button>` });
    el.querySelector(".empty-invoke")?.addEventListener("click", () => openInvoke(account, { slug, name: oppName }));
    wireOutItems(el, { account, slug, oppName, memberName: _memberName });
    wireDownloadMenus(el, () => refreshOutputs());
  };

  // ── Multi-job status stack ──────────────────────────────────────────────
  // Each invoke gets its OWN status row, keyed by job id, so several skills can
  // run at once for the same opp and each shows independent progress. (Was a
  // single line that only ever tracked one job.) `rows` maps job id → its <div>;
  // a freshly-started run uses a temp key until the POST returns its real id.
  const rows = new Map();

  const ensureRow = (key) => {
    let row = rows.get(key);
    if (!row) {
      row = document.createElement("div");
      row.className = "status";
      rows.set(key, row);
      fStatus.appendChild(row);
    }
    return row;
  };

  // Status-only row — the result goes to Generated Outputs (no inline preview).
  const renderJob = (key, job, finishedAt) => {
    const row = ensureRow(key);
    if (job.status === "running") {
      row.className = "status running";
      row.innerHTML = `<span class="run-head"><span class="spinner"></span>Running ${esc(job.skill || "instruction")} … (keeps running even if you leave this page)</span>`;
      return;
    }
    const ok = job.ok;
    row.className = ok ? "status ok" : "status err";
    const ago = finishedAt ? ` · ran ${relTime(finishedAt)}` : "";
    const head = ok
      ? `✓ ${esc(job.skill || "run")}${ago} — saved to Generated Outputs below`
      : `✕ ${esc(job.skill || "run")}${ago} — finished with an error`;
    row.innerHTML = `<span class="run-head">${head}</span><button class="run-dismiss" title="Dismiss">✕</button>`;
    row.querySelector(".run-dismiss").onclick = () => { row.remove(); rows.delete(key); };
  };

  const watch = (jobId) => pollJob(jobId, (job) => {
    if (!document.getElementById("freebar-status")) return;  // left the page
    const wasRunning = rows.get(jobId)?.classList.contains("running");
    renderJob(jobId, job);
    if (job.status !== "running" && wasRunning) refreshOutputs();  // new file landed
  });

  // Recover runs started earlier (still-running jobs re-attach) — ALL of them,
  // not just the first. Finished results live in Generated Outputs (and chat-only
  // skills like next-move show their text when opened), so we only re-surface
  // the ones still in flight.
  const existing = await api(`/api/jobs?account=${encodeURIComponent(account)}&opp_slug=${encodeURIComponent(slug)}`).catch(() => []);
  existing.filter((j) => j.status === "running").forEach((j) => {
    renderJob(j.job_id, j);
    watch(j.job_id);
    trackJob(j.job_id, { account, slug, oppName, skill: j.skill || null });
  });

  // Start a run. `skill` = run that named skill (typed text becomes context);
  // otherwise run the typed text as a freeform instruction. Multiple runs can be
  // in flight at once — each gets its own row and the Run button stays enabled.
  let _tmpSeq = 0;
  const startRun = async (skill) => {
    const free = freeInput.value.trim();
    if (!skill && !free) return;
    suggestBox.classList.add("hidden");
    const tmpKey = `pending-${_tmpSeq++}`;
    const row = ensureRow(tmpKey);
    row.className = "status running";
    row.innerHTML = `<span class="run-head"><span class="spinner"></span>Starting ${esc(skill || "instruction")}…</span>`;
    const payload = skill
      ? { account, opportunity: oppName, opp_slug: slug, skill, extra: free || null }
      : { account, opportunity: oppName, opp_slug: slug, freeform: free };
    try {
      const res = await invokeWithPlan(payload);
      if (res.persistence_warning) warnPersistence(`job:${res.job_id}`, res.persistence_warning);
      // Re-key the pending row to the real job id so watch() updates it in place.
      rows.delete(tmpKey); rows.set(res.job_id, row);
      // Global toast when it finishes (fires even if you navigate away); the
      // inline `watch` handles the on-page status while you stay here.
      trackJob(res.job_id, { account, slug, oppName, skill: skill || null });
      await watch(res.job_id);
    } catch (e) {
      row.className = "status err"; row.textContent = "Error: " + e.message;
      rows.delete(tmpKey);
    }
  };

  // ── Coverage Handoff (PTO) ────────────────────────────────────────────
  // Run the coverage-handoff skill with structured PTO context from a modal
  // form. Reuses the same job stack/watch as startRun; the form is serialized
  // into `extra` so the skill's prompt receives it.
  const startHandoff = async (extraText) => {
    const tmpKey = `pending-${_tmpSeq++}`;
    const row = ensureRow(tmpKey);
    row.className = "status running";
    row.innerHTML = `<span class="run-head"><span class="spinner"></span>Generating coverage handoff…</span>`;
    try {
      const payload = { account, opportunity: oppName, opp_slug: slug, skill: "coverage-handoff", extra: extraText };
      const res = await invokeWithPlan(payload);
      if (res.persistence_warning) warnPersistence(`job:${res.job_id}`, res.persistence_warning);
      rows.delete(tmpKey); rows.set(res.job_id, row);
      trackJob(res.job_id, { account, slug, oppName, skill: "coverage-handoff" });
      await watch(res.job_id);
    } catch (e) {
      row.className = "status err"; row.textContent = "Error: " + e.message;
      rows.delete(tmpKey);
    }
  };

  document.getElementById("handoff-btn").onclick = () => openHandoffModal(oppName, startHandoff);

  // ── Suggestive skill dropdown ─────────────────────────────────────────
  // As you type, surface skills whose label / id / triggers match a word.
  const suggestBox = document.getElementById("free-suggest");
  const matchSkills = (q) => {
    q = q.toLowerCase();
    const words = q.split(/\s+/).filter((w) => w.length >= 3);
    if (!words.length) return [];
    return SKILLS.map((s) => {
      const help = SKILLS_HELP[s.id] || {};
      const hay = [s.id, s.label, help.description, ...(help.triggers || [])].join(" ").toLowerCase();
      // score = how many typed words appear in this skill's text
      let score = 0;
      for (const w of words) if (hay.includes(w)) score += (s.label.toLowerCase().includes(w) || s.id.includes(w)) ? 2 : 1;
      return { s, score };
    }).filter((x) => x.score > 0).sort((a, b) => b.score - a.score).slice(0, 5).map((x) => x.s);
  };
  let sugIndex = -1, sugList = [];
  const renderSuggest = () => {
    sugList = matchSkills(freeInput.value.trim());
    if (!sugList.length) { suggestBox.classList.add("hidden"); sugIndex = -1; return; }
    suggestBox.innerHTML = sugList.map((s, i) => {
      const h = SKILLS_HELP[s.id] || {};
      return `<div class="sug-item${i === sugIndex ? " active" : ""}" data-skill="${esc(s.id)}">
        <span class="sug-label">⚡ ${esc(s.label)}</span>
        <span class="sug-blurb">${esc((h.description || s.blurb || "").slice(0, 90))}</span>
      </div>`;
    }).join("");
    suggestBox.classList.remove("hidden");
    suggestBox.querySelectorAll(".sug-item").forEach((el) => {
      el.onmousedown = (e) => { e.preventDefault(); startRun(el.dataset.skill); };
    });
  };
  freeInput.oninput = () => { sugIndex = -1; renderSuggest(); };
  freeInput.onblur = () => setTimeout(() => suggestBox.classList.add("hidden"), 120);
  freeInput.onfocus = () => { if (freeInput.value.trim()) renderSuggest(); };

  freeBtn.onclick = () => startRun();
  freeInput.onkeydown = (e) => {
    const open = !suggestBox.classList.contains("hidden") && sugList.length;
    if (e.key === "ArrowDown" && open) { e.preventDefault(); sugIndex = (sugIndex + 1) % sugList.length; renderSuggest(); }
    else if (e.key === "ArrowUp" && open) { e.preventDefault(); sugIndex = (sugIndex - 1 + sugList.length) % sugList.length; renderSuggest(); }
    else if (e.key === "Escape") { suggestBox.classList.add("hidden"); sugIndex = -1; }
    else if (e.key === "Enter") {
      if (open && sugIndex >= 0) startRun(sugList[sugIndex].id);  // chose a suggestion
      else startRun();                                            // run as freeform
    }
  };
}

// Shorten a heading for the sidebar index: drop trailing parentheticals and
// "— …" qualifiers so labels stay scannable (e.g. "Suggested Agenda (30 min)"
// → "Suggested Agenda"; "What the AE Already Learned (from prior Gong call)"
// → "What the AE Already Learned").
function conciseLabel(text) {
  return (text || "").replace(/\s*\([^)]*\)\s*$/, "").replace(/\s*[—–-]\s.*$/, "").trim() || text;
}

// Convert "**Label:** value" lines into a scannable label/value grid (.kv).
// Handles a paragraph of several "· "-separated pairs (the title key-lines) and
// single-pair list items (At a Glance bullets). Anything that doesn't match the
// label:value shape is left untouched.
function upgradeKeyValues(root) {
  const KV = /^\s*<strong>([^<:]{1,40}):<\/strong>\s*([\s\S]*)$/;

  const toRow = (html) => {
    const m = html.match(KV);
    if (!m) return null;
    return `<div class="kv"><span class="kv-k">${m[1].trim()}</span><span class="kv-v">${m[2].trim()}</span></div>`;
  };

  // Title key-lines: one <p> with multiple "**L:** v · **L:** v" pairs.
  root.querySelectorAll("p.md-p").forEach((p) => {
    const parts = p.innerHTML.split(/\s*·\s*/);
    const rows = parts.map(toRow);
    if (rows.length >= 2 && rows.every(Boolean)) {
      const grid = document.createElement("div");
      grid.className = "kv-grid";
      grid.innerHTML = rows.join("");
      p.replaceWith(grid);
    }
  });

  // At-a-Glance style bullets: a <ul> where every <li> is "**Label:** value".
  root.querySelectorAll("ul.md-list").forEach((ul) => {
    const items = Array.from(ul.children);
    const rows = items.map((li) => toRow(li.innerHTML));
    if (items.length && rows.every(Boolean)) {
      const grid = document.createElement("div");
      grid.className = "kv-grid";
      grid.innerHTML = rows.join("");
      ul.replaceWith(grid);
    }
  });
}

// ── Decision-First reader helpers ─────────────────────────────────────────
// These re-present structure the analytical skills ALREADY emit (the At-a-Glance
// decision card, risk/blocker callouts) as a scannable hero + risk strip. All are
// defensive: a doc without these blocks (call-prep, account-refresher, old docs)
// simply skips them and renders as before.

// Which At-a-Glance labels become prominent decision tiles, and in what order.
// Matched by case-insensitive substring against the row label. Covers both the
// analytical skills (Probability/Stage/Blocker/Motion/Confidence) and the
// account-refresher / prep vocabulary (Current state, Key players, Last touch,
// Open items). Each `key` de-dupes so a synonym match doesn't double a tile.
const TILE_LABELS = [
  { match: ["probability", "verdict", "fit", "current state", "status", "current read"], key: "verdict" },
  { match: ["stage", "trajectory", "momentum"], key: "stage" },
  { match: ["#1 blocker", "primary risk", "top blocker", "top risk", "main blocker", "main open", "key risk", "blocker"], key: "blocker" },
  { match: ["recommended motion", "recommended next", "next gate", "next step", "next move", "next best", "motion"], key: "motion" },
  { match: ["key players", "stakeholders", "owner", "champion", "economic buyer"], key: "players" },
  { match: ["last touch", "last activity", "last contact"], key: "lasttouch" },
  { match: ["open items", "open item", "open questions"], key: "open" },
  { match: ["confidence", "source confidence"], key: "confidence" },
];

// Infer a sentiment class for a tile from its value text (status dots, band words,
// probability ranges). Drives the tile's left-accent color.
function tileSentiment(label, valueText) {
  const t = (valueText || "").toLowerCase();
  const l = (label || "").toLowerCase();
  // "High/Medium/Low" reads as good/warn/danger on a confidence tile. Scope the
  // bare-word match to confidence tiles so "low" in an unrelated value (e.g. a
  // Stage "low engagement") isn't mis-colored red.
  if (/confidence/.test(l)) {
    if (/🔴/.test(valueText) || /\blow\b/.test(t)) return "sev-danger";
    if (/🟡/.test(valueText) || /\bmedium\b/.test(t)) return "sev-warn";
    if (/🟢/.test(valueText) || /\bhigh\b/.test(t)) return "sev-good";
  }
  if (/🔴/.test(valueText) || /\bat risk\b|\bblocker\b|\bsilent\b|\bdead\b|\bdying\b|\bweak\b|<\s*20/.test(t)) return "sev-danger";
  if (/🟡/.test(valueText) || /\bneeds\b|\bcaution\b|\bsteady\b|\bmedium\b|20[–-]40|20[–-]60|40[–-]60/.test(t)) return "sev-warn";
  if (/🟢/.test(valueText) || /\blikely\b|\bvery likely\b|\bcommitted\b|\bstrong\b|\bviable\b|\bgo\b|60[–-]80|>\s*80/.test(t)) return "sev-good";
  // motion/next-step tiles read as the primary action — accent them.
  if (/motion|next/.test((label || "").toLowerCase())) return "sev-accent";
  return "";
}

// Build a single, scannable Document status bar from output metadata. Combines
// validation state and reference freshness into one color-coded line, with the
// verbose explanation hidden behind a Details toggle.
function buildDocStatus(meta) {
  const issues = [];
  let severity = "ok";

  if (!meta) {
    issues.push({ text: "Metadata unavailable. This output could not be checked.", type: "warn" });
    severity = "warn";
  } else {
    const vstatus = meta.validation_status || "unvalidated";
    if (vstatus === "invalid") {
      issues.push({
        text: "This output is missing required sections: " + (meta.validation_errors || []).slice(0, 3).join("; ") + ".",
        type: "error",
      });
      severity = "error";
    } else if (vstatus === "unvalidated") {
      issues.push({
        text: "This output could not be validated against the current output contract. It may predate the required-section format or use headings the parser does not recognize.",
        type: "info",
      });
      if (severity === "ok") severity = "info";
    }

    const atGen = meta.reference_freshness_at_generation;
    const changed = meta.reference_changed_since_generation || [];
    const legacy = atGen == null;
    if (legacy) {
      issues.push({
        text: "Reference freshness unknown for this legacy output; product claims may be stale.",
        type: "warn",
      });
      if (severity === "ok" || severity === "info") severity = "warn";
    } else {
      const stale = atGen.filter((r) => !r.fresh);
      if (stale.length) {
        issues.push({
          text: "Reference data was stale/missing when generated: " + stale.map((r) => `${r.label}${r.age_days != null ? " (" + r.age_days + " days old)" : r.status === "missing" ? " (missing)" : ""}`).join(", ") + ".",
          type: "warn",
        });
        if (severity === "ok" || severity === "info") severity = "warn";
      }
      if (changed.length) {
        issues.push({
          text: "Reference data has changed since generation: " + changed.map((c) => `${c.label}${c.new_date ? " (now " + c.new_date + ")" : ""}`).join(", ") + ".",
          type: "warn",
        });
        if (severity === "ok" || severity === "info") severity = "warn";
      }
    }
  }

  const config = {
    ok: { icon: "✓", label: "Ready", cls: "ok" },
    info: { icon: "?", label: "Needs review", cls: "info" },
    warn: { icon: "⚠", label: "Review sources", cls: "warn" },
    error: { icon: "✕", label: "Output incomplete", cls: "error" },
  }[severity];

  if (severity === "ok" && !issues.length) {
    return `<div class="doc-status doc-status--ok"><span class="doc-status-icon">${config.icon}</span><span class="doc-status-label">${config.label}</span></div>`;
  }

  const details = issues.map((i) => `<li class="doc-status-issue doc-status-issue--${i.type}">${esc(i.text)}</li>`).join("");
  const summary = issues.length === 1 ? issues[0].text : `${issues.length} issues`;
  return `<div class="doc-status doc-status--${config.cls}" id="doc-status">
    <div class="doc-status-main">
      <span class="doc-status-icon">${config.icon}</span>
      <span class="doc-status-label">${config.label}</span>
      <span class="doc-status-summary">${esc(summary)}</span>
      <button class="doc-status-toggle ghost smallest" id="doc-status-toggle">Details ▾</button>
    </div>
    <ul class="doc-status-details hidden" id="doc-status-details">${details}</ul>
  </div>`;
}

// Map a (concise) section title to a sidebar intent group. Order of groups is
// fixed; anything unmatched falls into "Context" so nothing is dropped. Covers
// both the analytical skills (deal-assessment, tech-qual, …) and account-refresher
// ("Who's Who", "The Story So Far", "Watch-outs", …).
function tocGroup(title) {
  const t = (title || "").toLowerCase();
  if (/at a glance|10-second|bottom line|current read|what would close|what would lose|deal blocker|where things stand|what changed|recommendation|fit verdict|verdict/.test(t)) return "Decision";
  if (/next action|next step|next move|coaching|recommended|what'?s open|open question|watch-?out|risk|action|email|poc|plan|workshop|agenda/.test(t)) return "Execution";
  return "Context";
}
const TOC_GROUP_ORDER = ["Decision", "Context", "Execution"];

// Sections collapsed by default (audit / supporting detail). Matched on the H2.
const COLLAPSE_DEFAULT = /source coverage|activity trajectory|meddpicc|coaching|appendix|raw|evidence reviewed/i;

// Sections whose bullets read as risks/watch-outs — their `**Lead.** detail`
// list items become severity cards and feed the Top-Risks strip. (account-refresher
// "Watch-outs", deal-assessment "What Would Lose It", post-call "New Objections".)
const RISK_SECTION = /watch-?outs?|what would lose|new objections|concerns|risks?(?!\w)|red flags/i;

// Sections whose `**Lead.** detail` bullets read as neutral structured items —
// rendered as calm info cards (no severity pill) for scannability. (Constraints &
// Edge Cases, Decision Criteria, etc.) Distinct from RISK_SECTION (severity cards).
const INFO_SECTION = /constraints?|edge cases?|considerations?|assumptions?/i;

// Ranked action lists (next-move "Ranked Next Moves") — lead-bolded bullets like
// "**1 · Run tech-qual — …**" become the same calm info cards, so the ranked moves
// read as scannable cards with a bold lead-title instead of a dense list. Kept
// separate from INFO_SECTION for clarity; both render as `.risk-card.info`.
const EXEC_SECTION = /ranked next moves?/i;

// Infer a severity for a risk bullet from its wording (no explicit dot in prose).
function riskSeverity(text) {
  const t = (text || "").toLowerCase();
  if (/🔴|\bhigh\b|\bblocker\b|\bcan'?t close|cannot close|deal-?killer|center of gravity|fiction|dead\b/.test(t)) return "blocker";
  return "risk";
}

// One-line preview shown when a section is collapsed. For Source Coverage, count
// the top-level evidence bullets; otherwise use the section's first sentence.
function sectionSummary(sectionEl, titleText) {
  if (/source coverage/i.test(titleText)) {
    const items = sectionEl.querySelectorAll(".sec-body > ul.md-list > li, .sec-body > .md-list > li");
    const n = items.length;
    if (n) return `${n} source${n === 1 ? "" : "s"} reviewed — click to expand the full audit trail.`;
  }
  const p = sectionEl.querySelector(".sec-body p.md-p, .sec-body li");
  if (p) {
    const txt = (p.textContent || "").trim().replace(/\s+/g, " ");
    const sentence = txt.split(/(?<=[.!?])\s/)[0];
    return sentence.length > 180 ? sentence.slice(0, 177) + "…" : sentence;
  }
  return "Click to expand.";
}

// `ctx` = { account, slug, oppName } so Back returns to the exact opportunity.
// Navigate to an output by changing the hash, so the reader becomes a real
// browser-history entry. This makes the hardware Back button pop the reader →
// the opp page (one level), instead of skipping the reader (which left the hash
// at #/opp/… so Back jumped up to the account page). `path` is already
// encodeURIComponent'd (from data-path); encode once more so its slashes don't
// split the route — the router decodes the segment once before calling openOutput.
function navOpenOutput(path, title, ctx) {
  const seg = encodeURIComponent(path);
  location.hash = `#/output/${encodeURIComponent(ctx.account)}/${encodeURIComponent(ctx.slug)}/${encodeURIComponent(ctx.oppName)}/${seg}`;
}

async function openOutput(path, title, ctx) {
  const decodedPath = decodeURIComponent(path);
  const meta = outputMeta[decodedPath] || await api("/api/output/meta?path=" + encodeURIComponent(decodedPath)).catch(() => null);
  if (meta) outputMeta[decodedPath] = meta;
  const text = await api("/api/output?path=" + encodeURIComponent(decodedPath));
  const toc = [];
  const bodyHtml = await mdToHtml(text, toc);

  // Build the body DOM, then restructure: use the H1 as the page title, wrap
  // each H2 section in its own bordered card, and drop the inline "Jump to"
  // line (the sidebar replaces it).
  const tmp = document.createElement("div");
  tmp.innerHTML = bodyHtml;

  // Friendly title = the doc's own H1 (fallback to the passed filename title).
  const h1 = tmp.querySelector("h1");
  const docTitle = h1 ? h1.textContent.trim() : title;
  if (h1) h1.remove();

  // Drop the "Jump to:" paragraph if the skill emitted one.
  tmp.querySelectorAll("p.md-p").forEach((p) => {
    if (/^\s*jump to:/i.test(p.textContent)) p.remove();
  });

  // Shorten the in-doc section headers too (sidebar + headers stay consistent).
  tmp.querySelectorAll("h2.md-h, h3.md-h").forEach((h) => {
    const short = conciseLabel(h.textContent);
    if (short && short !== h.textContent) h.textContent = short;
  });

  // Turn dense key/value lines into a scannable definition layout. A paragraph
  // or list item shaped like "**Label:** value" becomes a .kv row (label left,
  // value right). De-bolds the label (CSS styles it), keeps ==key== emphasis.
  upgradeKeyValues(tmp);

  // Group the flat node list into H2-delimited SECTIONS inside ONE doc sheet.
  // Content before the first H2 (title key-lines, At a Glance) is the lead section.
  const sections = [];
  let cur = null;
  const newSection = () => { cur = document.createElement("section"); cur.className = "doc-section"; sections.push(cur); };
  for (const n of Array.from(tmp.childNodes)) {
    if (n.nodeType === 1 && n.tagName === "H2") newSection();
    if (!cur) newSection();
    cur.appendChild(n);
  }
  // ── Promote the At-a-Glance lead into a standalone executive decision card.
  // The lead section (content before the first H2) carries the title meta line +
  // the At-a-Glance decision card. Pull its glance .kv rows out as decision tiles.
  // Defensive: if there's no recognizable glance block, leave everything in the
  // sheet (non-analytical / old docs render exactly as before).
  let execCardHtml = "";
  const leadSection = sections[0];
  // The lead is the pre-first-H2 block (title meta + At-a-Glance). Promote it only
  // when it actually holds an At-a-Glance card and isn't itself an H2 section.
  const leadIsGlance = leadSection && /at a glance/i.test(leadSection.textContent || "")
    && !leadSection.querySelector(":scope > h2.md-h2");
  let glancePromoted = false;
  if (leadIsGlance) {
    // "Current read" narrative — the account-refresher's "10-Second Version" (or a
    // "Current read" H3) is the human summary; surface it at the top of the card.
    // Collect the paragraphs following that H3 until the next heading, and mark
    // those nodes so they're not also re-injected into the sheet below.
    let readHtml = "";
    const readNodes = new Set();
    const heads = Array.from(leadSection.querySelectorAll(":scope > h3.md-h3"));
    const readHead = heads.find((h) => /\d+-second|current read|the .*version|in (a )?nutshell/i.test(h.textContent || ""));
    if (readHead) {
      readNodes.add(readHead);
      let n = readHead.nextElementSibling, parts = [];
      // Stop the narrative at the next heading, a callout, or a rule — a callout
      // (e.g. next-move's [!blocker]/[!risk] overrides) is its own block and must
      // survive to feed the Top-Risks strip, not get absorbed into the summary.
      while (n && !/^h[1-6]$/i.test(n.tagName) && n.tagName !== "HR"
             && !n.classList?.contains("callout")) {
        if (!n.classList?.contains("kv-grid") && (n.textContent || "").trim()) parts.push(n.innerHTML);
        readNodes.add(n); n = n.nextElementSibling;
      }
      if (parts.length) readHtml = `<div class="exec-read">${parts.join(" ")}</div>`;
    }
    const kvs = Array.from(leadSection.querySelectorAll(".kv-grid .kv"));
    const tiles = [], rest = [], tiledLabels = new Set();
    for (const kv of kvs) {
      const label = (kv.querySelector(".kv-k")?.textContent || "").trim();
      const valHtml = kv.querySelector(".kv-v")?.innerHTML || "";
      const valText = kv.querySelector(".kv-v")?.textContent || "";
      const spec = TILE_LABELS.find((s) => s.match.some((m) => label.toLowerCase().includes(m)));
      if (spec && !tiles.some((t) => t.key === spec.key)) {
        tiles.push({ key: spec.key, label, valHtml, sev: tileSentiment(label, valText) });
        tiledLabels.add(label.toLowerCase());
      } else if (!tiledLabels.has(label.toLowerCase())) {
        // skip a remainder row whose label already became a tile (meta-line dupes)
        rest.push(`<div class="kv"><span class="kv-k">${esc(label)}</span><span class="kv-v">${valHtml}</span></div>`);
      }
    }
    if (tiles.length) {
      // order tiles by TILE_LABELS sequence
      tiles.sort((a, b) => TILE_LABELS.findIndex((s) => s.key === a.key) - TILE_LABELS.findIndex((s) => s.key === b.key));
      const tileHtml = tiles.map((t) =>
        `<div class="tile ${t.sev}"><div class="tile-label">${esc(t.label)}</div><div class="tile-value">${t.valHtml}</div></div>`
      ).join("");
      const restHtml = rest.length ? `<div class="exec-rest"><div class="kv-grid">${rest.join("")}</div></div>` : "";
      execCardHtml = `<div class="exec-card"><div class="exec-card-eyebrow">Executive Assessment</div>`
        + `${readHtml}<div class="tile-grid">${tileHtml}</div>${restHtml}</div>`;
      glancePromoted = true;
      // The lead section's content (meta line + At-a-Glance) is now fully
      // represented by the tiles + rest, so drop it from the sheet. Any stray
      // CONTENT prose (rare) is re-injected; drop headings, kv-grids, and the
      // italic "*Decision card — lead with the call.*" caption (meta, not content).
      sections.shift();
      const stray = Array.from(leadSection.childNodes).filter((n) => {
        if (n.nodeType !== 1) return false;
        if (readNodes.has(n)) return false;  // already surfaced as the exec "Current read"
        if (n.classList?.contains("kv-grid") || /^h[1-6]$/i.test(n.tagName)) return false;
        const txt = (n.textContent || "").trim();
        if (!txt) return false;
        // a wholly-italic caption line (the glance descriptor) is meta — skip it
        if (n.tagName === "P" && n.children.length === 1 && n.firstElementChild?.tagName === "EM"
            && n.firstElementChild.textContent.trim() === txt) return false;
        return true;
      });
      if (stray.length) {
        const lead = document.createElement("section");
        lead.className = "doc-section";
        stray.forEach((n) => lead.appendChild(n));
        sections.unshift(lead);
      }
    }
  }

  // Tag the section containing "At a Glance" so it gets the summary panel (only
  // reached if the glance wasn't promoted above — keeps backward behavior).
  for (const s of sections) {
    if (/at a glance/i.test(s.querySelector("h2,h3")?.textContent || "")) s.classList.add("is-glance");
  }

  // ── "**Lead.** detail" bullet sections → cards. RISK_SECTION (Watch-outs / What
  // Would Lose It / …) become SEVERITY cards (left-accent + pill, fed into the
  // Top-Risks strip). INFO_SECTION (Constraints / Edge Cases / …) become calm
  // NEUTRAL cards (no pill) just for scannability. Both only fire when every
  // bullet is lead-bolded (the card shape); otherwise the list renders as-is.
  // [!risk]-callout docs are unaffected (this is the bullet path).
  let riskCardSeq = 0;
  for (const s of sections) {
    const title = s.querySelector(":scope > h2.md-h2")?.textContent || "";
    const isRisk = RISK_SECTION.test(title);
    const isExec = !isRisk && EXEC_SECTION.test(title);
    const isInfo = !isRisk && !isExec && INFO_SECTION.test(title);
    if (!isRisk && !isInfo && !isExec) continue;

    // Ranked-move shape: a bold-lead PARAGRAPH ("**1 · skill — headline.**")
    // followed by its detail nodes (a kv-grid of Why-now/Inputs/Effort, or prose)
    // until the next bold-lead paragraph. Group each run into a calm info card so
    // the ranked moves read as scannable cards. (The bullet path below handles
    // the RISK/INFO `<ul>` shape.)
    if (isExec) {
      const kids = Array.from(s.children).filter((n) => n !== s.querySelector(":scope > h2.md-h2"));
      const isLead = (n) => n.tagName === "P" && n.firstElementChild?.tagName === "STRONG"
        && n.firstElementChild === n.firstChild;
      if (kids.some(isLead)) {
        const wrap = document.createElement("div");
        wrap.className = "risk-cards";
        let card = null;
        kids.forEach((n) => {
          if (isLead(n)) {
            const leadText = (n.firstElementChild.textContent || "").replace(/[.:]\s*$/, "").trim();
            card = document.createElement("div");
            card.className = "risk-card info";
            card.innerHTML = `<div class="risk-card-head"><span class="risk-card-title">${esc(leadText)}</span></div>`
              + `<div class="risk-card-body"></div>`;
            wrap.appendChild(card);
          } else if (card) {
            card.querySelector(".risk-card-body").appendChild(n.cloneNode(true));
          } else {
            wrap.appendChild(n.cloneNode(true));  // stray intro before first lead
          }
        });
        kids.forEach((n) => n.remove());
        const h2 = s.querySelector(":scope > h2.md-h2");
        if (h2) h2.after(wrap); else s.prepend(wrap);
      }
      continue;
    }

    const ul = s.querySelector(":scope > ul.md-list");
    if (!ul) continue;
    const items = Array.from(ul.children).filter((li) => li.tagName === "LI");
    // only transform when the bullets are lead-bolded (the card shape)
    if (!items.length || !items.every((li) => li.querySelector(":scope > strong"))) continue;
    const wrap = document.createElement("div");
    wrap.className = "risk-cards";
    items.forEach((li) => {
      const lead = li.querySelector(":scope > strong");
      const leadText = (lead?.textContent || "").replace(/[.:]\s*$/, "").trim();
      const sev = isRisk ? riskSeverity(li.textContent || "") : "info";
      // body = the li's html minus the lead <strong>
      const clone = li.cloneNode(true);
      clone.querySelector(":scope > strong")?.remove();
      const bodyHtml = clone.innerHTML.replace(/^[\s.:—-]+/, "");
      const card = document.createElement("div");
      card.className = `risk-card ${sev}`;
      if (isRisk) card.id = `risk-card-${riskCardSeq++}`;  // only risk cards anchor the strip
      const pill = isRisk ? `<span class="risk-sev ${sev}">${sev}</span>` : "";
      card.innerHTML = `<div class="risk-card-head">${pill}`
        + `<span class="risk-card-title">${esc(leadText)}</span></div>`
        + (bodyHtml.trim() ? `<div class="risk-card-body">${bodyHtml}</div>` : "");
      wrap.appendChild(card);
    });
    ul.replaceWith(wrap);
  }

  // ── Mark audit/detail sections collapsible (progressive disclosure). Wrap each
  // H2 section's post-heading content in a .sec-body, add a toggle + a one-line
  // summary preview. Decision sections stay open; supporting detail collapses.
  for (const s of sections) {
    const h2 = s.querySelector(":scope > h2.md-h2");
    if (!h2) continue;
    const titleText = h2.textContent || "";
    const body = document.createElement("div");
    body.className = "sec-body";
    Array.from(s.childNodes).forEach((n) => { if (n !== h2) body.appendChild(n); });
    const summary = document.createElement("div");
    summary.className = "sec-summary";
    s.appendChild(body);
    summary.textContent = sectionSummary(s, titleText);
    // toggle affordance inside the H2 band
    const tog = document.createElement("span");
    tog.className = "sec-toggle"; tog.textContent = "▾";
    h2.appendChild(tog);
    s.classList.add("collapsible");
    s.insertBefore(summary, body);
    if (COLLAPSE_DEFAULT.test(titleText)) s.classList.add("collapsed");
  }

  // ── Drop a leading section that only contains document meta (Date / Skill / Account).
  // The page header and breadcrumbs already carry that context, so showing it as a
  // full section pushes the real content down without adding value.
  if (sections[0] && !sections[0].querySelector(":scope > h2.md-h2")) {
    const kv = sections[0].querySelector(":scope > .kv-grid");
    if (kv) {
      const labels = Array.from(kv.querySelectorAll(".kv-k")).map((k) => k.textContent.trim().toLowerCase());
      const metaLabels = new Set(["date", "skill", "account", "opportunity", "generated", "source", "output"]);
      if (labels.length && labels.every((l) => metaLabels.has(l))) sections.shift();
    }
  }

  // ── Visually distinguish the highest-stakes narrative sections.
  const sectionAccent = {
    blocker: /\bdeal blocker\b|\bprimary blocker\b|\b#1 blocker\b/i,
    win: /\bwhat would close\b|\bhow to win\b|\bwinning path\b/i,
    risk: /\bwhat would lose\b|\bwatch[- ]?outs?\b|\brisk factors\b/i,
  };
  for (const s of sections) {
    const title = (s.querySelector(":scope > h2.md-h2")?.textContent || "").toLowerCase();
    if (sectionAccent.blocker.test(title)) s.classList.add("doc-section--blocker");
    else if (sectionAccent.win.test(title)) s.classList.add("doc-section--win");
    else if (sectionAccent.risk.test(title)) s.classList.add("doc-section--risk");
  }

  // ── Top-risk strip: a navigational summary of the doc's risk/blocker callouts
  // AND risk-section cards (above), surfaced high on the page. Anchors back to each
  // in place (not a copy). Scanned in document order across both shapes.
  const riskItems = [];
  sections.forEach((s) => {
    // `.risk-card.info` are neutral cards (Constraints / Edge Cases / ranked moves)
    // — they carry no severity, so keep them OUT of the Top-Risks strip.
    s.querySelectorAll(".callout-risk, .callout-blocker, .risk-card:not(.info)").forEach((c) => {
      let sev, title;
      if (c.classList.contains("risk-card")) {
        sev = c.classList.contains("blocker") ? "blocker" : "risk";
        title = (c.querySelector(".risk-card-title")?.textContent || "").trim().replace(/\s+/g, " ");
      } else {
        sev = c.classList.contains("callout-blocker") ? "blocker" : "risk";
        title = (c.querySelector(".callout-title")?.textContent
          || c.querySelector(".callout-body")?.textContent || "").trim().replace(/\s+/g, " ");
        if (!c.id) c.id = `risk-anchor-${riskItems.length}`;
      }
      if (!title) return;
      riskItems.push({ sev, title: title.length > 160 ? title.slice(0, 157) + "…" : title, id: c.id });
    });
  });
  // blockers first, then risks; cap to keep it a summary not a second wall
  riskItems.sort((a, b) => (a.sev === "blocker" ? 0 : 1) - (b.sev === "blocker" ? 0 : 1));
  const topRisks = riskItems.slice(0, 4);
  const riskStripHtml = topRisks.length
    ? `<div class="risk-strip"><div class="risk-strip-head">Top Risks</div>`
      + topRisks.map((r) =>
        `<button class="risk-item" data-risk-target="${r.id}">`
        + `<span class="risk-sev ${r.sev}">${r.sev}</span>`
        + `<span class="risk-title">${esc(r.title)}</span><span class="risk-arrow">→</span></button>`
      ).join("") + `</div>`
    : "";

  const sheetHtml = sections.map((s) => s.outerHTML).join("");

  // Sidebar index — H2/H3 only, concise labels, grouped by intent. H3s stay with
  // their preceding H2's group. Empty groups are omitted; order is fixed.
  // Drop the At-a-Glance entry from the index once it's promoted to the exec card
  // (its in-sheet anchor no longer exists). Keep it otherwise (backward compat).
  const tocEntries = toc.filter((t) => (t.level === 2 || t.level === 3)
    && !(glancePromoted && /at a glance|\d+-second|current read|in (a )?nutshell/i.test(t.text)));
  let lastH2Group = "Context";
  const grouped = { Decision: [], Context: [], Execution: [] };
  tocEntries.forEach((t) => {
    const label = conciseLabel(t.text);
    if (t.level === 2) lastH2Group = tocGroup(label);
    const g = grouped[lastH2Group] || grouped["Context"];
    g.push(`<a href="#toc-${t.id}" class="doc-toc-link lvl${t.level}">${esc(label)}</a>`);
  });
  const tocHtml = TOC_GROUP_ORDER
    .filter((g) => grouped[g].length)
    .map((g) => `<div class="doc-toc-group">${g}</div>${grouped[g].join("")}`)
    .join("");

  const backHref = ctx
    ? `#/opp/${encodeURIComponent(ctx.account)}/${encodeURIComponent(ctx.slug)}/${encodeURIComponent(ctx.oppName)}`
    : null;
  const skill = decodedPath.split("/").slice(-2, -1)[0] || "";
  const compareBtn = skill === "deal-assessment" && ctx
    ? `<button class="ghost small" id="doc-compare" title="Compare this deal assessment with a previous one">↔ Compare</button>` : "";
  const docStatusHtml = buildDocStatus(meta);
  view.innerHTML = `
    <div class="row"><h1>${esc(docTitle)}</h1>
      <div class="row-actions row-actions--doc">
        <button class="ghost small" id="doc-back" title="Back to opportunity">← Back</button>
        ${compareBtn}
        <span class="row-actions-spacer"></span>
        ${downloadMenuHtml(encodeURIComponent(decodedPath), "dl-menu-doc", "Export", false)}
        <button class="danger small" id="doc-delete" title="Delete this output">Delete</button>
        <button class="primary small" id="doc-chat-toggle" title="Ask a follow-up or run a skill on this doc">💬 Chat</button>
      </div></div>
    ${docStatusHtml}
    <div class="doc-layout" id="doc-layout">
      ${tocHtml ? `<aside class="doc-toc"><div class="doc-toc-head">On this page</div>${tocHtml}</aside>` : ""}
      <article class="md-body">
        ${execCardHtml}
        ${riskStripHtml}
        <div id="feedback-panel" class="feedback-panel"></div>
        <div class="doc-sheet">${sheetHtml}</div>
      </article>
      <aside class="doc-chat">
        <div class="doc-chat-head">Ask about this deal
          <button class="doc-chat-close" id="doc-chat-close" title="Hide chat" aria-label="Hide chat">✕</button>
        </div>
        <div class="doc-assist">
          <div>
            <div class="doc-assist-group-head">Suggested questions</div>
            <div class="doc-chips" id="doc-chips"></div>
          </div>
          <div>
            <div class="doc-assist-group-head">Actions</div>
            <div class="doc-chips" id="doc-actions"></div>
          </div>
        </div>
        <div class="doc-qa" id="doc-qa"></div>
        <div class="doc-askbar">
          <textarea id="doc-ask" rows="1" autocomplete="off" placeholder="Ask a follow-up about this document…"></textarea>
          <button class="primary small" id="doc-ask-send">Ask</button>
        </div>
      </aside>
    </div>`;
  if (ctx) setCrumbs([...(await accountCrumbs(ctx.account)),
                      { label: ctx.account, href: `#/account/${encodeURIComponent(ctx.account)}` },
                      { label: ctx.oppName, href: backHref },
                      { label: "output" }]);
  else setCrumbs([{ label: "Team", href: "#/" }, { label: "output" }]);

  await loadFeedbackPanel(path);

  // Deterministic Back — go to the opp page in one step. The reader is now its
  // own route (#/output/…), so the hardware Back button already pops here to the
  // opp page. This in-app button mirrors that: setting the hash fires a normal
  // hashchange → route(). (Guard the equal-hash case for safety.)
  const goBack = () => {
    const target = backHref || "#/";
    if (location.hash === target) route();
    else location.hash = target;
  };
  document.getElementById("doc-back").onclick = goBack;
  // After deleting the open output, leave the now-gone doc → back to the opp.
  wireDownloadMenus(view, goBack);

  // Separate, explicit Delete button in the reader header.
  const deleteBtn = document.getElementById("doc-delete");
  if (deleteBtn) {
    let armed = false;
    deleteBtn.onclick = async () => {
      if (!armed) {
        armed = true;
        deleteBtn.textContent = "Confirm delete";
        setTimeout(() => { armed = false; deleteBtn.textContent = "Delete"; }, 4000);
        return;
      }
      deleteBtn.disabled = true; deleteBtn.textContent = "Deleting…";
      try {
        await deleteOutput(decodedPath);
        goBack();
      } catch (err) {
        showToast("Delete failed: " + err.message, "err");
        deleteBtn.disabled = false; deleteBtn.textContent = "Delete"; armed = false;
      }
    };
  }

  // Document status bar: toggle the detailed issue list.
  const statusToggle = document.getElementById("doc-status-toggle");
  const statusDetails = document.getElementById("doc-status-details");
  if (statusToggle && statusDetails) {
    statusToggle.onclick = () => {
      const hidden = statusDetails.classList.toggle("hidden");
      statusToggle.textContent = hidden ? "Details ▾" : "Details ▴";
    };
  }

  const compareBtnEl = document.getElementById("doc-compare");
  if (compareBtnEl && ctx) compareBtnEl.onclick = () => openDealDiffModal(ctx, decodedPath);

  // Collapsible sections — clicking the H2 band toggles its body. Ignore clicks
  // on links inside the heading (there are none today, but stay safe).
  view.querySelectorAll(".doc-section.collapsible > .md-h2").forEach((h2) => {
    h2.onclick = (e) => {
      if (e.target.closest("a")) return;
      h2.closest(".doc-section").classList.toggle("collapsed");
    };
  });

  // Scroll an element into view, opening any collapsed section that contains it
  // first (so jumping from the TOC or a risk chip into a folded section works).
  const revealTarget = (el) => {
    if (!el) return;
    const sec = el.closest(".doc-section.collapsible");
    if (sec) sec.classList.remove("collapsed");
    // wait a frame so layout settles after expand, then scroll + flash
    requestAnimationFrame(() => { el.scrollIntoView({ behavior: "smooth", block: "start" }); flashEl(el); });
  };

  // Sidebar links scroll-jump locally (no router involvement).
  view.querySelectorAll(".doc-toc-link").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      revealTarget(document.getElementById(a.getAttribute("href").slice(5))); // strip "#toc-"
    };
  });

  // Top-risk strip → jump to the source callout in place (open it if collapsed).
  view.querySelectorAll(".risk-item").forEach((b) => {
    b.onclick = () => revealTarget(document.getElementById(b.dataset.riskTarget));
  });

  // In-doc anchor links (e.g. the exec card's "see Source Coverage") should also
  // open a collapsed target section before scrolling — route them via revealTarget.
  view.querySelectorAll('.md-body a[href^="#"]:not(.doc-toc-link)').forEach((a) => {
    const id = a.getAttribute("href").slice(1);
    if (!id) return;
    a.addEventListener("click", (e) => {
      const el = document.getElementById(id);
      if (el) { e.preventDefault(); revealTarget(el); }
    });
  });

  // Scroll-spy: highlight the TOC link for the section currently in view.
  const tocLinks = Array.from(view.querySelectorAll(".doc-toc-link"));
  const headings = Array.from(view.querySelectorAll(".md-body .md-h2, .md-body .md-h3"));
  if (tocLinks.length && headings.length && "IntersectionObserver" in window) {
    const linkFor = (id) => tocLinks.find((a) => a.getAttribute("href") === `#toc-${id}`);
    let activeId = null;
    const setActive = (id) => {
      if (id === activeId) return;
      activeId = id;
      tocLinks.forEach((a) => a.classList.remove("active"));
      const link = id && linkFor(id);
      if (link) link.classList.add("active");
    };
    const visible = new Map();
    const obs = new IntersectionObserver((entries) => {
      entries.forEach((en) => {
        if (en.isIntersecting) visible.set(en.target.id, en.boundingClientRect.top);
        else visible.delete(en.target.id);
      });
      if (visible.size) {
        // topmost visible heading wins
        const top = [...visible.entries()].sort((a, b) => a[1] - b[1])[0][0];
        setActive(top);
      }
    }, { rootMargin: "-80px 0px -65% 0px", threshold: 0 });
    headings.forEach((h) => obs.observe(h));
  }

  // Follow-up chat against this document (quick → Claude API; deep → claude -p).
  // The chat panel is HIDDEN by default so the document uses the full width — it
  // only takes a column once you open it (toggle button, or auto on send/recover).
  const layout = document.getElementById("doc-layout");
  const askInput = document.getElementById("doc-ask");
  const askSend = document.getElementById("doc-ask-send");
  const thread = document.getElementById("doc-qa");
  const openChat = (focus = true) => {
    layout.classList.add("has-chat");
    if (focus) askInput.focus();
  };
  const closeChat = () => layout.classList.remove("has-chat");
  document.getElementById("doc-chat-toggle").onclick = () =>
    layout.classList.contains("has-chat") ? closeChat() : openChat();
  document.getElementById("doc-chat-close").onclick = closeChat;

  const docAsk = () => {
    const q = askInput.value.trim();
    if (!q) return;
    askInput.value = "";
    autosizeTextarea(askInput);  // collapse back to one line after sending
    openChat(false);             // make sure the thread is visible
    // "run connector feasibility" → invoke that skill (generates a saved output
    // on the opp page), instead of answering as a doc question. Needs opp context.
    const skill = ctx?.account && ctx?.slug ? detectSkillInvocation(q) : null;
    if (skill) { invokeFromChat(thread, q, skill, ctx); return; }
    askThread(thread, q, "/api/output/ask", {
      path: decodeURIComponent(path), question: q,
      account: ctx?.account || null, opportunity: ctx?.oppName || null,
    });
  };
  askSend.onclick = docAsk;
  askInput.addEventListener("input", () => autosizeTextarea(askInput));
  // Enter sends; Shift+Enter inserts a newline (the box grows to fit).
  askInput.onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); docAsk(); } };

  // ── Action-oriented assistant panel: suggested-question chips + workflow
  // actions, all wired to capabilities that already exist (docAsk / invoke / DL).
  const chipsEl = document.getElementById("doc-chips");
  const actionsEl = document.getElementById("doc-actions");
  if (chipsEl) {
    const lc = (docTitle || "").toLowerCase();
    const suggestions = ["What's the single biggest risk here?", "What should the AE do next?"];
    if (/deal assessment|probability|deal/.test(lc)) suggestions.unshift("Why is the probability in this band?");
    else if (/tech qual|technical/.test(lc)) suggestions.unshift("What's the biggest technical risk?");
    else if (/connector|feasib/.test(lc)) suggestions.unshift("Which connectors are the riskiest?");
    else if (/poc|proof of concept/.test(lc)) suggestions.unshift("What are the exit criteria?");
    else suggestions.unshift("Summarize this in three bullets.");
    chipsEl.innerHTML = suggestions.slice(0, 4)
      .map((q) => `<button class="doc-chip">${esc(q)}</button>`).join("");
    chipsEl.querySelectorAll(".doc-chip").forEach((c) => {
      c.onclick = () => { askInput.value = c.textContent; autosizeTextarea(askInput); docAsk(); };
    });
  }
  if (actionsEl) {
    const acts = [
      { label: "📋 Copy summary", id: "copy" },
      { label: "✉️ Draft follow-up email", id: "email" },
      { label: "⬇ Export brief", id: "export" },
    ];
    actionsEl.innerHTML = acts.map((a) => `<button class="doc-action" data-act="${a.id}">${a.label}</button>`).join("");
    actionsEl.querySelectorAll(".doc-action").forEach((b) => {
      b.onclick = () => {
        const act = b.dataset.act;
        if (act === "copy") {
          const txt = (view.querySelector(".exec-card")?.innerText
            || view.querySelector(".md-body")?.innerText || "").trim();
          navigator.clipboard?.writeText(txt).then(() => {
            b.classList.add("copied"); const o = b.textContent; b.textContent = "✓ Copied";
            setTimeout(() => { b.classList.remove("copied"); b.textContent = o; }, 1500);
          });
        } else if (act === "email") {
          askInput.value = "draft a follow-up email"; autosizeTextarea(askInput); docAsk();  // routes via detectSkillInvocation
        } else if (act === "export") {
          view.querySelector(".dl-menu-doc .dl-btn")?.click();
        }
      };
    });
  }

  // Recover an in-flight skill run started earlier from this chat (e.g. next-move).
  // The job keeps running server-side even after leaving; re-attach so re-opening
  // the output shows its live status again (mirrors the opp page's recovery).
  if (ctx?.account && ctx?.slug) {
    const jobs = await api(`/api/jobs?account=${encodeURIComponent(ctx.account)}&opp_slug=${encodeURIComponent(ctx.slug)}`).catch(() => []);
    const running = (jobs || []).find((j) => j.status === "running" && j.skill && j.skill !== "output-ask");
    if (running) { openChat(false); reattachInvoke(thread, running, ctx); }
  }
}

// Grow a chat textarea to fit its content, up to `maxPx` then scroll. Used by
// the output reader's follow-up box so a long question wraps instead of
// scrolling horizontally inside one line.
function autosizeTextarea(el, maxPx = 160) {
  el.style.height = "auto";
  el.style.height = Math.min(el.scrollHeight, maxPx) + "px";
  el.style.overflowY = el.scrollHeight > maxPx ? "auto" : "hidden";
}

// Shared follow-up-chat renderer: append a Q card + answer to `threadEl`, POST
// `payload` to `endpoint`, and stream/poll the answer (⚡ quick / 🔧 deep).
// Used by the output reader (and shaped like the Live Transcribe ask).
async function askThread(threadEl, q, endpoint, payload) {
  const item = document.createElement("div");
  item.className = "qa-item";
  item.innerHTML = `<div class="qa-q">${esc(q)}</div><div class="qa-a"><span class="qa-tag">…</span><span class="qa-body"></span></div>`;
  threadEl.appendChild(item);
  item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  const tag = item.querySelector(".qa-tag"), bodyEl = item.querySelector(".qa-body");

  const res = await fetch(endpoint, {
    method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(payload),
  });
  const ctype = res.headers.get("content-type") || "";

  if (ctype.includes("application/json")) {
    const data = await res.json();
    if (data.mode === "deep") {
      tag.textContent = "🔧"; bodyEl.innerHTML = `<span class="muted">searching codebase &amp; skills…</span>`;
      if (data.persistence_warning) warnPersistence(`job:${data.job_id}`, data.persistence_warning);
      await pollJob(data.job_id, async (job) => {
        if (job.status !== "running") {
          const md = job.stdout || job.stderr || "(no output)";
          item.dataset.answerMd = md;   // raw md for "Download with Q&A"
          bodyEl.innerHTML = await mdToHtml(md);
        }
      });
    } else {
      tag.textContent = "⚠️"; bodyEl.textContent = data.reason || data.error || "Unavailable.";
    }
    return;
  }
  // SSE token stream (quick path)
  tag.textContent = "⚡";
  const reader = res.body.getReader(); const dec = new TextDecoder();
  let buf = "", acc = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    // Normalize CRLF → LF: sse-starlette emits "event: …\r\ndata: …\r\n\r\n",
    // so split-on-blank-line and the regex must not assume bare \n.
    buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    const events = buf.split("\n\n"); buf = events.pop();
    for (const ev of events) {
      const m = ev.match(/^event: (\w+)\ndata: (.*)$/ms);
      if (!m) continue;
      if (m[1] === "token") { const payload = JSON.parse(m[2]); acc += payload.text; item.dataset.answerMd = acc; bodyEl.innerHTML = addMdClasses(payload.html); }
      else if (m[1] === "error") { bodyEl.innerHTML = `<span class="muted">Error: ${esc(JSON.parse(m[2]).error)}</span>`; }
    }
  }
}

// Append a ⚙️-running skill card to the thread (shared by fresh invokes and the
// re-attach recovery). Returns { item, tag, bodyEl } for the caller to poll.
function appendInvokeCard(threadEl, q, skillId, oppName) {
  const item = document.createElement("div");
  item.className = "qa-item";
  item.innerHTML = `<div class="qa-q">${esc(q)}</div>`
    + `<div class="qa-a"><span class="qa-tag">⚙️</span><span class="qa-body">`
    + `<span class="muted">Running <strong>${esc(prettySkill(skillId))}</strong> for ${esc(oppName)}… `
    + `(keeps running even if you leave this page; result lands in Generated Outputs)</span></span></div>`;
  threadEl.appendChild(item);
  item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return { item, tag: item.querySelector(".qa-tag"), bodyEl: item.querySelector(".qa-body") };
}

// Poll an invoke job to completion and render the ✓ / ✕ end-state onto an
// already-appended card. Shared by invokeFromChat and reattachInvoke.
async function pollInvokeJob(card, jobId, skillId, ctx) {
  await pollJob(jobId, (job) => {
    if (job.status === "running") return;
    if (job.ok) {
      card.tag.textContent = "✓";
      const oppHref = `#/opp/${encodeURIComponent(ctx.account)}/${encodeURIComponent(ctx.slug)}/${encodeURIComponent(ctx.oppName)}`;
      card.bodyEl.innerHTML = `<strong>${esc(prettySkill(skillId))}</strong> finished — saved to `
        + `<a href="${oppHref}">Generated Outputs</a> for ${esc(ctx.oppName)}.`;
    } else {
      card.tag.textContent = "✕";
      card.bodyEl.innerHTML = `<span class="muted">${esc(prettySkill(skillId))} finished with an error. `
        + `Check the opportunity page, or try the Invoke Skill button there.</span>`;
    }
  });
}

// Run a skill from the output chat bar. Mirrors the opp-page invoke flow:
// POST /api/invoke (scoped to this opp), poll the job, and report that the
// result lands in the opp's Generated Outputs. `ctx` = { account, slug, oppName }.
async function invokeFromChat(threadEl, q, skill, ctx) {
  const card = appendInvokeCard(threadEl, q, skill.id, ctx.oppName);
  try {
    const payload = { account: ctx.account, opportunity: ctx.oppName, opp_slug: ctx.slug, skill: skill.id };
    const res = await invokeWithPlan(payload);
    if (res.persistence_warning) warnPersistence(`job:${res.job_id}`, res.persistence_warning);
    trackJob(res.job_id, { ...ctx, skill: skill.id });  // global toast when it finishes
    await pollInvokeJob(card, res.job_id, skill.id, ctx);
  } catch (e) {
    card.tag.textContent = "⚠️";
    card.bodyEl.innerHTML = `<span class="muted">Couldn't start ${esc(prettySkill(skill.id))}: ${esc(e.message)}</span>`;
  }
}

// Re-attach to a skill run that's still executing (started earlier from this
// chat, survived a leave/return). Renders the same ⚙️→✓/✕ card and polls it.
function reattachInvoke(threadEl, job, ctx) {
  const card = appendInvokeCard(threadEl, `Resumed: ${prettySkill(job.skill)}`, job.skill, ctx.oppName);
  trackJob(job.job_id, { ...ctx, skill: job.skill || null });  // global toast when it finishes
  pollInvokeJob(card, job.job_id, job.skill, ctx);
}

// Brief highlight on an anchored element (reuses the help-flash animation).
function flashEl(el) {
  el.classList.add("doc-flash");
  setTimeout(() => el.classList.remove("doc-flash"), 1200);
}

// ---- Page: live transcribe + AI copilot -----------------------------------
let _liveState = null;  // { sessionId, sse, timer, askThread } while recording

async function pageLive(account, slug, oppName) {
  setCrumbs([...(await accountCrumbs(account)), { label: account, href: `#/account/${encodeURIComponent(account)}` },
            { label: oppName, href: `#/opp/${encodeURIComponent(account)}/${encodeURIComponent(slug)}/${encodeURIComponent(oppName)}` },
            { label: "Live" }]);

  let devs = { devices: [], has_blackhole: false, model: "" };
  try { devs = await api("/api/audio-devices"); }
  catch (e) {
    view.innerHTML = `<div class="row"><div><h1>Live Transcribe</h1><p class="sub">${esc(account)} · ${esc(oppName)}</p></div></div>
      <div class="callout callout-blocker"><div class="callout-title">Audio capture unavailable</div>
      <div class="callout-body">${esc(e.message)}<br>Run <code>brew install portaudio</code>, then restart the app. See the README → Live Transcribe setup.</div></div>`;
    return;
  }

  const opts = (sel) => devs.devices.map((d) =>
    `<option value="${d.index}">${esc(d.name)} (${d.channels}ch)</option>`).join("");

  view.innerHTML = `
    <div class="row">
      <div><h1>🎙 Live Transcribe</h1><p class="sub">${esc(account)} · ${esc(oppName)} · model <code>${esc(devs.model)}</code></p></div>
    </div>
    <div class="live-past" id="live-past"></div>
    <div class="live-setup" id="live-setup">
      <label>Your mic
        <select id="dev-mic">${opts()}</select>
        <input type="text" id="mic-label" value="You" maxlength="80" placeholder="Label (e.g. You)" />
      </label>
      <label>Call audio — everyone else (optional)
        <select id="dev-call"><option value="">— none (single stream, no labels) —</option>${opts()}</select>
        <input type="text" id="call-label" value="Call" maxlength="80" placeholder="Label (e.g. Call)" />
      </label>
      <button class="primary" id="live-start">● Start</button>
      <span id="live-timer" class="live-timer hidden">00:00</span>
      <button class="danger live-stop hidden" id="live-stop">■ Stop &amp; Save</button>
      ${devs.has_blackhole ? "" : `<span class="live-hint">No BlackHole/Aggregate detected — only your mic is capturable until you set it up (README).</span>`}
    </div>
    <div class="live-layout">
      <section class="live-transcript" id="live-transcript">
        <div class="live-col-head">Transcript</div>
        <div class="live-segs" id="live-segs"><div class="empty">Press Start to begin transcribing.</div></div>
      </section>
      <section class="live-qa" id="live-qa">
        <div class="live-col-head">Copilot Q&amp;A <span class="ai-badge" id="live-ai-badge"></span></div>
        <div class="live-thread" id="live-thread"><div class="empty">Ask anything about the call below — e.g. “what do they actually need?”, “is a Snowflake connector feasible?”, “edge cases here?”</div></div>
      </section>
    </div>
    <div class="live-askbar">
      <textarea id="live-ask" rows="1" autocomplete="off" placeholder="Ask the copilot about the live call… (Shift+Enter for newline)" disabled></textarea>
      <button class="primary" id="live-send" disabled>Send</button>
    </div>`;

  const $ = (id) => document.getElementById(id);
  const segsEl = $("live-segs"), threadEl = $("live-thread");
  const askInput = $("live-ask"), sendBtn = $("live-send");
  const startBtn = $("live-start"), stopBtn = $("live-stop"), timerEl = $("live-timer");

  // ── AI path badge: tell the SE whether the fast ⚡ path is available ──
  // Without an Anthropic key the copilot still works via the slower deep
  // path — but silently, so the SE wonders why answers lag. Surface it.
  (async () => {
    const badge = $("live-ai-badge");
    if (!badge) return;
    try {
      const s = await api("/api/ai-status");
      if (s.quick_path) {
        badge.className = "ai-badge ok";
        badge.textContent = "⚡ fast path ready";
        badge.title = "Anthropic key found — simple questions stream instantly.";
      } else {
        badge.className = "ai-badge warn";
        badge.textContent = "⚠️ deep path only — add a key for ⚡";
        badge.title = "No ANTHROPIC_API_KEY found. Questions route through the slower claude -p path. "
          + "Add a key (see README → “AI ask-bar key”) for fast streaming answers.";
      }
    } catch { /* status is best-effort; leave the badge empty on failure */ }
  })();

  // ── Transcript rendering ────────────────────────────────────────────
  let micLabel = "You", callLabel = "Call";
  let hasSegs = false;
  const addSegment = (seg) => {
    if (!hasSegs) { segsEl.innerHTML = ""; hasSegs = true; }
    let whoClass = "";
    if (seg.speaker) {
      if (seg.speaker === micLabel) whoClass = "seg-you";
      else if (seg.speaker === callLabel) whoClass = "seg-call";
      else whoClass = "seg-unknown";
    }
    const who = seg.speaker ? `<span class="seg-who ${whoClass}">${esc(seg.speaker)}</span>` : "";
    const div = document.createElement("div");
    div.className = "live-seg";
    div.innerHTML = `<span class="seg-t">${esc(seg.t)}</span>${who}<span class="seg-text">${esc(seg.text)}</span>`;
    segsEl.appendChild(div);
    segsEl.scrollTop = segsEl.scrollHeight;
  };

  // ── Past transcripts: list saved files for this opp, reopen read-only ─
  const pastEl = $("live-past");
  const fmtSize = (n) => n > 1024 ? `${(n / 1024).toFixed(0)} KB` : `${n} B`;
  const loadPast = async () => {
    try {
      const { transcripts } = await api(`/api/transcripts?account=${encodeURIComponent(account)}`);
      if (!transcripts.length) { pastEl.innerHTML = ""; return; }
      pastEl.innerHTML = `<div class="live-past-head">Past transcripts</div>
        <div class="live-past-list">${transcripts.map((t) =>
          `<button class="past-item" data-name="${esc(t.name)}">
             <span class="past-name">${esc(t.name)}</span>
             <span class="past-meta">${fmtSize(t.size)}</span>
           </button>`).join("")}</div>`;
      pastEl.querySelectorAll(".past-item").forEach((b) =>
        b.onclick = () => openSaved(b.dataset.name));
    } catch { pastEl.innerHTML = ""; }  // best-effort; never block the page
  };

  // Open a saved transcript: render segments read-only and enable the copilot
  // in FILE mode so the SE can ask questions about a past call.
  const openSaved = async (name) => {
    if (_liveState && _liveState.sessionId !== "file") {
      if (!confirm("A recording is in progress. Open the saved transcript anyway?")) return;
    }
    try {
      const data = await api(`/api/transcripts/${encodeURIComponent(name)}?account=${encodeURIComponent(account)}`);
      micLabel = data.mic_label || "You";
      callLabel = data.call_label || "Call";
      if ($("mic-label")) $("mic-label").value = micLabel;
      if ($("call-label")) $("call-label").value = callLabel;
      segsEl.innerHTML = ""; hasSegs = false;
      data.segments.forEach(addSegment);
      segsEl.insertAdjacentHTML("afterbegin",
        `<div class="callout callout-note"><div class="callout-title">Reviewing saved transcript</div>
         <div class="callout-body"><code>${esc(name)}</code> — read-only. Ask the copilot about it below.</div></div>`);
      // File mode: ask-bar talks to the saved file, not a live session.
      _liveState = { sessionId: "file", transcriptName: name };
      askInput.disabled = sendBtn.disabled = false;
      askInput.placeholder = "Ask the copilot about this saved transcript… (Shift+Enter for newline)";
      threadEl.innerHTML = `<div class="empty">Ask anything about <code>${esc(name)}</code>.</div>`;
      threadHasItems = false;
    } catch (e) { alert("Could not open transcript: " + e.message); }
  };

  // ── Recording mode (shared by Start and reconnect-on-return) ─────────
  // Attaches the SSE stream (which replays existing segments), flips the UI to
  // recording, and runs the timer from `startedAtMs` so a reconnected session
  // shows correct elapsed time.
  const enterRecording = (sessionId, startedAtMs, labels = {}) => {
    micLabel = labels.micLabel || micLabel || "You";
    callLabel = labels.callLabel || callLabel || "Call";
    // tear down any prior client-side handles (avoid leaking a 2nd SSE/timer)
    if (_liveState) { try { _liveState.sse?.close(); } catch {} if (_liveState.timer) clearInterval(_liveState.timer); }
    _liveState = { sessionId, recovered: false };
    segsEl.innerHTML = ""; hasSegs = false;
    const sse = new EventSource(`/api/transcribe/${sessionId}/stream`);
    sse.addEventListener("segment", (e) => addSegment(JSON.parse(e.data)));
    _liveState.sse = sse;
    $("dev-mic").disabled = $("dev-call").disabled = true;
    $("mic-label").disabled = $("call-label").disabled = true;
    startBtn.classList.add("hidden");
    stopBtn.classList.remove("hidden"); stopBtn.disabled = false;
    stopBtn.textContent = "■ Stop & Save";
    timerEl.classList.remove("hidden"); timerEl.classList.add("rec");
    askInput.disabled = sendBtn.disabled = false;
    const t0 = startedAtMs || Date.now();
    const tick = () => {
      const s = Math.max(0, Math.floor((Date.now() - t0) / 1000));
      timerEl.textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    };
    tick();
    _liveState.timer = setInterval(tick, 1000);
  };

  // ── Recovered session mode ─────────────────────────────────────────
  // A session that was active when the app restarted has its transcript on
  // disk but can no longer capture audio. Show it read-only and let the SE save.
  const enterRecovered = (active) => {
    if (_liveState) { try { _liveState.sse?.close(); } catch {} if (_liveState.timer) clearInterval(_liveState.timer); }
    micLabel = active.mic_label || "You";
    callLabel = active.call_label || "Call";
    if ($("mic-label")) $("mic-label").value = micLabel;
    if ($("call-label")) $("call-label").value = callLabel;
    _liveState = { sessionId: active.session_id, recovered: true };
    segsEl.innerHTML = ""; hasSegs = false;
    active.segments.forEach(addSegment);
    segsEl.insertAdjacentHTML("afterbegin",
      `<div class="callout callout-note"><div class="callout-title">Recovered session</div>
       <div class="callout-body">The app restarted while this live session was active. Audio capture is stopped — click <b>Stop &amp; Save</b> to keep the recovered transcript.</div></div>`);
    startBtn.classList.add("hidden");
    stopBtn.classList.remove("hidden"); stopBtn.disabled = false;
    stopBtn.textContent = "■ Save recovered transcript";
    timerEl.classList.add("hidden");
    askInput.disabled = sendBtn.disabled = false;
    askInput.placeholder = "Ask the copilot about the recovered transcript… (Shift+Enter for newline)";
    $("dev-mic").disabled = $("dev-call").disabled = true;
    $("mic-label").disabled = $("call-label").disabled = true;
  };

  // ── Start / Stop ────────────────────────────────────────────────────
  startBtn.onclick = async () => {
    const mic = $("dev-mic").value;
    const call = $("dev-call").value || null;
    micLabel = ($("mic-label").value || "You").trim();
    callLabel = ($("call-label").value || "Call").trim();
    startBtn.disabled = true;
    try {
      const res = await api("/api/transcribe/start", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ account, opp_slug: slug, opportunity: oppName,
                               mic_device: Number(mic), call_device: call === null ? null : Number(call),
                               mic_label: micLabel, call_label: callLabel }),
      });
      enterRecording(res.session_id, Date.now(), { micLabel: res.mic_label, callLabel: res.call_label });
      if (res.persistence_warning) warnPersistence(`live:${res.session_id}`, res.persistence_warning);
    } catch (e) {
      alert("Could not start: " + e.message); startBtn.disabled = false;
    }
  };

  stopBtn.onclick = async () => {
    if (!_liveState) return;
    stopBtn.disabled = true;
    try {
      const res = await api(`/api/transcribe/${_liveState.sessionId}/stop`, { method: "POST" });
      if (_liveState.sse) _liveState.sse.close();
      if (_liveState.timer) clearInterval(_liveState.timer);
      timerEl.classList.remove("rec");
      if (res.persistence_warning) warnPersistence(`live:stop:${_liveState.sessionId}`, res.persistence_warning);
      const savedName = res.saved_to.split("/").slice(-1)[0];
      segsEl.insertAdjacentHTML("beforeend",
        `<div class="callout callout-verdict"><div class="callout-title">Saved (${res.segments} segments)</div>
         <div class="callout-body">Transcript written to <code>${esc(savedName)}</code>. The copilot stays available below — keep asking about this call.
         <button class="linklike" id="run-postcall">Run post-call summary →</button></div></div>`);
      segsEl.scrollTop = segsEl.scrollHeight;
      const rp = document.getElementById("run-postcall");
      if (rp) rp.onclick = () => openInvoke(account, { slug, name: oppName });
      // Keep the copilot alive: switch the ask-bar to FILE mode against the
      // just-saved transcript so the SE can keep querying the call.
      _liveState = { sessionId: "file", transcriptName: savedName };
      askInput.placeholder = "Ask the copilot about this saved transcript… (Shift+Enter for newline)";
      stopBtn.classList.add("hidden"); startBtn.classList.remove("hidden"); startBtn.disabled = false;
      $("dev-mic").disabled = $("dev-call").disabled = false;
      $("mic-label").disabled = $("call-label").disabled = false;
      loadPast();  // refresh the past-transcripts list to include this one
    } catch (e) { alert("Stop failed: " + e.message); stopBtn.disabled = false; }
  };

  // ── Ask bar ─────────────────────────────────────────────────────────
  let threadHasItems = false;
  const ask = async () => {
    const q = askInput.value.trim();
    if (!q || !_liveState) return;
    askInput.value = "";
    askInput.style.height = "";  // reset autogrow
    if (!threadHasItems) { threadEl.innerHTML = ""; threadHasItems = true; }
    const ts = new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    const item = document.createElement("div");
    item.className = "qa-item";
    item.innerHTML = `<div class="qa-q"><span class="qa-time">${ts}</span>${esc(q)}</div><div class="qa-a"><span class="qa-tag">…</span><span class="qa-body"></span></div>`;
    threadEl.appendChild(item);
    threadEl.scrollTop = threadEl.scrollHeight;
    const tag = item.querySelector(".qa-tag"), bodyEl = item.querySelector(".qa-body");

    const payload = { question: q };
    if (_liveState.sessionId === "file") {           // saved-transcript ask
      payload.transcript_name = _liveState.transcriptName;
      payload.account = account;
      payload.opportunity = oppName;
    }
    const res = await fetch(`/api/transcribe/${_liveState.sessionId}/ask`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(payload),
    });
    const ctype = res.headers.get("content-type") || "";

    if (ctype.includes("application/json")) {
      const data = await res.json();
      if (data.mode === "deep") {           // claude -p job → poll
        tag.textContent = "🔧"; bodyEl.innerHTML = `<span class="muted">searching codebase &amp; skills…</span>`;
        if (data.persistence_warning) warnPersistence(`job:${data.job_id}`, data.persistence_warning);
        await pollJob(data.job_id, async (job) => {
          if (job.status !== "running") {
            bodyEl.innerHTML = await mdToHtml(job.stdout || job.stderr || "(no output)");
            threadEl.scrollTop = threadEl.scrollHeight;
          }
        });
      } else {                              // needs_deep / error
        tag.textContent = "⚠️"; bodyEl.textContent = data.reason || data.error || "Unavailable.";
      }
      return;
    }

    // SSE token stream (quick path)
    tag.textContent = "⚡";
    bodyEl.innerHTML = `<span class="muted">thinking…</span>`;
    const reader = res.body.getReader(); const dec = new TextDecoder();
    let buf = "", acc = "", gotError = false;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true }).replace(/\r\n/g, "\n");  // CRLF → LF (sse-starlette)
        const events = buf.split("\n\n"); buf = events.pop();
        for (const ev of events) {
          const m = ev.match(/^event: (\w+)\ndata: (.*)$/ms);
          if (!m) continue;
          if (m[1] === "token") { const payload = JSON.parse(m[2]); acc += payload.text; bodyEl.innerHTML = addMdClasses(payload.html); threadEl.scrollTop = threadEl.scrollHeight; }
          else if (m[1] === "error") { gotError = true; tag.textContent = "⚠️"; bodyEl.innerHTML = `<span class="muted">Error: ${esc(JSON.parse(m[2]).error)}</span>`; }
        }
      }
    } catch (e) {
      gotError = true; tag.textContent = "⚠️";
      bodyEl.innerHTML = `<span class="muted">Connection dropped: ${esc(e.message)}</span>`;
    }
    // Guard against a silent empty stream (no tokens, no error) — never leave a blank answer.
    if (!acc && !gotError) {
      tag.textContent = "⚠️";
      bodyEl.innerHTML = `<span class="muted">No response — the copilot returned nothing. Try re-asking, or include a word like “connector”/“codebase” to route to the deep path.</span>`;
    }
  };
  sendBtn.onclick = ask;
  askInput.onkeydown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(); }
    // Shift+Enter falls through → textarea inserts a newline.
  };
  // Autogrow the textarea up to a few lines as the SE types.
  askInput.oninput = () => {
    askInput.style.height = "auto";
    askInput.style.height = Math.min(askInput.scrollHeight, 120) + "px";
  };

  // Populate the past-transcripts list on load.
  loadPast();

  // ── Reconnect if a session is active or recovered for this opp ───────
  // Leaving and returning re-renders this page, but the session lives on the
  // server — pick it back up so the transcript isn't lost.
  const active = await api(`/api/transcribe/active?account=${encodeURIComponent(account)}&opp_slug=${encodeURIComponent(slug)}`).catch(() => null);
  if (active && active.session_id) {
    if (active.recovered) {
      enterRecovered(active);
    } else {
      enterRecording(active.session_id, (active.started_at || 0) * 1000,
                    { micLabel: active.mic_label, callLabel: active.call_label });
    }
    if (active.persistence_warning) warnPersistence(`live:${active.session_id}`, active.persistence_warning);
  }
}

// ---- Invoke modal ---------------------------------------------------------
const modal = document.getElementById("modal");
// opp = { slug, name } | null
// Build the skill picker <option>s grouped by tier, with a step number prefixed
// to workflow skills (e.g. "1 · PREP CALL"). Tiers keep the order they first
// appear in SKILLS (already dependency-sorted by the backend). Labels use the
// pretty upper-case form (PREP CALL) per the display convention.
function skillOptionsGrouped() {
  const tiers = [];                       // preserve first-seen tier order
  const byTier = {};
  for (const s of SKILLS) {
    const t = s.tier || "Other";
    if (!byTier[t]) { byTier[t] = []; tiers.push(t); }
    byTier[t].push(s);
  }
  return tiers.map((t) => {
    const opts = byTier[t].map((s) => {
      const num = s.step ? `${s.step} · ` : "";
      return `<option value="${s.id}">${num}${esc(prettySkill(s.id))}</option>`;
    }).join("");
    return `<optgroup label="${esc(t)}">${opts}</optgroup>`;
  }).join("");
}

function openInvoke(account, opp = null) {
  const ctx = opp ? `${account} · ${opp.name}` : account;
  document.getElementById("modal-title").textContent = `Invoke — ${ctx}`;
  const sel = document.getElementById("skill-select");
  sel.innerHTML = skillOptionsGrouped();

  const summaryEl = document.getElementById("skill-summary");
  const detailsEl = document.getElementById("skill-details");
  const moreBtn = document.getElementById("skill-more");
  const prereqEl = document.getElementById("skill-prereq");
  const permEl = document.getElementById("skill-perm");
  const tierEl = document.getElementById("skill-tier");

  const para = (text) => esc(text).split("\n").filter((l) => l.trim()).map((l) => `<p>${l}</p>`).join("");

  const renderDetails = () => {
    const h = SKILLS_HELP[sel.value] || {};
    const parts = [];
    if (h.description) {
      parts.push(`<div class="skill-detail-section"><div class="skill-detail-label">What it does</div><div class="skill-detail-body">${para(h.description)}</div></div>`);
    }
    if (h.output_location) {
      parts.push(`<div class="skill-detail-section"><div class="skill-detail-label">Saves to</div><div class="skill-detail-body">${esc(h.output_location)}</div></div>`);
    }
    if (h.triggers?.length) {
      const chips = h.triggers.slice(0, 6).map((t) => `<code class="skill-trig">${esc(t)}</code>`).join(" ");
      parts.push(`<div class="skill-detail-section"><div class="skill-detail-label">Triggers</div><div class="skill-detail-body">${chips}</div></div>`);
    }
    if (h.methodologies?.length) {
      const chips = h.methodologies.map((m) => `<span class="skill-chip">${esc(m)}</span>`).join(" ");
      parts.push(`<div class="skill-detail-section"><div class="skill-detail-label">Methodology</div><div class="skill-detail-body">${chips}</div></div>`);
    }
    detailsEl.innerHTML = parts.join("");
  };

  const setSkillInfo = () => {
    const h = SKILLS_HELP[sel.value] || {};
    const base = SKILLS.find((s) => s.id === sel.value) || {};

    summaryEl.textContent = h.summary || base.blurb || "";
    renderDetails();
    detailsEl.classList.add("hidden");
    const hasDetails = h.description && h.description !== (h.summary || base.blurb || "");
    moreBtn.classList.toggle("hidden", !hasDetails);
    moreBtn.textContent = "Show details";

    if (base.tier) {
      const short = base.step ? `Step ${base.step}` : base.tier.split(" —")[0];
      tierEl.textContent = short;
      tierEl.title = base.tier;
      tierEl.classList.remove("hidden");
    } else {
      tierEl.classList.add("hidden");
    }

    if (h.prerequisites) {
      const lines = h.prerequisites.split("\n").map((l) => l.trim()).filter(Boolean);
      const first = lines[0];
      const rest = lines.slice(1).join("\n");
      const more = rest
        ? `<button class="linklike prereq-more" type="button">Show more</button><div class="prereq-rest hidden">${para(rest)}</div>`
        : "";
      prereqEl.innerHTML = `<div class="skill-disclosure-head"><span class="skill-disclosure-icon">ℹ️</span><b>Prerequisite</b></div><div class="skill-disclosure-body">${esc(first)}${more}</div>`;
      prereqEl.classList.remove("hidden");
      if (rest) {
        prereqEl.querySelector(".prereq-more").onclick = (e) => {
          const restEl = prereqEl.querySelector(".prereq-rest");
          restEl.classList.toggle("hidden");
          e.target.textContent = restEl.classList.contains("hidden") ? "Show more" : "Show less";
        };
      }
    } else {
      prereqEl.classList.add("hidden");
      prereqEl.innerHTML = "";
    }
  };

  const setPermissions = () => {
    const base = SKILLS.find((s) => s.id === sel.value) || {};
    const p = base.permissions || {};
    if (p.requires_approval) {
      const chips = [];
      if (p.write) chips.push(`<span class="skill-perm-chip">writes a file</span>`);
      if (p.shell) chips.push(`<span class="skill-perm-chip skill-perm-chip--warn">runs shell commands</span>`);
      if (p.git) chips.push(`<span class="skill-perm-chip skill-perm-chip--warn">runs git commands</span>`);
      const summary = p.summary ? `<div class="skill-perm-summary">${esc(p.summary)}</div>` : "";
      permEl.innerHTML = `<div class="skill-disclosure-head"><span class="skill-disclosure-icon">🔒</span><b>Expected permissions</b></div><div class="skill-disclosure-body"><div class="skill-perm-chips">${chips.join("")}</div>${summary}<div class="skill-perm-mode">Runs with <code>--permission-mode acceptEdits</code>.</div></div>`;
      permEl.classList.remove("hidden");
    } else {
      permEl.classList.add("hidden");
      permEl.innerHTML = "";
    }
  };

  moreBtn.onclick = () => {
    detailsEl.classList.toggle("hidden");
    const showing = !detailsEl.classList.contains("hidden");
    moreBtn.textContent = showing ? "Hide details" : "Show details";
    if (showing && !detailsEl.innerHTML) renderDetails();
  };

  sel.onchange = () => { setSkillInfo(); setPermissions(); };
  setSkillInfo(); setPermissions();
  document.getElementById("skill-extra").value = "";

  // Runtime skill discovery — reload skills from disk without restarting the app.
  const refreshBtn = document.getElementById("skill-refresh");
  if (refreshBtn) refreshBtn.onclick = async () => {
    refreshBtn.disabled = true; refreshBtn.textContent = "…";
    try {
      await api("/api/reload", { method: "POST" });
      SKILLS = await api("/api/skills");
      try { SKILLS_HELP = Object.fromEntries((await api("/api/skills/help")).map((h) => [h.id, h])); } catch {}
      const prev = sel.value;
      sel.innerHTML = skillOptionsGrouped();
      if ([...sel.options].some((o) => o.value === prev)) sel.value = prev;
      setSkillInfo();
      setPermissions();
      showToast("Skills reloaded", "ok");
    } catch (e) {
      showToast("Reload failed: " + e.message, "err");
    } finally {
      refreshBtn.disabled = false; refreshBtn.textContent = "↻";
    }
  };

  const status = document.getElementById("invoke-status");
  const output = document.getElementById("invoke-output");
  status.className = "status hidden"; output.className = "output hidden"; output.textContent = "";
  modal.classList.remove("hidden");

  document.getElementById("invoke-cancel").onclick = () => modal.classList.add("hidden");
  const runBtn = document.getElementById("invoke-run");
  runBtn.onclick = async () => {
    const payload = { account, opportunity: opp?.name || null, opp_slug: opp?.slug || null };
    payload.skill = sel.value;
    payload.extra = document.getElementById("skill-extra").value.trim() || null;
    status.className = "status running";
    status.innerHTML = `<span class="run-head"><span class="spinner"></span>Starting…</span>`;
    runBtn.disabled = true;
    try {
      await invokeWithPlan(payload);
      // Close the modal immediately and let the opportunity page take over —
      // re-rendering it re-attaches to the now-running job and shows the live
      // status + result inline (same place freebar results land).
      modal.classList.add("hidden");
      runBtn.disabled = false;
      if (opp) pageOpportunity(account, opp.slug, opp.name);
    } catch (e) {
      status.className = "status err"; status.textContent = "Error: " + e.message;
      runBtn.disabled = false;
    }
  };
}

// ---- Page: skills help ----------------------------------------------------
async function pageHelp() {
  setCrumbs([{ label: "Team", href: "#/" }, { label: "Skills Help" }]);
  const help = await api("/api/skills/help");

  // A labeled row; `body` is plain text rendered as clean prose (bullets, line breaks).
  const row = (label, body) => {
    if (!body) return "";
    const htmlBody = esc(body)
      .replace(/^• /gm, "")                         // bullets handled by list below
      .split("\n").filter((l) => l.trim());
    const isList = body.split("\n").filter((l) => l.trim().startsWith("•")).length >= 2;
    const content = isList
      ? `<ul class="help-ul">${htmlBody.map((l) => `<li>${l}</li>`).join("")}</ul>`
      : `<div class="help-prose">${htmlBody.map((l) => `<p>${l}</p>`).join("")}</div>`;
    return `<div class="help-row"><div class="help-key">${label}</div><div class="help-val">${content}</div></div>`;
  };
  const chips = (label, arr, cls) =>
    arr && arr.length
      ? `<div class="help-row"><div class="help-key">${label}</div><div class="help-val">${arr.map((x) => `<span class="chip ${cls}">${esc(x)}</span>`).join(" ")}</div></div>`
      : "";
  const relatedChips = (label, ids) =>
    ids && ids.length
      ? `<div class="help-row"><div class="help-key">${label}</div><div class="help-val">${ids.map((id) => `<a href="#help-${id}" class="chip chip-skill">${esc(id)}</a>`).join(" ")}</div></div>`
      : "";

  view.innerHTML = `
    <h1>Skills Help</h1>
    <p class="sub">Every skill you can invoke — what it does, the methodology behind it, what it needs, and how it works. Auto-generated from the skill files, always current.</p>
    <div class="help-toc">
      ${help.map((s) => `<a href="#help-${s.id}" class="toc-item">${esc(s.label)}</a>`).join("")}
    </div>
    <div class="help-list">
      ${help.map((s) => `
        <section class="help-card" id="help-${s.id}">
          <div class="help-head">
            <h2>${esc(s.label)}</h2>
            <code class="skill-id">${esc(s.id)}</code>
          </div>
          <p class="help-summary">${esc(s.summary)}</p>
          ${row("What it does", s.description)}
          ${chips("Methodology", s.methodologies, "chip-method")}
          ${chips("Triggers", s.triggers, "chip-trigger")}
          ${row("How it works", s.how_it_works)}
          ${relatedChips("Works with", s.related_skills)}
          ${row("Prerequisites", s.prerequisites)}
          ${row("Data sources", s.data_sources)}
          ${s.output_location ? row("Output saved to", s.output_location) : ""}
          ${!s.found ? `<div class="help-warn">⚠️ SKILL.md not found — install the skills (./install.sh)</div>` : ""}
        </section>`).join("")}
    </div>`;
}

// ---- Theme toggle ---------------------------------------------------------
function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.textContent = theme === "light" ? "☀️" : "🌙";
}
function initTheme() {
  const saved = localStorage.getItem("se-hub-theme") || "dark";
  applyTheme(saved);
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.onclick = () => {
    const next = (document.documentElement.getAttribute("data-theme") === "light") ? "dark" : "light";
    localStorage.setItem("se-hub-theme", next);
    applyTheme(next);
  };
}

// ---- Router ---------------------------------------------------------------
async function route() {
  const h = location.hash.slice(1) || "/";
  try {
    // In-page anchor on the Help page (TOC chips → scroll to a skill card).
    // Don't re-route; just ensure Help is rendered and scroll to the card.
    if (h.startsWith("help-")) {
      if (!document.getElementById(h)) await pageHelp();
      const el = document.getElementById(h);
      if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); el.classList.add("help-flash"); setTimeout(() => el.classList.remove("help-flash"), 1200); }
      return;
    }
    // Doc sidebar TOC links use "#toc-…" — handled locally in openOutput; ignore here.
    if (h.startsWith("toc-")) return;
    // In-doc "Jump to" links target a heading id on the current page — scroll, don't route.
    if (!h.startsWith("/") && document.getElementById(h)) {
      const el = document.getElementById(h);
      el.scrollIntoView({ behavior: "smooth", block: "start" }); flashEl(el);
      return;
    }
    if (h === "/" || h === "") return pageMembers();
    if (h === "/help") return pageHelp();
    const parts = h.split("/");          // ["", kind, arg, ...]
    const kind = parts[1];
    if (kind === "member") return pageMember(decodeURIComponent(parts[2]));
    if (kind === "account") return pageAccount(decodeURIComponent(parts[2]));
    if (kind === "opp") return pageOpportunity(
      decodeURIComponent(parts[2]), decodeURIComponent(parts[3]), decodeURIComponent(parts[4] || parts[3]));
    if (kind === "output") {
      const account = decodeURIComponent(parts[2]);
      const slug = decodeURIComponent(parts[3]);
      const oppName = decodeURIComponent(parts[4] || parts[3]);
      // parts[5] is the double-encoded path; decode once → the single-encoded
      // form openOutput expects (it decodeURIComponent's internally).
      const path = decodeURIComponent(parts.slice(5).join("/"));
      return openOutput(path, "", { account, slug, oppName });
    }
    if (kind === "live") return pageLive(
      decodeURIComponent(parts[2]), decodeURIComponent(parts[3]), decodeURIComponent(parts[4] || parts[3]));
    pageMembers();
  } catch (e) {
    view.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

// Global: close any open card dropdown when clicking outside it, or on Escape.
// Attached once (not per-render) so listeners don't stack.
function closeAllDropdowns() {
  document.querySelectorAll(".dropdown-menu").forEach((mn) => mn.classList.add("hidden"));
}
document.addEventListener("click", (e) => {
  // If the click wasn't on a kebab button or inside an open menu, close everything.
  if (!e.target.closest(".kebab") && !e.target.closest(".dropdown-menu")) {
    closeAllDropdowns();
  }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAllDropdowns(); });

// ── Output review/feedback panel (UX-001) ────────────────────────────────
// Loads and records SE approval / comments / corrections on a generated output.
// Feedback is stored server-side in a sidecar JSONL file so it persists with the doc.
async function loadFeedbackPanel(path) {
  const panel = document.getElementById("feedback-panel");
  if (!panel) return;

  const safePath = encodeURIComponent(decodeURIComponent(path));
  let entries = [];
  try {
    const data = await api("/api/output/feedback?path=" + safePath);
    entries = data.entries || [];
  } catch (e) {
    panel.innerHTML = `<div class="feedback-empty">Could not load feedback: ${esc(e.message)}</div>`;
    return;
  }

  const actionLabels = { approve: "Approve", comment: "Comment", correct: "Correct" };
  const actionIcons = { approve: "✓", comment: "💬", correct: "✎" };
  const actionClass = { approve: "ok", comment: "info", correct: "warn" };

  function renderForm(selectedAction) {
    return `
      <div class="feedback-form${selectedAction ? " show" : ""}" id="feedback-form">
        <textarea id="feedback-comment" placeholder="What would you change? What did the model get right or wrong?" rows="2"></textarea>
        <div class="feedback-form-row">
          <input type="text" id="feedback-author" placeholder="Your name (optional)" />
          <button class="primary small" id="feedback-submit" data-action="${selectedAction || "comment"}">${actionLabels[selectedAction || "comment"]}</button>
          <button class="ghost small" id="feedback-cancel">Cancel</button>
        </div>
      </div>`;
  }

  function renderEntries() {
    if (!entries.length) return `<div class="feedback-empty">No review yet</div>`;
    const latest = entries[entries.length - 1];
    const latestText = `${actionIcons[latest.action] || "•"} ${actionLabels[latest.action] || esc(latest.action)}${latest.author ? ` by ${esc(latest.author)}` : ""}${latest.timestamp ? ` · ${new Date(latest.timestamp).toLocaleDateString()}` : ""}`;
    const list = entries.slice().reverse().map((e) => `
      <div class="feedback-entry feedback-entry--${esc(e.action)}">
        <div class="feedback-meta">${actionIcons[e.action] || "•"} ${actionLabels[e.action] || esc(e.action)} ${e.author ? `by ${esc(e.author)}` : ""} · ${e.timestamp ? new Date(e.timestamp).toLocaleString() : ""}</div>
        ${e.comment ? `<div class="feedback-body">${esc(e.comment)}</div>` : ""}
      </div>
    `).join("");
    return `<div class="feedback-latest feedback-latest--${actionClass[latest.action] || "info"}">${latestText}${entries.length > 1 ? ` <span class="feedback-count">(${entries.length})</span>` : ""}</div>
      <div class="feedback-entries" id="feedback-entries-list">${list}</div>`;
  }

  const status = entries.length ? "reviewed" : "none";
  const statusIcon = status === "reviewed" ? actionIcons[entries[entries.length - 1].action] : "•";
  const statusText = status === "reviewed"
    ? `${actionLabels[entries[entries.length - 1].action]} by ${entries[entries.length - 1].author || "someone"}`
    : "No review yet";

  panel.innerHTML = `
    <div class="feedback-head">
      <div class="feedback-status" title="Latest review status">
        <span class="feedback-status-icon">${statusIcon}</span>
        <span class="feedback-status-text">${esc(statusText)}</span>
      </div>
      <div class="feedback-actions">
        <button class="ghost small feedback-toggle" data-action="approve" title="Approve">✓ Approve</button>
        <button class="ghost small feedback-toggle" data-action="comment" title="Comment">💬 Comment</button>
        <button class="ghost small feedback-toggle" data-action="correct" title="Correct">✎ Correct</button>
      </div>
    </div>
    ${renderForm("")}
    <div id="feedback-entries">${renderEntries()}</div>
  `;

  const form = document.getElementById("feedback-form");
  const toggles = panel.querySelectorAll(".feedback-toggle");
  let currentAction = "";

  toggles.forEach((btn) => {
    btn.onclick = () => {
      const a = btn.dataset.action;
      currentAction = currentAction === a ? "" : a;
      toggles.forEach((b) => b.classList.toggle("active", b.dataset.action === currentAction));
      form.classList.toggle("show", !!currentAction);
      const submit = document.getElementById("feedback-submit");
      if (submit) {
        submit.textContent = actionLabels[currentAction] || "Save";
        submit.dataset.action = currentAction;
      }
    };
  });

  const cancel = document.getElementById("feedback-cancel");
  if (cancel) cancel.onclick = () => {
    currentAction = "";
    toggles.forEach((b) => b.classList.remove("active"));
    form.classList.remove("show");
  };

  const submit = document.getElementById("feedback-submit");
  if (submit) submit.onclick = async () => {
    const action = submit.dataset.action || currentAction || "comment";
    const comment = document.getElementById("feedback-comment").value || "";
    const author = document.getElementById("feedback-author").value || "";
    try {
      await api("/api/output/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: decodeURIComponent(path), action, comment: comment.trim(), author: author.trim() }),
      });
      await loadFeedbackPanel(path);
    } catch (e) {
      showToast("Feedback failed: " + e.message, "err");
    }
  };

}

// ── Deal-assessment diff / trend view (UX-002) ───────────────────────────
// Opens a modal for comparing two `deal-assessment` outputs side-by-side.
async function openDealDiffModal(ctx, currentPath) {
  const outs = await api(`/api/accounts/${encodeURIComponent(ctx.account)}/outputs?opp=${encodeURIComponent(ctx.slug)}`).catch(() => []);
  const da = outs.filter((o) => o.skill === "deal-assessment").sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  if (da.length < 2) { showToast("Need at least two deal-assessment outputs to compare.", "err"); return; }
  const currentIdx = da.findIndex((o) => o.path === currentPath);
  const rightIdx = currentIdx >= 0 ? currentIdx : 0;
  const leftIdx = Math.min(rightIdx + 1, da.length - 1);

  const opts = (sel) => da.map((o, i) => `<option value="${esc(o.path)}"${i === sel ? " selected" : ""}>${esc(o.filename)}</option>`).join("");

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.innerHTML = `
    <div class="modal diff-modal">
      <div class="modal-head"><h2>Deal Assessment — what changed</h2><button class="modal-close">✕</button></div>
      <div class="diff-form">
        <label>Older<select id="diff-left">${opts(leftIdx)}</select></label>
        <label>Newer<select id="diff-right">${opts(rightIdx)}</select></label>
        <button class="primary small" id="diff-run">Compare</button>
      </div>
      <div class="diff-body" id="diff-body"><div class="feedback-empty">Select two deal assessments and click Compare.</div></div>
      <div class="modal-foot"><button class="ghost small modal-cancel">Close</button></div>
    </div>`;
  document.body.appendChild(overlay);
  const close = () => overlay.remove();
  overlay.querySelector(".modal-close").onclick = close;
  overlay.querySelector(".modal-cancel").onclick = close;
  overlay.onclick = (e) => { if (e.target === overlay) close(); };

  async function runDiff() {
    const left = document.getElementById("diff-left").value;
    const right = document.getElementById("diff-right").value;
    if (left === right) { showToast("Select two different assessments to compare.", "err"); return; }
    try {
      const data = await api("/api/output/diff", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ left, right }),
      });
      const body = document.getElementById("diff-body");
      const rowHtml = (r) => {
        const c = r.type;
        const l = r.left !== null ? `<div class="diff-cell diff-left ${c === "delete" || c === "replace" ? "diff-del" : ""}">${esc(r.left)}</div>` : `<div class="diff-cell diff-left diff-empty"></div>`;
        const rgt = r.right !== null ? `<div class="diff-cell diff-right ${c === "insert" || c === "replace" ? "diff-ins" : ""}">${esc(r.right)}</div>` : `<div class="diff-cell diff-right diff-empty"></div>`;
        return `<div class="diff-row ${c}">${l}${rgt}</div>`;
      };
      body.innerHTML = `
        <div class="diff-legend">
          <span class="diff-legend-del">Removed in newer</span>
          <span class="diff-legend-ins">Added in newer</span>
          <span class="diff-legend-rep">Changed</span>
        </div>
        <div class="diff-grid">
          <div class="diff-header">${esc(data.left_title || left)}</div>
          <div class="diff-header">${esc(data.right_title || right)}</div>
          ${data.rows.map(rowHtml).join("")}
        </div>`;
    } catch (e) {
      showToast("Diff failed: " + e.message, "err");
    }
  }

  document.getElementById("diff-run").onclick = runDiff;
  // Default to comparing immediately if a previous sibling was auto-selected.
  if (da.length >= 2 && leftIdx !== rightIdx) await runDiff();
}

(async function init() {
  initTheme();
  try { SKILLS = await api("/api/skills"); } catch { SKILLS = []; }
  try {
    const help = await api("/api/skills/help");
    SKILLS_HELP = Object.fromEntries(help.map((h) => [h.id, h]));
  } catch { SKILLS_HELP = {}; }
  window.addEventListener("hashchange", route);
  route();
})();
