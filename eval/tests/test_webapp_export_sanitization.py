"""Deterministic tests for Markdown/HTML export sanitization.

`webapp/pdf_render.markdown_to_body_html` is the shared body-HTML path used by both
PDF export and `webapp/internal_html.py`. These tests verify that unsafe tags,
inline handlers, and dangerous URL schemes are stripped while ordinary skill
output still renders correctly.
"""

import pytest

from webapp.md_render import markdown_to_body_html


NORMAL_MARKDOWN = """# Executive summary

This is a **test** document for ==Acme==.

> [!warning] Watch out
> Something risky.

| Name | Value |
|------|------:|
| One  | 1     |
| Two  | 2     |

- item one
- item two

```python
print("hello")
```

Next move: 🟢 go.
"""


def _render(md: str) -> str:
    """Render markdown to a body HTML fragment."""
    return markdown_to_body_html(md)


@pytest.mark.parametrize(
    "md,forbidden",
    [
        pytest.param(
            "Hello <script>alert('xss')</script> world",
            {"<script", "alert('xss')", "</script>"},
            id="script_tag",
        ),
        pytest.param(
            '<a href="javascript:alert(1)">click me</a>',
            {"javascript:", "alert(1)"},
            id="javascript_link",
        ),
        pytest.param(
            '<a href="vbscript:msgbox(1)">click me</a>',
            {"vbscript:", "msgbox"},
            id="vbscript_link",
        ),
        pytest.param(
            '<p onclick="alert(1)" onload="alert(2)">paragraph</p>',
            {"onclick", "onload", "alert(1)", "alert(2)"},
            id="inline_event_handlers",
        ),
        pytest.param(
            '<iframe src="https://evil.com"></iframe>',
            {"<iframe", "evil.com"},
            id="iframe",
        ),
        pytest.param(
            '<object data="https://evil.com/x.swf"></object>',
            {"<object", "x.swf"},
            id="object",
        ),
        pytest.param(
            '<img src="data:image/svg+xml,<script>alert(1)</script>">',
            {"data:image", "<script", "alert(1)"},
            id="data_uri_image",
        ),
        pytest.param(
            '<a href="#" style="background:url(javascript:alert(1))">x</a>',
            {"javascript:", "style="},
            id="style_attribute_blocked",
        ),
        pytest.param(
            "Normal text with no unsafe content",
            set(),
            id="safe_plaintext",
        ),
    ],
)
def test_unsafe_markup_is_sanitized(md: str, forbidden: set[str]) -> None:
    """Dangerous tags, handlers, and schemes are not present in rendered output."""
    html = _render(md)
    for fragment in forbidden:
        assert fragment not in html, f"forbidden fragment {fragment!r} found in {html!r}"


def test_normal_markdown_renders() -> None:
    """Expected structural tags survive for normal skill output."""
    html = _render(NORMAL_MARKDOWN)

    assert '<h1 id="executive-summary">Executive summary</h1>' in html
    assert "<strong>test</strong>" in html
    assert "<mark>" in html and "Acme" in html
    assert '<div class="admon admon-warning">' in html
    assert "<table>" in html
    assert "<thead>" in html
    assert "<tbody>" in html
    assert "<tr>" in html
    assert "<th>" in html or "<th " in html
    assert "<td" in html
    assert "<ul>" in html
    assert "<li>" in html
    assert "<code>" in html or "<code " in html
    assert '<span class="dot">🟢</span>' in html


def test_safe_links_preserved_with_rel() -> None:
    """Ordinary external links keep their href and get safe rel attributes."""
    html = _render('[Airbyte](https://airbyte.com "Airbyte")')
    assert 'href="https://airbyte.com"' in html
    assert "airbyte.com" in html
    assert 'rel="noopener noreferrer"' in html


def test_internal_relative_links_preserved() -> None:
    """Relative links used for internal navigation are preserved."""
    html = _render("[Go to section](#section)")
    assert 'href="#section"' in html


def test_table_alignment_style_preserved() -> None:
    """python-markdown's `style="text-align: right"` on cells is preserved."""
    html = _render("| a | b |\n|---|---:|\n| 1 | 2 |")
    assert "text-align" in html
    assert "2" in html


def test_unsafe_html_in_raw_markup_is_stripped() -> None:
    """Raw `<script>` tags and their content do not survive into exported HTML."""
    html = _render("<p><script>alert(1)</script> rest</p>")
    assert "<script" not in html
    assert "alert(1)" not in html


def test_dangerous_url_schemes_removed() -> None:
    """Only a small set of safe URL schemes survive in link hrefs."""
    md = (
        "[safe](https://ok.com) "
        "[bad](javascript:alert(1)) "
        "[data](data:text/html,<script>alert(1)</script>) "
        "[blob](blob:https://x)"
    )
    html = _render(md)
    assert 'href="https://ok.com"' in html
    assert "javascript" not in html
    assert "data:" not in html
    assert "blob:" not in html
