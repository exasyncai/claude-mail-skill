"""Command line: `mailskill <command>`. Human output by default, `--json` for tools.

Exit codes: 0 ok, 1 something failed (message on stderr), 2 usage, 3 no account / no secret.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path

from . import __version__, setup, store
from .discover import discover
from .mailbox import Mailbox, MailboxError, own_part
from .oauth import OAuthError
from .util import days_ago, redact, utf7_decode

for _s in (sys.stdout, sys.stderr):  # Windows consoles are cp1252; subjects carry anything
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def say(msg: str, *, err: bool = False):
    print(redact(msg), file=sys.stderr if err else sys.stdout, flush=True)


def out_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str), flush=True)


def _account(args) -> store.Account:
    acc = store.get_account(getattr(args, "account", None))
    if not acc:
        if store.list_accounts():
            say("Which account? Pass --account <address> (see: mailskill accounts).", err=True)
        else:
            say("No account yet. Start with: mailskill add you@example.com", err=True)
        sys.exit(3)
    return acc


def _secret(acc: store.Account) -> str:
    """Password from the keychain, or for a Microsoft account a fresh access token (silent refresh)."""
    if acc.auth == "oauth":
        try:
            return setup.access_token(acc)
        except (setup.SetupError, OAuthError) as e:
            say(str(e), err=True)
            sys.exit(3)
    s = store.get_secret(acc.address)
    if not s:
        say(f"No mailbox login stored for {acc.address}. Run: mailskill add {acc.address}  "
            f"(or export {store.PW_ENV} for this session)", err=True)
        sys.exit(3)
    return s


def _open(args) -> tuple[store.Account, Mailbox]:
    acc = _account(args)
    mb = Mailbox(acc, _secret(acc))
    try:
        mb.connect()
    except MailboxError as e:
        say(str(e), err=True)
        sys.exit(1)
    except Exception as e:
        say(f"connection to {acc.host}:{acc.port} failed: {e}", err=True)
        sys.exit(1)
    return acc, mb


def _persist_roles(acc: store.Account):
    """Folder roles are learned on the first LIST; keep them so the next call skips the guessing."""
    try:
        store.save_account(acc)
    except Exception:
        pass


# ------------------------------------------------------------------ commands


def _report_added(acc: store.Account, folders, where: str):
    how = "Signed in with Microsoft" if acc.auth == "oauth" else "Login ok"
    kept = "token" if acc.auth == "oauth" else "secret"
    say(f"{how}, {len(folders)} folders. The {kept} is stored in: {where} ({store.keyring_backend_name()}).")
    say(f"Settings: {store.accounts_file()}  (no secret in there)")
    roles = {f.role: f.name for f in folders if f.role}
    if roles:
        say("Folders: " + ", ".join(f"{k}={v}" for k, v in roles.items()))
    say("Try:  mailskill search --unseen --limit 10")


def cmd_add(args):
    address = (args.address or "").strip().lower()
    use_gui = not args.no_gui and not args.host and not args.dry_run and not args.json and not os.environ.get(store.PW_ENV)
    if not address and use_gui:
        from . import gui

        if gui.available():
            res = gui.run_add_dialog(client_id=args.client_id)
            if not res:
                say("Window closed, nothing stored.", err=True)
                return 1
            say(f"Added {res['address']} ({res['host']}, {res['folders']} folders). Try:  mailskill search --unseen --limit 10")
            return 0
        say("Note: tkinter is not available in this Python, using the terminal instead.")
    if not address:
        if not sys.stdin.isatty():
            say("Usage: mailskill add you@example.com  (no window available here)", err=True)
            return 2
        try:
            address = input("Email address: ").strip().lower()
        except EOFError:
            address = ""
        if not address:
            say("No address, nothing stored.", err=True)
            return 2

    say(f"Looking up the mail server for {address} ..." if not args.host else f"Using {args.host} as given.")
    try:
        plan = setup.plan_for(address, host=args.host or "", port=args.port or 0, starttls=args.starttls,
                              username=args.username or "", discover_fn=discover,
                              verbose=(lambda m: say("  " + m)) if args.verbose else None)
    except setup.SetupError as e:
        say(str(e), err=True)
        return 2
    for n in plan.notes:
        say("Note: " + n)
    if plan.kind == "none":
        say(plan.hint, err=True)
        if plan.tried:
            say("Tried: " + ", ".join(plan.tried[:8]) + (" ..." if len(plan.tried) > 8 else ""), err=True)
        return 1
    cand = plan.candidate
    if cand.source != "manual":
        say(f"Found: {cand.host}:{cand.port} {cand.security} (via {cand.source})")
    acc = setup.account_for(plan)
    if args.dry_run:
        out_json(acc.__dict__) if args.json else say("Dry run: nothing stored.")
        return 0

    if plan.kind == "oauth":
        try:
            acc, folders, where = setup.sign_in_microsoft(plan, client_id=args.client_id, status=say, make_default=args.default)
        except (setup.SetupError, OAuthError) as e:
            say(str(e), err=True)
            return 1
        _report_added(acc, folders, where)
        return 0

    secret = os.environ.get(store.PW_ENV) or ""
    if not secret:
        if not sys.stdin.isatty():
            say(f"No terminal for the prompt. Export {store.PW_ENV} and run again.", err=True)
            return 3
        if plan.hint:
            say(plan.hint + (f"  {plan.link}" if plan.link else ""))
        secret = getpass.getpass(f"Password for {acc.login} (app password where the provider needs one): ")
        if not secret:
            say("Empty input, nothing stored.", err=True)
            return 2
    say(f"Testing login at {acc.host} ...")
    try:
        folders = setup.test_login(acc, secret)
    except setup.SetupError as e:
        say(str(e), err=True)
        if e.hint:
            say(e.hint + (f"  {e.link}" if e.link else ""), err=True)
        return 1
    where = setup.finish_password(acc, secret, make_default=args.default)
    if where.startswith("not stored"):
        say("Login worked; the secret was not stored. Export it per session or install keyring.", err=True)
    _report_added(acc, folders, where)
    return 0


def cmd_accounts(args):
    accs = store.list_accounts()
    default = store.default_address()
    if args.json:
        out_json([{**a.__dict__, "default": a.address == default} for a in accs])
        return 0
    if not accs:
        say("No accounts. Start with: mailskill add you@example.com")
        return 0
    for a in accs:
        mark = "*" if a.address == default else " "
        say(f"{mark} {a.address:40} {a.host}:{a.port} {a.security}" + (f"  user={a.username}" if a.username else "")
            + ("  (Microsoft sign-in)" if a.auth == "oauth" else ""))
    say(f"Keychain backend: {store.keyring_backend_name()}")
    return 0


def cmd_remove(args):
    if store.remove_account(args.address):
        say(f"Removed {args.address} and its stored secret.")
        return 0
    say(f"No such account: {args.address}", err=True)
    return 1


def cmd_discover(args):
    try:
        d = discover(args.address, verbose=(lambda m: say("  " + m)) if args.verbose else None)
    except ValueError as e:
        say(str(e), err=True)
        return 2
    if args.json:
        out_json(d.as_dict())
        return 0 if d.chosen else 1
    say(f"Domain: {d.domain}   MX: {', '.join(d.mx) or '(none)'}")
    for n in d.notes:
        say("Note: " + n)
    for c in d.candidates:
        flag = "OK " if c.verified else "   "
        say(f"{flag} {c.host}:{c.port} {c.security:8} {c.source}")
    if d.chosen:
        say(f"Chosen: {d.chosen.host}:{d.chosen.port} {d.chosen.security}")
        return 0
    say("No server answered.", err=True)
    return 1


def cmd_folders(args):
    acc, mb = _open(args)
    with mb:
        folders = mb.folders()
    _persist_roles(acc)
    if args.json:
        out_json([f.as_dict() for f in folders])
        return 0
    for f in folders:
        say(f"{(f.role or ''):8} {f.name}" + (f"   [{f.raw}]" if f.raw != f.name else ""))
    return 0


def _print_summaries(rows, folder=""):
    for s in rows:
        flags = ("A" if "\\Answered" in s.flags else " ") + (" " if "\\Seen" in s.flags else "N") + ("!" if "\\Flagged" in s.flags else " ") + ("@" if s.has_attachments else " ")
        say(f"{s.uid:>6} {flags} {s.date[:22]:22} {s.from_[:32]:32} {s.subject[:70]}")


def cmd_search(args):
    acc, mb = _open(args)
    with mb:
        since = args.since or (days_ago(args.days) if args.days else "")
        try:
            rows = mb.search(args.folder, from_=args.from_ or "", to=args.to or "", subject=args.subject or "",
                             text=args.text or "", since=since, before=args.before or "", unseen=args.unseen,
                             flagged=args.flagged, unanswered=args.unanswered, limit=args.limit)
        except MailboxError as e:
            say(str(e), err=True)
            return 1
    _persist_roles(acc)
    if args.json:
        out_json([s.as_dict() for s in rows])
        return 0
    if not rows:
        say("No matches.")
        return 0
    say(f"   UID flags date                   from                             subject     (A=answered N=new !=flagged @=attachment)")
    _print_summaries(rows)
    return 0


def cmd_read(args):
    acc, mb = _open(args)
    with mb:
        try:
            full = mb.fetch(args.uid, args.folder, mark_seen=args.mark_seen)
        except MailboxError as e:
            say(str(e), err=True)
            return 1
        replies = mb.own_replies(full) if args.replies else None
        saved = mb.save_attachments(full, args.save) if args.save else []
    _persist_roles(acc)
    text = full.text if not args.own_part else own_part(full.text)
    if args.max_chars and len(text) > args.max_chars:
        text = text[: args.max_chars] + f"\n... [truncated, {len(full.text)} characters in total]"
    if args.json:
        d = full.as_dict()
        d["text"] = text
        d["attachments"] = [{k: v for k, v in a.items() if k != "_payload"} for a in full.attachments]
        if replies is not None:
            d["own_replies"] = replies
        if saved:
            d["saved"] = [str(p) for p in saved]
        out_json(d)
        return 0
    s = full.summary
    say(f"UID     : {s.uid}   folder: {args.folder}")
    say(f"From    : {s.from_}")
    say(f"To      : {s.to}")
    for k, v in full.headers.items():
        say(f"{k.title():8}: {v}")
    say(f"Date    : {s.date}")
    say(f"Subject : {s.subject}")
    answered = "yes" if "\\Answered" in s.flags else "no"
    say(f"Flags   : {' '.join(s.flags) or '-'}   answered: {answered}")
    if full.attachments:
        say("Attachments: " + ", ".join(f"[{a['index']}] {a['filename']} ({a['size']} bytes)" for a in full.attachments))
    say("-" * 78)
    say(text)
    if saved:
        say("-" * 78)
        for p in saved:
            say(f"saved {p}")
    if replies is not None:
        say("-" * 78)
        if not replies:
            say("Own replies: none found (checked Sent and INBOX).")
        for r in replies:
            say(f"Own reply in {r['folder']}: UID {r['uid']} | {r['date']} | to {r['to']}")
    return 0


def cmd_attachments(args):
    acc, mb = _open(args)
    with mb:
        try:
            full = mb.fetch(args.uid, args.folder)
        except MailboxError as e:
            say(str(e), err=True)
            return 1
        only = [int(x) for x in args.only.split(",")] if args.only else None
        saved = mb.save_attachments(full, args.save, only)
    if args.json:
        out_json({"uid": args.uid, "saved": [str(p) for p in saved],
                  "attachments": [{k: v for k, v in a.items() if k != "_payload"} for a in full.attachments]})
        return 0
    if not full.attachments:
        say("No attachments.")
    for p in saved:
        say(f"saved {p}")
    return 0


def cmd_replied(args):
    acc, mb = _open(args)
    with mb:
        try:
            full = mb.fetch(args.uid, args.folder)
        except MailboxError as e:
            say(str(e), err=True)
            return 1
        replies = mb.own_replies(full)
    answered = "\\Answered" in full.summary.flags
    if args.json:
        out_json({"uid": args.uid, "subject": full.summary.subject, "answered_flag": answered, "own_replies": replies,
                  "replied": bool(answered or replies)})
        return 0
    say(f"Subject      : {full.summary.subject}")
    say(f"Answered flag: {'yes' if answered else 'no'}")
    if replies:
        for r in replies:
            say(f"Own reply    : {r['folder']} UID {r['uid']} on {r['date']} to {r['to']}")
    else:
        say("Own reply    : none found in Sent or INBOX")
    say("Verdict      : " + ("already replied" if (answered or replies) else "no reply yet"))
    return 0


def cmd_draft(args):
    acc, mb = _open(args)
    body = Path(args.body).read_text(encoding="utf-8") if args.body != "-" else sys.stdin.read()
    html = Path(args.html).read_text(encoding="utf-8") if args.html else ""
    in_reply_to, references, subject, to = args.in_reply_to or "", "", args.subject, args.to
    with mb:
        if args.reply_to_uid:
            try:
                orig = mb.fetch(args.reply_to_uid, args.folder)
            except MailboxError as e:
                say(str(e), err=True)
                return 1
            in_reply_to = orig.summary.message_id
            references = orig.headers.get("References", "")
            subject = subject or ("Re: " + orig.summary.subject if not orig.summary.subject.lower().startswith("re:") else orig.summary.subject)
            to = to or (orig.headers.get("Reply-To") or orig.summary.from_)
        if not to or not subject:
            say("--to and --subject are required (or --reply-to-uid)", err=True)
            return 2
        for p in args.attach or []:
            if not Path(p).is_file():
                say(f"attachment not found: {p}", err=True)
                return 2
        if args.dry_run:
            out_json({"to": to, "cc": args.cc, "subject": subject, "in_reply_to": in_reply_to, "body_chars": len(body),
                      "attachments": args.attach or [], "folder": args.drafts_folder or "(Drafts)", "dry_run": True})
            return 0
        try:
            folder = mb.append_draft(to, subject, body, cc=args.cc or "", bcc=args.bcc or "", html=html,
                                     attachments=args.attach, in_reply_to=in_reply_to, references=references,
                                     folder=args.drafts_folder or "")
        except MailboxError as e:
            say(str(e), err=True)
            return 1
    _persist_roles(acc)
    if args.json:
        out_json({"ok": True, "folder": utf7_decode(folder), "to": to, "subject": subject})
    else:
        say(f"Draft saved to {utf7_decode(folder)}: '{subject}' -> {to}. Nothing was sent.")
    return 0


def cmd_cleanup(args):
    from . import cleanup as cl

    try:
        rules = cl.load_rules(args.rules)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        say(f"rules file: {e}", err=True)
        return 2
    folder = args.folder or rules.get("folder", "INBOX")
    acc, mb = _open(args)
    with mb:
        mb.folders()
        rows = mb.headers_since(folder, days_ago(args.days) if args.days else "")
        planned = cl.plan(rules, rows)
        counts = cl.apply(mb, folder, planned) if args.apply and planned else {}
    _persist_roles(acc)
    if args.json:
        out_json({"folder": folder, "scanned": len(rows), "planned": [p.as_dict() for p in planned],
                  "applied": bool(args.apply), "counts": counts})
        return 0
    say(f"Scanned {len(rows)} messages in {folder}; {len(planned)} match a rule.")
    for p in planned:
        say(f"  {p.action:6} {p.target:24} [{p.rule}] {p.date[:16]:16} {p.from_[:30]:30} {p.subject[:60]}")
    if not args.apply:
        say("Dry run. Nothing changed. Add --apply to execute this plan.")
    else:
        say("Applied: " + ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "Nothing to do.")
    return 0


def cmd_threads(args):
    from . import cleanup as cl

    acc, mb = _open(args)
    with mb:
        if args.archive:
            acc.archive_folder = mb.resolve_folder(args.archive)
        try:
            res = cl.tidy_threads(mb, days=args.days, apply_changes=args.apply)
        except RuntimeError as e:
            say(str(e), err=True)
            return 1
    _persist_roles(acc)
    if args.json:
        out_json(res)
        return 0
    say(f"INBOX {res['inbox']} messages, {res['sent_in_window']} sent in the last {args.days} days.")
    for p in res["archive"]:
        say(f"  archive  {p['date'][:16]:16} {p['from'][:30]:30} {p['subject'][:60]}")
    for s in res["waiting_for_reply"]:
        say(f"  waiting  {s['date'][:16]:16} to {s['to'][:27]:27} {s['subject'][:60]}   (copy of my sent mail into INBOX)")
    if not res["archive"] and not res["waiting_for_reply"]:
        say("Nothing to tidy.")
    elif not args.apply:
        say(f"Dry run. Nothing changed. Add --apply to move {len(res['archive'])} to {res['archive_folder']}.")
    else:
        say(f"Done: {len(res['archive'])} archived, {len(res['waiting_for_reply'])} sent mails copied into INBOX.")
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mailskill", description="Read a mailbox over IMAP. Drafts only, no sending.")
    p.add_argument("--version", action="version", version=f"mailskill {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, folder=True):
        sp.add_argument("--account", "-a", help="address (or unique prefix) when more than one account is set up")
        sp.add_argument("--json", action="store_true", help="machine-readable output")
        if folder:
            sp.add_argument("--folder", "-f", default="INBOX", help="folder name or role: inbox, sent, drafts, trash, archive")

    s = sub.add_parser("add", help="set up an address: without arguments a small window opens; with an address the terminal asks. "
                                   "Finds the server, tests the login, keeps the secret in the keychain. Microsoft 365 signs in via the browser.")
    s.add_argument("address", nargs="?", help="email address (omit it to get the window)")
    s.add_argument("--host"); s.add_argument("--port", type=int); s.add_argument("--starttls", action="store_true")
    s.add_argument("--username", help="login name if it is not the address")
    s.add_argument("--default", action="store_true", help="make this the default account")
    s.add_argument("--dry-run", action="store_true", help="discover and print, store nothing")
    s.add_argument("--no-gui", action="store_true", help="never open a window, ask in the terminal")
    s.add_argument("--client-id", help=f"Microsoft app (client) id, overrides {setup.oauth.MS_CLIENT_ID_ENV} and the built-in one")
    s.add_argument("--verbose", "-v", action="store_true"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("discover", help="only look up the server for an address, store nothing")
    s.add_argument("address"); s.add_argument("--verbose", "-v", action="store_true"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("accounts", help="list configured accounts"); s.add_argument("--json", action="store_true"); s.set_defaults(fn=cmd_accounts)
    s = sub.add_parser("remove", help="remove an account and its stored secret"); s.add_argument("address"); s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("folders", help="list folders with their roles"); common(s, folder=False); s.set_defaults(fn=cmd_folders)

    s = sub.add_parser("search", help="find messages (newest first)"); common(s)
    s.add_argument("--from", dest="from_"); s.add_argument("--to"); s.add_argument("--subject"); s.add_argument("--text", help="full text (server-side)")
    s.add_argument("--since", help="IMAP date, e.g. 1-Sep-2026"); s.add_argument("--before"); s.add_argument("--days", type=int, help="last N days")
    s.add_argument("--unseen", action="store_true"); s.add_argument("--flagged", action="store_true"); s.add_argument("--unanswered", action="store_true")
    s.add_argument("--limit", type=int, default=50)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("read", help="show one message by UID"); common(s); s.add_argument("uid")
    s.add_argument("--save", metavar="DIR", help="save attachments to DIR"); s.add_argument("--replies", action="store_true", help="also look for my own replies")
    s.add_argument("--own-part", action="store_true", help="cut quoted text below my reply"); s.add_argument("--mark-seen", action="store_true")
    s.add_argument("--max-chars", type=int, default=20000)
    s.set_defaults(fn=cmd_read)

    s = sub.add_parser("attachments", help="save attachments of a message"); common(s); s.add_argument("uid")
    s.add_argument("--save", metavar="DIR", required=True); s.add_argument("--only", help="indexes, e.g. 1,3")
    s.set_defaults(fn=cmd_attachments)

    s = sub.add_parser("replied", help="did I already answer this message?"); common(s); s.add_argument("uid"); s.set_defaults(fn=cmd_replied)

    s = sub.add_parser("draft", help="put a draft into the Drafts folder (never sends)"); common(s)
    s.add_argument("--to"); s.add_argument("--cc"); s.add_argument("--bcc"); s.add_argument("--subject")
    s.add_argument("--body", required=True, help="text file, or - for stdin"); s.add_argument("--html", help="optional HTML alternative file")
    s.add_argument("--attach", action="append", metavar="FILE"); s.add_argument("--reply-to-uid", help="reply to this message (sets To, Subject, threading headers)")
    s.add_argument("--in-reply-to", help="Message-ID to thread under"); s.add_argument("--drafts-folder"); s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_draft)

    s = sub.add_parser("cleanup", help="apply a rules file to a folder; dry run unless --apply"); common(s)
    s.add_argument("--rules", required=True, metavar="FILE"); s.add_argument("--days", type=int, help="only look at the last N days")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_cleanup)
    s.set_defaults(folder="")

    s = sub.add_parser("threads", help="keep only the newest message per conversation in INBOX; dry run unless --apply"); common(s, folder=False)
    s.add_argument("--days", type=int, default=7, help="look at my sent mail of the last N days"); s.add_argument("--archive", help="archive folder if it is not detected")
    s.add_argument("--apply", action="store_true")
    s.set_defaults(fn=cmd_threads)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except SystemExit as e:
        return int(e.code or 0)
    except KeyboardInterrupt:
        return 130
    except (MailboxError, OAuthError) as e:
        say(str(e), err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
