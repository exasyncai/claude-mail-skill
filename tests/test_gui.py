"""The window itself, driven without a person: needs a display, otherwise skipped."""
import time

import pytest

from fake_imap import FakeIMAP
from mailskill import setup, store
from mailskill.discover import Candidate, Discovery

tk = pytest.importorskip("tkinter")


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except Exception as e:  # no display (CI on Linux without Xvfb)
        pytest.skip(f"no display: {e}")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:
        pass


def pump(root, dlg, seconds=2.0, until=lambda: False):
    end = time.time() + seconds
    while time.time() < end and not until():
        root.update()
        time.sleep(0.02)


def disc(chosen):
    return lambda addr, verbose=None: Discovery(addr, addr.split("@")[1], [], [chosen] if chosen else [], chosen, [])


def test_dialog_hides_password_for_microsoft_and_signs_in(root, home, memkeyring, monkeypatch):
    from mailskill import gui

    fake = FakeIMAP()
    monkeypatch.setattr(gui, "DEBOUNCE_MS", 10)
    monkeypatch.setattr(gui, "CLOSE_AFTER_MS", 50)
    c = Candidate(host="outlook.office365.com", port=993, security="ssl", source="mx", provider="microsoft365", verified=True)
    dlg = gui.AddDialog(root, client_id="cid", discover_fn=disc(c), mailbox_cls=lambda acc, secret: _FakeMailbox(fake, acc, secret),
                        sign_in=lambda addr, cid, status: {"access_token": "at", "refresh_token": "rt"})
    assert dlg.pw_entry.winfo_manager() == "grid"
    dlg.addr.set("bob@outlook.com")
    dlg._on_typing()
    root.update()
    assert dlg.pw_entry.winfo_manager() == "" and dlg.button.cget("text") == "Sign in with Microsoft"
    dlg._connect()
    pump(root, dlg, until=lambda: dlg.result is not None)
    assert dlg.result and dlg.result["auth"] == "oauth" and dlg.result["folders"] == 1
    assert store.get_refresh_token("bob@outlook.com") == "rt" and store.get_account("bob@outlook.com").auth == "oauth"


def test_dialog_password_path_and_gmail_link(root, home, memkeyring, monkeypatch):
    from mailskill import gui

    fake = FakeIMAP()
    monkeypatch.setattr(gui, "DEBOUNCE_MS", 10)
    monkeypatch.setattr(gui, "CLOSE_AFTER_MS", 50)
    c = Candidate(host="imap.gmail.com", port=993, security="ssl", source="mx", provider="gmail", verified=True)
    dlg = gui.AddDialog(root, discover_fn=disc(c), mailbox_cls=lambda acc, secret: _FakeMailbox(fake, acc, secret))
    dlg.addr.set("bob@gmail.com")
    dlg._on_typing()
    pump(root, dlg, until=lambda: dlg.plan is not None and dlg.plan.candidate is not None)
    assert dlg.pw_entry.winfo_manager() == "grid" and "apppasswords" in dlg.link.cget("text")
    dlg._connect()
    root.update()
    assert "password" in dlg.status.cget("text").lower()          # empty password: asks, does nothing
    dlg.pw.set("app-pw")
    dlg._connect()
    pump(root, dlg, until=lambda: dlg.result is not None)
    assert dlg.result["auth"] == "password" and store.get_secret("bob@gmail.com") == "app-pw"


def test_dialog_login_refused_shows_hint_and_stays_open(root, home, memkeyring, monkeypatch):
    from mailskill import gui

    fake = FakeIMAP(login_ok=False)
    monkeypatch.setattr(gui, "DEBOUNCE_MS", 10)
    c = Candidate(host="imap.gmail.com", port=993, security="ssl", source="mx", provider="gmail", verified=True)
    dlg = gui.AddDialog(root, discover_fn=disc(c), mailbox_cls=lambda acc, secret: _FakeMailbox(fake, acc, secret), preset_address="bob@gmail.com")
    pump(root, dlg, until=lambda: dlg.plan is not None and dlg.plan.candidate is not None)
    dlg.pw.set("wrong")
    dlg._connect()
    pump(root, dlg, until=lambda: "refused" in dlg.status.cget("text"))
    assert "login refused" in dlg.status.cget("text") and dlg.result is None and not dlg._busy
    assert store.list_accounts() == []


class _FakeMailbox(setup.Mailbox):
    def __init__(self, fake, acc, secret):
        super().__init__(acc, secret)
        self._fake = fake

    def connect(self):
        import imaplib

        from mailskill.mailbox import MailboxError

        self.conn = self._fake
        try:
            if self.account.auth == "oauth":
                self._fake.authenticate("XOAUTH2", lambda _: b"user=xauth=Bearer y")
            else:
                self._fake.login(self.account.login, self._secret)
        except imaplib.IMAP4.error as e:
            raise MailboxError(f"login refused for {self.account.login} at {self.account.host}: {e}") from e
        return self
