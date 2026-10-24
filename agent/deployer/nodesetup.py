"""What the Linux package's maintainer scripts do (docs/06-install.md 4.3, 4.3-1).

``postinst configure`` -> ``node_setup``; ``prerm upgrade`` -> ``node_remove(stop_only)``;
``prerm remove`` -> ``node_remove``. postrm is a few shell lines (dpkg has already
removed this program by then). Everything the package makes that dpkg does not track
is listed in the node manifest (02 14), so removal takes exactly that.
All changes to the system go through ``System``: tests use a fake one and a temporary
root folder (L0: standard library only, 12 1-1).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.deployer import auto_login
from agent.deployer.manifest import Item, Manifest, utc_now
from agent.deployer.media_import import MediaImport
from agent.deployer.modules import Agent, Layout, ModuleStore

ACCOUNT = "zeroloss"
GPU_GROUPS = ("render", "video")  # the decode check uses the GPU (VA-API)
# 06 4.3: Deployer, node, discovery, the ISP role's DHCP (bound to the WAN NIC only)
PORTS = ("10991/tcp", "10992/tcp", "10992/udp", "67/udp")
RMEM_MAX = 16 * 1024 * 1024  # 05 4.3: 16 MB receive buffer
SERVICES = ("zeroloss-deployer", "zeroloss-agent")  # the Deployer first: it watches the node
SYSCTL_KEY = "net.core.rmem_max"
# 06 4.3 (GAP-107): a test PC never sleeps; masked at install, unmasked at removal
SLEEP_TARGETS = ("sleep.target", "suspend.target", "hibernate.target", "hybrid-sleep.target")
# 06 4.3: the node account may join the test Wi-Fi through NetworkManager (03 F17
# "무선 수신 PC") and keep the PC awake during a test (09 SYS-005); only these actions,
# only this account.
POLKIT_RULE = """// ZeroLoss: the node service connects a receiver PC to the test Wi-Fi (S-51),
// keeps the PC from sleeping while a test runs (SYS-005) and holds its automatic time
// sync during a test, turning it back on after (SYS-012)
polkit.addRule(function(action, subject) {
    if (subject.user == "zeroloss" &&
        (action.id == "org.freedesktop.NetworkManager.settings.modify.system" ||
         action.id == "org.freedesktop.NetworkManager.network-control" ||
         action.id == "org.freedesktop.NetworkManager.wifi.scan" ||
         action.id == "org.freedesktop.login1.inhibit-block-sleep" ||
         action.id == "org.freedesktop.login1.inhibit-block-idle" ||
         action.id == "org.freedesktop.timedate1.set-ntp")) {
        return polkit.Result.YES;
    }
});
"""
# 01-overview D3: the receiving PC drives up to three tablets over USB (adb) as the node
# account. A tablet's USB device is root:plugdev, with a desktop ACL for the person at the
# screen only; this gives the node account the devices that show an adb interface
# (class ff, subclass 42, protocol 01), whatever the maker (2026-10-23 T460, Galaxy Tab).
UDEV_RULE = """# ZeroLoss: the node service drives Android tablets over adb (01-overview D3)
ACTION=="add|change|bind", SUBSYSTEM=="usb", ENV{DEVTYPE}=="usb_device", \
  ENV{ID_USB_INTERFACES}=="*:ff4201:*", MODE="0660", GROUP="zeroloss"
"""


@dataclass(frozen=True)
class NodePaths:
    root: Path = Path("/")

    def at(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    @property
    def opt(self) -> Path:
        return self.at("/opt/zeroloss")

    @property
    def modules(self) -> Path:
        return self.at("/opt/zeroloss/modules")

    @property
    def data(self) -> Path:
        return self.at("/var/lib/zeroloss")

    @property
    def deployer(self) -> Path:
        return self.data / "deployer"

    @property
    def manifest(self) -> Path:
        return self.deployer / "manifests" / "node-agent.json"

    @property
    def sysctl_file(self) -> Path:
        return self.at("/etc/sysctl.d/90-zeroloss.conf")

    @property
    def polkit_rule(self) -> Path:
        return self.at("/etc/polkit-1/rules.d/90-zeroloss.rules")

    @property
    def udev_rule(self) -> Path:
        return self.at("/etc/udev/rules.d/70-zeroloss-adb.rules")

    @property
    def gdm_conf(self) -> Path:
        return self.at("/etc/gdm3/custom.conf")

    @property
    def auto_login_orig(self) -> Path:
        return self.deployer / "auto-login-orig.json"

    @property
    def sysctl_orig(self) -> Path:
        return self.deployer / "sysctl-orig.json"

    @property
    def account_marker(self) -> Path:
        return self.data / ".account-created"  # postrm purge deletes the account only then


class System:
    """The real system (root on Linux). Every command has a time limit (12 4-3)."""

    def run(self, argv: list[str]) -> tuple[int, str]:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        return done.returncode, (done.stdout + done.stderr).strip()

    def user_exists(self, name: str) -> bool:
        import pwd

        try:
            pwd.getpwnam(name)
        except KeyError:
            return False
        return True

    def chown(self, path: Path, user: str) -> None:
        shutil.chown(path, user, user)

    def ufw_active(self) -> bool:
        if not shutil.which("ufw"):
            return False
        rc, out = self.run(["ufw", "status"])
        return rc == 0 and out.lower().startswith("status: active")

    def sysctl_get(self, key: str) -> str | None:
        rc, out = self.run(["sysctl", "-n", key])
        return out if rc == 0 and out else None

    def masked(self, unit: str) -> bool:
        _, out = self.run(["systemctl", "is-enabled", unit])
        return out.strip() == "masked"


def node_setup(paths: NodePaths, system: System) -> dict[str, Any]:
    """postinst configure: account, folders, sysctl, firewall, manifest, services."""
    m = Manifest.load(paths.manifest) or Manifest("node", "agent", "", utc_now())
    have = {(i.type, i.path) for i in m.items}
    steps: list[str] = []

    def keep(item: Item) -> None:
        if (item.type, item.path) not in have:
            m.items.append(item)
            have.add((item.type, item.path))

    if not system.user_exists(ACCOUNT):
        argv = ["adduser", "--system", "--group", "--home", str(paths.data), "--no-create-home"]
        rc, out = system.run([*argv, "--shell", "/usr/sbin/nologin", ACCOUNT])
        if rc != 0:
            raise RuntimeError(f"adduser failed: {out}")
        keep(Item("account", ACCOUNT))
        paths.data.mkdir(parents=True, exist_ok=True)
        paths.account_marker.write_text(ACCOUNT + "\n", encoding="utf-8")
        steps.append("account created")
    # 2026-10-16 T460: the node's decode check could not open the GPU (VA-API needs
    # /dev/dri/renderD*, group "render"; older systems "video") and said "no hardware
    # decoding" on a PC whose player decodes in hardware. Only memberships we add are kept.
    for group in GPU_GROUPS:
        rc, out = system.run(["getent", "group", group])
        if rc != 0 or not out.strip():
            continue  # no such group here
        if ACCOUNT in out.strip().rsplit(":", 1)[-1].split(","):
            continue  # already a member (ours from before stays recorded)
        rc, out = system.run(["adduser", ACCOUNT, group])
        if rc == 0:
            keep(Item("group_member", group))
            steps.append(f"added to group {group}")
    for folder, owner in ((paths.data, ACCOUNT), (paths.deployer, None), (paths.modules, None)):
        if not folder.exists():
            folder.mkdir(parents=True)
            keep(Item("dir", str(folder)))
        if owner:
            system.chown(folder, owner)
    (paths.deployer / "manifests").mkdir(parents=True, exist_ok=True)
    if not paths.sysctl_orig.exists():  # the value before ZeroLoss, recorded once
        orig = system.sysctl_get(SYSCTL_KEY)
        paths.sysctl_orig.write_text(json.dumps({SYSCTL_KEY: orig}), encoding="utf-8")
    paths.sysctl_file.parent.mkdir(parents=True, exist_ok=True)
    paths.sysctl_file.write_text(f"# ZeroLoss receive buffer (06 4.3)\n{SYSCTL_KEY} = {RMEM_MAX}\n",
                                 encoding="utf-8")  # fmt: skip
    keep(Item("sysctl_file", str(paths.sysctl_file)))
    system.run(["sysctl", "-p", str(paths.sysctl_file)])
    steps.append("sysctl applied")
    if paths.polkit_rule.parent.is_dir():  # no polkit (no NetworkManager): nothing to allow
        paths.polkit_rule.write_text(POLKIT_RULE, encoding="utf-8")
        keep(Item("polkit_rule", str(paths.polkit_rule)))
        steps.append("polkit rule written")
    if paths.udev_rule.parent.parent.is_dir():  # /etc/udev: tablets plugged in now get it too
        paths.udev_rule.parent.mkdir(exist_ok=True)
        paths.udev_rule.write_text(UDEV_RULE, encoding="utf-8")
        keep(Item("udev_rule", str(paths.udev_rule)))
        system.run(["udevadm", "control", "--reload"])
        system.run(["udevadm", "trigger", "--subsystem-match=usb", "--action=change"])
        steps.append("tablet USB rule written")
    for target in SLEEP_TARGETS:  # an admin's own mask is not recorded, so never undone
        if system.masked(target):
            continue  # ours from before stays recorded; someone else's stays theirs
        rc, out = system.run(["systemctl", "mask", target])
        if rc != 0:
            raise RuntimeError(f"systemctl mask {target} failed: {out}")
        keep(Item("sleep_mask", target))
    steps.append("sleep off")
    if system.ufw_active():
        for port in PORTS:
            rule = f"ufw:{port}"
            if ("firewall_rule", rule) not in have:
                system.run(["ufw", "allow", port, "comment", "ZeroLoss"])
                keep(Item("firewall_rule", rule))
        steps.append("firewall rules added")
    m.installed_utc = m.installed_utc or utc_now()
    m.save(paths.manifest)
    system.run(["systemctl", "daemon-reload"])
    for unit in SERVICES:
        system.run(["systemctl", "enable", "--now", f"{unit}.service"])
    steps.append("services started")
    return {"steps": steps, "items": len(m.items)}


class _NoNode(Agent):
    """While the package is being removed the node is already stopped."""

    def busy(self) -> bool:
        return False

    def restart(self) -> bool:
        return True


def node_remove(paths: NodePaths, system: System, stop_only: bool = False) -> dict[str, Any]:
    """prerm: stop services; on remove also module code, firewall rules and sysctl."""
    for unit in reversed(SERVICES):  # the node first, so the Deployer does not restart it
        system.run(["systemctl", "stop", f"{unit}.service"])
    if stop_only:
        return {"steps": ["services stopped"], "left": []}
    for unit in SERVICES:
        system.run(["systemctl", "disable", f"{unit}.service"])
    steps = ["services stopped"]
    left: list[str] = []
    store = ModuleStore(Layout(paths.data, paths.modules), _NoNode())
    for mod in store.installed():
        try:
            store.remove(mod["id"])  # settings stay (user data), code and cache go (06 12)
        except Exception as e:  # report and go on: the package removal must finish
            left.append(f"module {mod['id']}: {e}")
    steps.append("modules removed")
    # 03 F20: Agent Desktops still open in user sessions go with the package (their players
    # already stopped when the node did)
    system.run(["pkill", "-f", "/opt/zeroloss/desktop/zeroloss-agent-desktop"])
    left += _remove_desktop_folders(paths)
    shutil.rmtree(paths.deployer / "packages", ignore_errors=True)  # 06 11.5: not user data
    paths.at("/etc/apt/apt.conf.d/99zeroloss-test-hold").unlink(missing_ok=True)  # SYS-005
    (paths.deployer / "update.json").unlink(missing_ok=True)
    m = Manifest.load(paths.manifest)
    rest = []
    for item in m.items if m else []:
        if item.type == "firewall_rule":
            port = item.path.split(":", 1)[1]
            rc, out = system.run(["ufw", "delete", "allow", port])
            if rc != 0:
                left.append(f"{item.path}: {out}")
                rest.append(item)
        elif item.type == "sysctl_file":
            Path(item.path).unlink(missing_ok=True)
            _restore_sysctl(paths, system)
        elif item.type == "polkit_rule":
            Path(item.path).unlink(missing_ok=True)
        elif item.type == "udev_rule":
            Path(item.path).unlink(missing_ok=True)
            system.run(["udevadm", "control", "--reload"])
        elif item.type == "group_member":  # only what the install added
            rc, out = system.run(["deluser", ACCOUNT, item.path])
            if rc != 0:
                left.append(f"group {item.path}: {out}")
                rest.append(item)
        elif item.type == "sleep_mask":  # only what the install masked (GAP-107)
            rc, out = system.run(["systemctl", "unmask", item.path])
            if rc != 0:
                left.append(f"{item.path}: still masked ({out})")
                rest.append(item)
        elif item.type == "auto_login":  # the GDM file as it was (00-vision, GAP-67)
            why = auto_login.disable(Path(item.path), paths.auto_login_orig)
            if why:
                left.append(why)
                rest.append(item)
        else:
            rest.append(item)  # account and folders hold user data: postrm purge
    if m is not None:
        m.items = rest
        m.save(paths.manifest)
    left += _undo_wan_address(paths, system)
    # 12 1 media_import (GAP-109): what the Deployer brought from users' folders goes,
    # what a person put in those folders stays
    left += MediaImport(paths.root, paths.data, paths.modules, lambda p: None,
                        lambda level, text: None).remove()  # fmt: skip
    for folder in (paths.modules, paths.opt):  # empty folders only (dpkg may still own files)
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    for leftover in (paths.sysctl_file, paths.polkit_rule, paths.udev_rule):
        if leftover.exists():
            left.append(f"{leftover}: still there")
    if paths.gdm_conf.is_file() and auto_login.MARK in paths.gdm_conf.read_text("utf-8"):
        left.append(f"{paths.gdm_conf}: automatic login still on")
    steps.append("firewall and sysctl restored")
    return {"steps": steps, "left": left}


def _remove_desktop_folders(paths: NodePaths) -> list[str]:
    """03 F20: each user's Agent Desktop folder (players' work files, S-53 choice) goes
    with the package (06 1: nothing left)."""
    left = []
    for home in [*sorted(paths.at("/home").glob("*")), paths.at("/root")]:
        folder = home / ".local" / "share" / "zeroloss" / "desktop"
        if folder.is_dir():
            shutil.rmtree(folder, ignore_errors=True)
            if folder.exists():
                left.append(f"{folder}: still there")
            if not any(folder.parent.iterdir()):  # zeroloss/ only when nothing else is in it
                folder.parent.rmdir()
    return left


def _undo_wan_address(paths: NodePaths, system: System) -> list[str]:
    """07 3.2 B: the address the sender's ISP role added to its WAN NIC goes with the
    package (only that one; the node records it in ``wan-lease.json``)."""
    try:
        doc = json.loads((paths.data / "wan-lease.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    nic, added = doc.get("nic"), doc.get("added_ip")
    if not nic or not added:
        return []
    profile = doc.get("nm_profile")
    if profile:  # GAP-88: NetworkManager profile; the NIC's own profile comes back
        rc, out = system.run(["nmcli", "connection", "delete", str(profile)])
        if rc != 0 and "not found" not in out.lower() and "unknown" not in out.lower():
            return [f"NetworkManager profile {profile}: {out}"]
    else:
        rc, out = system.run(["ip", "addr", "del", str(added), "dev", str(nic)])
        if rc != 0 and "Cannot assign" not in out:
            return [f"{added} on {nic}: {out}"]
    doc["nic"], doc["added_ip"], doc["nm_profile"] = None, None, None
    (paths.data / "wan-lease.json").write_text(json.dumps(doc), encoding="utf-8")
    return []


def _restore_sysctl(paths: NodePaths, system: System) -> None:
    try:
        orig = json.loads(paths.sysctl_orig.read_text(encoding="utf-8")).get(SYSCTL_KEY)
    except (OSError, ValueError):
        return
    if orig:
        system.run(["sysctl", "-w", f"{SYSCTL_KEY}={orig}"])
    paths.sysctl_orig.unlink(missing_ok=True)


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def turn_on_auto_login(paths: NodePaths, user: str) -> dict[str, Any]:
    """``zeroloss-deployer auto-login --user <u>`` (root, over SSH at install, 06 11.3):
    GDM automatic login for that user, recorded in the node manifest for removal."""
    out = auto_login.enable(paths.gdm_conf, paths.auto_login_orig, user)
    m = Manifest.load(paths.manifest) or Manifest("node", "agent", "", utc_now())
    if not any(i.type == "auto_login" for i in m.items):
        m.items.append(Item("auto_login", str(paths.gdm_conf)))
        m.save(paths.manifest)
    return out
