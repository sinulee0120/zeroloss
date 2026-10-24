"""Deployer service, TCP 10991 (docs/02-data.md 15, docs/06-install.md 11-12).

L0: Python standard library only, no Core or module code (12 1-1), so it keeps working
when the node is broken. One install or removal at a time (INS-027); a repeated
``command_id`` gets the first answer again (02 3). It watches the node on this PC every
second and restarts it after 10 s without an answer (SYS-009), leaving a note the new
node reports as its first event. On start it rolls back an operation a crash cut
short (INS-013) and carries an open node update to its end (``node_update``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from collections import OrderedDict
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent.deployer.agent_link import AgentLink
from agent.deployer.errors import DeployError, damaged
from agent.deployer.manifest import utc_now
from agent.deployer.media_import import EVERY_S as MEDIA_EVERY_S
from agent.deployer.media_import import MediaImport
from agent.deployer.modules import Layout, ModuleStore, this_os
from agent.deployer.node_update import Launcher, NodeUpdate
from agent.deployer.package import PAYLOAD, open_package
from agent.deployer.update_hold import UpdateHold

API = 1
PORT = 10991
WATCH_S = 10.0  # SYS-009: no answer for this long -> restart
POLL_S = 1.0
MAX_PACKAGE = 2 * 1024**3
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(zlm|zlp|zlt)$")
RESTART_NOTE = "restart-note.json"  # read and removed by the node at start (SYS-009)
NODE_STOPPED = "node-stopped.json"  # F11 Shut Down All: the watchdog waits
APK_NAME = "zeroloss-receiver.apk"  # the node installs the tablets' app from data/tablet
TABLET_TOOLS = Path("/opt/zeroloss/tools/platform-tools")  # the node's adb (tool_locator)


def _node_owns(path: Path) -> None:
    """A copy or folder the Deployer makes for the node is the node account's (06 4.3)."""
    with contextlib.suppress(LookupError, OSError):
        shutil.chown(path, "zeroloss", "zeroloss")


def read_build(root: Path | None = None) -> str:
    base = root or Path(__file__).resolve().parents[2]
    try:
        return (base / "VERSION").read_text(encoding="ascii").strip() or "0.0"
    except OSError:
        return "0.0"


class Deployer:
    def __init__(
        self,
        layout: Layout,
        agent: AgentLink,
        host: str = "0.0.0.0",
        port: int = PORT,
        build: str | None = None,
        watch: bool = True,
        watch_s: float = WATCH_S,
        poll_s: float = POLL_S,
        os_name: str | None = None,
        owner: str | None = None,
        launcher: Launcher | None = None,
        apt_dir: Path | None = None,  # the installed service passes APT_DIR (SYS-005)
        media_root: Path | None = None,  # the installed service passes "/" (GAP-109)
    ) -> None:
        self.layout = layout
        self.agent = agent
        self.store = ModuleStore(layout, agent, os_name, owner)
        self.build = build or read_build()
        self.update_hold = UpdateHold(  # SYS-005 (Linux): apt and snaps
            apt_dir if watch else None, layout.deployer if watch and apt_dir else None
        )
        self.media = (  # 12 1 media_import: files the modules ask for from users' folders
            MediaImport(media_root, layout.data_dir, layout.modules_dir, _node_owns, self.log)
            if media_root is not None and watch
            else None
        )
        self._media_at = float("-inf")
        self.updates = NodeUpdate(layout, agent, self.build, launcher or Launcher(), self.log)
        self.boot_id = str(uuid.uuid4())
        self.watch_s, self.poll_s, self.watching = watch_s, poll_s, watch
        self.restarts = 0
        self._seen: dict[str, Any] | None = None
        self._op = threading.Lock()
        self._done: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._done_lock = threading.Lock()
        self._halt = threading.Event()
        self._watch: threading.Thread | None = None
        self._server = ThreadingHTTPServer((host, port), _handler(self))
        self._server.daemon_threads = True
        self._open: set[socket.socket] = set()
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    # ---------------------------------------------------------------- life

    def start(self) -> None:
        for folder in (
            self.layout.incoming,
            self.layout.rollback,
            self.layout.deployer / "manifests",
            self.updates.folder,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        for part in self.layout.incoming.glob("*.part"):
            part.unlink(missing_ok=True)  # a transfer a crash cut short
        note = self.store.recover()
        self.update_hold.clear_leftover()
        self.log("info", f"Deployer {self.build} started" + (f"; {note}" if note else ""))
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
            name="deployer-api", daemon=True,
        )  # fmt: skip
        self._thread.start()
        if self.updates.open:
            self._resume_update()
        if self.watching:
            self._watch = threading.Thread(
                target=self._watch_loop, name="deployer-watch", daemon=True
            )
            self._watch.start()

    def stop(self) -> None:
        self._halt.set()
        self.updates.halt()
        if self._watch is not None:
            self._watch.join(5)
        self._server.shutdown()
        self._server.server_close()
        for conn in list(self._open):  # a stopped Deployer never answers on a kept connection
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)

    def log(self, level: str, text: str) -> None:
        path = self.layout.deployer / "deployer.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{utc_now()} {level} {text}\n")

    # ---------------------------------------------------------------- watch (SYS-009)

    def _watch_loop(self) -> None:
        last_ok = time.monotonic()
        while not self._halt.wait(self.poll_s):
            if self._op.locked() or self.node_stopped or self.updates.open:  # our own
                last_ok = time.monotonic()  # restarts, stopped on purpose, an update
                continue
            self._seen = self.agent.health(timeout_s=min(2.0, self.poll_s * 2))
            self._hold_updates()
            self._import_media()
            now = time.monotonic()
            if self._seen is not None:
                last_ok = now
            elif now - last_ok >= self.watch_s:
                self.restarts += 1
                why = f"not answering for {self.watch_s:g} s"
                note = {"utc": utc_now(), "reason": why, "restarts": self.restarts}
                (self.layout.data_dir / RESTART_NOTE).write_text(json.dumps(note), encoding="utf-8")
                self.log("warn", f"ZL-SYS-009 node {why}; restarting ({self.restarts})")
                try:
                    self.agent.service.restart()
                except Exception as e:  # the next round tries again
                    self.log("error", f"ZL-SYS-009 restart failed: {e!r}")
                last_ok = time.monotonic()  # give the new node the same time to answer

    def _hold_updates(self) -> None:
        """SYS-005: automatic updates off while the node is in a test; logged on change."""
        h = self.update_hold
        was = (h.held, h.error)
        h.seen((self._seen or {}).get("state") if self._seen else None)
        if (h.held, h.error) != was:
            text = h.error or ("automatic updates held for the test" if h.held
                               else "automatic updates back on")  # fmt: skip
            self.log("warn" if h.error else "info", f"ZL-SYS-005 {text}")

    def _import_media(self) -> None:
        """12 1 media_import: only while the node is idle (never during a test), every
        EVERY_S; a failure is logged and tried again next time."""
        if self.media is None or (self._seen or {}).get("state") != "IDLE":
            return
        now = time.monotonic()
        if now - self._media_at < MEDIA_EVERY_S:
            return
        self._media_at = now
        try:
            for line in self.media.run():
                self.log("info", f"media_import {line}")
        except Exception as e:  # a home folder that cannot be read must not stop the watch
            self.log("error", f"media_import failed: {e!r}")

    # ---------------------------------------------------------------- API

    def health(self) -> dict[str, Any]:
        seen = self._seen if self.watching else self.agent.health()
        return {
            "build": self.build,
            "deployer_api": API,
            "os": this_os(),
            "boot_id": self.boot_id,
            "busy": self._op.locked() or self.updates.open,
            "node_stopped": self.node_stopped,
            "update": self.updates.record(),
            "updates_held": self.update_hold.held,
            "agent": {
                "responding": seen is not None,
                "build": (seen or {}).get("build"),
                "state": (seen or {}).get("state"),
                "restarts": self.restarts,
            },
        }

    @property
    def _stopped_mark(self) -> Path:
        return self.layout.deployer / NODE_STOPPED

    @property
    def node_stopped(self) -> bool:
        """The Controller stopped the node on purpose (F11 Shut Down All): the watchdog
        leaves it alone until ``node start``. Kept on disk across Deployer restarts."""
        return self._stopped_mark.exists()

    def node(self, params: dict[str, Any]) -> dict[str, Any]:
        """02 15 ``POST /v1/deployer/node``: restart (F11 [Restart Agent]), stop (F11
        [Shut Down All]) or start the node service on this PC. Never during a test."""
        action = params.get("action")
        if action not in ("restart", "stop", "start"):
            raise DeployError("DSC-012", "Request rejected", detail="action: restart/stop/start")
        with self._one_at_a_time():
            if action != "start" and self.agent.busy():
                raise DeployError(
                    "INS-022", "A test is running on this PC",
                    "Restarting or stopping the node now would break it.",
                    "Stop the test first.",
                )  # fmt: skip
            try:
                if action == "stop":
                    self._stopped_mark.write_text(json.dumps({"utc": utc_now()}), encoding="utf-8")
                    self.agent.service.stop()
                    ok = self.agent.wait_gone()
                elif action == "start":
                    self._stopped_mark.unlink(missing_ok=True)
                    self.agent.service.start()
                    ok = self.agent.wait_answer()
                else:
                    ok = self.agent.restart()
            finally:
                self._seen = self.agent.health()
        self.log("info" if ok else "error", f"node {action}: {'done' if ok else 'not confirmed'}")
        if not ok:
            raise DeployError(
                "SYS-006" if action == "stop" else "INS-014",
                f"The node did not {'stop' if action == 'stop' else 'answer'} in time",
                f"{action} was asked; it was not confirmed in {self.agent.come_back_s:g} s.",
                "Restart the PC.",
            )  # fmt: skip
        return {"action": action, "done": True, "node": self._seen}

    def receive(self, name: str, length: int, sha: str, stream: Any) -> dict[str, Any]:
        if not NAME.match(name):
            raise DeployError("DSC-012", "Request rejected", detail="package file name")
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            raise DeployError("DSC-012", "Request rejected", detail="X-ZL-SHA256 header")
        if not 0 < length <= MAX_PACKAGE:
            raise DeployError("DSC-012", "Request rejected", detail="package size")
        dest = self.layout.incoming / name
        part = dest.with_name(name + ".part")
        digest = hashlib.sha256()
        left = length
        with part.open("wb") as out:
            while left:
                block = stream.read(min(left, 1 << 20))
                if not block:
                    break
                digest.update(block)
                out.write(block)
                left -= len(block)
        if left or digest.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise damaged("the received file does not match X-ZL-SHA256" if not left
                          else "the transfer ended early")  # fmt: skip
        part.replace(dest)
        self.log("info", f"received {name} ({length} bytes)")
        return {"name": name, "bytes": length, "sha256": sha}

    def install(self, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("package", ""))
        path = self.layout.incoming / name
        if not NAME.match(name) or not path.is_file():
            raise DeployError("DSC-012", "Request rejected", detail=f"no received package {name!r}")
        with self._one_at_a_time():
            try:
                pkg = open_package(path)
            except DeployError:
                path.unlink(missing_ok=True)
                raise
            if pkg.kind == "node":
                return self._update_node(pkg)
            if pkg.kind == "tools":
                return self._install_tools(pkg)
            if pkg.kind == "tablet":
                return self._install_tablet(pkg)
            try:
                result = self.store.install(pkg)
            finally:
                self._seen = self.agent.health()  # the node was restarted: answer as it is now
        self.log("info", f"install {pkg.id} {pkg.build}: {result['result']}")
        return result

    def _install_tablet(self, pkg: Any) -> dict[str, Any]:
        """The tablet app and Google's adb (01-overview D3, tools-manifest ``adb``): the
        app goes where the node installs it from (``data/tablet``), adb to
        ``/opt/zeroloss/tools/platform-tools`` (where the node looks for it). Each is put
        in whole and then swapped in, so a half copy is never used."""
        apk_dir = self.layout.data_dir / "tablet"
        tools = TABLET_TOOLS if self.layout.data_dir == Path("/var/lib/zeroloss") else (
            self.layout.data_dir / "tools")  # fmt: skip
        try:
            if self.agent.busy():
                raise DeployError(
                    "INS-022",
                    "A test is running on this PC",
                    "The tablets' app is in use.",
                    "Stop the test first.",
                )
            new_tools = tools.parent / (tools.name + ".new")
            shutil.rmtree(new_tools, ignore_errors=True)
            apk_dir.mkdir(parents=True, exist_ok=True)
            _node_owns(apk_dir)
            got_apk = got_adb = False
            with zipfile.ZipFile(pkg.path) as zf:
                for name in pkg.files:
                    data = zf.read(PAYLOAD + name)
                    if name == APK_NAME:
                        tmp = apk_dir / (APK_NAME + ".new")
                        tmp.write_bytes(data)
                        _node_owns(tmp)
                        tmp.replace(apk_dir / APK_NAME)
                        got_apk = True
                    elif name.startswith("platform-tools/") and ".." not in name:
                        target = new_tools / name[len("platform-tools/") :]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                        target.chmod(0o755)
                        got_adb = got_adb or name == "platform-tools/adb"
            if not got_apk:
                raise DeployError("INS-006", "The tablet package has no app")
            if got_adb:
                old = tools.parent / (tools.name + ".old")
                shutil.rmtree(old, ignore_errors=True)
                tools.parent.mkdir(parents=True, exist_ok=True)
                if tools.exists():
                    tools.replace(old)
                new_tools.replace(tools)
                shutil.rmtree(old, ignore_errors=True)
            (apk_dir / "build.txt").write_text(pkg.build, encoding="ascii")
        finally:
            pkg.path.unlink(missing_ok=True)
        self.log("info", f"tablet {pkg.build}: app and {'adb' if got_adb else 'no adb'} in place")
        return {"id": pkg.id, "build": pkg.build, "result": "installed", "adb": got_adb}

    def _install_tools(self, pkg: Any) -> dict[str, Any]:
        """Tools a module needs (e.g. iperf3), as Ubuntu .deb files in the package, put in
        with apt from those files only -- no network, no password (2026-10-16 user: the
        program installs what it needs by itself; PCs without internet). Never during a
        test. What apt did is logged; the tools stay (system packages)."""
        try:
            if self.agent.busy():
                raise DeployError(
                    "INS-022", "A test is running on this PC",
                    "Installing tools now could disturb the test.", "Stop the test first.",
                )  # fmt: skip
            work = self.layout.deployer / "tools"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            debs = []
            with zipfile.ZipFile(pkg.path) as zf:
                for name in pkg.files:  # payload-relative names
                    if name.endswith(".deb") and "/" not in name:
                        target = work / name
                        target.write_bytes(zf.read(PAYLOAD + name))
                        debs.append(target)
            if not debs:
                raise DeployError("INS-006", "The tools package has no .deb files")
            argv = ["apt-get", "install", "-y", "-q", "--no-install-recommends",
                    "-o", "DPkg::Lock::Timeout=600", *(str(d) for d in sorted(debs))]  # fmt: skip
            env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
            done = subprocess.run(argv, capture_output=True, text=True, timeout=900, env=env)
            tail = (done.stdout + done.stderr).strip().splitlines()[-3:]
            if done.returncode != 0:
                raise DeployError("INS-013", f"Installing {pkg.id} failed", " | ".join(tail))
        finally:
            pkg.path.unlink(missing_ok=True)
            shutil.rmtree(self.layout.deployer / "tools", ignore_errors=True)
        self.log("info", f"tools {pkg.id} {pkg.build}: installed {len(debs)} packages")
        return {"id": pkg.id, "build": pkg.build, "result": "installed", "debs": len(debs)}

    def _update_node(self, pkg: Any) -> dict[str, Any]:
        """06 11.5 Linux: the install runs as its own systemd job; answer at once."""
        try:
            if self.agent.busy():
                raise DeployError(
                    "INS-022", "A test is running on this PC",
                    "Updating the node now would break the test.", "Stop the test first.",
                )  # fmt: skip
            result = self.updates.start(pkg)
        finally:
            pkg.path.unlink(missing_ok=True)  # 11.5 5, 9: the .deb was taken out or refused
        self._resume_update()
        return result

    def _resume_update(self) -> None:
        def run() -> None:
            try:
                note = self.updates.resume()
            except Exception as e:  # never kill the Deployer; the record says what is left
                self.log("error", f"ZL-INS-013 node update: {e!r}")
                return
            if note:
                self.log("info", note)
            self._seen = self.agent.health()

        threading.Thread(target=run, name="deployer-update", daemon=True).start()

    def remove(self, params: dict[str, Any]) -> dict[str, Any]:
        if params.get("kind", "module") != "module":
            raise DeployError("RUN-013", "Only modules can be removed over 10991 in this build")
        mid = str(params.get("id", ""))
        with self._one_at_a_time():
            try:
                result = self.store.remove(
                    mid, bool(params.get("purge")), bool(params.get("dry_run"))
                )
            finally:
                self._seen = self.agent.health()
            if self.media is not None and not params.get("dry_run"):  # what it brought (1-3)
                for left in self.media.remove(mid):
                    self.log("warn", f"media_import {left}")
        self.log("info", f"remove {mid}: {len(result['removed'])} items")
        return result

    @contextlib.contextmanager
    def _one_at_a_time(self) -> Iterator[None]:
        if self.updates.open or not self._op.acquire(blocking=False):
            raise DeployError(
                "INS-027", "Another install is in progress",
                "The device installs one package at a time.",
                "Wait until it finishes, then try again.",
            )  # fmt: skip
        try:
            yield
        finally:
            self._op.release()

    def remembered(self, command_id: str, fn: Any) -> dict[str, Any]:
        """02 3: the same command_id returns the earlier answer (never runs twice)."""
        if command_id:
            with self._done_lock:
                if command_id in self._done:
                    return self._done[command_id]
        answer = envelope(fn)
        if command_id:
            with self._done_lock:
                self._done[command_id] = answer
                while len(self._done) > 64:
                    self._done.popitem(last=False)
        return answer


def envelope(fn: Any) -> dict[str, Any]:
    try:
        return {"ok": True, "data": fn()}
    except DeployError as e:
        return {"ok": False, "error": e.to_dict()}
    except Exception as e:  # never a bare traceback over the wire
        err = DeployError("SYS-008", "Deployer internal error", detail=repr(e))
        return {"ok": False, "error": err.to_dict()}


def _handler(dep: Deployer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            dep._open.add(self.connection)

        def finish(self) -> None:
            dep._open.discard(self.connection)
            super().finish()

        def log_message(self, *_: Any) -> None:  # the Deployer has its own log
            return

        def _send(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            routes = {
                "/v1/deployer/health": dep.health,
                "/v1/deployer/modules": lambda: {"modules": dep.store.installed()},
            }
            if path not in routes:
                self._send(404, envelope(lambda: _no_path(path)))
                return
            self._send(200, envelope(routes[path]))

        def do_PUT(self) -> None:
            path = urlsplit(self.path).path
            prefix = "/v1/deployer/incoming/"
            if not path.startswith(prefix):
                self._send(404, envelope(lambda: _no_path(path)))
                return
            length = int(self.headers.get("Content-Length") or 0)
            sha = str(self.headers.get("X-ZL-SHA256") or "").lower()
            self._send(
                200, envelope(lambda: dep.receive(path[len(prefix) :], length, sha, self.rfile))
            )

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                params = body.get("params") or {}
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
            except (ValueError, AttributeError) as e:
                why = str(e)
                self._send(200, envelope(lambda: _bad(why)))
                return
            routes = {"/v1/deployer/install": dep.install, "/v1/deployer/remove": dep.remove,
                      "/v1/deployer/node": dep.node}  # fmt: skip
            if path not in routes:
                self._send(404, envelope(lambda: _no_path(path)))
                return
            fn = routes[path]
            self._send(200, dep.remembered(str(body.get("command_id") or ""), lambda: fn(params)))

    return Handler


def _no_path(path: str) -> dict[str, Any]:
    raise DeployError("DSC-012", "Request rejected", detail=f"no such path: {path}")


def _bad(detail: str) -> dict[str, Any]:
    raise DeployError("DSC-012", "Request rejected", "The request is not in the expected form.",
                      detail=detail)  # fmt: skip
