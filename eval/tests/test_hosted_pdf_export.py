"""Slice 6B1: fidelity and inertness of the in-process hosted PDF renderer.

These tests are deterministic and need no container: they exercise the
translation from approved Markdown to ReportLab flowables, plus the bounded
in-memory build. The hosted export route is covered in `test_hosted_exports.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from reportlab.platypus import Flowable, Paragraph, Preformatted, Table

from hosted import pdf_export

_FIXTURE = Path("eval/fixtures/outputs/post-call-full.md")


def _flowable_text(md_text: str) -> str:
    """Return the text the renderer would place in the PDF, in document order.

    ReportLab compresses page streams, so the rendered bytes are not greppable.
    Asserting on the flowables instead keeps the fidelity checks readable and
    still covers the only translation step the hosted export performs.
    """
    parser = pdf_export._FragmentParser()
    parser.feed(pdf_export.md_render.markdown_to_body_html(md_text))
    parser.close()
    parts: list[str] = []

    def _walk(flowables: list[Flowable]) -> None:
        for flowable in flowables:
            if isinstance(flowable, Paragraph):
                parts.append(flowable.text)
            elif isinstance(flowable, Preformatted):
                parts.extend(flowable.lines)
            elif isinstance(flowable, Table):
                for row in flowable._cellvalues:
                    _walk(list(row))

    _walk(pdf_export._blocks(parser.root, pdf_export._styles()))
    return "\n".join(parts)


def test_reviewed_material_survives_rendering() -> None:
    """Sections, tables, lists, and ordering of a real output are preserved."""
    text = _flowable_text(_FIXTURE.read_text(encoding="utf-8"))
    for heading in ("Key Takeaways", "Actions &amp; Next Step", "Source Coverage"):
        assert heading in text
    assert text.index("Key Takeaways") < text.index("Source Coverage")


@pytest.mark.parametrize(
    "markdown,present,absent",
    [
        pytest.param(
            "<script>alert(1)</script>\n\nAfter the script.",
            ["After the script."],
            ["alert(1)"],
            id="raw_script_is_dropped_by_the_shared_sanitizer",
        ),
        pytest.param(
            '<img src=x onerror="alert(1)">\n\nAfter the image.',
            ["After the image."],
            ["onerror", "alert(1)"],
            id="event_handler_never_reaches_the_pdf",
        ),
        pytest.param(
            "[click me](javascript:alert(1))",
            ["click me"],
            ["<link", "javascript:"],
            id="javascript_url_renders_as_inert_text",
        ),
        pytest.param(
            "[docs](https://docs.airbyte.com/)",
            ['<link href="https://docs.airbyte.com/"', "docs"],
            [],
            id="http_link_stays_clickable",
        ),
        pytest.param(
            "A `<b>literal</b>` snippet",
            ["&lt;b&gt;literal&lt;/b&gt;"],
            ["<b>literal</b>"],
            id="code_span_html_stays_escaped_text",
        ),
        pytest.param(
            "**bold** and *italic*",
            ["<b>bold</b>", "<i>italic</i>"],
            [],
            id="emphasis_becomes_reportlab_markup",
        ),
        pytest.param(
            "| Owner | Due |\n| --- | --- |\n| Ada | Friday |",
            ["Owner", "Due", "Ada", "Friday"],
            [],
            id="table_cells_are_preserved",
        ),
        pytest.param(
            "1. first\n2. second\n   - nested\n",
            ["first", "second", "nested"],
            [],
            id="ordered_and_nested_lists_are_preserved",
        ),
    ],
)
def test_translation_is_faithful_and_inert(
    markdown: str, present: list[str], absent: list[str]
) -> None:
    text = _flowable_text(markdown)
    for needle in present:
        assert needle in text, needle
    for needle in absent:
        assert needle not in text, needle


def _wide_table_markdown(columns: int) -> str:
    header = "| " + " | ".join(f"H{n}" for n in range(1, columns + 1)) + " |"
    divider = "| " + " | ".join(["---"] * columns) + " |"
    row = "| " + " | ".join(f"cell-{n}" for n in range(1, columns + 1)) + " |"
    return "\n".join([header, divider, row, ""])


def test_wide_tables_keep_every_reviewed_cell() -> None:
    """A table wider than the grid ceiling must not lose reviewed content."""
    columns = pdf_export._MAX_TABLE_COLUMNS + 8
    text = _flowable_text(_wide_table_markdown(columns))
    for n in range(1, columns + 1):
        assert f"H{n}" in text, f"header {n} disappeared"
        assert f"cell-{n}" in text, f"cell {n} disappeared"


def test_wide_tables_still_render_to_pdf() -> None:
    body = pdf_export.render_markdown_pdf(_wide_table_markdown(30))
    assert body.startswith(b"%PDF-")


def test_tables_within_the_ceiling_stay_a_grid() -> None:
    """The stacked fallback only applies past the ceiling."""
    parser = pdf_export._FragmentParser()
    parser.feed(pdf_export.md_render.markdown_to_body_html(_wide_table_markdown(4)))
    parser.close()
    flow = pdf_export._blocks(parser.root, pdf_export._styles())
    assert any(isinstance(flowable, Table) for flowable in flow)


def test_unicode_is_preserved_or_refused_never_substituted() -> None:
    """Reviewed characters are either rendered or rejected, never replaced."""
    markdown = "Unicode: café — ✓ Привет Ωμέγα"
    if pdf_export.UNICODE_FONTS:
        text = _flowable_text(markdown)
        for needle in ("café", "✓", "Привет", "Ωμέγα"):
            assert needle in text, needle
        assert "?" not in text
    else:
        with pytest.raises(pdf_export.PdfFontCoverageError):
            pdf_export.render_markdown_pdf(markdown)


def test_missing_unicode_font_fails_closed_instead_of_rewriting_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With only the built-in CP1252 family, non-CP1252 text is refused."""
    monkeypatch.setattr(pdf_export, "_BODY_COVERAGE", None)
    monkeypatch.setattr(pdf_export, "_MONO_COVERAGE", None)
    with pytest.raises(pdf_export.PdfFontCoverageError):
        pdf_export.render_markdown_pdf("Checkmark ✓ and Привет")
    # CP1252 content is unaffected, so the fallback still exports normally.
    assert pdf_export.render_markdown_pdf("Plain café text").startswith(b"%PDF-")


def test_font_coverage_gap_is_refused_for_code_spans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Monospace runs are checked against the monospace face's own coverage."""
    monkeypatch.setattr(pdf_export, "_MONO_COVERAGE", frozenset(range(0x7F)))
    with pytest.raises(pdf_export.PdfFontCoverageError):
        pdf_export.render_markdown_pdf("Inline `Привет` code")
    with pytest.raises(pdf_export.PdfFontCoverageError):
        pdf_export.render_markdown_pdf("```\nПривет\n```\n")


def test_font_coverage_error_message_carries_no_reviewed_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pdf_export, "_BODY_COVERAGE", None)
    with pytest.raises(pdf_export.PdfFontCoverageError) as raised:
        pdf_export.render_markdown_pdf("Secret ✓ line")
    message = str(raised.value)
    assert "Secret" not in message
    assert "✓" not in message


def test_font_status_reports_what_the_process_resolved() -> None:
    status = pdf_export.unicode_font_status()
    assert status.unicode_fonts is pdf_export.UNICODE_FONTS
    assert status.body_font == ("SEBody" if pdf_export.UNICODE_FONTS else "Helvetica")
    assert status.searched_dirs == pdf_export._FONT_DIRS


def test_render_markdown_pdf_returns_a_pdf_document() -> None:
    body = pdf_export.render_markdown_pdf(_FIXTURE.read_text(encoding="utf-8"))
    assert body.startswith(b"%PDF-")
    assert body.rstrip().endswith(b"%%EOF")
    assert len(body) > 1000


@pytest.mark.parametrize(
    "markdown",
    [
        pytest.param("", id="empty_document"),
        pytest.param("# Only a heading", id="heading_only"),
        pytest.param("| a |\n| --- |\n" + "| x |\n" * 400, id="long_table"),
        pytest.param("\n".join(f"- item {n}" for n in range(2000)), id="long_list"),
        pytest.param("> quoted\n>\n> - inside\n", id="blockquote_with_list"),
        pytest.param("```\nraw <script>x</script>\n```\n", id="fenced_code_block"),
        pytest.param("---\n\nafter a rule\n", id="horizontal_rule"),
    ],
)
def test_bounded_documents_still_render(markdown: str) -> None:
    assert pdf_export.render_markdown_pdf(markdown).startswith(b"%PDF-")


def test_oversized_documents_are_refused_before_rendering() -> None:
    oversized = "a" * (pdf_export.MAX_MARKDOWN_BYTES + 1)
    with pytest.raises(pdf_export.PdfRenderError):
        pdf_export.render_markdown_pdf(oversized)


def test_element_count_is_bounded() -> None:
    with pytest.raises(pdf_export.PdfRenderError):
        pdf_export.render_markdown_pdf("\n\n".join(["x"] * (pdf_export._MAX_NODES + 10)))
