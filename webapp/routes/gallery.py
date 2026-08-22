"""Output Gallery routes — developer-facing preview of the reader's visual system.

The gallery renders committed *synthetic* fixtures through the same production
path a real output uses (`OutputService.render_markdown` → `reader.js`), so a
visual review of the gallery is a review of the real renderer, not of a second
one. It serves only files from `eval/fixtures/gallery/`, matched against an
explicit allowlist derived from that directory; no user-supplied path ever
reaches the filesystem.
"""
from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

import config

router = APIRouter()

GALLERY_DIR = (config.WEBAPP_DIR.parent / "eval" / "fixtures" / "gallery").resolve()

# Fixture names are single path segments of this shape and nothing else.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,60}$")


def _fixture_files() -> list[Path]:
    if not GALLERY_DIR.is_dir():
        return []
    return sorted(p for p in GALLERY_DIR.glob("*.md") if p.is_file() and _NAME_RE.match(p.stem))


def allowed_names() -> list[str]:
    """The explicit allowlist: every committed gallery fixture, by stem."""
    return [p.stem for p in _fixture_files()]


def _title_of(text: str, fallback: str) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


@router.get("/api/gallery/fixtures")
def api_gallery_fixtures() -> dict:
    items = []
    for path in _fixture_files():
        text = path.read_text(encoding="utf-8")
        items.append({
            "name": path.stem,
            "title": _title_of(text, path.stem),
            "lines": len(text.splitlines()),
        })
    return {"fixtures": items}


@router.get("/api/gallery/fixture", response_class=PlainTextResponse)
def api_gallery_fixture(name: str) -> str:
    if name not in allowed_names():
        raise HTTPException(status_code=404, detail="Unknown gallery fixture")
    return (GALLERY_DIR / f"{name}.md").read_text(encoding="utf-8")
