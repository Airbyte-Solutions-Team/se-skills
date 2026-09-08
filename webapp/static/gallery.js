/* Output Gallery — developer preview of the reader's visual system.
 *
 * Renders committed synthetic fixtures through exactly the production path a
 * real skill output uses: POST /api/output/render (server Markdown → sanitized
 * HTML) → seReader.buildReaderDocument (presentation transforms). There is no
 * second renderer here; if the gallery looks right, the reader looks right.
 *
 * The preview lives in an iframe so the width selector produces a real, isolated
 * viewport: production `@media` breakpoints in the shared /style.css evaluate
 * against the selected width exactly as they would in a 420px browser window.
 * No gallery-specific responsive rules exist — the frame loads the same
 * stylesheet the app does.
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

const FRAME_WIDTHS = { full: "100%", narrow: "420px", tablet: "780px" };

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") || "dark";
}

function previewFrame() {
  return docBox.querySelector("iframe.gallery-frame");
}

function applyWidth() {
  const frame = previewFrame();
  if (frame) frame.style.width = FRAME_WIDTHS[widthSel.value] || FRAME_WIDTHS.full;
}

// Measure the body box, not documentElement.scrollHeight: inside an iframe the
// latter is floored at the frame's current viewport height, so the frame could
// only ever grow and left dead space after a section collapsed.
function syncFrameHeight(frame) {
  const d = frame.contentDocument;
  if (!d || !d.body) return;
  const style = frame.contentWindow ? frame.contentWindow.getComputedStyle(d.body) : null;
  const marginBottom = style ? parseFloat(style.marginBottom) || 0 : 0;
  const height = d.body.getBoundingClientRect().height + d.body.offsetTop + marginBottom;
  const next = Math.ceil(height) + "px";
  // No-op when unchanged: writing the height resizes the frame's viewport, which
  // would re-notify the body observer inside the same delivery loop.
  if (frame.style.height !== next) frame.style.height = next;
}

// The frame is a real browsing context loading the production stylesheet, so the
// document inside it is laid out for its own width — that is the whole point of
// the width selector. Its height tracks the content because the outer page owns
// scrolling.
function writeFrame(bodyHtml) {
  const frame = previewFrame() || (() => {
    const el = document.createElement("iframe");
    el.className = "gallery-frame";
    el.title = "Rendered output preview";
    docBox.innerHTML = "";
    docBox.appendChild(el);
    return el;
  })();
  const d = frame.contentDocument;
  d.open();
  d.write(
    '<!doctype html><html data-theme="' + currentTheme() + '"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Public+Sans:wght@400;500;600;700;800&family=IBM+Plex+Mono:wght@400;500;600&display=swap">' +
    '<link rel="stylesheet" href="/style.css">' +
    "</head><body><main>" + bodyHtml + "</main></body></html>"
  );
  d.close();
  wireDocument(d);
  if (window.ResizeObserver) {
    // Defer the write out of the observer callback for the same reason.
    let queued = false;
    new ResizeObserver(() => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(() => {
        queued = false;
        syncFrameHeight(frame);
      });
    }).observe(d.body);
  }
  d.addEventListener("click", () => setTimeout(() => syncFrameHeight(frame), 0));
  // The stylesheet loads asynchronously, so measure again once it applies.
  if (frame.contentWindow) {
    frame.contentWindow.addEventListener("load", () => syncFrameHeight(frame));
  }
  syncFrameHeight(frame);
  applyWidth();
  return frame;
}

function applyFrameTheme() {
  const frame = previewFrame();
  const d = frame && frame.contentDocument;
  if (d && d.documentElement) d.documentElement.setAttribute("data-theme", currentTheme());
}

function wireDocument(root) {
  const ownerDoc = root.ownerDocument || root;
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
      reveal(ownerDoc.getElementById(a.getAttribute("href").slice(5)));
    };
  });
  root.querySelectorAll(".risk-item").forEach((b) => {
    b.onclick = () => reveal(ownerDoc.getElementById(b.dataset.riskTarget || ""));
  });
}

async function openFixture(name, title) {
  errBox.classList.add("hidden");
  picker.querySelectorAll(".gallery-tab").forEach((b) => {
    b.classList.toggle("active", b.dataset.name === name);
  });
  const md = await getText("/api/gallery/fixture?name=" + encodeURIComponent(name));
  const serverHtml = await renderMarkdown(md);
  const doc = window.seReader.buildReaderDocument(serverHtml, { title });
  writeFrame(`
    <div class="row"><h1>${esc(doc.docTitle)}</h1></div>
    <div class="doc-layout">
      ${doc.tocHtml ? `<aside class="doc-toc"><div class="doc-toc-head">On this page</div>${doc.tocHtml}</aside>` : ""}
      <article class="md-body">
        ${doc.execCardHtml}
        ${doc.riskStripHtml}
        <div class="doc-sheet">${doc.sheetHtml}</div>
      </article>
    </div>`);
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
      applyFrameTheme();
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
