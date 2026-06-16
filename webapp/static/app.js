// SE Skills — local hub frontend. Tiny hash-router, no build step.
const view = document.getElementById("view");
const crumbs = document.getElementById("crumbs");

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.headers.get("content-type")?.includes("application/json") ? r.json() : r.text();
};
const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let SKILLS = [];

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
// `tab` is "active" (default) or "archived"
async function pageMember(memberId, tab = "active") {
  const members = await api("/api/members");
  const m = members.find((x) => x.id === memberId) || { id: memberId, name: memberId };
  setCrumbs([{ label: "Team", href: "#/" }, { label: m.name }]);
  const data = await api(`/api/members/${encodeURIComponent(memberId)}/accounts`);
  const active = data.active || [];
  const archived = data.archived || [];
  const showing = tab === "archived" ? archived : active;

  const ownerBadge = (a) =>
    a.owner === memberId ? '<span class="badge owned">yours</span>'
      : (a.owner ? "" : '<span class="badge">unowned</span>');

  const card = (a) => tab === "archived"
    ? `<div class="card archived-card">
         <a href="#/account/${encodeURIComponent(a.name)}" class="card-link"><h3>${esc(a.name)}</h3>
           <div class="meta">${ownerBadge(a)} <span class="badge">archived</span></div></a>
         <div class="card-foot"><button class="ghost small unarchive-btn" data-acct="${esc(a.name)}">Unarchive</button></div>
       </div>`
    : `<div class="card">
         <a href="#/account/${encodeURIComponent(a.name)}" class="card-link"><h3>${esc(a.name)}</h3>
           <div class="meta">${ownerBadge(a)}</div></a>
         <div class="card-foot">
           <div class="dropdown">
             <button class="ghost small archive-toggle" data-acct="${esc(a.name)}" aria-haspopup="true">Archive ▾</button>
             <div class="dropdown-menu hidden">
               <button class="danger small archive-confirm" data-acct="${esc(a.name)}">Archive “${esc(a.name)}”</button>
             </div>
           </div>
         </div>
       </div>`;

  view.innerHTML = `
    <div class="row">
      <div><h1>${esc(m.name)}</h1><p class="sub">Accounts</p></div>
      <div>
        <input id="new-acct" type="text" placeholder="New account name…" />
        <button class="primary small" id="create-acct">+ Create Account</button>
      </div>
    </div>
    <div class="tabs">
      <button class="tab ${tab === "active" ? "active" : ""}" data-tab="active">Active (${active.length})</button>
      <button class="tab ${tab === "archived" ? "active" : ""}" data-tab="archived">Archived (${archived.length})</button>
    </div>
    <div class="grid" id="acct-grid">
      ${showing.length ? showing.map(card).join("")
        : `<div class="empty">${tab === "archived" ? "No archived accounts." : "No active accounts. Create one to get started."}</div>`}
    </div>`;

  // Tab switching
  view.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => pageMember(memberId, t.dataset.tab);
  });

  // Create
  document.getElementById("create-acct").onclick = async () => {
    const name = document.getElementById("new-acct").value.trim();
    if (!name) return;
    try {
      await api("/api/accounts", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ name, owner: memberId }),
      });
      pageMember(memberId, "active");
    } catch (e) { alert("Could not create account: " + e.message); }
  };

  // Archive: two-step. First click opens the dropdown; the confirm inside does it.
  const closeAllMenus = () => view.querySelectorAll(".dropdown-menu").forEach((mn) => mn.classList.add("hidden"));
  view.querySelectorAll(".archive-toggle").forEach((b) => {
    b.onclick = (e) => {
      e.preventDefault(); e.stopPropagation();
      const menu = b.nextElementSibling;
      const wasOpen = !menu.classList.contains("hidden");
      closeAllMenus();           // only one open at a time
      if (!wasOpen) menu.classList.remove("hidden");
    };
  });
  view.querySelectorAll(".archive-confirm").forEach((b) => {
    b.onclick = async (e) => {
      e.preventDefault(); e.stopPropagation();
      await api(`/api/accounts/${encodeURIComponent(b.dataset.acct)}/archive`, { method: "POST" });
      pageMember(memberId, "active");
    };
  });
  // Click anywhere else closes any open dropdown
  document.addEventListener("click", closeAllMenus, { once: true });

  // Unarchive (single action — restoring is non-destructive, no confirm needed)
  view.querySelectorAll(".unarchive-btn").forEach((b) => {
    b.onclick = async (e) => {
      e.preventDefault(); e.stopPropagation();
      await api(`/api/accounts/${encodeURIComponent(b.dataset.acct)}/unarchive`, { method: "POST" });
      pageMember(memberId, "archived");
    };
  });
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
    <div class="doc">${esc(text)}</div>`;
  setCrumbs([{ label: "Team", href: "#/" }, { label: "output" }]);
}

// ---- Invoke modal ---------------------------------------------------------
const modal = document.getElementById("modal");
function openInvoke(account) {
  document.getElementById("modal-title").textContent = `Invoke skill — ${account}`;
  const sel = document.getElementById("skill-select");
  sel.innerHTML = SKILLS.map((s) => `<option value="${s.id}">${esc(s.label)}</option>`).join("");
  const blurb = document.getElementById("skill-blurb");
  const setBlurb = () => { blurb.textContent = (SKILLS.find((s) => s.id === sel.value) || {}).blurb || ""; };
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
      status.textContent = res.ok ? "Done. Output saved — refresh the account to see it." : "Skill returned a non-zero exit. See output below.";
      output.className = "output";
      output.textContent = (res.stdout || "") + (res.stderr ? "\n\n[stderr]\n" + res.stderr : "");
    } catch (e) {
      status.className = "status err"; status.textContent = "Error: " + e.message;
    } finally {
      document.getElementById("invoke-run").disabled = false;
    }
  };
}

// ---- Router ---------------------------------------------------------------
async function route() {
  const h = location.hash.slice(1) || "/";
  try {
    if (h === "/" || h === "") return pageMembers();
    const [, kind, arg] = h.split("/");
    if (kind === "member") return pageMember(decodeURIComponent(arg));
    if (kind === "account") return pageAccount(decodeURIComponent(arg));
    pageMembers();
  } catch (e) {
    view.innerHTML = `<div class="empty">Error: ${esc(e.message)}</div>`;
  }
}

(async function init() {
  try { SKILLS = await api("/api/skills"); } catch { SKILLS = []; }
  window.addEventListener("hashchange", route);
  route();
})();
