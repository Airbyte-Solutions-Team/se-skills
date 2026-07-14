"""Tests for STRUCT-002: unified Markdown -> HTML renderer.

The web reader, PDF export, and internal.airbyte.ai HTML export must all use
`md_render.markdown_to_body_html` so the same skill output renders identically
and safely across every surface.
"""

from __future__ import annotations

import app
import internal_html
import pdf_render
from webapp.md_render import markdown_to_body_html

SAMPLE_MD = """# Executive summary

This is a **test** document for ==Acme==.

> [!warning] Watch out
> Something risky.

| Name | Value |
|------|------:|
| One  | 1     |
| Two  | 2     |

- item one
- item two

- [ ] todo
- [x] done

```python
print("hello")
```

Next move: 🟢 go.
"""


def test_pdf_and_web_endpoint_share_body_html():
    """PDF wrapper and web endpoint both return the shared body renderer."""
    body = markdown_to_body_html(SAMPLE_MD)
    assert pdf_render.markdown_to_body_html(SAMPLE_MD) == body
    resp = app.api_output_render(app.OutputRender(md=SAMPLE_MD))
    assert resp.html == body


def test_internal_html_export_uses_shared_renderer():
    """The internal HTML export renders the same markdown through the shared renderer."""
    full = internal_html.render_internal_html(SAMPLE_MD, customer="Acme")
    assert "Executive summary" in full
    assert "Acme" in full
    assert "Watch out" in full
    assert "<table>" in full


def test_render_endpoint_sanitizes_input():
    """Raw HTML/script tags are stripped by the shared renderer endpoint."""
    md = "Hello <script>alert('xss')</script> world"
    resp = app.api_output_render(app.OutputRender(md=md))
    assert "<script" not in resp.html
    assert "alert" not in resp.html
    assert "world" in resp.html


def test_render_endpoint_rejects_oversized_markdown():
    """A payload above the max length is rejected by the Pydantic model."""
    from pydantic import ValidationError
    try:
        app.OutputRender(md="x" * 2_000_001)
    except ValidationError as e:
        assert "md" in str(e)
    else:
        raise AssertionError("expected ValidationError")
