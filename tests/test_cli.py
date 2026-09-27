import json

import pytest

from fake_imap import FakeIMAP, make_msg
from mailskill import cli, store
from mailskill.discover import Candidate, Discovery


@pytest.fixture
def fake_server(home, memkeyring, monkeypatch):
    fake = FakeIMAP({
        "INBOX": {"attrs": [], "msgs": {}, "next": 1},
        "Sent": {"attrs": ["\\Sent"], "msgs": {}, "next": 1},
        "Drafts": {"attrs": ["\\Drafts"], "msgs": {}, "next": 1},
        "Archive": {"attrs": ["\\Archive"], "msgs": {}, "next": 1},
        "Trash": {"attrs": ["\\Trash"], "msgs": {}, "next": 1},
    })
    import mailskill.mailbox as mm

    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    store.save_account(store.Account(address="me@example.com", host="imap.example.com", port=993, security="ssl"))
    store.set_secret("me@example.com", "s")
    return fake


def run(capsys, *argv):
    rc = cli.main(list(argv))
    out = capsys.readouterr()
    return rc, out.out, out.err


def test_no_account(home, memkeyring, capsys):
    rc, out, err = run(capsys, "folders")
    assert rc == 3 and "mailskill add" in err


def test_folders_and_search_json(fake_server, capsys):
    fake_server.add("INBOX", make_msg(from_="alice@x.y", subject="Hello"))
    rc, out, _ = run(capsys, "folders", "--json")
    assert rc == 0 and {f["role"] for f in json.loads(out)} >= {"inbox", "sent", "drafts", "archive", "trash"}
    rc, out, _ = run(capsys, "search", "--json", "--from", "alice")
    rows = json.loads(out)
    assert rc == 0 and rows[0]["subject"] == "Hello" and rows[0]["answered"] is False
    rc, out, _ = run(capsys, "search", "--from", "nobody")
    assert rc == 0 and "No matches" in out


def test_read_replied_and_attachments(fake_server, capsys, tmp_path):
    uid = fake_server.add("INBOX", make_msg(from_="alice@x.y", subject="Offer", message_id="<o1>", body="see file", attachment=("f.txt", b"data")))
    rc, out, _ = run(capsys, "read", uid, "--json", "--save", str(tmp_path))
    d = json.loads(out)
    assert rc == 0 and d["text"].strip() == "see file" and d["attachments"][0]["filename"] == "f.txt" and (tmp_path / "f.txt").exists()
    assert "_payload" not in json.dumps(d)
    rc, out, _ = run(capsys, "replied", uid, "--json")
    assert json.loads(out)["replied"] is False
    fake_server.add("Sent", make_msg(from_="me@example.com", to="alice@x.y", subject="Re: Offer", in_reply_to="<o1>"))
    rc, out, _ = run(capsys, "replied", uid)
    assert "already replied" in out
    rc, out, _ = run(capsys, "attachments", uid, "--save", str(tmp_path / "b"), "--only", "1")
    assert rc == 0 and (tmp_path / "b" / "f.txt").exists()
    rc, out, err = run(capsys, "read", "77")
    assert rc == 1 and "no message with UID 77" in err


def test_draft_reply_to_uid(fake_server, capsys, tmp_path):
    uid = fake_server.add("INBOX", make_msg(from_="Alice <alice@x.y>", subject="Offer", message_id="<o1>"))
    body = tmp_path / "b.txt"
    body.write_text("Thanks, Alice.\n", encoding="utf-8")
    rc, out, _ = run(capsys, "draft", "--reply-to-uid", uid, "--body", str(body), "--dry-run")
    d = json.loads(out)
    assert rc == 0 and d["subject"] == "Re: Offer" and "alice@x.y" in d["to"] and d["dry_run"]
    assert len(fake_server.folders["Drafts"]["msgs"]) == 0
    rc, out, _ = run(capsys, "draft", "--reply-to-uid", uid, "--body", str(body))
    assert rc == 0 and "Nothing was sent" in out and len(fake_server.folders["Drafts"]["msgs"]) == 1
    rc, out, err = run(capsys, "draft", "--body", str(body))
    assert rc == 2


def test_cleanup_dry_run_then_apply(fake_server, capsys, tmp_path):
    fake_server.add("INBOX", make_msg(from_="news@shop.example", subject="deals"))
    fake_server.add("INBOX", make_msg(from_="alice@x.y", subject="real"))
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [{"name": "news", "from": ["news@"], "action": "move", "to": "archive"}]}), encoding="utf-8")
    rc, out, _ = run(capsys, "cleanup", "--rules", str(rules))
    assert rc == 0 and "Dry run" in out and len(fake_server.folders["INBOX"]["msgs"]) == 2
    rc, out, _ = run(capsys, "cleanup", "--rules", str(rules), "--apply", "--json")
    d = json.loads(out)
    assert d["applied"] and d["counts"] == {"move:archive": 1} and len(fake_server.folders["INBOX"]["msgs"]) == 1
    rc, out, err = run(capsys, "cleanup", "--rules", str(tmp_path / "missing.json"))
    assert rc == 2


def test_threads_dry_run(fake_server, capsys):
    fake_server.add("INBOX", make_msg(from_="alice@x.y", to="me@example.com", subject="T", message_id="<t1>", date="Mon, 21 Sep 2026 09:00:00 +0000"))
    fake_server.add("Sent", make_msg(from_="me@example.com", to="alice@x.y", subject="Re: T", in_reply_to="<t1>", date="Mon, 21 Sep 2026 10:00:00 +0000"))
    rc, out, _ = run(capsys, "threads", "--days", "3650", "--json")
    d = json.loads(out)
    assert rc == 0 and len(d["archive"]) == 1 and not d["applied"]


def test_add_with_discovery_and_env_secret(home, memkeyring, monkeypatch, capsys):
    fake = FakeIMAP({"INBOX": {"attrs": [], "msgs": {}, "next": 1}, "Sent Items": {"attrs": ["\\Sent"], "msgs": {}, "next": 1}})
    import mailskill.mailbox as mm

    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    chosen = Candidate(host="imap.example.com", port=993, security="ssl", source="ispdb", verified=True)
    monkeypatch.setattr(cli, "discover", lambda addr, verbose=None: Discovery(addr, "example.com", ["mx.example.com"], [chosen], chosen, ["note here"]))
    monkeypatch.setenv(store.PW_ENV, "env-secret")
    rc, out, err = run(capsys, "add", "bob@example.com")
    assert rc == 0, err
    assert "Found: imap.example.com:993" in out and "Login ok" in out and "note here" in out
    assert store.get_account("bob@example.com").host == "imap.example.com"
    assert memkeyring.get_password(store.SERVICE, "bob@example.com") == "env-secret"
    monkeypatch.delenv(store.PW_ENV)
    rc, out, _ = run(capsys, "accounts")
    assert "* bob@example.com" in out
    rc, out, _ = run(capsys, "remove", "bob@example.com")
    assert rc == 0 and store.list_accounts() == []


def test_add_manual_host_login_refused(home, memkeyring, monkeypatch, capsys):
    fake = FakeIMAP(login_ok=False)
    import mailskill.mailbox as mm

    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    monkeypatch.setenv(store.PW_ENV, "wrong")
    rc, out, err = run(capsys, "add", "bob@example.com", "--host", "imap.example.com")
    assert rc == 1 and "login refused" in err
    assert store.list_accounts() == []


def test_add_microsoft_account_via_browser(home, memkeyring, monkeypatch, capsys):
    fake = FakeIMAP({"INBOX": {"attrs": [], "msgs": {}, "next": 1}, "Sent Items": {"attrs": ["\\Sent"], "msgs": {}, "next": 1}})
    import mailskill.mailbox as mm
    from mailskill import setup

    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    chosen = Candidate(host="outlook.office365.com", port=993, security="ssl", source="mx", provider="microsoft365", verified=True)
    monkeypatch.setattr(cli, "discover", lambda addr, verbose=None: Discovery(addr, "corp.example", ["corp.mail.protection.outlook.com"], [chosen], chosen, ["m365"]))
    monkeypatch.setattr(setup.oauth, "sign_in", lambda addr, cid, status, **k: {"access_token": "at-live", "refresh_token": "rt-live"})
    rc, out, err = run(capsys, "add", "bob@corp.example", "--client-id", "cid-test")
    assert rc == 0, err
    assert "Signed in with Microsoft" in out and "token is stored" in out
    acc = store.get_account("bob@corp.example")
    assert acc.auth == "oauth" and acc.host == "outlook.office365.com"
    assert memkeyring.get_password(store.SERVICE, "oauth:bob@corp.example") == "rt-live"
    assert memkeyring.get_password(store.SERVICE, "bob@corp.example") is None
    rc, out, _ = run(capsys, "accounts")
    assert "(Microsoft sign-in)" in out
    # later command: silent refresh, then XOAUTH2
    monkeypatch.setattr(setup.oauth, "refresh", lambda cid, rt: {"access_token": "at-2", "refresh_token": "rt-2"})
    monkeypatch.setenv(setup.oauth.MS_CLIENT_ID_ENV, "cid-test")
    rc, out, _ = run(capsys, "folders", "--json")
    assert rc == 0 and fake.log[-2].startswith("AUTHENTICATE XOAUTH2 user=bob@corp.example auth=Bearer at-2")
    assert memkeyring.get_password(store.SERVICE, "oauth:bob@corp.example") == "rt-2"
    # revoked: refresh fails with a clear sentence, exit 3
    from mailskill.oauth import OAuthError
    monkeypatch.setattr(setup.oauth, "refresh", lambda cid, rt: (_ for _ in ()).throw(OAuthError("sign in again please", code="invalid_grant")))
    rc, out, err = run(capsys, "folders")
    assert rc == 3 and "sign in again" in err


def test_add_without_address_and_without_terminal(home, memkeyring, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    rc, out, err = run(capsys, "add", "--no-gui")
    assert rc == 2 and "mailskill add you@example.com" in err


def test_add_nothing_found(home, memkeyring, monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover", lambda addr, verbose=None: Discovery(addr, "dead.example", [], [Candidate("imap.dead.example", 993, "ssl", "guess")], None, []))
    rc, out, err = run(capsys, "add", "bob@dead.example")
    assert rc == 1 and "--host" in err


def test_version(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert "mailskill" in capsys.readouterr().out
