#!/bin/sh
# claude-mail-skill installer for macOS and Linux. Fetches a TAGGED release, verifies it
# against SHA256SUMS, unpacks it to ~/.claude-mail-skill/app and installs the Claude Code
# skill to ~/.claude/skills/mail. Uninstall: rm -rf ~/.claude-mail-skill/app ~/.claude/skills/mail
set -eu
REPO="exasyncai/claude-mail-skill"
VERSION="${MAILSKILL_VERSION:-v0.2.0}"
HOME_DIR="${MAILSKILL_HOME:-$HOME/.claude-mail-skill}"
DEST="$HOME_DIR/app"
ARCHIVE="claude-mail-skill-$VERSION.tar.gz"
BASE="${MAILSKILL_BASE_URL:-https://github.com/$REPO/releases/download/$VERSION}"   # mirror or test server

PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    echo "Python 3.10 or newer is required (python3 not found or too old)." >&2
    exit 1
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
echo "Downloading claude-mail-skill $VERSION ..."
curl -fsSL "$BASE/$ARCHIVE" -o "$tmp/$ARCHIVE"
curl -fsSL "$BASE/SHA256SUMS" -o "$tmp/SHA256SUMS"

expected="$(awk -v f="$ARCHIVE" '$2 == f || $2 == "*" f { print $1 }' "$tmp/SHA256SUMS")"
if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$tmp/$ARCHIVE" | awk '{ print $1 }')"
else
    actual="$(shasum -a 256 "$tmp/$ARCHIVE" | awk '{ print $1 }')"
fi
if [ -z "$expected" ] || [ "$expected" != "$actual" ]; then
    echo "Checksum mismatch for $ARCHIVE - nothing installed." >&2
    exit 1
fi

rm -rf "$DEST"
mkdir -p "$DEST"
tar -xzf "$tmp/$ARCHIVE" -C "$DEST" --strip-components=1
echo "Installed to $DEST (checksum ok)."

# keychain support: optional, but without it the password lives only in MAILSKILL_PASSWORD
if ! "$PY" -c 'import keyring' 2>/dev/null; then
    echo "Installing the keyring package for your user (python -m pip install --user keyring) ..."
    "$PY" -m pip install --user --quiet keyring || echo "keyring could not be installed; export MAILSKILL_PASSWORD per session instead."
fi

# launcher
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/mailskill" <<EOF
#!/bin/sh
exec "$PY" "$DEST/mailskill.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/mailskill"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) echo "Command: mailskill" ;;
    *) echo "Add ~/.local/bin to your PATH, or call: $HOME/.local/bin/mailskill" ;;
esac

# Claude Code skill
if [ -d "$HOME/.claude" ] || [ -n "${CLAUDE_INSTALL_SKILL:-}" ]; then
    mkdir -p "$HOME/.claude/skills/mail"
    cp "$DEST/skill/mail/SKILL.md" "$HOME/.claude/skills/mail/SKILL.md"
    echo "Claude Code skill installed: ~/.claude/skills/mail"
else
    echo "No ~/.claude found; skill not installed. Later: cp $DEST/skill/mail/SKILL.md ~/.claude/skills/mail/"
fi
echo "Start with:  mailskill add you@example.com"
