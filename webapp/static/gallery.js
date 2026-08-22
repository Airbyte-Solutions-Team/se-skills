/* Output Gallery — developer preview of the reader's visual system.
 *
 * Renders committed synthetic fixtures through exactly the production path a
 * real skill output uses: POST /api/output/render (server Markdown → sanitized
 * HTML) → seReader.buildReaderDocument (presentation transforms). There is no
 * second renderer here; if the gallery looks right, the reader looks right.
 */
"use strict";

const view = document.getElementById("view");
const errBox = document.getElementById("gallery-error");
const picker = document.getElementById("gallery-picker");
const docBox = document.getElementById("gallery-doc");
const widthSel = document.getElementById("gallery-width");

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showError(msg) {
  errBox.textContent = msg;
  errBox.classList.remove("hidden");
}

// Surface any runtime error instead of silently rendering a broken document —
// the gallery's job is to make renderer breakage obvious.
window.addEventListener("error", (e) => showError("JS error: " + e.message));
window.addEventListener("unhandledrejection", (e) => showError("Unhandled rejection: " + (e.reason && e.reason.message || e.reason)));

async function getJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.json();
}

async function getText(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.text();
}

async function renderMarkdown(md) {
  const res = await fetch("/api/output/render", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ md: md || "" }),
  });
  if (!res.ok) throw new Error(`render failed: ${res.status}`);
  return (await res.json()).html;
}

function applyWidth() {
  docBox.classList.remove("width-narrow", "width-tablet");
  if (widthSel.value === "narrow") docBox.classList.add("width-narrow");
  if (widthSel.value === "tablet") docBox.classList.add("width-tablet");
}

function wireDocument(root) {
  root.querySelectorAll(".doc-section.collapsible > .md-h2").forEach((h2) => {
    h2.onclick = (e) => {
      if (e.target.closest("a")) return;
      h2.closest(".doc-section").classList.toggle("collapsed");
    };
  });
  const reveal = (el) => {
    if (!el) return;
    const sec = el.closest(".doc-section.collapsible");
    if (sec) sec.classList.remove("collapsed");
    el.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  root.querySelectorAll(".doc-toc-link").forEach((a) => {
    a.onclick = (e) => {
      e.preventDefault();
      reveal(document.getElementById(a.getAttribute("href").slice(5)));
    };
  });
  root.querySelectorAll(".risk-item").forEach((b) => {
    b.onclick = () => reveal(document.getElementById(b.dataset.riskTarget || ""));
  });
}

async function openFixture(name, title) {
  errBox.classList.add("hidden");
  picker.querySelectorAll(".gallery-tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.name === name);
  });
  docBox.innerHTML = `<p class="muted">Rendering ${esc(name)}…</p>`;
  const md = await getText("/api/gallery/fixture?name=" + encodeURIComponent(name));
  const serverHtml = await renderMarkdown(md);
  const doc = window.seReader.buildReaderDocument(serverHtml, { title });
  docBox.innerHTML = `
    <div class="row"><h1>${esc(doc.docTitle)}</h1></div>
    <div class="doc-layout">
      ${doc.tocHtml ? `<aside class="doc-toc"><div class="doc-toc-head">On this page</div>${doc.tocHtml}</aside>` : ""}
      <article class="md-body">
        ${doc.execCardHtml}
        ${doc.riskStripHtml}
        <div class="doc-sheet">${doc.sheetHtml}</div>
      </article>
    </div>`;
  wireDocument(docBox);
  applyWidth();
  location.hash = "#" + name;
}

async function main() {
  widthSel.onchange = applyWidth;
  // Same appearance-mode contract as the main app, so both modes can be
  // reviewed here (contrast has to hold in each).
  const themeBtn = document.getElementById("theme-toggle");
  const applyTheme = (theme) => {
    document.documentElement.setAttribute("data-theme", theme);
    if (themeBtn) themeBtn.textContent = theme === "light" ? "☀️" : "🌙";
  };
  applyTheme(localStorage.getItem("se-hub-theme") || "dark");
  if (themeBtn) {
    themeBtn.onclick = () => {
      const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
      localStorage.setItem("se-hub-theme", next);
      applyTheme(next);
    };
  }
  const { fixtures } = await getJson("/api/gallery/fixtures");
  if (!fixtures.length) {
    showError("No gallery fixtures found.");
    return;
  }
  picker.innerHTML = fixtures.map((f) =>
    `<button class="gallery-tab" data-name="${esc(f.name)}" title="${esc(f.title)}">${esc(f.name)}` +
    `<span class="gallery-tab-meta">${f.lines} lines</span></button>`).join("");
  picker.querySelectorAll(".gallery-tab").forEach((b) => {
    const f = fixtures.find((x) => x.name === b.dataset.name);
    b.onclick = () => openFixture(f.name, f.title);
  });
  const requested = decodeURIComponent(location.hash.replace(/^#/, ""));
  const start = fixtures.find((f) => f.name === requested) || fixtures[0];
  await openFixture(start.name, start.title);
}

main().catch((e) => showError(e.message));
