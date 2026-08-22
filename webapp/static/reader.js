// Output reader document builder.
//
// This module owns EVERY presentation-only transformation applied to the
// server-rendered (and server-sanitized) output HTML: classification of the
// information shapes the skills already emit in Markdown, the executive summary
// card, risk/action/people/fact components, table handling, collapsible audit
// sections and the sidebar index.
//
// It is deliberately separate from app.js so that the reader route and the
// developer Output Gallery run the SAME code — there is no second renderer.
// Nothing here parses Markdown or trusts raw strings: the input is always the
// sanitized fragment returned by `POST /api/output/render`
// (webapp/md_render.py, nh3 allowlist), and every value this module injects as
// text is escaped.
(function (root, factory) {
  const seReader = factory();
  if (root) root.seReader = seReader;
  if (typeof module !== "undefined" && module.exports) module.exports = seReader;
})(typeof window !== "undefined" ? window : globalThis, function () {
  "use strict";

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[character]));
  }

  // ── Profile summaries ────────────────────────────────────────────────────
  // Profile-specific summary headings are deliberately explicit so the reader
  // can promote every current producer profile while retaining legacy outputs.
  // This is the ONLY list of summary names: promotion, the `is-glance` panel and
  // sidebar filtering all derive from it (see isSummaryHeadingText).
  const PROFILE_SUMMARY_NAMES = [
    "Meeting Snapshot",
    "Call Snapshot",
    "Decision Summary",
    "Decision Summary / Bottom Line",
    "POC Summary",
    "Business-Case Summary",
    "Close Summary",
    "Account Snapshot",
    "Recommendation",
    "Meeting / Decision Summary",
    "Coverage Snapshot",
    "Severity / Bottom Line",
  ];

  function summaryHeadingLabelText(text) {
    const value = (text || "").trim();
    const match = PROFILE_SUMMARY_NAMES.find((name) => name.toLowerCase() === value.toLowerCase());
    if (match) return match;
    return /^at a glance$/i.test(value) ? "At a Glance" : "";
  }

  function isSummaryHeadingText(text) {
    return Boolean(summaryHeadingLabelText(text))
      || /^(?:\d+-second|current read|in (?:a )?nutshell)\b/i.test((text || "").trim());
  }

  function filterSummaryTocEntries(toc) {
    return (toc || []).filter((t) => (t.level === 2 || t.level === 3)
      && !isSummaryHeadingText(t.text));
  }

  function summaryHeadingLabel(section) {
    const heading = section?.querySelector(":scope > h2, :scope > h3");
    return summaryHeadingLabelText(heading?.textContent || "");
  }

  // Shorten a heading for the sidebar index: drop trailing parentheticals and
  // "— …" qualifiers so labels stay scannable (e.g. "Suggested Agenda (30 min)"
  // → "Suggested Agenda").
  function conciseLabel(text) {
    return (text || "").replace(/\s*\([^)]*\)\s*$/, "").replace(/\s*[—–-]\s.*$/, "").trim() || text;
  }

  // ── Summary tiles ───────────────────────────────────────────────────────
  // Which summary labels become prominent decision tiles, and in what order.
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

  // Infer a sentiment class for a tile from its value text (status dots, band
  // words, probability ranges). Drives the tile's status color, which is only
  // ever used for real status/verdict/risk meaning — never decoration.
  function tileSentiment(label, valueText) {
    const t = (valueText || "").toLowerCase();
    const l = (label || "").toLowerCase();
    if (/confidence/.test(l)) {
      if (/🔴/.test(valueText) || /\blow\b/.test(t)) return "sev-danger";
      if (/🟡/.test(valueText) || /\bmedium\b/.test(t)) return "sev-warn";
      if (/🟢/.test(valueText) || /\bhigh\b/.test(t)) return "sev-good";
    }
    if (/🔴/.test(valueText) || /\bat risk\b|\bblocker\b|\bsilent\b|\bdead\b|\bdying\b|\bweak\b|<\s*20/.test(t)) return "sev-danger";
    if (/🟡/.test(valueText) || /\bneeds\b|\bcaution\b|\bsteady\b|\bmedium\b|20[–-]40|20[–-]60|40[–-]60/.test(t)) return "sev-warn";
    if (/🟢/.test(valueText) || /\blikely\b|\bvery likely\b|\bcommitted\b|\bstrong\b|\bviable\b|\bgo\b|60[–-]80|>\s*80/.test(t)) return "sev-good";
    if (/motion|next/.test(l)) return "sev-accent";
    return "";
  }

  // ── Section classification ──────────────────────────────────────────────
  // Sections collapsed by default (audit / supporting detail). Matched on the H2.
  const COLLAPSE_DEFAULT = /source coverage|activity trajectory|meddpicc|coaching|appendix|raw|evidence reviewed/i;

  // Sections whose bullets read as risks/watch-outs.
  const RISK_SECTION = /watch-?outs?|what would lose|new objections|concerns|risks?(?!\w)|red flags/i;

  // Sections whose `**Lead.** detail` bullets read as neutral structured items.
  const INFO_SECTION = /constraints?|edge cases?|considerations?|assumptions?/i;

  // Ranked action lists (next-move "Ranked Next Moves").
  const EXEC_SECTION = /ranked next moves?/i;

  // Audit/provenance sections: quieter surface, never hidden or dropped.
  const EVIDENCE_SECTION = /source coverage|evidence reviewed|context inventory|appendix|raw (notes|transcript)|meddpicc|coaching/i;

  // Sections that list people. Recognition is heading-driven (document
  // structure), never inferred from prose.
  const PEOPLE_SECTION = /who'?s who|attendees|stakeholders?|key players|participants|buying (committee|group)|cast|roles (&|and) responsibilities|two-sided responsibilities/i;

  // Sections that carry committed work: the action reads first, owner/date/status
  // stay subordinate.
  const ACTION_SECTION = /next steps?|next moves?|action items?|recommended (next )?(actions?|steps?|moves?)|commitments|path to signature|decisions (required|needed)|external actions|asks/i;

  // Sections that are lists of questions.
  const QUESTION_SECTION = /questions?(?!\w)/i;

  // Sections whose lead paragraph is a thesis/headline claim.
  const THESIS_SECTION = /why airbyte|why this move|point of view to test|deal thesis|one-slide eb view|use case summary/i;

  function sectionRole(title) {
    const text = (title || "").trim();
    if (!text) return "plain";
    if (EVIDENCE_SECTION.test(text)) return "evidence";
    if (PEOPLE_SECTION.test(text)) return "people";
    if (RISK_SECTION.test(text)) return "risk";
    if (ACTION_SECTION.test(text)) return "action";
    if (QUESTION_SECTION.test(text)) return "questions";
    if (THESIS_SECTION.test(text)) return "thesis";
    return "plain";
  }

  // Infer a severity for a risk bullet from its wording.
  function riskSeverity(text) {
    const t = (text || "").toLowerCase();
    if (/🔴|\bhigh\b|\bblocker\b|\bcan'?t close|cannot close|deal-?killer|center of gravity|fiction|dead\b/.test(t)) return "blocker";
    return "risk";
  }

  // ── Table classification ────────────────────────────────────────────────
  // A Connector Feasibility system table is recognized by its HEADER SHAPE only.
  // Anything unrecognized keeps its table (wrapped so it can never clip) rather
  // than being reshaped on a guess.
  const SYSTEM_FIELD_PATTERNS = [
    /^connector/i,
    /^exists/i,
    /^(availability|ga|ga status|release stage|support)/i,
    /(use[- ]case fit|fit)$/i,
    /^confidence/i,
    /(risk|top risk|biggest risk)/i,
    /^(sync|cdc|incremental)/i,
  ];

  function classifyTableShape(headers) {
    const cells = (headers || []).map((h) => (h || "").trim()).filter(Boolean);
    if (cells.length < 4) return "generic";
    if (!/^(system|source system|source|target system|platform)$/i.test(cells[0])) return "generic";
    const matched = SYSTEM_FIELD_PATTERNS.filter((re) => cells.slice(1).some((c) => re.test(c)));
    return matched.length >= 3 ? "system-records" : "generic";
  }

  // ── People rows ─────────────────────────────────────────────────────────
  // "Name — Title, note" / "**Name** — Title" / "Name: Title". Returns null when
  // the item is not shaped like one person, so a mixed list is left untouched.
  function parsePersonEntry(text) {
    const value = (text || "").replace(/\s+/g, " ").trim();
    const match = value.match(/^([^—–:]{2,60}?)\s*(?:—|–|:)\s+(.{2,})$/);
    if (!match) return null;
    const name = match[1].trim();
    if (!name || /[.!?]$/.test(name)) return null;
    if (name.split(/\s+/).length > 5) return null;
    if (!/^[\p{Lu}\p{N}"'(]/u.test(name)) return null;
    return { name, detail: match[2].trim() };
  }

  // ── Action metadata ─────────────────────────────────────────────────────
  const ACTION_META_LABELS = /^(owner|owners|due|due date|date|status|when|by when|target|target date|deadline|confirm by)$/i;

  // Split "Migrate the pipeline · Owner: Dana Reyes · Due: 2026-08-01" into the
  // action and its subordinate metadata. Returns null when no metadata label is
  // present, so ordinary bullets stay ordinary bullets.
  function splitActionMeta(text) {
    const value = (text || "").replace(/\s+/g, " ").trim();
    if (!value) return null;
    const segments = value.split(/\s*(?:·|\||;)\s*/).filter((s) => s.trim());
    if (segments.length < 2) return null;
    const meta = [];
    const kept = [];
    segments.forEach((segment) => {
      const match = segment.match(/^([A-Za-z][A-Za-z ]{1,12}?)\s*:\s*(.+)$/);
      if (match && ACTION_META_LABELS.test(match[1].trim())) {
        meta.push({ label: match[1].trim(), value: match[2].trim() });
      } else {
        kept.push(segment.trim());
      }
    });
    if (!meta.length || !kept.length) return null;
    return { action: kept.join(" · "), meta };
  }

  // ── HTML class pass ─────────────────────────────────────────────────────
  // Decorate the sanitized server fragment with presentation classes. Operates
  // only on the server's own markup shapes; it never introduces new tags for
  // untrusted text.
  function addMdClasses(html) {
    html = html.replace(
      /<div class="admon admon-(\w+)"><div class="admon-label">([\s\S]*?)<\/div><div class="admon-body">([\s\S]*?)<\/div><\/div>/g,
      '<div class="admon callout callout-$1 admon-$1"><div class="admon-label callout-title">$2</div><div class="admon-body callout-body">$3</div></div>'
    );
    html = html.replace(/<h([1-6])([^>]*)>/g, '<h$1 class="md-h md-h$1"$2>');
    html = html.replace(/<p>/g, '<p class="md-p">');
    html = html.replace(/<ul>/g, '<ul class="md-list">');
    html = html.replace(/<ol>/g, '<ol class="md-list">');
    html = html.replace(/<table>/g, '<table class="md-table">');
    html = html.replace(/<pre>/g, '<pre class="md-pre">');
    html = html.replace(/<hr\s*\/?>/g, '<hr class="md-hr" />');
    html = html.replace(/<mark>/g, '<mark class="md-key">');
    html = html.replace(/<li>(☐|☑)\s+([\s\S]*?)<\/li>/g, (m, box, text) => {
      const done = box === '☑';
      return `<li class="md-check"><span class="md-cbox${done ? ' done' : ''}" aria-hidden="true">${done ? '✓' : ''}</span><span class="md-check-text">${text}</span></li>`;
    });
    html = html.replace(/<a href="([^"]*)"([^>]*)>/g, (m, href, rest) => {
      if (rest.includes('target=')) return m;
      if (rest.includes('rel=')) return `<a href="${href}"${rest} target="_blank">`;
      return `<a href="${href}"${rest} target="_blank" rel="noopener">`;
    });
    return html;
  }

  // ── DOM transformations ─────────────────────────────────────────────────
  // Convert "**Label:** value" lines into a scannable label/value grid (.kv).
  function upgradeKeyValues(root) {
    const KV = /^\s*<strong>([^<:]{1,40}):<\/strong>\s*([\s\S]*)$/;

    const toRow = (html) => {
      const m = html.match(KV);
      if (!m) return null;
      return `<div class="kv"><span class="kv-k">${m[1].trim()}</span><span class="kv-v">${m[2].trim()}</span></div>`;
    };

    root.querySelectorAll("p.md-p").forEach((p) => {
      const parts = p.innerHTML.split(/\s*·\s*/);
      const rows = parts.map(toRow);
      if (rows.length >= 2 && rows.every(Boolean)) {
        const grid = p.ownerDocument.createElement("div");
        grid.className = "kv-grid";
        grid.innerHTML = rows.join("");
        p.replaceWith(grid);
      }
    });

    root.querySelectorAll("ul.md-list").forEach((ul) => {
      const items = Array.from(ul.children);
      const rows = items.map((li) => toRow(li.innerHTML));
      if (items.length && rows.every(Boolean)) {
        const grid = ul.ownerDocument.createElement("div");
        grid.className = "kv-grid";
        grid.innerHTML = rows.join("");
        ul.replaceWith(grid);
      }
    });
  }

  // Tables: recognized Connector Feasibility system tables become one vertical
  // record per system (no clipped right-most columns); everything else is
  // wrapped in a scroll container so a wide generic table stays usable instead
  // of overflowing the page.
  function upgradeTables(root) {
    root.querySelectorAll("table.md-table").forEach((table) => {
      const doc = table.ownerDocument;
      const headerCells = Array.from(table.querySelectorAll("thead th"));
      const headers = headerCells.map((th) => th.textContent || "");
      const bodyRows = Array.from(table.querySelectorAll("tbody tr"))
        .filter((tr) => tr.querySelectorAll("td").length === headers.length);

      if (classifyTableShape(headers) === "system-records" && bodyRows.length) {
        const wrap = doc.createElement("div");
        wrap.className = "sys-records";
        bodyRows.forEach((tr) => {
          const cells = Array.from(tr.querySelectorAll("td"));
          const record = doc.createElement("div");
          record.className = "sys-record";
          const title = doc.createElement("div");
          title.className = "sys-record-title";
          title.innerHTML = cells[0].innerHTML;
          record.appendChild(title);
          const fields = doc.createElement("div");
          fields.className = "sys-fields";
          cells.slice(1).forEach((td, index) => {
            const field = doc.createElement("div");
            field.className = "sys-field";
            const label = doc.createElement("span");
            label.className = "sys-field-k";
            label.textContent = (headers[index + 1] || "").trim();
            const value = doc.createElement("span");
            value.className = "sys-field-v";
            value.innerHTML = (td.textContent || "").trim() ? td.innerHTML : "—";
            field.appendChild(label);
            field.appendChild(value);
            fields.appendChild(field);
          });
          record.appendChild(fields);
          wrap.appendChild(record);
        });
        table.replaceWith(wrap);
        return;
      }

      const scroll = doc.createElement("div");
      scroll.className = "md-table-wrap";
      scroll.setAttribute("tabindex", "0");
      scroll.setAttribute("role", "region");
      scroll.setAttribute("aria-label", "Table (scrolls horizontally)");
      table.replaceWith(scroll);
      scroll.appendChild(table);
    });
  }

  // Nearest heading above an element, within its section.
  function nearestHeadingText(el) {
    let node = el;
    while (node) {
      let prev = node.previousElementSibling;
      while (prev) {
        if (/^h[1-6]$/i.test(prev.tagName)) return prev.textContent || "";
        prev = prev.previousElementSibling;
      }
      node = node.parentElement;
      if (!node || node.classList?.contains("doc-section")) {
        const h2 = node?.querySelector(":scope > h2");
        return h2 ? h2.textContent || "" : "";
      }
    }
    return "";
  }

  // People lists → one person per row (name prominent, role subordinate).
  function upgradePeopleLists(section) {
    section.querySelectorAll("ul.md-list").forEach((ul) => {
      if (!PEOPLE_SECTION.test(nearestHeadingText(ul))) return;
      const items = Array.from(ul.children).filter((li) => li.tagName === "LI");
      if (items.length < 2) return;
      const parsed = items.map((li) => {
        const entry = parsePersonEntry(li.textContent || "");
        if (!entry) return null;
        const html = li.innerHTML;
        const cut = html.match(/^([\s\S]*?)\s*(?:—|–|:)\s+([\s\S]+)$/);
        if (!cut) return null;
        return { name: entry.name, detailHtml: cut[2] };
      });
      if (!parsed.every(Boolean)) return;
      const doc = ul.ownerDocument;
      const wrap = doc.createElement("div");
      wrap.className = "people";
      parsed.forEach((person) => {
        const row = doc.createElement("div");
        row.className = "person";
        row.innerHTML = `<span class="person-name">${escapeHtml(person.name)}</span>`
          + `<span class="person-role">${person.detailHtml}</span>`;
        wrap.appendChild(row);
      });
      ul.replaceWith(wrap);
    });
  }

  // Action lists → the action reads first, owner/date/status become subordinate
  // chips. Only fires when the Markdown actually carries labeled metadata.
  function upgradeActionLists(section) {
    section.querySelectorAll("ul.md-list, ol.md-list").forEach((list) => {
      if (!ACTION_SECTION.test(nearestHeadingText(list))) return;
      const items = Array.from(list.children).filter((li) => li.tagName === "LI");
      if (!items.length) return;
      const parsed = items.map((li) => {
        if (li.querySelector("ul, ol")) return null;
        const split = splitActionMeta(li.textContent || "");
        if (!split) return null;
        const segments = li.innerHTML.split(/\s*(?:·|\||;)\s*/);
        const kept = [];
        const meta = [];
        segments.forEach((segment) => {
          const plain = segment.replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
          const match = plain.match(/^([A-Za-z][A-Za-z ]{1,12}?)\s*:\s*(.+)$/);
          if (match && ACTION_META_LABELS.test(match[1].trim())) {
            meta.push({ label: match[1].trim(), value: match[2].trim() });
          } else if (plain) {
            kept.push(segment.trim());
          }
        });
        if (!meta.length || !kept.length) return null;
        return { html: kept.join(" · "), meta };
      });
      if (!parsed.some(Boolean)) return;
      list.classList.add("action-list");
      items.forEach((li, index) => {
        const entry = parsed[index];
        if (!entry) return;
        const chips = entry.meta.map((m) =>
          `<span class="action-chip"><span class="action-chip-k">${escapeHtml(m.label)}</span>`
          + `<span class="action-chip-v">${escapeHtml(m.value)}</span></span>`).join("");
        li.classList.add("action");
        li.innerHTML = `<span class="action-text">${entry.html}</span>`
          + `<span class="action-meta">${chips}</span>`;
      });
    });
  }

  // Thesis sections: headline claim on its own line, explanation as prose, a
  // wholly-italic trailing line as a muted supporting note. Heading-scoped so
  // arbitrary paragraphs never become cards.
  function upgradeThesis(section) {
    const heading = section.querySelector(":scope > h2");
    if (!heading || !THESIS_SECTION.test(heading.textContent || "")) return;
    const first = heading.nextElementSibling;
    if (!first || first.tagName !== "P") return;
    const lead = first.firstElementChild;
    if (!lead || lead.tagName !== "STRONG" || lead !== first.firstChild) return;
    const doc = section.ownerDocument;
    const wrap = doc.createElement("div");
    wrap.className = "thesis";
    const head = doc.createElement("div");
    head.className = "thesis-head";
    head.textContent = (lead.textContent || "").trim();
    wrap.appendChild(head);
    const clone = first.cloneNode(true);
    clone.firstElementChild?.remove();
    const bodyHtml = clone.innerHTML.replace(/^[\s.:—-]+/, "");
    if (bodyHtml.trim()) {
      const body = doc.createElement("div");
      body.className = "thesis-body";
      body.innerHTML = bodyHtml;
      wrap.appendChild(body);
    }
    first.replaceWith(wrap);
    const note = wrap.nextElementSibling;
    if (note && note.tagName === "P" && note.children.length === 1
        && note.firstElementChild?.tagName === "EM"
        && note.firstElementChild.textContent.trim() === (note.textContent || "").trim()) {
      note.classList.add("thesis-note");
    }
  }

  // Question lists get a scannable class: the question stays prominent, an
  // optional "why this matters" tail stays secondary. Order is never changed.
  function upgradeQuestionLists(section) {
    section.querySelectorAll("ul.md-list, ol.md-list").forEach((list) => {
      if (!QUESTION_SECTION.test(nearestHeadingText(list))) return;
      list.classList.add("qa-list");
    });
  }

  // ── Discovery groups ────────────────────────────────────────────────────
  // The canonical Prep Call architecture gives `Discovery Plan` sibling H3
  // question groups (Must-Ask / Implication-Depth / Persona-Specific). The
  // must-ask group is the one the AE has to get through on the call, so it reads
  // as the primary group and the topical groups stay legible but subordinate.
  // Recognition is purely structural — the section's own H2 must be the
  // discovery section and the groups must be its sibling H3s. No prose is
  // inspected, nothing is reordered and no text is added or removed.
  const DISCOVERY_SECTION = /^discovery (plan|questions)$/i;
  const MUST_ASK_GROUP = /^must[- ]ask\b/i;

  function upgradeDiscoveryGroups(section, doc) {
    const h2 = section.querySelector(":scope > h2.md-h2");
    if (!h2 || !DISCOVERY_SECTION.test((h2.textContent || "").trim())) return;
    const heads = Array.from(section.querySelectorAll(":scope > h3.md-h3"));
    if (heads.length < 2) return;
    const mustAsk = heads.filter((h) => MUST_ASK_GROUP.test((h.textContent || "").trim()));
    if (mustAsk.length !== 1) return;
    section.classList.add("has-discovery-groups");
    heads.forEach((head) => {
      const group = doc.createElement("div");
      group.className = head === mustAsk[0]
        ? "qa-group qa-group--primary"
        : "qa-group qa-group--supporting";
      head.before(group);
      let node = group.nextSibling;
      while (node) {
        const next = node.nextSibling;
        if (node !== head && node.nodeType === 1 && /^H[1-6]$/.test(node.tagName)) break;
        group.appendChild(node);
        node = next;
      }
    });
  }

  // One-line preview shown when a section is collapsed.
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

  // ── Document builder ────────────────────────────────────────────────────
  // `serverHtml` is the sanitized fragment from /api/output/render.
  // Returns the pieces the reader (and the gallery) compose into a page.
  function buildReaderDocument(serverHtml, options) {
    const opts = options || {};
    const doc = opts.document
      || (typeof document !== "undefined" ? document : null);
    if (!doc) throw new Error("buildReaderDocument requires a document");

    const tmp = doc.createElement("div");
    tmp.innerHTML = addMdClasses(serverHtml || "");

    // Sidebar index is collected from the document in SOURCE ORDER before any
    // restructuring, so it can never contradict the Markdown.
    const toc = [];
    tmp.querySelectorAll("h1, h2, h3, h4, h5, h6").forEach((h) => {
      toc.push({ level: parseInt(h.tagName[1], 10), text: (h.textContent || "").trim(), id: h.id });
    });

    const h1 = tmp.querySelector("h1");
    const docTitle = h1 ? h1.textContent.trim() : (opts.title || "");
    if (h1) h1.remove();

    // Drop the "Jump to:" paragraph if the skill emitted one (the sidebar
    // replaces it).
    tmp.querySelectorAll("p.md-p").forEach((p) => {
      if (/^\s*jump to:/i.test(p.textContent)) p.remove();
    });

    // Shorten the in-doc section headers too (sidebar + headers stay consistent).
    tmp.querySelectorAll("h2.md-h, h3.md-h").forEach((h) => {
      const short = conciseLabel(h.textContent);
      if (short && short !== h.textContent) h.textContent = short;
    });

    upgradeKeyValues(tmp);
    upgradeTables(tmp);

    // Group the flat node list into H2-delimited SECTIONS inside ONE sheet.
    const sections = [];
    let cur = null;
    const newSection = () => {
      cur = doc.createElement("section");
      cur.className = "doc-section";
      sections.push(cur);
    };
    for (const n of Array.from(tmp.childNodes)) {
      if (n.nodeType === 1 && n.tagName === "H2") newSection();
      if (!cur) newSection();
      cur.appendChild(n);
    }

    // ── Promote the profile summary into a standalone decision card.
    let execCardHtml = "";
    const leadSection = sections[0];
    const leadSummaryLabel = summaryHeadingLabel(leadSection);
    const leadIsGlance = leadSection && leadSummaryLabel
      && !leadSection.querySelector(":scope > h2.md-h2");
    if (leadIsGlance) {
      let readHtml = "";
      const readNodes = new Set();
      const heads = Array.from(leadSection.querySelectorAll(":scope > h3.md-h3"));
      const readHead = heads.find((h) => /\d+-second|current read|the .*version|in (a )?nutshell/i.test(h.textContent || ""));
      if (readHead) {
        readNodes.add(readHead);
        let n = readHead.nextElementSibling;
        const parts = [];
        while (n && !/^h[1-6]$/i.test(n.tagName) && n.tagName !== "HR"
               && !n.classList?.contains("callout")) {
          if (!n.classList?.contains("kv-grid") && (n.textContent || "").trim()) parts.push(n.innerHTML);
          readNodes.add(n);
          n = n.nextElementSibling;
        }
        if (parts.length) readHtml = `<div class="exec-read">${parts.join(" ")}</div>`;
      }
      const kvs = Array.from(leadSection.querySelectorAll(".kv-grid .kv"));
      const tiles = [];
      const rest = [];
      const tiledLabels = new Set();
      for (const kv of kvs) {
        const label = (kv.querySelector(".kv-k")?.textContent || "").trim();
        const valHtml = kv.querySelector(".kv-v")?.innerHTML || "";
        const valText = kv.querySelector(".kv-v")?.textContent || "";
        const spec = TILE_LABELS.find((s) => s.match.some((m) => label.toLowerCase().includes(m)));
        if (spec && !tiles.some((t) => t.key === spec.key)) {
          tiles.push({ key: spec.key, label, valHtml, sev: tileSentiment(label, valText) });
          tiledLabels.add(label.toLowerCase());
        } else if (!tiledLabels.has(label.toLowerCase())) {
          rest.push(`<div class="kv"><span class="kv-k">${escapeHtml(label)}</span><span class="kv-v">${valHtml}</span></div>`);
        }
      }
      if (tiles.length) {
        tiles.sort((a, b) => TILE_LABELS.findIndex((s) => s.key === a.key) - TILE_LABELS.findIndex((s) => s.key === b.key));
        const tileHtml = tiles.map((t) =>
          `<div class="tile ${t.sev}"><div class="tile-label">${escapeHtml(t.label)}</div><div class="tile-value">${t.valHtml}</div></div>`
        ).join("");
        const restHtml = rest.length ? `<div class="exec-rest"><div class="kv-grid">${rest.join("")}</div></div>` : "";
        execCardHtml = `<div class="exec-card"><div class="exec-card-eyebrow">${escapeHtml(leadSummaryLabel)}</div>`
          + `${readHtml}<div class="tile-grid">${tileHtml}</div>${restHtml}</div>`;
        sections.shift();
        const stray = Array.from(leadSection.childNodes).filter((n) => {
          if (n.nodeType !== 1) return false;
          if (readNodes.has(n)) return false;
          if (n.classList?.contains("kv-grid") || /^h[1-6]$/i.test(n.tagName)) return false;
          const txt = (n.textContent || "").trim();
          if (!txt) return false;
          if (n.tagName === "P" && n.children.length === 1 && n.firstElementChild?.tagName === "EM"
              && n.firstElementChild.textContent.trim() === txt) return false;
          return true;
        });
        if (stray.length) {
          const lead = doc.createElement("section");
          lead.className = "doc-section";
          stray.forEach((n) => lead.appendChild(n));
          sections.unshift(lead);
        }
      }
    }

    // Tag an unpromoted summary section so it still gets the summary surface.
    for (const s of sections) {
      if (summaryHeadingLabel(s)) s.classList.add("is-glance");
    }

    // ── "**Lead.** detail" bullet sections → severity / neutral cards.
    let riskCardSeq = 0;
    for (const s of sections) {
      const title = s.querySelector(":scope > h2.md-h2")?.textContent || "";
      const isRisk = RISK_SECTION.test(title);
      const isExec = !isRisk && EXEC_SECTION.test(title);
      const isInfo = !isRisk && !isExec && INFO_SECTION.test(title);
      if (!isRisk && !isInfo && !isExec) continue;

      if (isExec) {
        const kids = Array.from(s.children).filter((n) => n !== s.querySelector(":scope > h2.md-h2"));
        const isLead = (n) => n.tagName === "P" && n.firstElementChild?.tagName === "STRONG"
          && n.firstElementChild === n.firstChild;
        if (kids.some(isLead)) {
          const wrap = doc.createElement("div");
          wrap.className = "risk-cards";
          let card = null;
          kids.forEach((n) => {
            if (isLead(n)) {
              const leadText = (n.firstElementChild.textContent || "").replace(/[.:]\s*$/, "").trim();
              card = doc.createElement("div");
              card.className = "risk-card info";
              card.innerHTML = `<div class="risk-card-head"><span class="risk-card-title">${escapeHtml(leadText)}</span></div>`
                + `<div class="risk-card-body"></div>`;
              wrap.appendChild(card);
            } else if (card) {
              card.querySelector(".risk-card-body").appendChild(n.cloneNode(true));
            } else {
              wrap.appendChild(n.cloneNode(true));
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
      if (!items.length || !items.every((li) => li.querySelector(":scope > strong"))) continue;
      const wrap = doc.createElement("div");
      wrap.className = "risk-cards";
      items.forEach((li) => {
        const lead = li.querySelector(":scope > strong");
        const leadText = (lead?.textContent || "").replace(/[.:]\s*$/, "").trim();
        const sev = isRisk ? riskSeverity(li.textContent || "") : "info";
        const clone = li.cloneNode(true);
        clone.querySelector(":scope > strong")?.remove();
        const bodyHtml = clone.innerHTML.replace(/^[\s.:—-]+/, "");
        const card = doc.createElement("div");
        card.className = `risk-card ${sev}`;
        if (isRisk) card.id = `risk-card-${riskCardSeq++}`;
        const pill = isRisk ? `<span class="risk-sev ${sev}">${sev}</span>` : "";
        card.innerHTML = `<div class="risk-card-head">${pill}`
          + `<span class="risk-card-title">${escapeHtml(leadText)}</span></div>`
          + (bodyHtml.trim() ? `<div class="risk-card-body">${bodyHtml}</div>` : "");
        wrap.appendChild(card);
      });
      ul.replaceWith(wrap);
    }

    // ── Information-aware components, all scoped by the document's own headings.
    for (const s of sections) {
      upgradeThesis(s);
      upgradePeopleLists(s);
      upgradeActionLists(s);
      upgradeQuestionLists(s);
      upgradeDiscoveryGroups(s, doc);
    }

    // ── Role classes (surface weight) + collapsible audit/detail sections.
    for (const s of sections) {
      const h2 = s.querySelector(":scope > h2.md-h2");
      if (!h2) continue;
      const titleText = h2.textContent || "";
      s.classList.add(`doc-section--role-${sectionRole(titleText)}`);
      const body = doc.createElement("div");
      body.className = "sec-body";
      Array.from(s.childNodes).forEach((n) => { if (n !== h2) body.appendChild(n); });
      const summary = doc.createElement("div");
      summary.className = "sec-summary";
      s.appendChild(body);
      summary.textContent = sectionSummary(s, titleText);
      const tog = doc.createElement("span");
      tog.className = "sec-toggle";
      tog.textContent = "▾";
      h2.appendChild(tog);
      s.classList.add("collapsible");
      s.insertBefore(summary, body);
      if (COLLAPSE_DEFAULT.test(titleText)) s.classList.add("collapsed");
    }

    // ── Drop a leading section that only contains document meta.
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

    // ── Top-risk strip.
    const riskItems = [];
    sections.forEach((s) => {
      s.querySelectorAll(".callout-risk, .callout-blocker, .risk-card:not(.info)").forEach((c) => {
        let sev;
        let title;
        if (c.classList.contains("risk-card")) {
          sev = c.classList.contains("blocker") ? "blocker" : "risk";
          title = (c.querySelector(".risk-card-title")?.textContent || "").trim().replace(/\s+/g, " ");
        } else {
          sev = c.classList.contains("callout-blocker") ? "blocker" : "risk";
          const label = (c.querySelector(".callout-title")?.textContent || "").trim();
          const body = (c.querySelector(".callout-body")?.textContent || "").trim();
          // A bare "[!risk]" callout renders its type as the title; the strip
          // needs the actual sentence, not the severity word twice.
          title = (/^(risk|blocker|verdict|info|note|warning)$/i.test(label) || !label ? body : label)
            .replace(/\s+/g, " ");
          if (!c.id) c.id = `risk-anchor-${riskItems.length}`;
        }
        if (!title) return;
        riskItems.push({ sev, title: title.length > 160 ? title.slice(0, 157) + "…" : title, id: c.id });
      });
    });
    riskItems.sort((a, b) => (a.sev === "blocker" ? 0 : 1) - (b.sev === "blocker" ? 0 : 1));
    const topRisks = riskItems.slice(0, 4);
    const riskStripHtml = topRisks.length
      ? `<div class="risk-strip"><div class="risk-strip-head">Top Risks</div>`
        + topRisks.map((r) =>
          `<button class="risk-item" data-risk-target="${escapeHtml(r.id)}">`
          + `<span class="risk-sev ${r.sev}">${r.sev}</span>`
          + `<span class="risk-title">${escapeHtml(r.title)}</span><span class="risk-arrow">→</span></button>`
        ).join("") + `</div>`
      : "";

    const sheetHtml = sections.map((s) => s.outerHTML).join("");

    // Sidebar index — H2/H3 only, concise labels, in document order. Summary
    // headings are lead metadata, not navigable sections, and are filtered with
    // the same predicate that drives promotion.
    const tocEntries = filterSummaryTocEntries(toc);
    const tocHtml = tocEntries
      .map((t) => `<a href="#toc-${escapeHtml(t.id)}" class="doc-toc-link lvl${t.level}">${escapeHtml(conciseLabel(t.text))}</a>`)
      .join("");

    return { docTitle, execCardHtml, riskStripHtml, sheetHtml, tocEntries, tocHtml };
  }

  return {
    escapeHtml,
    PROFILE_SUMMARY_NAMES,
    summaryHeadingLabelText,
    isSummaryHeadingText,
    filterSummaryTocEntries,
    summaryHeadingLabel,
    conciseLabel,
    TILE_LABELS,
    tileSentiment,
    riskSeverity,
    sectionRole,
    upgradeDiscoveryGroups,
    classifyTableShape,
    parsePersonEntry,
    splitActionMeta,
    addMdClasses,
    upgradeKeyValues,
    upgradeTables,
    sectionSummary,
    buildReaderDocument,
  };
});
