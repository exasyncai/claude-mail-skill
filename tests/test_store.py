import json
import os
import stat
import sys

from mailskill import store


def test_accounts_round_trip(home, memkeyring):
    assert store.list_accounts() == []
    a = store.Account(address="Bob@Example.com", host="imap.example.com", port=993, security="ssl")
    store.save_account(a)
    assert store.default_address() == "bob@example.com"
    assert store.get_account().address == "Bob@Example.com"
    assert store.get_account("bob").host == "imap.example.com"         # unique prefix
    b = store.Account(address="x@y.z", host="h", port=143, security="starttls", username="xuser")
    store.save_account(b)
    assert store.get_account("x@y.z").login == "xuser"
    assert store.get_account().address == "Bob@Example.com"             # default unchanged
    store.save_account(b, make_default=True)
    assert store.default_address() == "x@y.z"
    assert store.get_account("nope") is None
    assert store.get_account("b") is None or store.get_account("b").address == "Bob@Example.com"


def test_settings_file_has_no_secret(home, memkeyring):
    store.save_account(store.Account(address="a@b.c", host="h", port=993, security="ssl"))
    store.set_secret("a@b.c", "hunter2-not-real")
    text = store.accounts_file().read_text(encoding="utf-8")
    assert "hunter2" not in text
    assert json.loads(text)["accounts"]["a@b.c"]["host"] == "h"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(store.accounts_file()).st_mode) == 0o600


def test_secret_keyring_and_env(home, memkeyring, monkeypatch):
    assert store.get_secret("a@b.c") is None
    assert store.set_secret("a@b.c", "s3") == "keyring"
    assert store.get_secret("A@B.C") == "s3"
    monkeypatch.setenv(store.PW_ENV, "from-env")
    assert store.get_secret("a@b.c") == "from-env"      # env wins (containers, CI)
    monkeypatch.delenv(store.PW_ENV)
    store.delete_secret("a@b.c")
    assert store.get_secret("a@b.c") is None


def test_remove_account_removes_secret(home, memkeyring):
    store.save_account(store.Account(address="a@b.c", host="h", port=993, security="ssl"))
    store.set_secret("a@b.c", "x")
    assert store.remove_account("a@b.c") is True
    assert store.get_secret("a@b.c") is None
    assert store.list_accounts() == []
    assert store.remove_account("a@b.c") is False


def test_no_keyring_backend(home, monkeypatch):
    monkeypatch.setattr(store, "_keyring", lambda: None)
    assert "none" in store.keyring_backend_name()
    try:
        store.set_secret("a@b.c", "x")
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "keyring" in str(e)
    assert store.get_secret("a@b.c") is None


def test_corrupt_settings_file_is_tolerated(home, memkeyring):
    home.mkdir(parents=True)
    store.accounts_file().write_text("{not json", encoding="utf-8")
    assert store.list_accounts() == []
