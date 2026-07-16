"""Output lifecycle service for the SE Skills webapp.

Owns output resolution, listing, reading, metadata, sidecars, reference freshness,
PDF/internal HTML export, delete, diff, golden promotion, push-to-repo handoff,
run persistence, and overview output summaries. This module intentionally stays
free of FastAPI request parsing and route formatting.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import urllib.parse
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import golden
import internal_html
import md_render
import output_schema
import pdf_render
import reference_freshness
import security

from .path_utils import resolve_within

logger = logging.getLogger(__name__)


class OutputError(Exception):
    """Domain exception carrying an HTTP-like status code and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class OutputService:
    """Cohesive output lifecycle service backed by the customer workspace."""

    def __init__(
        self,
        customers_dir: Path,
        workspace: Path,
        repo_root: Path,
        *,
        se_config: Callable[[], dict],
        safe_name: Callable[[str], str],
        slug: Callable[[str], str],
        run_cmd: Callable[..., Awaitable[tuple[int, str, str]]] | None = None,
        internal_repo: Callable[[], Path] | None = None,
    ) -> None:
        self.customers_dir = customers_dir
        self.workspace = workspace
        self.repo_root = repo_root
        self._se_config = se_config
        self._safe_name = safe_name
        self._slug = slug
        self._run_cmd = run_cmd
        self._internal_repo = internal_repo

    # -----------------------------------------------------------------------
    # Path safety
    # -----------------------------------------------------------------------
    def _resolve_output(self, path: str, customers_dir: Path | None = None) -> Path:
        """Resolve `path` under the customer workspace and confirm it stays inside.

        `path` is relative to `customers_dir`; absolute paths, `..` traversal, or
        symlinks that escape `customers_dir` are rejected. Returns the resolved
        `Path` if it is an existing file.
        """
        customers_dir = customers_dir or self.customers_dir
        try:
            target = resolve_within(customers_dir, path)
        except ValueError:
            raise OutputError(404, "Not found")
        if not target.is_file():
            raise OutputError(404, "Not found")
        return target

    def _feedback_file(self, md_path: Path) -> Path:
        """Sidecar JSONL path for a Markdown output's feedback history."""
        return md_path.with_suffix(".feedback.jsonl")

    # -----------------------------------------------------------------------
    # Listing and reading
    # -----------------------------------------------------------------------
    def list_outputs(
        self,
        account: str,
        opp: str | None = None,
        *,
        customers_dir: Path | None = None,
        config: dict | None = None,
    ) -> list[dict]:
        """Saved skill outputs, newest first, scoped to account and opportunity.

        Each Markdown output gets a `.json` sidecar with parsed metadata and
        validation results; the list exposes `valid` / `validation_errors` so the UI
        can warn the SE when an output looks incomplete.
        """
        customers_dir = customers_dir or self.customers_dir
        account = self._safe_name(account)
        if opp:
            opp = self._safe_name(opp)
        base = (
            (customers_dir / account / "opportunities" / opp / "outputs")
            if opp
            else (customers_dir / account / "outputs")
        )
        if not base.exists():
            return []

        cfg = config if config is not None else self._se_config()
        items: list[dict] = []
        for skill_dir in sorted(base.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            skill = skill_dir.name
            for f in sorted([*skill_dir.glob("*.md"), *skill_dir.glob("*.html")]):
                try:
                    rel = str(f.relative_to(customers_dir))
                    resolve_within(customers_dir, rel)
                    st = f.stat()
                except (OSError, ValueError):
                    continue
                entry: dict[str, Any] = {
                    "skill": skill,
                    "filename": f.name,
                    "path": rel,
                    "ext": f.suffix.lstrip("."),
                    "mtime": st.st_mtime,
                    "modified": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                    "size": st.st_size,
                }
                if f.suffix == ".md":
                    try:
                        meta = output_schema.read_or_parse_sidecar(f, skill)
                        if output_schema.skill_has_schema(skill):
                            current = reference_freshness.compute_reference_freshness(
                                cfg, self.workspace, self.repo_root, skill=skill
                            )
                            meta.reference_changed_since_generation = (
                                reference_freshness.compare_to_generation(
                                    current, meta.reference_freshness_at_generation
                                )
                            )
                        else:
                            meta.reference_changed_since_generation = []
                        entry["valid"] = meta.valid
                        entry["validation_status"] = meta.validation_status
                        entry["validation_errors"] = meta.validation_errors
                        entry["missing_sections"] = meta.missing_sections
                        entry["reference_freshness_at_generation"] = (
                            [r.model_dump() for r in meta.reference_freshness_at_generation]
                            if meta.reference_freshness_at_generation is not None
                            else None
                        )
                        entry["reference_changed_since_generation"] = (
                            [c.model_dump() for c in meta.reference_changed_since_generation]
                            if meta.reference_changed_since_generation is not None
                            else None
                        )
                    except (OSError, ValueError, TypeError):
                        logger.warning("Could not parse sidecar for %s", f)
                items.append(entry)
        items.sort(key=lambda x: x["mtime"], reverse=True)
        return items

    def count_outputs(
        self,
        account: str,
        opp: str | None = None,
        *,
        customers_dir: Path | None = None,
    ) -> int:
        """Count the saved skill outputs for an account/opportunity."""
        customers_dir = customers_dir or self.customers_dir
        account = self._safe_name(account)
        if opp:
            opp = self._safe_name(opp)
        base = (
            (customers_dir / account / "opportunities" / opp / "outputs")
            if opp
            else (customers_dir / account / "outputs")
        )
        if not base.exists():
            return 0

        count = 0
        for skill_dir in sorted(base.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            for f in sorted([*skill_dir.glob("*.md"), *skill_dir.glob("*.html")]):
                if any(part.startswith(".") for part in f.relative_to(skill_dir).parts):
                    continue
                try:
                    resolve_within(customers_dir, str(f.relative_to(customers_dir)))
                except ValueError:
                    continue
                count += 1
        return count

    def read_output_content(self, path: str, customers_dir: Path | None = None) -> str:
        target = self._resolve_output(path, customers_dir)
        return target.read_text()

    def read_output_meta(self, path: str, customers_dir: Path | None = None) -> dict:
        target = self._resolve_output(path, customers_dir)
        skill = target.parent.name
        try:
            meta = output_schema.read_or_parse_sidecar(target, skill)
            if output_schema.skill_has_schema(skill):
                cfg = self._se_config()
                current = reference_freshness.compute_reference_freshness(
                    cfg, self.workspace, self.repo_root, skill=skill
                )
                meta.reference_changed_since_generation = (
                    reference_freshness.compare_to_generation(
                        current, meta.reference_freshness_at_generation
                    )
                )
            else:
                meta.reference_changed_since_generation = []
            return meta.model_dump()
        except (OSError, ValueError, TypeError) as e:
            raise OutputError(500, f"Could not parse output metadata: {e}")

    def read_output_html(self, path: str, customers_dir: Path | None = None) -> str:
        target = self._resolve_output(path, customers_dir)
        if target.suffix != ".html":
            raise OutputError(400, "Not an HTML output")
        return target.read_text()

    def delete_output(self, path: str, customers_dir: Path | None = None) -> dict:
        target = self._resolve_output(path, customers_dir)
        if target.suffix != ".md":
            raise OutputError(400, "Only generated .md outputs can be deleted here")
        customers_dir = customers_dir or self.customers_dir
        root = customers_dir.resolve()
        trash = customers_dir / "_trash"
        trash.mkdir(exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        rel = target.relative_to(root)
        flat = str(rel).replace("/", "__")
        dest = trash / f"{stamp}__{flat}"
        shutil.move(str(target), str(dest))
        return {"path": path, "deleted": True, "trash_id": dest.name}

    # -----------------------------------------------------------------------
    # Markdown / PDF / internal HTML rendering
    # -----------------------------------------------------------------------
    @staticmethod
    def render_markdown(md: str) -> str:
        return md_render.markdown_to_body_html(md)

    def export_pdf(
        self,
        path: str,
        *,
        append_md: str | None = None,
        customers_dir: Path | None = None,
    ) -> tuple[bytes, str]:
        target = self._resolve_output(path, customers_dir)
        if target.suffix not in (".md", ".html"):
            raise OutputError(400, "Only .md or .html outputs can be exported to PDF")
        try:
            text = target.read_text()
            if append_md:
                text = text + append_md
            data = pdf_render.render_html_pdf(text) if target.suffix == ".html" else pdf_render.render_pdf(text)
        except RuntimeError as e:
            raise OutputError(503, security.redact_sensitive(str(e)))
        except subprocess.SubprocessError as e:
            raise OutputError(500, security.redact_sensitive(f"PDF render failed: {e}"))
        return data, target.stem + ".pdf"

    def export_internal_html(
        self,
        path: str,
        customers_dir: Path | None = None,
    ) -> tuple[str, str]:
        target = self._resolve_output(path, customers_dir)
        if target.suffix != ".md":
            raise OutputError(400, "Only .md outputs can be exported to internal HTML")
        customers_dir = customers_dir or self.customers_dir
        rel = target.relative_to(customers_dir.resolve())
        customer = rel.parts[0].replace("-", " ") if rel.parts else ""
        try:
            doc = internal_html.render_internal_html(target.read_text(), customer=customer)
        except Exception as e:  # best-effort render; surface rather than 500 silently
            raise OutputError(500, security.redact_sensitive(f"Internal HTML render failed: {e}"))
        return doc, target.stem + ".html"

    # -----------------------------------------------------------------------
    # Coverage handoff (push to internal.airbyte.ai)
    # -----------------------------------------------------------------------
    def repo_path(self, account: str, member: str = "") -> dict:
        account = self._safe_name(account)
        member_slug = self._slug(member).lower() if member else "<your-member-slug>"
        account_slug = self._slug(account.replace("-", " ")).lower()
        rel = f"accounts/{account_slug}/index.html"
        full = f"src/solutions-team/members/{member_slug}/{rel}"
        return {"relative": rel, "full": full, "account_slug": account_slug, "member_slug": member_slug}

    async def push_to_repo(self, body) -> dict:
        if self._internal_repo is None or self._run_cmd is None:
            raise OutputError(500, "Push-to-repo dependencies are not configured")

        # 1. Resolve + validate the source handoff HTML.
        src = self._resolve_output(body.path)
        if src.suffix != ".html":
            raise OutputError(400, "Only .html outputs can be pushed to the internal repo")
        handoff_html = src.read_text()

        # 2. Compute the target path inside the internal repo.
        repo = self._internal_repo()
        if not (repo / ".git").exists():
            raise OutputError(400, f"internal repo not cloned at {repo}")
        account = self._safe_name(body.account)
        member_slug = self._slug(body.member).lower() if body.member else ""
        if not member_slug:
            raise OutputError(400, "member (owner) is required to place the account page")
        account_slug = self._slug(account.replace("-", " ")).lower()
        member_dir = repo / "src" / "solutions-team" / "members" / member_slug
        handover_path = member_dir / "handover.html"
        if not handover_path.is_file():
            raise OutputError(400, f"No handover.html for member '{member_slug}' — expected {handover_path}")
        account_dir = member_dir / "accounts" / account_slug
        index_path = account_dir / "index.html"

        # 3. Sync clone: fetch origin/main and branch fresh off it.
        try:
            await self._run_cmd(["git", "fetch", "origin", "main"], repo)
        except Exception as e:
            raise OutputError(500, security.redact_sensitive(str(e)))
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        branch = f"se/coverage-handoff-{account_slug}-{ts}"
        try:
            await self._run_cmd(["git", "checkout", "-B", branch, "origin/main"], repo)
        except Exception as e:
            raise OutputError(500, security.redact_sensitive(str(e)))

        # 4. Write the account page.
        account_dir.mkdir(parents=True, exist_ok=True)
        index_path.write_text(handoff_html)

        # 5. Update the member's handover.html card.
        stats = self._parse_handoff_stats(handoff_html)
        meta = body.meta.strip() or self._build_card_meta(stats)
        description = body.description.strip()
        handover_path.write_text(
            self._upsert_handover_card(handover_path.read_text(), account, account_slug, description, meta)
        )

        # 6. Commit, push, open a PR.
        rel_index = str(index_path.relative_to(repo))
        rel_handover = str(handover_path.relative_to(repo))
        try:
            await self._run_cmd(["git", "add", rel_index, rel_handover], repo)
        except Exception as e:
            raise OutputError(500, security.redact_sensitive(str(e)))
        commit_msg = (
            f"docs(solutions-team): add {account} coverage handoff ({account_slug})\n\n"
            "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
        )
        try:
            await self._run_cmd(["git", "commit", "-m", commit_msg], repo)
            await self._run_cmd(["git", "push", "-u", "origin", branch], repo)
        except Exception as e:
            raise OutputError(500, security.redact_sensitive(str(e)))
        pr_body = (
            f"Auto-generated coverage handoff for **{account}**, added under "
            f"`members/{member_slug}/accounts/{account_slug}/` with a card in "
            f"`{member_slug}/handover.html`.\n\n"
            "Generated from the SE-skills webapp coverage-handoff skill.\n\n"
            "🤖 Generated with [Claude Code](https://claude.com/claude-code)"
        )
        try:
            _, pr_out, _ = await self._run_cmd([
                "gh", "pr", "create", "--repo", "airbytehq/internal.airbyte.ai",
                "--head", branch, "--base", "main",
                "--title", f"docs(solutions-team): add {account} coverage handoff",
                "--body", pr_body,
            ], repo)
        except Exception as e:
            raise OutputError(500, security.redact_sensitive(str(e)))
        pr_url = pr_out.strip().splitlines()[-1] if pr_out.strip() else ""
        return {"pr_url": pr_url, "branch": branch, "target": rel_index}

    async def push_status(self, account: str) -> dict:
        if self._internal_repo is None:
            return {"open_pr": None}
        repo = self._internal_repo()
        if not (repo / ".git").exists():
            return {"open_pr": None}
        try:
            account_slug = self._slug(self._safe_name(account).replace("-", " ")).lower()
        except Exception:
            return {"open_pr": None}
        branch_base = f"se/coverage-handoff-{account_slug}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "pr", "list", "--repo", "airbytehq/internal.airbyte.ai",
                "--state", "open", "--search", f"head:{branch_base}",
                "--json", "number,url,headRefName", "--limit", "20",
                cwd=str(repo),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=25)
            if proc.returncode != 0:
                return {"open_pr": None}
            prs = json.loads(out or "[]")
        except Exception:
            return {"open_pr": None}
        ts_suffix = re.compile(re.escape(branch_base) + r"-\d{8}-\d{6}$")
        for pr in prs:
            ref = pr.get("headRefName") or ""
            if ref == branch_base or ts_suffix.match(ref):
                return {"open_pr": {"number": pr.get("number"), "url": pr.get("url")}}
        return {"open_pr": None}

    @staticmethod
    def _html_escape(s: str) -> str:
        """Escape text destined for HTML card markup."""
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    @staticmethod
    def _parse_handoff_stats(html: str) -> dict:
        """Best-effort extraction of card facts from a coverage-handoff page."""
        stats: dict[str, str] = {}
        for num, label in re.findall(
            r'<div class="stat">\s*<div class="num">(.*?)</div>\s*<div class="label">(.*?)</div>',
            html, re.DOTALL,
        ):
            stats[label.strip().lower()] = num.strip()
        for label, value in re.findall(
            r'<div class="cv">\s*<span class="label">(.*?)</span>\s*<span class="value">(.*?)</span>',
            html, re.DOTALL,
        ):
            stats["cv:" + label.strip().lower()] = value.strip()
        return stats

    @staticmethod
    def _build_card_meta(stats: dict) -> str:
        """Assemble the card meta line from parsed stats."""
        parts = []
        if stats.get("deal size"):
            parts.append(stats["deal size"])
        if stats.get("stage"):
            parts.append(f"Stage {stats['stage']}")
        cv = stats.get("cv:coverage window")
        if cv:
            parts.append(f"Coverage {cv}")
        return " &middot; ".join(parts)

    def _upsert_handover_card(self, handover_html: str, account: str, account_slug: str,
                              description: str, meta: str) -> str:
        """Insert or replace the account's nav-card in a member's handover.html."""
        acct_e = self._html_escape(account)
        desc_e = self._html_escape(description) or f"Coverage handoff for {acct_e}."
        meta_e = self._html_escape(meta) if meta else ""
        meta_inner = '<span class="badge">Active</span>' + (f" &nbsp; {meta_e}" if meta_e else "")
        card = (
            f'    <a href="accounts/{account_slug}/" class="nav-card">\n'
            f'      <div class="accent-bar"></div>\n'
            f'      <span class="arrow">&rarr;</span>\n'
            f'      <h2>{acct_e}</h2>\n'
            f'      <p>{desc_e}</p>\n'
            f'      <div class="meta">{meta_inner}</div>\n'
            f'    </a>'
        )
        existing = re.compile(
            r'[ \t]*<a href="accounts/' + re.escape(account_slug) + r'/"[^>]*>.*?</a>',
            re.DOTALL,
        )
        if existing.search(handover_html):
            return existing.sub(card, handover_html, count=1)
        grid = re.compile(r'(<div class="nav-grid">[^\n]*\n)')
        if not grid.search(handover_html):
            raise OutputError(500, 'Could not find `<div class="nav-grid">` in handover.html')
        return grid.sub(lambda m: m.group(1) + card + "\n", handover_html, count=1)

    # -----------------------------------------------------------------------
    # Golden fixtures
    # -----------------------------------------------------------------------
    def golden_manifests(self, skill: str) -> dict:
        """Return the Phase 1 manifest scenarios that exercise a skill."""
        return {"skill": skill, "scenarios": golden.manifest_scenarios(skill)}

    def promote_to_golden(self, body, customers_dir: Path | None = None, repo_root: Path | None = None) -> dict:
        target = self._resolve_output(body.path, customers_dir)
        if target.suffix != ".md":
            raise OutputError(404, "Not found")
        if not body.confirm_synthetic:
            raise OutputError(
                400,
                "Confirm that this content is synthetic and contains no customer or confidential data.",
            )

        meta = output_schema.read_or_parse_sidecar(target, target.parent.name)
        skill = meta.skill or target.parent.name

        scenario = (body.scenario or target.stem).strip()
        if not scenario:
            scenario = target.stem
        scenario = re.sub(r"[^\w\-]+", "_", scenario).strip("_").lower()[:100] or target.stem

        active_scenarios = golden.manifest_scenarios(skill)
        if scenario not in active_scenarios:
            raise OutputError(
                400,
                f"Scenario '{scenario}' is not exercised by a Phase 1 manifest for skill '{skill}'. "
                f"Choose one of: {', '.join(active_scenarios) or 'none'}.",
            )

        text = body.text if body.text is not None else target.read_text(encoding="utf-8")
        golden_path = golden.save_golden(skill, scenario, text)
        repo_root = repo_root or self.repo_root
        try:
            golden_rel = str(golden_path.relative_to(repo_root))
        except ValueError:
            golden_rel = str(golden_path)
        return {
            "path": body.path,
            "skill": skill,
            "scenario": scenario,
            "golden_path": golden_rel,
            "active": True,
        }

    # -----------------------------------------------------------------------
    # Diff
    # -----------------------------------------------------------------------
    def diff_outputs(self, body, customers_dir: Path | None = None) -> dict:
        customers_dir = customers_dir or self.customers_dir
        try:
            target_left = resolve_within(customers_dir, body.left)
            target_right = resolve_within(customers_dir, body.right)
        except ValueError:
            raise OutputError(404, "Not found")
        if not target_left.is_file() or not target_right.is_file():
            raise OutputError(404, "Not found")
        if target_left.suffix != ".md" or target_right.suffix != ".md":
            raise OutputError(400, "Only generated .md outputs can be diffed")
        left_text = target_left.read_text(encoding="utf-8")
        right_text = target_right.read_text(encoding="utf-8")

        semantic = None
        try:
            left_meta = output_schema.read_or_parse_sidecar(target_left, target_left.parent.name)
            right_meta = output_schema.read_or_parse_sidecar(target_right, target_right.parent.name)
            semantic = output_schema.semantic_diff(left_meta, right_meta)
        except (OSError, ValueError, TypeError) as e:
            logger.warning("Could not build semantic diff for %s / %s: %s", body.left, body.right, e)

        return {
            "left": body.left,
            "right": body.right,
            "left_title": target_left.name,
            "right_title": target_right.name,
            "semantic": semantic,
            "rows": self.diff_lines(left_text, right_text),
        }

    @staticmethod
    def diff_lines(left_text: str, right_text: str) -> list[dict]:
        """Return a side-by-side line diff of two Markdown texts."""
        from itertools import zip_longest
        import difflib

        left_lines = left_text.splitlines()
        right_lines = right_text.splitlines()
        sm = difflib.SequenceMatcher(None, left_lines, right_lines)
        rows: list[dict] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for a, b in zip_longest(left_lines[i1:i2], right_lines[j1:j2]):
                    rows.append({"left": a, "right": b, "type": "equal"})
            elif tag == "delete":
                for line in left_lines[i1:i2]:
                    rows.append({"left": line, "right": None, "type": "delete"})
            elif tag == "insert":
                for line in right_lines[j1:j2]:
                    rows.append({"left": None, "right": line, "type": "insert"})
            elif tag == "replace":
                for a, b in zip_longest(left_lines[i1:i2], right_lines[j1:j2]):
                    rows.append({"left": a, "right": b, "type": "replace"})
        return rows

    # -----------------------------------------------------------------------
    # Run persistence (used by JobService)
    # -----------------------------------------------------------------------
    def _runs_dir(self, account: str, opp_slug: str | None) -> Path | None:
        if not opp_slug:
            return None
        return self.customers_dir / account / "opportunities" / opp_slug / "outputs" / ".runs"

    def _output_dir(self, account: str, opp_slug: str | None, skill: str) -> Path | None:
        if not opp_slug:
            return None
        return self.customers_dir / account / "opportunities" / opp_slug / "outputs" / skill

    @staticmethod
    def _sidecar_for_md(md_path: Path) -> Path:
        """Sidecar JSON path for a Markdown output file."""
        return md_path.with_suffix(md_path.suffix + ".json")

    def _write_output_sidecar(self, account: str, opp_slug: str | None, skill: str) -> None:
        """Parse the most recent Markdown output for this run and write its `.json` sidecar."""
        out_dir = self._output_dir(account, opp_slug, skill)
        if not out_dir or not out_dir.exists():
            return
        md_files = [f for f in out_dir.glob("*.md") if f.is_file()]
        if not md_files:
            return
        newest = max(md_files, key=lambda p: p.stat().st_mtime)
        try:
            resolve_within(self.customers_dir, str(newest.relative_to(self.customers_dir)))
            text = newest.read_text(encoding="utf-8")
        except (OSError, ValueError):
            logger.warning("Could not read output %s for sidecar", newest)
            return
        metadata = output_schema.parse_output(skill, text)
        try:
            metadata.reference_freshness_at_generation = (
                reference_freshness.compute_reference_freshness(
                    self._se_config(), self.workspace, self.repo_root, skill=skill
                )
            )
            metadata.reference_changed_since_generation = []
        except Exception:
            logger.exception("Could not compute reference freshness for %s", newest)
            metadata.reference_freshness_at_generation = []
            metadata.reference_changed_since_generation = []
        try:
            output_schema.write_sidecar(newest, metadata)
        except OSError:
            logger.warning("Could not write sidecar for %s", newest)

    def persist_run(self, account: str, opp_slug: str | None, skill: str, record: dict[str, Any]) -> None:
        """Persist the finished run record and write the output sidecar."""
        d = self._runs_dir(account, opp_slug)
        if not d:
            return
        d.mkdir(parents=True, exist_ok=True)
        safe_skill = re.sub(r"[^A-Za-z0-9._-]", "-", skill or "freeform")
        (d / f"{safe_skill}.json").write_text(json.dumps(record))
        self._write_output_sidecar(account, opp_slug, skill)

    def latest_run(self, account: str, opp_slug: str | None) -> dict | None:
        """The most recently finished run for this opportunity, read from disk."""
        d = self._runs_dir(account, opp_slug)
        if not d or not d.exists():
            return None
        best = None
        for f in d.glob("*.json"):
            try:
                resolve_within(self.customers_dir, str(f.relative_to(self.customers_dir)))
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            except Exception:
                continue
            if best is None or (rec.get("finished_at", 0) > best.get("finished_at", 0)):
                best = rec
        return best

    # -----------------------------------------------------------------------
    # Overview aggregation helpers
    # -----------------------------------------------------------------------
    def output_validation_status(self, sidecar_path: Path) -> tuple[bool, str]:
        """Read an output's `.md.json` sidecar and decide whether it needs attention."""
        if not sidecar_path.exists():
            return False, "unknown"
        try:
            sc = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False, "unknown"
        if not isinstance(sc, dict):
            return False, "unknown"
        valid = sc.get("valid")
        validation_status = sc.get("validation_status")
        ref_fresh = sc.get("reference_freshness_at_generation") or []
        stale = any(not r.get("fresh", True) for r in ref_fresh if isinstance(r, dict))
        if valid is False or validation_status == "invalid":
            return True, "invalid"
        if stale:
            return True, "stale"
        if valid is not True:
            return True, "incomplete"
        if validation_status == "unvalidated":
            return False, "unvalidated"
        return False, "valid"

    def output_review_status(self, feedback_path: Path) -> tuple[bool, str]:
        """Read an output's `.feedback.jsonl` sidecar and decide review status."""
        if not feedback_path.exists():
            return True, "awaiting review"
        try:
            lines = feedback_path.read_text(encoding="utf-8").splitlines()
        except (OSError, ValueError, TypeError):
            return True, "awaiting review"
        entries = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if isinstance(entry, dict) and entry.get("action") in ("approve", "comment", "correct"):
                    entries.append(entry)
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        if not entries:
            return True, "awaiting review"
        latest = entries[-1]
        action = latest.get("action")
        if action == "approve":
            return False, "approved"
        if action == "comment":
            return True, "commented"
        if action == "correct":
            return True, "corrected"
        return True, "awaiting review"

    def output_href(self, account: str, opp_slug: str | None, opp_name: str, path: str) -> str:
        """Build a hash-router output-reader link with safe URL encoding."""
        enc = urllib.parse.quote
        slug = opp_slug or ""
        name = opp_name or (opp_slug.capitalize() if opp_slug else account)
        return f"#/output/{enc(account)}/{enc(slug)}/{enc(name)}/{enc(path)}"

    def collect_output(
        self,
        account: str,
        opp_slug: str | None,
        skill: str,
        f: Path,
        meta: dict,
        recent_outputs: list,
        needs_attention_outputs: list,
        customers_dir: Path | None = None,
    ) -> None:
        """Update account metadata and global output lists for one output file."""
        customers_dir = customers_dir or self.customers_dir
        try:
            rel = str(f.relative_to(customers_dir))
            resolve_within(customers_dir, rel)
            st = f.stat()
        except (OSError, ValueError):
            return
        mtime = st.st_mtime
        meta["output_count"] += 1
        if mtime > meta["last_updated_ts"]:
            meta["last_updated_ts"] = mtime
            meta["last_output"] = {
                "path": str(f.relative_to(customers_dir)),
                "mtime": mtime,
                "modified": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "skill": skill,
                "filename": f.name,
                "account": account,
                "opp_slug": opp_slug,
                "opp_name": opp_slug.capitalize() if opp_slug else account,
            }
        sidecar = f.with_suffix(f.suffix + ".json")
        feedback = self._feedback_file(f)
        needs_attention, validation_status = self.output_validation_status(sidecar)
        awaiting_review, review_status = self.output_review_status(feedback)

        attention_type = None
        attention_status = None
        if needs_attention:
            attention_type = "attention"
            attention_status = validation_status
            if awaiting_review:
                attention_status = f"{validation_status} · {review_status}"
        elif awaiting_review:
            attention_type = "review"
            attention_status = review_status

        if attention_type:
            meta["needs_attention"] += 1
            needs_attention_outputs.append({
                "path": str(f.relative_to(customers_dir)),
                "mtime": mtime,
                "skill": skill,
                "filename": f.name,
                "account": account,
                "opp_slug": opp_slug,
                "opp_name": opp_slug.capitalize() if opp_slug else account,
                "type": attention_type,
                "status": attention_status,
                "validation_status": validation_status,
                "review_status": review_status,
            })
        recent_outputs.append({
            "path": str(f.relative_to(customers_dir)),
            "mtime": mtime,
            "modified": datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "skill": skill,
            "filename": f.name,
            "account": account,
            "opp_slug": opp_slug,
            "opp_name": opp_slug.capitalize() if opp_slug else account,
            "needs_attention": bool(needs_attention or awaiting_review),
            "validation_status": validation_status,
            "review_status": review_status,
        })

    def walk_account_outputs(
        self,
        account_dir: Path,
        account: str,
        meta: dict,
        recent_outputs: list,
        needs_attention_outputs: list,
        customers_dir: Path | None = None,
    ) -> None:
        """Traverse one account's output directories and populate metadata."""
        try:
            base = account_dir / "outputs"
            if base.exists():
                for skill_dir in sorted(base.iterdir()):
                    try:
                        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                            continue
                        for f in sorted([*skill_dir.glob("*.md"), *skill_dir.glob("*.html")]):
                            if any(part.startswith(".") for part in f.relative_to(skill_dir).parts):
                                continue
                            self.collect_output(account, None, skill_dir.name, f, meta, recent_outputs, needs_attention_outputs, customers_dir)
                    except (OSError, ValueError, TypeError):
                        continue

            opp_root = account_dir / "opportunities"
            if opp_root.exists():
                for opp_dir in sorted(opp_root.iterdir()):
                    try:
                        if not opp_dir.is_dir() or opp_dir.name.startswith("."):
                            continue
                        base = opp_dir / "outputs"
                        if not base.exists():
                            continue
                        for skill_dir in sorted(base.iterdir()):
                            try:
                                if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                                    continue
                                for f in sorted([*skill_dir.glob("*.md"), *skill_dir.glob("*.html")]):
                                    if any(part.startswith(".") for part in f.relative_to(skill_dir).parts):
                                        continue
                                    self.collect_output(account, opp_dir.name, skill_dir.name, f, meta, recent_outputs, needs_attention_outputs, customers_dir)
                                    meta["opp_slugs"].add(opp_dir.name)
                            except (OSError, ValueError, TypeError):
                                continue
                    except (OSError, ValueError, TypeError):
                        continue
        except (OSError, ValueError, TypeError):
            return

    def walk_all_outputs(
        self,
        customers_dir: Path | None = None,
    ) -> tuple[list[dict], list[dict], dict[str, dict]]:
        """Walk the entire customer workspace and return raw output lists and per-account metadata."""
        customers_dir = customers_dir or self.customers_dir
        account_meta: dict[str, dict] = {}
        recent_outputs: list[dict] = []
        needs_attention_outputs: list[dict] = []

        if customers_dir.exists():
            for account_dir in sorted(customers_dir.iterdir()):
                try:
                    if not account_dir.is_dir() or account_dir.name.startswith("_") or account_dir.name.startswith("."):
                        continue
                    account = account_dir.name
                    meta: dict[str, Any] = {
                        "output_count": 0,
                        "last_updated_ts": 0.0,
                        "last_output": None,
                        "needs_attention": 0,
                        "opp_count": 0,
                        "opp_slugs": set(),
                    }
                    self.walk_account_outputs(account_dir, account, meta, recent_outputs, needs_attention_outputs, customers_dir)
                    meta["opp_count"] = len(meta["opp_slugs"])
                    account_meta[account] = meta
                except (OSError, ValueError, TypeError):
                    continue

        return recent_outputs, needs_attention_outputs, account_meta
