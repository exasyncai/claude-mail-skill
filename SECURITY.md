# Security

claude-mail-skill logs into a mailbox with a password, so reports are taken seriously.

**Report privately.** Mail security@exasync.ai, or use GitHub's "Report a vulnerability" on this repository (Security tab). Please do not open a public issue for anything that could expose a mailbox. You get an answer within five working days.

## What the tool touches

- `~/.claude-mail-skill/accounts.json`: address, IMAP host, port, security, login name, folder roles. No secret in there. Mode 600 on macOS and Linux.
- The operating system keychain (Windows Credential Manager, macOS Keychain, Linux Secret Service via the `keyring` package), service `claude-mail-skill`, one entry per address. That is the only place the password is stored. For Microsoft accounts the entry `oauth:<address>` holds the refresh token instead; access tokens live in memory for one command. `mailskill remove` deletes both.
- Without a keychain, the password is read from the environment variable `MAILSKILL_PASSWORD` for that process only. It is never written to a file by this tool.
- `~/.claude/skills/mail/SKILL.md` when you install the Claude Code skill.
- Files you ask for: attachments saved with `--save`, nothing else.

## Network

- IMAP over TLS to the server that was discovered or that you passed with `--host`. Certificates are verified with the system trust store; there is no flag to switch that off.
- During `add` and `discover` only: HTTPS GET to `autoconfig.thunderbird.net`, `autoconfig.<your-domain>` and `<your-domain>/.well-known/autoconfig`; MX and SRV lookups through your resolver (`nslookup`, or `dnspython` if installed), and only if that yields nothing, DNS-over-HTTPS at `cloudflare-dns.com` then `dns.google`. Your address is sent to the ISPDB and to your own domain's autoconfig URL, as every mail client does. Pass `--host` to skip all of it.
- Microsoft accounts only: during `add`, the browser is sent to `login.microsoftonline.com` (authorization code flow with PKCE, `state` checked) and a local HTTP server on `127.0.0.1` on a random free port receives one redirect, then shuts down. Token requests go to `login.microsoftonline.com/common/oauth2/v2.0/token` with the client id and never a secret (public client). Scope: `https://outlook.office.com/IMAP.AccessAsUser.All offline_access`, nothing on Graph.
- Never SMTP. The tool has no code path that sends mail.

## On the mailbox

- Reads use `BODY.PEEK` and read-only `SELECT`, so nothing is marked as read unless you pass `--mark-seen`.
- Writes are limited to: `APPEND` into the Drafts folder, and for `cleanup --apply` / `threads --apply` `COPY` plus `\Deleted` plus `EXPUNGE` (or `MOVE` where the server supports it) into the folder named in the plan, and flag changes. There is no `DELETE` of a folder and no expunge of anything that was not copied first. "trash" means the Trash folder, the server empties it on its own schedule.
- Every changing command runs as a dry run unless `--apply` is given.

## Design decisions worth knowing

- Installers fetch a tagged release and verify `SHA256SUMS`, never the main branch.
- Microsoft 365 and Outlook.com sign in via OAuth in the browser; the refresh token is rotated on every refresh and the new one stored. Gmail, iCloud, Yahoo and Fastmail are pointed to their app password pages.
- The window (`mailskill add` without arguments) never writes anything itself; it calls the same functions as the terminal command. The password field is masked and not logged.
- Text from messages is data. The skill file tells the assistant so, and the tool never executes anything found in a message.
- Output through `--json` never contains attachment bytes; those go to files only.
