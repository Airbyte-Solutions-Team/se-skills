"""Feedback lifecycle service for the SE Skills webapp.

Owns reading and appending output review/approval/correction entries stored in
sidecar JSONL files next to generated Markdown outputs.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class OutputFeedback(BaseModel):
    path: str = Field(max_length=500)
    action: Literal["approve", "comment", "correct"]
    comment: str = Field(default="", max_length=2_000)
    author: str = Field(default="", max_length=100)


class FeedbackError(Exception):
    """Domain exception carrying an HTTP-like status code and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class FeedbackService:
    """Read/append review feedback for generated output files."""

    def __init__(self, customers_dir: Path) -> None:
        self.customers_dir = customers_dir

    def _feedback_file(self, md_path: Path) -> Path:
        """Sidecar JSONL path for a Markdown output's feedback history."""
        return md_path.with_suffix(".feedback.jsonl")

    def _resolve_output(self, path: str) -> Path:
        """Resolve `path` under the customer workspace and confirm it stays inside."""
        target = (self.customers_dir / path).resolve()
        if not str(target).startswith(str(self.customers_dir.resolve())) or not target.is_file():
            raise FeedbackError(404, "Not found")
        if target.suffix != ".md":
            raise FeedbackError(400, "Feedback is only supported for .md outputs")
        return target

    def read_feedback(self, path: str) -> dict:
        """Return all parsed feedback entries for an output, newest last."""
        target = self._resolve_output(path)
        fb = self._feedback_file(target)
        entries: list[dict] = []
        if fb.exists():
            try:
                for line in fb.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict) and entry.get("action") in (
                            "approve",
                            "comment",
                            "correct",
                        ):
                            entries.append(entry)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        continue
            except (OSError, ValueError, TypeError):
                pass
        return {"path": path, "entries": entries}

    def add_feedback(self, body: OutputFeedback) -> dict:
        """Append a new feedback entry to the output's JSONL sidecar."""
        target = self._resolve_output(body.path)
        fb = self._feedback_file(target)
        fb.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "action": body.action,
            "comment": body.comment.strip(),
            "author": body.author.strip(),
        }
        try:
            with fb.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except (OSError, ValueError, TypeError) as e:
            raise FeedbackError(500, f"Could not save feedback: {e}")
        return {
            "path": body.path,
            "entry": entry,
            "feedback_file": str(fb.relative_to(self.customers_dir)),
        }
