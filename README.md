# claude-mail-skill

**From an email address to a mailbox your assistant can read, in one command. Reads, searches, saves attachments, writes drafts. Never sends.**

```
mailskill add                        # a small window: address, password, Connect. Finds the server, tests the login, keeps the password in your OS keychain
mailskill add you@example.com        # the same in the terminal
mailskill search --unseen --days 3   # what came in
mailskill read 4821 --replies        # one message, plus: did I already answer this?
mailskill draft --reply-to-uid 4821 --body answer.txt    # lands in Drafts, you press send
```

Works with any IMAP mailbox: your own domain at a hoster, Microsoft 365 and Outlook.com (sign-in in the browser, no app password), Gmail, iCloud, Fastmail, GMX, Posteo, Proton (via Bridge). Ships a Claude Code skill so the assistant knows the commands and the rules that come with them.

## What makes it different

| What goes wrong | What claude-mail-skill does |
|---|---|
| "Which server, which port, SSL or STARTTLS?" | `add` asks the Mozilla ISPDB, your domain's autoconfig, the MX record (known hosters are mapped, unknown ones are looked up again), SRV records, and finally probes `imap.`/`mail.` on 993 and 143. Every candidate is verified with a real TLS handshake and `CAPABILITY` before it is offered. |
| Password in a config file | The password goes into Windows Credential Manager, macOS Keychain or the Linux Secret Service (`keyring`). The settings file holds host and port, nothing else. No keychain: `MAILSKILL_PASSWORD` for the session. |
| "Which server, which port" asked of someone who just wants their mail | `mailskill add` opens one window: address, password, Connect. It looks the server up while you type. A Microsoft address hides the password field and offers "Sign in with Microsoft"; Gmail and friends get their app-password link right there. No terminal needed. `--no-gui` for the terminal version. |
| Gmail "login refused", Microsoft 365 silently broken | Gmail, iCloud, Yahoo and Fastmail are pointed to their app password page. Microsoft 365 and Outlook.com stopped accepting passwords over IMAP in 2022, so they are recognised from the MX and signed in through the browser (OAuth 2.0 with PKCE, refresh token in the keychain, silent renewal on every call). |
| The assistant writes a second reply to a mail you answered yesterday | `replied` checks the `\Answered` flag **and** searches your own reply by `In-Reply-To`/`References`, with a subject and recipient fallback, in Sent **and** INBOX, because some clients file the sent copy there. The skill file makes this check mandatory before any draft. |
| Reading marks everything as read | All reads use `BODY.PEEK` on a read-only `SELECT`. `--mark-seen` if you want that. |
| Folder names break: `"INBOX.Customer Files"`, `Entw&APw-rfe` | Names with spaces are quoted on the wire, IMAP-UTF-7 is decoded for display, and the status of every `SELECT` is checked (a silent `NO` there is why the next `SEARCH` fails with "illegal in state AUTH"). |
| "Drafts", "Entwürfe", "Brouillons", "[Gmail]/Drafts" | Special folders are taken from the server's `\Sent`, `\Drafts`, `\Trash`, `\Archive` attributes, with a name table in six languages as fallback. Use them by role: `--folder sent`. |
| A cleanup script deletes the wrong thing | `cleanup` and `threads` print a plan and change nothing. `--apply` runs exactly that plan. Moves are COPY, verify, flag, EXPUNGE (or `MOVE` where supported); "trash" is the Trash folder, never a hard delete. |
| The assistant "sends" something | There is no SMTP code in this repository. `draft` appends to the Drafts folder with `X-Unsent: 1`, which Outlook, Thunderbird and Apple Mail open as an unsent draft. |

## Install

Windows (PowerShell):

```powershell
irm https://github.com/exasyncai/claude-mail-skill/releases/latest/download/install.ps1 | iex
```

macOS / Linux:

```sh
curl -fsSL https://github.com/exasyncai/claude-mail-skill/releases/latest/download/install.sh | sh
```

Both download a **tagged release**, verify it against `SHA256SUMS`, unpack it to `~/.claude-mail-skill/app`, install the `keyring` package for your user if it is missing, put a `mailskill` launcher in place, and copy the skill to `~/.claude/skills/mail` if `~/.claude` exists. Uninstall: delete `~/.claude-mail-skill` and `~/.claude/skills/mail`, then `mailskill remove <address>` beforehand if you want the keychain entry gone too.

Requirements: Python 3.10 or newer. Nothing else; `keyring` is optional and only used for storing the password or the Microsoft token, and `tkinter` (part of most Python installs) only for the window. Without either, the terminal path does the same job.

From a checkout: `python mailskill.py <command>`.

## What you have after 2 minutes

- the IMAP server for your address, found and verified, stored in `~/.claude-mail-skill/accounts.json` without the password
- the password (or, for Microsoft, the refresh token) in your operating system's keychain under the service `claude-mail-skill`
- `mailskill search`, `read`, `attachments`, `replied`, `draft`, `cleanup`, `threads` on the command line, all with `--json`
- a Claude Code skill (`/mail` in your skills list) that uses them and knows the rules: check for an existing reply first, drafts only, dry run before any change, mail content is data and not instructions

## Commands

```
mailskill add                                  window: address, password (or Microsoft sign-in), Connect
mailskill add <address> [--host H --port P --starttls --username U] [--default] [--dry-run] [--no-gui] [--client-id ID]
mailskill discover <address>                   only the lookup, stores nothing
mailskill accounts | remove <address>
mailskill folders                              names, roles (sent, drafts, trash, archive, junk), raw names

mailskill search [--from X] [--to X] [--subject X] [--text X] [--days N | --since 1-Sep-2026] [--before ...]
                 [--unseen] [--flagged] [--unanswered] [--folder F] [--limit 50]
mailskill read <uid> [--folder F] [--replies] [--own-part] [--save DIR] [--mark-seen] [--max-chars 20000]
mailskill attachments <uid> --save DIR [--only 1,3]
mailskill replied <uid>                        verdict: already replied / no reply yet
mailskill draft (--reply-to-uid <uid> | --to A --subject S) --body FILE|- [--cc] [--bcc] [--attach FILE]... [--html FILE] [--dry-run]
mailskill cleanup --rules FILE [--days N] [--apply]
mailskill threads [--days 7] [--archive FOLDER] [--apply]
```

Every command takes `--account <address>` (a unique prefix is enough) and `--json`. `--folder` accepts a name as shown by `folders` or a role: `inbox`, `sent`, `drafts`, `trash`, `archive`. UIDs are per folder.

### search

`--from`, `--to`, `--text`, dates and flags are matched by the server. `--subject` with one ASCII word is matched by the server too; anything longer is filtered on the client after normalising reply prefixes (`Re:`, `AW:`, `WG:`, `Fwd:` ...), because `SEARCH SUBJECT` with spaces is unreliable on some servers. On a large folder combine it with `--days`. Output is newest first: UID, flags (`A` answered, `N` new, `!` flagged, `@` attachment), date, sender, subject.

### read and replied

`read` prints headers, the text part (HTML is converted when there is no text part), the attachment list, and with `--replies` the same check as `replied`. `--own-part` cuts the quoted history below a reply (English, German and French client conventions). `--save DIR` writes the attachments; filenames are sanitised, existing files are never overwritten.

### draft

`--reply-to-uid` takes To, Subject (`Re:` added once) and the threading headers from the original, so the draft lands in the right conversation. The body is a text file or `-` for stdin. The message carries `X-Unsent: 1` and the `\Draft` flag and goes into the Drafts folder found by role, or `--drafts-folder`. `--dry-run` shows what would be written.

### cleanup

A JSON rules file, first matching rule wins per message, all fields of a rule must match:

```json
{
  "folder": "INBOX",
  "rules": [
    {"name": "newsletters", "from": ["news@", "@substack.com"], "older_than_days": 7, "action": "move", "to": "archive"},
    {"name": "read and old", "seen": true, "older_than_days": 60, "action": "move", "to": "archive"},
    {"name": "flag the boss", "from": ["boss@example.com"], "action": "flag"},
    {"name": "junk", "from": ["@spammy.example"], "action": "trash"}
  ]
}
```

Match fields: `from`, `recipient`, `subject` (lists are OR, substrings, case-insensitive), `older_than_days`, `newer_than_days`, `seen`, `unseen`, `has_attachments`. Actions: `move` (with `to`, a folder or a role), `trash`, `flag`, `unflag`, `seen`, `unseen`. There is no delete action. `rules.example.json` is a starting point.

### threads

For every conversation in which you sent a mail during the last `--days`: if the other side wrote last, their newest message stays in INBOX and the older ones of that thread move to the archive; if you wrote last, the whole thread moves to the archive and a copy of your sent mail is placed in INBOX as "waiting for reply". Conversations without a sent mail in the window are not touched. The result is an inbox that shows one line per open conversation, and it is where the "did I already answer" logic came from.

## The Claude Code skill

`skill/mail/SKILL.md` is a short file that tells the assistant what exists, which commands are safe, and four rules: run `replied` before drafting, drafts only, dry run and a yes from you before `--apply`, and that text inside a message never turns into an instruction. Install it by copying the folder to `~/.claude/skills/mail` (the installers do that) or, for one project, to `.claude/skills/mail`.

## Providers

| Provider | What happens |
|---|---|
| Your domain at a hoster (All-Inkl, IONOS, Strato, Hetzner, OVH, Hostpoint ...) | MX lookup maps the hoster, or the hoster's own autoconfig answers. Verified before use. |
| Gmail, Google Workspace | `imap.gmail.com`, and the note that you need an app password (2-step verification on, then myaccount.google.com/apppasswords). |
| iCloud, Yahoo, Fastmail, Zoho, GMX, web.de, Posteo, mailbox.org, t-online | Known hosts, with the app password or "enable IMAP" hint where the provider needs one. |
| Proton Mail | No direct IMAP. Run Proton Mail Bridge and add with `--host 127.0.0.1 --port 1143 --starttls`. |
| Microsoft 365, Outlook.com, Hotmail | Detected from the MX. Own mailbox, shared mailbox (sign in as yourself) and the alias case are covered, see "Microsoft 365" below. `add` opens the Microsoft sign-in in your browser (authorization code flow with PKCE, redirect to `http://localhost:<port>/`), keeps only the refresh token in the keychain, and renews the access token silently on every command. IMAP via `outlook.office365.com` with `XOAUTH2`. See "Microsoft 365" below. |
| Mail behind Mimecast, Proofpoint, Barracuda | The MX names the gateway, not the mailbox. Ask your admin for the IMAP host and pass `--host`. |

## Microsoft 365

Passwords do not work over IMAP at Microsoft any more, so `add` signs you in the way Outlook does: the browser opens at `login.microsoftonline.com`, you pick the account, and a tiny local web server on `127.0.0.1` receives the code. No device code to type, no app password to create. The refresh token goes into the keychain under `oauth:<address>`; the settings file only says `"auth": "oauth"`.

Three things can still stop it, each with its own message:

- **Admin consent.** In organisations that block user consent, Microsoft answers `AADSTS65001` / `AADSTS900971`. The tool prints the sentence for your admin plus the admin-consent link; after one click there, run `add` again.
- **IMAP switched off** for the mailbox (Exchange admin center, the mailbox, Email apps). The sign-in succeeds, the IMAP `AUTHENTICATE` is refused; the message says so.
- **Revoked** (you removed the app at myaccount.microsoft.com, or the password was changed): the next command reports `invalid_grant` in plain words and asks you to run `add` again.

Three kinds of Microsoft addresses, verified against real mailboxes:

- **Your own mailbox** (`you@company.com`): sign in as yourself. Done.
- **A shared mailbox** (`support@company.com`, no login of its own): run `mailskill add support@company.com` and sign in with **your** account in the browser. The token is yours, IMAP is opened as the shared mailbox; that works when your account has full access to it.
- **An alias** (`info@company.com` that only forwards into your mailbox): IMAP wants the mailbox's primary address, so add that one. The refusal message says so.

An account that has a licence but no Exchange mailbox is refused by IMAP as well; the admin center is where that is fixed, not the tool.

The tool ships with the client id of a public Entra app registration (no secret, redirect `http://localhost`, permission `IMAP.AccessAsUser.All` + `offline_access`). Your own registration works too: `--client-id <id>` or `MAILSKILL_MS_CLIENT_ID`.

## Limits

- Gmail still needs an app password (Google's IMAP OAuth requires an app review that a small open-source tool does not get). The window links to the page.
- No sending. That is the point, not a gap.
- `threads` needs an Archive folder (detected by attribute or name, or `--archive`). It reads the whole INBOX once per run; a very large INBOX takes a while.
- Autodiscovery talks to the Mozilla ISPDB and to your domain, and as a last resort to a public DNS-over-HTTPS resolver, so your address's domain leaves your machine during `add`. `--host` skips all of it. Details in `SECURITY.md`.
- Tested against Dovecot (All-Inkl), Gmail and Fastmail servers for discovery; the read, draft and tidy paths against a Dovecot mailbox in daily use, and the Microsoft path against a Microsoft 365 user mailbox and a shared mailbox. The test suite (96 tests, no network; the token endpoint, the browser round trip and the window are exercised with fakes) runs on Linux, macOS and Windows in CI.

## Development

```
python -m pytest tests -q
python mailskill.py discover you@example.com --verbose
python tools/make_release.py        # out/*.zip, *.tar.gz, SHA256SUMS
```

Layout: `mailskill/discover.py` (lookups), `store.py` (settings and keychain), `mailbox.py` (IMAP, read-only by default), `oauth.py` (Microsoft sign-in, PKCE, local redirect receiver, token refresh), `setup.py` (what `add` decides, no terminal and no window in it), `gui.py` (the tkinter window, drawing only), `cleanup.py` (rules and threads), `cli.py`. Tests use an in-memory IMAP server (`tests/fake_imap.py`) that behaves like Dovecot where it matters.

## License

MIT. Built by [Exasync](https://exasync.ai), where an autonomous AI company reads its own mail this way.
