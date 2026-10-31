"""Screen-side copy of the Controller state (docs/11-ui.md 1, 1.12).

The real state lives in the Controller service. ``Feed`` (a worker thread) reads it from
127.0.0.1:10994 and ``Commands`` (another worker) sends Start/Stop; both only put
messages on one queue. The screen thread drains that queue into ``Store``, the single
place the screen draws from, and redraws only when something changed. No network work
runs on the screen thread.

P2: the Feed reads the status once a second; switching it to the 10994 event stream is
P3 (11-ui 1.10 "P2 임시").
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from controller.ui.control_state import Ctl, control_states
from controller.ui.texts import TEXT
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

P = "/v1/controller"
READ = "GET "  # Commands.read: a path sent as a GET
# ("status", Status) | ("done", Done) | ("sent", name) | ("setup", answer of 02 2-11)
# | ("schema", settings of 02 2-11) | ("edit", changes) | ("idlecheck", (key, checklist))
Message = tuple[str, Any]


@dataclass(frozen=True)
class Status:
    connected: bool
    build: str = ""
    run: dict[str, Any] = field(default_factory=dict)
    devices: tuple[dict[str, Any], ...] = ()
    error: str = ""
    reports_dir: str = ""
    problems: dict[str, Any] = field(default_factory=dict)  # 02 2-2
    actions: dict[str, Any] = field(default_factory=dict)  # 02 2-3
    storage_low: tuple[dict[str, Any], ...] = ()  # 02 2-1, 09 SYS-010
    broadcast: dict[str, Any] = field(default_factory=dict)  # 02 2-1 (F18)


@dataclass(frozen=True)
class Done:
    """Outcome of one screen command, shown in the Status line or a dialog."""

    name: str
    ok: bool
    data: dict[str, Any]
    error: dict[str, str] | None = None


# Not in this build (16-settings 3): shown with the value in use, never changeable.
UNAVAILABLE = {
    "send_reports": "Always on in this build (Off comes in P3)",
    "snapshot": "Snapshot comes with the player (P3)",
    "device_manage": "Use 'zeroloss devices add' in this build",
}
SETUP_EVERY_S = 10.0  # device sides may change (a cable moved, a PC came up)


def _client(port: int) -> NodeClient:  # one per worker thread
    return NodeClient("127.0.0.1", port, f"desktop-{uuid.uuid4()}", "Controller service")


class Feed:
    """Reads the Controller status every ``interval_s``; ``poke()`` reads at once."""

    def __init__(self, port: int, out: queue.Queue[Message], interval_s: float = 1.0) -> None:
        self.port = port
        self.out = out
        self.interval_s = interval_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="ui-feed", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def poke(self) -> None:
        self._wake.set()

    def stop(self, wait_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=wait_s)  # 0: a closing window never waits (daemon)

    def _loop(self) -> None:
        client = _client(self.port)
        try:
            while not self._stop.is_set():
                self.out.put(("status", self._read(client)))
                self._wake.wait(self.interval_s)
                self._wake.clear()
        finally:
            client.close()

    @staticmethod
    def _read(client: NodeClient) -> Status:
        try:
            s = client.get(f"{P}/status", timeout_s=2.0)
            d = client.get(f"{P}/devices", timeout_s=2.0)
            pr = client.get(f"{P}/problems", timeout_s=2.0)
            ac = client.get(f"{P}/actions", timeout_s=2.0)
        except ZlError as e:
            return Status(False, error=f"{e.code}: {e.what}")
        except Exception as e:  # a screen must never die from a bad answer
            return Status(False, error=repr(e))
        return Status(
            True,
            str(s.get("build", "")),
            dict(s["run"]),
            tuple(d["devices"]),
            reports_dir=str(s.get("reports_dir", "")),
            problems=dict(pr),
            actions=dict(ac),
            storage_low=tuple(s.get("storage_low") or ()),
            broadcast=dict(s.get("broadcast") or {}),
        )


IDLE_CHECK_S = 30.0  # 03 F17: light check every 30 s and when the plan or devices change
IDLE_CHECK_TIMEOUT_S = 60.0


class IdleCheck:
    """F17 light check (steps 1-4, no traffic) off the screen thread: every
    ``interval_s`` and at once after ``want()``. Puts ("idlecheck", (key, checklist));
    the store keeps it only while ``key`` is still the current plan + devices."""

    def __init__(
        self, port: int, out: queue.Queue[Message], interval_s: float = IDLE_CHECK_S
    ) -> None:
        self.port = port
        self.out = out
        self.interval_s = interval_s
        self._want: tuple[str, dict[str, Any]] | None = None
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="ui-idlecheck", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, wait_s: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=wait_s)  # 0: a closing window never waits (daemon)

    def want(self, key: str, plan: dict[str, Any] | None) -> None:
        with self._lock:
            self._want = (key, dict(plan)) if plan is not None else None
        self._wake.set()

    def _loop(self) -> None:
        client = _client(self.port)
        try:
            while not self._stop.is_set():
                self._wake.wait(self.interval_s)
                self._wake.clear()
                with self._lock:
                    want = self._want
                if want is None or self._stop.is_set():
                    continue
                key, plan = want
                try:
                    gen = int(client.get(f"{P}/status").get("generation", 0))
                    got = client.command(f"{P}/precheck", gen, timeout_s=IDLE_CHECK_TIMEOUT_S,
                                         plan=plan, full=False)  # fmt: skip
                except ZlError:
                    continue  # a test is running or the service is away: nothing to say
                except Exception:  # a screen must never die from a bad answer
                    continue
                self.out.put(("idlecheck", (key, got)))
        finally:
            client.close()


def check_key(plan: dict[str, Any] | None, devices: tuple[dict[str, Any], ...]) -> str:
    """What a light check result belongs to: the plan and the device list."""
    names = sorted(str(d.get("name")) for d in devices)
    return json.dumps([plan, names], sort_keys=True, default=str)


class SetupFeed:
    """Reads the screen choice (02 2-11 ``GET setup``: roles need device answers, so off
    the screen thread) every ``interval_s`` and at once after ``poke()``; the settings
    screen data once. Puts ("setup", answer) and ("schema", settings)."""

    def __init__(
        self, port: int, out: queue.Queue[Message], interval_s: float = SETUP_EVERY_S
    ) -> None:
        self.port, self.out, self.interval_s = port, out, interval_s
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._idle = threading.Event()  # only between tests: no extra control traffic (07 7)
        self._idle.set()
        self._thread = threading.Thread(target=self._loop, name="ui-setup", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def poke(self) -> None:
        self._wake.set()

    def between_tests(self, idle: bool) -> None:
        """The screen thread says whether a test runs; asking devices waits meanwhile."""
        if idle and not self._idle.is_set():
            self._idle.set()
            self._wake.set()  # read again at once: roles may have changed
        elif not idle:
            self._idle.clear()

    def stop(self, wait_s: float = 5.0) -> None:
        self._stop.set()
        self._idle.set()
        self._wake.set()
        self._thread.join(timeout=wait_s)  # 0: a closing window never waits (daemon)

    def _loop(self) -> None:
        client = _client(self.port)
        schema_sent = False
        try:
            while not self._stop.is_set():
                self._idle.wait()
                if self._stop.is_set():
                    return
                try:
                    if not schema_sent:
                        self.out.put(("schema", client.get(f"{P}/settings")["settings"]))
                        schema_sent = True
                    self.out.put(("setup", client.get(f"{P}/setup", timeout_s=30.0)))
                except ZlError:
                    pass  # the status feed shows the service as away
                except Exception:  # a screen must never die from a bad answer
                    pass
                self._wake.wait(self.interval_s)
                self._wake.clear()
        finally:
            client.close()


LONG_JOBS = frozenset({"run", "install_linux"})  # answer after minutes: their own thread


class Commands:
    """Sends screen commands one at a time. A command already waiting or running is
    not queued again (double click, 11-ui 1.12). A Start answers only when it is done —
    up to hours while videos are made — so it goes on its own thread: a Cancel or Stop
    behind it in the line never arrived (2026-10-09 GAP-123, the real window)."""

    def __init__(self, port: int, out: queue.Queue[Message], after: Callable[[], None]) -> None:
        self.port = port
        self.out = out
        self.after = after  # e.g. Feed.poke: show the new state at once
        self._jobs: queue.Queue[tuple[str, str, dict[str, Any], float] | None] = queue.Queue()
        self._busy: set[str] = set()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="ui-commands", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, wait_s: float = 5.0) -> None:
        self._jobs.put(None)
        self._thread.join(timeout=wait_s)

    def send(self, job: str, path: str, /, timeout_s: float = 5.0, **params: Any) -> bool:
        name = job
        with self._lock:
            if name in self._busy:
                return False
            self._busy.add(name)
        if name in LONG_JOBS:
            threading.Thread(target=self._alone, args=((name, path, params, timeout_s),),
                             name=f"ui-{name}", daemon=True).start()  # fmt: skip
            return True
        self._jobs.put((name, path, params, timeout_s))
        return True

    def busy_prefix(self, prefix: str) -> bool:
        """A job whose name starts so is still on its way (e.g. "setup:" saves)."""
        with self._lock:
            return any(n.startswith(prefix) for n in self._busy)

    def read(self, job: str, path: str, /, timeout_s: float = 5.0, **query: Any) -> bool:
        """A GET off the screen thread (e.g. the storage list), one at a time per name."""
        return self.send(job, READ + path, timeout_s, **query)

    def busy(self, name: str) -> bool:
        with self._lock:
            return name in self._busy

    def _loop(self) -> None:
        client = _client(self.port)
        try:
            while (job := self._jobs.get()) is not None:
                self._one(client, job)
        finally:
            client.close()

    def _alone(self, job: tuple[str, str, dict[str, Any], float]) -> None:
        client = _client(self.port)
        try:
            self._one(client, job)
        finally:
            client.close()

    def _one(self, client: NodeClient, job: tuple[str, str, dict[str, Any], float]) -> None:
        name, path, params, timeout_s = job
        try:
            if path.startswith(READ):
                data = client.get(path[len(READ) :], timeout_s=timeout_s, **params)
            else:
                gen = int(client.get(f"{P}/status").get("generation", 0))
                data = client.command(path, gen, timeout_s=timeout_s, **params)
            done = Done(name, True, data)
        except ZlError as e:
            done = Done(name, False, {}, e.to_dict())
        except Exception as e:
            done = Done(name, False, {}, {"code": "ZL-SYS-008", "what": repr(e)})
        with self._lock:
            self._busy.discard(name)
        self.out.put(("done", done))
        self.after()


class Store:
    """The screen's single state. Only the screen thread calls ``apply``."""

    def __init__(self) -> None:
        self.status = Status(False, error="Connecting…")
        self.last_done: Done | None = None
        self.done_by: dict[str, Done] = {}  # the last answer of each job name
        self.setup: dict[str, Any] | None = None  # 02 2-11 answer: choice, sides, roles, plan
        self.setup_at = 0.0  # when the last setup answer came (monotonic)
        self.schema: dict[str, dict[str, Any]] = {}  # settings screen data (16 1)
        self.pending: frozenset[str] = frozenset()  # commands sent, answer not back yet
        self._finished: dict[str, Any] | None = None  # a result to show once (F15)
        self._known_result: str | None = None
        self._first_status = True
        self.idle_check: tuple[str, dict[str, Any]] | None = None  # (check_key, checklist)
        self.version = 0  # bumps on every real change; the screen redraws on a new value

    def apply(self, msg: Message) -> bool:
        kind, value = msg
        if kind == "status":
            if value == self.status:
                return False
            self.status = value
            self._note_result(value)
        elif kind == "sent":
            if value in self.pending:
                return False
            self.pending = self.pending | {value}
        elif kind == "done":
            self.last_done = value
            self.done_by[value.name] = value  # windows read their own job's answer
            self.pending = self.pending - {value.name}
        elif kind == "setup":
            self.setup_at = time.monotonic()  # read again, same or not (a tablet's Scan)
            if value == self.setup:
                return False
            self.setup = value
        elif kind == "schema":
            if value == self.schema:
                return False
            self.schema = dict(value)
        elif kind == "idlecheck":
            if value == self.idle_check:
                return False
            self.idle_check = value
        elif kind == "edit":  # a screen change on its way to the service: shown at once
            if self.setup is None or self.run_state != "IDLE":  # decided when handled (1.12)
                return False
            choice = {**self.setup["setup"], **value}
            if choice == self.setup["setup"]:
                return False
            # 2026-10-31 user ("버퍼 선택하면 또 start가 눌렸다가 회색으로 변하고 다시 파란색으로
            # 돌아오네"): the plan is kept while the change is saved (Start waits for the save,
            # MainWindow._start_when_saved), so Start no longer flickers grey on every pick
            self.setup = {**self.setup, "setup": choice}
        else:
            return False
        self.version += 1
        return True

    @property
    def run_state(self) -> str:
        return str(self.status.run.get("state", "IDLE")) if self.status.connected else "IDLE"

    def _note_result(self, s: Status) -> None:
        """F15: a run that finished while we watched is shown once. A result that is
        already there when the screen first connects is not new."""
        if not s.connected:
            return
        result = s.run.get("result")
        run_id = str(result.get("run_id")) if isinstance(result, dict) else None
        done = s.run.get("state") in ("IDLE", "FAULT")
        if self._first_status:
            self._first_status = False
            self._known_result = run_id
            return
        if done and run_id and run_id != self._known_result:
            self._known_result = run_id
            self._finished = dict(result) if isinstance(result, dict) else None

    def take_finished(self) -> dict[str, Any] | None:
        out, self._finished = self._finished, None
        return out

    @property
    def plan(self) -> dict[str, Any] | None:
        """The plan the saved choice makes (Start builds the same one in the service)."""
        return (self.setup or {}).get("plan")

    @property
    def choice(self) -> dict[str, Any]:
        return dict((self.setup or {}).get("setup") or {})

    @property
    def plan_ok(self) -> bool:
        return self.plan is not None

    @property
    def not_ready_why(self) -> str:
        """Why no plan yet (02 2-11 ``roles.why``), shown on the grey Start."""
        if self.setup is None:
            return TEXT["setup_loading"]
        if self.plan is None:
            return str((self.setup.get("roles") or {}).get("why") or TEXT["setup_saving"])
        return ""

    def setting(self, sid: str) -> Any:
        """The value in use: during a test the running plan's (a window opened then has no
        saved choice yet: it showed Generated while a movie was sent, GAP-126), else the
        one changed by the user, else the default (16 1)."""
        running = self.status.run.get("plan") if self.run_state != "IDLE" else None
        if isinstance(running, dict) and isinstance(running.get("settings"), dict):
            changed = running["settings"]
        else:
            changed = self.choice.get("settings") or {}
        return changed.get(sid, (self.schema.get(sid) or {}).get("value"))

    @property
    def check_key(self) -> str:
        return check_key(self.plan, self.status.devices)

    def current_idle_check(self) -> dict[str, Any] | None:
        """The light check of the plan and devices shown now (older ones are dropped)."""
        if self.idle_check is None or self.idle_check[0] != self.check_key:
            return None
        return self.idle_check[1]

    def precheck(self) -> tuple[bool, str]:
        """(Start allowed, why not) for 11-ui 1.10: no plan, or the current light check
        failed. No result yet counts as ready: Start runs the whole check anyway."""
        if self.plan is None:
            return False, self.not_ready_why
        c = self.current_idle_check()
        bad = next((s for s in (c or {}).get("steps") or [] if s.get("state") == "failed"), None)
        if bad is None:
            return True, ""
        return False, f"{bad['name']}: {bad.get('what') or 'failed'}"

    def screen_controls(self) -> dict[str, Ctl]:
        """What the main screen shows: the 1.10 table with this store's plan and check."""
        ready, why = self.precheck()
        return self.controls(ready, why or TEXT["setup_loading"])

    def controls(self, precheck_ok: bool, precheck_reason: str = "") -> dict[str, Ctl]:
        s = self.status
        has_report = bool(s.run.get("result"))
        return control_states(
            self.run_state,
            precheck_ok,
            has_report,
            s.connected,
            any_device_connected=bool(s.devices),
            precheck_reason=precheck_reason or "See the Status box for what to fix",
            unavailable=UNAVAILABLE,
            pending=self.pending,
            preparing=bool(s.run.get("preparing")),
            auto_channel=bool((s.run.get("channels") or {}).get("auto")),
            broadcast_on=bool(s.broadcast.get("on")),
            storage_low=TEXT["storage_low_tip"].format(pcs=", ".join(
                f"{x.get('device')} ({(x.get('free_mb') or 0) / 1024:.1f} GB free)"
                for x in s.storage_low)) if s.storage_low else "",
        )  # fmt: skip
