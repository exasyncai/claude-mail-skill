import base64
import hashlib
import threading
import urllib.request

import pytest

from fake_imap import FakeIMAP
from mailskill import oauth
from mailskill.mailbox import Mailbox, MailboxError
from mailskill.store import Account

CID = "11111111-2222-3333-4444-555555555555"


def test_pkce_pair_is_s256():
    v, c = oauth.pkce_pair()
    assert 43 <= len(v) <= 128 and all(ch.isalnum() or ch in "-_" for ch in v)
    assert c == base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    assert oauth.pkce_pair()[0] != v


def test_xoauth2_string():
    assert oauth.xoauth2_string("bob@example.com", "tok") == b"user=bob@example.com\x01auth=Bearer tok\x01\x01"


def test_client_id_sources(monkeypatch):
    monkeypatch.delenv(oauth.MS_CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(oauth, "MS_CLIENT_ID", "")
    with pytest.raises(oauth.OAuthError) as e:
        oauth.client_id()
    assert e.value.code == "no_client_id"
    monkeypatch.setenv(oauth.MS_CLIENT_ID_ENV, "from-env")
    assert oauth.client_id() == "from-env"
    assert oauth.client_id("explicit") == "explicit"
    monkeypatch.setattr(oauth, "MS_CLIENT_ID", "built-in")
    monkeypatch.delenv(oauth.MS_CLIENT_ID_ENV)
    assert oauth.client_id() == "built-in"


def test_authorize_url_has_pkce_and_scopes():
    url = oauth.authorize_url(CID, "http://localhost:5000/", "chal", "st", login_hint="bob@example.com")
    assert url.startswith(oauth.MS_AUTHORITY + "/authorize?")
    assert "code_challenge=chal" in url and "code_challenge_method=S256" in url and "state=st" in url
    assert "IMAP.AccessAsUser.All" in url and "offline_access" in url and "login_hint=bob%40example.com" in url
    assert "client_secret" not in url


class TokenEndpoint:
    """Stands in for post_form; records requests, answers from a script."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, data, timeout=0):
        self.calls.append((url, dict(data)))
        return self.answers.pop(0)


def test_exchange_code_sends_verifier_and_returns_tokens():
    ep = TokenEndpoint([{"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600}])
    t = oauth.exchange_code(CID, "the-code", "the-verifier", "http://localhost:1234/", post=ep)
    assert t["access_token"] == "at-1" and t["refresh_token"] == "rt-1"
    url, form = ep.calls[0]
    assert url.endswith("/token") and form["grant_type"] == "authorization_code" and form["code"] == "the-code"
    assert form["code_verifier"] == "the-verifier" and form["redirect_uri"] == "http://localhost:1234/" and "client_secret" not in form


def test_exchange_without_refresh_token_is_an_error():
    ep = TokenEndpoint([{"access_token": "at-1"}])
    with pytest.raises(oauth.OAuthError) as e:
        oauth.exchange_code(CID, "c", "v", "http://localhost:1/", post=ep)
    assert e.value.code == "no_refresh_token" and "offline_access" in str(e.value)


def test_refresh_rotates_token():
    ep = TokenEndpoint([{"access_token": "at-2", "refresh_token": "rt-2"}])
    t = oauth.refresh(CID, "rt-1", post=ep)
    assert t["access_token"] == "at-2" and t["refresh_token"] == "rt-2"
    assert ep.calls[0][1]["grant_type"] == "refresh_token" and ep.calls[0][1]["refresh_token"] == "rt-1"


def test_invalid_grant_says_sign_in_again():
    ep = TokenEndpoint([{"error": "invalid_grant", "error_description": "AADSTS70000: token expired or revoked"}])
    with pytest.raises(oauth.OAuthError) as e:
        oauth.refresh(CID, "rt-old", post=ep)
    assert e.value.code == "invalid_grant" and "mailskill add" in str(e.value)


def test_admin_consent_error_carries_the_admin_url():
    ep = TokenEndpoint([{"error": "invalid_grant",
                         "error_description": "AADSTS65001: The user or administrator has not consented to use the application"}])
    with pytest.raises(oauth.OAuthError) as e:
        oauth.exchange_code(CID, "c", "v", "http://localhost:1/", post=ep)
    assert e.value.code == "admin_consent"
    assert e.value.admin_consent_url == "https://login.microsoftonline.com/common/adminconsent?client_id=" + CID
    assert "admin" in str(e.value).lower() and CID in str(e.value)


def test_unknown_client_id_is_explained():
    ep = TokenEndpoint([{"error": "unauthorized_client", "error_description": "AADSTS700016: Application not found"}])
    with pytest.raises(oauth.OAuthError) as e:
        oauth.refresh(CID, "rt", post=ep)
    assert e.value.code == "unauthorized_client" and "client id" in str(e.value)


def _wait_listening(port, seconds=10.0):
    """The receiver binds in its own thread; on a slow runner (macOS CI) it is not up after a fixed sleep."""
    import socket
    import time
    end = time.time() + seconds
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read()
    except Exception as e:  # pragma: no cover
        return -1, str(e).encode()


def test_receive_code_over_loopback():
    port = oauth.free_port()
    out = {}

    def listen():
        try:
            out["code"] = oauth.receive_code(port, "st-1", timeout=10)
        except oauth.OAuthError as e:
            out["err"] = e

    t = threading.Thread(target=listen, daemon=True)
    t.start()
    assert _wait_listening(port)
    status, body = _get(f"http://127.0.0.1:{port}/?code=abc123&state=st-1")
    t.join(5)
    assert status == 200 and b"Done" in body and out.get("code") == "abc123"


def test_receive_code_rejects_wrong_state_and_reports_error_param():
    for query, needle in (("?code=abc&state=other", "state mismatch"), ("?error=access_denied&error_description=user+said+no&state=st", "access_denied")):
        port = oauth.free_port()
        out = {}

        def listen(port=port):
            try:
                out["code"] = oauth.receive_code(port, "st", timeout=10)
            except oauth.OAuthError as e:
                out["err"] = e

        t = threading.Thread(target=listen, daemon=True)
        t.start()
        assert _wait_listening(port)
        status, body = _get(f"http://127.0.0.1:{port}/{query}")
        t.join(5)
        assert status == 200 and b"did not complete" in body
        assert "err" in out and needle in str(out["err"])


def test_sign_in_end_to_end_with_fakes():
    ep = TokenEndpoint([{"access_token": "at", "refresh_token": "rt"}])
    seen = {}

    def open_url(url):
        seen["url"] = url
        return True

    def listen(port, state):
        seen["port"], seen["state"] = port, state
        return "code-xyz"

    lines = []
    t = oauth.sign_in("bob@example.com", CID, open_url=open_url, listen=listen, post=ep, status=lines.append)
    assert t["refresh_token"] == "rt"
    import urllib.parse
    assert urllib.parse.quote(f"http://localhost:{seen['port']}/", safe="") in seen["url"] and seen["state"] in seen["url"]
    assert ep.calls[0][1]["code"] == "code-xyz" and ep.calls[0][1]["redirect_uri"] == f"http://localhost:{seen['port']}/"
    assert any("browser" in line.lower() for line in lines)


def test_sign_in_prints_url_when_no_browser():
    ep = TokenEndpoint([{"access_token": "at", "refresh_token": "rt"}])
    lines = []
    oauth.sign_in("bob@example.com", CID, open_url=lambda u: False, listen=lambda p, s: "c", post=ep, status=lines.append)
    assert any("Open this address yourself" in line for line in lines)


def test_mailbox_authenticates_with_xoauth2(monkeypatch):
    import mailskill.mailbox as mm

    fake = FakeIMAP()
    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    acc = Account(address="bob@example.com", host="outlook.office365.com", port=993, security="ssl", auth="oauth")
    mb = Mailbox(acc, "access-token-1")
    mb.connect()
    assert fake.log[-1] == "AUTHENTICATE XOAUTH2 user=bob@example.com auth=Bearer access-token-1"
    mb.close()


def test_mailbox_xoauth2_refused_explains_imap_disabled(monkeypatch):
    import mailskill.mailbox as mm

    fake = FakeIMAP(login_ok=False)
    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    acc = Account(address="bob@example.com", host="outlook.office365.com", port=993, security="ssl", auth="oauth")
    with pytest.raises(MailboxError) as e:
        Mailbox(acc, "tok").connect()
    assert "IMAP is switched off" in str(e.value) and "mailskill add" in str(e.value)
