"""IMAP access. Read-only unless a method says otherwise in its name.

Lessons baked in (each one cost us an afternoon at some point):
- mailbox names with spaces must be quoted, and SELECT's status must be checked;
  otherwise the next SEARCH fails with "illegal in state AUTH"
- the special folders (Sent, Drafts, Trash, Archive) are found via LIST
  attributes, never by guessing a name. Names differ per server and are
  IMAP-UTF-7 ("Entw&APw-rfe"), so the raw name is used on the wire and the
  decoded one for display
- BODY.PEEK keeps the unread flag; a fetch of RFC822 would mark mail as read
- SEARCH SUBJECT with spaces is unreliable on some servers (Dovecot), so subject
  filters are applied client-side on top of a broader server-side query
- UIDs, not sequence numbers, so a result stays valid while mail arrives
- own replies can sit in INBOX instead of Sent depending on the client that
  sent them, so "did I reply" looks at both
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import mimetypes
import re
import ssl
import time
from dataclasses import dataclass, field, asdict
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .store import Account
from .util import (all_addrs, decode_hdr, first_addr, norm_subject, parse_date, parse_flags,
                   parse_list_line, parse_uid, quote_mailbox, safe_filename, utf7_decode)

imaplib._MAXLINE = 10_000_000  # large folders and big attachments


@dataclass
class Folder:
    raw: str
    name: str
    attrs: list[str]
    delimiter: str
    role: str = ""          # sent | drafts | trash | archive | junk | inbox | ""

    def as_dict(self):
        return asdict(self)


@dataclass
class Summary:
    uid: str
    date: str
    from_: str
    to: str
    subject: str
    flags: list[str]
    has_attachments: bool
    size: int = 0
    message_id: str = ""
    in_reply_to: str = ""

    def as_dict(self):
        d = asdict(self)
        d["from"] = d.pop("from_")
        d["answered"] = "\\Answered" in self.flags
        d["seen"] = "\\Seen" in self.flags
        return d


@dataclass
class Full:
    summary: Summary
    text: str
    html: str
    attachments: list[dict] = field(default_factory=list)
    headers: dict = field(default_factory=dict)

    def as_dict(self):
        return {"summary": self.summary.as_dict(), "text": self.text, "html_present": bool(self.html),
                "attachments": self.attachments, "headers": self.headers}


class MailboxError(RuntimeError):
    pass


def _check(typ, what):
    if typ != "OK":
        raise MailboxError(f"{what} failed ({typ})")


def _pick_by_role(folders: list[Folder], role: str) -> str:
    attr = {"sent": "\\Sent", "drafts": "\\Drafts", "trash": "\\Trash", "archive": "\\Archive", "junk": "\\Junk"}[role]
    for f in folders:
        if attr in f.attrs:
            return f.raw
    names = {
        "sent": ("sent", "sent items", "sent mail", "gesendet", "gesendete elemente", "gesendete objekte", "envoyés", "enviados", "inviati"),
        "drafts": ("drafts", "draft", "entwürfe", "brouillons", "borradores", "bozze"),
        "trash": ("trash", "deleted items", "deleted messages", "papierkorb", "gelöschte elemente", "corbeille", "papelera", "cestino"),
        "archive": ("archive", "archiv", "archives", "all mail"),
        "junk": ("junk", "spam", "junk e-mail", "junk email", "bulk mail"),
    }[role]
    for f in folders:
        leaf = f.name.split(f.delimiter)[-1].lower() if f.delimiter else f.name.lower()
        if leaf in names or f.name.lower() in names:
            return f.raw
    return ""


class Mailbox:
    """One IMAP session. Use as a context manager."""

    def __init__(self, account: Account, secret: str, timeout: int = 30):
        self.account = account
        self._secret = secret
        self.timeout = timeout
        self.conn: imaplib.IMAP4 | None = None
        self._folders: list[Folder] | None = None
        self._selected: str | None = None
        self._readonly = True

    # ------------------------------------------------------------ session
    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        a = self.account
        ctx = ssl.create_default_context()
        if a.security == "ssl":
            c = imaplib.IMAP4_SSL(a.host, a.port, ssl_context=ctx, timeout=self.timeout)
        else:
            c = imaplib.IMAP4(a.host, a.port, timeout=self.timeout)
            c.starttls(ssl_context=ctx)
        if a.auth == "oauth":
            from .oauth import imap_auth_error, xoauth2_string

            initial = xoauth2_string(a.login, self._secret)
            try:
                c.authenticate("XOAUTH2", lambda _challenge: initial)
            except imaplib.IMAP4.error as e:
                raise MailboxError(imap_auth_error(a.login, a.host, e)) from e
        else:
            try:
                c.login(a.login, self._secret)
            except imaplib.IMAP4.error as e:
                raise MailboxError(f"login refused for {a.login} at {a.host}: {e}") from e
        self.conn = c
        return self

    def close(self):
        if self.conn is not None:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    # ------------------------------------------------------------ folders
    def folders(self, refresh: bool = False) -> list[Folder]:
        if self._folders is not None and not refresh:
            return self._folders
        typ, data = self.conn.list()
        _check(typ, "LIST")
        out: list[Folder] = []
        for line in data or []:
            if line is None:
                continue
            if isinstance(line, tuple):   # literal mailbox name: (b'(\\HasNoChildren) "/" {12}', b'Name')
                head = line[0].decode("utf-8", "replace")
                m = re.match(r'^\((?P<attrs>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)', head)
                if not m:
                    continue
                attrs = m.group("attrs").split()
                delim = "" if m.group("delim") == "NIL" else m.group("delim").strip('"')
                raw = line[1].decode("utf-8", "replace")
            else:
                p = parse_list_line(line)
                if not p:
                    continue
                attrs, delim, raw = p
            if "\\Noselect" in attrs or "\\NonExistent" in attrs:
                continue
            out.append(Folder(raw=raw, name=utf7_decode(raw), attrs=attrs, delimiter=delim))
        for f in out:
            if f.raw.upper() == "INBOX":
                f.role = "inbox"
        for role in ("sent", "drafts", "trash", "archive", "junk"):
            raw = _pick_by_role(out, role)
            for f in out:
                if f.raw == raw and not f.role:
                    f.role = role
        self._folders = out
        # remember on the account so the CLI can show/save them
        a = self.account
        a.sent_folder = a.sent_folder or _pick_by_role(out, "sent")
        a.drafts_folder = a.drafts_folder or _pick_by_role(out, "drafts")
        a.trash_folder = a.trash_folder or _pick_by_role(out, "trash")
        a.archive_folder = a.archive_folder or _pick_by_role(out, "archive")
        a.delimiter = a.delimiter or (out[0].delimiter if out else "/")
        return out

    def resolve_folder(self, name: str) -> str:
        """User-facing name (decoded, case-insensitive, or a role like 'sent') -> raw name."""
        if not name:
            return "INBOX"
        roles = {"inbox": "INBOX", "sent": self.account.sent_folder, "drafts": self.account.drafts_folder,
                 "trash": self.account.trash_folder, "archive": self.account.archive_folder}
        self.folders()
        roles = {"inbox": "INBOX", "sent": self.account.sent_folder, "drafts": self.account.drafts_folder,
                 "trash": self.account.trash_folder, "archive": self.account.archive_folder}
        if name.lower() in roles and roles[name.lower()]:
            return roles[name.lower()]
        for f in self._folders:
            if f.raw == name or f.name == name:
                return f.raw
        for f in self._folders:
            if f.raw.lower() == name.lower() or f.name.lower() == name.lower():
                return f.raw
        return name  # let the server decide

    def select(self, folder: str = "INBOX", readonly: bool = True) -> int:
        raw = self.resolve_folder(folder)
        if self._selected == raw and self._readonly == readonly:
            return -1
        typ, data = self.conn.select(quote_mailbox(raw), readonly=readonly)
        if typ != "OK":
            raise MailboxError(f"folder not selectable: {utf7_decode(raw)} ({data[0].decode('utf-8', 'replace') if data and data[0] else typ})")
        self._selected, self._readonly = raw, readonly
        try:
            return int(data[0])
        except Exception:
            return 0

    # ------------------------------------------------------------ search
    def search(self, folder: str = "INBOX", *, from_: str = "", to: str = "", subject: str = "",
               text: str = "", since: str = "", before: str = "", unseen: bool = False,
               flagged: bool = False, unanswered: bool = False, limit: int = 50, newest_first: bool = True) -> list[Summary]:
        self.select(folder, readonly=True)
        crit: list[str] = []
        if from_:
            crit += ["FROM", from_]
        if to:
            crit += ["TO", to]
        if text:
            crit += ["TEXT", text]
        if since:
            crit += ["SINCE", since]
        if before:
            crit += ["BEFORE", before]
        if unseen:
            crit.append("UNSEEN")
        if flagged:
            crit.append("FLAGGED")
        if unanswered:
            crit.append("UNANSWERED")
        if subject and " " not in subject and subject.isascii():
            crit += ["SUBJECT", subject]        # safe form; multi-word subjects are filtered below
        if not crit:
            crit = ["ALL"]
        charset = None if all(c.isascii() for c in crit) else "UTF-8"
        typ, data = self.conn.uid("SEARCH", charset, *crit) if charset else self.conn.uid("SEARCH", None, *crit)
        if typ != "OK":
            # some servers refuse UTF-8 charset; retry with ASCII-only criteria
            crit2 = [c for c in crit if c.isascii()]
            typ, data = self.conn.uid("SEARCH", None, *(crit2 or ["ALL"]))
            _check(typ, "SEARCH")
        uids = data[0].split() if data and data[0] else []
        uids.sort(key=int, reverse=newest_first)
        want = norm_subject(subject) if subject else ""
        out: list[Summary] = []
        # fetch headers in chunks; stop once we have `limit` matches
        step = 100
        for i in range(0, len(uids), step):
            chunk = uids[i : i + step]
            for s in self._summaries(chunk):
                if want and want not in norm_subject(s.subject):
                    continue
                out.append(s)
                if len(out) >= limit:
                    return out
        return out

    def _summaries(self, uids: list[bytes]) -> list[Summary]:
        if not uids:
            return []
        seq = b",".join(uids).decode()
        typ, data = self.conn.uid("FETCH", seq, "(UID FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (DATE FROM TO SUBJECT MESSAGE-ID IN-REPLY-TO)])")
        _check(typ, "FETCH")
        out: list[Summary] = []
        for item in data or []:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            prefix, hdr = item[0], item[1]
            msg = email.message_from_bytes(hdr)
            ptxt = prefix.decode("utf-8", "replace")
            size = 0
            m = re.search(r"RFC822.SIZE (\d+)", ptxt)
            if m:
                size = int(m.group(1))
            has_att = bool(re.search(r'\("attachment"|"NAME"|"FILENAME"', ptxt, re.I))
            out.append(Summary(
                uid=parse_uid(prefix), date=(msg.get("Date") or "").strip(),
                from_=decode_hdr(msg.get("From")), to=decode_hdr(msg.get("To")),
                subject=decode_hdr(msg.get("Subject")), flags=parse_flags(prefix), has_attachments=has_att,
                size=size, message_id=(msg.get("Message-ID") or "").strip(),
                in_reply_to=(msg.get("In-Reply-To") or "").strip()))
        by_uid = {s.uid: s for s in out}
        return [by_uid[u.decode()] for u in uids if u.decode() in by_uid]

    # ------------------------------------------------------------ read
    def fetch(self, uid: str, folder: str = "INBOX", mark_seen: bool = False) -> Full:
        self.select(folder, readonly=not mark_seen)
        part = "BODY[]" if mark_seen else "BODY.PEEK[]"
        typ, data = self.conn.uid("FETCH", uid, f"(UID FLAGS {part})")
        _check(typ, "FETCH")
        if not data or not isinstance(data[0], tuple):
            raise MailboxError(f"no message with UID {uid} in {utf7_decode(self._selected)}")
        prefix, raw = data[0][0], data[0][1]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        text, html = "", ""
        atts: list[dict] = []
        idx = 0
        for p in msg.walk():
            if p.is_multipart():
                continue
            ctype = p.get_content_type()
            disp = (p.get("Content-Disposition") or "").lower()
            fname = p.get_filename()
            if fname or disp.startswith("attachment"):
                idx += 1
                payload = p.get_payload(decode=True) or b""
                atts.append({"index": idx, "filename": safe_filename(fname or f"part-{idx}"), "content_type": ctype,
                             "size": len(payload), "_payload": payload})
                continue
            if ctype == "text/plain" and not text:
                text = _text_of(p)
            elif ctype == "text/html" and not html:
                html = _text_of(p)
        if not text and html:
            text = html_to_text(html)
        s = Summary(uid=uid, date=(msg.get("Date") or "").strip(), from_=decode_hdr(msg.get("From")),
                    to=decode_hdr(msg.get("To")), subject=decode_hdr(msg.get("Subject")), flags=parse_flags(prefix),
                    has_attachments=bool(atts), size=len(raw), message_id=(msg.get("Message-ID") or "").strip(),
                    in_reply_to=(msg.get("In-Reply-To") or "").strip())
        headers = {k: decode_hdr(v) for k, v in msg.items() if k.lower() in ("cc", "reply-to", "list-unsubscribe", "references", "x-mailer")}
        return Full(summary=s, text=text, html=html, attachments=atts, headers=headers)

    def save_attachments(self, full: Full, directory: str | Path, only: list[int] | None = None) -> list[Path]:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        saved: list[Path] = []
        for a in full.attachments:
            if only and a["index"] not in only:
                continue
            target = d / a["filename"]
            n = 1
            while target.exists():
                target = d / f"{Path(a['filename']).stem}-{n}{Path(a['filename']).suffix}"
                n += 1
            target.write_bytes(a["_payload"])
            saved.append(target)
        return saved

    # ------------------------------------------------------------ replies
    def own_replies(self, full: Full, folders: list[str] | None = None) -> list[dict]:
        """Messages from this account that answer `full` (In-Reply-To/References, fallback subject+recipient+date)."""
        me = self.account.address.lower()
        mid = full.summary.message_id
        sender = first_addr(full.summary.from_)
        odate = parse_date(full.summary.date)
        scan = folders or [f for f in (self.account.sent_folder, "INBOX") if f]
        out: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for folder in scan:
            try:
                self.select(folder, readonly=True)
            except MailboxError:
                continue
            cands: list[bytes] = []
            if mid:
                for hdr in ("IN-REPLY-TO", "REFERENCES"):
                    typ, data = self.conn.uid("SEARCH", None, "HEADER", hdr, mid)
                    if typ == "OK" and data and data[0]:
                        cands += data[0].split()
            if not cands and sender:
                typ, data = self.conn.uid("SEARCH", None, "TO", sender, "FROM", me)
                if typ == "OK" and data and data[0]:
                    cands += data[0].split()[-60:]
            want = norm_subject(full.summary.subject)
            for s in self._summaries(sorted(set(cands), key=int)):
                if first_addr(s.from_) != me:
                    continue
                if (folder, s.uid) in seen:
                    continue
                if mid and (s.in_reply_to == mid):
                    pass
                elif norm_subject(s.subject) != want:
                    continue
                rd = parse_date(s.date)
                if odate and rd and rd < odate:
                    continue
                seen.add((folder, s.uid))
                d = s.as_dict()
                d["folder"] = utf7_decode(folder)
                out.append(d)
        return out

    # ------------------------------------------------------------ drafts (writes to the Drafts folder only)
    def append_draft(self, to: str, subject: str, body: str, *, cc: str = "", bcc: str = "", html: str = "",
                     attachments: list[str] | None = None, in_reply_to: str = "", references: str = "",
                     folder: str = "") -> str:
        """Put a draft into the Drafts folder. Returns the folder used. Never sends."""
        self.folders()
        target = folder or self.account.drafts_folder
        if not target:
            raise MailboxError("no Drafts folder found on this server; pass --folder")
        msg = EmailMessage()
        msg["From"] = self.account.address
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.account.address.rsplit("@", 1)[1])
        msg["X-Unsent"] = "1"   # Outlook opens it as an unsent draft, not as received mail
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = (references + " " + in_reply_to).strip() if references else in_reply_to
        msg.set_content(body)
        if html:
            msg.add_alternative(html, subtype="html")
        for p in attachments or []:
            path = Path(p)
            ctype, _ = mimetypes.guess_type(path.name)
            main, sub = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(path.read_bytes(), maintype=main, subtype=sub, filename=path.name)
        typ, data = self.conn.append(quote_mailbox(target), "(\\Draft \\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
        if typ != "OK":
            raise MailboxError(f"APPEND to {utf7_decode(target)} failed: {data}")
        return target

    # ------------------------------------------------------------ changes (used by cleanup, always after a dry run)
    def move(self, uids: list[str], src: str, dst: str) -> int:
        """COPY, verify, flag \\Deleted, EXPUNGE. Reversible up to the expunge; never a hard delete without a copy."""
        if not uids:
            return 0
        self.select(src, readonly=False)
        raw_dst = self.resolve_folder(dst)
        seq = ",".join(uids)
        caps = self.conn.capabilities or ()
        if "MOVE" in caps:
            typ, _ = self.conn.uid("MOVE", seq, quote_mailbox(raw_dst))
            _check(typ, "MOVE")
            return len(uids)
        typ, _ = self.conn.uid("COPY", seq, quote_mailbox(raw_dst))
        _check(typ, "COPY")
        typ, _ = self.conn.uid("STORE", seq, "+FLAGS.SILENT", "(\\Deleted)")
        _check(typ, "STORE")
        self.conn.expunge()
        return len(uids)

    def set_flags(self, uids: list[str], folder: str, add: list[str] | None = None, remove: list[str] | None = None) -> None:
        if not uids:
            return
        self.select(folder, readonly=False)
        seq = ",".join(uids)
        if add:
            typ, _ = self.conn.uid("STORE", seq, "+FLAGS.SILENT", "(" + " ".join(add) + ")")
            _check(typ, "STORE +FLAGS")
        if remove:
            typ, _ = self.conn.uid("STORE", seq, "-FLAGS.SILENT", "(" + " ".join(remove) + ")")
            _check(typ, "STORE -FLAGS")

    def headers_since(self, folder: str, since: str = "") -> list[Summary]:
        """All summaries of a folder (optionally SINCE date), oldest first. For cleanup and threads."""
        self.select(folder, readonly=True)
        crit = ["SINCE", since] if since else ["ALL"]
        typ, data = self.conn.uid("SEARCH", None, *crit)
        _check(typ, "SEARCH")
        uids = data[0].split() if data and data[0] else []
        uids.sort(key=int)
        out: list[Summary] = []
        for i in range(0, len(uids), 200):
            out += self._summaries(uids[i : i + 200])
        return out


# ------------------------------------------------------------------ helpers


def _text_of(part) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True) or b""
        return payload.decode(part.get_content_charset() or "utf-8", "replace")


def html_to_text(html: str) -> str:
    """Good enough for reading: drop scripts and styles, keep line breaks for block elements."""
    h = re.sub(r"(?is)<(script|style|head).*?</\1>", "", html)
    h = re.sub(r"(?i)<br\s*/?>", "\n", h)
    h = re.sub(r"(?i)</(p|div|tr|li|h[1-6]|blockquote|table)>", "\n", h)
    h = re.sub(r"(?s)<[^>]+>", "", h)
    import html as _html

    h = _html.unescape(h)
    h = re.sub(r"[ \t\xa0]+", " ", h)
    h = re.sub(r"\n\s*\n\s*\n+", "\n\n", h)
    return h.strip()


def own_part(text: str) -> str:
    """The part of a reply above the quoted original (several client conventions)."""
    cut = re.split(r"\n_{10,}\n|\n-{5,} ?Original Message ?-{5,}|\nOn .{5,120} wrote:\n|\nAm .{5,120} schrieb .{1,80}:\n|\nVon: |\nFrom: |\nLe .{5,120} a écrit :\n", text, maxsplit=1)
    return cut[0].strip()


def thread_key(s: Summary) -> str:
    return norm_subject(s.subject)


def participants(s: Summary) -> set[str]:
    return all_addrs(s.from_, s.to)
