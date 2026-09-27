from datetime import datetime, timezone

from mailskill.util import (all_addrs, days_ago, decode_hdr, first_addr, imap_date, norm_subject, parse_date,
                            parse_flags, parse_list_line, parse_uid, quote_mailbox, redact, safe_filename,
                            utf7_decode, utf7_encode)


def test_decode_hdr_rfc2047_and_broken():
    assert decode_hdr("=?utf-8?q?Gr=C3=BC=C3=9Fe?=") == "Grüße"
    assert decode_hdr(None) == ""
    assert decode_hdr(b"plain") == "plain"


def test_norm_subject_strips_prefixes_in_several_languages():
    assert norm_subject("Re: AW: WG: Fwd:  Meeting  tomorrow") == "meeting tomorrow"
    assert norm_subject("RE[2]: x") == "x"
    assert norm_subject("Antwort: Angebot") == "angebot"
    assert norm_subject(None) == ""
    assert norm_subject("Retail numbers") == "retail numbers"   # 'Re' inside a word is not a prefix


def test_addresses():
    assert first_addr('"Alice Smith" <Alice@Example.com>') == "alice@example.com"
    assert first_addr("nothing here") == ""
    assert all_addrs("a@x.org, b@y.org", "c@z.org") == {"a@x.org", "b@y.org", "c@z.org"}


def test_dates():
    d = parse_date("Tue, 22 Sep 2026 10:15:00 +0200")
    assert d and d.tzinfo is not None
    assert parse_date("garbage") is None
    assert imap_date(datetime(2026, 9, 3)) == "3-Sep-2026"
    assert days_ago(1, datetime(2026, 1, 1, tzinfo=timezone.utc)) == "31-Dec-2025"


def test_quote_mailbox():
    assert quote_mailbox("INBOX") == "INBOX"
    assert quote_mailbox("INBOX/Erledigt") == "INBOX/Erledigt"
    assert quote_mailbox("INBOX.GV Spedition") == '"INBOX.GV Spedition"'
    assert quote_mailbox('"already"') == '"already"'
    assert quote_mailbox('say "hi"') == '"say \\"hi\\""'


def test_utf7_round_trip():
    assert utf7_decode("Entw&APw-rfe") == "Entwürfe"
    assert utf7_decode("INBOX/Sch&APY-nheitsklinik") == "INBOX/Schönheitsklinik"
    assert utf7_decode("Fish &- Chips") == "Fish & Chips"
    assert utf7_decode("plain") == "plain"
    for name in ("Entwürfe", "Gelöschte Elemente", "日本語", "Fish & Chips", "plain"):
        assert utf7_decode(utf7_encode(name)) == name


def test_parse_list_line():
    assert parse_list_line(b'(\\HasNoChildren \\Drafts) "/" "Entw&APw-rfe"') == (["\\HasNoChildren", "\\Drafts"], "/", "Entw&APw-rfe")
    assert parse_list_line(b'(\\HasChildren) "." INBOX') == (["\\HasChildren"], ".", "INBOX")
    assert parse_list_line(b'(\\Noselect) NIL "[Gmail]"') == (["\\Noselect"], "", "[Gmail]")
    assert parse_list_line(b"garbage") is None


def test_parse_flags_and_uid():
    prefix = b"3 (UID 41 FLAGS (\\Seen \\Answered) RFC822.SIZE 100 BODY[HEADER] {12}"
    assert parse_flags(prefix) == ["\\Seen", "\\Answered"]
    assert parse_uid(prefix) == "41"
    assert parse_flags(b"3 (FLAGS () UID 2") == []


def test_safe_filename():
    assert safe_filename("../../etc/passwd") == "passwd"
    assert safe_filename("C:\\x\\report:2026?.pdf") == "report_2026_.pdf"
    assert safe_filename("   ") == "attachment"
    assert safe_filename("=?utf-8?q?Bericht_M=C3=A4rz.pdf?=") == "Bericht März.pdf"


def test_redact_masks_key_value_but_not_prose():
    assert redact("token=abc123 more") == "token=*** more"
    assert redact("Authorization: Bearer eyJ.x.y") == "Authorization: Bearer ***"
    assert redact("Gmail needs an app password: 2-step verification") == "Gmail needs an app password: 2-step verification"
