"""Automatic login set at the SSH install and put back at removal (docs/00-vision.md 1차
표, 01 D43, 20 GAP-67; 06 1 rule 2: after removal the PC is as before)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from agent.deployer import auto_login as al
from agent.deployer.nodesetup import NodePaths, node_remove, node_setup, turn_on_auto_login
from tests.agent.test_nodesetup import FakeSystem

UBUNTU = """# GDM configuration storage
#
# See /usr/share/gdm/gdm.schemas for a list of available options.

[daemon]
# Uncomment the line below to force the login screen to use Xorg
#WaylandEnable=false

# Enabling automatic login
#  AutomaticLoginEnable = true
#  AutomaticLogin = user1

[security]

[xdmcp]

[chooser]

[debug]
# Uncomment the line below to turn on debugging
#Enable=true
"""


def files(tmp_path: Path, text: str | None = UBUNTU) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    conf = tmp_path / "custom.conf"
    if text is not None:
        conf.write_text(text)
    return conf, tmp_path / "orig.json"


def active(text: str) -> dict[str, str]:
    """GDM's view: the keys set in [daemon]."""
    out, section = {}, None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            section = s.strip("[]")
        elif section == "daemon" and "=" in s and not s.startswith("#"):
            k, v = (x.strip() for x in s.split("=", 1))
            out[k] = v
    return out


def test_on_and_back(tmp_path: Path) -> None:
    conf, orig = files(tmp_path)
    r = al.enable(conf, orig, "tester")
    assert r["changed"] and active(conf.read_text()) == {
        "AutomaticLoginEnable": "true", "AutomaticLogin": "tester"}  # fmt: skip
    al.enable(conf, orig, "tester")  # twice = once
    assert conf.read_text().count(al.MARK) == 1
    assert al.disable(conf, orig) is None
    assert conf.read_text() == UBUNTU and not orig.exists()  # exactly as before


def test_the_persons_own_setting_comes_back(tmp_path: Path) -> None:
    mine = UBUNTU.replace("#  AutomaticLogin = user1", "AutomaticLogin = bob")
    conf, orig = files(tmp_path, mine)
    al.enable(conf, orig, "tester")
    assert active(conf.read_text())["AutomaticLogin"] == "tester"  # ours wins meanwhile
    conf.write_text(conf.read_text() + "\n[extra]\nX=1\n")  # changed by the person since
    al.disable(conf, orig)
    got = conf.read_text()
    assert al.MARK not in got and active(got) == {"AutomaticLogin": "bob"} and "[extra]" in got


def test_no_daemon_section_and_no_gdm(tmp_path: Path) -> None:
    conf, orig = files(tmp_path, "[security]\n")
    al.enable(conf, orig, "tester")
    assert active(conf.read_text())["AutomaticLogin"] == "tester"
    al.disable(conf, orig)
    assert conf.read_text() == "[security]\n"
    conf2, orig2 = files(tmp_path / "none", None)
    with pytest.raises(al.NotSupported):
        al.enable(conf2, orig2, "tester")
    assert not orig2.exists()


@pytest.mark.parametrize("user", ["root", "bad user", "../x", "", "A" * 40])
def test_bad_users(tmp_path: Path, user: str) -> None:
    conf, orig = files(tmp_path)
    with pytest.raises(ValueError):
        al.enable(conf, orig, user)
    assert conf.read_text() == UBUNTU


def test_monkey_files(tmp_path: Path) -> None:
    """Random GDM files (sections, comments, our keys set or not, no final newline):
    on -> the user logs in; off -> byte for byte as before."""
    rnd = random.Random(67)
    parts = ["[daemon]\n", "[security]\n", "#AutomaticLogin=x\n", "AutomaticLogin=bob\n",
             "AutomaticLoginEnable=false\n", "WaylandEnable=false\n", "\n", "# note\n"]  # fmt: skip
    for seed in range(200):
        text = "".join(rnd.choice(parts) for _ in range(rnd.randint(0, 8)))
        if rnd.random() < 0.3:
            text = text.rstrip("\n")
        conf, orig = files(tmp_path / str(seed), text)
        al.enable(conf, orig, "tester")
        on = active(conf.read_text())
        assert on.get("AutomaticLogin") == "tester" and on.get("AutomaticLoginEnable") == "true"
        al.disable(conf, orig)
        assert conf.read_text() == text, seed


def test_removal_puts_it_back(tmp_path: Path) -> None:
    paths, fake = NodePaths(tmp_path), FakeSystem()
    node_setup(paths, fake)
    paths.gdm_conf.parent.mkdir(parents=True)
    paths.gdm_conf.write_text(UBUNTU)
    turn_on_auto_login(paths, "tester")
    m = json.loads(paths.manifest.read_text())
    assert {"type": "auto_login", "path": str(paths.gdm_conf)} in [
        {k: i[k] for k in ("type", "path")} for i in m["items"]]  # fmt: skip
    out = node_remove(paths, fake)
    assert out["left"] == [] and paths.gdm_conf.read_text() == UBUNTU
    assert not paths.auto_login_orig.exists()


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from agent.deployer.__main__ import main

    (tmp_path / "etc/gdm3").mkdir(parents=True)
    (tmp_path / "etc/gdm3/custom.conf").write_text(UBUNTU)
    assert main(["auto-login", "--user", "tester", "--root", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["user"] == "tester"
    assert main(["auto-login", "--user", "root", "--root", str(tmp_path)]) == 2
    capsys.readouterr()
    assert main(["auto-login", "--user", "tester", "--root", str(tmp_path / "x")]) == 3
