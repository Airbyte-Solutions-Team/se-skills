"""Explicit, bounded Salesforce portfolio snapshot for the local pilot."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from services.account_service import AccountService

_ID = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
_FIELDS = ("sfdc_id", "name", "account_id", "account_name", "stage", "owner", "close_date", "sfdc_url")
MAX_ROWS = 200


class SalesforcePortfolioError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class SalesforcePortfolioService:
    def __init__(self, *, customers_dir: Path, accounts: AccountService, salesforce: Any,
                 clock=None) -> None:
        self._dir = Path(customers_dir) / ".command-center"
        self._accounts = accounts
        self._salesforce = salesforce
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()

    def _read(self, name: str, default: dict) -> dict:
        try:
            data = json.loads((self._dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data.get("schema_version") == 1 else default
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".salesforce-", dir=self._dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self._dir / name)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def snapshot(self) -> dict:
        return self._read("salesforce-portfolio.json", {"schema_version": 1,
                          "last_success": None, "last_attempt": None})

    def mappings(self) -> dict[str, dict[str, str]]:
        raw = self._read("salesforce-mappings.json", {"schema_version": 1, "mappings": {}})
        return raw.get("mappings", {}) if isinstance(raw.get("mappings"), dict) else {}

    def local_identity(self, account: str, slug: str) -> dict | None:
        """Return CRM display metadata only for an explicitly mapped local folder."""
        mappings = self.mappings()
        records = (self.snapshot().get("last_success") or {}).get("records", [])
        by_id = {row["sfdc_id"]: row for row in records}
        for crm_id, target in mappings.items():
            if (target.get("account"), target.get("opportunity_slug")) == (account, slug):
                return by_id.get(crm_id)
        return None

    async def refresh(self, member_id: str, ae_names: list[str]) -> dict:
        member = self._accounts.member_by_id(member_id)
        if not member or not str(member.get("name") or "").strip():
            raise SalesforcePortfolioError(404, "Select a known SE before refreshing.")
        if len(ae_names) > 20 or any(not isinstance(a, str) or not 1 <= len(a.strip()) <= 120 for a in ae_names):
            raise SalesforcePortfolioError(400, "Select at most 20 AE names, each at most 120 characters.")
        aes = sorted({a.strip() for a in ae_names}, key=str.casefold)
        scope = {"member_id": member_id, "se_name": member["name"], "ae_names": aes,
                 "rule": "Open opportunities where SE_Name__c matches the selected SE or Owner.Name matches a selected AE"}
        async with self._lock:
            try:
                result = await self._salesforce.active_opportunity_portfolio(member["name"], aes, limit=MAX_ROWS)
            except Exception:  # noqa: BLE001
                result = {"state": "error"}
            state = result.get("state") if isinstance(result, dict) else "error"
            if state not in ("ok", "auth_error", "error", "disabled", "invalid_scope"):
                state = "error"
            snapshot = self.snapshot()
            now = self._clock().astimezone(timezone.utc).isoformat()
            if state == "ok":
                try:
                    source_rows = result["records"]
                    if not isinstance(source_rows, list) or len(source_rows) > MAX_ROWS:
                        raise ValueError("Invalid row count")
                    rows = []
                    for raw in source_rows:
                        if not isinstance(raw, dict) or not _ID.fullmatch(str(raw.get("sfdc_id") or "")):
                            raise ValueError("Invalid opportunity ID")
                        row = {key: raw.get(key) for key in _FIELDS}
                        link = urlparse(str(row.get("sfdc_url") or ""))
                        if link.scheme == "https" and link.hostname and link.hostname.endswith((".salesforce.com", ".force.com")):
                            row["sfdc_url"] = f"https://{link.hostname}/lightning/r/Opportunity/{row['sfdc_id']}/view"
                        else:
                            row["sfdc_url"] = None
                        rows.append(row)
                    if len({row["sfdc_id"] for row in rows}) != len(rows):
                        raise ValueError("Duplicate opportunity ID")
                    snapshot["last_success"] = {
                        "scope": scope, "refreshed_at": now, "state": "success",
                        "complete": not bool(result.get("truncated")),
                        "truncated": bool(result.get("truncated")), "count": len(rows), "records": rows,
                    }
                except (KeyError, ValueError, TypeError):
                    state = "error"
            snapshot["last_attempt"] = {"scope": scope, "at": now, "state": state}
            self._write("salesforce-portfolio.json", snapshot)
            return snapshot

    def confirm_mapping(self, sfdc_id: str, account: str, slug: str,
                        local_opportunities: list[dict], verified: list[dict] | None = None) -> dict:
        if not _ID.fullmatch(sfdc_id):
            raise SalesforcePortfolioError(400, "Invalid Salesforce opportunity ID.")
        snapshot = self.snapshot().get("last_success") or {}
        if sfdc_id not in {row.get("sfdc_id") for row in snapshot.get("records", [])}:
            raise SalesforcePortfolioError(404, "Refresh the selected scope before mapping this opportunity.")
        if (account, slug) not in {(row["account"], row["opportunity_slug"]) for row in local_opportunities}:
            raise SalesforcePortfolioError(404, "Create a local opportunity before mapping it.")
        for source in verified or []:
            assoc = source["association"]
            if assoc.get("state") != "associated":
                continue
            known_id = assoc.get("crm_opportunity_id")
            known_key = (assoc.get("account"), assoc.get("opportunity_slug"))
            if (known_id == sfdc_id and known_key != (account, slug)) or (
                known_key == (account, slug) and known_id and known_id != sfdc_id
            ):
                raise SalesforcePortfolioError(409, "A confirmed source has a different Salesforce opportunity ID.")
        mappings = self.mappings()
        for other_id, target in mappings.items():
            if other_id != sfdc_id and (target.get("account"), target.get("opportunity_slug")) == (account, slug):
                raise SalesforcePortfolioError(409, "That local opportunity is already mapped to another Salesforce ID.")
        mappings[sfdc_id] = {"account": account, "opportunity_slug": slug,
                             "confirmed_at": self._clock().astimezone(timezone.utc).isoformat()}
        self._write("salesforce-mappings.json", {"schema_version": 1, "mappings": mappings})
        return {"sfdc_id": sfdc_id, **mappings[sfdc_id]}

    def establish_local(self, sfdc_id: str, local_opportunities: list[dict],
                        verified: list[dict] | None = None) -> dict:
        """Create an ID-based local workspace only after the user explicitly asks."""
        if not _ID.fullmatch(sfdc_id):
            raise SalesforcePortfolioError(400, "Invalid Salesforce opportunity ID.")
        success = self.snapshot().get("last_success") or {}
        crm = next((r for r in success.get("records", []) if r.get("sfdc_id") == sfdc_id), None)
        if crm is None:
            raise SalesforcePortfolioError(404, "Refresh the selected scope before creating a local opportunity.")
        if any(source["association"].get("state") == "associated" and
               source["association"].get("crm_opportunity_id") == sfdc_id
               for source in verified or []):
            raise SalesforcePortfolioError(409, "A confirmed source already links this Salesforce opportunity; review its local opportunity.")
        account_id = crm.get("account_id")
        if not account_id or not _ID.fullmatch(str(account_id)):
            raise SalesforcePortfolioError(409, "This CRM row has no stable account ID; map it to an existing local opportunity.")
        # Keep both folders recognizable while the ID suffix preserves distinct
        # records with identical account or opportunity names.
        account_name = self._accounts.titlecase(str(crm.get("account_name") or "CRM"))[:55] or "CRM"
        opportunity_name = self._accounts.slug(str(crm.get("name") or "Opportunity"))[:55] or "Opportunity"
        account = self._accounts.titlecase(
            f"{account_name}-{hashlib.sha256(str(account_id).encode()).hexdigest()[:10]}"
        )
        slug = f"{opportunity_name}-{hashlib.sha256(sfdc_id.encode()).hexdigest()[:10]}"
        existing = self.mappings().get(sfdc_id)
        if existing:
            return {"sfdc_id": sfdc_id, **existing}
        account_dir = self._accounts._resolve_account_dir(account, must_exist=False)
        if not account_dir.exists():
            self._accounts.create_account(account, owner=success["scope"]["member_id"],
                                          sfdc_name=crm.get("account_name"))
        opp_dir = self._accounts._resolve_opportunity_dir(account, slug, must_exist=False)
        opp_dir.mkdir(parents=True, exist_ok=True)
        return self.confirm_mapping(sfdc_id, account, slug,
                                    [*local_opportunities, {"account": account, "opportunity_slug": slug}], verified)
