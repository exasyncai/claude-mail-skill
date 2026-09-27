"""Tidy a mailbox from a rules file. Dry run by default: the plan is printed, nothing moves.

Rules file (JSON):
{
  "folder": "INBOX",
  "rules": [
    {"name": "newsletters", "from": ["news@", "@mailchimp.com"], "older_than_days": 7, "action": "move", "to": "Archive/Newsletters"},
    {"name": "notifications", "subject": ["[GitHub]", "Your order"], "action": "move", "to": "Archive"},
    {"name": "read old", "seen": true, "older_than_days": 60, "action": "move", "to": "archive"},
    {"name": "flag boss", "from": ["boss@example.com"], "action": "flag"},
    {"name": "junk", "from": ["@spammy.example"], "action": "trash"}
  ]
}

Match fields (all optional, all must match): from, recipient, subject, older_than_days,
newer_than_days, seen, unseen, has_attachments. List values are OR'ed, matched
case-insensitively as substrings. Actions: move (to a folder or a role like
"archive"/"trash"), trash, flag, unflag, seen, unseen. Delete is not an action:
"trash" moves to the Trash folder, the server empties it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .mailbox import Mailbox, Summary, thread_key
from .util import days_ago, first_addr, parse_date, utf7_decode

ACTIONS = {"move", "trash", "flag", "unflag", "seen", "unseen"}


@dataclass
class Planned:
    uid: str
    rule: str
    action: str
    target: str
    subject: str
    from_: str
    date: str

    def as_dict(self):
        d = self.__dict__.copy()
        d["from"] = d.pop("from_")
        return d


def load_rules(path: str | Path) -> dict:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(d.get("rules"), list) or not d["rules"]:
        raise ValueError("rules file needs a non-empty 'rules' list")
    for r in d["rules"]:
        if r.get("action") not in ACTIONS:
            raise ValueError(f"rule {r.get('name', '?')}: action must be one of {sorted(ACTIONS)}")
        if r["action"] == "move" and not r.get("to"):
            raise ValueError(f"rule {r.get('name', '?')}: move needs 'to'")
    d.setdefault("folder", "INBOX")
    return d


def _contains_any(hay: str, needles) -> bool:
    if isinstance(needles, str):
        needles = [needles]
    h = (hay or "").lower()
    return any(n.lower() in h for n in needles if n)


def matches(rule: dict, s: Summary, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if "from" in rule and not _contains_any(s.from_, rule["from"]):
        return False
    if "recipient" in rule and not _contains_any(s.to, rule["recipient"]):
        return False
    if "subject" in rule and not _contains_any(s.subject, rule["subject"]):
        return False
    if "seen" in rule and (("\\Seen" in s.flags) != bool(rule["seen"])):
        return False
    if "unseen" in rule and (("\\Seen" not in s.flags) != bool(rule["unseen"])):
        return False
    if "has_attachments" in rule and s.has_attachments != bool(rule["has_attachments"]):
        return False
    if "older_than_days" in rule or "newer_than_days" in rule:
        d = parse_date(s.date)
        if d is None:
            return False
        age = (now - d).days
        if "older_than_days" in rule and age < int(rule["older_than_days"]):
            return False
        if "newer_than_days" in rule and age > int(rule["newer_than_days"]):
            return False
    return True


def plan(rules: dict, summaries: list[Summary], now: datetime | None = None) -> list[Planned]:
    """First matching rule wins per message."""
    out: list[Planned] = []
    for s in summaries:
        for r in rules["rules"]:
            if matches(r, s, now):
                action = r["action"]
                target = r.get("to", "") if action == "move" else ("trash" if action == "trash" else "")
                out.append(Planned(uid=s.uid, rule=r.get("name", "?"), action=action, target=target,
                                   subject=s.subject, from_=s.from_, date=s.date))
                break
    return out


def apply(mb: Mailbox, folder: str, planned: list[Planned]) -> dict:
    """Execute a plan. Moves are grouped per target; flags per action. Returns counts."""
    counts: dict[str, int] = {}
    by_move: dict[str, list[str]] = {}
    flags: dict[str, list[str]] = {"flag": [], "unflag": [], "seen": [], "unseen": []}
    for p in planned:
        if p.action in ("move", "trash"):
            by_move.setdefault(p.target, []).append(p.uid)
        else:
            flags[p.action].append(p.uid)
    if flags["flag"]:
        mb.set_flags(flags["flag"], folder, add=["\\Flagged"]); counts["flag"] = len(flags["flag"])
    if flags["unflag"]:
        mb.set_flags(flags["unflag"], folder, remove=["\\Flagged"]); counts["unflag"] = len(flags["unflag"])
    if flags["seen"]:
        mb.set_flags(flags["seen"], folder, add=["\\Seen"]); counts["seen"] = len(flags["seen"])
    if flags["unseen"]:
        mb.set_flags(flags["unseen"], folder, remove=["\\Seen"]); counts["unseen"] = len(flags["unseen"])
    for target, uids in by_move.items():
        n = mb.move(uids, folder, target)
        counts["move:" + target] = n
    return counts


# ------------------------------------------------------------------ thread tidy


def plan_threads(inbox: list[Summary], sent: list[Summary], me: str) -> tuple[list[Planned], list[Summary]]:
    """Keep only the newest message per conversation in the inbox.

    For every conversation that has one of *my* sent messages in the window:
    - if the other side wrote last, their newest stays, older inbox messages go to the archive
    - if I wrote last, all inbox messages of that conversation go to the archive and my sent
      message is copied into the inbox as "waiting for reply" (returned separately)
    Only conversations with a sent message in the window are touched.
    """
    me = me.lower()
    to_archive: dict[str, Planned] = {}
    waiting: dict[str, Summary] = {}
    inbox_mids = {s.message_id for s in inbox if s.message_id}
    for s in sent:
        if first_addr(s.from_) != me:
            continue
        sdate = parse_date(s.date)
        if not sdate:
            continue
        key = thread_key(s)
        if not key:
            continue
        recipients = {a for a in (s.to or "").lower().replace(";", ",").split(",")}
        recipients = {first_addr(r) for r in recipients if r.strip()}
        members = [m for m in inbox if parse_date(m.date) and (
            (m.message_id and m.message_id == s.in_reply_to) or
            (s.message_id and m.in_reply_to == s.message_id) or
            (thread_key(m) == key and (first_addr(m.from_) in recipients or first_addr(m.from_) == me)))]
        if not members:
            continue
        theirs = [m for m in members if first_addr(m.from_) != me]
        newest_theirs = max(theirs, key=lambda m: parse_date(m.date), default=None)
        if newest_theirs and parse_date(newest_theirs.date) > sdate:
            for m in members:
                if m.uid != newest_theirs.uid:
                    to_archive[m.uid] = Planned(uid=m.uid, rule="threads", action="move", target="archive",
                                                subject=m.subject, from_=m.from_, date=m.date)
        else:
            for m in members:
                if m.message_id != s.message_id:
                    to_archive[m.uid] = Planned(uid=m.uid, rule="threads", action="move", target="archive",
                                                subject=m.subject, from_=m.from_, date=m.date)
            if s.message_id not in inbox_mids:
                cur = waiting.get(key)
                if not cur or sdate > parse_date(cur.date):
                    waiting[key] = s
    return list(to_archive.values()), list(waiting.values())


def tidy_threads(mb: Mailbox, days: int = 7, apply_changes: bool = False) -> dict:
    mb.folders()
    a = mb.account
    if not a.sent_folder:
        raise RuntimeError("no Sent folder found on this server")
    if not a.archive_folder:
        raise RuntimeError("no Archive folder found; create one named Archive (or pass --archive)")
    inbox = mb.headers_since("INBOX")
    sent = mb.headers_since(a.sent_folder, days_ago(days))
    to_archive, waiting = plan_threads(inbox, sent, a.address)
    result = {"inbox": len(inbox), "sent_in_window": len(sent), "archive": [p.as_dict() for p in to_archive],
              "waiting_for_reply": [s.as_dict() for s in waiting], "applied": False}
    if apply_changes:
        if waiting:
            mb.select(a.sent_folder, readonly=True)
            mb.conn.uid("COPY", ",".join(s.uid for s in waiting), "INBOX")
        if to_archive:
            mb.move([p.uid for p in to_archive], "INBOX", a.archive_folder)
        result["applied"] = True
    result["archive_folder"] = utf7_decode(a.archive_folder)
    return result
