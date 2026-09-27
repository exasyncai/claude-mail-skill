"""Repository text gate: no long dashes (U+2013, U+2014) and no internal paths in any text file.

The built-in list covers generic local paths only. Terms that identify a machine, a user or a
company-internal name belong in tools/check_text.local (one term per line, git-ignored) or in the
environment variable CHECK_TEXT_INTERNAL (comma separated), so the repository never lists them.
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = {".md", ".py", ".sh", ".ps1", ".json", ".yml", ".txt"}
SKIP = {".git", "out", ".pytest_cache", "__pycache__"}
SKIP_FILES = {"check_text.local"}

BS = chr(92)
GENERIC = ["C:" + BS + "dev" + BS, "C:" + BS + "Users" + BS, "/Us" + "ers/", "/ho" + "me/", "One" + "Drive"]


def internal_terms() -> list[str]:
    terms = [re.escape(t) for t in GENERIC]
    local = ROOT / "tools" / "check_text.local"
    if local.is_file():
        terms += [re.escape(t.strip()) for t in local.read_text(encoding="utf-8").splitlines() if t.strip() and not t.startswith("#")]
    terms += [re.escape(t.strip()) for t in os.environ.get("CHECK_TEXT_INTERNAL", "").split(",") if t.strip()]
    return terms


CHECKS = [(re.compile("[" + chr(0x2013) + chr(0x2014) + "]"), "long dash"), (re.compile("|".join(internal_terms()), re.I), "internal path")]


def main() -> int:
    hits = 0
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in EXT or SKIP & set(p.parts) or p.name in SKIP_FILES:
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for rx, what in CHECKS:
                if rx.search(line):
                    print(f"{p.relative_to(ROOT)}:{i}: {what}")
                    hits += 1
    print("text gate ok" if not hits else f"{hits} hit(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
