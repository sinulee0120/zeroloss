"""Linux package maintainer work (docs/06-install.md 4.3, 4.3-1): setup, upgrade and
removal on a fake system under a temporary root; the real shell scripts run with
DPKG_ROOT and stand-in commands. The end state after install -> remove -> purge is
the state before (files, account, firewall, sysctl)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent.deployer.manifest import Manifest
from agent.deployer.modules import Layout, ModuleStore
from agent.deployer.nodesetup import (
    RMEM_MAX,
    SLEEP_TARGETS,
    NodePaths,
    System,
    node_remove,
    node_setup,
)
from agent.deployer.package import open_package
from tests.agent.conftest import FakeAgent, demo_module, module_package, snapshot

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "installer" / "linux"


class FakeSystem(System):
    def __init__(self, ufw: bool = True) -> None:
        self.users: set[str] = set()
        self.ufw_on = ufw
        self.rules: set[str] = set()
        self.sysctl = {"net.core.rmem_max": "212992"}
        self.calls: list[list[str]] = []
        self.owned: dict[str, str] = {}
        self.masks: set[str] = set()  # systemd units masked on this PC

    def run(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:2] == ["systemctl", "is-enabled"]:
            return 0, "masked" if argv[2] in self.masks else "static"
        if argv[:2] == ["systemctl", "mask"]:
            self.masks.add(argv[2])
        elif argv[:2] == ["systemctl", "unmask"]:
            self.masks.discard(argv[2])
        elif argv[0] == "adduser":
            self.users.add(argv[-1])
        elif argv[:2] == ["ufw", "allow"]:
            self.rules.add(argv[2])
        elif argv[:3] == ["ufw", "delete", "allow"]:
            if argv[3] not in self.rules:
                return 1, "Could not delete non-existent rule"
            self.rules.discard(argv[3])
        elif argv[:2] == ["sysctl", "-p"]:
            for line in Path(argv[2]).read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    k, v = (x.strip() for x in line.split("=", 1))
                    self.sysctl[k] = v
        elif argv[:2] == ["sysctl", "-w"]:
            k, v = argv[2].split("=", 1)
            self.sysctl[k] = v
        return 0, ""

    def user_exists(self, name: str) -> bool:
        return name in self.users

    def chown(self, path: Path, user: str) -> None:
        self.owned[str(path)] = user

    def ufw_active(self) -> bool:
        return self.ufw_on

    def sysctl_get(self, key: str) -> str | None:
        return self.sysctl.get(key)


def systemctl(fake: FakeSystem) -> list[str]:
    """Service steps only (the sleep masks have their own test)."""
    sleep = ("is-enabled", "mask", "unmask")
    return [" ".join(c[1:]) for c in fake.calls if c[0] == "systemctl" and c[1] not in sleep]


def test_setup_turns_sleep_off_and_removal_undoes_only_its_own(tmp_path: Path) -> None:
    """06 4.3 (GAP-107, user: no hand work): the four sleep targets are masked; one the
    admin had masked already is neither recorded nor unmasked at removal."""
    paths, fake = NodePaths(tmp_path), FakeSystem()
    fake.masks.add("hibernate.target")  # the admin's
    node_setup(paths, fake)
    assert fake.masks == set(SLEEP_TARGETS)
    m = Manifest.load(paths.manifest)
    assert m is not None
    ours = [i.path for i in m.items if i.type == "sleep_mask"]
    assert ours == ["sleep.target", "suspend.target", "hybrid-sleep.target"]
    fake.masks.discard("suspend.target")  # someone unmasked ours: the next setup masks it again
    node_setup(paths, fake)
    m = Manifest.load(paths.manifest)
    assert m is not None and [i.path for i in m.items if i.type == "sleep_mask"] == ours
    assert fake.masks == set(SLEEP_TARGETS)
    out = node_remove(paths, fake)
    assert out["left"] == [] and fake.masks == {"hibernate.target"}


def test_setup_makes_what_the_table_says(tmp_path: Path) -> None:
    paths, fake = NodePaths(tmp_path), FakeSystem()
    out = node_setup(paths, fake)
    assert "zeroloss" in fake.users and paths.account_marker.exists()
    assert paths.data.is_dir() and paths.deployer.is_dir() and paths.modules.is_dir()
    assert fake.owned[str(paths.data)] == "zeroloss"
    assert fake.sysctl["net.core.rmem_max"] == str(RMEM_MAX)
    assert json.loads(paths.sysctl_orig.read_text()) == {"net.core.rmem_max": "212992"}
    assert fake.rules == {"10991/tcp", "10992/tcp", "10992/udp", "67/udp"}  # 06 10.2, 07 3.2
    assert systemctl(fake) == ["daemon-reload", "enable --now zeroloss-deployer.service",
                               "enable --now zeroloss-agent.service"]  # fmt: skip
    m = Manifest.load(paths.manifest)
    assert m is not None and {(i.type, i.path) for i in m.items} >= {
        ("account", "zeroloss"), ("firewall_rule", "ufw:10991/tcp"),
        ("firewall_rule", "ufw:10992/tcp"), ("sysctl_file", str(paths.sysctl_file)),
    }  # fmt: skip
    assert out["items"] == len(m.items)


def test_setup_again_is_an_upgrade(tmp_path: Path) -> None:
    """postinst runs again on upgrade: nothing is doubled, the pre-ZeroLoss value stays."""
    paths, fake = NodePaths(tmp_path), FakeSystem()
    node_setup(paths, fake)
    before = Manifest.load(paths.manifest)
    fake.calls.clear()
    node_setup(paths, fake)
    after = Manifest.load(paths.manifest)
    assert before is not None and after is not None and len(after.items) == len(before.items)
    assert not [c for c in fake.calls if c[0] in ("adduser", "ufw")]
    assert json.loads(paths.sysctl_orig.read_text()) == {"net.core.rmem_max": "212992"}


def test_the_node_may_use_the_gpu_and_removal_takes_only_that(tmp_path: Path) -> None:
    """2026-10-16 T460: the node's user was in no GPU group, VA-API failed in the decode
    check. "render" is joined; "video" (the person already put it there) is left alone."""

    class GpuSystem(FakeSystem):
        def run(self, argv: list[str]) -> tuple[int, str]:
            if argv[:2] == ["getent", "group"]:
                self.calls.append(argv)
                return {
                    "render": (0, "render:x:109:sniffer"),
                    "video": (0, "video:x:44:sniffer,zeroloss"),
                }.get(argv[2], (2, ""))
            return super().run(argv)

    paths, fake = NodePaths(tmp_path), GpuSystem()
    node_setup(paths, fake)
    assert ["adduser", "zeroloss", "render"] in fake.calls
    assert ["adduser", "zeroloss", "video"] not in fake.calls
    m = Manifest.load(paths.manifest)
    assert m is not None and ("group_member", "render") in {(i.type, i.path) for i in m.items}
    fake.calls.clear()
    node_remove(paths, fake)
    assert ["deluser", "zeroloss", "render"] in fake.calls
    assert not [c for c in fake.calls if c[:3] == ["deluser", "zeroloss", "video"]]


def test_upgrade_prerm_only_stops(tmp_path: Path) -> None:
    paths, fake = NodePaths(tmp_path), FakeSystem()
    node_setup(paths, fake)
    fake.calls.clear()
    (paths.deployer / "packages").mkdir(parents=True)
    (paths.deployer / "packages" / "current.deb").write_bytes(b"deb")
    node_remove(paths, fake, stop_only=True)
    assert systemctl(fake) == ["stop zeroloss-agent.service", "stop zeroloss-deployer.service"]
    assert paths.sysctl_file.exists() and fake.rules
    assert (paths.deployer / "packages" / "current.deb").exists()  # an update needs it


def test_install_remove_purge_is_as_before(tmp_path: Path) -> None:
    """06 9: nothing left, nothing else touched; module settings survive a remove."""
    root = tmp_path / "root"
    for system_dir in ("etc/sysctl.d", "etc/polkit-1/rules.d", "opt", "var/lib"):  # Ubuntu
        (root / system_dir).mkdir(parents=True)
    paths, fake = NodePaths(root), FakeSystem()
    before_files, before_rules = snapshot(root), set(fake.rules)
    node_setup(paths, fake)
    store = ModuleStore(Layout(paths.data, paths.modules), FakeAgent())
    store.install(open_package(module_package(demo_module(tmp_path / "src"), tmp_path / "pkg")))
    settings = paths.data / "modules" / "demo" / "settings" / "s.json"
    settings.write_text("{}")
    (paths.deployer / "packages").mkdir(parents=True)
    (paths.deployer / "packages" / "current.deb").write_bytes(b"deb")  # 06 11.5 rollback copy
    (paths.deployer / "update.json").write_text("{}")
    out = node_remove(paths, fake)
    assert out["left"] == [], out
    assert not (paths.deployer / "packages").exists()  # not user data: goes with the package
    assert not (paths.deployer / "update.json").exists()
    assert systemctl(fake)[-4:] == ["stop zeroloss-agent.service", "stop zeroloss-deployer.service",
                                    "disable zeroloss-deployer.service",
                                    "disable zeroloss-agent.service"]  # fmt: skip
    assert not (paths.modules / "demo").exists() and settings.exists()  # remove keeps data
    assert fake.rules == before_rules and fake.sysctl["net.core.rmem_max"] == "212992"
    assert not paths.sysctl_file.exists() and not paths.opt.exists()
    assert not paths.polkit_rule.exists() and not fake.masks  # sleep is back as it was
    run_script("postrm", "purge", root)  # dpkg has removed its own files before postrm
    assert snapshot(root) == before_files


def test_no_firewall_when_ufw_is_off(tmp_path: Path) -> None:
    paths, fake = NodePaths(tmp_path), FakeSystem(ufw=False)
    node_setup(paths, fake)
    node_remove(paths, fake)
    assert not [c for c in fake.calls if c[0] == "ufw"]


def test_existing_account_is_kept(tmp_path: Path) -> None:
    """An account that was there before is not ours: purge must not delete it."""
    paths, fake = NodePaths(tmp_path), FakeSystem()
    fake.users.add("zeroloss")
    node_setup(paths, fake)
    assert not paths.account_marker.exists()
    m = Manifest.load(paths.manifest)
    assert m is not None and ("account", "zeroloss") not in {(i.type, i.path) for i in m.items}


# ---------------------------------------------------------------- the shell scripts


def run_script(name: str, arg: str, root: Path, log: Path | None = None) -> str:
    """Run the real maintainer script with DPKG_ROOT and stand-in commands."""
    bin_dir = root.parent / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    log = log or root.parent / "calls.log"
    for tool in ("deluser",):
        shim = bin_dir / tool
        shim.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\n')
        shim.chmod(0o755)
    dep = root / "opt/zeroloss/deployer/zeroloss-deployer"
    if name != "postrm" and not dep.exists():  # dpkg removes it before postrm
        dep.parent.mkdir(parents=True, exist_ok=True)
        dep.write_text(f'#!/bin/sh\necho "deployer $*" >> "{log}"\n')
        dep.chmod(0o755)
    env = dict(os.environ, DPKG_ROOT=str(root), PATH=f"{bin_dir}:{os.environ['PATH']}")
    subprocess.run(["sh", str(SCRIPTS / name), arg], env=env, check=True, timeout=30)
    return log.read_text() if log.exists() else ""


@pytest.mark.parametrize(("script", "arg", "want"), [
    ("postinst", "configure", "deployer node-setup --root {root}"),
    ("prerm", "upgrade", "deployer node-remove --stop-only --root {root}"),
    ("prerm", "remove", "deployer node-remove --root {root}"),
])  # fmt: skip
def test_scripts_call_the_right_step(tmp_path: Path, script: str, arg: str, want: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert run_script(script, arg, root).strip() == want.format(root=root)


def test_postinst_ignores_other_actions(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert run_script("postinst", "abort-upgrade", root) == ""


def test_postrm_purge_removes_data_and_only_our_account(tmp_path: Path) -> None:
    root = tmp_path / "root"
    data = root / "var/lib/zeroloss"
    (data / "runs").mkdir(parents=True)
    (data / ".account-created").write_text("zeroloss\n")
    (root / "opt/zeroloss/modules").mkdir(parents=True)
    log = run_script("postrm", "purge", root)
    assert "deluser --system zeroloss" in log and not data.exists()
    assert not (root / "opt/zeroloss").exists() or any((root / "opt/zeroloss").iterdir())
    root2 = tmp_path / "root2"
    (root2 / "var/lib/zeroloss").mkdir(parents=True)  # account was there before us
    assert "deluser" not in run_script("postrm", "purge", root2, tmp_path / "log2")


def test_postrm_remove_keeps_data(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "var/lib/zeroloss/runs").mkdir(parents=True)
    (root / "opt/zeroloss/modules").mkdir(parents=True)
    run_script("postrm", "remove", root)
    assert (root / "var/lib/zeroloss/runs").is_dir()
    assert not (root / "opt/zeroloss/modules").exists()


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="needs systemd-analyze (Linux)")
def test_units_are_valid(tmp_path: Path) -> None:
    """systemd-analyze accepts the units (only the not-yet-installed program is missing)."""
    for unit in ("zeroloss-agent.service", "zeroloss-deployer.service"):
        out = subprocess.run(["systemd-analyze", "verify", str(SCRIPTS / unit)],
                             capture_output=True, text=True, timeout=60)  # fmt: skip
        problems = [line for line in (out.stdout + out.stderr).splitlines()
                    if line.strip() and "is not executable" not in line]  # fmt: skip
        assert not problems, problems


def test_polkit_rule_only_for_the_node_account_and_only_with_polkit(tmp_path: Path) -> None:
    """06 4.3: NetworkManager rights for the Wi-Fi receiver PC and sleep inhibit (SYS-005),
    recorded and removed."""
    paths, fake = NodePaths(tmp_path / "a"), FakeSystem()
    node_setup(paths, fake)
    assert not paths.polkit_rule.exists()  # no polkit on this PC: nothing written
    paths = NodePaths(tmp_path / "b")
    paths.polkit_rule.parent.mkdir(parents=True)
    node_setup(paths, fake)
    rule = paths.polkit_rule.read_text()
    import re

    allowed = set(re.findall(r'action\.id == "([^"]+)"', rule))
    assert 'subject.user == "zeroloss"' in rule and allowed == {  # only these (06 4.3)
        "org.freedesktop.NetworkManager.settings.modify.system",
        "org.freedesktop.NetworkManager.network-control",
        "org.freedesktop.NetworkManager.wifi.scan",
        "org.freedesktop.login1.inhibit-block-sleep",  # SYS-005
        "org.freedesktop.login1.inhibit-block-idle",
        "org.freedesktop.timedate1.set-ntp",  # SYS-012
    }
    m = Manifest.load(paths.manifest)
    assert m is not None and ("polkit_rule", str(paths.polkit_rule)) in {
        (i.type, i.path) for i in m.items}  # fmt: skip
    node_remove(paths, fake)
    assert not paths.polkit_rule.exists() and paths.polkit_rule.parent.is_dir()


def test_remove_takes_off_the_wan_address_the_node_added(tmp_path: Path) -> None:
    """07 3.2 B: the ISP role's 10.0.0.1/24 goes with the package, and only that."""
    paths, fake = NodePaths(tmp_path), FakeSystem()
    node_setup(paths, fake)
    lease = {"schema_version": 1, "nic": "enp1s0", "added_ip": "10.0.0.1/24", "lease": None}
    (paths.data / "wan-lease.json").write_text(json.dumps(lease))
    fake.calls.clear()
    out = node_remove(paths, fake)
    assert ["ip", "addr", "del", "10.0.0.1/24", "dev", "enp1s0"] in fake.calls
    assert out["left"] == []
    assert json.loads((paths.data / "wan-lease.json").read_text())["added_ip"] is None


def test_removal_takes_the_users_desktop_folders(tmp_path: Path) -> None:
    """03 F20 / 06 1: each user's Agent Desktop folder goes with the package; the rest of
    the user's files stay."""
    paths, fake = NodePaths(tmp_path), FakeSystem()
    node_setup(paths, fake)
    mine = paths.at("/home/alice/.local/share/zeroloss/desktop")
    (mine / "jobs").mkdir(parents=True)
    (mine / "desktop.json").write_text("{}")
    other = paths.at("/home/alice/.local/share/other-app")
    other.mkdir(parents=True)
    out = node_remove(paths, fake)
    assert out["left"] == [] and not mine.exists() and not mine.parent.exists()
    assert other.is_dir()
    assert ["pkill", "-f", "/opt/zeroloss/desktop/zeroloss-agent-desktop"] in fake.calls


def test_removal_deletes_the_isp_roles_nm_profile(tmp_path: Path) -> None:
    """GAP-88: the role's NetworkManager profile goes with the package."""
    import json as _json

    paths, fake = NodePaths(tmp_path), FakeSystem()
    node_setup(paths, fake)
    (paths.data / "wan-lease.json").write_text(_json.dumps(
        {"nic": "enp37s0", "added_ip": "10.0.0.1/24", "nm_profile": "zeroloss-wan"}))  # fmt: skip
    out = node_remove(paths, fake)
    assert out["left"] == [] and ["nmcli", "connection", "delete", "zeroloss-wan"] in fake.calls
    doc = _json.loads((paths.data / "wan-lease.json").read_text())
    assert doc["nm_profile"] is None and doc["added_ip"] is None


def test_tablet_usb_rule_for_the_node_account_recorded_and_removed(tmp_path: Path) -> None:
    """01-overview D3: the node account drives tablets over adb; only adb interfaces
    (ff/42/01) get its group, the rule goes with the package."""
    paths, fake = NodePaths(tmp_path / "a"), FakeSystem()
    node_setup(paths, fake)
    assert not paths.udev_rule.exists()  # no udev on this PC: nothing written
    paths = NodePaths(tmp_path / "b")
    paths.at("/etc/udev").mkdir(parents=True)
    node_setup(paths, fake)
    rule = paths.udev_rule.read_text()
    assert '"*:ff4201:*"' in rule and 'GROUP="zeroloss"' in rule and 'MODE="0660"' in rule
    m = Manifest.load(paths.manifest)
    assert m is not None and ("udev_rule", str(paths.udev_rule)) in {
        (i.type, i.path) for i in m.items
    }
    node_remove(paths, fake)
    assert not paths.udev_rule.exists()


def test_the_node_may_hand_a_wifi_card_to_its_own_wpa_supplicant() -> None:
    """2026-10-29 (throughput Wi-Fi settings on Linux): `nmcli device set <card> managed
    no/yes` needs NetworkManager's network-control for the zeroloss user."""
    from agent.deployer.nodesetup import POLKIT_RULE

    assert "org.freedesktop.NetworkManager.network-control" in POLKIT_RULE
