#!/usr/bin/env python3
"""Prove the hosted export path runs on the `hosted` extra alone.

CI installs the `dev` extra, which happens to carry every hosted runtime
dependency, so it cannot tell whether a hosted deployment would have them. This
runs under an environment built from `--extra hosted` with no dev packages: it
imports the hosted review/render/export modules, renders Markdown to sanitized
HTML, renders a PDF in-process, and asserts the dev-only test dependencies are
absent so the check cannot silently pass in the wrong environment.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "webapp"))

import md_render  # noqa: E402 - importable only once `webapp` is on the path
from hosted import exports, pdf_export  # noqa: E402

DEV_ONLY = ("pytest", "testcontainers", "numpy")

MARKDOWN = """# Call Summary: Northwind

## Key Takeaways

- Unicode holds: café ✓ Привет
- `inline code` and a [link](https://example.com/docs)

<script>alert('xss')</script>

| Column A | Column B |
| --- | --- |
| one | two |

## Source Coverage

Read northwind.txt in full (10 / 10 lines).
"""


def main() -> int:
    present = [name for name in DEV_ONLY if importlib.util.find_spec(name) is not None]
    if present:
        print(f"FAIL: dev-only packages present, this is not a hosted-only env: {present}")
        return 1

    body_html = md_render.markdown_to_body_html(MARKDOWN)
    for hostile in ("<script", "onerror", "javascript:"):
        if hostile in body_html:
            print(f"FAIL: sanitizer let {hostile!r} through")
            return 1
    if "<table" not in body_html or "café" not in body_html:
        print("FAIL: rendered HTML lost table or Unicode content")
        return 1

    pdf = pdf_export.render_markdown_pdf(MARKDOWN)
    if not pdf.startswith(b"%PDF-"):
        print("FAIL: renderer did not return a PDF")
        return 1

    font = pdf_export.unicode_font_status()
    if not font.unicode_fonts:
        print(
            "FAIL: no Unicode font family found in "
            f"{font.searched_dirs}; install fonts-dejavu-core so reviewed text "
            "renders instead of being refused"
        )
        return 1

    if not hasattr(exports, "create_output_export"):
        print("FAIL: hosted export route did not import")
        return 1

    print(
        f"OK: hosted extra renders {len(pdf)} PDF bytes with "
        f"{font.body_font}/{font.mono_font}, sanitizer and export route import clean"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
