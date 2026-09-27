"""Find the IMAP server for an email address, in this order:

1. Mozilla ISPDB (autoconfig.thunderbird.net) for the address domain
2. the provider's own autoconfig (autoconfig.<domain>, <domain>/.well-known)
3. MX records: known hosters are mapped directly, unknown MX domains are looked
   up in the ISPDB as well (a domain hosted at Hetzner, Kasserver, IONOS ...)
4. SRV records _imaps._tcp / _imap._tcp
5. port probe: imap.<domain>, mail.<domain>, <domain> on 993 then 143

Every candidate is verified with a real TLS connection and CAPABILITY before it
is accepted. Nothing here needs a password.

Network access is isolated in `fetch_url`, `resolve_mx`, `resolve_srv` and
`probe` so the tests can replace them.
"""
from __future__ import annotations

import json
import re
import socket
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict, field

TIMEOUT = 8
USER_AGENT = "claude-mail-skill/0.1 (+https://github.com/exasyncai/claude-mail-skill)"

M365_NOTE = ("Microsoft 365 and Outlook.com no longer accept passwords over IMAP (basic auth was switched off in 2022). "
             "This tool speaks IMAP with a password only, so this mailbox is not supported yet. OAuth support is on the list.")

# MX suffix -> (imap host, port, security, provider id, note)
KNOWN_MX = {
    "google.com": ("imap.gmail.com", 993, "ssl", "gmail",
                   "Gmail needs an app password: 2-step verification on, then myaccount.google.com/apppasswords."),
    "googlemail.com": ("imap.gmail.com", 993, "ssl", "gmail",
                       "Gmail needs an app password: 2-step verification on, then myaccount.google.com/apppasswords."),
    "outlook.com": ("outlook.office365.com", 993, "ssl", "microsoft365", M365_NOTE),
    "protection.outlook.com": ("outlook.office365.com", 993, "ssl", "microsoft365", M365_NOTE),
    "hotmail.com": ("outlook.office365.com", 993, "ssl", "microsoft365", M365_NOTE),
    "icloud.com": ("imap.mail.me.com", 993, "ssl", "icloud",
                   "iCloud needs an app-specific password: appleid.apple.com, Sign-In and Security, App-Specific Passwords."),
    "yahoodns.net": ("imap.mail.yahoo.com", 993, "ssl", "yahoo",
                     "Yahoo needs an app password (Account Security, Generate app password)."),
    "zoho.com": ("imap.zoho.com", 993, "ssl", "zoho", "Zoho: enable IMAP access in the mail settings first."),
    "zoho.eu": ("imap.zoho.eu", 993, "ssl", "zoho", "Zoho: enable IMAP access in the mail settings first."),
    "fastmail.com": ("imap.fastmail.com", 993, "ssl", "fastmail",
                     "Fastmail needs an app password (Settings, Privacy and Security, Integrations)."),
    "messagingengine.com": ("imap.fastmail.com", 993, "ssl", "fastmail",
                            "Fastmail needs an app password (Settings, Privacy and Security, Integrations)."),
    "protonmail.ch": (None, None, None, "proton",
                      "Proton Mail has no direct IMAP. Run Proton Mail Bridge locally and add the account with --host 127.0.0.1 --port 1143 --starttls."),
    "proton.me": (None, None, None, "proton",
                  "Proton Mail has no direct IMAP. Run Proton Mail Bridge locally and add the account with --host 127.0.0.1 --port 1143 --starttls."),
    "mail.protection.outlook.com": ("outlook.office365.com", 993, "ssl", "microsoft365", M365_NOTE),
    "kasserver.com": (None, 993, "ssl", "all-inkl",
                      "All-Inkl: the IMAP host is your KAS server (wXXXXXXX.kasserver.com), the MX record names it."),
    "mail.hostpoint.ch": ("asmtp.mail.hostpoint.ch", 993, "ssl", "hostpoint", None),
    "ionos.com": ("imap.ionos.com", 993, "ssl", "ionos", None),
    "ionos.de": ("imap.ionos.de", 993, "ssl", "ionos", None),
    "1and1.com": ("imap.ionos.com", 993, "ssl", "ionos", None),
    "kundenserver.de": ("imap.ionos.de", 993, "ssl", "ionos", None),
    "strato.de": ("imap.strato.de", 993, "ssl", "strato", None),
    "gmx.net": ("imap.gmx.net", 993, "ssl", "gmx", "GMX: enable IMAP under E-Mail, Einstellungen, POP3/IMAP Abruf."),
    "web.de": ("imap.web.de", 993, "ssl", "web.de", "web.de: enable IMAP under E-Mail, Einstellungen, POP3/IMAP."),
    "t-online.de": ("secureimap.t-online.de", 993, "ssl", "t-online", None),
    "posteo.de": ("posteo.de", 993, "ssl", "posteo", None),
    "mailbox.org": ("imap.mailbox.org", 993, "ssl", "mailbox.org", None),
    "hetzner.com": ("mail.your-server.de", 993, "ssl", "hetzner", None),
    "your-server.de": ("mail.your-server.de", 993, "ssl", "hetzner", None),
    "ovh.net": ("ssl0.ovh.net", 993, "ssl", "ovh", None),
    "mail.ovh.net": ("ssl0.ovh.net", 993, "ssl", "ovh", None),
    "mimecast.com": (None, None, None, "gateway",
                     "The MX points at a mail security gateway (Mimecast). The mailbox itself is usually Microsoft 365 or Google Workspace; "
                     "ask your admin for the IMAP host or pass it with --host."),
    "pphosted.com": (None, None, None, "gateway",
                     "The MX points at a mail security gateway (Proofpoint). Ask your admin for the IMAP host or pass it with --host."),
    "barracudanetworks.com": (None, None, None, "gateway",
                              "The MX points at a mail security gateway (Barracuda). Ask your admin for the IMAP host or pass it with --host."),
}


@dataclass
class Candidate:
    host: str
    port: int
    security: str          # "ssl" | "starttls"
    source: str            # where it came from
    username: str = ""     # "" means: use the full address
    provider: str = ""
    note: str | None = None
    verified: bool = False
    capabilities: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Discovery:
    address: str
    domain: str
    mx: list[str]
    candidates: list[Candidate]
    chosen: Candidate | None
    notes: list[str]

    def as_dict(self) -> dict:
        d = {
            "address": self.address,
            "domain": self.domain,
            "mx": self.mx,
            "candidates": [c.as_dict() for c in self.candidates],
            "chosen": self.chosen.as_dict() if self.chosen else None,
            "notes": self.notes,
        }
        return d


# ------------------------------------------------------------------ network


def fetch_url(url: str, timeout: int = TIMEOUT) -> bytes | None:
    """GET a URL, None on any error. Redirects are followed, only https and http."""
    if not url.startswith(("https://", "http://")):
        return None
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(512_000)
    except Exception:
        return None


def _run(cmd: list[str], timeout: int = TIMEOUT) -> str:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


def resolve_mx(domain: str) -> list[str]:
    """MX hosts, lowest preference first. Local resolver first, DNS-over-HTTPS as fallback."""
    hosts: list[tuple[int, str]] = []
    try:  # dnspython, if the user has it
        import dns.resolver  # type: ignore

        for r in dns.resolver.resolve(domain, "MX"):
            hosts.append((int(r.preference), str(r.exchange).rstrip(".").lower()))
    except Exception:
        pass
    if not hosts:
        out = _run(["nslookup", "-type=mx", domain])
        for m in re.finditer(r"(?i)mail exchanger\s*=\s*(?:(\d+)\s+)?([\w.-]+)", out):
            hosts.append((int(m.group(1) or 0), m.group(2).rstrip(".").lower()))
        for m in re.finditer(r"(?i)MX preference\s*=\s*(\d+),\s*mail exchanger\s*=\s*([\w.-]+)", out):
            hosts.append((int(m.group(1)), m.group(2).rstrip(".").lower()))
    if not hosts:
        raw = fetch_url(f"https://cloudflare-dns.com/dns-query?name={domain}&type=MX&ct=application/dns-json")
        if raw is None:
            raw = fetch_url(f"https://dns.google/resolve?name={domain}&type=MX")
        if raw:
            try:
                for a in json.loads(raw).get("Answer", []):
                    if a.get("type") == 15:
                        pref, host = a["data"].split(None, 1)
                        hosts.append((int(pref), host.rstrip(".").lower()))
            except Exception:
                pass
    seen, ordered = set(), []
    for _, h in sorted(hosts):
        if h and h not in seen:
            seen.add(h)
            ordered.append(h)
    return ordered


def resolve_srv(name: str) -> list[tuple[str, int]]:
    """SRV targets (host, port), best priority first."""
    res: list[tuple[int, str, int]] = []
    try:
        import dns.resolver  # type: ignore

        for r in dns.resolver.resolve(name, "SRV"):
            res.append((int(r.priority), str(r.target).rstrip(".").lower(), int(r.port)))
    except Exception:
        pass
    if not res:
        out = _run(["nslookup", "-type=srv", name])
        for m in re.finditer(r"(?i)priority\s*=\s*(\d+).*?port\s*=\s*(\d+).*?svr hostname\s*=\s*([\w.-]+)", out, re.S):
            res.append((int(m.group(1)), m.group(3).rstrip(".").lower(), int(m.group(2))))
        for m in re.finditer(r"(?i)service\s*=\s*(\d+)\s+\d+\s+(\d+)\s+([\w.-]+)", out):
            res.append((int(m.group(1)), m.group(3).rstrip(".").lower(), int(m.group(2))))
    if not res:
        raw = fetch_url(f"https://cloudflare-dns.com/dns-query?name={name}&type=SRV&ct=application/dns-json")
        if raw:
            try:
                for a in json.loads(raw).get("Answer", []):
                    if a.get("type") == 33:
                        pr, _w, port, host = a["data"].split()
                        res.append((int(pr), host.rstrip(".").lower(), int(port)))
            except Exception:
                pass
    return [(h, p) for _, h, p in sorted(res) if h and h != "."]


def probe(host: str, port: int, security: str, timeout: int = TIMEOUT) -> list[str] | None:
    """Open the connection the way an IMAP client would and return CAPABILITY, or None."""
    ctx = ssl.create_default_context()
    try:
        if security == "ssl":
            with socket.create_connection((host, port), timeout=timeout) as raw:
                with ctx.wrap_socket(raw, server_hostname=host) as s:
                    s.settimeout(timeout)
                    greeting = s.recv(4096).decode("utf-8", "replace")
                    if not greeting.startswith("* OK") and not greeting.startswith("* PREAUTH"):
                        return None
                    s.sendall(b"a1 CAPABILITY\r\n")
                    data = s.recv(8192).decode("utf-8", "replace")
        else:
            with socket.create_connection((host, port), timeout=timeout) as raw:
                raw.settimeout(timeout)
                greeting = raw.recv(4096).decode("utf-8", "replace")
                if not greeting.startswith("* OK"):
                    return None
                raw.sendall(b"a0 STARTTLS\r\n")
                if not raw.recv(4096).decode("utf-8", "replace").startswith("a0 OK"):
                    return None
                with ctx.wrap_socket(raw, server_hostname=host) as s:
                    s.settimeout(timeout)
                    s.sendall(b"a1 CAPABILITY\r\n")
                    data = s.recv(8192).decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"\* CAPABILITY ([^\r\n]+)", data)
    return m.group(1).split() if m else []


# ------------------------------------------------------------------ parsing


def parse_autoconfig(xml_bytes: bytes, address: str) -> list[Candidate]:
    """Mozilla autoconfig XML -> IMAP candidates with the username placeholder resolved."""
    out: list[Candidate] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return out
    local, _, domain = address.partition("@")
    for prov in root.iter("emailProvider"):
        pid = prov.get("id", "")
        for srv in prov.findall("incomingServer"):
            if srv.get("type") != "imap":
                continue
            host = (srv.findtext("hostname") or "").strip()
            port = int((srv.findtext("port") or "0").strip() or 0)
            sec = (srv.findtext("socketType") or "").strip().upper()
            user = (srv.findtext("username") or "%EMAILADDRESS%").strip()
            user = user.replace("%EMAILADDRESS%", address).replace("%EMAILLOCALPART%", local).replace("%EMAILDOMAIN%", domain)
            if not host or not port:
                continue
            security = "ssl" if sec == "SSL" else "starttls" if sec == "STARTTLS" else ""
            if not security:
                continue
            out.append(Candidate(host=host.lower(), port=port, security=security, source="autoconfig", username=user, provider=pid))
    # SSL before STARTTLS, keep document order otherwise
    out.sort(key=lambda c: 0 if c.security == "ssl" else 1)
    return out


def match_known_mx(mx_hosts: list[str]) -> tuple[Candidate | None, str | None, str]:
    """(candidate or None, note, provider) for the first MX with a known suffix."""
    for mx in mx_hosts:
        parts = mx.split(".")
        for i in range(len(parts) - 1):
            suffix = ".".join(parts[i:])
            if suffix in KNOWN_MX:
                host, port, sec, pid, note = KNOWN_MX[suffix]
                if pid == "all-inkl":
                    host = mx   # the MX itself is the mailbox server
                if host:
                    return Candidate(host=host, port=port, security=sec, source=f"mx:{suffix}", provider=pid, note=note), note, pid
                return None, note, pid
    return None, None, ""


def mx_domain(mx: str) -> str:
    """'mx1.mail.example-hoster.net' -> 'example-hoster.net' (best effort, 2 labels; 3 for co.uk-like)."""
    parts = mx.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# ------------------------------------------------------------------ main


def discover(address: str, *, probe_fn=probe, fetch_fn=fetch_url, mx_fn=resolve_mx, srv_fn=resolve_srv,
             verbose=None) -> Discovery:
    """Run all lookups for an address. `verbose` is a callable for progress lines or None."""
    say = verbose or (lambda _m: None)
    address = address.strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise ValueError(f"not an email address: {address!r}")
    domain = address.rsplit("@", 1)[1].lower()
    notes: list[str] = []
    cands: list[Candidate] = []

    def add(c: Candidate):
        for e in cands:
            if (e.host, e.port, e.security) == (c.host, c.port, c.security):
                if not e.username and c.username:
                    e.username = c.username
                return
        cands.append(c)

    # 1. ISPDB
    say(f"ISPDB lookup for {domain}")
    raw = fetch_fn(f"https://autoconfig.thunderbird.net/v1.1/{domain}")
    if raw:
        for c in parse_autoconfig(raw, address):
            c.source = "ispdb"
            add(c)

    # 2. provider autoconfig
    if not cands:
        for url in (f"https://autoconfig.{domain}/mail/config-v1.1.xml?emailaddress={address}",
                    f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml"):
            say(f"autoconfig {url.split('/')[2]}")
            raw = fetch_fn(url)
            if raw:
                for c in parse_autoconfig(raw, address):
                    c.source = "autoconfig:" + url.split("/")[2]
                    add(c)
                if cands:
                    break

    # 3. MX
    say(f"MX lookup for {domain}")
    mx = mx_fn(domain)
    known, note, pid = match_known_mx(mx)
    if note:
        notes.append(note)
    if known:
        add(known)
    elif mx and pid != "gateway":
        md = mx_domain(mx[0])
        if md != domain:
            say(f"ISPDB lookup for MX domain {md}")
            raw = fetch_fn(f"https://autoconfig.thunderbird.net/v1.1/{md}")
            if raw:
                for c in parse_autoconfig(raw, address):
                    c.source = f"ispdb:{md}"
                    add(c)
            # a hoster's MX often is the IMAP host, or imap./mail. on the hoster domain
            add(Candidate(host=mx[0], port=993, security="ssl", source="mx-host"))
            add(Candidate(host=f"imap.{md}", port=993, security="ssl", source="mx-domain"))
            add(Candidate(host=f"mail.{md}", port=993, security="ssl", source="mx-domain"))

    # 4. SRV
    for svc, sec, dport in (("_imaps._tcp", "ssl", 993), ("_imap._tcp", "starttls", 143)):
        say(f"SRV {svc}.{domain}")
        for host, port in srv_fn(f"{svc}.{domain}"):
            add(Candidate(host=host, port=port or dport, security=sec, source="srv"))

    # 5. guesses
    for h in (f"imap.{domain}", f"mail.{domain}", domain):
        add(Candidate(host=h, port=993, security="ssl", source="guess"))
        add(Candidate(host=h, port=143, security="starttls", source="guess"))

    # verify in order, first hit wins
    chosen = None
    for c in cands:
        if pid == "microsoft365" and c.host == "outlook.office365.com":
            continue   # would connect fine and then refuse every password; do not present it as working
        say(f"probe {c.host}:{c.port} {c.security} ({c.source})")
        caps = probe_fn(c.host, c.port, c.security)
        if caps is None:
            continue
        c.verified = True
        c.capabilities = caps
        upper = {x.upper() for x in caps}
        if "IMAP4REV1" in upper or "IMAP4REV2" in upper or not caps:
            chosen = c
            break
    if chosen is None and pid == "microsoft365":
        notes.append("No usable IMAP server found for a password login.")
    return Discovery(address=address, domain=domain, mx=mx, candidates=cands, chosen=chosen, notes=notes)


if __name__ == "__main__":  # quick manual check: python -m mailskill.discover me@example.com
    d = discover(sys.argv[1], verbose=lambda m: print("  " + m))
    print(json.dumps(d.as_dict(), indent=2))
