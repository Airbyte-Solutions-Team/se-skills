#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28.1", "pydantic>=2.0"]
# ///
"""Authorize the local Gmail read-only route in the user's own browser session.

Runs Google's installed-app (loopback) OAuth flow with exactly one scope,
`https://www.googleapis.com/auth/gmail.readonly`, PKCE and a random state, and
writes the resulting refresh token to a 0600 file in the user's context
(`~/.config/se-skills/gmail-oauth.json`, or `SE_GMAIL_OAUTH_FILE`). Nothing is sent
anywhere but Google, nothing is written to the repo, and no token is ever printed.

Prerequisite the user owns: a Google Cloud project with the Gmail API enabled and an
OAuth client of type **Desktop app** whose downloaded JSON is passed as
`--client-file`. The consent screen must list the gmail.readonly scope; while the
app is in "Testing" the signing-in account must be added as a test user.

Modes (each exits 0 only on success, prints statuses and never secrets):

  --client-file <path>   run the consent flow and write the credential file
  --status               report whether a credential file exists, its scopes and
                         authorization time
  --revoke               revoke the stored grant at Google and delete the file
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import threading
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import parse_qs, urlparse

repo_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(repo_root / "webapp"))

from integrations.gmail import READONLY_SCOPE
from integrations.gmail_live import (
    CREDENTIALS_CONTRACT,
    REVOKE_URL,
    TOKEN_URL,
    GmailTransportError,
    HttpClient,
    HttpxClient,
    authorization_url,
    credentials_path,
    load_local_credentials,
)
from services.private_store import atomic_write_private, mkdir_private

LOOPBACK_HOST = "127.0.0.1"


class AuthorizeError(Exception):
    pass


def load_client_file(path: Path) -> tuple[str, str]:
    """Reads a downloaded Desktop-app OAuth client JSON; returns (client_id, client_secret)."""
    try:
        raw = json.loads(path.read_bytes()[:65_536])
    except (OSError, ValueError) as exc:
        raise AuthorizeError("client file could not be read as JSON") from exc
    installed = raw.get("installed") if isinstance(raw, dict) else None
    if not isinstance(installed, dict):
        raise AuthorizeError("client file is not a Desktop-app ('installed') OAuth client")
    client_id, client_secret = installed.get("client_id"), installed.get("client_secret")
    if not (isinstance(client_id, str) and client_id and isinstance(client_secret, str) and client_secret):
        raise AuthorizeError("client file lacks client_id/client_secret")
    return client_id, client_secret


def pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


async def exchange_code(
    http: HttpClient, *, code: str, client_id: str, client_secret: str, redirect_uri: str, code_verifier: str
) -> dict[str, Any]:
    """Exchanges the authorization code; returns the credential-file payload (never printed)."""
    response = await http.request(
        "POST",
        TOKEN_URL,
        headers={"Accept": "application/json"},
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    )
    try:
        payload = json.loads(response.body or b"{}")
    except ValueError:
        payload = {}
    if response.status != 200 or not isinstance(payload, dict):
        raise AuthorizeError(f"token exchange failed with status {response.status}")
    refresh = payload.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise AuthorizeError("token exchange returned no refresh token (re-run; consent must be granted afresh)")
    granted = sorted(s for s in str(payload.get("scope", "")).split() if s)
    if granted != [READONLY_SCOPE]:
        raise AuthorizeError("granted scopes are not exactly gmail.readonly; nothing was stored")
    return {
        "contract": CREDENTIALS_CONTRACT,
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "scopes": granted,
        "authorized_at": datetime.now(UTC).isoformat(),
    }


def write_credentials(path: Path, payload: dict[str, Any]) -> None:
    mkdir_private(path.parent)
    atomic_write_private(path, json.dumps(payload, indent=2).encode("utf-8"))


class _Callback(BaseHTTPRequestHandler):
    result: ClassVar[dict[str, list[str]]] = {}
    done = threading.Event()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        type(self).result = parse_qs(parsed.query)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"SE Skills: Gmail read-only authorization received. You can close this tab.")
        type(self).done.set()

    def log_message(self, *_args: Any) -> None:
        return


def run_consent_flow(client_file: Path, target: Path, *, timeout: float = 300.0) -> int:
    client_id, client_secret = load_client_file(client_file)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    server = HTTPServer((LOOPBACK_HOST, 0), _Callback)
    redirect_uri = f"http://{LOOPBACK_HOST}:{server.server_port}/callback"
    url = authorization_url(client_id=client_id, redirect_uri=redirect_uri, state=state, code_challenge=challenge)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"status: waiting_for_consent scope=gmail.readonly redirect={redirect_uri}")
    print("If no browser opened, paste this URL into the browser where you are signed in to the mailbox:")
    print(url)
    webbrowser.open(url)
    try:
        if not _Callback.done.wait(timeout):
            print("status: timed_out")
            return 2
    finally:
        server.shutdown()
    result = _Callback.result
    if result.get("state", [None])[0] != state:
        print("status: state_mismatch (nothing stored)")
        return 2
    if "error" in result:
        print("status: consent_denied (nothing stored)")
        return 2
    code = result.get("code", [None])[0]
    if not code:
        print("status: no_code (nothing stored)")
        return 2
    payload = asyncio.run(
        exchange_code(
            HttpxClient(), code=code, client_id=client_id, client_secret=client_secret,
            redirect_uri=redirect_uri, code_verifier=verifier,
        )
    )
    write_credentials(target, payload)
    print(f"status: authorized scopes={payload['scopes']} file={target} mode=0600")
    print("Next: start the app with SE_GMAIL_TRANSPORT=live_readonly and run 'Check access' on the Sources page.")
    return 0


def report_status(target: Path) -> int:
    try:
        credentials = load_local_credentials(target)
    except GmailTransportError as exc:
        print(f"status: not_authorized code={exc.code} file={target}")
        return 1
    mode = oct(target.stat().st_mode & 0o777) if os.name == "posix" else "n/a"
    print(f"status: authorized scopes={list(credentials.scopes)} authorized_at={credentials.authorized_at} file={target} mode={mode}")
    return 0


async def revoke(http: HttpClient, target: Path) -> int:
    try:
        credentials = load_local_credentials(target)
    except GmailTransportError as exc:
        print(f"status: nothing_to_revoke code={exc.code}")
        return 1
    response = await http.request("POST", REVOKE_URL, data={"token": credentials.refresh_token})
    target.unlink(missing_ok=True)
    print(f"status: revoked_at_google={response.status == 200} file_deleted=True")
    return 0 if response.status == 200 else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--client-file", type=Path, help="downloaded Desktop-app OAuth client JSON")
    group.add_argument("--status", action="store_true")
    group.add_argument("--revoke", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds to wait for consent")
    args = parser.parse_args(argv)
    target = credentials_path()
    try:
        if args.status:
            return report_status(target)
        if args.revoke:
            return asyncio.run(revoke(HttpxClient(), target))
        return run_consent_flow(args.client_file, target, timeout=args.timeout)
    except (AuthorizeError, GmailTransportError) as exc:
        print(f"status: failed detail={exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
