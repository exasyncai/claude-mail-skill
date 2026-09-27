"""Repository text gate: no long dashes (U+2013, U+2014) and no internal paths in any text file.

The forbidden strings are assembled from pieces so this file does not trip its own check.
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXT = {".md", ".py", ".sh", ".ps1", ".json", ".yml", ".txt"}
SKIP = {".git", "out", ".pytest_cache", "__pycache__"}
INTERNAL = ["C:" + "\\\\" + "dev" + "\\\\", "One" + "Drive", "exasync-" + "one" + "drive", "Nexus" + "Mind", "bod" + "ob"]
CHECKS = [(re.compile("[" + chr(0x2013) + chr(0x2014) + "]"), "long dash"), (re.compile("|".join(INTERNAL), re.I), "internal path")]


def main() -> int:
    hits = 0
    for p in ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in EXT or SKIP & set(p.parts):
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
