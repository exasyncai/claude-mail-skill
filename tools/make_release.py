"""Build the release archives and SHA256SUMS into out/.

python tools/make_release.py            -> out/claude-mail-skill-v<version>.zip, .tar.gz, SHA256SUMS
The version comes from mailskill/__init__.py and must match the tag and the installers.
"""
from __future__ import annotations

import hashlib
import io
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = ["mailskill", "skill", "mailskill.py", "install.ps1", "install.sh", "README.md", "LICENSE", "SECURITY.md", "rules.example.json"]


def version() -> str:
    m = re.search(r'__version__ = "([^"]+)"', (ROOT / "mailskill" / "__init__.py").read_text(encoding="utf-8"))
    return m.group(1)


def files():
    for name in INCLUDE:
        p = ROOT / name
        if p.is_file():
            yield p
        else:
            for f in sorted(p.rglob("*")):
                if f.is_file() and "__pycache__" not in f.parts and not f.name.endswith(".pyc"):
                    yield f


def main() -> int:
    v = "v" + version()
    for name in ("install.ps1", "install.sh"):
        txt = (ROOT / name).read_text(encoding="utf-8")
        if v not in txt:
            print(f"{name} does not pin {v}; fix the default version first", file=sys.stderr)
            return 1
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    prefix = f"claude-mail-skill-{v}"
    zpath, tpath = out / f"{prefix}.zip", out / f"{prefix}.tar.gz"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files():
            z.write(f, f"{prefix}/{f.relative_to(ROOT).as_posix()}")
    with tarfile.open(tpath, "w:gz") as t:
        for f in files():
            info = t.gettarinfo(f, f"{prefix}/{f.relative_to(ROOT).as_posix()}")
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            if f.suffix == ".sh":
                info.mode = 0o755
            with open(f, "rb") as fh:
                t.addfile(info, fh)
    lines = []
    for p in (zpath, tpath):
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}")
    (out / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="ascii")
    for p in (zpath, tpath, out / "SHA256SUMS"):
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
