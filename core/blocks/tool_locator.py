"""Find external tools and their versions (F17 step 2, docs/15-tools.md, 02 4 ``tools``).

One job: for a tool name, the program path (or None) and its version string. Only the
names below are known (a fixed list: nothing typed by a user is ever run). The version
command runs once per program file (path + size + change time) with a time limit.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.blocks.process_runner import NO_WINDOW

VERSION_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class ToolSpec:
    names: tuple[str, ...]  # program names in search order
    version_args: tuple[str, ...]
    version_re: str
    windows_paths: tuple[str, ...] = ()  # usual install places when not on PATH
    linux_paths: tuple[str, ...] = ()  # the same on Linux (tools ZeroLoss brings itself)


KNOWN: dict[str, ToolSpec] = {
    "ffmpeg": ToolSpec(("ffmpeg",), ("-version",), r"ffmpeg version (\S+)"),
    "ffprobe": ToolSpec(("ffprobe",), ("-version",), r"ffprobe version (\S+)"),
    "tsp": ToolSpec(
        ("tsp",),
        ("--version",),
        r"version (\d+\.\d+-\d+)",
        (r"C:\Program Files\TSDuck\bin\tsp.exe",),
    ),
    # cvlc on Linux: the same player without the Qt window
    "vlc": ToolSpec(
        ("vlc",) if sys.platform == "win32" else ("cvlc", "vlc"),
        ("--version",),
        r"VLC (?:media player |version )(\d+\.\d+\.\d+)",
        (r"C:\Program Files\VideoLAN\VLC\vlc.exe",),
    ),
    "mpv": ToolSpec(("mpv",), ("--version",), r"mpv v?(\S+)"),
    # 2026-10-17: the throughput test checked for iperf3, which was not listed here, so
    # every PC said "iperf3 is not installed". Windows: the copy next to the program.
    "iperf3": ToolSpec(
        ("iperf3",),
        ("--version",),
        r"^\w+ (\d+\.\d+)",  # its first line: the name, then the version
        (str(Path(sys.executable).with_name("iperf3.exe")),),
    ),
    # Google's platform-tools (tools-manifest "latest"), unpacked by ZeroLoss; Ubuntu's own
    # adb on PATH is used when there is one
    "adb": ToolSpec(
        ("adb",),
        ("version",),
        r"Version (\S+)",
        linux_paths=("/opt/zeroloss/tools/platform-tools/adb",),
    ),
}


class ToolLocator:
    def __init__(self) -> None:
        self._versions: dict[tuple[str, int, int], str | None] = {}
        self._lock = threading.Lock()

    def path(self, name: str) -> str | None:
        spec = KNOWN.get(name)
        if spec is None:
            return None
        for program in spec.names:
            found = shutil.which(program)
            if found:
                return found
        places = spec.windows_paths if sys.platform == "win32" else spec.linux_paths
        return next((p for p in places if os.path.isfile(p)), None)

    def find(self, name: str) -> dict[str, Any] | None:
        """``{"path", "version"}`` or None when the tool is not there (or not known)."""
        found = self.path(name)
        if found is None:
            return None
        return {"path": found, "version": self._version(name, found)}

    def find_all(self, names: list[str]) -> dict[str, dict[str, Any] | None]:
        return {n: self.find(n) for n in sorted(set(names))}

    def _version(self, name: str, path: str) -> str | None:
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (path, st.st_size, st.st_mtime_ns)
        with self._lock:  # one version run per file even when asked at once
            if key not in self._versions:
                self._versions[key] = _run_version(KNOWN[name], path)
            return self._versions[key]


def _run_version(spec: ToolSpec, path: str) -> str | None:
    try:
        done = subprocess.run(
            [path, *spec.version_args], capture_output=True, text=True,
            timeout=VERSION_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
            errors="replace", creationflags=NO_WINDOW,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(spec.version_re, done.stdout + done.stderr)
    return m.group(1) if m else None
