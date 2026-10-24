"""A stand-in Linux PC over SSH (asyncssh server in a thread) for the installer tests.

It answers the commands the installer runs (uname, os-release, df, ss, sudo, dpkg …),
keeps uploaded files under a temporary root, and calls ``on_dpkg`` when a package
"installs" so a test can start a real node there."""

from __future__ import annotations

import asyncio
import shlex
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import asyncssh


class FakeLinuxPC:
    def __init__(self, root: Path, users: dict[str, str], sudoers: set[str]) -> None:
        self.root = root
        self.users = users
        self.sudoers = sudoers
        self.os_id = "ubuntu 24.04"
        self.arch = "x86_64"
        self.free_kb = 50_000_000
        self.listening = ""
        self.locked_times = 0
        self.dpkg_error = ""
        self.tools = {"ffmpeg", "ffprobe", "tsp"}  # the players are missing
        self.online = True  # apt can reach the Ubuntu mirrors
        self.apt_runs: list[str] = []
        self.on_dpkg: Callable[[], None] = lambda: None
        self.no_gdm = False
        self.auto_login_user: str | None = None
        self.commands: list[str] = []
        self.stdin_seen: list[str] = []
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self.port = 0
        self._loop = asyncio.new_event_loop()
        self._server: Any = None
        started = threading.Event()
        threading.Thread(target=self._run, args=(started,), daemon=True).start()
        started.wait(10)

    def _run(self, started: threading.Event) -> None:
        asyncio.set_event_loop(self._loop)
        pc = self

        class Server(asyncssh.SSHServer):
            def begin_auth(self, username: str) -> bool:
                return True

            def password_auth_supported(self) -> bool:
                return True

            def validate_password(self, username: str, password: str) -> bool:
                return pc.users.get(username) == password

        class Sftp(asyncssh.SFTPServer):
            def __init__(self, chan: Any) -> None:
                super().__init__(chan, chroot=str(pc.root).encode())

        async def start() -> None:
            self._server = await asyncssh.create_server(
                Server, "127.0.0.1", 0, server_host_keys=[self.key],
                process_factory=self._handle, sftp_factory=Sftp,
            )  # fmt: skip
            self.port = self._server.sockets[0].getsockname()[1]
            started.set()

        self._loop.run_until_complete(start())
        self._loop.run_forever()

    def new_key(self) -> None:
        """The PC was reinstalled: a different host key from now on."""
        self.key = asyncssh.generate_private_key("ssh-ed25519")
        self._server.update(server_host_keys=[self.key])

    def close(self) -> None:
        self._loop.call_soon_threadsafe(self._server.close)
        self._loop.call_soon_threadsafe(self._loop.stop)

    def at(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    async def _handle(self, process: asyncssh.SSHServerProcess[str]) -> None:
        cmd = str(process.command or "")
        self.commands.append(cmd)
        user = process.get_extra_info("username")
        sudo = cmd.startswith("sudo -S -p '' ")
        if sudo:
            given = (await process.stdin.readline()).rstrip("\n")
            self.stdin_seen.append(given)
            if user not in self.sudoers or self.users.get(user) != given:
                process.stderr.write("Sorry, user is not allowed to execute this command.\n")
                process.exit(1)
                return
            cmd = cmd[len("sudo -S -p '' ") :]
        out, err, code = self._answer(cmd)
        process.stdout.write(out)
        process.stderr.write(err)
        process.exit(code)

    def _answer(self, cmd: str) -> tuple[str, str, int]:
        if cmd == "uname -m":
            return self.arch + "\n", "", 0
        if cmd.startswith(". /etc/os-release"):
            return self.os_id + "\n", "", 0
        if cmd.startswith("df -Pk"):
            line = f"/dev/sda2 100000000 1000 {self.free_kb} 1% /\n"
            return line * 2, "", 0
        if cmd == "systemctl is-active zeroloss-agent":
            return "inactive\n", "", 3
        if cmd == "ss -ltnH":
            return self.listening, "", 0
        if cmd == "true":
            return "", "", 0
        if cmd.startswith("mkdir -p "):
            self.at(cmd.split()[-1]).mkdir(parents=True, exist_ok=True)
            return "", "", 0
        if cmd.startswith("rm -rf "):
            shutil.rmtree(self.at(cmd.split()[-1]), ignore_errors=True)
            return "", "", 0
        if cmd.startswith("dpkg -i "):
            deb = shlex.split(cmd)[-1]
            if self.locked_times:
                self.locked_times -= 1
                return "", "dpkg: error: dpkg frontend lock was locked by another process\n", 2
            if self.dpkg_error:
                return "", self.dpkg_error + "\n", 1
            if not self.at(deb).is_file():
                return "", f"dpkg: error: cannot access archive '{deb}'\n", 2
            self.on_dpkg()
            return "Setting up zeroloss-agent ...\n", "", 0
        if cmd.startswith("sh -c ") and "apt-get" in cmd:  # 06 11.3 4-3 tool install
            self.apt_runs.append(cmd)
            words = shlex.split(shlex.split(cmd)[2].rsplit("--no-install-recommends", 1)[1])
            if not self.online:
                return "", "E: Unable to locate package ffmpeg (no network)\n", 100
            for w in words:
                if w.endswith(".deb"):
                    if not self.at(w).is_file():
                        return "", f"E: Unsupported file {w}\n", 100
                    if "tsduck" in w:
                        self.tools.add("tsp")
                elif w == "ffmpeg":
                    self.tools |= {"ffmpeg", "ffprobe"}
                elif w in ("mpv", "vlc"):
                    self.tools.add(w)
            return "Setting up ...\n", "", 0
        if cmd.startswith("sh -c ") and "current.deb" in cmd:  # the rollback copy (06 11.5)
            deb = shlex.split(cmd)[-1]
            if not self.at(deb).is_file():
                return "", f"install: cannot stat '{deb}'\n", 1
            kept = self.at("/var/lib/zeroloss/deployer/packages/current.deb")
            kept.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.at(deb), kept)
            return "", "", 0
        if cmd.startswith("/opt/zeroloss/deployer/zeroloss-deployer auto-login --user "):
            user = shlex.split(cmd)[-1]
            if self.no_gdm:
                return (
                    '{"error": "/etc/gdm3/custom.conf not found (only GDM is supported)"}\n',
                    "",
                    3,
                )
            self.auto_login_user = user
            return f'{{"user": "{user}", "changed": true}}\n', "", 0
        if cmd == "dpkg -r zeroloss-agent":
            return "Removing zeroloss-agent ...\n", "", 0
        if cmd.startswith("command -v "):
            missing = [x.split("||")[1].split()[1] for x in cmd.split(";")
                       if x.split()[2] not in self.tools]  # fmt: skip
            return "".join(m + "\n" for m in missing), "", 0
        return "", f"sh: 1: {cmd.split()[0]}: not found\n", 127
