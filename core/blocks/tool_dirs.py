"""Where the bundled tools of a Windows node are (docs/06-install.md 4.2, 15).

2026-11-21 (user: "윈도우즈 sender,receiver도 원클릭 인스톨되게", the tools bundled in the
kit): the Windows node setup puts TSDuck, ffmpeg and mpv in their own folders under
``Program Files\\ZeroLoss\\Tools`` (apart from ZeroLoss: ffmpeg and mpv are GPL, CLAUDE.md).
A service started by Windows does not see a changed system PATH until the PC restarts,
so the node and Agent Desktop put those folders in front of their own PATH when they
start. Elsewhere (Linux: the tools come from the distribution) nothing changes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

TOOLS = ("TSDuck", "ffmpeg", "mpv")  # each a folder with its programs


def tools_root() -> Path:
    return Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "ZeroLoss" / "Tools"


def add_windows_tool_dirs(root: Path | None = None) -> list[str]:
    """Put the bundled tool folders that exist in front of PATH (Windows); returns them."""
    if sys.platform != "win32":
        return []
    base = root or tools_root()
    found = [str(base / t) for t in TOOLS if (base / t).is_dir()]
    if found:
        have = os.environ.get("PATH", "")
        os.environ["PATH"] = os.pathsep.join([*found, have]) if have else os.pathsep.join(found)
    return found
