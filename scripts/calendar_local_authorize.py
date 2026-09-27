#!/usr/bin/env python3
"""Authorize the optional local Google Calendar reader with events.readonly only.

Run: uv run python scripts/calendar_local_authorize.py --client-file <Desktop OAuth JSON>
Then start SE Skills with SE_CALENDAR_TRANSPORT=live_readonly. The refresh token
stays under the user's home/profile, separate from any Gmail authorization.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import threading
import webbrowser
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "webapp"))
from integrations.calendar_readonly import (  # noqa: E402
    CREDENTIALS_CONTRACT, READONLY_SCOPE, credentials_path, load_credentials,
)
from integrations.gmail_live import AUTH_URL, HttpxClient, REVOKE_URL, TOKEN_URL, validate_credentials_path  # noqa: E402
from services.private_store import atomic_write_private, mkdir_private  # noqa: E402


class Callback(BaseHTTPRequestHandler):
    result: dict[str, list[str]] = {}
    done = threading.Event()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/callback":
            self.send_error(404)
            return
        type(self).result = parse_qs(parsed.query)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SE Skills: Calendar authorization received. You can close this tab.")
        type(self).done.set()

    def log_message(self, *_args) -> None:
        pass


async def authorize(client_file: Path, *, timeout: int = 300) -> None:
    from gmail_local_authorize import load_client_file, pkce_pair

    target = validate_credentials_path(credentials_path())
    client_id, client_secret = load_client_file(client_file)
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    Callback.done.clear()
    Callback.result = {}
    server = HTTPServer(("127.0.0.1", 0), Callback)
    redirect = f"http://127.0.0.1:{server.server_port}/callback"
    url = f"{AUTH_URL}?{urlencode({'client_id': client_id, 'redirect_uri': redirect, 'response_type': 'code', 'scope': READONLY_SCOPE, 'access_type': 'offline', 'prompt': 'consent', 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256'})}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print("Waiting for Calendar read-only consent in your browser.")
    if not webbrowser.open(url):
        print(f"Open this URL in your browser: {url}")
    try:
        if not await asyncio.to_thread(Callback.done.wait, timeout):
            raise ValueError("authorization timed out")
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    result = Callback.result
    if result.get("state", [None])[0] != state or "error" in result:
        raise ValueError("authorization was denied or state did not match")
    code = result.get("code", [None])[0]
    if not code:
        raise ValueError("authorization returned no code")
    response = await HttpxClient().request("POST", TOKEN_URL, data={
        "grant_type": "authorization_code", "code": code, "client_id": client_id,
        "client_secret": client_secret, "redirect_uri": redirect, "code_verifier": verifier,
    })
    payload = json.loads(response.body) if response.status == 200 else {}
    if not isinstance(payload, dict) or not payload.get("refresh_token"):
        raise ValueError("token exchange returned no refresh token")
    if sorted(str(payload.get("scope", "")).split()) != [READONLY_SCOPE]:
        raise ValueError("grant was not exactly Calendar events.readonly")
    mkdir_private(target.parent)
    atomic_write_private(target, json.dumps({
        "contract": CREDENTIALS_CONTRACT, "client_id": client_id,
        "client_secret": client_secret, "refresh_token": payload["refresh_token"],
        "scopes": [READONLY_SCOPE], "authorized_at": datetime.now(UTC).isoformat(),
    }).encode())
    print("Calendar read-only authorization saved under your profile.")


async def revoke() -> None:
    target = credentials_path()
    token = load_credentials(target)["refresh_token"]
    response = await HttpxClient().request("POST", REVOKE_URL, data={"token": token})
    if response.status != 200:
        raise ValueError("Google did not confirm revocation")
    target.unlink()
    print("Calendar authorization revoked and local credential removed.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--client-file", type=Path)
    group.add_argument("--status", action="store_true")
    group.add_argument("--revoke", action="store_true")
    args = parser.parse_args()
    try:
        if args.status:
            saved = load_credentials(credentials_path())
            print(f"Calendar authorization present: {saved.get('authorized_at', 'unknown')}")
        elif args.revoke:
            asyncio.run(revoke())
        else:
            asyncio.run(authorize(args.client_file))
    except Exception as exc:
        print(f"Calendar authorization failed: {type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
