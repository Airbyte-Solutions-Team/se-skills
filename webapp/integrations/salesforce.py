"""Salesforce integration boundary for the SE Skills webapp.

Encapsulates `sf` CLI command construction, SOQL execution, JSON parsing, and
Salesforce record normalization. Callers receive plain dicts and never deal with
`sf` output directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import soql
from services.path_utils import resolve_within

logger = logging.getLogger(__name__)


class SalesforceIntegrationError(Exception):
    """Raised for Salesforce-specific failures that callers may choose to surface."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


class SalesforceIntegration:
    """Python-side Salesforce integration using the authenticated `sf` CLI.

    All methods are best-effort: if Salesforce is disabled, unauthenticated, or
    the `sf` CLI fails, they return safe empty results rather than raising.
    """

    def __init__(
        self,
        customers_dir: Path,
        workspace: Path,
        sf_config: Callable[[], dict[str, Any]],
        *,
        titlecase: Callable[[str], str],
        slug: Callable[[str], str],
        timeout: float = 25.0,
        max_stage_amount_accounts: int = 50,
    ) -> None:
        self.customers_dir = Path(customers_dir)
        self.workspace = Path(workspace)
        self._sf_config = sf_config
        self._titlecase = titlecase
        self._slug = slug
        self._timeout = timeout
        self._max_stage_amount_accounts = max_stage_amount_accounts

    # -----------------------------------------------------------------------
    # Config / availability
    # -----------------------------------------------------------------------
    def _config(self) -> dict[str, Any]:
        return self._sf_config() or {}

    def is_enabled(self) -> bool:
        return self._config().get("enabled", True)

    def _org_alias(self) -> str:
        return self._config().get("org_alias", "airbyte-prod")

    # -----------------------------------------------------------------------
    # Sidecar helpers
    # -----------------------------------------------------------------------
    def _sfdc_name_file(self, account_dir: Path) -> Path:
        return account_dir / ".sfdc-name"

    def _read_sfdc_name(self, account: str) -> str | None:
        """Return the true SFDC Account.Name captured at create time, if any.

        Folder names are lossy (punctuation stripped for filesystem safety), so
        the real name is stored verbatim in a sidecar and used for SOQL matching.
        """
        try:
            account_dir = resolve_within(self.customers_dir, account)
        except ValueError:
            return None
        f = self._sfdc_name_file(account_dir)
        return f.read_text().strip() if f.exists() else None

    def _sfdc_like_prefix(self, account: str) -> str:
        """A SOQL-LIKE-safe prefix for matching this account's opportunities.

        Prefers the stored real SFDC name. Falls back to the first alphanumeric
        token of the folder for legacy folders with no captured `.sfdc-name`.
        """
        real = self._read_sfdc_name(account)
        if real:
            return soql.soql_like_prefix(real)
        first = next((p for p in re.split(r"[^A-Za-z0-9]+", account) if p), account)
        return soql.soql_like_prefix(first)

    # -----------------------------------------------------------------------
    # Core SOQL runner
    # -----------------------------------------------------------------------
    async def _run_query(self, query: str) -> list[dict[str, Any]] | None:
        """Run a SOQL query via the `sf` CLI. Returns records, or `None` on any failure."""
        if not self.is_enabled():
            return None
        alias = self._org_alias()
        try:
            proc = await asyncio.create_subprocess_exec(
                "sf", "data", "query", "--query", query, "--target-org", alias, "--json",
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            if proc.returncode != 0:
                return None
            return json.loads(out).get("result", {}).get("records", [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("Salesforce query failed: %s", exc)
            return None

    @staticmethod
    def _quote(value: str) -> str:
        """Escape `value` for use as a SOQL single-quoted string literal."""
        return soql.soql_string_literal(value or "")

    # -----------------------------------------------------------------------
    # Public operations
    # -----------------------------------------------------------------------
    async def opportunities_for_account(self, account: str) -> list[dict[str, Any]]:
        """All SFDC opportunities for an account. Best-effort; returns [] if unavailable.

        Each opportunity contains `name`, `slug`, `stage`, `stage_num`, `amount`,
        `close_date`, `type`, `is_closed`, and `ae`.
        """
        if not self.is_enabled():
            return []
        like = self._sfdc_like_prefix(account)
        query = (
            "SELECT Name, StageName, Stage_Number__c, Amount, CloseDate, Type, "
            "IsClosed, Owner.Name "
            f"FROM Opportunity WHERE Account.Name LIKE '{like}%' ORDER BY CloseDate DESC"
        )
        records = await self._run_query(query)
        if not records:
            return []
        opps = []
        for r in records:
            name = r.get("Name") or "Opportunity"
            opps.append({
                "name": name,
                "slug": self._slug(name),
                "stage": r.get("StageName"),
                "stage_num": r.get("Stage_Number__c"),
                "amount": r.get("Amount"),
                "close_date": r.get("CloseDate"),
                "type": r.get("Type"),
                "is_closed": r.get("IsClosed"),
                "ae": ((r.get("Owner") or {}).get("Name")),
            })
        return opps

    async def stage_and_amount_for_accounts(
        self, account_names: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Return `{account_name: {stage, stage_num, amount, ae, ...}}` for the most
        relevant open (else latest) opportunity per account.

        One batched SOQL for all names. Returns `{}` on any failure.
        """
        if not self.is_enabled() or not account_names:
            return {}
        names = account_names[: self._max_stage_amount_accounts]
        likes = " OR ".join(
            f"Account.Name LIKE '{self._sfdc_like_prefix(n)}%'" for n in names
        )
        query = (
            "SELECT Account.Name, StageName, Stage_Number__c, Amount, CloseDate, "
            "IsClosed, Type, Owner.Name "
            f"FROM Opportunity WHERE {likes} ORDER BY CloseDate DESC"
        )
        records = await self._run_query(query)
        if not records:
            return {}

        # Exact map from stored SFDC name -> folder, plus a lossy token/prefix map
        # for legacy folders with no captured `.sfdc-name`.
        exact_for: dict[str, str] = {}
        prefix_for: dict[str, str] = {}
        for n in names:
            real = self._read_sfdc_name(n)
            if real:
                exact_for[real.lower()] = n
            prefix_for[self._sfdc_like_prefix(n).lower()] = n

        by_acct: dict[str, dict[str, Any]] = {}
        for r in records:
            acct_name = ((r.get("Account") or {}).get("Name") or "").lower()
            folder = exact_for.get(acct_name)
            if not folder:
                folder = next(
                    (
                        fn
                        for key, fn in prefix_for.items()
                        if acct_name.startswith(key) or key.startswith(acct_name)
                    ),
                    None,
                )
            if not folder:
                continue
            cand = {
                "stage": r.get("StageName"),
                "stage_num": r.get("Stage_Number__c"),
                "amount": r.get("Amount"),
                "ae": ((r.get("Owner") or {}).get("Name")),
                "type": r.get("Type"),
                "close_date": r.get("CloseDate"),
                "is_closed": r.get("IsClosed"),
                "open": not r.get("IsClosed"),
                "renewal": (r.get("Type") == "Renewal"),
            }
            cur = by_acct.get(folder)
            def score(c: dict[str, Any]) -> int:
                return 2 if c["open"] and not c["renewal"] else (1 if c["open"] else 0)

            if cur is None or score(cand) > score(cur):
                by_acct[folder] = cand
        return {
            k: {
                "stage": v["stage"],
                "stage_num": v["stage_num"],
                "amount": v["amount"],
                "ae": v["ae"],
                "type": v["type"],
                "close_date": v["close_date"],
                "is_closed": v["is_closed"],
            }
            for k, v in by_acct.items()
        }

    async def list_account_executives(self) -> list[str]:
        """All distinct AE names (`Opportunity.Owner.Name`) on open, future-dated
        opportunities org-wide. Best-effort `[]`.
        """
        if not self.is_enabled():
            return []
        today = datetime.now().strftime("%Y-%m-%d")
        query = (
            "SELECT Owner.Name FROM Opportunity "
            f"WHERE IsClosed = false AND CloseDate >= {today} "
            "ORDER BY Owner.Name"
        )
        records = await self._run_query(query)
        if not records:
            return []
        aes = {((r.get("Owner") or {}).get("Name") or "").strip() for r in records}
        return sorted(a for a in aes if a)

    async def accounts_for_member(
        self, member: dict[str, Any], ae_names: list[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Open, future-dated opportunities where `SE_Name__c` is the member OR
        `Owner.Name` is one of `ae_names`. Deduped to the best opportunity per
        account and split into `new_business` / `renewals`. Best-effort empty
        buckets on failure.
        """
        if not self.is_enabled():
            return {"new_business": [], "renewals": []}
        name = self._quote(member.get("name", ""))
        today = datetime.now().strftime("%Y-%m-%d")
        clauses = [f"SE_Name__c = '{name}'"]
        quoted_aes = [f"'{self._quote(a)}'" for a in ae_names if a]
        if quoted_aes:
            clauses.append(f"Owner.Name IN ({', '.join(quoted_aes)})")
        where_owner = " OR ".join(clauses)
        query = (
            "SELECT Account.Name, Amount, StageName, Stage_Number__c, CloseDate, "
            "Type, Owner.Name, SE_Name__c FROM Opportunity "
            f"WHERE IsClosed = false AND CloseDate >= {today} "
            f"AND ({where_owner}) ORDER BY Account.Name"
        )
        records = await self._run_query(query)
        if not records:
            return {"new_business": [], "renewals": []}

        def score(rec: dict[str, Any]) -> int:
            return 1 if rec.get("Type") != "Renewal" else 0

        by_acct: dict[str, dict[str, Any]] = {}
        for r in records:
            acct = ((r.get("Account") or {}).get("Name") or "").strip()
            if not acct:
                continue
            cur = by_acct.get(acct)
            if cur is None or score(r) > score(cur):
                by_acct[acct] = r

        new_business: list[dict[str, Any]] = []
        renewals: list[dict[str, Any]] = []
        for acct, r in sorted(by_acct.items()):
            folder = self._titlecase(acct)
            renewal = (r.get("Type") == "Renewal")
            item = {
                "name": folder,
                "account_name": acct,
                "amount": r.get("Amount"),
                "stage": r.get("StageName"),
                "stage_num": r.get("Stage_Number__c"),
                "close_date": r.get("CloseDate"),
                "type": r.get("Type"),
                "ae": ((r.get("Owner") or {}).get("Name")),
                "se": r.get("SE_Name__c"),
                "renewal": renewal,
                "exists": (self.customers_dir / folder).exists(),
            }
            (renewals if renewal else new_business).append(item)
        return {"new_business": new_business, "renewals": renewals}
