import pytest

from fake_imap import FakeIMAP, make_msg
from mailskill.mailbox import Mailbox, MailboxError, html_to_text, own_part
from mailskill.store import Account


def kasserver_like():
    """Folder layout as seen on a Dovecot/All-Inkl box: dot delimiter, UTF-7 names, special-use flags."""
    f = {
        "INBOX": {"attrs": ["\\HasChildren"], "msgs": {}, "next": 1},
        "INBOX.GV Spedition": {"attrs": ["\\HasNoChildren"], "msgs": {}, "next": 1},
        "Gesendet": {"attrs": ["\\HasNoChildren", "\\Sent"], "msgs": {}, "next": 1},
        "Entw&APw-rfe": {"attrs": ["\\HasNoChildren", "\\Drafts"], "msgs": {}, "next": 1},
        "Papierkorb": {"attrs": ["\\HasNoChildren", "\\Trash"], "msgs": {}, "next": 1},
        "Archiv": {"attrs": ["\\HasNoChildren"], "msgs": {}, "next": 1},
        "Spam": {"attrs": ["\\HasNoChildren", "\\Junk"], "msgs": {}, "next": 1},
    }
    return FakeIMAP(f, delimiter=".")


@pytest.fixture
def box(monkeypatch):
    fake = kasserver_like()
    acc = Account(address="me@example.com", host="imap.example.com", port=993, security="ssl")
    mb = Mailbox(acc, "secret")
    mb.conn = fake
    fake.login("me@example.com", "secret")
    return mb, fake


def test_connect_login_refused(monkeypatch):
    fake = FakeIMAP(login_ok=False)
    import mailskill.mailbox as mm

    monkeypatch.setattr(mm.imaplib, "IMAP4_SSL", lambda *a, **k: fake)
    mb = Mailbox(Account(address="me@example.com", host="h", port=993, security="ssl"), "bad")
    with pytest.raises(MailboxError) as e:
        mb.connect()
    assert "login refused" in str(e.value)


def test_folders_roles_from_attributes_and_names(box):
    mb, fake = box
    fs = mb.folders()
    roles = {f.role: f.name for f in fs if f.role}
    assert roles == {"inbox": "INBOX", "sent": "Gesendet", "drafts": "Entwürfe", "trash": "Papierkorb", "archive": "Archiv", "junk": "Spam"}
    assert mb.account.drafts_folder == "Entw&APw-rfe"     # raw name kept for the wire
    assert mb.account.delimiter == "."


def test_resolve_folder_by_role_decoded_name_and_case(box):
    mb, _ = box
    assert mb.resolve_folder("drafts") == "Entw&APw-rfe"
    assert mb.resolve_folder("Entwürfe") == "Entw&APw-rfe"
    assert mb.resolve_folder("gesendet") == "Gesendet"
    assert mb.resolve_folder("") == "INBOX"


def test_select_quotes_names_with_spaces_and_checks_status(box):
    mb, fake = box
    mb.select("INBOX.GV Spedition")
    assert fake.selected == "INBOX.GV Spedition"
    assert any(l.startswith('SELECT "INBOX.GV Spedition"') for l in fake.log)
    with pytest.raises(MailboxError) as e:
        mb.select("Does Not Exist")
    assert "not selectable" in str(e.value)


def test_search_and_summaries(box):
    mb, fake = box
    fake.add("INBOX", make_msg(from_="alice@example.com", subject="Invoice 42"), flags={"\\Seen"})
    fake.add("INBOX", make_msg(from_="bob@example.com", subject="Re: Invoice 42", attachment=("x.bin", b"123")))
    fake.add("INBOX", make_msg(from_="carol@example.com", subject="Lunch"), flags={"\\Seen", "\\Answered"})
    rows = mb.search("INBOX")
    assert [r.uid for r in rows] == ["3", "2", "1"]                 # newest first
    assert rows[1].has_attachments and not rows[0].has_attachments
    assert mb.search("INBOX", from_="bob")[0].subject == "Re: Invoice 42"
    assert [r.uid for r in mb.search("INBOX", subject="invoice 42")] == ["2", "1"]   # multi-word subject filtered client-side
    assert [r.uid for r in mb.search("INBOX", unseen=True)] == ["2"]
    assert [r.uid for r in mb.search("INBOX", unanswered=True)] == ["2", "1"]
    assert len(mb.search("INBOX", limit=2)) == 2
    assert mb.search("INBOX", from_="nobody") == []
    assert all("ro=True" in l for l in fake.log if l.startswith("SELECT"))


def test_fetch_keeps_unread_unless_asked(box):
    mb, fake = box
    uid = fake.add("INBOX", make_msg(subject="Hi", body="line one\nline two", attachment=("report.pdf", b"%PDF-")))
    full = mb.fetch(uid, "INBOX")
    assert full.text.strip() == "line one\nline two"
    assert full.attachments[0]["filename"] == "report.pdf" and full.attachments[0]["size"] == 5
    assert "\\Seen" not in fake.folders["INBOX"]["msgs"][uid]["flags"]
    mb.fetch(uid, "INBOX", mark_seen=True)
    assert "\\Seen" in fake.folders["INBOX"]["msgs"][uid]["flags"]
    with pytest.raises(MailboxError):
        mb.fetch("999", "INBOX")


def test_fetch_html_only_becomes_text(box):
    mb, fake = box
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = "a@b.c"; m["To"] = "me@example.com"; m["Subject"] = "html"
    m.set_content("<p>Hello <b>there</b></p><p>Second</p>", subtype="html")
    uid = fake.add("INBOX", m.as_bytes())
    full = mb.fetch(uid)
    assert full.text == "Hello there\nSecond"


def test_save_attachments_no_overwrite(box, tmp_path):
    mb, fake = box
    uid = fake.add("INBOX", make_msg(attachment=("../evil.txt", b"a")))
    full = mb.fetch(uid)
    p1 = mb.save_attachments(full, tmp_path)
    p2 = mb.save_attachments(full, tmp_path)
    assert p1[0].name == "evil.txt" and p2[0].name == "evil-1.txt"
    assert (tmp_path / "evil.txt").read_bytes() == b"a"


def test_own_replies_via_headers_and_fallback(box):
    mb, fake = box
    mid = "<orig-1@example.com>"
    uid = fake.add("INBOX", make_msg(from_="alice@example.com", subject="Offer", message_id=mid, date="Mon, 21 Sep 2026 10:00:00 +0000"))
    full = mb.fetch(uid)
    assert mb.own_replies(full) == []
    # reply threaded by In-Reply-To, sitting in Sent
    fake.add("Gesendet", make_msg(from_="me@example.com", to="alice@example.com", subject="Re: Offer", in_reply_to=mid, date="Mon, 21 Sep 2026 11:00:00 +0000"))
    r = mb.own_replies(full)
    assert len(r) == 1 and r[0]["folder"] == "Gesendet"
    # a second reply without headers, copied by the client into INBOX: found by subject + recipient + date
    fake.add("INBOX", make_msg(from_="me@example.com", to="alice@example.com", subject="AW: Offer", date="Mon, 21 Sep 2026 12:00:00 +0000"))
    r = mb.own_replies(full)
    assert {x["folder"] for x in r} == {"Gesendet", "INBOX"}
    # something older than the original with the same subject is not a reply
    fake.add("INBOX", make_msg(from_="me@example.com", to="alice@example.com", subject="Offer", date="Sun, 20 Sep 2026 12:00:00 +0000"))
    assert len(mb.own_replies(full)) == 2


def test_append_draft_goes_to_drafts_with_threading(box):
    mb, fake = box
    orig_uid = fake.add("INBOX", make_msg(from_="alice@example.com", subject="Offer", message_id="<o@x>"))
    orig = mb.fetch(orig_uid)
    folder = mb.append_draft("alice@example.com", "Re: Offer", "Thanks.\n", in_reply_to=orig.summary.message_id)
    assert folder == "Entw&APw-rfe"
    msgs = fake.folders["Entw&APw-rfe"]["msgs"]
    assert len(msgs) == 1
    import email

    d = email.message_from_bytes(next(iter(msgs.values()))["raw"])
    assert d["X-Unsent"] == "1" and d["In-Reply-To"] == "<o@x>" and d["From"] == "me@example.com"
    assert "\\Draft" in next(iter(msgs.values()))["flags"]
    assert not any(l.startswith("SEND") for l in fake.log)


def test_append_draft_with_attachment(box, tmp_path):
    mb, fake = box
    f = tmp_path / "a.txt"
    f.write_text("x")
    mb.append_draft("a@b.c", "s", "b", attachments=[str(f)])
    import email

    d = email.message_from_bytes(next(iter(fake.folders["Entw&APw-rfe"]["msgs"].values()))["raw"])
    assert [p.get_filename() for p in d.walk() if p.get_filename()] == ["a.txt"]


def test_move_copy_then_expunge_and_native_move(box):
    mb, fake = box
    u1 = fake.add("INBOX", make_msg(subject="1"))
    u2 = fake.add("INBOX", make_msg(subject="2"))
    assert mb.move([u1], "INBOX", "Archiv") == 1
    assert u1 not in fake.folders["INBOX"]["msgs"] and len(fake.folders["Archiv"]["msgs"]) == 1
    assert fake.expunged == [u1]
    with pytest.raises(MailboxError):
        mb.move([u2], "INBOX", "Nope")
    assert u2 in fake.folders["INBOX"]["msgs"]      # COPY failed, nothing deleted
    fake.capabilities = ("IMAP4REV1", "MOVE")
    assert mb.move([u2], "INBOX", "archive") == 1
    assert len(fake.folders["Archiv"]["msgs"]) == 2


def test_set_flags(box):
    mb, fake = box
    u = fake.add("INBOX", make_msg())
    mb.set_flags([u], "INBOX", add=["\\Flagged"])
    assert "\\Flagged" in fake.folders["INBOX"]["msgs"][u]["flags"]
    mb.set_flags([u], "INBOX", remove=["\\Flagged"])
    assert "\\Flagged" not in fake.folders["INBOX"]["msgs"][u]["flags"]


def test_html_to_text_and_own_part():
    assert html_to_text("<html><style>x{}</style><body><p>A&amp;B</p><br><div>C</div></body></html>") == "A&B\n\nC"
    assert own_part("my answer\n\nOn Mon, 21 Sep 2026, Alice wrote:\n> old") == "my answer"
    assert own_part("Antwort\n\nVon: Alice\nGesendet: x") == "Antwort"
    assert own_part("plain") == "plain"
