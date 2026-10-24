"""tool_locator block (F17 step 2): fixed names only, version once per program file."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from core.blocks import tool_locator
from core.blocks.tool_locator import ToolLocator

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="shell script tools")


def fake_tool(folder: Path, name: str, text: str) -> Path:
    path = folder / name
    counter = folder / f"{name}.runs"
    path.write_text(f"#!/bin/sh\necho x >> {counter}\necho '{text}'\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def runs(folder: Path, name: str) -> int:
    f = folder / f"{name}.runs"
    return len(f.read_text().splitlines()) if f.exists() else 0


def test_found_version_cached_and_unknown_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    fake_tool(
        tmp_path, "tsp", "tsp: TSDuck - The MPEG Transport Stream Toolkit - version 3.45-4798"
    )
    fake_tool(tmp_path, "ffmpeg", "ffmpeg version 6.1.1-3ubuntu5 Copyright")
    fake_tool(tmp_path, "rm", "never run")
    loc = ToolLocator()
    got = loc.find_all(["tsp", "ffmpeg", "tsp", "ffprobe", "rm"])
    assert got["tsp"] == {"path": str(tmp_path / "tsp"), "version": "3.45-4798"}
    assert got["ffmpeg"] is not None and got["ffmpeg"]["version"] == "6.1.1-3ubuntu5"
    assert got["ffprobe"] is None and got["rm"] is None  # missing / not a known tool
    loc.find("tsp")
    assert runs(tmp_path, "tsp") == 1 and runs(tmp_path, "rm") == 0
    fake_tool(tmp_path, "tsp", "tsp: version 3.46-5000 and more text")  # updated program
    os.utime(tmp_path / "tsp", ns=(1, 1))
    assert loc.find("tsp") == {"path": str(tmp_path / "tsp"), "version": "3.46-5000"}


def test_cvlc_first_on_linux_and_unreadable_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    fake_tool(tmp_path, "vlc", "VLC version 3.0.20 Vetinari")
    assert ToolLocator().find("vlc") == {"path": str(tmp_path / "vlc"), "version": "3.0.20"}
    fake_tool(tmp_path, "cvlc", "VLC media player 3.0.21 Vetinari")
    assert ToolLocator().find("vlc") == {"path": str(tmp_path / "cvlc"), "version": "3.0.21"}
    fake_tool(tmp_path, "mpv", "garbage")
    assert ToolLocator().find("mpv") == {"path": str(tmp_path / "mpv"), "version": None}


def test_a_hanging_tool_is_cut_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(tool_locator, "VERSION_TIMEOUT_S", 0.5)
    path = tmp_path / "adb"
    path.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
    path.chmod(0o755)
    assert ToolLocator().find("adb") == {"path": str(path), "version": None}


def test_iperf3_is_a_known_tool_with_its_version() -> None:
    """2026-10-17: every PC said "iperf3 is not installed" -- it was not in the list."""
    import re

    from core.blocks.tool_locator import KNOWN

    spec = KNOWN["iperf3"]
    assert spec.names == ("iperf3",) and spec.windows_paths[0].endswith("iperf3.exe")
    m = re.search(spec.version_re, "iperf 3.16 (cJSON 1.7.15)\nLinux t460 6.8.0")
    assert m is not None and m.group(1) == "3.16"
