import pytest

from mailskill.discover import Candidate, discover, match_known_mx, mx_domain, parse_autoconfig

ISPDB_GMAIL = b"""<?xml version="1.0"?>
<clientConfig version="1.1">
  <emailProvider id="googlemail.com">
    <domain>gmail.com</domain>
    <incomingServer type="imap">
      <hostname>imap.gmail.com</hostname><port>993</port><socketType>SSL</socketType>
      <username>%EMAILADDRESS%</username><authentication>OAuth2</authentication>
    </incomingServer>
    <incomingServer type="pop3"><hostname>pop.gmail.com</hostname><port>995</port><socketType>SSL</socketType></incomingServer>
    <outgoingServer type="smtp"><hostname>smtp.gmail.com</hostname><port>465</port></outgoingServer>
  </emailProvider>
</clientConfig>"""

ISPDB_STARTTLS_FIRST = b"""<clientConfig version="1.1"><emailProvider id="x">
  <incomingServer type="imap"><hostname>imap.x.test</hostname><port>143</port><socketType>STARTTLS</socketType><username>%EMAILLOCALPART%</username></incomingServer>
  <incomingServer type="imap"><hostname>imap.x.test</hostname><port>993</port><socketType>SSL</socketType><username>%EMAILLOCALPART%</username></incomingServer>
</emailProvider></clientConfig>"""


def test_parse_autoconfig_picks_imap_and_resolves_placeholders():
    c = parse_autoconfig(ISPDB_GMAIL, "bob@gmail.com")
    assert len(c) == 1
    assert (c[0].host, c[0].port, c[0].security, c[0].username, c[0].provider) == ("imap.gmail.com", 993, "ssl", "bob@gmail.com", "googlemail.com")


def test_parse_autoconfig_prefers_ssl_and_local_part():
    c = parse_autoconfig(ISPDB_STARTTLS_FIRST, "bob@x.test")
    assert [(x.port, x.security) for x in c] == [(993, "ssl"), (143, "starttls")]
    assert c[0].username == "bob"


def test_parse_autoconfig_garbage():
    assert parse_autoconfig(b"<not xml", "a@b.c") == []


def test_match_known_mx():
    c, note, pid = match_known_mx(["alt1.gmail-smtp-in.l.google.com"])
    assert c.host == "imap.gmail.com" and pid == "gmail" and "app password" in note
    c, note, pid = match_known_mx(["w0123abc.kasserver.com"])
    assert c.host == "w0123abc.kasserver.com" and c.port == 993 and pid == "all-inkl"
    c, note, pid = match_known_mx(["example-com.mail.protection.outlook.com"])
    assert c.host == "outlook.office365.com" and pid == "microsoft365" and "OAuth" in note
    c, note, pid = match_known_mx(["mx1.unknown-hoster.example"])
    assert c is None and note is None and pid == ""
    c, note, pid = match_known_mx(["us-smtp-inbound-1.mimecast.com"])
    assert c is None and pid == "gateway" and "--host" in note


def test_mx_domain():
    assert mx_domain("mx1.mail.hoster.net") == "hoster.net"
    assert mx_domain("mx.example.co.uk") == "example.co.uk"


def _probe_factory(ok: set[tuple[str, int]]):
    calls = []

    def probe(host, port, security):
        calls.append((host, port, security))
        return ["IMAP4rev1", "IDLE"] if (host, port) in ok else None

    probe.calls = calls
    return probe


def test_discover_ispdb_first_hit_wins():
    probe = _probe_factory({("imap.gmail.com", 993)})
    fetch = lambda url: ISPDB_GMAIL if "thunderbird.net/v1.1/gmail.com" in url else None
    d = discover("bob@gmail.com", probe_fn=probe, fetch_fn=fetch, mx_fn=lambda dom: ["gmail-smtp-in.l.google.com"], srv_fn=lambda n: [])
    assert d.chosen and d.chosen.host == "imap.gmail.com" and d.chosen.source == "ispdb"
    assert probe.calls == [("imap.gmail.com", 993, "ssl")]      # stopped at the first verified server
    assert any("app password" in n for n in d.notes)


def test_discover_falls_back_to_mx_hoster_and_guesses():
    probe = _probe_factory({("mail.hoster.net", 993)})
    d = discover("bob@firma.example", probe_fn=probe, fetch_fn=lambda url: None,
                 mx_fn=lambda dom: ["mx1.hoster.net"], srv_fn=lambda n: [])
    assert d.chosen and d.chosen.host == "mail.hoster.net" and d.chosen.source == "mx-domain"
    hosts = [c.host for c in d.candidates]
    assert hosts[:3] == ["mx1.hoster.net", "imap.hoster.net", "mail.hoster.net"]
    assert "imap.firma.example" in hosts and "firma.example" in hosts


def test_discover_srv_records():
    probe = _probe_factory({("imap.srv.example", 9993)})
    d = discover("bob@firma.example", probe_fn=probe, fetch_fn=lambda url: None, mx_fn=lambda dom: [],
                 srv_fn=lambda n: [("imap.srv.example", 9993)] if n.startswith("_imaps") else [])
    assert d.chosen and (d.chosen.host, d.chosen.port, d.chosen.source) == ("imap.srv.example", 9993, "srv")


def test_discover_microsoft365_is_not_presented_as_working():
    probe = _probe_factory({("outlook.office365.com", 993)})
    d = discover("bob@corp.example", probe_fn=probe, fetch_fn=lambda url: None,
                 mx_fn=lambda dom: ["corp-example.mail.protection.outlook.com"], srv_fn=lambda n: [])
    assert d.chosen is None
    assert not any(c[0] == "outlook.office365.com" for c in probe.calls)
    assert any("OAuth" in n for n in d.notes)


def test_discover_nothing_found():
    d = discover("bob@dead.example", probe_fn=_probe_factory(set()), fetch_fn=lambda url: None, mx_fn=lambda dom: [], srv_fn=lambda n: [])
    assert d.chosen is None and len(d.candidates) == 6


def test_discover_rejects_non_address():
    with pytest.raises(ValueError):
        discover("not-an-address", probe_fn=_probe_factory(set()), fetch_fn=lambda u: None, mx_fn=lambda d: [], srv_fn=lambda n: [])


def test_candidate_dict():
    assert Candidate(host="h", port=1, security="ssl", source="s").as_dict()["verified"] is False
