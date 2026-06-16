// SE Skills — local hub frontend. Tiny hash-router, no build step.
const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.headers.get("content-type")?.includes("application/json") ? r.json() : r.text();
};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// Minimal, dependency-free Markdown → HTML for rendering skill outputs nicely.
// Handles: headings, bold/italic, inline code, code fences, tables, blockquotes,
// hr, ordered/unordered lists (incl. [ ] checkboxes), paragraphs.
function mdToHtml(md) {
  const lines = (md || "").replace(/\r\n/g, "\n").split("\n");
  let html = "", i = 0;
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
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
    // heading
    const h = ln.match(/^(#{1,6})\s+(.*)$/);
    if (h) { const lvl = h[1].length; html += `<h${lvl} class="md-h md-h${lvl}">${inline(h[2])}</h${lvl}>`; i++; continue; }
    // hr
    if (/^\s*---\s*$/.test(ln)) { html += "<hr class='md-hr'/>"; i++; continue; }
    // blockquote
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
        item = item.replace(/^\[ \]\s*/, "☐ ").replace(/^\[[xX]\]\s*/, "☑ ");
        buf.push(`<li>${inline(item)}</li>`); i++;
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

function setCrumbs(parts) {
  crumbs.innerHTML = parts
    .map((p, i) => (p.href ? `<a href="${p.href}">${esc(p.label)}</a>` : `<span>${esc(p.label)}</span>`))
    .join('<span class="sep">/</span>');
}

// ---- Page: members --------------------------------------------------------
async function pageMembers() {
  setCrumbs([{ label: "Team" }]);
  const members = await api("/api/members");
  view.innerHTML = `
    <h1>Solutions Team</h1>
    <p class="sub">Pick a team member to see their accounts.</p>
    <div class="grid">
      ${members.map((m) => `
        <a class="card" href="#/member/${encodeURIComponent(m.id)}">
          <h3>${esc(m.name)}</h3>
          <div class="meta">${esc(m.role || "")}${m.email ? " · " + esc(m.email) : ""}</div>
        </a>`).join("")}
    </div>`;
}

// ---- Page: member's accounts ---------------------------------------------
// `tab` is "active" (default), "archived", or "trash"
async function pageMember(memberId, tab = "active") {
  const members = await api("/api/members");
  const m = members.find((x) => x.id === memberId) || { id: memberId, name: memberId };
  setCrumbs([{ label: "Team", href: "#/" }, { label: m.name }]);
  const data = await api(`/api/members/${encodeURIComponent(memberId)}/accounts`);
  const active = data.active || [];
  const archived = data.archived || [];
  const trash = await api("/api/trash").catch(() => []);

  const ownerLabel = (a) =>
    a.owner === memberId ? '<span class="badge owned">yours</span>'
      : (a.owner ? `<span class="badge">${esc(a.owner)}</span>` : '<span class="badge unowned" title="No owner recorded — created before ownership tracking. Use ⋯ → Claim to take ownership.">unowned</span>');

  // Account row (list layout): ⋮ menu on the LEFT, then name + aligned columns.
  const acctRow = (a, isArchived) => `
    <div class="acct-row${isArchived ? " is-archived" : ""}" data-acct="${esc(a.name)}">
      <div class="acct-row-menu">
        <button class="kebab" aria-label="Account actions">⋮</button>
        <div class="dropdown-menu hidden">
          ${isArchived
            ? `<button class="menu-item unarchive-btn" data-acct="${esc(a.name)}">Unarchive</button>`
            : `<button class="menu-item claim-btn" data-acct="${esc(a.name)}" data-owner="${a.owner === memberId}">${a.owner === memberId ? "✓ Owned by you" : "Claim ownership"}</button>
               <button class="menu-item archive-btn" data-acct="${esc(a.name)}">Archive</button>`}
          <button class="menu-item danger delete-btn" data-acct="${esc(a.name)}">Delete…</button>
        </div>
      </div>
      <a href="#/account/${encodeURIComponent(a.name)}" class="acct-row-main">
        <span class="acct-name">${esc(a.name)}</span>
        <span class="acct-col col-sfdc" data-sfdc="${esc(a.name)}"><span class="sfdc-val muted">…</span></span>
        <span class="acct-col col-updated">${a.last_updated ? esc(a.last_updated) : '<span class="muted">—</span>'}</span>
        <span class="acct-col col-outputs">${a.output_count}</span>
        <span class="acct-col col-owner">${ownerLabel(a)}${isArchived ? ' <span class="badge">archived</span>' : ""}</span>
      </a>
    </div>`;

  const trashRow = (t) => `
    <div class="acct-row trash-row">
      <div class="acct-row-menu"><button class="ghost small restore-btn" data-tid="${esc(t.trash_id)}">Restore</button></div>
      <div class="acct-row-main no-link">
        <span class="acct-name">${esc(t.name)}</span>
        <span class="acct-col col-sfdc"></span>
        <span class="acct-col col-updated"><span class="muted">deleted</span> ${esc(t.deleted_at)}</span>
        <span class="acct-col col-outputs"></span>
        <span class="acct-col col-owner"></span>
      </div>
    </div>`;

  const showing = tab === "trash" ? trash : (tab === "archived" ? archived : active);
  const renderRow = tab === "trash" ? trashRow : (a) => acctRow(a, tab === "archived");
  const emptyMsg = { active: "No active accounts. Create one to get started.", archived: "No archived accounts.", trash: "Trash is empty." }[tab];

  // Column header (only for active/archived list) — spacer cell matches the menu width
  const listHeader = tab === "trash" ? "" : `
    <div class="acct-row acct-head">
      <div class="acct-row-menu"></div>
      <div class="acct-row-main no-link">
        <span class="acct-name">Account</span>
        <span class="acct-col col-sfdc">SFDC stage / amount</span>
        <span class="acct-col col-updated">Updated</span>
        <span class="acct-col col-outputs">Outputs</span>
        <span class="acct-col col-owner">Owner</span>
      </div>
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
    <div class="acct-list" id="acct-grid">
      ${showing.length ? listHeader + showing.map(renderRow).join("") : `<div class="empty">${emptyMsg}</div>`}
    </div>`;

  view.querySelectorAll(".tab").forEach((t) => { t.onclick = () => pageMember(memberId, t.dataset.tab); });

  document.getElementById("create-acct").onclick = async () => {
    const name = document.getElementById("new-acct").value.trim();
    if (!name) return;
    try {
      await api("/api/accounts", { method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ name, owner: memberId }) });
      pageMember(memberId, "active");
    } catch (e) { alert("Could not create account: " + e.message); }
  };

  // ⋮ kebab menus — one open at a time
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
  view.querySelectorAll(".claim-btn").forEach((b) => b.onclick = async (e) => {
    stop(e);
    if (b.dataset.owner === "true") return; // already yours
    await api(`/api/accounts/${encodeURIComponent(b.dataset.acct)}/owner`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ owner: memberId }) });
    pageMember(memberId, "active");
  });
  view.querySelectorAll(".delete-btn").forEach((b) => b.onclick = (e) => {
    stop(e);
    const acct = b.dataset.acct;
    // two-step confirm: turn the menu item into a confirm
    if (b.dataset.armed !== "1") {
      b.dataset.armed = "1"; b.textContent = `Confirm delete “${acct}”`; b.classList.add("armed");
      setTimeout(() => { if (b.dataset.armed === "1") { b.dataset.armed = "0"; b.textContent = "Delete…"; b.classList.remove("armed"); } }, 4000);
      return;
    }
    api(`/api/accounts/${encodeURIComponent(acct)}`, { method: "DELETE" })
      .then(() => pageMember(memberId, "active"))
      .catch((err) => alert("Delete failed: " + err.message));
  });
  view.querySelectorAll(".restore-btn").forEach((b) => b.onclick = async (e) => {
    stop(e);
    try { await api(`/api/trash/${encodeURIComponent(b.dataset.tid)}/restore`, { method: "POST" }); pageMember(memberId, "trash"); }
    catch (err) { alert("Restore failed: " + err.message); }
  });

  // Async SFDC stage/amount enrichment — fill the "…" placeholders without blocking render
  if (tab !== "trash" && showing.length) {
    api("/api/sfdc/stage-amount", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ accounts: showing.map((a) => a.name) }) })
      .then((map) => {
        view.querySelectorAll(".col-sfdc[data-sfdc]").forEach((el) => {
          const info = map[el.dataset.sfdc];
          const val = el.querySelector(".sfdc-val");
          if (info && info.stage) {
            const amt = info.amount ? ` · $${Number(info.amount).toLocaleString()}` : "";
            val.textContent = info.stage + amt;
            val.classList.remove("muted");
          } else { val.textContent = "—"; }
        });
      })
      .catch(() => view.querySelectorAll(".sfdc-val").forEach((v) => (v.textContent = "—")));
  }
}

// ---- Page: account (outputs + invoke) ------------------------------------
async function pageAccount(account) {
  setCrumbs([{ label: "Team", href: "#/" }, { label: account }]);
  const outputs = await api(`/api/accounts/${encodeURIComponent(account)}/outputs`);
  view.innerHTML = `
    <div class="row">
      <div><h1>${esc(account)}</h1><p class="sub">Outputs &amp; skills</p></div>
      <button class="primary" id="invoke-btn">⚡ Invoke Skill</button>
    </div>
    <h2>Generated outputs</h2>
    <div class="outputs" id="outputs">
      ${outputs.length ? outputs.map((o) => `
        <div class="out-item" data-path="${encodeURIComponent(o.path)}" data-title="${esc(o.skill)} — ${esc(o.filename)}">
          <div><div class="skill">${esc(o.skill)}</div><div class="when">${esc(o.filename)}</div></div>
          <div class="when">${esc(o.modified)} UTC</div>
        </div>`).join("") : `<div class="empty">No outputs yet. Invoke a skill to generate one.</div>`}
    </div>`;

  document.querySelectorAll(".out-item").forEach((el) => {
    el.onclick = () => openOutput(el.dataset.path, el.dataset.title);
  });
  document.getElementById("invoke-btn").onclick = () => openInvoke(account);
}

async function openOutput(path, title) {
  const text = await api("/api/output?path=" + encodeURIComponent(decodeURIComponent(path)));
  view.innerHTML = `
    <div class="row"><h1>${esc(title)}</h1><button class="ghost" onclick="history.back()">← Back</button></div>
    <div class="doc md-body">${mdToHtml(text)}</div>`;
  setCrumbs([{ label: "Team", href: "#/" }, { label: "output" }]);
}

// ---- Invoke modal ---------------------------------------------------------
const modal = document.getElementById("modal");
function openInvoke(account) {
  document.getElementById("modal-title").textContent = `Invoke skill — ${account}`;
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
  document.getElementById("invoke-run").onclick = async () => {
    const skill = sel.value;
    const extra = document.getElementById("skill-extra").value.trim();
    status.className = "status running";
    status.innerHTML = `<span class="spinner"></span>Running <b>${esc(skill)}</b> on ${esc(account)} … (this can take a minute)`;
    document.getElementById("invoke-run").disabled = true;
    try {
      const res = await api("/api/invoke", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ skill, account, extra: extra || null }),
      });
      status.className = res.ok ? "status ok" : "status err";
      status.textContent = res.ok ? "Done. Output saved — open the account to read the formatted report." : "Skill returned a non-zero exit. See output below.";
      output.className = "output md-body";
      // Render the skill's markdown output formatted, not raw.
      output.innerHTML = mdToHtml(res.stdout || "") + (res.stderr ? `<hr/><pre class="md-pre"><code>[stderr]\n${esc(res.stderr)}</code></pre>` : "");
    } catch (e) {
      status.className = "status err"; status.textContent = "Error: " + e.message;
    } finally {
      document.getElementById("invoke-run").disabled = false;
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
    if (h === "/" || h === "") return pageMembers();
    if (h === "/help") return pageHelp();
    const [, kind, arg] = h.split("/");
    if (kind === "member") return pageMember(decodeURIComponent(arg));
    if (kind === "account") return pageAccount(decodeURIComponent(arg));
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
