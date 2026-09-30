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
import shutil
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import soql
from services.path_utils import resolve_within

logger = logging.getLogger(__name__)

# Known `sf` CLI stderr substrings (lowercased) indicating the stored org auth is
# invalid/expired rather than some other failure (network, CLI bug, etc.). Best-known
# sf/sfdx CLI conventions — verify against real stderr from an actually-expired session
# and extend this list if a new failure mode is seen misclassified as generic "error".
_AUTH_ERROR_PATTERNS = (
    "expired access/refresh token",
    "no authorization information found",
    "notorgfound",
    "invalid_grant",
    "authinfooverwriteerror",
    "invalid session id",
)

# Salesforce record ID: 15-char case-sensitive or 18-char case-insensitive.
_ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")


def _classify_sf_failure(stderr: str) -> str:
    """Classify a non-zero `sf` CLI exit as "auth_error" or generic "error"."""
    low = (stderr or "").lower()
    return "auth_error" if any(p in low for p in _AUTH_ERROR_PATTERNS) else "error"


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

    def _sf_executable(self) -> str:
        """Resolve the `sf` CLI to its actual executable path.

        On Windows, `sf` installed via npm is a `.cmd` shim; `asyncio.create_subprocess_exec`
        cannot launch it by the bare name `sf` (no shell involved), so it must be resolved via
        `shutil.which` first. Falls back to the bare name if not found so the ensuing subprocess
        call fails with a clear FileNotFoundError instead of masking a resolution bug.
        """
        return shutil.which("sf") or "sf"

    async def _org_display_raw(self) -> tuple[int, str, str]:
        """Run `sf org display --json` once; return (returncode, stdout, stderr).

        Never raises; returncode -1 signals a local exception (timeout, spawn failure).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._sf_executable(), "org", "display", "--target-org", self._org_alias(), "--json",
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")
        except Exception as exc:  # noqa: BLE001
            logger.debug("Salesforce org display failed: %s", exc)
            return -1, "", str(exc)

    async def instance_url(self) -> str | None:
        """Return the org's Salesforce base URL (e.g. https://airbyte.my.salesforce.com),
        used to build Lightning record links. Prefers an explicit `instance_url` in the
        `salesforce:` config; otherwise derives it once from `sf org display --json` and
        caches it. Returns None if unavailable (links then degrade to plain text."""
        explicit = self._config().get("instance_url")
        if explicit:
            return str(explicit).rstrip("/")
        if not self.is_enabled():
            return None
        cached = getattr(self, "_instance_url_cache", "__unset__")
        if cached != "__unset__":
            return cached
        url: str | None = None
        rc, out, _ = await self._org_display_raw()
        if rc == 0:
            # `sf` may prepend a non-JSON update-warning line; parse from the first `{`.
            brace = out.find("{")
            if brace != -1:
                try:
                    data = json.loads(out[brace:])
                    raw = (data.get("result") or {}).get("instanceUrl")
                    if raw:
                        url = str(raw).rstrip("/")
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Salesforce instance_url parse failed: %s", exc)
        self._instance_url_cache = url
        return url

    @staticmethod
    def lightning_record_url(base_url: str | None, object_name: str, record_id: str | None) -> str | None:
        """Build a Lightning record URL, or None if base_url/record_id is missing."""
        if not base_url or not record_id:
            return None
        return f"{base_url}/lightning/r/{object_name}/{record_id}/view"

    # -----------------------------------------------------------------------
    # Connection health (for the UI status badge)
    # -----------------------------------------------------------------------
    _STATUS_CACHE_TTL = 60.0

    async def status(self, *, force: bool = False) -> dict[str, Any]:
        """Live Salesforce connection health for the UI status badge.

        `state` is one of "ok" | "auth_error" | "not_installed" | "error" | "disabled".
        Cached for `_STATUS_CACHE_TTL` seconds (auth state can change at runtime, e.g.
        after the user runs `sf org login web`, without a server restart); `force=True`
        bypasses the cache for an immediate recheck.
        """
        alias = self._org_alias()
        if not self.is_enabled():
            return {
                "enabled": False,
                "state": "disabled",
                "message": "Salesforce integration is disabled in .se-config.yaml.",
                "org_alias": alias,
            }
        if shutil.which("sf") is None:
            return {
                "enabled": True,
                "state": "not_installed",
                "message": "The Salesforce CLI (`sf`) isn't installed or isn't on PATH.",
                "org_alias": alias,
            }

        now = asyncio.get_event_loop().time()
        cached = getattr(self, "_status_cache", None)
        if cached and not force and (now - cached[0]) < self._STATUS_CACHE_TTL:
            return cached[1]

        rc, out, err = await self._org_display_raw()
        # `sf org display` can exit 0 even with a dead session: a stale/expired refresh
        # token surfaces only inside the "successful" JSON, as a non-"Connected"
        # `connectedStatus` string (plus a "unable to refresh auth for org" warning) —
        # NOT as a non-zero exit code. Must inspect connectedStatus, not just rc.
        username = None
        connected_status = None
        brace = out.find("{")
        if brace != -1:
            try:
                result_obj = json.loads(out[brace:]).get("result") or {}
                username = result_obj.get("username")
                connected_status = result_obj.get("connectedStatus")
            except Exception:  # noqa: BLE001
                pass

        if rc == 0 and (connected_status is None or connected_status.lower() == "connected"):
            result = {
                "enabled": True,
                "state": "ok",
                "message": f"Connected to Salesforce as {username}." if username else "Connected to Salesforce.",
                "org_alias": alias,
            }
        else:
            state = _classify_sf_failure(connected_status or err)
            logger.info("Salesforce health check failed (state=%s): %s", state, (connected_status or err).strip()[:300])
            result = {
                "enabled": True,
                "state": state,
                "message": (
                    "Salesforce session needs to be reconnected (often happens after a password reset)."
                    if state == "auth_error"
                    else "Salesforce commands are currently failing."
                ),
                "org_alias": alias,
            }
        self._status_cache = (now, result)
        return result

    async def reauthenticate(self) -> dict[str, Any]:
        """Kick off `sf org login web` in the background.

        This opens a real browser window on this machine for the user to complete
        Salesforce OAuth — since the webapp runs locally, that's the same flow as
        running the command in a terminal. Does not wait for the flow to finish (the
        user may take a while); the caller polls `status(force=True)` to detect
        completion. Refuses to double-spawn while one is already in flight.
        """
        if getattr(self, "_reauth_in_flight", False):
            return {"started": False, "message": "A reauthentication is already in progress."}
        alias = self._org_alias()
        try:
            proc = await asyncio.create_subprocess_exec(
                self._sf_executable(), "org", "login", "web", "--alias", alias,
                cwd=str(self.workspace),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("Salesforce reauthenticate spawn failed: %s", exc)
            return {"started": False, "message": f"Couldn't launch `sf org login web`: {exc}"}

        self._reauth_in_flight = True

        async def _wait_and_clear() -> None:
            try:
                await proc.wait()
            finally:
                self._reauth_in_flight = False
                self._status_cache = None  # force a fresh check next time status() is called

        asyncio.create_task(_wait_and_clear())
        return {"started": True, "message": f"Opening a browser to log in to {alias}…"}

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
        return f.read_text(encoding="utf-8").strip() if f.exists() else None

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
                self._sf_executable(), "data", "query", "--query", query, "--target-org", alias, "--json",
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

    async def active_opportunity_portfolio(
        self, se_name: str, ae_names: list[str], *, limit: int = 200
    ) -> dict[str, Any]:
        """One bounded, ID-preserving read for a selected SE and AEs.

        An extra row detects truncation. No close-date condition is used: an
        overdue open opportunity is still active. This path does not use the
        account helpers, which intentionally select one row per account.
        """
        if not self.is_enabled():
            return {"state": "disabled", "records": [], "truncated": False}
        if not 1 <= limit <= 200 or not se_name.strip() or len(ae_names) > 20:
            return {"state": "invalid_scope", "records": [], "truncated": False}
        clauses = [f"SE_Name__c = '{self._quote(se_name)}'"]
        if ae_names:
            quoted = ", ".join(f"'{self._quote(a)}'" for a in ae_names)
            clauses.append(f"Owner.Name IN ({quoted})")
        query = (
            "SELECT Id, Name, Account.Id, Account.Name, StageName, Owner.Name, "
            "CloseDate, IsClosed FROM Opportunity "
            f"WHERE IsClosed = false AND ({' OR '.join(clauses)}) "
            f"ORDER BY Id LIMIT {limit + 1}"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                self._sf_executable(), "data", "query", "--query", query,
                "--target-org", self._org_alias(), "--json",
                cwd=str(self.workspace), stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            if proc.returncode != 0:
                return {"state": _classify_sf_failure(err.decode(errors="replace")),
                        "records": [], "truncated": False}
            output = out.decode(errors="replace")
            payload = json.loads(output[output.index("{"):])
            result = payload.get("result")
            if payload.get("status") not in (None, 0) or not isinstance(result, dict):
                raise ValueError("Invalid Salesforce query result")
            records = result.get("records")
            if not isinstance(records, list):
                raise ValueError("Missing Salesforce records")
            if len(records) > limit + 1:
                raise ValueError("Salesforce returned more than the requested bound")
            base_url = await self.instance_url()
            rows = []
            for record in records[:limit]:
                if not isinstance(record, dict) or not record.get("Id"):
                    raise ValueError("Salesforce returned an opportunity without an ID")
                opp_id = str(record["Id"])
                account = record.get("Account") or {}
                owner = record.get("Owner") or {}
                rows.append({
                    "sfdc_id": opp_id, "name": str(record.get("Name") or "Opportunity"),
                    "account_id": account.get("Id"), "account_name": account.get("Name"),
                    "stage": record.get("StageName"), "owner": owner.get("Name"),
                    "close_date": record.get("CloseDate"),
                    "sfdc_url": self.lightning_record_url(base_url, "Opportunity", opp_id),
                })
            if len({row["sfdc_id"] for row in rows}) != len(rows):
                raise ValueError("Salesforce returned duplicate opportunity IDs")
            return {"state": "ok", "records": rows,
                    "truncated": len(records) > limit or result.get("done") is False}
        except Exception:  # noqa: BLE001 - never expose CLI output or credentials
            return {"state": "error", "records": [], "truncated": False}

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
            "SELECT Id, Name, StageName, Stage_Number__c, Amount, CloseDate, Type, "
            "IsClosed, Owner.Name, Account.Id "
            f"FROM Opportunity WHERE Account.Name LIKE '{like}%' ORDER BY CloseDate DESC"
        )
        records = await self._run_query(query)
        if not records:
            return []
        base_url = await self.instance_url()
        opps = []
        for r in records:
            name = r.get("Name") or "Opportunity"
            opp_id = r.get("Id")
            acct_id = (r.get("Account") or {}).get("Id")
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
                "sfdc_id": opp_id,
                "sfdc_account_id": acct_id,
                "sfdc_url": self.lightning_record_url(base_url, "Opportunity", opp_id),
                "sfdc_account_url": self.lightning_record_url(base_url, "Account", acct_id),
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
            "SELECT Account.Name, Account.Id, StageName, Stage_Number__c, Amount, CloseDate, "
            "IsClosed, Type, Owner.Name "
            f"FROM Opportunity WHERE {likes} ORDER BY CloseDate DESC"
        )
        records = await self._run_query(query)
        if not records:
            return {}
        base_url = await self.instance_url()

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
                "sfdc_account_id": (r.get("Account") or {}).get("Id"),
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
                "sfdc_account_id": v.get("sfdc_account_id"),
                "sfdc_account_url": self.lightning_record_url(base_url, "Account", v.get("sfdc_account_id")),
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
            "SELECT Account.Id, Account.Name, Amount, StageName, Stage_Number__c, CloseDate, "
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
            account_id = (r.get("Account") or {}).get("Id")
            if account_id and not _ACCOUNT_ID_RE.fullmatch(str(account_id)):
                account_id = None
            # A folder elsewhere already carrying this Salesforce Account.Id is the
            # same account under a name Salesforce has since renamed; treat it as
            # already added (under its current local name) rather than new business,
            # so the caller can reconcile the two names instead of creating a duplicate.
            local_match = self._find_local_account_by_sfdc_id(account_id) if account_id else None
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
                "exists": (self.customers_dir / folder).exists() or bool(local_match),
                "sfdc_account_id": account_id,
                "renamed_from": local_match if local_match and local_match != folder else None,
            }
            (renewals if renewal else new_business).append(item)
        return {"new_business": new_business, "renewals": renewals}

    def _sfdc_id_file(self, account_dir: Path) -> Path:
        return account_dir / ".sfdc-account-id"

    def _find_local_account_by_sfdc_id(self, sfdc_id: str) -> str | None:
        """Local folder name whose captured Salesforce Account.Id matches, if any.

        Read-only lookup across `customers_dir`; the actual identity sidecar is
        written by `AccountService.create_account`/`rename_account`/`set_sfdc_identity`.
        """
        if not sfdc_id or not self.customers_dir.exists():
            return None
        for d in sorted(self.customers_dir.iterdir()):
            if not d.is_dir() or d.name.startswith(("_", ".")):
                continue
            f = self._sfdc_id_file(d)
            if f.exists() and f.read_text(encoding="utf-8").strip() == sfdc_id:
                return d.name
        return None
