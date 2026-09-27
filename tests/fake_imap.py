"""An in-memory stand-in for imaplib.IMAP4 with just enough of the protocol for the tests.

It answers the way Dovecot does where that matters: an unquoted SELECT of a
name with spaces fails, folder names are IMAP-UTF-7, LIST carries special-use
attributes, UID SEARCH/FETCH/COPY/STORE/MOVE work on UIDs.
"""
from __future__ import annotations

import email
import re
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


def make_msg(*, from_="alice@example.com", to="me@example.com", subject="Hello", body="hi there",
             date=None, message_id=None, in_reply_to=None, attachment: tuple[str, bytes] | None = None, html=None) -> bytes:
    m = EmailMessage()
    m["From"] = from_
    m["To"] = to
    m["Subject"] = subject
    m["Date"] = date or formatdate(localtime=False)
    m["Message-ID"] = message_id or make_msgid(domain="example.com")
    if in_reply_to:
        m["In-Reply-To"] = in_reply_to
        m["References"] = in_reply_to
    m.set_content(body)
    if html:
        m.add_alternative(html, subtype="html")
    if attachment:
        name, data = attachment
        m.add_attachment(data, maintype="application", subtype="octet-stream", filename=name)
    return m.as_bytes()


class FakeIMAP:
    """Folders: {raw_name: {"attrs": [...], "msgs": {uid: {"raw": bytes, "flags": set()}}, "next": int}}"""

    capabilities = ("IMAP4REV1", "UIDPLUS")

    def __init__(self, folders: dict | None = None, delimiter: str = "/", login_ok=True, support_move=False):
        self.folders = folders if folders is not None else {"INBOX": {"attrs": ["\\HasNoChildren"], "msgs": {}, "next": 1}}
        self.delim = delimiter
        self.login_ok = login_ok
        self.selected: str | None = None
        self.readonly = True
        self.log: list[str] = []
        self.expunged: list[str] = []
        if support_move:
            self.capabilities = ("IMAP4REV1", "UIDPLUS", "MOVE")

    # ---- helpers for tests
    def add(self, folder: str, raw: bytes, flags=()) -> str:
        f = self.folders[folder]
        uid = str(f["next"])
        f["next"] += 1
        f["msgs"][uid] = {"raw": raw, "flags": set(flags)}
        return uid

    # ---- protocol
    def login(self, user, secret):
        self.log.append(f"LOGIN {user}")
        if not self.login_ok:
            import imaplib

            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Authentication failed.")
        return "OK", [b"Logged in"]

    def authenticate(self, mechanism, authobject):
        initial = authobject(None)
        shown = initial.decode("utf-8", "replace").replace("\x01", " ").strip() if isinstance(initial, bytes) else str(initial)
        self.log.append(f"AUTHENTICATE {mechanism} {shown}")
        if not self.login_ok or not shown.startswith("user=") or "auth=Bearer " not in shown:
            import imaplib

            raise imaplib.IMAP4.error("AUTHENTICATE failed.")
        return "OK", [b"AUTHENTICATE completed."]

    def logout(self):
        self.log.append("LOGOUT")
        return "BYE", [b""]

    def starttls(self, ssl_context=None):
        return "OK", [b""]

    def list(self):
        out = []
        for name, f in self.folders.items():
            attrs = " ".join(f["attrs"])
            q = f'"{name}"' if (" " in name or not name.isascii()) else name
            out.append(f'({attrs}) "{self.delim}" {q}'.encode())
        return "OK", out

    def select(self, mailbox, readonly=False):
        self.log.append(f"SELECT {mailbox} ro={readonly}")
        name = mailbox
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        elif " " in name:
            return "NO", [b"Mailbox doesn't exist: " + name.encode()]
        if name not in self.folders:
            return "NO", [b"Mailbox doesn't exist: " + name.encode()]
        self.selected, self.readonly = name, readonly
        return "OK", [str(len(self.folders[name]["msgs"])).encode()]

    def _msgs(self):
        assert self.selected, "SEARCH illegal in state AUTH"
        return self.folders[self.selected]["msgs"]

    def _match(self, uid, entry, crit: list[str]) -> bool:
        msg = email.message_from_bytes(entry["raw"])
        i = 0
        while i < len(crit):
            c = crit[i].upper()
            if c == "ALL":
                i += 1
            elif c in ("FROM", "TO", "SUBJECT", "TEXT"):
                val = crit[i + 1].strip('"').lower()
                hay = (msg.get(c.title()) or "") if c != "TEXT" else entry["raw"].decode("utf-8", "replace")
                if val not in hay.lower():
                    return False
                i += 2
            elif c == "HEADER":
                hdr, val = crit[i + 1], crit[i + 2]
                if val not in (msg.get(hdr) or ""):
                    return False
                i += 3
            elif c == "UNSEEN":
                if "\\Seen" in entry["flags"]:
                    return False
                i += 1
            elif c == "FLAGGED":
                if "\\Flagged" not in entry["flags"]:
                    return False
                i += 1
            elif c == "UNANSWERED":
                if "\\Answered" in entry["flags"]:
                    return False
                i += 1
            elif c in ("SINCE", "BEFORE"):
                i += 2  # date filters are not modelled; the tests pass explicit sets
            else:
                raise AssertionError(f"fake does not support {c}")
        return True

    def uid(self, cmd, *args):
        cmd = cmd.upper()
        self.log.append(f"UID {cmd} {' '.join(str(a) for a in args)}")
        if cmd == "SEARCH":
            charset, crit = args[0], [a for a in args[1:]]
            msgs = self._msgs()
            hits = [u for u, e in msgs.items() if self._match(u, e, list(crit))]
            return "OK", [" ".join(sorted(hits, key=int)).encode()]
        if cmd == "FETCH":
            seq, items = args[0], args[1]
            msgs = self._msgs()
            uids = [u for u in seq.split(",") if u in msgs] if not seq.endswith(":*") else list(msgs)
            out = []
            for u in uids:
                e = msgs[u]
                msg = email.message_from_bytes(e["raw"])
                flags = " ".join(sorted(e["flags"]))
                if "HEADER.FIELDS" in items:
                    fields = re.search(r"HEADER.FIELDS \(([^)]*)\)", items).group(1).split()
                    hdr = "".join(f"{k}: {msg.get(k)}\r\n" for k in [f.title() if f.upper() not in ("MESSAGE-ID", "IN-REPLY-TO") else {"MESSAGE-ID": "Message-ID", "IN-REPLY-TO": "In-Reply-To"}[f.upper()] for f in fields] if msg.get(k)) + "\r\n"
                    bs = '("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 10 1)'
                    if any(p.get_filename() for p in msg.walk()):
                        bs = '(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 10 1)("APPLICATION" "OCTET-STREAM" ("NAME" "x.bin") NIL NIL "BASE64" 8 NIL ("attachment" ("FILENAME" "x.bin")) NIL) "MIXED")'
                    prefix = f'{u} (UID {u} FLAGS ({flags}) RFC822.SIZE {len(e["raw"])} BODYSTRUCTURE {bs} BODY[HEADER.FIELDS (...)] {{{len(hdr)}}}'.encode()
                    out.append((prefix, hdr.encode()))
                    out.append(b")")
                else:
                    if "BODY[]" in items and not self.readonly:
                        e["flags"].add("\\Seen")
                    prefix = f"{u} (UID {u} FLAGS ({flags}) BODY[] {{{len(e['raw'])}}}".encode()
                    out.append((prefix, e["raw"]))
                    out.append(b")")
            return "OK", out
        if cmd == "COPY":
            seq, dst = args
            dst = dst.strip('"')
            if dst not in self.folders:
                return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
            msgs = self._msgs()
            for u in seq.split(","):
                if u in msgs:
                    self.add(dst, msgs[u]["raw"], msgs[u]["flags"])
            return "OK", [b"Copy completed"]
        if cmd == "MOVE":
            seq, dst = args
            dst = dst.strip('"')
            if dst not in self.folders:
                return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
            msgs = self._msgs()
            for u in seq.split(","):
                if u in msgs:
                    self.add(dst, msgs[u]["raw"], msgs[u]["flags"])
                    del msgs[u]
            return "OK", [b"Move completed"]
        if cmd == "STORE":
            seq, op, flags = args
            assert not self.readonly, "STORE on a read-only mailbox"
            fl = set(flags.strip("()").split())
            msgs = self._msgs()
            for u in seq.split(","):
                if u in msgs:
                    if op.startswith("+"):
                        msgs[u]["flags"] |= fl
                    else:
                        msgs[u]["flags"] -= fl
            return "OK", [b""]
        raise AssertionError(f"fake does not support UID {cmd}")

    def expunge(self):
        msgs = self._msgs()
        gone = [u for u, e in msgs.items() if "\\Deleted" in e["flags"]]
        for u in gone:
            del msgs[u]
        self.expunged += gone
        return "OK", [b""]

    def append(self, mailbox, flags, date_time, message):
        name = mailbox.strip('"')
        if name not in self.folders:
            return "NO", [b"[TRYCREATE] Mailbox doesn't exist"]
        self.add(name, message, set((flags or "").strip("()").split()))
        return "OK", [b"[APPENDUID 1 1] Append completed"]
