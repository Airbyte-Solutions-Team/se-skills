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
  const stageCell = (s) => s?.stage_num ? esc(s.stage_num) : (s?.stage ? esc(s.stage) : '<span class="muted">—</span>');
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
        drawer.innerHTML = opps.length ? `<div class="opp-list">${opps.map((o) => oppRow(acct, o)).join("")}</div>`
          : `<div class="opp-drawer-loading muted">No opportunities found.</div>`;
      }
    };
  });
}

// Shared opportunity row (used in the expand drawer and the account page)
function oppRow(account, o) {
  const fmtAmt = (n) => (n || n === 0) ? "$" + Number(n).toLocaleString() : '<span class="muted">—</span>';
  const stage = o.stage_num ? esc(o.stage_num) : (o.stage ? esc(o.stage) : '<span class="muted">—</span>');
  const statusBadge = o.is_closed === false ? '<span class="badge owned">open</span>'
    : (o.is_closed ? '<span class="badge">closed</span>' : "");
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
  setCrumbs([{ label: "Team", href: "#/" }, { label: account }]);
  view.innerHTML = `<div class="row"><div><h1>${esc(account)}</h1><p class="sub">Opportunities — pick one to view outputs &amp; run skills</p></div></div>
    <div class="empty" id="opps-loading">Loading opportunities from Salesforce…</div>`;
  const opps = await api(`/api/accounts/${encodeURIComponent(account)}/opportunities`).catch(() => []);

  const oppHeader = `
    <div class="opp-row opp-row-head no-link">
      <span class="opp-row-name">Opportunity</span>
      <span class="opp-row-col opp-row-stage">SFDC Stage</span>
      <span class="opp-row-col opp-row-amount">Amount</span>
      <span class="opp-row-col opp-row-type">Type</span>
      <span class="opp-row-col opp-row-close">Close Date</span>
      <span class="opp-row-col opp-row-status">Status</span>
      <span class="opp-row-col opp-row-outputs">Outputs</span>
    </div>`;

  view.innerHTML = `
    <div class="row"><div><h1>${esc(account)}</h1><p class="sub">Opportunities — pick one to view outputs &amp; run skills</p></div></div>
    <div class="opp-list">
      ${opps.length ? oppHeader + opps.map((o) => oppRow(account, o)).join("") : `<div class="empty">No opportunities found.</div>`}
    </div>`;
}

// ---- Page: opportunity (outputs + invoke) --------------------------------
async function pageOpportunity(account, slug, oppName) {
  setCrumbs([{ label: "Team", href: "#/" }, { label: account, href: `#/account/${encodeURIComponent(account)}` }, { label: oppName }]);
  const outputs = await api(`/api/accounts/${encodeURIComponent(account)}/outputs?opp=${encodeURIComponent(slug)}`);
  view.innerHTML = `
    <div class="row">
      <div><h1>${esc(oppName)}</h1><p class="sub">${esc(account)} · outputs &amp; skills</p></div>
      <button class="primary" id="invoke-btn">⚡ Invoke Skill</button>
    </div>
    <h2>Generated outputs</h2>
    <div class="outputs" id="outputs">
      ${outputs.length ? outputs.map((o) => `
        <div class="out-item" data-path="${encodeURIComponent(o.path)}" data-title="${esc(o.skill)} — ${esc(o.filename)}">
          <div><div class="skill">${esc(o.skill)}</div><div class="when">${esc(o.filename)}</div></div>
          <div class="when">${esc(o.modified)} UTC</div>
        </div>`).join("") : `<div class="empty">No outputs yet for this opportunity. Invoke a skill to generate one.</div>`}
    </div>`;
  document.querySelectorAll(".out-item").forEach((el) => {
    el.onclick = () => openOutput(el.dataset.path, el.dataset.title);
  });
  document.getElementById("invoke-btn").onclick = () => openInvoke(account, { slug, name: oppName });
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
  document.getElementById("free-text").value = "";

  // Mode toggle: skill picker vs. free-text instruction
  let mode = "skill";
  const skillMode = document.getElementById("skill-mode");
  const freeModeEl = document.getElementById("free-mode");
  const btnSkill = document.getElementById("mode-skill");
  const btnFree = document.getElementById("mode-free");
  const setMode = (mDist) => {
    mode = mDist;
    btnSkill.classList.toggle("active", mode === "skill");
    btnFree.classList.toggle("active", mode === "free");
    skillMode.classList.toggle("hidden", mode !== "skill");
    freeModeEl.classList.toggle("hidden", mode !== "free");
  };
  btnSkill.onclick = () => setMode("skill");
  btnFree.onclick = () => setMode("free");
  setMode("skill");

  const status = document.getElementById("invoke-status");
  const output = document.getElementById("invoke-output");
  status.className = "status hidden"; output.className = "output hidden"; output.textContent = "";
  modal.classList.remove("hidden");

  document.getElementById("invoke-cancel").onclick = () => modal.classList.add("hidden");
  document.getElementById("invoke-run").onclick = async () => {
    const payload = { account, opportunity: opp?.name || null, opp_slug: opp?.slug || null };
    let label;
    if (mode === "free") {
      const free = document.getElementById("free-text").value.trim();
      if (!free) { alert("Enter an instruction, or switch to 'Pick a skill'."); return; }
      payload.freeform = free; label = "custom instruction";
    } else {
      payload.skill = sel.value;
      payload.extra = document.getElementById("skill-extra").value.trim() || null;
      label = sel.value;
    }
    status.className = "status running";
    status.innerHTML = `<span class="spinner"></span>Running <b>${esc(label)}</b> on ${esc(ctx)} … (this can take a minute)`;
    document.getElementById("invoke-run").disabled = true;
    try {
      const res = await api("/api/invoke", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      });
      status.className = res.ok ? "status ok" : "status err";
      status.textContent = res.ok ? "Done. Output saved — close this to see it in the opportunity's outputs." : "Skill returned a non-zero exit. See output below.";
      output.className = "output md-body";
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
    const parts = h.split("/");          // ["", kind, arg, ...]
    const kind = parts[1];
    if (kind === "member") return pageMember(decodeURIComponent(parts[2]));
    if (kind === "account") return pageAccount(decodeURIComponent(parts[2]));
    if (kind === "opp") return pageOpportunity(
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
