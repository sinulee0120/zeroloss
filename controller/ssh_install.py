"""First install of a Linux node over SSH (docs/06-install.md 11.3, 13-ai-loop).

Order: host key (before any password is sent) -> log in -> checks (CPU, OS, disk,
ports) -> sudo -> upload the .deb -> ``dpkg -i`` (waits while the package database is
locked) -> the node and its Deployer answer -> device list -> module packages ->
tools present. The password is used once: only as the SSH password and on sudo's
standard input; it is never stored, logged or put in a result or error.
Host keys: trusted and saved the first time, a different key later stops (INS-009).
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncssh

from controller.deploy import Deploy
from controller.devices import DEPLOYER_PORT, DeviceList
from core.events.bus import utc_now
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

CONNECT_TIMEOUT_S = 15.0  # 09 0.4 SSH
LOCK_WAIT_S, LOCK_LIMIT_S = 30.0, 600.0  # INS-011: every 30 s, at most 10 min
ANSWER_LIMIT_S = 60.0  # the new node and Deployer must answer within this
DISK_MIN_MB = 1024
SUPPORTED = {("ubuntu", "24.04")}  # the package is built on the same OS (06 11.3)
TOOLS = ("ffmpeg", "ffprobe", "tsp", "mpv", "vlc")
# 06 11.3 4-3: how each tool is installed — from Ubuntu (apt) or from the Controller's
# packages/tools/ (not in Ubuntu: TSDuck). libxcb-cursor0: Agent Desktop's screen (Qt).
APT_PACKAGE = {"ffmpeg": "ffmpeg", "ffprobe": "ffmpeg", "mpv": "mpv", "vlc": "vlc"}
BUNDLED = {"tsp": "tsduck_*.deb"}
ALWAYS_APT = ("libxcb-cursor0",)
APT_LOCK_S = 600  # INS-011: apt waits for the lock itself, up to 10 minutes
LOCKED = ("dpkg frontend lock", "could not get lock", "dpkg status database is locked")
DEPLOYER = "/opt/zeroloss/deployer/zeroloss-deployer"  # put there by the .deb (06 4.3)


@dataclass
class Target:
    host: str
    user: str
    password: str = field(repr=False)
    name: str = ""
    ssh_port: int = 22
    node_port: int = 10992
    deployer_port: int = DEPLOYER_PORT
    auto_login: bool = False  # the person said yes (00-vision "확인 1회", GAP-67)


class KnownHosts:
    """``ssh-known-hosts.json`` in the Controller data folder (02 11)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, Any]:
        try:
            data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"schema_version": 1, "hosts": {}}
        return data

    def check(self, host: str, key: asyncssh.SSHKey, accept_new: bool) -> str:
        """``new`` (saved now), ``same`` or raises INS-009 for a changed key."""
        data = self._load()
        sha = key.get_fingerprint("sha256")
        known = data["hosts"].get(host)
        if known and known.get("sha256") == sha:
            return "same"
        if known and not accept_new:
            raise ZlError(
                "INS-009",
                f"The SSH host key of {host} has changed",
                "The PC may have been reinstalled, or another machine answers on this IP.",
                "Check the PC, then install again with --accept-new-host-key.",
                detail=f"known {known.get('sha256')}, now {sha}",
            )
        data["hosts"][host] = {
            "key_type": key.get_algorithm(),
            "sha256": sha,
            "added_utc": utc_now(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return "new" if not known else "replaced"


class LinuxInstaller:
    def __init__(
        self,
        known_hosts: KnownHosts,
        deb: Path,
        modules: list[Path],
        devices: DeviceList,
        boot_id: str,
        generation: int = 0,
        lock_wait_s: float = LOCK_WAIT_S,
        lock_limit_s: float = LOCK_LIMIT_S,
        answer_limit_s: float = ANSWER_LIMIT_S,
        on_step: Callable[[str], None] | None = None,
        tools_dir: Path | None = None,
    ) -> None:
        self.tools_dir = tools_dir  # Controller packages/tools/ (06 11.3 4-3)
        self.known_hosts = known_hosts
        self.deb = deb
        self.modules = modules
        self.devices = devices
        self.boot_id = boot_id
        self.generation = generation
        self.lock_wait_s, self.lock_limit_s = lock_wait_s, lock_limit_s
        self.answer_limit_s = answer_limit_s
        self.on_step = on_step or (lambda _: None)

    def install(self, t: Target, accept_new_host_key: bool = False) -> dict[str, Any]:
        steps: list[dict[str, Any]] = []
        result: dict[str, Any] = {"host": t.host, "name": t.name or t.host, "steps": steps}
        remote = asyncio.run(self._remote(t, accept_new_host_key, steps, result))
        result.update(remote)
        self._step(steps, "answer", self._wait_answer(t))
        name = t.name or t.host.replace(".", "-")
        dev = self.devices.add(name, t.host, t.node_port, self.boot_id, t.deployer_port)
        self._step(steps, "register", dev.name)
        deploy = Deploy(self.devices, self.boot_id, self.generation)
        for zlm in self.modules:
            r = deploy.install(dev.name, zlm)
            self._step(steps, "module", f"{r['id']} {r['build']} {r['result']}")
        result["name"] = dev.name
        return result

    def _step(self, steps: list[dict[str, Any]], name: str, detail: str = "") -> None:
        steps.append({"name": name, "ok": True, "detail": detail})
        self.on_step(name)

    # ---------------------------------------------------------------- over SSH

    async def _remote(
        self, t: Target, accept_new: bool, steps: list[dict[str, Any]], result: dict[str, Any]
    ) -> dict[str, Any]:
        key = await self._host_key(t)
        seen = self.known_hosts.check(t.host, key, accept_new)
        result["host_key"] = {"sha256": key.get_fingerprint("sha256"), "status": seen}
        self._step(steps, "host_key", seen)
        try:
            conn = await asyncio.wait_for(
                asyncssh.connect(
                    t.host,
                    t.ssh_port,
                    username=t.user,
                    password=t.password,
                    known_hosts=([key], [], []),
                    client_keys=None,
                    agent_path=None,
                    preferred_auth="password,keyboard-interactive",
                ),
                CONNECT_TIMEOUT_S,
            )
        except asyncssh.PermissionDenied:
            if t.user == "root":
                raise ZlError(
                    "INS-008",
                    "root may not log in over SSH",
                    "Ubuntu blocks root password logins.",
                    "Use a sudo account (the same account you log in with) instead of root.",
                ) from None
            raise ZlError(
                "INS-007",
                f"SSH login failed for {t.user}@{t.host}",
                "The user name or password is wrong.",
                "Check them and try again (up to 3 times).",
            ) from None
        except (OSError, asyncssh.Error, TimeoutError) as e:
            raise ZlError(
                "DSC-014",
                f"Cannot reach {t.host} over SSH",
                str(e),
                "Check the PC, its cable and that SSH is running.",
            ) from None
        async with conn:
            self._step(steps, "login")
            sudo = "" if t.user == "root" else "sudo -S -p '' "
            await self._checks(conn, t)
            self._step(steps, "checks")
            if sudo:
                await self._sudo_ok(conn, t)
                self._step(steps, "sudo")
            work = f"/tmp/zeroloss-install-{int(time.time())}"
            try:
                await conn.run(f"mkdir -p {work}", check=True)
                target = f"{work}/{self.deb.name}"
                async with conn.start_sftp_client() as sftp:
                    await sftp.put(str(self.deb), target)
                self._step(steps, "upload", self.deb.name)
                await self._dpkg(conn, t, sudo, target)
                self._step(steps, "install")
                self._step(steps, "rollback_copy", await self._keep_deb(conn, t, sudo, target))
                if t.auto_login:
                    result["auto_login"] = await self._auto_login(conn, t, sudo)
                    self._step(steps, "auto_login", result["auto_login"])
                missing = await self._tools(conn)
                if missing:
                    got = await self._install_tools(conn, t, sudo, work, missing)
                    result["tools_installed"] = got["installed"]
                    self._step(steps, "tools_install", got["detail"])
                    missing = got["missing"]
                self._step(steps, "tools", ", ".join(missing) or "all present")
            finally:
                await conn.run(f"rm -rf {work}", check=False)
        return {"tools_missing": missing}

    async def _host_key(self, t: Target) -> asyncssh.SSHKey:
        try:
            key = await asyncio.wait_for(
                asyncssh.get_server_host_key(t.host, t.ssh_port), CONNECT_TIMEOUT_S
            )
        except (OSError, asyncssh.Error, TimeoutError) as e:
            raise ZlError(
                "DSC-014",
                f"Cannot reach {t.host} over SSH",
                str(e),
                "Check the PC, its cable and that SSH is running.",
            ) from None
        if key is None:
            raise ZlError("DSC-014", f"{t.host} sent no SSH host key")
        return key

    async def _checks(self, conn: asyncssh.SSHClientConnection, t: Target) -> None:
        arch = (await conn.run("uname -m", check=False)).stdout
        os_id = (await conn.run('. /etc/os-release; echo "$ID $VERSION_ID"', check=False)).stdout
        found = tuple(str(os_id or "").split()[:2])
        if str(arch).strip() != "x86_64" or found not in SUPPORTED:
            want = ", ".join(f"{a} {b}" for a, b in sorted(SUPPORTED))
            here = f"{' '.join(found) or '?'} on {str(arch).strip()}"
            raise ZlError(
                "INS-005",
                f"{t.host} is {here}",
                f"The package is built for x86_64 {want}.",
                "Use a supported PC, or build the package on that OS (06 11.3).",
            )
        df = str((await conn.run("df -Pk /opt /var/lib | tail -n +2", check=False)).stdout)
        free = min(
            (int(line.split()[3]) // 1024 for line in df.splitlines() if line.split()), default=0
        )
        if free < DISK_MIN_MB:
            raise ZlError(
                "INS-002",
                f"Not enough disk on {t.host}",
                f"{free} MB free.",
                f"Free at least {DISK_MIN_MB} MB.",
            )
        ours = (await conn.run("systemctl is-active zeroloss-agent", check=False)).stdout
        if str(ours).strip() != "active":  # a reinstall may find our own ports in use
            ports = str((await conn.run("ss -ltnH", check=False)).stdout)
            used = [p for p in (t.node_port, t.deployer_port) if f":{p} " in ports + " "]
            if used:
                raise ZlError(
                    "INS-003",
                    f"Port {used[0]} is in use on {t.host}",
                    "Another program listens there.",
                    "Stop that program, then install again.",
                )

    async def _sudo_ok(self, conn: asyncssh.SSHClientConnection, t: Target) -> None:
        r = await conn.run("sudo -S -p '' true", input=t.password + "\n", check=False)
        if r.exit_status != 0:
            raise ZlError(
                "INS-010",
                f"{t.user} cannot use sudo on {t.host}",
                _last(str(r.stderr)) or "sudo refused the password.",
                "Use an account in the sudo group.",
            )

    async def _dpkg(
        self, conn: asyncssh.SSHClientConnection, t: Target, sudo: str, deb: str
    ) -> None:
        end = time.monotonic() + self.lock_limit_s
        cmd = f"{sudo}dpkg -i {shlex.quote(deb)}"
        while True:
            r = await conn.run(cmd, input=t.password + "\n" if sudo else None, check=False)
            out = str(r.stdout or "") + str(r.stderr or "")
            if r.exit_status == 0:
                return
            if any(x in out.lower() for x in LOCKED) and time.monotonic() < end:
                self.on_step("waiting for the package lock")  # INS-011
                await asyncio.sleep(self.lock_wait_s)
                continue
            if any(x in out.lower() for x in LOCKED):
                raise ZlError(
                    "INS-011",
                    f"The package system on {t.host} stayed busy",
                    "Automatic updates held the dpkg lock for 10 minutes.",
                    "Wait for the updates to finish, then install again.",
                )
            await conn.run(
                f"{sudo}dpkg -r zeroloss-agent",
                input=t.password + "\n" if sudo else None,
                check=False,
            )
            raise ZlError(
                "INS-028",
                f"Install failed on {t.host}",
                _last(out),
                "Read the line above, fix the cause on that PC, then install again.",
            )

    async def _auto_login(self, conn: asyncssh.SSHClientConnection, t: Target, sudo: str) -> str:
        """00-vision / D43: this account logs in by itself after a reboot, so Agent Desktop
        (the PC player, 03 F20) comes back. Not a reason to fail the install."""
        if t.user == "root":
            return "not set: root is not a desktop user (log in once and turn it on)"
        cmd = f"{sudo}{DEPLOYER} auto-login --user {shlex.quote(t.user)}"
        r = await conn.run(cmd, input=t.password + "\n" if sudo else None, check=False)
        if r.exit_status == 0:
            return f"on for {t.user}"
        why = _last(str(r.stdout or r.stderr or ""))
        return f"not set ({why}): turn on automatic login on that PC once"

    async def _keep_deb(
        self, conn: asyncssh.SSHClientConnection, t: Target, sudo: str, deb: str
    ) -> str:
        """06 11.5: the installed .deb stays as ``current.deb`` so the first update over
        10991 can go back to it. Not a reason to fail the install."""
        script = ('. /etc/zeroloss/zeroloss.conf && '
                  'install -D -m 0600 "$1" "$ZL_DATA/deployer/packages/current.deb"')  # fmt: skip
        r = await conn.run(f"{sudo}sh -c {shlex.quote(script)} sh {shlex.quote(deb)}",
                           input=t.password + "\n" if sudo else None, check=False)  # fmt: skip
        if r.exit_status == 0:
            return "kept"
        return (
            f"not kept ({_last(str(r.stderr or r.stdout or ''))}): the first update cannot go back"
        )

    async def _install_tools(
        self,
        conn: asyncssh.SSHClientConnection,
        t: Target,
        sudo: str,
        work: str,
        missing: list[str],
    ) -> dict[str, Any]:
        """06 11.3 4-3 (GAP-73 ①): the missing tools in the same SSH session and sudo —
        apt for what Ubuntu has, the Controller's bundled .deb for the rest. Never
        fails the install: what is still missing is told with why."""
        pkgs = sorted({APT_PACKAGE[x] for x in missing if x in APT_PACKAGE} | set(ALWAYS_APT))
        debs = []
        for tool in missing:
            pattern = BUNDLED.get(tool)
            found = sorted(self.tools_dir.glob(pattern)) if pattern and self.tools_dir else []
            if found:
                debs.append(found[-1])
        remote = []
        if debs:
            async with conn.start_sftp_client() as sftp:
                for d in debs:
                    await sftp.put(str(d), f"{work}/{d.name}")
                    remote.append(f"{work}/{d.name}")
        lock = f"-o DPkg::Lock::Timeout={APT_LOCK_S}"
        script = ("export DEBIAN_FRONTEND=noninteractive; "
                  f"apt-get {lock} update -qq; "
                  f"apt-get {lock} install -y -q --no-install-recommends "
                  + " ".join(shlex.quote(x) for x in [*pkgs, *remote]))  # fmt: skip
        self.on_step("installing tools")
        r = await conn.run(f"{sudo}sh -c {shlex.quote(script)}",
                           input=t.password + "\n" if sudo else None, check=False)  # fmt: skip
        still = await self._tools(conn)
        installed = [x for x in missing if x not in still]
        why = _last(str(r.stderr or r.stdout or "")) if still else ""
        detail = (f"installed {', '.join(installed) or 'nothing'}"
                  + (f"; still missing {', '.join(still)}: {why}" if still else ""))  # fmt: skip
        return {"installed": installed, "missing": still, "detail": detail}

    async def _tools(self, conn: asyncssh.SSHClientConnection) -> list[str]:
        script = "; ".join(f"command -v {x} >/dev/null || echo {x}" for x in TOOLS)
        r = await conn.run(script, check=False)
        return [x for x in str(r.stdout).split() if x in TOOLS]

    # ---------------------------------------------------------------- after

    def _wait_answer(self, t: Target) -> str:
        end = time.monotonic() + self.answer_limit_s
        node = NodeClient(t.host, t.node_port, self.boot_id, t.host)
        dep = NodeClient(t.host, t.deployer_port, self.boot_id, t.host)
        try:
            while True:
                try:
                    build = node.get("/v1/health").get("build")
                    dep.get("/v1/deployer/health")
                    return f"node {build}"
                except ZlError as e:
                    if time.monotonic() > end:
                        raise ZlError(
                            "INS-014",
                            f"The new node on {t.host} does not answer",
                            e.why,
                            "Look at 'systemctl status zeroloss-agent' there.",
                        ) from None
                    time.sleep(0.5)
        finally:
            node.close()
            dep.close()


def newest_deb(folder: Path) -> Path:
    """The newest ``zeroloss-agent_<build>_amd64.deb`` the Controller holds (06 11.3)."""
    debs = sorted(folder.glob("zeroloss-agent_*_amd64.deb"))
    if not debs:
        raise ZlError("INS-012", "No Linux node package on this Controller", str(folder),
                      "Build it (python -m tools.build deb) or give --package.")  # fmt: skip
    return debs[-1]


def newest_tablet(folder: Path) -> list[Path]:
    """The newest tablet package there (the app and adb, ``zeroloss-tablet-<build>.zlt``):
    a PC installed over SSH gets it with the modules, so tablets work from the start."""
    found = sorted(folder.glob("zeroloss-tablet-*.zlt"))
    return found[-1:]


def newest_modules(folder: Path) -> list[Path]:
    """The newest build of every module package there (``zeroloss-module-<id>-<build>.zlm``)."""
    best: dict[str, Path] = {}
    for p in sorted(folder.glob("zeroloss-module-*-*.zlm")):
        mid = p.name[len("zeroloss-module-") :].rsplit("-", 1)[0]
        best[mid] = p  # sorted: the last one per id is the newest build
    return [best[k] for k in sorted(best)]


def _last(text: str) -> str:
    lines = [x for x in text.strip().splitlines() if x.strip()]
    return lines[-1][:200] if lines else ""
