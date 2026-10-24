"""Automatic login of the PC's desktop user (docs/00-vision.md 1차 표, 01 D43, 06 11.3,
20 GAP-67).

Asked for at the SSH install (``--auto-login`` is the person's confirmation, 00-vision
"사용자 확인 후"), so that after a reboot the user session and Agent Desktop (the PC
player, 03 F20) come back without anyone. GDM only (Ubuntu's): ``[daemon]
AutomaticLoginEnable`` / ``AutomaticLogin`` in ``/etc/gdm3/custom.conf``. The file as it
was is kept, and removal puts it back (06 1 rule 2: leave the PC as before); if the
person changed the file since, only our lines go. Standard library only (L0).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

MARK = "# ZeroLoss: automatic login for unattended tests (removed with ZeroLoss)"
KEYS = ("AutomaticLoginEnable", "AutomaticLogin")
OFF = "# (ZeroLoss) "  # the person's own line of our keys, put back on removal
_USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


class NotSupported(Exception):
    """No GDM here: the person turns automatic login on (Action Required)."""


def enable(conf: Path, orig: Path, user: str) -> dict[str, Any]:
    if not _USER.match(user) or user == "root":
        raise ValueError(f"not a desktop user name: {user!r}")
    if not conf.is_file():
        raise NotSupported(f"{conf} not found (only GDM is supported)")
    before = conf.read_text(encoding="utf-8")
    kept = json.loads(orig.read_text(encoding="utf-8")) if orig.is_file() else None
    original = kept["text"] if kept else before  # a second install keeps the first original
    after = _with_ours(_without_ours(before), user)
    conf.write_text(after, encoding="utf-8")
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_text(json.dumps({"text": original, "ours": after, "user": user}), encoding="utf-8")
    return {"user": user, "file": str(conf), "changed": after != before}


def disable(conf: Path, orig: Path) -> str | None:
    """Removal: the file as it was. Returns what could not be done, or None."""
    try:
        kept = json.loads(orig.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # never turned on by us
    try:
        now = conf.read_text(encoding="utf-8") if conf.is_file() else None
        if now is not None:
            conf.write_text(kept["text"] if now == kept["ours"] else _without_ours(now),
                            encoding="utf-8")  # fmt: skip
    except OSError as e:
        return f"{conf}: {e}"
    orig.unlink(missing_ok=True)
    return None


def _without_ours(text: str) -> str:
    """Our marked block (the mark and the two lines after it) taken out."""
    out, skip = [], 0
    for line in text.splitlines(keepends=True):
        if skip:
            skip -= 1
            continue
        if line.rstrip("\n") == MARK:
            skip = len(KEYS)
            continue
        out.append(line[len(OFF) :] if line.startswith(OFF) else line)  # the person's own
    return "".join(out)


def _with_ours(text: str, user: str) -> str:
    """Active lines of our keys in [daemon] are commented out, ours go right under the
    section header (a [daemon] section is added when there is none)."""
    lines = text.splitlines(keepends=True)
    block = [MARK + "\n", "AutomaticLoginEnable=true\n", f"AutomaticLogin={user}\n"]
    out: list[str] = []
    section = None
    placed = False
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1]
            out.append(line if line.endswith("\n") else line + "\n")
            if section == "daemon" and not placed:
                out += block
                placed = True
            continue
        key = s.split("=", 1)[0].strip() if "=" in s and not s.startswith("#") else ""
        if section == "daemon" and key in KEYS:
            out.append(OFF + line)
            continue
        out.append(line)
    if not placed:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out += ["[daemon]\n", *block]
    return "".join(out)
