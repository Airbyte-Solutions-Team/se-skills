"""Local Gmail read-only HTTP transport, verified with canned provider responses only.

No live mailbox, OAuth client or Google account is exercised here: `FakeHttp` replays
fixture responses shaped like the Gmail REST API and records every request so the
tests can prove what was (and was not) asked of the provider.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from integrations.gmail import (
    MAX_MESSAGES_PER_GET,
    READONLY_SCOPE,
    GmailImportError,
    GmailSourceAdapter,
    GmailThreadRow,
    GmailTransportError,
)
from integrations.gmail_live import (
    CREDENTIALS_CONTRACT,
    GMAIL_API,
    MAX_LIST_PAGES,
    MAX_LIST_TERMS,
    TOKEN_URL,
    TOKENINFO_URL,
    HttpResponse,
    LiveGmailReadOnlyTransport,
    authorization_url,
    credentials_path,
    load_local_credentials,
)
from services.evidence_ledger_service import EvidenceLedgerError, EvidenceLedgerService
from services.gmail_intake_service import (
    GmailIntakeError,
    GmailIntakeService,
    connection_id_for,
    mailbox_key,
)
from services.job_service import JobService

from eval.tests.test_gmail_intake import _opps, _retrieve

ACCESS_TOKEN = "ya29.FAKE-ACCESS-TOKEN-SENTINEL"
REFRESH_TOKEN = "1//FAKE-REFRESH-TOKEN-SENTINEL"
MAILBOX = "se@vendor.example"
WINDOW = {"time_range": "custom", "custom_start": date(2026, 9, 1), "custom_end": date(2026, 9, 30)}
T_ACME, T_GLOBEX, T_UNRELATED = "19a0000000000a01", "19a0000000000a02", "19a0000000000a03"
M_ASK, M_REPLY, M_GLOBEX, M_UNRELATED = "19a0000000000b01", "19a0000000000b02", "19a0000000000b03", "19a0000000000b04"
M_BAD_B64, M_NO_FROM, M_HTML_ONLY, M_ATTACH = "19a0000000000b05", "19a0000000000b06", "19a0000000000b07", "19a0000000000b08"
BODY_SENTINEL = "FAKE-HTTP-BODY"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def _json(status: int, payload: Any, headers: Mapping[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, json.dumps(payload).encode("utf-8"), headers or {})


def _api_error(status: int, reason: str) -> HttpResponse:
    return _json(status, {"error": {"code": status, "message": "fake", "errors": [{"reason": reason}]}})


def _hdr(**headers: str) -> list[dict[str, str]]:
    return [{"name": k.replace("_", "-"), "value": v} for k, v in headers.items()]


def _meta_message(mid: str, tid: str, *, frm: str, to: str, subject: str, stamp: int, cc: str = "") -> dict[str, Any]:
    headers = _hdr(From=frm, To=to, Subject=subject, Date="Thu, 10 Sep 2026 10:00:00 +0000")
    if cc:
        headers.append({"name": "Cc", "value": cc})
    return {"id": mid, "threadId": tid, "internalDate": str(stamp), "snippet": "SNIPPET-MUST-NOT-LEAK", "payload": {"headers": headers}}


def _full_message(mid: str, tid: str, *, frm: str, to: str, subject: str, body: str | None, extra_parts: list | None = None,
                  in_reply_to: str | None = None) -> dict[str, Any]:
    headers = _hdr(From=frm, To=to, Subject=subject, Date="Thu, 10 Sep 2026 10:00:00 +0000", Message_ID=f"<{mid}@mail.example>")
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
        headers.append({"name": "References", "value": in_reply_to})
    parts: list[dict[str, Any]] = []
    if body is not None:
        parts.append({"mimeType": "text/plain", "filename": "", "body": {"size": len(body), "data": _b64(body)}})
    parts.extend(extra_parts or [])
    return {
        "id": mid, "threadId": tid, "internalDate": "1788000000000", "sizeEstimate": 2048, "snippet": "SNIPPET-MUST-NOT-LEAK",
        "payload": {"mimeType": "multipart/mixed", "headers": headers, "body": {"size": 0}, "parts": parts},
    }


def mailbox_fixture() -> dict[str, Any]:
    """Canned Gmail REST responses keyed by (method, url-without-query)."""
    acme, globex = "alice@acme.example", "bob@globex.example"
    return {
        "threads": {
            T_ACME: {"id": T_ACME, "messages": [
                _meta_message(M_ASK, T_ACME, frm=f"Alice <{acme}>", to=MAILBOX, subject="Acme pilot ask", stamp=1788000000000),
                _meta_message(M_REPLY, T_ACME, frm=MAILBOX, to=acme, subject="Re: Acme pilot ask", stamp=1788003600000),
            ]},
            T_GLOBEX: {"id": T_GLOBEX, "messages": [
                _meta_message(M_GLOBEX, T_GLOBEX, frm=f"Bob <{globex}>", to=MAILBOX, subject="Globex renewal", stamp=1788010000000),
            ]},
            T_UNRELATED: {"id": T_UNRELATED, "messages": [
                _meta_message(M_UNRELATED, T_UNRELATED, frm="news@unrelated.example", to=MAILBOX, subject="Newsletter", stamp=1788020000000),
            ]},
        },
        "messages": {
            M_ASK: _full_message(M_ASK, T_ACME, frm=f"Alice <{acme}>", to=MAILBOX, subject="Acme pilot ask",
                                 body=f"Can you send the SSO configuration guide by Friday? {BODY_SENTINEL}"),
            M_REPLY: _full_message(M_REPLY, T_ACME, frm=MAILBOX, to=acme, subject="Re: Acme pilot ask",
                                   body="Sent, see attached guide.\n\nOn Thu wrote:\n> Can you send the SSO configuration guide",
                                   in_reply_to=f"<{M_ASK}@mail.example>"),
            M_GLOBEX: _full_message(M_GLOBEX, T_GLOBEX, frm=f"Bob <{globex}>", to=MAILBOX, subject="Globex renewal", body="Renewal question."),
            M_BAD_B64: _full_message(M_BAD_B64, T_ACME, frm=acme, to=MAILBOX, subject="broken", body=None,
                                     extra_parts=[{"mimeType": "text/plain", "filename": "", "body": {"data": "!!!not*base64!!!"}}]),
            M_NO_FROM: {"id": M_NO_FROM, "threadId": T_ACME, "internalDate": "1788000000000",
                        "payload": {"mimeType": "text/plain", "headers": _hdr(Subject="no sender"), "body": {"data": _b64("x")}}},
            M_HTML_ONLY: _full_message(M_HTML_ONLY, T_ACME, frm=acme, to=MAILBOX, subject="html", body=None, extra_parts=[
                {"mimeType": "text/html", "filename": "", "body": {"data": _b64("<p>Hello <b>there</b></p><script>x()</script>")}}]),
            M_ATTACH: _full_message(M_ATTACH, T_ACME, frm=acme, to=MAILBOX, subject="deck", body="Deck attached.", extra_parts=[
                {"mimeType": "application/pdf", "filename": "deck.pdf", "body": {"attachmentId": "ATTACHMENT-ID-NEVER-FETCH", "size": 9000}}]),
        },
    }


class FakeHttp:
    """Replays Gmail-shaped responses; records every request; supports scripted overrides."""

    def __init__(self, fixture: dict[str, Any] | None = None, *, mailbox: str = MAILBOX, scopes: str = READONLY_SCOPE) -> None:
        self.fixture = fixture or mailbox_fixture()
        self.mailbox = mailbox
        self.scopes = scopes
        self.requests: list[dict[str, Any]] = []
        self.overrides: dict[str, list[HttpResponse]] = {}
        self.token_responses: list[HttpResponse] = []
        self.list_pages: list[dict[str, Any]] | None = None
        self.tokens_issued = 0

    def override(self, url: str, *responses: HttpResponse) -> None:
        self.overrides.setdefault(url, []).extend(responses)

    async def request(self, method: str, url: str, *, headers=None, params=None, data=None) -> HttpResponse:
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {}), "params": dict(params or {}), "data": dict(data or {})})
        if self.overrides.get(url):
            return self.overrides[url].pop(0)
        if url == TOKEN_URL:
            if self.token_responses:
                return self.token_responses.pop(0)
            assert data["grant_type"] == "refresh_token" and data["refresh_token"] == REFRESH_TOKEN
            self.tokens_issued += 1
            return _json(200, {"access_token": ACCESS_TOKEN, "expires_in": 3599, "token_type": "Bearer"})
        assert (headers or {}).get("Authorization") == f"Bearer {ACCESS_TOKEN}" or url == TOKENINFO_URL, "unauthenticated provider call"
        if url == TOKENINFO_URL:
            assert params["access_token"] == ACCESS_TOKEN
            return _json(200, {"scope": self.scopes, "expires_in": "3000"})
        if url == f"{GMAIL_API}/profile":
            return _json(200, {"emailAddress": self.mailbox, "messagesTotal": 1, "historyId": "1"})
        if url == f"{GMAIL_API}/threads":
            return self._list_threads(params)
        match = re.fullmatch(rf"{re.escape(GMAIL_API)}/threads/([0-9a-f]+)", url)
        if match:
            assert params.get("format") == "metadata", "thread listing must be metadata-only"
            thread = self.fixture["threads"].get(match.group(1))
            return _json(200, thread) if thread else _api_error(404, "notFound")
        match = re.fullmatch(rf"{re.escape(GMAIL_API)}/messages/([0-9a-f]+)", url)
        if match:
            message = self.fixture["messages"].get(match.group(1))
            return _json(200, message) if message else _api_error(404, "notFound")
        raise AssertionError(f"unexpected provider request {method} {url}")

    def _list_threads(self, params: Mapping[str, Any]) -> HttpResponse:
        assert 1 <= int(params["maxResults"]) <= 100
        if self.list_pages is not None:
            index = int(params.get("pageToken", "page-0").split("-")[1])
            page = dict(self.list_pages[index])
            if index + 1 < len(self.list_pages):
                page["nextPageToken"] = f"page-{index + 1}"
            return _json(200, page)
        query = params["q"]
        rows = []
        for tid, thread in self.fixture["threads"].items():
            participants = " ".join(h["value"] for m in thread["messages"] for h in m["payload"]["headers"] if h["name"] in ("From", "To", "Cc"))
            if any(term in participants for term in re.findall(r"from:(\S+)", query)):
                rows.append({"id": tid, "snippet": "SNIPPET-MUST-NOT-LEAK", "historyId": "1"})
        return _json(200, {"threads": rows, "resultSizeEstimate": len(rows)})

    def urls(self, fragment: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if fragment in r["url"]]


def _write_credentials(tmp_path: Path, **overrides: Any) -> Path:
    path = tmp_path / "gmail-oauth.json"
    payload = {
        "contract": CREDENTIALS_CONTRACT, "client_id": "fake-client.apps.googleusercontent.com", "client_secret": "fake-installed-secret",
        "refresh_token": REFRESH_TOKEN, "scopes": [READONLY_SCOPE], "authorized_at": "2026-09-25T00:00:00+00:00", **overrides,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _transport(tmp_path: Path, http: FakeHttp | None = None) -> tuple[LiveGmailReadOnlyTransport, FakeHttp]:
    http = http or FakeHttp()
    return LiveGmailReadOnlyTransport(credentials_file=_write_credentials(tmp_path), http=http), http


def _service(tmp_path: Path, http: FakeHttp | None = None) -> tuple[GmailIntakeService, FakeHttp, EvidenceLedgerService, JobService]:
    transport, http = _transport(tmp_path, http)
    customers = tmp_path / "customers"
    customers.mkdir(exist_ok=True)
    ledger = EvidenceLedgerService(customers)
    jobs = JobService(tmp_path, model_for=lambda _: "unused", persist_run=lambda *args: None)
    service = GmailIntakeService(transport=transport, adapter=GmailSourceAdapter(), ledger=ledger, job_service=jobs, local_opportunities=_opps)
    return service, http, ledger, jobs


def _no_secret_leak(*texts: str) -> None:
    for text in texts:
        assert ACCESS_TOKEN not in text and REFRESH_TOKEN not in text and "fake-installed-secret" not in text


# ------------------------------------------------------------ activation


def test_transport_is_not_selected_implicitly_and_fails_closed_without_credentials(tmp_path: Path) -> None:
    transport = LiveGmailReadOnlyTransport(credentials_file=tmp_path / "missing.json", http=FakeHttp())
    described = transport.describe()
    assert described.mode == "live_readonly" and described.live_retrieval_available is False
    assert described.hosted_credentials is False and described.attachments == "never_fetched"
    with pytest.raises(GmailTransportError) as exc:
        import asyncio
        asyncio.run(transport.check_access())
    assert exc.value.code == "credentials_missing" and not exc.value.retryable
    assert credentials_path({}) == Path.home() / ".config" / "se-skills" / "gmail-oauth.json"
    assert credentials_path({"SE_GMAIL_OAUTH_FILE": str(tmp_path / "x.json")}) == tmp_path / "x.json"


def test_credential_file_contract_is_strict(tmp_path: Path) -> None:
    for bad in ({"contract": "other"}, {"refresh_token": ""}, {"scopes": "not-a-list"}):
        with pytest.raises(GmailTransportError) as exc:
            load_local_credentials(_write_credentials(tmp_path, **bad))
        assert exc.value.code == "credentials_missing"
        _no_secret_leak(exc.value.detail)
    loaded = load_local_credentials(_write_credentials(tmp_path))
    assert loaded.scopes == (READONLY_SCOPE,)


def test_app_selects_live_transport_only_by_explicit_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = (Path(__file__).resolve().parents[2] / "webapp" / "app.py").read_text(encoding="utf-8")
    assert 'if gmail_mode == "live_readonly":' in source
    assert "LiveGmailReadOnlyTransport(credentials_file=credentials_path())" in source
    assert 'elif gmail_mode in ("", "unavailable"):' in source and "UnavailableGmailTransport()" in source


def test_authorization_url_requests_exactly_one_scope_offline_with_pkce() -> None:
    url = authorization_url(client_id="cid", redirect_uri="http://127.0.0.1:1234/callback", state="st", code_challenge="ch")
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fgmail.readonly&" in url
    assert "access_type=offline" in url and "code_challenge_method=S256" in url and "include_granted_scopes=false" in url
    assert url.count("scope=") == 1


def test_authorize_script_stores_only_an_exact_readonly_grant(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[2] / "scripts" / "gmail_local_authorize.py"
    spec = importlib.util.spec_from_file_location("gmail_local_authorize", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    class TokenHttp:
        def __init__(self, scope: str) -> None:
            self.scope = scope

        async def request(self, method, url, *, headers=None, params=None, data=None):
            assert url == TOKEN_URL and data["grant_type"] == "authorization_code" and data["code_verifier"] == "ver"
            return _json(200, {"access_token": ACCESS_TOKEN, "refresh_token": REFRESH_TOKEN, "scope": self.scope, "expires_in": 3599})

    import asyncio
    kwargs = {"code": "code", "client_id": "cid", "client_secret": "sec", "redirect_uri": "http://127.0.0.1:1/callback", "code_verifier": "ver"}
    with pytest.raises(module.AuthorizeError):
        asyncio.run(module.exchange_code(TokenHttp(f"{READONLY_SCOPE} https://www.googleapis.com/auth/gmail.modify"), **kwargs))
    payload = asyncio.run(module.exchange_code(TokenHttp(READONLY_SCOPE), **kwargs))
    target = tmp_path / "nested" / "gmail-oauth.json"
    module.write_credentials(target, payload)
    assert load_local_credentials(target).scopes == (READONLY_SCOPE,)
    if os.name == "posix":
        assert oct(target.stat().st_mode & 0o777) == "0o600" and oct(target.parent.stat().st_mode & 0o777) == "0o700"
    client = tmp_path / "client.json"
    client.write_text(json.dumps({"web": {"client_id": "x"}}), encoding="utf-8")
    with pytest.raises(module.AuthorizeError):
        module.load_client_file(client)


# ------------------------------------------------------------ access check


async def test_check_access_reports_mailbox_and_granted_scopes_and_refreshes_once(tmp_path: Path) -> None:
    transport, http = _transport(tmp_path)
    first = await transport.check_access()
    second = await transport.check_access()
    assert first == {"email_address": MAILBOX, "scopes": [READONLY_SCOPE]} == second
    assert http.tokens_issued == 1
    assert transport.describe().live_retrieval_available is True


async def test_broader_grant_is_refused_by_the_service(tmp_path: Path) -> None:
    service, http, _ledger, _jobs = _service(tmp_path, FakeHttp(scopes=f"{READONLY_SCOPE} https://www.googleapis.com/auth/gmail.modify"))
    result = await service.check_access()
    assert result["connected"] is False and result["error_code"] == "scope_not_readonly"
    with pytest.raises(GmailIntakeError) as exc:
        await service.list_threads(participants=["acme.example"], **WINDOW)
    assert exc.value.code == "not_checked"
    assert not http.urls("/threads")


# ------------------------------------------------------------ authorization loss


async def test_revoked_refresh_token_fails_closed_and_is_reported_as_access_revoked(tmp_path: Path) -> None:
    service, http, _ledger, _jobs = _service(tmp_path)
    assert (await service.check_access())["connected"] is True
    http.token_responses.append(_json(400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}))
    http.overrides.clear()
    transport = service._transport
    transport._access_token = None
    with pytest.raises(GmailIntakeError) as exc:
        await service.list_threads(participants=["acme.example"], **WINDOW)
    assert exc.value.code == "access_revoked"
    assert not http.urls("/threads")
    result = await service.check_access() if not http.token_responses else None
    assert result is None or result["connected"] is True


async def test_401_during_retrieval_imports_nothing_and_401_during_listing_fails_closed(tmp_path: Path) -> None:
    service, http, ledger, jobs = _service(tmp_path)
    await service.check_access()
    http.override(f"{GMAIL_API}/threads", _api_error(401, "authError"))
    with pytest.raises(GmailIntakeError) as exc:
        await service.list_threads(participants=["acme.example"], **WINDOW)
    assert exc.value.code == "not_authorized" and exc.value.status == 409
    http.override(f"{GMAIL_API}/messages/{M_ASK}", _api_error(401, "authError"))
    job = await _retrieve(service, jobs, [M_ASK, M_GLOBEX])
    assert job["counts"] == {"inaccessible": 2}
    assert ledger.list_sources()["total"] == 2
    assert all(row["availability"] == "access_lost" for row in job["results"])
    with pytest.raises(EvidenceLedgerError) as withheld:
        ledger.read_content(job["results"][0]["source_id"], revision=1)
    assert withheld.value.code == "content_withheld"


async def test_provider_error_details_never_carry_tokens_or_bodies(tmp_path: Path) -> None:
    transport, http = _transport(tmp_path)
    http.override(f"{GMAIL_API}/profile", HttpResponse(500, f"<html>{ACCESS_TOKEN} {BODY_SENTINEL}</html>".encode()))
    with pytest.raises(GmailTransportError) as exc:
        await transport.check_access()
    assert exc.value.code == "provider_unavailable" and exc.value.retryable
    _no_secret_leak(exc.value.detail, str(exc.value))
    assert BODY_SENTINEL not in exc.value.detail


# ------------------------------------------------------------ account switching


async def test_reauthorizing_as_another_mailbox_fails_closed_then_isolates_ledger_identity(tmp_path: Path) -> None:
    service, http, ledger, jobs = _service(tmp_path)
    await service.check_access()
    job = await _retrieve(service, jobs, [M_ASK])
    assert job["counts"] == {"imported": 1}
    first_source = job["results"][0]["source_id"]
    first_connection = ledger.get_source(first_source)["connection_id"]
    assert first_connection == connection_id_for(mailbox_key(MAILBOX))

    http.mailbox = "someone-else@vendor.example"
    with pytest.raises(GmailIntakeError) as exc:
        await service.list_threads(participants=["acme.example"], **WINDOW)
    assert exc.value.code == "mailbox_changed"
    assert not http.urls("/threads")
    with pytest.raises(GmailIntakeError) as exc:
        await service.start_retrieval([M_ASK])
    assert exc.value.code == "not_checked", "drift cleared the bound check; nothing runs until a fresh check"
    assert ledger.list_sources()["total"] == 1 and not http.urls("/messages/")[1:]

    check = await service.check_access()
    assert check["connected"] and check["mailbox_switched"] is True
    job = await _retrieve(service, jobs, [M_ASK])
    assert job["counts"] == {"imported": 1}
    second_source = job["results"][0]["source_id"]
    assert second_source != first_source
    assert ledger.get_source(second_source)["connection_id"] == connection_id_for(mailbox_key("someone-else@vendor.example"))
    listing = await service.list_threads(participants=["acme.example"], **WINDOW)
    acme_rows = [t for t in listing["threads"] if t["thread_id"] == T_ACME]
    assert acme_rows and acme_rows[0]["messages"][0]["ledger"]["source_id"] == second_source


# ------------------------------------------------------------ rate limits


@pytest.mark.parametrize("response", [_api_error(429, "rateLimitExceeded"), _api_error(403, "userRateLimitExceeded"), _api_error(403, "quotaExceeded")])
async def test_rate_limit_on_listing_is_retryable_and_nothing_else_is_requested(tmp_path: Path, response: HttpResponse) -> None:
    service, http, _ledger, _jobs = _service(tmp_path)
    await service.check_access()
    http.override(f"{GMAIL_API}/threads", response)
    with pytest.raises(GmailIntakeError) as exc:
        await service.list_threads(participants=["acme.example"], **WINDOW)
    assert exc.value.code == "rate_limited" and exc.value.status == 502
    assert len(http.urls("/threads")) == 1


async def test_rate_limit_during_retrieval_marks_every_selected_message_retryable_then_succeeds(tmp_path: Path) -> None:
    service, http, ledger, jobs = _service(tmp_path)
    await service.check_access()
    http.override(f"{GMAIL_API}/messages/{M_GLOBEX}", _api_error(429, "rateLimitExceeded"))
    job = await _retrieve(service, jobs, [M_ASK, M_GLOBEX])
    assert job["counts"] == {"failed_retryable": 2} and ledger.list_sources()["total"] == 0
    job = await _retrieve(service, jobs, [M_ASK, M_GLOBEX])
    assert job["counts"] == {"imported": 2}


async def test_403_forbidden_without_rate_reason_is_not_retryable(tmp_path: Path) -> None:
    transport, http = _transport(tmp_path)
    http.override(f"{GMAIL_API}/threads", _api_error(403, "insufficientPermissions"))
    with pytest.raises(GmailTransportError) as exc:
        await transport.list_threads(after=date(2026, 9, 1), before=date(2026, 9, 30), participants=["acme.example"], max_results=50)
    assert exc.value.code == "not_authorized" and not exc.value.retryable


# ------------------------------------------------------------ malformed MIME


async def test_malformed_mime_is_rejected_per_message_and_never_guessed(tmp_path: Path) -> None:
    service, _http, ledger, jobs = _service(tmp_path)
    await service.check_access()
    job = await _retrieve(service, jobs, [M_BAD_B64, M_NO_FROM, M_ASK])
    by_id = {r["message_id"]: r for r in job["results"]}
    assert by_id[M_BAD_B64]["outcome"] == "rejected" and by_id[M_BAD_B64]["error_code"] == "malformed_mime"
    assert by_id[M_NO_FROM]["outcome"] == "rejected" and by_id[M_NO_FROM]["error_code"] == "malformed_mime"
    assert by_id[M_ASK]["outcome"] == "imported"
    assert ledger.list_sources()["total"] == 1


async def test_transport_flags_malformed_rows_and_adapter_rejects_them(tmp_path: Path) -> None:
    transport, _http = _transport(tmp_path)
    rows = await transport.get_messages([M_BAD_B64])
    assert rows == [{"id": M_BAD_B64, "thread_id": T_ACME, "error_code": "MALFORMED"}]
    with pytest.raises(GmailImportError) as exc:
        GmailSourceAdapter().normalize(rows[0], connection_id="c")
    assert exc.value.code == "malformed_mime"
    http = FakeHttp()
    http.override(f"{GMAIL_API}/messages/{M_ASK}", HttpResponse(200, b"<html>not json</html>"))
    transport = LiveGmailReadOnlyTransport(credentials_file=_write_credentials(tmp_path), http=http)
    with pytest.raises(GmailTransportError) as exc2:
        await transport.get_messages([M_ASK])
    assert exc2.value.code == "malformed_response" and not exc2.value.retryable
    http.override(f"{GMAIL_API}/messages/{M_ASK}", _json(200, {**mailbox_fixture()["messages"][M_ASK], "id": M_GLOBEX}))
    rows = await transport.get_messages([M_ASK])
    assert rows[0]["error_code"] == "MALFORMED", "a row stamped with another id is not trusted"


async def test_html_only_body_is_text_stripped_and_attachments_are_counted_not_fetched(tmp_path: Path) -> None:
    transport, http = _transport(tmp_path)
    rows = await transport.get_messages([M_HTML_ONLY, M_ATTACH, M_REPLY])
    html_row, attach_row, reply_row = rows
    assert html_row["body_text"] == "Hello there" and "<" not in html_row["body_text"]
    assert attach_row["attachment_count"] == 1 and attach_row["body_text"] == "Deck attached."
    assert reply_row["in_reply_to"] == f"<{M_ASK}@mail.example>" and reply_row["references"] == [f"<{M_ASK}@mail.example>"]
    assert not any("attachment" in r["url"].lower() for r in http.requests)
    assert all("ATTACHMENT-ID-NEVER-FETCH" not in json.dumps(r) for r in rows)
    for row in rows:
        assert "snippet" not in row
        GmailSourceAdapter().normalize(row, connection_id="c")


# ------------------------------------------------------------ pagination + bounds


async def test_listing_pages_through_next_page_tokens_within_caps(tmp_path: Path) -> None:
    transport, http = _transport(tmp_path)
    threads = {f"19b{i:013x}": {"id": f"19b{i:013x}", "messages": [
        _meta_message(f"19c{i:013x}", f"19b{i:013x}", frm=f"p{i}@acme.example", to=MAILBOX, subject=f"s{i}", stamp=1788000000000 + i)]}
        for i in range(250)}
    http.fixture["threads"] = threads
    ids = list(threads)
    http.list_pages = [{"threads": [{"id": t, "snippet": "x"} for t in ids[i:i + 100]]} for i in range(0, 250, 100)]
    rows = await transport.list_threads(after=date(2026, 9, 1), before=date(2026, 9, 30), participants=["acme.example"], max_results=150)
    assert len(rows) == 150 and [r["id"] for r in rows] == ids[:150]
    list_calls = [r for r in http.requests if r["url"] == f"{GMAIL_API}/threads"]
    assert [c["params"]["maxResults"] for c in list_calls] == [100, 50]
    assert list_calls[1]["params"]["pageToken"] == "page-1"
    assert len(http.urls("/threads/")) == 150

    http.requests.clear()
    rows = await transport.list_threads(after=date(2026, 9, 1), before=date(2026, 9, 30), participants=["acme.example"], max_results=10_000)
    assert len(rows) == 200, "max_results is clamped to MAX_LIST_RESULTS"
    list_calls = [r for r in http.requests if r["url"] == f"{GMAIL_API}/threads"]
    assert len(list_calls) <= MAX_LIST_PAGES

    http.requests.clear()
    http.list_pages = [{"threads": [{"id": ids[0]}]}] * 10
    rows = await transport.list_threads(after=date(2026, 9, 1), before=date(2026, 9, 30), participants=["acme.example"], max_results=200)
    list_calls = [r for r in http.requests if r["url"] == f"{GMAIL_API}/threads"]
    assert len(list_calls) == MAX_LIST_PAGES and len(rows) == 1, "a provider that keeps paging is cut off and ids are deduplicated"


async def test_bounds_are_enforced_before_any_provider_request(tmp_path: Path) -> None:
    transport, http = _transport(tmp_path)
    window: dict[str, Any] = {"after": date(2026, 9, 1), "before": date(2026, 9, 30), "max_results": 50}
    cases = [
        ({**window, "participants": []}, "empty participant bound"),
        ({**window, "participants": ["not a term"]}, "malformed term"),
        ({**window, "participants": [f"p{i}@acme.example" for i in range(MAX_LIST_TERMS + 1)]}, "too many terms"),
        ({**window, "after": date(2026, 9, 30), "before": date(2026, 9, 1), "participants": ["acme.example"]}, "inverted range"),
        ({**window, "after": date(2026, 1, 1), "participants": ["acme.example"]}, "range too long"),
        ({**window, "participants": ["acme.example"], "max_results": 0}, "non-positive results"),
    ]
    for kwargs, why in cases:
        with pytest.raises(GmailTransportError) as exc:
            await transport.list_threads(**kwargs)
        assert exc.value.code == "invalid_bound", why
    for ids, why in ([f"19d{i:013x}" for i in range(MAX_MESSAGES_PER_GET + 1)], "too many ids"), (["ZZZ"], "malformed id"), ([M_ASK, "../etc"], "path-like id"):
        with pytest.raises(GmailTransportError) as exc:
            await transport.get_messages(ids)
        assert exc.value.code == "invalid_bound", why
    assert http.requests == [], "no request left the process"


async def test_listing_query_is_bounded_and_returns_metadata_only(tmp_path: Path) -> None:
    transport, http = _transport(tmp_path)
    rows = await transport.list_threads(after=date(2026, 9, 1), before=date(2026, 9, 30), participants=["acme.example", "bob@globex.example"], max_results=50)
    query = next(r for r in http.requests if r["url"] == f"{GMAIL_API}/threads")["params"]["q"]
    assert query.startswith("after:2026/09/01 before:2026/10/01 (") and "from:acme.example" in query and "to:bob@globex.example" in query
    assert {r["id"] for r in rows} == {T_ACME, T_GLOBEX}
    for row in rows:
        GmailThreadRow.model_validate(row)
        assert set(row) == {"id", "subject", "message_count", "last_message_at", "participants", "message_ids"}
        assert "SNIPPET" not in json.dumps(row) and BODY_SENTINEL not in json.dumps(row)
    acme = next(r for r in rows if r["id"] == T_ACME)
    assert acme["message_count"] == 2 and acme["message_ids"] == [M_ASK, M_REPLY] and "alice@acme.example" in acme["participants"]
    for request in http.urls("/threads/"):
        assert request["params"]["format"] == "metadata" and set(request["params"]["metadataHeaders"]) == {"From", "To", "Cc", "Subject", "Date"}
    assert not http.urls("/messages/"), "listing never fetches message bodies"


# ------------------------------------------------------------ unrelated mail


async def test_unrelated_threads_returned_by_the_provider_are_dropped_before_the_service(tmp_path: Path) -> None:
    transport, http = _transport(tmp_path)
    http.list_pages = [{"threads": [{"id": T_ACME}, {"id": T_UNRELATED}, {"id": "19a0000000000fff"}]}]
    rows = await transport.list_threads(after=date(2026, 9, 1), before=date(2026, 9, 30), participants=["acme.example"], max_results=50)
    assert [r["id"] for r in rows] == [T_ACME], "unrelated participants and missing threads are dropped"


async def test_service_lists_only_bound_threads_and_bodies_are_fetched_only_for_selected_ids(tmp_path: Path) -> None:
    service, http, ledger, jobs = _service(tmp_path)
    await service.check_access()
    listing = await service.list_threads(participants=["acme.example"], **WINDOW)
    assert [t["thread_id"] for t in listing["threads"]] == [T_ACME]
    assert listing["excluded_unrelated"] == 0 and BODY_SENTINEL not in json.dumps(listing) and "SNIPPET" not in json.dumps(listing)
    assert not http.urls("/messages/")
    job = await _retrieve(service, jobs, [M_ASK])
    assert job["counts"] == {"imported": 1} and job["results"][0]["proposed_candidates"] >= 1
    fetched = [r["url"].rsplit("/", 1)[1] for r in http.urls("/messages/")]
    assert fetched == [M_ASK]
    assert BODY_SENTINEL not in json.dumps(job) and BODY_SENTINEL not in json.dumps(listing)
    assert BODY_SENTINEL in json.dumps(ledger.read_content(job["results"][0]["source_id"], revision=1))
    _no_secret_leak(json.dumps(job), json.dumps(listing), json.dumps(service.connection()))
