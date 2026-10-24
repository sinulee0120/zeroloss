"""Agent Desktop's side of the screen jobs (docs/03-features.md F20 구현 규칙, 02 2-12).

Once a second: read the node's job list, start the listed jobs that are not running,
stop the running ones that are no longer listed, and report every job back (state,
the second just taken, events). Bound to the node: when the node has not answered for
``LINK_S`` (or came back with a new boot ID) every job is stopped. A stop runs on its
own thread so the reporting goes on; the job reports STOPPING, then IDLE once.
No Qt and no module rules here: a job part comes from the module's ``desktop_entry``
(12 2) and gives ``start()``, ``stop()``, ``state``, ``change(config)`` and ``report()``
(its own fields for 02 2-12, e.g. the player's second). The window only shows ``status``.
"""

from __future__ import annotations

import shutil
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.desktop_jobs import LINK_S
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

POLL_S = 1.0
STATUS_EVERY = 1  # polls between status reads (the window's data)


@dataclass
class Running:
    id: str
    module: str
    kind: str
    part: Any  # the module's job part: start(), stop(), state, change(config), report()
    config: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, str]] = field(default_factory=list)
    seq: int = 0
    stopping: threading.Thread | None = None
    stopped: bool = False  # confirmed gone: report IDLE once more, then forget
    failed: str | None = None


class DesktopLink:
    def __init__(self, port: int, factories: dict[tuple[str, str], Callable[..., Any]],
                 work_dir: Path, clock: Callable[[], float] = time.monotonic,
                 desktop_id: str | None = None) -> None:  # fmt: skip
        self.port = port
        self.factories = factories  # (module, kind) -> make(config, work_dir, procs, on_event)
        self.work_dir = work_dir
        self.desktop_id = desktop_id or str(uuid.uuid4())
        self._clock = clock
        self._client = NodeClient("127.0.0.1", port, f"desktop-{self.desktop_id}", "node")
        self._jobs: dict[str, Running] = {}
        self._lock = threading.Lock()
        self._last_ok = clock()
        self._boot: str | None = None
        self.status: dict[str, Any] | None = None  # 02 2-12 status, None = node not answering
        self.node_ok = False
        self.refused: str | None = None  # another Desktop is connected (02 2-12)
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- life

    def start(self) -> None:
        # job folders a crashed Desktop left behind (no player of ours runs now)
        shutil.rmtree(self.work_dir / "jobs", ignore_errors=True)
        self._thread = threading.Thread(target=self._loop, name="desktop-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Quit: every player goes with the Desktop (it is bound to it, 12 4-3)."""
        self._halt.set()
        if self._thread is not None:
            self._thread.join(5)
        self._stop_all("Agent Desktop quit")
        self._wait_stops(35.0)
        self._client.close()

    def _loop(self) -> None:
        while not self._halt.wait(POLL_S):
            self.step()

    # ---------------------------------------------------------------- one round

    def step(self) -> None:
        now = self._clock()
        try:
            got = self._client.get("/v1/desktop/jobs", timeout_s=2.0, desktop_id=self.desktop_id)
            self.refused = None
        except ZlError as e:
            if e.code != "ZL-DSC-014":  # the node answered: another Desktop has it
                self.refused = e.what
                self._stop_all("another Agent Desktop is connected")
                return
            if now - self._last_ok >= LINK_S:
                self.node_ok, self.status = False, None
                self._stop_all("the node stopped answering")
            return
        self._last_ok, self.node_ok = now, True
        if self._boot not in (None, got.get("node_boot_id")):
            self._stop_all("the node restarted")  # never carry an old run over (G-13)
        self._boot = got.get("node_boot_id")
        wanted = {str(j["id"]): j for j in got.get("jobs") or []}
        self._reconcile(wanted)
        self._report()
        try:
            self.status = self._client.get("/v1/desktop/status", timeout_s=2.0)
        except ZlError:
            self.status = None

    def _reconcile(self, wanted: dict[str, dict[str, Any]]) -> None:
        with self._lock:
            for jid, j in wanted.items():
                run = self._jobs.get(jid)
                if run is None:
                    self._jobs[jid] = self._start(j)
                elif (j.get("config") or {}) != run.config and run.part is not None \
                        and run.stopping is None:  # fmt: skip
                    run.config = dict(j.get("config") or {})
                    try:
                        run.part.change(run.config)
                    except Exception as e:
                        run.events.append({"code": "ZL-SYS-008", "level": "warn",
                                           "text": f"{run.kind} change failed: {e!r}"})  # fmt: skip
            for jid, run in self._jobs.items():
                if jid not in wanted and run.stopping is None and not run.stopped:
                    self._begin_stop(run)

    def _start(self, j: dict[str, Any]) -> Running:
        key = (str(j.get("module")), str(j.get("kind")))
        run = Running(str(j["id"]), key[0], key[1], None, dict(j.get("config") or {}))

        def on_event(code: str, level: str, text: str) -> None:
            with self._lock:
                run.events.append({"code": code, "level": level, "text": text})

        make = self.factories.get(key)
        if make is None:
            run.failed = f"this Agent Desktop has no {key[1]} for module {key[0]}"
            run.events.append({"code": "ZL-RCV-019", "level": "error", "text": run.failed})
            return run
        folder = self._job_folder(run.id)
        try:
            run.part = make(dict(run.config), folder, self.work_dir / "procs", on_event)
            run.part.start()
        except Exception as e:
            run.failed = f"{key[1]} did not start: {e!r}"
            run.events.append({"code": "ZL-RCV-019", "level": "error", "text": run.failed})
        return run

    def _job_folder(self, job_id: str) -> Path:
        return self.work_dir / "jobs" / job_id.replace("/", "_")

    def _begin_stop(self, run: Running) -> None:
        def stop() -> None:
            try:
                if run.part is not None and run.part.state in ("STARTING", "RUNNING", "FAULT"):
                    run.part.stop()
            except Exception as e:
                run.failed = f"stop failed: {e!r}"
            if run.part is None or run.part.state == "IDLE":  # its pictures and logs go too
                shutil.rmtree(self._job_folder(run.id), ignore_errors=True)  # (G-11)
            run.stopped = True

        run.stopping = threading.Thread(target=stop, name=f"stop-{run.kind}", daemon=True)
        run.stopping.start()

    def _stop_all(self, why: str) -> None:
        with self._lock:
            for run in self._jobs.values():
                if run.stopping is None and not run.stopped:
                    self._begin_stop(run)
        self._boot = None

    def _wait_stops(self, limit_s: float) -> None:
        end = time.monotonic() + limit_s
        for run in list(self._jobs.values()):
            if run.stopping is not None:
                run.stopping.join(max(0.0, end - time.monotonic()))

    def _report(self) -> None:
        reports, done = [], []
        with self._lock:
            for jid, run in self._jobs.items():
                reports.append(self._one(run))
                run.events = []
                if run.stopped:
                    done.append(jid)
        try:
            self._client.command("/v1/desktop/report", 0, timeout_s=2.0,
                                 desktop_id=self.desktop_id, reports=reports)  # fmt: skip
        except ZlError:
            return  # sent again next round; the node's LINK_S decides
        with self._lock:
            for jid in done:
                self._jobs.pop(jid, None)

    def _one(self, run: Running) -> dict[str, Any]:
        base: dict[str, Any] = {"id": run.id, "events": list(run.events)}
        if run.stopped:
            state = "FAULT" if run.failed and run.part is not None else "IDLE"
            return {**base, "state": state}
        if run.part is None or (run.failed and run.stopping is None):
            return {**base, "state": "FAULT"}
        if run.stopping is not None:
            return {**base, "state": "STOPPING"}
        state = str(run.part.state)
        if state not in ("STARTING", "RUNNING"):
            return {**base, "state": state}
        run.seq += 1
        extra = {k: v for k, v in dict(run.part.report()).items() if k not in base}
        return {**base, **extra, "state": state, "seq": run.seq}

    def tell(self, code: str, level: str, text: str, clears: list[str] | None = None) -> None:
        """A Desktop-wide happening (e.g. the screen locked, RCV-006) goes with every
        running job's next report."""
        e: dict[str, Any] = {"code": code, "level": level, "text": text}
        if clears:
            e["clears"] = clears
        with self._lock:
            for run in self._jobs.values():
                if not run.stopped and run.stopping is None:
                    run.events.append(dict(e))

    @property
    def running_jobs(self) -> list[str]:
        with self._lock:
            return [j for j, r in self._jobs.items() if not r.stopped]
