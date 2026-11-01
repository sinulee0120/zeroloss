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


def test_the_kit_takes_a_made_file_the_sender_will_send_as_it_is(tmp_path: Path) -> None:
    """2026-10-31 user: the kit carries the three finished 10-minute videos only. Each is
    renamed for its channel and checked with the sender's own rule before the kit."""
    import json

    from core.events.bus import EventBus
    from modules.iptv.sender.prepare import Preparer
    from tools.kit.make_kit import sample

    src = tmp_path / "cache"
    src.mkdir()
    p = Preparer(src, EventBus(), src)
    made = src / "ch2_UHD_35M_fabc12345.ts"
    made.write_bytes(b"ts")
    key = p._key("UHD", 2, 35_000_000, Path("in"), "fabc12345")
    made.with_suffix(".json").write_text(json.dumps({"key": key, "check": [], "bytes": 2}))
    got = sample(made, tmp_path / "usb", "sample_2")
    assert got.name == "sample_2_CH2_UHD.ts" and got.with_suffix(".json").is_file()
    made.with_suffix(".json").write_text(json.dumps({"key": key, "check": [], "bytes": 9}))
    with pytest.raises(SystemExit, match="as it is"):
        sample(made, tmp_path / "usb2", "sample_2")


def test_the_public_kit_has_no_videos_and_no_ubuntu_packages(tmp_path: Path) -> None:
    """2026-11-01 user ("Video만 뺀 압축파일을 github에 올릴수가 있어?"): the public kit has
    no films and no GPL Ubuntu packages; its guide says the Linux PCs need internet once
    and has no sample step; the offline kit's guide is as it was."""
    from tools.kit import make_kit

    text = (KIT / build.GUIDE).read_text(encoding="utf-8")
    pub = make_kit.guide_text(text, "20990101.0000", True, "ZeroLoss-Kit-20990101.0000-public")
    off = make_kit.guide_text(text, "20990101.0000")
    for g in (pub, off):
        assert "<!--" not in g.replace("<!DOCTYPE", "") and "{BUILD}" not in g and "{KIT}" not in g
    assert (
        "인터넷이 필요합니다" in pub and "Generated (default)" in pub and 'id="sample"' not in pub
    )
    assert "인터넷 없이" in off and 'id="sample"' in off and "Generated (default)" not in off
    assert r"C:\ZeroLoss-Kit-20990101.0000-public" in pub
    usb = tmp_path / "usb"
    (usb / "offline").mkdir(parents=True)
    (usb / "offline" / "bash_5.deb").write_bytes(b"x")
    (usb / "tsduck_3.deb").write_bytes(b"t")
    setup = (
        tmp_path / f"ZeroLoss-Controller-Setup-{(build.ROOT / 'VERSION').read_text().strip()}.exe"
    )
    setup.write_bytes(b"exe")
    make_kit.main(["--public", "--setup", str(setup), "--offline", str(usb),
                   "--out", str(tmp_path / "out")])  # fmt: skip
    (kit,) = (tmp_path / "out").iterdir()
    files = sorted(f.relative_to(kit).as_posix() for f in kit.rglob("*") if f.is_file())
    assert kit.name.endswith("-public") and files == [
        "1_Windows_PC/" + setup.name, "2_Linux_PC_USB/tsduck_3.deb",
        "2_Linux_PC_USB/zeroloss-linux-setup.sh", build.GUIDE]  # fmt: skip
    with pytest.raises(SystemExit):
        make_kit.main(["--setup", str(setup), "--offline", str(usb), "--out", str(tmp_path)])
