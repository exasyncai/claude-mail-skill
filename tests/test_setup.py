import pytest

from fake_imap import FakeIMAP
from mailskill import oauth, setup, store
from mailskill.discover import Candidate, Discovery


def disc(chosen, notes=(), cands=None):
    def fn(addr, verbose=None):
        return Discovery(addr, addr.split("@")[1], [], cands or ([chosen] if chosen else []), chosen, list(notes))
    return fn


def test_quick_kind_without_network():
    assert setup.quick_kind("a@outlook.com") == "oauth" and setup.quick_kind("a@hotmail.de") == "oauth"
    assert setup.quick_kind("a@gmail.com") == "password"
    assert setup.quick_kind("a@firma.example") == "" and setup.quick_kind("nonsense") == ""


def test_plan_microsoft_is_oauth():
    c = Candidate(host="outlook.office365.com", port=993, security="ssl", source="mx", provider="microsoft365", verified=True)
    p = setup.plan_for("Bob@Corp.example", discover_fn=disc(c, ["m365 note"]))
    assert p.kind == "oauth" and p.address == "bob@corp.example" and p.notes == ["m365 note"]
    v = p.view
    assert v["password_field"] is False and v["button"] == "Sign in with Microsoft"
    acc = setup.account_for(p)
    assert acc.auth == "oauth" and acc.host == "outlook.office365.com"


def test_plan_gmail_has_app_password_link():
    c = Candidate(host="imap.gmail.com", port=993, security="ssl", source="mx", provider="gmail", verified=True)
    p = setup.plan_for("bob@gmail.com", discover_fn=disc(c))
    assert p.kind == "password" and "app password" in p.hint and p.link.startswith("https://myaccount.google.com")
    assert p.view["password_field"] is True and p.view["link"] == p.link


def test_plan_unknown_hoster_is_plain_password():
    c = Candidate(host="imap.hoster.example", port=993, security="ssl", source="ispdb", verified=True, username="bob.user")
    p = setup.plan_for("bob@firma.example", discover_fn=disc(c))
    assert p.kind == "password" and p.hint == "" and setup.account_for(p).login == "bob.user"


def test_plan_manual_host_skips_discovery():
    p = setup.plan_for("bob@firma.example", host="127.0.0.1", port=1143, starttls=True,
                       discover_fn=lambda a, verbose=None: (_ for _ in ()).throw(AssertionError("no lookup")))
    assert p.kind == "password" and p.candidate.security == "starttls" and p.candidate.port == 1143


def test_plan_nothing_found():
    p = setup.plan_for("bob@dead.example", discover_fn=disc(None, cands=[Candidate("imap.dead.example", 993, "ssl", "guess")]))
    assert p.kind == "none" and "--host" in p.hint and p.tried == ["imap.dead.example:993 ssl (guess)"]
    assert p.view["password_field"] is False


def test_plan_gateway_hint():
    p = setup.plan_for("bob@corp.example", discover_fn=disc(None, ["The MX points at a mail security gateway (Mimecast)."]))
    assert p.kind == "none" and "gateway" in p.hint


def test_plan_rejects_bad_address():
    with pytest.raises(setup.SetupError):
        setup.plan_for("nope", discover_fn=disc(None))


def test_test_login_error_carries_provider_hint(monkeypatch):
    import mailskill.mailbox as mm

    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: FakeIMAP(login_ok=False))
    acc = store.Account(address="bob@gmail.com", host="imap.gmail.com", port=993, security="ssl", provider="gmail")
    with pytest.raises(setup.SetupError) as e:
        setup.test_login(acc, "wrong")
    assert e.value.code == "login" and "app password" in e.value.hint and e.value.link


def test_finish_password_stores_both(home, memkeyring):
    acc = store.Account(address="bob@firma.example", host="h", port=993, security="ssl")
    assert setup.finish_password(acc, "pw-x") == "keyring"
    assert store.get_secret("bob@firma.example") == "pw-x" and store.default_address() == "bob@firma.example"


def test_sign_in_microsoft_stores_refresh_token_not_access_token(home, memkeyring, monkeypatch):
    import mailskill.mailbox as mm

    fake = FakeIMAP()
    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    c = Candidate(host="outlook.office365.com", port=993, security="ssl", source="mx", provider="microsoft365", verified=True)
    p = setup.plan_for("bob@corp.example", discover_fn=disc(c))
    acc, folders, where = setup.sign_in_microsoft(p, client_id="cid-1", sign_in=lambda addr, cid, status: {"access_token": "at-secret-1", "refresh_token": "rt-secret-1"})
    assert where == "keyring" and acc.auth == "oauth" and len(folders) == 1
    assert store.get_refresh_token("bob@corp.example") == "rt-secret-1"
    assert store.get_secret("bob@corp.example") is None            # no password entry for an oauth account
    assert "secret-1" not in store.accounts_file().read_text(encoding="utf-8")
    assert fake.log[-2].startswith("AUTHENTICATE XOAUTH2 user=bob@corp.example auth=Bearer at-secret-1")


def test_sign_in_microsoft_without_client_id(home, memkeyring, monkeypatch):
    monkeypatch.delenv(oauth.MS_CLIENT_ID_ENV, raising=False)
    monkeypatch.setattr(oauth, "MS_CLIENT_ID", "")
    p = setup.Plan(address="bob@corp.example", kind="oauth", provider="microsoft365",
                   candidate=Candidate(host="outlook.office365.com", port=993, security="ssl", source="mx"))
    with pytest.raises(oauth.OAuthError) as e:
        setup.sign_in_microsoft(p, sign_in=lambda *a, **k: {})
    assert e.value.code == "no_client_id"


def test_access_token_refreshes_and_rotates(home, memkeyring):
    acc = store.Account(address="bob@corp.example", host="outlook.office365.com", port=993, security="ssl", auth="oauth")
    store.save_account(acc)
    store.set_refresh_token(acc.address, "rt-1")
    calls = []

    def refresh(cid, rt):
        calls.append((cid, rt))
        return {"access_token": "at-9", "refresh_token": "rt-2"}

    assert setup.access_token(acc, client_id="cid", refresh=refresh) == "at-9"
    assert calls == [("cid", "rt-1")] and store.get_refresh_token(acc.address) == "rt-2"


def test_access_token_without_stored_sign_in(home, memkeyring):
    acc = store.Account(address="bob@corp.example", host="h", port=993, security="ssl", auth="oauth")
    with pytest.raises(setup.SetupError) as e:
        setup.access_token(acc, client_id="cid", refresh=lambda c, r: {})
    assert e.value.code == "no_token" and "mailskill add" in str(e.value)


def test_remove_account_drops_refresh_token(home, memkeyring):
    acc = store.Account(address="bob@corp.example", host="h", port=993, security="ssl", auth="oauth")
    store.save_account(acc)
    store.set_refresh_token(acc.address, "rt")
    assert store.remove_account("bob@corp.example") and store.get_refresh_token("bob@corp.example") is None


def test_old_accounts_file_without_auth_field(home, memkeyring):
    home.mkdir(parents=True)
    store.accounts_file().write_text('{"default": "a@b.c", "accounts": {"a@b.c": {"address": "a@b.c", "host": "h", "port": 993, "security": "ssl"}}}', encoding="utf-8")
    assert store.get_account().auth == "password"
