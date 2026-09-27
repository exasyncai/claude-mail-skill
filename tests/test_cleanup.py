import json
from datetime import datetime, timezone

import pytest

from fake_imap import FakeIMAP, make_msg
from mailskill import cleanup as cl
from mailskill.mailbox import Mailbox, Summary
from mailskill.store import Account

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def S(uid, **kw):
    d = dict(uid=uid, date="Sun, 20 Sep 2026 10:00:00 +0000", from_="news@shop.example", to="me@example.com",
             subject="Weekly deals", flags=[], has_attachments=False)
    d.update(kw)
    return Summary(**d)


def test_load_rules_validates(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"rules": [{"name": "x", "action": "move"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        cl.load_rules(p)
    p.write_text(json.dumps({"rules": [{"name": "x", "action": "delete"}]}), encoding="utf-8")
    with pytest.raises(ValueError):
        cl.load_rules(p)
    p.write_text(json.dumps({"rules": []}), encoding="utf-8")
    with pytest.raises(ValueError):
        cl.load_rules(p)
    p.write_text(json.dumps({"rules": [{"name": "ok", "from": ["a@"], "action": "trash"}]}), encoding="utf-8")
    assert cl.load_rules(p)["folder"] == "INBOX"


def test_matches_fields():
    s = S("1", flags=["\\Seen"])
    assert cl.matches({"from": ["NEWS@"]}, s, NOW)
    assert not cl.matches({"from": "boss@"}, s, NOW)
    assert cl.matches({"subject": ["nothing", "deals"]}, s, NOW)
    assert cl.matches({"seen": True}, s, NOW) and not cl.matches({"unseen": True}, s, NOW)
    assert cl.matches({"older_than_days": 7}, s, NOW) and not cl.matches({"older_than_days": 8}, s, NOW)
    assert cl.matches({"newer_than_days": 7}, s, NOW) and not cl.matches({"newer_than_days": 6}, s, NOW)
    assert not cl.matches({"older_than_days": 1}, S("2", date="garbage"), NOW)
    assert cl.matches({"has_attachments": False}, s, NOW)
    assert cl.matches({}, s, NOW)


def test_plan_first_rule_wins():
    rules = {"rules": [
        {"name": "flag boss", "from": ["boss@"], "action": "flag"},
        {"name": "old news", "from": ["news@"], "older_than_days": 3, "action": "move", "to": "Archive/News"},
        {"name": "junk", "from": ["news@"], "action": "trash"},
    ]}
    rows = [S("1"), S("2", from_="boss@corp.example"), S("3", from_="news@shop.example", date="Sat, 26 Sep 2026 10:00:00 +0000"), S("4", from_="friend@x.y")]
    p = cl.plan(rules, rows, NOW)
    assert [(x.uid, x.action, x.target) for x in p] == [("1", "move", "Archive/News"), ("2", "flag", ""), ("3", "trash", "trash")]


def _mb():
    fake = FakeIMAP({
        "INBOX": {"attrs": [], "msgs": {}, "next": 1},
        "Sent": {"attrs": ["\\Sent"], "msgs": {}, "next": 1},
        "Archive": {"attrs": ["\\Archive"], "msgs": {}, "next": 1},
        "Trash": {"attrs": ["\\Trash"], "msgs": {}, "next": 1},
    })
    mb = Mailbox(Account(address="me@example.com", host="h", port=993, security="ssl"), "s")
    mb.conn = fake
    return mb, fake


def test_apply_moves_flags_and_trash():
    mb, fake = _mb()
    u1 = fake.add("INBOX", make_msg(from_="news@shop.example", subject="deals"))
    u2 = fake.add("INBOX", make_msg(from_="boss@corp.example", subject="urgent"))
    u3 = fake.add("INBOX", make_msg(from_="spam@bad.example", subject="win"))
    rules = {"rules": [
        {"name": "n", "from": ["news@"], "action": "move", "to": "archive"},
        {"name": "b", "from": ["boss@"], "action": "flag"},
        {"name": "s", "from": ["spam@"], "action": "trash"},
    ]}
    mb.folders()
    planned = cl.plan(rules, mb.headers_since("INBOX"), NOW)
    assert len(planned) == 3
    counts = cl.apply(mb, "INBOX", planned)
    assert counts == {"flag": 1, "move:archive": 1, "move:trash": 1}
    assert list(fake.folders["INBOX"]["msgs"]) == [u2]
    assert "\\Flagged" in fake.folders["INBOX"]["msgs"][u2]["flags"]
    assert len(fake.folders["Archive"]["msgs"]) == 1 and len(fake.folders["Trash"]["msgs"]) == 1


def test_plan_threads():
    me = "me@example.com"
    # conversation A: I wrote last -> everything of A in INBOX goes to archive, my sent mail is "waiting"
    a1 = S("1", from_="alice@x.y", subject="Project A", date="Mon, 21 Sep 2026 09:00:00 +0000", message_id="<a1>")
    a_sent = S("s1", from_=me, to="alice@x.y", subject="Re: Project A", date="Mon, 21 Sep 2026 10:00:00 +0000", message_id="<as1>", in_reply_to="<a1>")
    # conversation B: they wrote last -> their newest stays, older B mails go to archive
    b1 = S("2", from_="bob@x.y", subject="Project B", date="Tue, 22 Sep 2026 09:00:00 +0000", message_id="<b1>")
    b_sent = S("s2", from_=me, to="bob@x.y", subject="Re: Project B", date="Tue, 22 Sep 2026 10:00:00 +0000", message_id="<bs>", in_reply_to="<b1>")
    b2 = S("3", from_="bob@x.y", subject="Re: Project B", date="Tue, 22 Sep 2026 11:00:00 +0000", message_id="<b2>", in_reply_to="<bs>")
    # conversation C: no sent mail in window -> untouched
    c1 = S("4", from_="carol@x.y", subject="Project C", date="Wed, 23 Sep 2026 09:00:00 +0000", message_id="<c1>")
    archive, waiting = cl.plan_threads([a1, b1, b2, c1], [a_sent, b_sent], me)
    assert sorted(p.uid for p in archive) == ["1", "2"]
    assert [w.uid for w in waiting] == ["s1"]


def test_tidy_threads_dry_run_and_apply():
    mb, fake = _mb()
    fake.add("INBOX", make_msg(from_="alice@x.y", to="me@example.com", subject="Topic", message_id="<t1>", date="Mon, 21 Sep 2026 09:00:00 +0000"))
    fake.add("Sent", make_msg(from_="me@example.com", to="alice@x.y", subject="Re: Topic", in_reply_to="<t1>", message_id="<t2>", date="Mon, 21 Sep 2026 10:00:00 +0000"))
    res = cl.tidy_threads(mb, days=30, apply_changes=False)
    assert len(res["archive"]) == 1 and len(res["waiting_for_reply"]) == 1 and res["applied"] is False
    assert len(fake.folders["INBOX"]["msgs"]) == 1
    res = cl.tidy_threads(mb, days=30, apply_changes=True)
    assert res["applied"] is True
    inbox = fake.folders["INBOX"]["msgs"]
    assert len(inbox) == 1 and b"<t2>" in next(iter(inbox.values()))["raw"]      # my sent mail now waits in INBOX
    assert len(fake.folders["Archive"]["msgs"]) == 1
