"""Small helpers shared by the other modules. No I/O in here."""
from __future__ import annotations

import email.utils
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

_REPLY_PREFIX = re.compile(r"^\s*(re|aw|wg|fw|fwd|tr|sv|vs|antwort|odp|rif)\s*(\[\d+\])?\s*:\s*", re.I)
_ADDR = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def decode_hdr(value) -> str:
    """RFC 2047 header to text. Never raises; a broken header comes back as-is."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    try:
        out = str(make_header(decode_header(value)))
    except Exception:
        out = str(value)
    return re.sub(r"\s+", " ", out).strip()   # folded headers carry newlines


def norm_subject(subject: str | None) -> str:
    """Strip reply and forward prefixes in several languages, lower-case, collapse spaces."""
    s = subject or ""
    for _ in range(8):
        new = _REPLY_PREFIX.sub("", s)
        if new == s:
            break
        s = new
    return re.sub(r"\s+", " ", s).strip().lower()


def first_addr(value) -> str:
    """First email address in a header value, lower-case, or ''."""
    m = _ADDR.search(decode_hdr(value))
    return m.group(0).lower() if m else ""


def all_addrs(*values) -> set[str]:
    out: set[str] = set()
    for v in values:
        for m in _ADDR.finditer(decode_hdr(v)):
            out.add(m.group(0).lower())
    return out


def parse_date(value) -> datetime | None:
    """Date header to an aware datetime (UTC if the header carried no zone)."""
    if not value:
        return None
    try:
        d = email.utils.parsedate_to_datetime(value)
    except Exception:
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def imap_date(d: datetime) -> str:
    """IMAP SEARCH date literal, e.g. 3-Sep-2026 (no leading zero)."""
    return f"{d.day}-{_MONTHS[d.month - 1]}-{d.year}"


def days_ago(days: int, now: datetime | None = None) -> str:
    return imap_date((now or datetime.now(timezone.utc)) - timedelta(days=days))


def quote_mailbox(name: str) -> str:
    """IMAP mailbox argument. Names with spaces or specials must be quoted, or SELECT
    silently answers NO and the next SEARCH fails with 'illegal in state AUTH'."""
    if name.startswith('"') and name.endswith('"'):
        return name
    if re.search(r'[\s"(){}%*\\]', name) or name == "":
        return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return name


def utf7_decode(name: str) -> str:
    """Modified UTF-7 (RFC 3501) to text: 'Entw&APw-rfe' -> 'Entwürfe'."""
    if "&" not in name:
        return name
    out = []
    i = 0
    while i < len(name):
        c = name[i]
        if c != "&":
            out.append(c)
            i += 1
            continue
        j = name.find("-", i)
        if j == -1:
            out.append(name[i:])
            break
        chunk = name[i + 1 : j]
        if chunk == "":
            out.append("&")
        else:
            b64 = chunk.replace(",", "/")
            b64 += "=" * (-len(b64) % 4)
            try:
                import base64

                out.append(base64.b64decode(b64).decode("utf-16-be"))
            except Exception:
                out.append(name[i : j + 1])
        i = j + 1
    return "".join(out)


def utf7_encode(name: str) -> str:
    """Text to modified UTF-7 for mailbox names with non-ASCII characters."""
    if all(0x20 <= ord(c) <= 0x7E for c in name) and "&" not in name:
        return name
    import base64

    out = []
    buf = []

    def flush():
        if buf:
            raw = "".join(buf).encode("utf-16-be")
            b64 = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
            out.append("&" + b64 + "-")
            buf.clear()

    for c in name:
        if c == "&":
            flush()
            out.append("&-")
        elif 0x20 <= ord(c) <= 0x7E:
            flush()
            out.append(c)
        else:
            buf.append(c)
    flush()
    return "".join(out)


def parse_list_line(line: bytes | str) -> tuple[list[str], str, str] | None:
    """One LIST response line -> (attributes, delimiter, raw mailbox name).

    Handles '(\\HasNoChildren \\Drafts) "/" "Entw&APw-rfe"' and unquoted names.
    """
    txt = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
    m = re.match(r'^\((?P<attrs>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$', txt.strip())
    if not m:
        return None
    attrs = m.group("attrs").split()
    delim = m.group("delim")
    delim = "" if delim == "NIL" else delim.strip('"')
    name = m.group("name").strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    elif name.startswith("{"):
        # literal form {n}\r\nname is not produced by imaplib's list(); keep as-is
        pass
    return attrs, delim, name


def parse_flags(prefix: bytes | str) -> list[str]:
    txt = prefix.decode("utf-8", "replace") if isinstance(prefix, bytes) else str(prefix or "")
    m = re.search(r"FLAGS \(([^)]*)\)", txt)
    return m.group(1).split() if m else []


def parse_uid(prefix: bytes | str) -> str:
    txt = prefix.decode("utf-8", "replace") if isinstance(prefix, bytes) else str(prefix or "")
    m = re.search(r"\bUID (\d+)", txt)
    return m.group(1) if m else ""


def safe_filename(name: str, fallback: str = "attachment") -> str:
    """Strip path components and characters that are unsafe on Windows or Unix."""
    n = decode_hdr(name).strip().replace("\\", "/").split("/")[-1]
    n = re.sub(r'[<>:"|?*\x00-\x1f]', "_", n).strip(" .")
    return n or fallback


def redact(text: str) -> str:
    """Mask things that look like secrets before they reach a log or a terminal."""
    text = re.sub(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)=\S+", r"\1=***", text)
    text = re.sub(r"(?i)\b(authorization:\s*(?:bearer|basic))\s+\S+", r"\1 ***", text)
    return text
