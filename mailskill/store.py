"""Account settings on disk (no secret), the secret in the OS keychain.

Settings: ~/.claude-mail-skill/accounts.json, mode 600 where the OS supports it.
Secret: keyring service "claude-mail-skill", username = the address.
Without keyring (or in a container) the secret is read from the environment
variable named in PW_ENV, never from a file.
"""
from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, asdict
from pathlib import Path

SERVICE = "claude-mail-skill"
PW_ENV = "MAILSKILL_PASSWORD"


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
    kr.set_password(SERVICE, address.lower(), secret)
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
