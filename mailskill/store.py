"""Account settings on disk (no secret), the secret in the OS keychain.

Settings: ~/.claude-mail-skill/accounts.json, mode 600 where the OS supports it.
Secret: keyring service "claude-mail-skill", username = the address.
Without keyring (or in a container) the secret is read from the environment
variable named in PW_ENV, never from a file.
Microsoft accounts (auth="oauth") keep a refresh token instead of a password,
under the keyring username "oauth:<address>". It is never read from the environment.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, asdict
from pathlib import Path

SERVICE = "claude-mail-skill"
PW_ENV = "MAILSKILL_PASSWORD"
OAUTH_PREFIX = "oauth:"


def home_dir() -> Path:
    return Path(os.environ.get("MAILSKILL_HOME") or (Path.home() / ".claude-mail-skill"))


def accounts_file() -> Path:
    return home_dir() / "accounts.json"


@dataclass
class Account:
    address: str
    host: str
    port: int
    security: str            # "ssl" | "starttls"
    username: str = ""       # "" -> address
    provider: str = ""
    auth: str = "password"   # "password" | "oauth" (Microsoft sign-in, token in the keychain)
    sent_folder: str = ""    # discovered on first use, cached here
    drafts_folder: str = ""
    trash_folder: str = ""
    archive_folder: str = ""
    delimiter: str = ""

    @property
    def login(self) -> str:
        return self.username or self.address


def _read() -> dict:
    p = accounts_file()
    if not p.is_file():
        return {"default": "", "accounts": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"default": "", "accounts": {}}
    d.setdefault("default", "")
    d.setdefault("accounts", {})
    return d


def _write(d: dict) -> None:
    hd = home_dir()
    hd.mkdir(parents=True, exist_ok=True)
    p = accounts_file()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    os.replace(tmp, p)


def _to_account(a: dict) -> Account:
    return Account(**{k: v for k, v in a.items() if k in Account.__dataclass_fields__})


def list_accounts() -> list[Account]:
    return [_to_account(a) for a in _read()["accounts"].values()]


def default_address() -> str:
    return _read()["default"]


def get_account(address: str | None = None) -> Account | None:
    d = _read()
    addr = (address or d["default"] or "").lower()
    if not addr:
        accs = d["accounts"]
        if len(accs) == 1:
            addr = next(iter(accs))
        else:
            return None
    a = d["accounts"].get(addr)
    if not a:
        # allow a unique prefix, e.g. "bob" for bob@example.com
        hits = [k for k in d["accounts"] if k.startswith(addr)]
        if len(hits) == 1:
            a = d["accounts"][hits[0]]
    return _to_account(a) if a else None


def save_account(acc: Account, make_default: bool = False) -> None:
    d = _read()
    d["accounts"][acc.address.lower()] = asdict(acc)
    if make_default or not d["default"]:
        d["default"] = acc.address.lower()
    _write(d)


def remove_account(address: str) -> bool:
    d = _read()
    addr = address.lower()
    if addr not in d["accounts"]:
        return False
    del d["accounts"][addr]
    if d["default"] == addr:
        d["default"] = next(iter(d["accounts"]), "")
    _write(d)
    delete_secret(addr)
    delete_refresh_token(addr)
    return True


# ------------------------------------------------------------------ secrets


def _keyring():
    try:
        import keyring  # type: ignore

        kr = keyring.get_keyring()
        if "fail" in kr.__class__.__module__ or kr.__class__.__name__ == "ChainerBackend" and not getattr(kr, "backends", None):
            return None
        return keyring
    except Exception:
        return None


def keyring_backend_name() -> str:
    kr = _keyring()
    if not kr:
        return f"none (set {PW_ENV} or: python -m pip install --user keyring)"
    try:
        return kr.get_keyring().__class__.__name__
    except Exception:
        return "unknown"


def set_secret(address: str, secret: str) -> str:
    """Store the mailbox secret in the keychain. Returns 'keyring' or raises."""
    kr = _keyring()
    if not kr:
        raise RuntimeError("no keychain backend available; install the keyring package "
                           f"(python -m pip install --user keyring) or export {PW_ENV} for this session")
    try:
        kr.set_password(SERVICE, address.lower(), secret)
    except Exception as e:
        raise RuntimeError(f"the keychain refused the entry: {e}") from e
    return "keyring"


def get_secret(address: str) -> str | None:
    env = os.environ.get(PW_ENV)
    if env:
        return env
    kr = _keyring()
    if not kr:
        return None
    try:
        return kr.get_password(SERVICE, address.lower())
    except Exception:
        return None


def delete_secret(address: str) -> None:
    kr = _keyring()
    if not kr:
        return
    try:
        kr.delete_password(SERVICE, address.lower())
    except Exception:
        pass


# ------------------------------------------------------------------ oauth refresh tokens
#
# Windows Credential Manager caps one entry at 2560 bytes (UTF-16, so about 1200 characters);
# a Microsoft refresh token is longer. The token is stored in numbered chunks: oauth:<addr>#0, #1 ...

CHUNK = 1000


def set_refresh_token(address: str, token: str) -> str:
    base = OAUTH_PREFIX + address.lower()
    delete_refresh_token(address)
    parts = [token[i:i + CHUNK] for i in range(0, len(token), CHUNK)] or [""]
    where = ""
    for n, part in enumerate(parts):
        where = set_secret(f"{base}#{n}", part)
    set_secret(base, str(len(parts)))
    return where


def get_refresh_token(address: str) -> str | None:
    kr = _keyring()
    if not kr:
        return None
    base = OAUTH_PREFIX + address.lower()
    try:
        count = kr.get_password(SERVICE, base)
        if not count:
            return None
        parts = [kr.get_password(SERVICE, f"{base}#{n}") for n in range(int(count))]
    except Exception:
        return None
    if any(p is None for p in parts):
        return None
    return "".join(parts)


def delete_refresh_token(address: str) -> None:
    kr = _keyring()
    if not kr:
        return
    base = OAUTH_PREFIX + address.lower()
    try:
        count = int(kr.get_password(SERVICE, base) or 0)
    except Exception:
        count = 0
    for n in range(max(count, 1)):
        delete_secret(f"{base}#{n}")
    delete_secret(base)
