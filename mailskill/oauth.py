"""Microsoft 365 / Outlook.com sign-in for IMAP (OAuth 2.0 authorization code flow with PKCE).

Microsoft switched password logins over IMAP off in 2022; a token is the only way in.
This module gets one with as little as possible from the user: the browser opens, they
sign in at Microsoft, a tiny local web server on 127.0.0.1 receives the code, and the
refresh token goes into the OS keychain. From then on every command refreshes the access
token silently. No device code, no secret: the app is registered as a public client.

Network is isolated in `post_form` and `receive_code` so the tests can replace them.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import secrets
import socket
import socketserver
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

# Registered in Entra as a public client with redirect http://localhost. Not a secret.
# Override per call with --client-id or the environment variable below.
MS_CLIENT_ID = "8f6b979a-d88e-4192-9564-ff5d311d0fa6"
MS_CLIENT_ID_ENV = "MAILSKILL_MS_CLIENT_ID"
MS_AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
MS_SCOPES = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
MS_IMAP_HOST = "outlook.office365.com"
MS_IMAP_PORT = 993
TIMEOUT = 20
SIGNIN_WAIT = 300      # seconds the local receiver waits for the browser

ADMIN_CONSENT_CODES = ("AADSTS65001", "AADSTS900971", "AADSTS650052", "AADSTS90094")


class OAuthError(RuntimeError):
    """Sign-in or token problem with a message written for the person at the keyboard."""

    def __init__(self, message: str, code: str = "", admin_consent_url: str = ""):
        super().__init__(message)
        self.code = code
        self.admin_consent_url = admin_consent_url


# ------------------------------------------------------------------ pure helpers


def client_id(override: str | None = None) -> str:
    import os

    cid = (override or os.environ.get(MS_CLIENT_ID_ENV) or MS_CLIENT_ID).strip()
    if not cid:
        raise OAuthError("No Microsoft client id configured. Pass --client-id <id> or set "
                         f"{MS_CLIENT_ID_ENV} (the id of the public Entra app registration).", code="no_client_id")
    return cid


def pkce_pair() -> tuple[str, str]:
    """(code_verifier, code_challenge) as in RFC 7636, S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def xoauth2_string(address: str, access_token: str) -> bytes:
    """The SASL XOAUTH2 initial response (imaplib base64-encodes it)."""
    return f"user={address}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")


def admin_consent_url(cid: str) -> str:
    return "https://login.microsoftonline.com/common/adminconsent?client_id=" + urllib.parse.quote(cid)


def authorize_url(cid: str, redirect_uri: str, challenge: str, state: str, login_hint: str = "") -> str:
    q = {
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "response_mode": "query",
        "scope": MS_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    if login_hint:
        q["login_hint"] = login_hint
    return f"{MS_AUTHORITY}/authorize?" + urllib.parse.urlencode(q)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ------------------------------------------------------------------ token endpoint


def post_form(url: str, data: dict, timeout: int = TIMEOUT) -> dict:
    """POST x-www-form-urlencoded, return the JSON body for 2xx and 4xx alike (errors are JSON too)."""
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"error": "http_error", "error_description": f"HTTP {e.code}: {raw[:300]}"}
    except Exception as e:
        raise OAuthError(f"could not reach the Microsoft token endpoint: {e}", code="network") from e


def _raise_for(resp: dict, cid: str) -> None:
    err = resp.get("error")
    if not err:
        return
    desc = resp.get("error_description", "") or ""
    for c in ADMIN_CONSENT_CODES:
        if c in desc or err == "consent_required":
            url = admin_consent_url(cid)
            raise OAuthError(
                "Your organisation has not approved this app yet. Ask your Microsoft 365 admin to open this link once "
                "and click Accept (it grants read access to mailboxes over IMAP for the people who sign in):\n  " + url +
                "\nThen run the sign-in again.", code="admin_consent", admin_consent_url=url)
    if err in ("invalid_grant", "interaction_required", "login_required"):
        raise OAuthError("The Microsoft sign-in is no longer valid (revoked, expired, or the password was changed). "
                         "Run: mailskill add <address>  to sign in again.", code="invalid_grant")
    if err == "unauthorized_client" or "AADSTS700016" in desc:
        raise OAuthError("Microsoft does not know this client id (AADSTS700016). Check --client-id / "
                         f"{MS_CLIENT_ID_ENV}.", code="unauthorized_client")
    if err == "access_denied":
        raise OAuthError("Sign-in cancelled at Microsoft.", code="access_denied")
    first = desc.split("\n")[0][:300]
    raise OAuthError(f"Microsoft sign-in failed: {err}: {first}", code=err)


def exchange_code(cid: str, code: str, verifier: str, redirect_uri: str, post=post_form) -> dict:
    resp = post(f"{MS_AUTHORITY}/token", {
        "client_id": cid, "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "code_verifier": verifier, "scope": MS_SCOPES,
    })
    _raise_for(resp, cid)
    if not resp.get("access_token") or not resp.get("refresh_token"):
        raise OAuthError("Microsoft returned no refresh token. The app registration needs the offline_access permission.",
                         code="no_refresh_token")
    return resp


def refresh(cid: str, refresh_token: str, post=post_form) -> dict:
    """New tokens from a refresh token. Microsoft rotates the refresh token; store the new one."""
    resp = post(f"{MS_AUTHORITY}/token", {
        "client_id": cid, "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": MS_SCOPES,
    })
    _raise_for(resp, cid)
    if not resp.get("access_token"):
        raise OAuthError("Microsoft returned no access token on refresh.", code="no_access_token")
    return resp


# ------------------------------------------------------------------ browser round trip

_DONE_HTML = b"""<!doctype html><html><head><meta charset="utf-8"><title>claude-mail-skill</title>
<style>body{font-family:system-ui,sans-serif;margin:3em;color:#222}</style></head>
<body><h2>Done, you can close this window.</h2><p>claude-mail-skill received the sign-in. Back to the terminal.</p></body></html>"""
_FAIL_HTML = b"""<!doctype html><html><head><meta charset="utf-8"><title>claude-mail-skill</title>
<style>body{font-family:system-ui,sans-serif;margin:3em;color:#222}</style></head>
<body><h2>Sign-in did not complete.</h2><p>Close this window and look at the terminal for the reason.</p></body></html>"""


class _LoopbackServer(http.server.HTTPServer):
    """HTTPServer without the reverse DNS lookup in server_bind (socket.getfqdn), which stalls
    for ten seconds and more on some machines (macOS) and is pointless on 127.0.0.1."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = "localhost"
        self.server_port = self.server_address[1]


def receive_code(port: int, state: str, timeout: int = SIGNIN_WAIT) -> str:
    """Serve one request on 127.0.0.1:port and return the authorization code from it."""
    result: dict = {}
    got = threading.Event()

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):   # quiet
            pass

        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if self.path.startswith("/favicon"):
                self.send_response(404); self.end_headers(); return
            if q.get("state", [""])[0] != state:
                result["error"] = "state mismatch (a different sign-in answered); run the command again"
            elif q.get("error"):
                result["error"] = q["error"][0] + ": " + (q.get("error_description", [""])[0] or "")
            elif q.get("code"):
                result["code"] = q["code"][0]
            else:
                result["error"] = "no code in the redirect"
            ok = "code" in result
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_DONE_HTML if ok else _FAIL_HTML)
            got.set()

    srv = _LoopbackServer(("127.0.0.1", port), H)
    srv.timeout = 1
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    t.start()
    try:
        if not got.wait(timeout):
            raise OAuthError(f"No answer from the browser within {timeout} seconds. Run the command again.", code="timeout")
    finally:
        srv.shutdown()
        srv.server_close()
    if "error" in result:
        raise OAuthError("Microsoft sign-in did not complete: " + result["error"], code="redirect_error")
    return result["code"]


def sign_in(address: str, cid: str, *, open_url=webbrowser.open, listen=receive_code, post=post_form,
            status=lambda m: None) -> dict:
    """Full interactive sign-in. Returns the token response (access_token, refresh_token, ...)."""
    port = free_port()
    redirect_uri = f"http://localhost:{port}/"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    url = authorize_url(cid, redirect_uri, challenge, state, login_hint=address)
    status("Opening the browser for the Microsoft sign-in ...")
    opened = False
    try:
        opened = bool(open_url(url))
    except Exception:
        opened = False
    if not opened:
        status("Could not open a browser. Open this address yourself:\n  " + url)
    status("Waiting for the sign-in to come back (up to %d seconds) ..." % SIGNIN_WAIT)
    code = listen(port, state)
    status("Exchanging the code for tokens ...")
    return exchange_code(cid, code, verifier, redirect_uri, post=post)


def imap_auth_error(address: str, host: str, err: Exception) -> str:
    """Text for an IMAP AUTHENTICATE failure after a good token."""
    return (f"Microsoft accepted the sign-in but {host} refused IMAP for {address}: {err}. Usual causes: IMAP is switched off "
            "for this mailbox (admin: Exchange admin center, the mailbox, Email apps, IMAP) or the mailbox is shared and needs "
            "its own sign-in. Run: mailskill add <address>  to sign in again after that is fixed.")
