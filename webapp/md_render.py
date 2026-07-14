"""Shared Markdown -> HTML renderer for SE skill outputs.

The web reader, PDF export, and internal.airbyte.ai HTML export all use the same
`markdown_to_body_html` function. This keeps the non-standard markdown the skills
emit — admonitions, ==highlight==, GFM checkboxes, status dots — handled in one
place and sanitized before it reaches any export or browser view.
"""

from __future__ import annotations

import html
import re

import markdown
import nh3

# HTML sanitizer configuration. Keep this in sync with the tags/attributes the
# skills emit and python-markdown produces.
_ALLOWED_TAGS = {
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "hr",
    "blockquote", "ol", "ul", "li", "dl", "dt", "dd",
    "div", "pre", "code",
    "em", "strong", "a", "img",
    "table", "thead", "tbody", "tr", "th", "td", "caption", "col", "colgroup",
    "sub", "sup", "span", "mark", "abbr", "acronym", "del", "s", "nav",
}

_ALLOWED_ATTRIBUTES = {
    "*": {"class", "id"},
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "title"},
    "th": {"style", "colspan", "rowspan", "align"},
    "td": {"style", "colspan", "rowspan", "align"},
}

_ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "tel"}

_ALLOWED_STYLE_PROPERTIES = {"text-align"}

_ADMON_LABELS = {
    "info": ("Note", "admon-info"),
    "risk": ("Risk", "admon-risk"),
    "blocker": ("Blocker", "admon-blocker"),
    "warning": ("Warning", "admon-warning"),
    "verdict": ("Verdict", "admon-info"),
}


def _sanitize_html(html: str) -> str:
    """Remove unsafe tags, attributes, and URL schemes from an HTML fragment.

    Preserves the structural and styling tags used by skill output while
    stripping `<script>`, inline event handlers, `javascript:` links, and
    unsupported URL schemes.
    """
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
        filter_style_properties=_ALLOWED_STYLE_PROPERTIES,
    )


def _admonition_body_to_html(body_text: str) -> str:
    """Convert admonition body text to inline-safe HTML.

    The body may contain inline markdown (bold, lists, links). We render it
    with python-markdown and then unwrap any wrapping `<p>` tags so the body
    sits inline inside the callout. Multiple paragraphs are joined with
    `<br><br>` to preserve breaks without introducing block-level `<p>` tags.
    """
    body = markdown.markdown(body_text.strip(), extensions=["extra"]).strip()
    body = re.sub(r"</p>\s*<p>", "<br><br>", body)
    body = re.sub(r"^<p>|</p>$", "", body)
    return body


def _process_admonitions(raw: str) -> str:
    """Convert GitHub-style admonition blockquotes into explicit divs."""
    lines = raw.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^>\s*\[!(\w+)\]\s*(.*)$", line)
        if m:
            kind = m.group(1).lower()
            title = m.group(2).strip()
            label, cls = _ADMON_LABELS.get(kind, ("Note", "admon-info"))
            body_lines: list[str] = []
            i += 1
            while i < len(lines) and lines[i].startswith(">"):
                body_lines.append(re.sub(r"^>\s?", "", lines[i]))
                i += 1
            body_html = _admonition_body_to_html("\n".join(body_lines))
            title_text = html.escape(title or label)
            out.append(
                f'<div class="admon {cls}">'
                f'<div class="admon-label">{title_text}</div>'
                f'<div class="admon-body">{body_html}</div>'
                f"</div>"
            )
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _fix_blank_lines(raw: str) -> str:
    """Insert the blank line python-markdown's tables/sane_lists needs."""
    fixed: list[str] = []
    prev = ""
    for ln in raw.split("\n"):
        stripped = ln.lstrip()
        starts_table = stripped.startswith("|")
        starts_list = bool(re.match(r"^(\s*[-*+]\s|\s*\d+\.\s)", ln))
        prev_is_block = (
            prev.strip() != ""
            and not prev.lstrip().startswith("|")
            and not re.match(r"^(\s*[-*+]\s|\s*\d+\.\s)", prev)
            and not prev.lstrip().startswith("#")
            and not prev.lstrip().startswith(">")
            and not prev.lstrip().startswith("<")
        )
        if (starts_table or starts_list) and prev_is_block:
            fixed.append("")
        fixed.append(ln)
        prev = ln
    return "\n".join(fixed)


def _tasklist_to_glyphs(raw: str) -> str:
    """Replace GFM task-list checkboxes with printable glyphs.

    python-markdown does not render task-list checkboxes as styled boxes, so we
    use a glyph that downstream renderers can turn into a checkbox affordance.
    """
    raw = re.sub(r"^(\s*)[-*+]\s+\[ \]\s+", lambda m: f"{m.group(1)}- \u2610 ", raw, flags=re.MULTILINE)
    raw = re.sub(r"^(\s*)[-*+]\s+\[[xX]\]\s+", lambda m: f"{m.group(1)}- \u2611 ", raw, flags=re.MULTILINE)
    return raw


def markdown_to_body_html(md_text: str) -> str:
    """Convert skill-output markdown to a safe HTML body FRAGMENT.

    Shared by the web reader, PDF export, and internal HTML export. Handles the
    non-standard markdown the skills emit — admonitions, ==highlight==, GFM
    checkboxes, status dots — and runs the result through an allowlist sanitizer.
    """
    raw = md_text or ""

    # Admonitions and highlights are converted before the main parser so they
    # survive inside tables and other markdown blocks.
    raw = _process_admonitions(raw)
    raw = re.sub(r"==(.+?)==", r"<mark>\1</mark>", raw)
    raw = _fix_blank_lines(raw)
    raw = _tasklist_to_glyphs(raw)

    body = markdown.markdown(raw, extensions=["extra", "sane_lists", "toc"])

    # python-markdown wraps simple list items and blockquote lines in `<p>` tags.
    # The web reader treats these as inline key/value rows / quote text, so unwrap
    # them and preserve paragraph breaks with `<br><br>`.
    def _unwrap_item(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        inner = re.sub(r"</p>\s*<p>", "<br><br>", inner)
        inner = re.sub(r"^<p>|</p>$", "", inner)
        return f"{m.group(0).split('>')[0]}>{inner}</li>"

    body = re.sub(r"<li>([\s\S]*?)</li>", _unwrap_item, body)

    def _unwrap_quote(m: re.Match[str]) -> str:
        inner = m.group(1).strip()
        inner = re.sub(r"</p>\s*<p>", "<br><br>", inner)
        inner = re.sub(r"^<p>|</p>$", "", inner)
        return f'<blockquote class="md-quote">{inner}</blockquote>'

    body = re.sub(r"<blockquote>([\s\S]*?)</blockquote>", _unwrap_quote, body)

    # Wrap status emoji dots so they can be styled consistently.
    for dot in ["🟢", "🟡", "🔴", "⚠️", "❌", "✅", "⭐", "★"]:
        body = body.replace(dot, f'<span class="dot">{dot}</span>')

    return _sanitize_html(body)
