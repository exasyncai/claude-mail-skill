"""Everything `mailskill add` decides, without a terminal or a window attached.

The terminal command and the dialog both call this: look the address up, decide whether it
takes a password or a Microsoft sign-in, test the login, store the account. The dialog
and the CLI only render what comes back.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import oauth, store
from .discover import Candidate, Discovery, discover
from .mailbox import Mailbox, MailboxError

# domains that are Microsoft without any lookup (the dialog reacts while you type)
MS_CONSUMER_DOMAINS = {"outlook.com", "outlook.de", "outlook.fr", "outlook.es", "outlook.it", "hotmail.com", "hotmail.de",
                       "hotmail.co.uk", "hotmail.fr", "live.com", "live.de", "live.co.uk", "msn.com"}
GOOGLE_DOMAINS = {"gmail.com", "googlemail.com"}

# provider -> (hint, link) shown next to the password field
APP_PASSWORD_HINTS = {
    "gmail": ("Gmail takes an app password here, not your normal one (2-step verification must be on).",
              "https://myaccount.google.com/apppasswords"),
    "icloud": ("iCloud takes an app-specific password here.", "https://account.apple.com/account/manage"),
    "yahoo": ("Yahoo takes an app password here.", "https://login.yahoo.com/myaccount/security/"),
    "fastmail": ("Fastmail takes an app password here.", "https://app.fastmail.com/settings/security/integrations"),
    "zoho": ("Zoho: IMAP access must be enabled in the mail settings first.", "https://mail.zoho.com/zm/#settings/all/mailaccounts"),
    "gmx": ("GMX: IMAP must be enabled under E-Mail, Einstellungen, POP3/IMAP Abruf.", "https://www.gmx.net"),
    "web.de": ("web.de: IMAP must be enabled under E-Mail, Einstellungen, POP3/IMAP.", "https://web.de"),
}

ADDRESS_RX = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class SetupError(RuntimeError):
    def __init__(self, message: str, *, hint: str = "", link: str = "", code: str = ""):
        super().__init__(message)
        self.hint = hint
        self.link = link
        self.code = code


@dataclass
class Plan:
    """What `add` found out about an address before any password is typed."""
    address: str
    kind: str                          # "password" | "oauth" | "none"
    candidate: Candidate | None = None
    provider: str = ""
    hint: str = ""
    link: str = ""
    notes: list[str] = field(default_factory=list)
    tried: list[str] = field(default_factory=list)

    @property
    def view(self) -> dict:
        """What a dialog shows for this plan: password field or a Microsoft button, plus the hint."""
        if self.kind == "oauth":
            return {"password_field": False, "button": "Sign in with Microsoft", "hint": self.hint, "link": ""}
        if self.kind == "none":
            return {"password_field": False, "button": "Connect", "hint": self.hint, "link": ""}
        return {"password_field": True, "button": "Connect", "hint": self.hint, "link": self.link}


def valid_address(address: str) -> bool:
    return bool(ADDRESS_RX.fullmatch(address.strip()))


def quick_kind(address: str) -> str:
    """Without network: "oauth" for Microsoft consumer domains, "password" for Google, "" if unknown."""
    dom = address.strip().lower().rsplit("@", 1)[-1] if "@" in address else ""
    if dom in MS_CONSUMER_DOMAINS:
        return "oauth"
    if dom in GOOGLE_DOMAINS:
        return "password"
    return ""


def plan_for(address: str, *, host: str = "", port: int = 0, starttls: bool = False, username: str = "",
             discover_fn=discover, verbose=None) -> Plan:
    address = address.strip().lower()
    if not valid_address(address):
        raise SetupError(f"not an email address: {address!r}", code="address")
    if host:
        cand = Candidate(host=host, port=port or (143 if starttls else 993), security="starttls" if starttls else "ssl",
                         source="manual", username=username)
        return Plan(address=address, kind="password", candidate=cand)
    d: Discovery = discover_fn(address, verbose=verbose)
    if d.chosen and d.chosen.provider == "microsoft365":
        c = d.chosen
        return Plan(address=address, kind="oauth", candidate=c, provider="microsoft365",
                    hint="Microsoft account: the sign-in opens in your browser, no password is typed here.", notes=d.notes)
    if not d.chosen:
        tried = [f"{c.host}:{c.port} {c.security} ({c.source})" for c in d.candidates]
        gateway = any("gateway" in n.lower() for n in d.notes)
        hint = ("No IMAP server answered for this address. " +
                ("The MX points at a mail gateway; ask your admin for the IMAP host." if gateway else
                 "If you know the server, pass it: mailskill add <address> --host imap.example.com"))
        return Plan(address=address, kind="none", hint=hint, notes=d.notes, tried=tried)
    c = d.chosen
    if username:
        c.username = username
    prov = c.provider or ""
    hint, link = APP_PASSWORD_HINTS.get(prov, ("", ""))
    return Plan(address=address, kind="password", candidate=c, provider=prov, hint=hint, link=link, notes=d.notes)


def account_for(plan: Plan) -> store.Account:
    c = plan.candidate
    return store.Account(address=plan.address, host=c.host, port=c.port, security=c.security,
                         username=c.username if c.username and c.username != plan.address else "",
                         provider=plan.provider or c.provider or "", auth="oauth" if plan.kind == "oauth" else "password")


def test_login(acc: store.Account, secret: str, mailbox_cls=Mailbox) -> list:
    """Connect once and list the folders. Raises SetupError with a hint the user can act on."""
    mb = mailbox_cls(acc, secret)
    try:
        mb.connect()
        return mb.folders()
    except MailboxError as e:
        hint, link = APP_PASSWORD_HINTS.get(acc.provider, ("", ""))
        raise SetupError(str(e), hint=hint, link=link, code="login") from e
    except Exception as e:
        raise SetupError(f"connection to {acc.host}:{acc.port} failed: {e}", code="connect") from e
    finally:
        mb.close()


def finish_password(acc: store.Account, secret: str, make_default: bool = False) -> str:
    """Store the account and the secret. Returns where the secret went."""
    try:
        where = store.set_secret(acc.address, secret)
    except RuntimeError as e:
        where = "not stored (" + str(e) + ")"
    store.save_account(acc, make_default=make_default or not store.default_address())
    return where


def sign_in_microsoft(plan: Plan, *, client_id: str | None = None, status=lambda m: None, sign_in=None,
                      mailbox_cls=Mailbox, make_default: bool = False) -> tuple[store.Account, list, str]:
    """Browser sign-in, IMAP check with the fresh access token, store the refresh token.

    Returns (account, folders, where_stored)."""
    cid = oauth.client_id(client_id)
    tokens = (sign_in or oauth.sign_in)(plan.address, cid, status=status)
    acc = account_for(plan)
    acc.host, acc.port, acc.security = oauth.MS_IMAP_HOST, oauth.MS_IMAP_PORT, "ssl"
    status(f"Testing IMAP at {acc.host} ...")
    folders = test_login(acc, tokens["access_token"], mailbox_cls=mailbox_cls)
    try:
        where = store.set_refresh_token(acc.address, tokens["refresh_token"])
    except RuntimeError as e:
        raise SetupError("Sign-in worked, but there is no keychain to keep the token in: " + str(e), code="keyring") from e
    store.save_account(acc, make_default=make_default or not store.default_address())
    return acc, folders, where


def access_token(acc: store.Account, *, client_id: str | None = None, refresh=None) -> str:
    """Silent refresh for a stored Microsoft account; stores the rotated refresh token."""
    rt = store.get_refresh_token(acc.address)
    if not rt:
        raise SetupError(f"No Microsoft sign-in stored for {acc.address}. Run: mailskill add {acc.address}", code="no_token")
    tokens = (refresh or oauth.refresh)(oauth.client_id(client_id), rt)
    new_rt = tokens.get("refresh_token")
    if new_rt and new_rt != rt:
        try:
            store.set_refresh_token(acc.address, new_rt)
        except RuntimeError:
            pass
    return tokens["access_token"]
