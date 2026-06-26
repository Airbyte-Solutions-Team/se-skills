// SE Skills — local hub frontend. Tiny hash-router, no build step.
const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.headers.get("content-type")?.includes("application/json") ? r.json() : r.text();
};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// "prep-call" → "PREP CALL", "deal-assessment" → "DEAL ASSESSMENT"
const prettySkill = (id) => (id || "").replace(/[-_]/g, " ").toUpperCase();

// ---- Output download (PDF via browser print, MD as raw file) -------------
// `path` is URI-encoded relative-to-CUSTOMERS_DIR (as stored on .out-item).
async function fetchOutputText(path) {
  return api("/api/output?path=" + encodeURIComponent(decodeURIComponent(path)));
}
function baseName(path) {
  const p = decodeURIComponent(path);
  return p.slice(p.lastIndexOf("/") + 1) || "output.md";
}
// Direct download of the raw .md source file.
async function downloadMd(path) {
  const text = await fetchOutputText(path);
  const blob = new Blob([text], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = baseName(path);
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
// Clean print view → browser "Save as PDF". Renders the same markdown the reader
// uses (callouts, ==highlights==, tables) but without app chrome or the TOC.
async function downloadPdf(path) {
  const text = await fetchOutputText(path);
  const bodyHtml = mdToHtml(text, null);
  const title = baseName(path).replace(/\.md$/i, "");
  const w = window.open("", "_blank");
  if (!w) { alert("Allow pop-ups to export PDF, or use Cmd+P on the document."); return; }
  w.document.write(`<!doctype html><html><head><meta charset="utf-8">
    <title>${esc(title)}</title>
    <link rel="stylesheet" href="/style.css?v=print">
    <style>
      html,body{background:#fff;color:#1a1d24;}
      body{max-width:7.5in;margin:0 auto;padding:0.5in;}
      .md-key{background:#eef1fb;color:#3b5bdb;font-weight:600;padding:0 3px;border-radius:3px;}
      @media print{body{padding:0;max-width:none;} a[href]:after{content:"";}}
    </style></head>
    <body class="print-doc"><article class="md-body print-md">${bodyHtml}</article>
    <script>window.onload=function(){setTimeout(function(){window.print();},250);};<\/script>
    </body></html>`);
  w.document.close();
}
// Build a ⬇ download control with a PDF/MD popup menu. `cls` lets callers add
// position modifiers. Wire it up afterward with wireDownloadMenus(root).
function downloadMenuHtml(path, cls = "") {
  return `<span class="dl-menu ${cls}" data-path="${path}">
    <button class="dl-btn" title="Download" aria-label="Download">⬇</button>
    <div class="dropdown-menu hidden">
      <button class="menu-item dl-pdf">Download PDF</button>
      <button class="menu-item dl-md">Download Markdown (.md)</button>
      <button class="menu-item danger dl-del">Delete output</button>
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
    // Delete is two-click: first click arms ("Delete — confirm?"), second deletes.
    const del = wrap.querySelector(".dl-del");
    del.onclick = async (e) => {
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

// Poll a background job until it finishes. `onTick` gets the job snapshot each
// poll; resolves with the final job. Polling is independent of any page — if
// you navigate away and the element is gone, onTick simply no-ops.
async function pollJob(jobId, onTick) {
  while (true) {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`).catch(() => null);
    if (!job) return null;
    if (onTick) onTick(job);
    if (job.status !== "running") return job;
    await new Promise((r) => setTimeout(r, 2000));
  }
}

// Minimal, dependency-free Markdown → HTML for rendering skill outputs nicely.
// Handles: headings, bold/italic, inline code, code fences, tables, blockquotes,
// hr, ordered/unordered lists (incl. [ ] checkboxes), paragraphs.
// `toc` (optional) collects {level, text, id} for an auto-generated index.
function mdToHtml(md, toc) {
  const lines = (md || "").replace(/\r\n/g, "\n").split("\n");
  let html = "", i = 0;
  const seenIds = {};  // de-dupe heading ids within this render
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/==([^=]+)==/g, '<mark class="md-key">$1</mark>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  while (i < lines.length) {
    const ln = lines[i];

    // code fence
    if (/^```/.test(ln)) {
      let buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i++; }
      i++; html += `<pre class="md-pre"><code>${esc(buf.join("\n"))}</code></pre>`; continue;
    }
    // table (header row + --- separator)
    if (/\|/.test(ln) && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s:|-]*$/.test(lines[i + 1])) {
      const row = (r) => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
      const head = row(ln);
      i += 2; const body = [];
      while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) { body.push(row(lines[i])); i++; }
      html += `<table class="md-table"><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>${
        body.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      continue;
    }
    // heading — emit a slug id and (optionally) collect into the TOC
    const h = ln.match(/^(#{1,6})\s+(.*)$/);
    if (h) {
      const lvl = h[1].length;
      let id = slugify(h[2]) || "section";
      if (seenIds[id] != null) { seenIds[id]++; id = `${id}-${seenIds[id]}`; } else { seenIds[id] = 0; }
      if (toc) toc.push({ level: lvl, text: stripInline(h[2]), id });
      html += `<h${lvl} id="${id}" class="md-h md-h${lvl}">${inline(h[2])}</h${lvl}>`;
      i++; continue;
    }
    // hr
    if (/^\s*---\s*$/.test(ln)) { html += "<hr class='md-hr'/>"; i++; continue; }
    // callout — GitHub-style admonition: > [!verdict|risk|blocker|info] title
    // MUST come before the generic blockquote branch (which would swallow it).
    const cm = ln.match(/^>\s*\[!(verdict|risk|blocker|info)\]\s*(.*)$/i);
    if (cm) {
      const type = cm[1].toLowerCase();
      const title = cm[2].trim();
      let buf = []; i++;
      while (i < lines.length && /^>\s?/.test(lines[i]) && !/^>\s*\[!/.test(lines[i])) {
        buf.push(lines[i].replace(/^>\s?/, "")); i++;
      }
      const body = buf.filter((l) => l.trim()).map((l) => inline(l)).join("<br>");
      html += `<div class="callout callout-${type}">`
        + (title ? `<div class="callout-title">${inline(title)}</div>` : "")
        + (body ? `<div class="callout-body">${body}</div>` : "")
        + `</div>`;
      continue;
    }
    // blockquote (plain — no [!type] marker)
    if (/^>\s?/.test(ln)) {
      let buf = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, "")); i++; }
      html += `<blockquote class="md-quote">${inline(buf.join(" "))}</blockquote>`; continue;
    }
    // lists
    if (/^\s*([-*]|\d+\.)\s+/.test(ln)) {
      const ordered = /^\s*\d+\.\s+/.test(ln);
      let buf = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        let item = lines[i].replace(/^\s*([-*]|\d+\.)\s+/, "");
        // Checkbox items render their own ☐/☑ glyph, so suppress the CSS list
        // marker (via .md-check) to avoid a double bullet (disc + box).
        const isCheck = /^\[(?: |[xX])\]\s*/.test(item);
        item = item.replace(/^\[ \]\s*/, "☐ ").replace(/^\[[xX]\]\s*/, "☑ ");
        buf.push(`<li${isCheck ? ' class="md-check"' : ""}>${inline(item)}</li>`); i++;
      }
      html += ordered ? `<ol class="md-list">${buf.join("")}</ol>` : `<ul class="md-list">${buf.join("")}</ul>`;
      continue;
    }
    // blank
    if (!ln.trim()) { i++; continue; }
    // paragraph (gather consecutive non-empty, non-special lines)
    let buf = [ln]; i++;
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|>|\s*([-*]|\d+\.)\s|```|\s*---\s*$)/.test(lines[i]) && !/\|/.test(lines[i])) {
      buf.push(lines[i]); i++;
    }
    html += `<p class="md-p">${inline(buf.join(" "))}</p>`;
  }
  return html;
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

// ---- Page: members --------------------------------------------------------
async function pageMembers() {
  setCrumbs([{ label: "Team" }]);
  const members = await api("/api/members");
  view.innerHTML = `
    <div class="row">
      <div><h1>Solutions Team</h1><p class="sub">Pick a team member to see their accounts.</p></div>
      <button class="primary small" id="add-member-btn">+ Add Team Member</button>
    </div>
    <div id="add-member-form" class="add-member-form hidden">
      <input id="m-name" type="text" placeholder="Full name *" />
      <input id="m-role" type="text" placeholder="Title (e.g. Solutions Engineer)" />
      <input id="m-email" type="text" placeholder="Email" />
      <button class="primary small" id="m-save">Add</button>
      <button class="ghost small" id="m-cancel">Cancel</button>
    </div>
    <div class="grid">
      ${members.map((m) => `
        <a class="card" href="#/member/${encodeURIComponent(m.id)}">
          <h3>${esc(m.name)}</h3>
          <div class="meta">${esc(m.role || "")}${m.email ? " · " + esc(m.email) : ""}</div>
        </a>`).join("")}
    </div>`;

  const form = document.getElementById("add-member-form");
  document.getElementById("add-member-btn").onclick = () => {
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) document.getElementById("m-name").focus();
  };
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
  const typeCell = (s) => s?.type ? esc(s.type) : '<span class="muted">—</span>';
  const dateCell = (d) => d ? esc(d) : '<span class="muted">—</span>';
  const aeCell = (s) => s?.ae ? esc(s.ae) : '<span class="muted">—</span>';

  const acctRow = (a, isArchived) => `
    <div class="acct-row${isArchived ? " is-archived" : ""}" data-acct="${esc(a.name)}">
      <label class="acct-check"><input type="checkbox" class="row-check" data-acct="${esc(a.name)}" /></label>
      <div class="acct-row-menu">
        <button class="kebab" aria-label="Account actions">⋮</button>
        <div class="dropdown-menu hidden">
          ${isArchived
            ? `<button class="menu-item unarchive-btn" data-acct="${esc(a.name)}">Unarchive</button>`
            : `<button class="menu-item archive-btn" data-acct="${esc(a.name)}">Archive</button>`}
          <button class="menu-item danger delete-btn" data-acct="${esc(a.name)}">Delete…</button>
        </div>
      </div>
      <a href="#/account/${encodeURIComponent(a.name)}" class="acct-row-main">
        <span class="acct-name">${esc(a.name)}</span>
        <span class="acct-col col-stage">${stageCell(a._sfdc)}</span>
        <span class="acct-col col-amount">${fmtAmt(a._sfdc?.amount)}</span>
        <span class="acct-col col-close">${dateCell(a._sfdc?.close_date)}</span>
        <span class="acct-col col-type">${typeCell(a._sfdc)}</span>
        <span class="acct-col col-ae">${aeCell(a._sfdc)}</span>
        <span class="acct-col col-updated">${a.last_updated ? esc(a.last_updated) : '<span class="muted">—</span>'}</span>
        <span class="acct-col col-outputs">${a.output_count}</span>
        <span class="acct-col col-owner">${a.owner === memberId ? '<span class="badge owned">you</span>' : `<span class="badge">${esc(a.owner || "—")}</span>`}${isArchived ? ' <span class="badge">archived</span>' : ""}</span>
      </a>
      <button class="acct-expand" data-acct="${esc(a.name)}" aria-label="Show opportunities" title="Show opportunities">▸</button>
    </div>
    <div class="opp-drawer hidden" data-drawer="${esc(a.name)}"></div>`;

  const trashRow = (t) => `
    <div class="acct-row trash-row">
      <div class="acct-row-menu"><button class="ghost small restore-btn" data-tid="${esc(t.trash_id)}">Restore</button></div>
      <div class="acct-row-main no-link">
        <span class="acct-name">${esc(t.name)}</span>
        <span class="acct-col" style="grid-column: span 5;"><span class="muted">deleted</span> ${esc(t.deleted_at)}</span>
      </div>
    </div>`;

  const renderRow = tab === "trash" ? trashRow : (a) => acctRow(a, tab === "archived");
  const emptyMsg = { active: "No active accounts. Create one to get started.", archived: "No archived accounts.", trash: "Trash is empty." }[tab];

  const hCell = (key, label, cls) => `<span class="acct-col ${cls} sortable" data-sort="${key}">${label}${sortArrow(key)}</span>`;
  const listHeader = tab === "trash" ? "" : `
    <div class="acct-row acct-head">
      <label class="acct-check"><input type="checkbox" id="check-all" /></label>
      <div class="acct-row-menu"></div>
      <div class="acct-row-main no-link">
        <span class="acct-name sortable" data-sort="name">Account${sortArrow("name")}</span>
        ${hCell("stage", "SFDC Stage", "col-stage")}
        ${hCell("amount", "Amount", "col-amount")}
        ${hCell("close", "Close Date", "col-close")}
        ${hCell("type", "Type", "col-type")}
        ${hCell("ae", "Account Executive", "col-ae")}
        ${hCell("updated", "Updated", "col-updated")}
        ${hCell("outputs", "Outputs", "col-outputs")}
        ${hCell("owner", "Owner", "col-owner")}
      </div>
      <span class="acct-expand-spacer"></span>
    </div>`;

  view.innerHTML = `
    <div class="row">
      <div><h1>${esc(m.name)}</h1><p class="sub">Accounts</p></div>
      <div class="create-box">
        <input id="new-acct" type="text" placeholder="New account name…" />
        <button class="primary small" id="create-acct">+ Create Account</button>
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
        <select class="bulk-transfer-sel"><option value="">Transfer to…</option>${
          members.filter((x) => x.id !== memberId).map((x) => `<option value="${x.id}">${esc(x.name)}</option>`).join("")}</select>
        <button class="danger small bulk-delete">Delete…</button>
      </div>
    </div>
    <div class="acct-list" id="acct-grid">
      ${showing.length ? listHeader + showing.map(renderRow).join("") : `<div class="empty">${emptyMsg}</div>`}
    </div>`;

  view.querySelectorAll(".tab").forEach((t) => { t.onclick = () => pageMember(memberId, t.dataset.tab); });

  // Sortable headers
  view.querySelectorAll(".sortable").forEach((h) => h.onclick = () => {
    const key = h.dataset.sort;
    _sort = (_sort.key === key) ? { key, dir: -_sort.dir } : { key, dir: 1 };
    localStorage.setItem("se-hub-sort", JSON.stringify(_sort));
    pageMember(memberId, tab);
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
        const opps = await api(`/api/accounts/${encodeURIComponent(acct)}/opportunities`).catch(() => []);
        drawer.dataset.loaded = "1";
        drawer.innerHTML = opps.length ? `<div class="opp-list">${oppHeaderRow()}${opps.map((o) => oppRow(acct, o)).join("")}</div>`
          : `<div class="opp-drawer-loading muted">No opportunities found.</div>`;
      }
    };
  });
}

// Shared opportunity header row (used in the expand drawer and the account page)
function oppHeaderRow() {
  return `
    <div class="opp-row opp-row-head no-link">
      <span class="opp-row-name">Opportunity</span>
      <span class="opp-row-col opp-row-stage">SFDC Stage</span>
      <span class="opp-row-col opp-row-amount">Amount</span>
      <span class="opp-row-col opp-row-type">Type</span>
      <span class="opp-row-col opp-row-close">Close Date</span>
      <span class="opp-row-col opp-row-status">Status</span>
      <span class="opp-row-col opp-row-outputs">Outputs</span>
    </div>`;
}

// Shared opportunity row (used in the expand drawer and the account page)
function oppRow(account, o) {
  const fmtAmt = (n) => (n || n === 0) ? "$" + Number(n).toLocaleString() : '<span class="muted">—</span>';
  const stage = o.stage_num ? esc(o.stage_num) : (o.stage ? esc(o.stage) : '<span class="muted">—</span>');
  const statusBadge = o.is_closed === false ? '<span class="badge owned">open</span>'
    : (o.is_closed ? '<span class="badge badge-closed">closed</span>' : "");
  return `
    <a class="opp-row" href="#/opp/${encodeURIComponent(account)}/${encodeURIComponent(o.slug)}/${encodeURIComponent(o.name)}">
      <span class="opp-row-name">${esc(o.name)}</span>
      <span class="opp-row-col opp-row-stage">${stage}</span>
      <span class="opp-row-col opp-row-amount">${fmtAmt(o.amount)}</span>
      <span class="opp-row-col opp-row-type">${o.type ? esc(o.type) : '<span class="muted">—</span>'}</span>
      <span class="opp-row-col opp-row-close">${o.close_date ? esc(o.close_date) : '<span class="muted">—</span>'}</span>
      <span class="opp-row-col opp-row-status">${statusBadge}</span>
      <span class="opp-row-col opp-row-outputs">${o.output_count}</span>
    </a>`;
}

// ---- Page: account → list of opportunities -------------------------------
async function pageAccount(account) {
  setCrumbs([...(await accountCrumbs(account)), { label: account }]);
  view.innerHTML = `<div class="row"><div><h1>${esc(account)}</h1><p class="sub">Opportunities — pick one to view outputs &amp; run skills</p></div></div>
    <div class="empty" id="opps-loading">Loading opportunities from Salesforce…</div>`;
  const opps = await api(`/api/accounts/${encodeURIComponent(account)}/opportunities`).catch(() => []);

  view.innerHTML = `
    <div class="row"><div><h1>${esc(account)}</h1><p class="sub">Opportunities — pick one to view outputs &amp; run skills</p></div></div>
    <div class="opp-list">
      ${opps.length ? oppHeaderRow() + opps.map((o) => oppRow(account, o)).join("") : `<div class="empty">No opportunities found.</div>`}
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
        ${groups[day].map((o) => `
          <div class="out-item" data-path="${encodeURIComponent(o.path)}" data-title="${esc(prettySkill(o.skill))} — ${esc(o.filename)}">
            <div><div class="skill">${esc(prettySkill(o.skill))}</div><div class="when">${esc(o.filename)}</div></div>
            <div class="out-item-right">
              <span class="when">${esc((o.modified || "").slice(11))} UTC</span>
              ${downloadMenuHtml(encodeURIComponent(o.path), "dl-menu-row")}
            </div>
          </div>`).join("")}
      </div>
    </div>`).join("");
}

// ---- Page: opportunity (outputs + invoke) --------------------------------
async function pageOpportunity(account, slug, oppName) {
  setCrumbs([...(await accountCrumbs(account)), { label: account, href: `#/account/${encodeURIComponent(account)}` }, { label: oppName }]);
  const outputs = await api(`/api/accounts/${encodeURIComponent(account)}/outputs?opp=${encodeURIComponent(slug)}`);
  view.innerHTML = `
    <div class="row">
      <div><h1>${esc(oppName)}</h1><p class="sub">${esc(account)} · outputs &amp; skills</p></div>
      <div class="row-actions">
        <a class="primary live-btn" href="#/live/${encodeURIComponent(account)}/${encodeURIComponent(slug)}/${encodeURIComponent(oppName)}">🎙 Live Transcribe</a>
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
      ${outputs.length ? renderOutputGroups(outputs) : `<div class="empty">No outputs yet for this opportunity. Invoke a skill to generate one.</div>`}
    </div>`;
  document.querySelectorAll(".out-item").forEach((el) => {
    el.onclick = () => openOutput(el.dataset.path, el.dataset.title, { account, slug, oppName });
  });
  // onDeleted runs on user click (after refreshOutputs is defined below) → safe.
  wireDownloadMenus(view, () => refreshOutputs());
  document.getElementById("invoke-btn").onclick = () => openInvoke(account, { slug, name: oppName });

  // Free-text instruction bar — runs the agent without picking a named skill
  const freeInput = document.getElementById("opp-free");
  const freeBtn = document.getElementById("opp-free-run");
  const fStatus = document.getElementById("freebar-status");

  const dismissRun = () => { fStatus.className = "status hidden"; fStatus.innerHTML = ""; };

  // Re-fetch the Generated Outputs list (after a run produces a new file).
  const refreshOutputs = async () => {
    const outs = await api(`/api/accounts/${encodeURIComponent(account)}/outputs?opp=${encodeURIComponent(slug)}`).catch(() => []);
    const el = document.getElementById("outputs");
    if (!el) return;
    el.innerHTML = outs.length ? renderOutputGroups(outs) : `<div class="empty">No outputs yet for this opportunity. Invoke a skill to generate one.</div>`;
    el.querySelectorAll(".out-item").forEach((it) => {
      it.onclick = () => openOutput(it.dataset.path, it.dataset.title, { account, slug, oppName });
    });
    wireDownloadMenus(el, () => refreshOutputs());
  };

  // Status-only line — the result goes to Generated Outputs (no inline preview).
  const renderJob = (job, finishedAt) => {
    if (job.status === "running") {
      fStatus.className = "status running";
      fStatus.innerHTML = `<span class="run-head"><span class="spinner"></span>Running ${esc(job.skill || "instruction")} … (keeps running even if you leave this page)</span>`;
      freeBtn.disabled = true;
      return;
    }
    freeBtn.disabled = false;
    const ok = job.ok;
    fStatus.className = ok ? "status ok" : "status err";
    const ago = finishedAt ? ` · ran ${relTime(finishedAt)}` : "";
    const head = ok
      ? `✓ ${esc(job.skill || "run")}${ago} — saved to Generated Outputs below`
      : `✕ ${esc(job.skill || "run")}${ago} — finished with an error`;
    fStatus.innerHTML = `<span class="run-head">${head}</span><button class="run-dismiss" id="run-dismiss" title="Dismiss">✕</button>`;
    document.getElementById("run-dismiss").onclick = dismissRun;
  };

  let _lastStatus = null;
  const watch = (jobId) => pollJob(jobId, (job) => {
    if (!document.getElementById("freebar-status")) return;  // left the page
    renderJob(job);
    if (job.status !== "running" && _lastStatus === "running") refreshOutputs();  // new file landed
    _lastStatus = job.status;
  });

  // Recover a run started earlier (still-running job re-attaches). We no longer
  // surface the last finished run inline — finished results live in Generated
  // Outputs (and chat-only skills like next-move show their text when opened).
  const existing = await api(`/api/jobs?account=${encodeURIComponent(account)}&opp_slug=${encodeURIComponent(slug)}`).catch(() => []);
  const running = existing.find((j) => j.status === "running");
  if (running) { _lastStatus = "running"; renderJob(running); watch(running.job_id); }

  // Start a run. `skill` = run that named skill (typed text becomes context);
  // otherwise run the typed text as a freeform instruction.
  const startRun = async (skill) => {
    const free = freeInput.value.trim();
    if (!skill && !free) return;
    suggestBox.classList.add("hidden");
    fStatus.className = "status running";
    fStatus.innerHTML = `<span class="run-head"><span class="spinner"></span>Starting…</span>`;
    freeBtn.disabled = true;
    const payload = skill
      ? { account, opportunity: oppName, opp_slug: slug, skill, extra: free || null }
      : { account, opportunity: oppName, opp_slug: slug, freeform: free };
    try {
      const res = await api("/api/invoke", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      await watch(res.job_id);
    } catch (e) {
      fStatus.className = "status err"; fStatus.textContent = "Error: " + e.message;
      freeBtn.disabled = false;
    }
  };

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

// `ctx` = { account, slug, oppName } so Back returns to the exact opportunity.
async function openOutput(path, title, ctx) {
  const text = await api("/api/output?path=" + encodeURIComponent(decodeURIComponent(path)));
  const toc = [];
  const bodyHtml = mdToHtml(text, toc);

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
  // Tag the section containing "At a Glance" so it gets the summary panel.
  for (const s of sections) {
    if (/at a glance/i.test(s.querySelector("h2,h3")?.textContent || "")) s.classList.add("is-glance");
  }
  const sheetHtml = sections.map((s) => s.outerHTML).join("");

  // Sidebar index — H2/H3 only, concise labels.
  const tocHtml = toc
    .filter((t) => t.level === 2 || t.level === 3)
    .map((t) => `<a href="#toc-${t.id}" class="doc-toc-link lvl${t.level}">${esc(conciseLabel(t.text))}</a>`)
    .join("");

  const backHref = ctx
    ? `#/opp/${encodeURIComponent(ctx.account)}/${encodeURIComponent(ctx.slug)}/${encodeURIComponent(ctx.oppName)}`
    : null;
  view.innerHTML = `
    <div class="row"><h1>${esc(docTitle)}</h1>
      <div class="row-actions">
        ${downloadMenuHtml(encodeURIComponent(decodeURIComponent(path)), "dl-menu-doc")}
        <button class="ghost" id="doc-back">← Back</button>
      </div></div>
    <div class="doc-layout">
      ${tocHtml ? `<aside class="doc-toc"><div class="doc-toc-head">On this page</div>${tocHtml}</aside>` : ""}
      <article class="md-body">
        <div class="doc-sheet">${sheetHtml}</div>
        <div class="doc-qa" id="doc-qa"></div>
        <div class="doc-askbar">
          <input id="doc-ask" type="text" autocomplete="off" placeholder="Ask a follow-up about this document…" />
          <button class="primary small" id="doc-ask-send">Ask</button>
        </div>
      </article>
    </div>`;
  if (ctx) setCrumbs([...(await accountCrumbs(ctx.account)),
                      { label: ctx.account, href: `#/account/${encodeURIComponent(ctx.account)}` },
                      { label: ctx.oppName, href: backHref },
                      { label: "output" }]);
  else setCrumbs([{ label: "Team", href: "#/" }, { label: "output" }]);
  // Deterministic Back — go to the opp page in one step. Set the hash AND
  // re-render directly: the output was opened without changing the hash, so it
  // may already equal backHref (in which case setting it fires no hashchange).
  const goBack = () => {
    const target = backHref || "#/";
    if (location.hash === target) route();   // same hash → no event, render manually
    else location.hash = target;             // different → hashchange fires route()
  };
  document.getElementById("doc-back").onclick = goBack;
  // After deleting the open output, leave the now-gone doc → back to the opp.
  wireDownloadMenus(view, goBack);

  // Sidebar links scroll-jump locally (no router involvement).
  view.querySelectorAll(".doc-toc-link").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      const el = document.getElementById(a.getAttribute("href").slice(5)); // strip "#toc-"
      if (el) { el.scrollIntoView({ behavior: "smooth", block: "start" }); flashEl(el); }
    };
  });

  // Follow-up chat against this document (quick → Claude API; deep → claude -p).
  const askInput = document.getElementById("doc-ask");
  const askSend = document.getElementById("doc-ask-send");
  const thread = document.getElementById("doc-qa");
  const docAsk = () => {
    const q = askInput.value.trim();
    if (!q) return;
    askInput.value = "";
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
  askInput.onkeydown = (e) => { if (e.key === "Enter") docAsk(); };
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
      await pollJob(data.job_id, (job) => {
        if (job.status !== "running") { bodyEl.innerHTML = mdToHtml(job.stdout || job.stderr || "(no output)"); }
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
    buf += dec.decode(value, { stream: true });
    const events = buf.split("\n\n"); buf = events.pop();
    for (const ev of events) {
      const m = ev.match(/^event: (\w+)\ndata: (.*)$/ms);
      if (!m) continue;
      if (m[1] === "token") { acc += JSON.parse(m[2]).text; bodyEl.innerHTML = mdToHtml(acc); }
      else if (m[1] === "error") { bodyEl.innerHTML = `<span class="muted">Error: ${esc(JSON.parse(m[2]).error)}</span>`; }
    }
  }
}

// Run a skill from the output chat bar. Mirrors the opp-page invoke flow:
// POST /api/invoke (scoped to this opp), poll the job, and report that the
// result lands in the opp's Generated Outputs. `ctx` = { account, slug, oppName }.
async function invokeFromChat(threadEl, q, skill, ctx) {
  const item = document.createElement("div");
  item.className = "qa-item";
  item.innerHTML = `<div class="qa-q">${esc(q)}</div>`
    + `<div class="qa-a"><span class="qa-tag">⚙️</span><span class="qa-body">`
    + `<span class="muted">Running <strong>${esc(prettySkill(skill.id))}</strong> for ${esc(ctx.oppName)}… `
    + `(keeps running even if you leave this page; result lands in Generated Outputs)</span></span></div>`;
  threadEl.appendChild(item);
  item.scrollIntoView({ behavior: "smooth", block: "nearest" });
  const bodyEl = item.querySelector(".qa-body"), tag = item.querySelector(".qa-tag");

  try {
    const res = await api("/api/invoke", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ account: ctx.account, opportunity: ctx.oppName, opp_slug: ctx.slug, skill: skill.id }),
    });
    await pollJob(res.job_id, (job) => {
      if (job.status === "running") return;
      if (job.ok) {
        tag.textContent = "✓";
        const oppHref = `#/opp/${encodeURIComponent(ctx.account)}/${encodeURIComponent(ctx.slug)}/${encodeURIComponent(ctx.oppName)}`;
        bodyEl.innerHTML = `<strong>${esc(prettySkill(skill.id))}</strong> finished — saved to `
          + `<a href="${oppHref}">Generated Outputs</a> for ${esc(ctx.oppName)}.`;
      } else {
        tag.textContent = "✕";
        bodyEl.innerHTML = `<span class="muted">${esc(prettySkill(skill.id))} finished with an error. `
          + `Check the opportunity page, or try the Invoke Skill button there.</span>`;
      }
    });
  } catch (e) {
    tag.textContent = "⚠️";
    bodyEl.innerHTML = `<span class="muted">Couldn't start ${esc(prettySkill(skill.id))}: ${esc(e.message)}</span>`;
  }
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
      <label>Your mic (You)
        <select id="dev-mic">${opts()}</select>
      </label>
      <label>Call audio — everyone else (optional)
        <select id="dev-call"><option value="">— none (single stream, no labels) —</option>${opts()}</select>
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
  let hasSegs = false;
  const addSegment = (seg) => {
    if (!hasSegs) { segsEl.innerHTML = ""; hasSegs = true; }
    const who = seg.speaker ? `<span class="seg-who seg-${seg.speaker === "You" ? "you" : "call"}">${esc(seg.speaker)}</span>` : "";
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
  const enterRecording = (sessionId, startedAtMs) => {
    // tear down any prior client-side handles (avoid leaking a 2nd SSE/timer)
    if (_liveState) { try { _liveState.sse?.close(); } catch {} if (_liveState.timer) clearInterval(_liveState.timer); }
    _liveState = { sessionId };
    segsEl.innerHTML = ""; hasSegs = false;
    const sse = new EventSource(`/api/transcribe/${sessionId}/stream`);
    sse.addEventListener("segment", (e) => addSegment(JSON.parse(e.data)));
    _liveState.sse = sse;
    $("dev-mic").disabled = $("dev-call").disabled = true;
    startBtn.classList.add("hidden");
    stopBtn.classList.remove("hidden"); stopBtn.disabled = false;
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

  // ── Start / Stop ────────────────────────────────────────────────────
  startBtn.onclick = async () => {
    const mic = $("dev-mic").value;
    const call = $("dev-call").value || null;
    startBtn.disabled = true;
    try {
      const res = await api("/api/transcribe/start", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ account, opp_slug: slug, opportunity: oppName,
                               mic_device: Number(mic), call_device: call === null ? null : Number(call) }),
      });
      enterRecording(res.session_id, Date.now());
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
        await pollJob(data.job_id, (job) => {
          if (job.status !== "running") {
            bodyEl.innerHTML = mdToHtml(job.stdout || job.stderr || "(no output)");
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
        buf += dec.decode(value, { stream: true });
        const events = buf.split("\n\n"); buf = events.pop();
        for (const ev of events) {
          const m = ev.match(/^event: (\w+)\ndata: (.*)$/ms);
          if (!m) continue;
          if (m[1] === "token") { acc += JSON.parse(m[2]).text; bodyEl.innerHTML = mdToHtml(acc); threadEl.scrollTop = threadEl.scrollHeight; }
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

  // ── Reconnect if a session is still recording for this opp ───────────
  // Leaving and returning re-renders this page, but the session lives on the
  // server — pick it back up so the transcript isn't lost.
  const active = await api(`/api/transcribe/active?account=${encodeURIComponent(account)}&opp_slug=${encodeURIComponent(slug)}`).catch(() => null);
  if (active && active.session_id) {
    // restore the device picker selections if we can infer them (best-effort)
    enterRecording(active.session_id, (active.started_at || 0) * 1000);
  }
}

// ---- Invoke modal ---------------------------------------------------------
const modal = document.getElementById("modal");
// opp = { slug, name } | null
function openInvoke(account, opp = null) {
  const ctx = opp ? `${account} · ${opp.name}` : account;
  document.getElementById("modal-title").textContent = `Invoke — ${ctx}`;
  const sel = document.getElementById("skill-select");
  sel.innerHTML = SKILLS.map((s) => `<option value="${s.id}">${esc(s.label)}</option>`).join("");
  const blurb = document.getElementById("skill-blurb");
  const setBlurb = () => {
    const h = SKILLS_HELP[sel.value] || {};
    const base = SKILLS.find((s) => s.id === sel.value) || {};
    const trig = (h.triggers || []).slice(0, 4).map((t) => `<code class="trig">${esc(t)}</code>`).join(" ");
    blurb.innerHTML = `
      <div class="hint-what">${esc(h.description || base.blurb || "")}</div>
      ${h.prerequisites ? `<div class="hint-line"><b>Needs:</b> ${esc(h.prerequisites.split("\n")[0]).slice(0, 160)}</div>` : ""}
      ${h.output_location ? `<div class="hint-line"><b>Saves to:</b> <span class="muted">${esc(h.output_location)}</span></div>` : ""}
      ${trig ? `<div class="hint-line"><b>Triggers:</b> ${trig}</div>` : ""}`;
  };
  sel.onchange = setBlurb; setBlurb();
  document.getElementById("skill-extra").value = "";

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
      await api("/api/invoke", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
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
