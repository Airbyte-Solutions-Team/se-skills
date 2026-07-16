"""Member, account, and opportunity domain service for the SE Skills webapp.

Owns team loading, account discovery, account CRUD, opportunity listing,
output/job summary coordination, and safe customer-directory traversal. It
reads job and output state through the dedicated services and never mutates
them.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import HTTPException

from services.job_service import JobService
from services.output_service import OutputService
from services.path_utils import resolve_within

logger = logging.getLogger(__name__)


class AccountError(Exception):
    """Domain exception carrying an HTTP-like status code and detail."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class AccountService:
    """Cohesive account/member/opportunity service backed by the workspace."""

    def __init__(
        self,
        customers_dir: Path,
        webapp_dir: Path,
        output_service: OutputService,
        job_service: JobService,
        *,
        safe_name: Callable[[str], str],
        titlecase: Callable[[str], str],
        slug: Callable[[str], str],
        team_file: Path | None = None,
        member_prefs_dir: Path | None = None,
        se_config_file: Path | None = None,
        sfdc_opportunities: Callable[[str], Awaitable[list[dict]]] | None = None,
    ) -> None:
        self.customers_dir = Path(customers_dir)
        self.webapp_dir = Path(webapp_dir)
        self._output_service = output_service
        self._job_service = job_service
        self._safe_name = safe_name
        self._titlecase = titlecase
        self._slug = slug
        self.team_file = team_file or (self.webapp_dir / "team-members.yaml")
        self.member_prefs_dir = member_prefs_dir or (self.webapp_dir / ".member-prefs")
        self.se_config_file = se_config_file or (Path.home() / "airbyte-work" / ".se-config.yaml")
        self._sfdc_opportunities = sfdc_opportunities

    # -----------------------------------------------------------------------
    # Name/slug helpers
    # -----------------------------------------------------------------------
    def _safe(self, name: str) -> str:
        try:
            return self._safe_name(name)
        except HTTPException as exc:
            raise AccountError(exc.status_code, exc.detail) from exc

    def safe_name(self, name: str) -> str:
        """Validate and return a filesystem-safe name."""
        return self._safe(name)

    def titlecase(self, name: str) -> str:
        """Convert a display name to a Title-Case-Hyphenated folder name."""
        return self._titlecase(name)

    def slug(self, name: str) -> str:
        """Return a filesystem-safe slug for an opportunity name."""
        return self._slug(name)

    # -----------------------------------------------------------------------
    # Path safety
    # -----------------------------------------------------------------------
    def _resolve_account_dir(self, account: str, must_exist: bool = True) -> Path:
        account = self._safe(account)
        try:
            acc_dir = resolve_within(self.customers_dir, account)
        except ValueError as exc:
            raise AccountError(400, f"Invalid account: {account}") from exc
        if must_exist and not acc_dir.is_dir():
            raise AccountError(404, "Unknown account")
        return acc_dir

    def _resolve_opportunity_dir(self, account: str, opp: str, must_exist: bool = True) -> Path:
        account = self._safe(account)
        opp = self._safe(opp)
        try:
            opp_dir = resolve_within(self.customers_dir, f"{account}/opportunities/{opp}")
        except ValueError as exc:
            raise AccountError(400, f"Invalid opportunity path: {opp}") from exc
        if must_exist and not opp_dir.is_dir():
            raise AccountError(404, "Unknown opportunity")
        return opp_dir

    # -----------------------------------------------------------------------
    # Team members
    # -----------------------------------------------------------------------
    def load_team(self) -> list[dict]:
        """Configured SE team from team-members.yaml, falling back to .se-config.yaml."""
        if self.team_file.exists():
            data = yaml.safe_load(self.team_file.read_text()) or {}
            members = data.get("members", [])
            if members:
                return members
        if self.se_config_file.exists():
            cfg = yaml.safe_load(self.se_config_file.read_text()) or {}
            return [{"id": "me", "name": cfg.get("name", "Me"), "email": cfg.get("email", "")}]
        return [{"id": "me", "name": "Me", "email": ""}]

    def save_team(self, members: list[dict]) -> None:
        self.team_file.write_text(yaml.safe_dump({"members": members}, sort_keys=False, allow_unicode=True))

    def member_by_id(self, member_id: str) -> dict | None:
        return next((m for m in self.load_team() if m.get("id") == member_id), None)

    def _member_id_from_name(self, name: str, existing_ids: set[str]) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "member"
        mid, n = base, 2
        while mid in existing_ids:
            mid = f"{base}-{n}"
            n += 1
        return mid

    def create_member(self, name: str, role: str | None = None, email: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise AccountError(400, "Name is required")
        members = self.load_team()
        existing = {m.get("id") for m in members}
        mid = self._member_id_from_name(name, existing)
        member = {
            "id": mid,
            "name": name,
            "email": (email or "").strip(),
            "role": (role or "Solutions Engineer").strip(),
        }
        members.append(member)
        self.save_team(members)
        return member

    # -----------------------------------------------------------------------
    # Member preferences
    # -----------------------------------------------------------------------
    def _member_prefs_file(self, member_id: str) -> Path:
        return self.member_prefs_dir / f"{self._safe(member_id)}.json"

    def read_member_prefs(self, member_id: str) -> dict:
        f = self._member_prefs_file(member_id)
        if not f.exists():
            return {}
        try:
            return json.loads(f.read_text()) or {}
        except (json.JSONDecodeError, OSError):
            return {}

    def save_member_prefs(self, member_id: str, prefs: dict) -> None:
        f = self._member_prefs_file(member_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(prefs, indent=2))

    # -----------------------------------------------------------------------
    # Account metadata files
    # -----------------------------------------------------------------------
    def _owner_file(self, account_dir: Path) -> Path:
        return account_dir / ".owner"

    def _read_owner(self, account_dir: Path) -> str | None:
        f = self._owner_file(account_dir)
        return f.read_text().strip() if f.exists() else None

    def _archived_file(self, account_dir: Path) -> Path:
        return account_dir / ".archived"

    def _is_archived(self, account_dir: Path) -> bool:
        return self._archived_file(account_dir).exists()

    # -----------------------------------------------------------------------
    # Account discovery
    # -----------------------------------------------------------------------
    def list_accounts(self) -> list[dict]:
        """All customer folders, skipping hidden/internal directories."""
        if not self.customers_dir.exists():
            return []
        accounts = []
        for d in sorted(self.customers_dir.iterdir()):
            if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
                continue
            try:
                acc_dir = resolve_within(self.customers_dir, d.name)
            except ValueError:
                continue
            if not acc_dir.is_dir():
                continue
            accounts.append({
                "name": d.name,
                "owner": self._read_owner(acc_dir),
                "archived": self._is_archived(acc_dir),
            })
        return accounts

    def _account_meta(self, account: str) -> dict:
        """Lightweight filesystem metadata for an account card."""
        try:
            acc_dir = self._resolve_account_dir(account)
        except AccountError:
            return {"output_count": 0, "last_updated": None, "last_updated_ts": None}

        meta: dict[str, Any] = {
            "output_count": 0,
            "last_updated_ts": 0.0,
            "last_output": None,
            "needs_attention": 0,
            "opp_count": 0,
            "opp_slugs": set(),
        }
        recent: list[dict] = []
        attention: list[dict] = []
        self._output_service.walk_account_outputs(acc_dir, account, meta, recent, attention, self.customers_dir)

        return {
            "output_count": meta["output_count"],
            "last_updated": (
                datetime.fromtimestamp(meta["last_updated_ts"], tz=timezone.utc).strftime("%Y-%m-%d")
                if meta["last_updated_ts"]
                else None
            ),
            "last_updated_ts": meta["last_updated_ts"] or None,
        }

    def member_accounts(self, member_id: str) -> dict:
        """Accounts visible to this member, split active/archived and enriched."""
        if not self.member_by_id(member_id):
            raise AccountError(404, "Unknown member")

        all_accounts = self.list_accounts()
        visible = [a for a in all_accounts if a["owner"] == member_id or a["owner"] is None]
        for a in visible:
            a.update(self._account_meta(a["name"]))
        visible.sort(key=lambda a: (a["owner"] != member_id, -(a["last_updated_ts"] or 0), a["name"].lower()))

        return {
            "active": [a for a in visible if not a["archived"]],
            "archived": [a for a in visible if a["archived"]],
        }

    def get_account(self, account: str) -> dict:
        acc_dir = self._resolve_account_dir(account)
        return {"name": acc_dir.name, "owner": self._read_owner(acc_dir)}

    # -----------------------------------------------------------------------
    # Account mutations
    # -----------------------------------------------------------------------
    def create_account(self, name: str, owner: str | None = None, sfdc_name: str | None = None) -> dict:
        folder = self.titlecase(self._safe(name))
        if not folder:
            raise AccountError(400, "Empty account name")

        acc_dir = self._resolve_account_dir(folder, must_exist=False)
        created = not acc_dir.exists()
        (acc_dir / "outputs").mkdir(parents=True, exist_ok=True)
        (acc_dir / "raw").mkdir(parents=True, exist_ok=True)
        if owner:
            self._owner_file(acc_dir).write_text(self._safe(owner))
        if sfdc_name and sfdc_name.strip():
            (acc_dir / ".sfdc-name").write_text(sfdc_name.strip())
        return {"name": folder, "created": created, "owner": owner}

    def set_owner(self, account: str, owner: str) -> dict:
        acc_dir = self._resolve_account_dir(account)
        self._owner_file(acc_dir).write_text(self._safe(owner))
        return {"name": acc_dir.name, "owner": owner}

    def archive(self, account: str) -> dict:
        acc_dir = self._resolve_account_dir(account)
        self._archived_file(acc_dir).write_text(
            datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        return {"name": acc_dir.name, "archived": True}

    def unarchive(self, account: str) -> dict:
        acc_dir = self._resolve_account_dir(account)
        f = self._archived_file(acc_dir)
        if f.exists():
            f.unlink()
        return {"name": acc_dir.name, "archived": False}

    def delete_account(self, account: str) -> dict:
        acc_dir = self._resolve_account_dir(account)
        trash = self.customers_dir / "_trash"
        trash.mkdir(exist_ok=True)
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest = trash / f"{acc_dir.name}__{stamp}"
        shutil.move(str(acc_dir), str(dest))
        return {"name": acc_dir.name, "deleted": True, "trash_id": dest.name}

    def bulk_action(self, action: str, accounts: list[str], owner: str | None = None) -> dict:
        if action not in ("archive", "unarchive", "delete", "set-owner"):
            raise AccountError(400, f"Unknown bulk action: {action}")
        if action == "set-owner" and not owner:
            raise AccountError(400, "owner required for set-owner")

        names = [self._safe(n) for n in accounts if isinstance(n, str)]
        if not names:
            raise AccountError(400, "No accounts given")

        results = []
        for name in names:
            try:
                acc_dir = self._resolve_account_dir(name)
            except AccountError as e:
                results.append({"name": name, "ok": False, "error": "not found"})
                continue

            try:
                if action == "archive":
                    self._archived_file(acc_dir).write_text(
                        datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    )
                elif action == "unarchive":
                    f = self._archived_file(acc_dir)
                    if f.exists():
                        f.unlink()
                elif action == "delete":
                    self.delete_account(name)
                elif action == "set-owner":
                    self._owner_file(acc_dir).write_text(self._safe(owner))
                results.append({"name": name, "ok": True})
            except AccountError:
                raise
            except Exception as e:
                results.append({"name": name, "ok": False, "error": str(e)})

        return {
            "action": action,
            "owner": owner,
            "results": results,
            "ok": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
        }

    # -----------------------------------------------------------------------
    # Trash
    # -----------------------------------------------------------------------
    _TRASH_ID = re.compile(r"^[A-Za-z0-9 ._-]+__\d{8}-\d{6}$")

    def list_trash(self) -> list[dict]:
        trash_dir = self.customers_dir / "_trash"
        if not trash_dir.exists():
            return []
        out = []
        for d in sorted(trash_dir.iterdir(), reverse=True):
            if not d.is_dir() or "__" not in d.name:
                continue
            orig, _, stamp = d.name.rpartition("__")
            try:
                deleted_at = datetime.strptime(stamp, "%Y%m%d-%H%M%S").strftime("%Y-%m-%d %H:%M")
            except ValueError:
                deleted_at = stamp
            out.append({"trash_id": d.name, "name": orig, "deleted_at": deleted_at})
        return out

    def restore_trash(self, trash_id: str) -> dict:
        if not self._TRASH_ID.match(trash_id):
            raise AccountError(400, "Invalid trash id")
        src = self.customers_dir / "_trash" / trash_id
        if not src.is_dir():
            raise AccountError(404, "Not in trash")
        orig = trash_id.rpartition("__")[0]
        dest = self.customers_dir / orig
        if dest.exists():
            raise AccountError(409, f"An account named {orig} already exists — rename or remove it first.")
        shutil.move(str(src), str(dest))
        return {"name": orig, "restored": True}

    # -----------------------------------------------------------------------
    # Opportunities
    # -----------------------------------------------------------------------
    async def list_opportunities(self, account: str) -> list[dict]:
        account = self._safe(account)
        self._resolve_account_dir(account)

        opps: list[dict] = []
        if self._sfdc_opportunities:
            try:
                opps = await self._sfdc_opportunities(account)
            except Exception:
                opps = []

        if not opps:
            opps = [{
                "name": "General",
                "slug": "general",
                "stage": None,
                "stage_num": None,
                "amount": None,
                "close_date": None,
                "type": None,
                "is_closed": None,
                "ae": None,
            }]

        for o in opps:
            slug = o.get("slug") or self.slug(o.get("name", "opportunity"))
            o["output_count"] = self._output_service.count_outputs(account, slug, customers_dir=self.customers_dir)
        return opps

    # -----------------------------------------------------------------------
    # Activity summaries (read-only through job service)
    # -----------------------------------------------------------------------
    def account_activity(self, account: str) -> dict:
        """Running-job count and latest finished run for an account."""
        jobs = self._job_service.list_jobs(account=self._safe(account))
        return self._summarize_activity(jobs)

    def opportunity_activity(self, account: str, opp_slug: str) -> dict:
        """Running-job count and latest finished run for an opportunity."""
        jobs = self._job_service.list_jobs(account=self._safe(account), opp_slug=self._safe(opp_slug))
        return self._summarize_activity(jobs)

    def _summarize_activity(self, jobs: list[dict]) -> dict:
        running = 0
        last_run = None
        for j in jobs:
            if j.get("status") == "running":
                running += 1
                continue
            finished = j.get("finished_at") or 0
            if last_run is None or finished > (last_run.get("finished_at") or 0):
                last_run = {
                    "ok": j.get("ok"),
                    "finished_at": finished,
                    "status": j.get("status"),
                    "stderr": j.get("stderr"),
                }
        return {"running": running, "last_run": last_run}
