"""Windows installer and kit sources (docs/06-install.md 1, 3, 7; 2026-10-31 user
"설치가이드. Offline kit. Installer . 사용법")."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tools import build

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "installer" / "windows" / "zeroloss-controller.iss"
KIT = ROOT / "tools" / "kit"


def _section(text: str, name: str) -> str:
    m = re.search(rf"^\[{name}\]\n(.*?)(?=^\[|\Z)", text, re.M | re.S)
    assert m, name
    return m.group(1)


def test_every_firewall_rule_added_is_removed_by_its_exact_name() -> None:
    """06 1 2: the uninstaller removes only what the installer made, by exact name."""
    text = ISS.read_text(encoding="utf-8")
    added = set(re.findall(r'add rule name=""([^"]+)""', _section(text, "Run")))
    removed = set(re.findall(r'delete rule name=""([^"]+)""', _section(text, "UninstallRun")))
    assert added and added == removed


def test_data_and_reports_stay_unless_the_person_says_so() -> None:
    """06 1 5: the question defaults to No, and nothing deletes them without it."""
    text = ISS.read_text(encoding="utf-8")
    assert "MB_DEFBUTTON2" in text and "IDNO) = IDYES" in text
    for kept in ("Reports", "commonappdata"):
        assert kept not in _section(text, "UninstallDelete")


def test_a_running_test_stops_install_and_uninstall() -> None:
    """06 1 7: both ask the Controller service first; only IDLE lets them go on."""
    text = ISS.read_text(encoding="utf-8")
    assert "function PrepareToInstall" in text and "function InitializeUninstall" in text
    assert text.count("EndService()") >= 3 and '\'"state":"IDLE"\'' in text


def test_the_guide_names_the_build_only_by_the_token() -> None:
    """The kit and the installer put their build in; no old build number stays."""
    text = (KIT / build.GUIDE).read_text(encoding="utf-8")
    assert "{BUILD}" in text and "ZeroLoss-Controller-Setup-{BUILD}.exe" in text
    assert not re.search(r"\b20\d{6}\.\d{4}\b", text)


def test_the_kit_scripts_are_unix_text() -> None:
    for name in ("zeroloss-linux-setup.sh", "zeroloss-sample-video.sh"):
        data = (KIT / name).read_bytes()
        assert data.startswith(b"#!/bin/bash")
        if shutil.which("bash"):
            text = data.replace(b"\r\n", b"\n")
            assert subprocess.run(["bash", "-n"], input=text, check=False).returncode == 0


def test_installer_command_needs_the_controller_folder(capsys: pytest.CaptureFixture[str]) -> None:
    assert build.main(["build", "installer"]) == 6
    assert build.main(["build", "installer", "--app"]) == 6
    assert build.main(["build", "installer", "--app", "x", "--bad", "y"]) == 6
    assert "installer --app DIR" in capsys.readouterr().err
