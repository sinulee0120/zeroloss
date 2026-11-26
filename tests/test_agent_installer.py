"""The Windows sender/receiver setup (docs/06-install.md 4.2, 2026-11-21 user: "윈도우즈
sender,receiver도 원클릭 인스톨되게"): what the script installs matches the spec, the
service programs take the service name, and the bundled tools are found."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from core.blocks import tool_dirs
from tools import build

ROOT = Path(__file__).resolve().parent.parent
ISS = (ROOT / "installer" / "windows" / "zeroloss-agent.iss").read_text(encoding="utf-8")


def test_the_script_installs_what_06_4_2_names() -> None:
    for service in ("ZeroLossAgent", "ZeroLossDeployer"):
        assert f"AddService('{service}'" in ISS and f"--windows-service {service}" in ISS
    assert "start= auto" in ISS and "failure " in ISS  # at boot, restarted when it fails
    assert "--player-host desktop" in ISS  # the player runs in the login session (D38)
    assert "--port 10991 --agent-port 10992" in ISS
    assert r"CurrentVersion\Run" in ISS and "zeroloss-agent-desktop.exe" in ISS
    assert r"{commonappdata}\ZeroLoss\Agent" in ISS
    assert "remoteip=localsubnet" in ISS  # 06 4.2: before pairing, the same subnet
    for tool in build.AGENT_TOOLS:  # the kit's tools, apart from ZeroLoss
        assert rf"{{autopf}}\ZeroLoss\Tools\{tool}" in ISS
    assert set(build.AGENT_TOOLS) == set(tool_dirs.TOOLS)


def test_a_running_test_and_the_controller_pc_are_refused() -> None:
    assert "CustomMessage('Running')" in ISS  # 06 1 7: a test is never cut
    assert "CustomMessage('IsController')" in ISS  # the same port 10992


def test_the_build_command_needs_the_folder_and_the_tools(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert build.main(["build", "installer-agent", "--app", "x"]) == 6
    assert build.main(["build", "installer-agent", "--tools", "y"]) == 6
    assert (
        build.main(["build", "installer-agent", "--app", "x", "--tools", "y", "--port", "1"]) == 6
    )


def test_the_bundled_tool_folders_come_first_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for t in ("TSDuck", "mpv"):  # ffmpeg missing: not added
        (tmp_path / t).mkdir()
    monkeypatch.setenv("PATH", "before")
    got = tool_dirs.add_windows_tool_dirs(tmp_path)
    if sys.platform == "win32":
        assert got == [str(tmp_path / "TSDuck"), str(tmp_path / "mpv")]
        assert os.environ["PATH"].split(os.pathsep) == [*got, "before"]
    else:  # Linux: the distribution's tools, nothing changes
        assert got == [] and os.environ["PATH"] == "before"
