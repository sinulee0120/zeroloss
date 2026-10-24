"""Node package update on Linux (docs/06-install.md 11.2, 11.5 "Linux 구현 규칙").

The .deb holds both the node and the Deployer, and its ``prerm`` stops both services,
so the Deployer cannot run ``dpkg -i`` as its own child: it would die with the
service. It starts the install as a separate systemd job instead
(``systemd-run --unit=zeroloss-update-<id>``) that ends by starting both services
again, and the new Deployer picks the result up from ``update.json`` at start:
done (the node answers with the new build), or the previous .deb goes back
(``rolling_back`` -> ``rolled_back``), or ``failed``. A power cut mid-install is found
the same way (no job, no exit code: INS-013). ``Launcher`` is the only part that
touches the OS, so tests bring their own.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent.deployer.errors import DeployError, damaged
from agent.deployer.manifest import utc_now
from agent.deployer.modules import Agent, Layout
from agent.deployer.package import Package

DEB_NAME = "zeroloss-agent"
_BUILD = re.compile(r"^\d{8}\.\d{4}$")
EXIT_WAIT_S = 600.0  # the install job's exit code (06 11.5: at most 10 minutes)
NODE_WAIT_S = 60.0  # the node answers with the new build
LOCK_TRIES, LOCK_WAIT_S = 20, 30  # INS-011: 30 s apart, up to 10 minutes
SERVICES = "zeroloss-deployer.service zeroloss-agent.service"
LOCKED = "frontend lock|database is locked|lock-frontend"  # dpkg busy (INS-011), not a fault
FINAL = ("done", "rolled_back", "failed")


def job_script(tries: int = LOCK_TRIES, wait_s: float = LOCK_WAIT_S) -> str:
    """The install job: ``$1`` package, ``$2`` log, ``$3`` exit code file. Only fixed
    paths reach the command line (11.7: no command execution over 10991)."""
    return (
        'i=0; while :; do dpkg -i "$1" >"$2" 2>&1; c=$?; '
        f'[ $c -ne 0 ] && grep -qiE "{LOCKED}" "$2" && [ $i -lt {tries} ] || break; '
        f"i=$((i+1)); sleep {wait_s:g}; done; "
        f'echo $c >"$3.tmp"; mv "$3.tmp" "$3"; systemctl start {SERVICES}'
    )


class Launcher:
    """The OS side: read a .deb's fields, start the install job, see if it still runs."""

    def __init__(self, script: str | None = None) -> None:
        self.script = script or job_script()

    def fields(self, deb: Path) -> dict[str, str]:
        done = subprocess.run(["dpkg-deb", "-f", str(deb), "Package", "Version"],
                              capture_output=True, text=True, timeout=60, check=False)  # fmt: skip
        if done.returncode != 0:
            raise damaged(f"dpkg-deb cannot read it: {done.stderr.strip()[-200:]}")
        out = {}
        for line in done.stdout.splitlines():
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
        return out

    def launch(self, job_id: str, deb: Path, log: Path, status: Path) -> None:
        argv = ["systemd-run", f"--unit=zeroloss-update-{job_id}", "--collect", "--quiet", "--",
                "/bin/sh", "-c", self.script, "sh", str(deb), str(log), str(status)]  # fmt: skip
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False)
        if done.returncode != 0:
            raise DeployError(
                "INS-028", "The install could not be started",
                done.stderr.strip()[-200:] or "systemd-run failed.",
                "Install again over SSH.",
            )  # fmt: skip

    def running(self, job_id: str) -> bool:
        done = subprocess.run(["systemctl", "is-active", f"zeroloss-update-{job_id}.service"],
                              capture_output=True, text=True, timeout=30, check=False)  # fmt: skip
        return done.stdout.strip() in ("active", "activating", "deactivating")


class NodeUpdate:
    def __init__(self, layout: Layout, agent: Agent, build: str, launcher: Launcher,
                 log: Any, exit_wait_s: float = EXIT_WAIT_S,
                 node_wait_s: float = NODE_WAIT_S, poll_s: float = 1.0) -> None:  # fmt: skip
        self.layout, self.agent, self.build, self.launcher = layout, agent, build, launcher
        self.log = log
        self.exit_wait_s, self.node_wait_s, self.poll_s = exit_wait_s, node_wait_s, poll_s
        self._lock = threading.Lock()
        self._halt = threading.Event()

    def halt(self) -> None:
        """The Deployer stops: a waiting ``resume`` ends without writing anything, and
        the next Deployer carries the update on."""
        self._halt.set()

    # ---------------------------------------------------------------- files

    @property
    def folder(self) -> Path:
        return self.layout.deployer / "packages"

    @property
    def record_path(self) -> Path:
        return self.layout.deployer / "update.json"

    def _job(self, job_id: str, suffix: str) -> Path:
        return self.folder / f"update-{job_id}.{suffix}"

    def record(self) -> dict[str, Any] | None:
        try:
            doc = json.loads(self.record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return doc if isinstance(doc, dict) else None

    def _save(self, doc: dict[str, Any]) -> None:
        tmp = self.record_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        tmp.replace(self.record_path)

    @property
    def open(self) -> bool:
        """An update not finished yet: no other install until it is (INS-027)."""
        doc = self.record()
        return doc is not None and doc.get("state") not in FINAL

    # ---------------------------------------------------------------- start

    def start(self, pkg: Package) -> dict[str, Any]:
        """Check the .zlp, keep the previous .deb, start the install job, answer at once."""
        if not sys.platform.startswith("linux"):
            raise DeployError(
                "RUN-013", "Node updates over 10991 are Linux only in this build",
                "Windows nodes update with the Windows installer (06 11.5, V18).",
                "Install the new build on that PC by hand.",
            )  # fmt: skip
        debs = [n for n in pkg.files if n.endswith(".deb") and "/" not in n]
        if pkg.info.get("id") != "agent" or pkg.info.get("os") != "linux" or len(debs) != 1 \
                or len(pkg.files) != 1:  # fmt: skip
            raise damaged("a node package holds one .deb for id agent, os linux (02 13)")
        self.folder.mkdir(parents=True, exist_ok=True)
        new = self.folder / "new.deb"
        pkg.extract(debs[0], new)
        try:
            f = self.launcher.fields(new)
            if f.get("Package") != DEB_NAME or f.get("Version") != pkg.build:
                raise damaged(f"the .deb says {f.get('Package')} {f.get('Version')}, "
                              f"the package {DEB_NAME} {pkg.build}")  # fmt: skip
            if _BUILD.match(self.build) and pkg.build < self.build:
                raise DeployError(
                    "INS-026", f"A newer build {self.build} is installed",
                    f"The package is {pkg.build}; going back is not allowed.",
                    "Send the same or a newer build.", detail=f"{self.build} > {pkg.build}",
                )  # fmt: skip
        except DeployError:
            new.unlink(missing_ok=True)
            raise
        current, previous = self.folder / "current.deb", self.folder / "previous.deb"
        if current.is_file():
            shutil.copyfile(current, previous)
        else:
            previous.unlink(missing_ok=True)
        job_id = uuid.uuid4().hex[:12]
        doc = {
            "id": job_id, "job": job_id, "from_build": self.build, "to_build": pkg.build,
            "state": "running",
            "why": None, "started_utc": utc_now(), "ended_utc": None,
            "no_rollback": not previous.is_file(),
        }  # fmt: skip
        self._save(doc)
        try:
            self.launcher.launch(job_id, new, self._job(job_id, "log"), self._job(job_id, "exit"))
        except DeployError as e:
            self._clean()
            self._finish(doc, "failed", e.why)
            raise
        self.log("info", f"node update {job_id}: {self.build} -> {pkg.build} started")
        return {"operation": "install", "kind": "node", "result": "started", "update_id": job_id,
                "from_build": self.build, "to_build": pkg.build,
                "no_rollback": doc["no_rollback"]}  # fmt: skip

    # ---------------------------------------------------------------- resume

    def resume(self) -> str | None:
        """Carry an open update to its end: at Deployer start, and on the Deployer that
        started it (dpkg may fail before it stops the services). Returns a note or None.
        A Deployer the job stops mid-way leaves the rest to the next one."""
        with self._lock:
            note = None
            try:
                while (doc := self.record()) is not None and doc.get("state") not in FINAL:
                    note = self._step(doc)
            except _Halted:
                return None
            return note

    def _step(self, doc: dict[str, Any]) -> str:
        job = str(doc.get("id"))
        code, tail = self._wait_exit(str(doc.get("job") or job))
        want = doc["to_build"] if doc["state"] == "running" else doc["from_build"]
        node_ok = code == 0 and self._node_answers(want)
        why = _why(code, tail, want)
        if doc["state"] == "rolling_back":  # this Deployer is the previous build again (or not)
            first = doc.get("why") or "the install did not finish"
            if node_ok:
                self._clean()
                self._finish(doc, "rolled_back", first)
                return f"node update rolled back to {doc['from_build']}: {first}"
            self._clean()
            self._finish(doc, "failed", f"{first}; going back: {why}")
            return f"node update failed: {first}"
        if node_ok:
            shutil.move(str(self.folder / "new.deb"), str(self.folder / "current.deb"))
            self._clean()
            self._finish(doc, "done", None)
            return f"node update {job} done: {doc['to_build']}"
        previous = self.folder / "previous.deb"
        if not previous.is_file():
            self._clean()
            self._finish(doc, "failed", f"{why}; no previous package to go back to")
            return f"node update {job} failed: {why}"
        back = f"{job}b"
        doc["state"], doc["why"], doc["job"] = "rolling_back", why, back
        self._save(doc)
        self.log("warn", f"ZL-INS-028 node update {job}: {why}; going back")
        try:
            self.launcher.launch(back, previous, self._job(back, "log"), self._job(back, "exit"))
        except DeployError as e:
            self._clean()
            self._finish(doc, "failed", f"{why}; going back: {e.why}")
        return f"node update {job}: going back to {doc['from_build']}"

    def _wait_exit(self, job: str) -> tuple[int | None, str]:
        """The job's exit code; None when it is gone without one (power cut, INS-013)."""
        status, log = self._job(job, "exit"), self._job(job, "log")
        end = time.monotonic() + self.exit_wait_s
        while True:
            try:
                code: int | None = int(status.read_text(encoding="ascii").strip())
                break
            except (OSError, ValueError):
                code = None
            if self._halt.is_set():
                raise _Halted
            if time.monotonic() >= end or not self.launcher.running(job):
                # the job may have written the file just before it ended
                try:
                    code = int(status.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    code = None
                break
            self._halt.wait(self.poll_s)
        try:
            lines = [x for x in log.read_text(encoding="utf-8", errors="replace").splitlines()
                     if x.strip()]  # fmt: skip
        except OSError:
            lines = []
        return code, (lines[-1][-200:] if lines else "")

    def _node_answers(self, build: str) -> bool:
        end = time.monotonic() + self.node_wait_s
        while True:
            h = self.agent.health()
            if h is not None and h.get("build") == build:
                return True
            if self._halt.is_set():
                raise _Halted
            if time.monotonic() >= end:
                return False
            self._halt.wait(self.poll_s)

    def _finish(self, doc: dict[str, Any], state: str, why: str | None) -> None:
        doc["state"], doc["why"], doc["ended_utc"] = state, why, utc_now()
        self._save(doc)
        level = "info" if state == "done" else "error"
        self.log(level, f"node update {doc['id']}: {state}" + (f" ({why})" if why else ""))

    def _clean(self) -> None:
        """11.5 8: no package left behind (``current.deb`` is the installed one). Runs
        before the final state is written: a final state means nothing is left."""
        for p in self.folder.glob("*"):
            if p.name != "current.deb":
                p.unlink(missing_ok=True)
        for p in self.layout.incoming.glob("*.zlp"):
            p.unlink(missing_ok=True)


class _Halted(Exception):
    pass


def _why(code: int | None, tail: str, build: str) -> str:
    if code is None:
        return "the install stopped part way (power cut or the job was killed)"
    if code != 0:
        return f"dpkg -i ended with {code}" + (f": {tail}" if tail else "")
    return f"the node did not answer with build {build} in time"
