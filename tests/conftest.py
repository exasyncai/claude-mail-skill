import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


class MemoryKeyring:
    """Minimal keyring stand-in: set/get/delete on a dict."""

    def __init__(self):
        self.data = {}

    def set_password(self, service, user, secret):
        self.data[(service, user)] = secret

    def get_password(self, service, user):
        return self.data.get((service, user))

    def delete_password(self, service, user):
        self.data.pop((service, user), None)

    def get_keyring(self):
        return self


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MAILSKILL_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MAILSKILL_PASSWORD", raising=False)
    return tmp_path / "home"


@pytest.fixture
def memkeyring(monkeypatch):
    from mailskill import store

    kr = MemoryKeyring()
    monkeypatch.setattr(store, "_keyring", lambda: kr)
    return kr
