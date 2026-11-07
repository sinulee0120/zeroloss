"""Run controller: one test from plan to report (docs/03-features.md F10, F11, F17).

State flow (F11, decided 2026-09-24): IDLE -> PREPARING -> RUNNING -> STOPPING ->
REPORTING -> IDLE; a lost device goes RUNNING -> SAFE_STOP -> REPORTING -> FAULT;
FAULT -> RESETTING -> IDLE. A failed start rolls back PREPARING -> STOPPING -> IDLE
without a report (F17). Commands outside the flow are refused (RUN-012).

Test-type rules come only from the module's ``run_entry``; this file never names a
test type. Summaries are written to disk the moment they arrive (F10), so a crash of
the Controller loses nothing the receivers already sent.
"""

from __future__ import annotations

import contextlib
import dataclasses
import ipaddress
import json
import statistics
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from controller.baselines import BaselineStore
from controller.devices import Device, DeviceList
from controller.discover import same_network
from controller.judge import in_plan, judge_run
from controller.links import LinkJudge, precheck
from controller.precheck import (
    CLOCK_WARN_S,
    DISK_WARN_MB,
    IDLE_STEPS,
    PRECHECK_STEPS,
    STEPS,
    Checklist,
    DeviceError,
    clock_offset_s,
    controller_route,
    is_wireless,
    most_hours,
    other_network,
    own_address,
    test_card,
)
from controller.precheck import Step as PrecheckStep
from controller.report import write_report
from controller.router_logger import RouterLogger
from controller.wan_route import WAN_B, ensure_wan_route, manual_fix
from controller.wifi_store import WifiConfig
from core.blocks.link_monitor import nic_of, read_links
from core.blocks.route_guard import default_route_nic
from core.blocks.unicode_path import display_name
from core.events.bus import Event, EventBus, Level, utc_now
from core.modules.registry import load_run_entry
from core.protocol.client import HEARTBEAT_TIMEOUT_S, NodeClient
from core.protocol.envelope import PROTOCOL_VERSION, ZlError
from core.records.jsonl import read_jsonl
from core.settings import CORE_DEFAULTS

BASELINE = "Baseline (no router)"  # S-54 (05 4.6.3)
SUSPECT_S = float(CORE_DEFAULTS["S-42"]["suspect_s"])
LOST_S = float(CORE_DEFAULTS["S-42"]["lost_s"])


class RunState(StrEnum):
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    REPORTING = "REPORTING"
    SAFE_STOP = "SAFE_STOP"
    FAULT = "FAULT"
    RESETTING = "RESETTING"


ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.IDLE: frozenset({RunState.PREPARING, RunState.RESETTING}),
    RunState.PREPARING: frozenset({RunState.RUNNING, RunState.STOPPING}),
    RunState.RUNNING: frozenset({RunState.STOPPING, RunState.SAFE_STOP}),
    RunState.STOPPING: frozenset({RunState.REPORTING, RunState.IDLE}),
    RunState.SAFE_STOP: frozenset({RunState.REPORTING}),
    RunState.REPORTING: frozenset({RunState.IDLE, RunState.FAULT}),
    RunState.FAULT: frozenset({RunState.RESETTING}),
    RunState.RESETTING: frozenset({RunState.IDLE, RunState.FAULT}),  # FAULT: not all clean
}


def new_run_id() -> str:
    return "R" + datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class DeviceWatch:
    device: Device
    client: NodeClient
    boot_id: str | None = None
    last_ok: float = field(default_factory=time.monotonic)
    suspect: bool = False
    rtts_ms: list[float] = field(default_factory=list)  # phi accrual later (12 4-4)
    last_seq: int = 0
    module_data: dict[str, Any] = field(default_factory=dict)
    build: str | None = None
    links: list[dict[str, Any]] = field(default_factory=list)
    wifi_changes: list[dict[str, Any]] = field(default_factory=list)
    wifi_seen: dict[str, dict[str, Any]] = field(default_factory=dict)  # last, across drops
    row: dict[str, Any] | None = None  # module's live_row (02 2-1)
    # receivers this PC relays (tablets on its USB, 01-overview D3): name -> their row
    relayed: dict[str, dict[str, Any]] = field(default_factory=dict)
    memo: dict[str, Any] = field(default_factory=dict)  # module-owned, per device
    last_error: str = ""  # the node's last error event (RUN-009 detail)


class RunController:
    def __init__(
        self,
        devices: DeviceList,
        reports_dir: Path,
        modules_dir: Path,
        bus: EventBus,
        boot_id: str | None = None,
        build: str = "",
        lost_s: float = LOST_S,
        suspect_s: float = SUSPECT_S,
        on_finished: Callable[[], None] | None = None,
        on_report: Callable[[str, list[str]], None] | None = None,
        problems_report: Callable[[str, float], dict[str, Any]] | None = None,
        wifi: Callable[[], WifiConfig | None] = lambda: None,
        web_traffic: Callable[[str], dict[str, int]] | None = None,
        baselines: BaselineStore | None = None,
    ) -> None:
        # 05 4.6.3: the last baseline of each receiver and player (02 2-13)
        self.baselines = baselines or BaselineStore(reports_dir / ".baselines.json")
        # Reset All's way to a node that is off or stuck: its Deployer (the service sets it)
        self.revive: Callable[[str], str] | None = None
        # 2026-10-17 user (the reference ran the Controller as a LAN agent too: "그래야 모든
        # LAN포트 검증을 하지"): this PC's own node and its cards, set by the service
        self.own: Callable[[], tuple[Device, list[dict[str, Any]]]] | None = None
        # a band network's password (S-65), added only to the step that joins it
        self.secret_of: Callable[[str], str | None] | None = None
        self._baseline = False  # this test is a baseline test (S-54)
        self._not_via: list[str] = []  # 09 NET-022: why this test may not go through the DUT
        # receivers whose card the multicast test tells (step 5, GAP-115 ②): name -> why
        # the address could not; and what each one's probe said about the arrival card
        self._by_arrival: dict[str, str] = {}
        self._arrivals: dict[str, dict[str, Any]] = {}
        self._web_traffic = web_traffic  # 07 7 / 02 10 results_web
        self._wifi = wifi  # S-51 for a receiver PC on Wi-Fi (03 F17 "무선 수신 PC")
        # NET-019 (13 1): this PC's cards; replaceable in tests
        self.default_route_nic: Callable[[], str | None] = default_route_nic
        self.nic_of: Callable[[str], str | None] = nic_of
        self.local_ips: Callable[[], dict[str, str]] = _local_ips
        self._route_told = False
        self._clock_told: set[str] = set()  # DSC-009 time sync button, once a PC
        self._problems_report = problems_report  # F21 report table (02 10)
        self._on_finished = on_finished or (lambda: None)  # test over: problems resolved (F21)
        self._on_report = on_report or (lambda run, devices: None)  # records (02 2-13)
        self.devices = devices
        self.reports_dir = reports_dir
        self.modules_dir = modules_dir
        self.bus = bus
        self._failed_checks: set[tuple[str, str]] = set()  # (device, code), F17 step 4
        self.wan_route: Callable[[], str] = ensure_wan_route  # 13 1, tests replace it
        # 05 5.3 trouble windows: device -> kind -> [[from, to or None, code]] (monotonic)
        self._windows: dict[str, dict[str, list[list[Any]]]] = {}
        self._seconds: dict[tuple[str, int], dict[int, tuple[float, dict[str, Any]]]] = {}
        # 16 S-66..S-69: the Router Logger, and problems waiting to be judged the router's
        self.router_logger = RouterLogger(lambda code, level, text, device: self._event(
            code, level, text, device))  # type: ignore[arg-type]  # fmt: skip
        self._logger_wait: list[tuple[float, float, str, list[str]]] = []
        self._stop_wanted = threading.Event()  # a Stop waits for the lock (2026-10-09)
        self._runs: dict[tuple[str, str], tuple[int, float]] = {}  # kind -> seconds in a row
        self._net022: set[str] = set()  # devices with an open NET-022 (cleared once known)
        self._doing = "starting"  # the run loop's step, for a Stop that had to wait
        # GAP-138: trouble stretches of the whole run for the live page (the seconds are
        # kept 15 min only); the module's live_view columns say what trouble is
        self._episodes: dict[tuple[str, int], list[dict[str, Any]]] = {}
        self._trouble_cols: list[dict[str, Any]] | None = None
        self._pending: list[tuple[float, str, dict[str, Any]]] = []  # waiting GRACE_S
        self.boot_id = boot_id or str(uuid.uuid4())
        self.build = build
        self.lost_s = lost_s
        self.suspect_s = suspect_s
        self.state = RunState.IDLE
        self.run_id: str | None = None
        self.result: dict[str, Any] | None = None
        self.generation = self._next_generation()
        self._lock = threading.RLock()  # one command at a time (single queue rule)
        self._rules: Any = None
        self._plan: Any = None
        self._raw_plan: dict[str, Any] = {}
        self._watch: dict[str, DeviceWatch] = {}
        self._links: LinkJudge | None = None  # this run's link judgement (05 1.8)
        self._stop_asked = ""  # reason when a node asks to end the test
        self._cancel = threading.Event()  # Cancel while videos are made (USR-006)
        self.preparing: dict[str, Any] | None = None  # 02 2-1 ``preparing``
        self.checklist: Checklist | None = None  # last Start / precheck (02 2-1)
        self._wireless: dict[str, dict[str, Any]] = {}  # this run's Wi-Fi PCs (02 10)
        self._nat = False  # 07 4, found at F17 step 3
        self.fault: dict[str, Any] | None = None  # 02 2-1: what stopped the test (S6)
        self.reset_result: dict[str, Any] | None = None  # 02 2-8: the last Reset All
        self._wan_ip: str | None = None
        self._wan_subnet: str | None = None
        self._wan_way_used: str | None = None  # 07 3.3: dhcp (ISP role) / static
        self.idle_check: Checklist | None = None  # last light idle check (02 2-1 precheck)
        self._run_dir: Path | None = None
        self._summaries: dict[str, list[dict[str, Any]]] = {}
        self._cells: dict[str, list[dict[str, Any]]] = {}  # live colors for screens
        self._started_utc: str | None = None
        self._measure_start: float | None = None
        # iptv 2.4 (20 GAP-85): the run clock, the run's events and receivers' offsets
        self._run_t0: float | None = None
        self._run_events: list[dict[str, Any]] = []
        self._events_written = 0
        self._run_log_lock = threading.Lock()
        self._offsets: dict[str, list[float]] = {}
        bus.subscribe(self._log_run_event)
        self._halt = threading.Event()
        self._channel_lock = threading.Lock()  # one channel change at a time (F19)
        self._zap_due: dict[str, float] = {}  # S-21: device -> next automatic change
        self._zap_busy: set[str] = set()
        # F18 Always-on: the broadcast that outlives a test (plan, rules, module, since)
        self._bc: dict[str, Any] | None = None
        self._on_broadcast = False  # this test uses a broadcast that ran before it
        self._bc_since: str | None = None  # 02 10 broadcast_since of this test
        self._bc_thread: threading.Thread | None = None
        self._closing = threading.Event()
        self._loop: threading.Thread | None = None
        self._stop_reason = ""
        self.on_change: Callable[[RunState, RunState], None] | None = None

    # ---------------------------------------------------------------- commands

    def start(self, plan: dict[str, Any], run_id: str | None = None) -> str:
        """The F17 checklist, start in order, wait for warm-up; returns the run ID. On any
        failure everything started is rolled back and ZlError is raised."""
        with self._lock:
            self._move(RunState.PREPARING)
            self._cancel.clear()
            with self._run_log_lock:  # the run clock starts with Start (iptv 2.4)
                self._run_events, self._events_written, self._offsets = [], 0, {}
                self._run_t0 = time.monotonic()
            self._run_dir = None  # a rollback must not touch the last run's files
            cl = self.checklist = Checklist("start", len(STEPS))
            try:
                self._checks(cl, plan, run_id or new_run_id())
                with cl.step(6) as s:  # videos first, then every stage but the last
                    if not self._on_broadcast:  # its videos are being sent already
                        self._prepare()  # before run-state: a crash leaves no run to recover
                    self._run_dir = self.reports_dir / str(self.run_id)
                    self._run_dir.mkdir(parents=True, exist_ok=True)
                    # the plan as used (test cards filled, GAP-99 2), for recovery (02 9)
                    _atomic_json(self._run_dir / "plan.json", self._raw_plan)
                    self._summaries = {}
                    self._cells = {}
                    self._windows, self._seconds, self._pending = {}, {}, []  # G-13
                    self._logger_wait = []
                    of = getattr(self._rules, "logger_settings", None)
                    self.router_logger.configure(
                        of(self._raw_plan.get("settings")) if of is not None else None)  # fmt: skip
                    self._runs = {}
                    self._episodes, self._trouble_cols = {}, None
                    self._write_state("running")
                    self._event("ZL-INFO", "info", f"Run {self.run_id} starting")
                    stages = (
                        self._rules.start_steps(self._plan, skip_send=True)
                        if self._on_broadcast
                        else self._rules.start_steps(self._plan)
                    )
                    for stage in stages[:-1]:
                        for step in stage:
                            self._send(step)
                            self._step_done(step)
                    s.detail = f"{sum(len(x) for x in stages[:-1])} started"
                with cl.step(7) as s:
                    for step in stages[-1]:
                        self._send(step)
                        self._step_done(step)
                    self._wait_measuring()
                    s.detail = "measuring"
            except ZlError as e:
                self._rollback(e)
                raise
            self._measure_start = time.monotonic()
            self._started_utc = utc_now()
            self._plan_auto_changes()
            self._note_broadcast()
            self._move(RunState.RUNNING)
            self._halt.clear()
            self._loop = threading.Thread(target=self._run_loop, name="run-loop", daemon=True)
            self._loop.start()
            assert self.run_id is not None
            return self.run_id

    def precheck(self, plan: dict[str, Any], full: bool) -> dict[str, Any]:
        """02 2-6: steps 1-4 without traffic (the screen's idle check), or 1-5 with the
        short test send (``zeroloss precheck``). Never starts a test; RUN-012 during one."""
        with self._lock:
            if self.state is not RunState.IDLE:
                raise ZlError("RUN-012", "Checks run only between tests", f"It is {self.state}.")
            saved = (self._rules, self._plan, self._raw_plan, self._watch, self._links, self.run_id)
            cl = Checklist("precheck" if full else "idle", PRECHECK_STEPS if full else IDLE_STEPS)
            try:
                self._checks(cl, plan, None)
            except ZlError:
                pass  # the checklist has it
            finally:
                if self._watch is not saved[3]:
                    for w in self._watch.values():
                        w.client.close()
                (self._rules, self._plan, self._raw_plan, self._watch, self._links,
                 self.run_id) = saved  # fmt: skip
            if full:
                self.checklist = cl
            else:
                self.idle_check = cl
            return cl.to_dict()

    def stop(self, reason: str = "stopped_by_user") -> dict[str, Any]:
        """Normal stop (F10): receivers, then senders, last summaries, report. While the
        start is making videos it is Cancel instead (iptv 1-1-2, USR-006)."""
        if self.state is RunState.PREPARING and self.preparing is not None:
            self._cancel.set()  # start() rolls back and returns to IDLE
            return {"cancelled": True}
        # 2026-10-09: a Stop waited 2.5 min for the run loop while the screen said nothing.
        # The loop skips its next round when a Stop waits, and a long wait is logged with
        # the step it was in.
        t0 = time.monotonic()
        self._stop_wanted.set()
        try:
            with self._lock:
                waited = time.monotonic() - t0
                if waited > STOP_WAIT_LOG_S:
                    why = f"Stop waited {waited:.0f} s for the test to end its step: {self._doing}"
                    self._event("ZL-INFO", "info", why, run_id=self.run_id or "")
                self._move(RunState.STOPPING)
                self._stop_reason = reason
                return self._finish(safe=False)
        finally:
            self._stop_wanted.clear()

    def channel(self, device: str, ch: int, mode: str = "manual") -> dict[str, Any]:
        """03 F19 (02 2-13): move one receiver to ``ch``. RUNNING only; by hand only while
        S-21 is Manual (K-11). The node answers at the first packet (up to 7 s), so the
        command queue is not held meanwhile; one change at a time."""
        with self._lock:
            if self.state is not RunState.RUNNING:
                raise ZlError(
                    "RUN-012", "Channels change only during a test", f"It is {self.state}."
                )
            rules, plan, run_id = self._rules, self._plan, self.run_id
            if getattr(rules, "channel_step", None) is None:
                raise ZlError("RUN-013", "This test has no channel change")
            if mode == "manual" and rules.auto_channel(plan) is not None:
                raise ZlError(
                    "RUN-012", "Automatic channel change is on",
                    "S-21 changes the channels by itself in this test (K-11).",
                    "Set S-21 to Manual for the next test to change by hand.",
                )  # fmt: skip
            now = self._channels_now()
            step = rules.channel_step(plan, device, ch, now, mode)
        from_ch = now[device][0]
        if step is None:
            return {"device": device, "from": from_ch, "to": ch, "join_method": None, "zap": None}
        with self._channel_lock:
            if self.state is not RunState.RUNNING or self.run_id != run_id:  # G-13
                raise ZlError("RUN-012", "The test ended before the channel change")
            got = self._device_send(step)
        method = step.params.get("join_method")
        self._event("ZL-INFO", "info", f"{device}: CH{from_ch} -> CH{ch} ({mode}, join {method})",
                    device, run_id=run_id or "")  # fmt: skip
        return {"device": device, "from": from_ch, "to": ch, "join_method": method,
                "zap": got.get("zap")}  # fmt: skip

    def _relayed_status(self) -> dict[str, dict[str, Any]]:
        """Tablets: a row each, reached through their PC (11-ui 3). From Start on, also
        before the PC reports them (2026-10-24 user: "devices창에 노트북 두개만 보여" while
        the movies were made): they wait, said so, until their own row comes."""
        out: dict[str, dict[str, Any]] = {}
        for host, w in self._watch.items():
            for name, row in w.relayed.items():
                out[name] = self._relayed_row(host, w, row)
        for name, ch in getattr(self._plan, "receivers", ()) if self._plan is not None else ():
            host = self._node(name)
            if host == name or name in out or host not in self._watch:
                continue
            first = [{"label": f"CH{ch}", "value": None, "unit": "", "key": False}] if ch else []
            waiting = {"color": "grey", "why": "", "link_label": "Wi-Fi",
                       "items": [*first, {"label": "Waiting to start", "value": None,
                                          "unit": "", "key": False}]}  # fmt: skip
            out[name] = self._relayed_row(host, self._watch[host], waiting)
        return out

    @staticmethod
    def _relayed_row(host: str, w: DeviceWatch, row: dict[str, Any]) -> dict[str, Any]:
        return {"suspect": w.suspect, "age_s": round(time.monotonic() - w.last_ok, 1),
                "module_data": {}, "links": [], "test_ip": None, "link_checks": [],
                "wifi_changes": [], "row": row, "via": host}  # fmt: skip

    def _node(self, device: str) -> str:
        """The PC a receiver is reached through: itself, or the PC a tablet hangs on by
        USB (the module says, 01-overview D3)."""
        of = getattr(self._rules, "node_of", None)
        return str(of(self._plan, device)) if of is not None and self._plan is not None else device

    def _nodes(self) -> set[str]:
        return {self._node(d) for d, _ in self._plan.receivers}

    def _channels_now(self) -> dict[str, list[int]]:
        devices = {d for d, _ in self._plan.receivers if self._node(d) == d}
        return {d: self._rules.current_channels(self._watch[d].module_data) for d in devices}

    def _plan_auto_changes(self) -> None:
        """S-21 / S-22: the first change N min after measuring began, receivers apart."""
        self._zap_due, self._zap_busy = {}, set()
        auto = getattr(self._rules, "auto_channel", None)
        cfg = auto(self._plan) if auto is not None else None
        if cfg is None:
            return
        t0 = self._measure_start or time.monotonic()
        for i, d in enumerate(sorted({d for d, _ in self._plan.receivers if self._node(d) == d})):
            self._zap_due[d] = t0 + cfg["every_s"] + i * cfg["stagger_s"]

    def _auto_changes(self) -> None:
        """Called by the run loop (RUNNING, lock held): start the changes that are due."""
        now = time.monotonic()
        cfg = self._rules.auto_channel(self._plan) if self._zap_due else None
        for d, due in list(self._zap_due.items()):
            if now < due or d in self._zap_busy or cfg is None:
                continue
            self._zap_due[d] = max(due + cfg["every_s"], now + 1.0)  # a late one does not pile up
            mine = self._rules.current_channels(self._watch[d].module_data)
            if len(mine) != 1:
                continue
            to = self._rules.next_channel(self._plan, mine[0])
            self._zap_busy.add(d)
            threading.Thread(target=self._auto_one, args=(d, to, self.run_id),
                             name=f"auto-channel-{d}", daemon=True).start()  # fmt: skip

    def _auto_one(self, device: str, ch: int, run_id: str | None) -> None:
        try:
            self.channel(device, ch, mode="auto")
        except ZlError as e:
            if self.state is RunState.RUNNING and self.run_id == run_id:  # not just the end
                self._event(e.code, "warn", f"Automatic channel change failed: {e.what}", device)
        finally:
            self._zap_busy.discard(device)

    # ---------------------------------------------------------------- broadcast (F18)

    def _use_broadcast(self, module: str) -> None:
        """Step 1: a broadcast is on. The plan must be the same Always-on sending; then
        the start skips the senders (RUN-014 otherwise)."""
        self._on_broadcast, self._bc_since = False, None
        bc = self._bc
        if bc is None:
            return
        same = getattr(self._rules, "same_broadcast", None)
        adopted = same(bc["plan"], self._plan) if same and bc["module"] == module else None
        if adopted is None:
            raise ZlError(
                "RUN-014", "Stop the broadcast first",
                "The broadcast that is on sends other TV channels, video or addresses, or "
                "this test is not Always-on (S-13, F18).",
                "Press Stop broadcast (zeroloss broadcast stop), or start with the same "
                "settings.",
            )  # fmt: skip
        self._plan, self._on_broadcast, self._bc_since = adopted, True, str(bc["since_utc"])

    def _note_broadcast(self) -> None:
        """RUNNING: an Always-on test that started its own senders begins a broadcast."""
        if self._on_broadcast or not getattr(self._rules, "always_on", lambda p: False)(self._plan):
            return
        info = self._rules.broadcast_info(self._plan)
        self._bc = {"plan": self._plan, "rules": self._rules,
                    "module": str(self._raw_plan.get("module", "")), "since_utc": utc_now(),
                    **info}  # fmt: skip
        self._event("ZL-INFO", "info", f"Broadcast on: {len(info['channels'])} TV channels")
        if self._bc_thread is None or not self._bc_thread.is_alive():
            self._bc_thread = threading.Thread(target=self._keep_broadcast, name="broadcast",
                                               daemon=True)  # fmt: skip
            self._bc_thread.start()

    def _end_broadcast(self, why: str) -> None:
        if self._bc is not None:
            self._bc = None
            self._event("ZL-INFO", "info", f"Broadcast off: {why}")

    def _keep_broadcast(self) -> None:
        """Outside RUNNING (between tests, while a report is written, while a Start
        checks) the sender hears from us every second, or it stops the broadcast (F11:
        no traffic without an owner). Ends with the broadcast."""
        client: NodeClient | None = None
        last_ok, boot = time.monotonic(), None
        try:
            while not self._closing.wait(1.0):
                bc, running = self._bc, self.state is RunState.RUNNING  # no lock: Start and
                if bc is None:  # the report hold it for seconds, longer than the sender waits
                    return
                if running:  # the run loop asks the sender every second
                    last_ok = time.monotonic()
                    continue
                if client is None:
                    dev = self.devices.get(str(bc["sender"]))
                    client = NodeClient(dev.host, dev.port, self.boot_id, dev.name)
                try:
                    hb = get_heartbeat(client)
                except ZlError:
                    if time.monotonic() - last_ok >= self.lost_s:
                        with self._lock:
                            if self._bc is bc:
                                self._bc_lost(bc)
                    continue
                if boot is not None and hb.get("boot_id") != boot:
                    with self._lock:
                        if self._bc is bc:
                            self._end_broadcast(f"{bc['sender']} restarted")
                    continue
                boot, last_ok = hb.get("boot_id"), time.monotonic()
        finally:
            if client is not None:
                client.close()

    def _bc_lost(self, bc: dict[str, Any]) -> None:
        name = str(bc["sender"])
        self._event("ZL-DSC-014", "warn", f"{name} not answering: the broadcast stopped", name)
        self._bc = None

    def stop_broadcast(self) -> dict[str, Any]:
        """02 2-13 ``broadcast {"on": false}`` (F18): only between tests (RUN-012)."""
        with self._lock:
            if self.state not in (RunState.IDLE, RunState.FAULT):
                raise ZlError("RUN-012", "The broadcast stops only between tests",
                              f"It is {self.state}.", "Stop the test first.")  # fmt: skip
            bc = self._bc
            if bc is None:
                return {"on": False, "stopped": []}
            dev = self.devices.get(str(bc["sender"]))
            client = NodeClient(dev.host, dev.port, self.boot_id, dev.name)
            try:
                for stage in bc["rules"].broadcast_stop_steps(bc["plan"]):
                    for step in stage:
                        client.command(step.path, self.generation, None,
                                       timeout_s=step.timeout_s, **step.params)  # fmt: skip
            finally:
                client.close()
            self._end_broadcast("Stop broadcast")
            return {"on": False, "stopped": list(bc["channels"])}

    def close(self) -> None:
        """Controller shutdown (G-11): the broadcast stops now, not 10 s later by the
        sender's own watch; the keeper thread ends."""
        try:
            self.stop_broadcast()
        except ZlError as e:  # the sender stops it by itself when we are gone (F11)
            self._event(e.code, "warn", f"Broadcast not stopped at shutdown: {e.what}")
        self._closing.set()
        if self._bc_thread is not None:
            self._bc_thread.join(timeout=5)

    def broadcast_status(self) -> dict[str, Any]:
        """02 2-1 ``broadcast``."""
        bc = self._bc
        if bc is None:
            return {"on": False, "since_utc": None, "channels": [], "sender": None}
        return {"on": True, "since_utc": bc["since_utc"], "channels": list(bc["channels"]),
                "sender": bc["sender"]}  # fmt: skip

    # ---------------------------------------------------------------- recovery (F10)

    def recover_unfinished(self) -> list[str]:
        """CTL-003: runs a crashed Controller left as running get a recovery report.
        The first request with our new boot ID makes each node stop the old test
        (02 3); the summaries the file does not have yet are pulled from the nodes."""
        done = []
        for state_file in sorted(self.reports_dir.glob("*/run-state.json")):
            try:
                state = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if state.get("status") not in ("running", "stopping"):
                continue
            with self._lock:
                if self.state is not RunState.IDLE:
                    break  # a new test started meanwhile: never mix (it gets its own report)
                try:
                    self._recover(state_file.parent, state)
                except Exception as e:  # one bad run must not stop the others
                    self._event(
                        "ZL-CTL-007", "error", f"Recovery of {state_file.parent.name} failed: {e!r}"
                    )
                    continue
            done.append(str(state.get("run_id")))
        return done

    def _recover(self, run_dir: Path, state: dict[str, Any]) -> None:
        run_id = str(state.get("run_id"))
        raw = json.loads((run_dir / "plan.json").read_text(encoding="utf-8"))
        rules = load_run_entry(self.modules_dir, str(raw.get("module", "")))
        plan = rules.validate(raw)
        summaries: dict[str, list[dict[str, Any]]] = {}
        last_seq: dict[str, int] = {}
        path = run_dir / "summaries.jsonl"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    s = json.loads(line)
                except ValueError:
                    continue  # a line cut by the crash
                summaries.setdefault(str(s.get("device")), []).append(s)
                node = str(s.get("node", ""))
                last_seq[node] = max(last_seq.get(node, 0), int(s.get("seq", 0)))
        missing = []
        for name in state.get("devices") or []:
            try:
                dev = self.devices.get(name)
                client = NodeClient(dev.host, dev.port, self.boot_id, dev.name)
                client.get("/v1/health")  # new boot ID: the node stops the old test now
                _wait_idle(client)  # its last partial cell is closed only after that stop
                got = client.get("/v1/summaries", after=last_seq.get(name, 0))
                client.close()
            except ZlError:
                missing.append(name)
                self._event("ZL-RPT-005", "warn", f"Data missing from {name}", name)
                continue
            items = [s for s in got.get("summaries") or [] if s.get("run_id") == run_id]
            with path.open("a", encoding="utf-8") as f:
                for s in items:
                    f.write(json.dumps({"node": name, **s}, ensure_ascii=False) + "\n")
            for s in items:
                summaries.setdefault(str(s.get("device", name)), []).append(s)
        measured = max(
            (float(s.get("start_elapsed_s", 0)) + float(s.get("duration_s", 0))
             for pieces in summaries.values() for s in pieces),
            default=0.0,
        )  # fmt: skip
        self._rules, self._plan, self._raw_plan = rules, plan, raw
        self.run_id, self._run_dir, self._watch = run_id, run_dir, {}
        result = judge_run(
            rules, plan, summaries, planned_s=plan.duration_s, measured_s=measured, completed=False
        )
        result.update(
            run_id=run_id,
            build=self.build,
            verdict_reason="controller_restarted",
            devices_rtt_ms={},
            **self._report_fields(result),
        )
        result["recovered"] = True
        events = read_jsonl(run_dir / "events.log")  # what the crashed service had written
        data = {"summaries": summaries, "events": events, "seconds": None, "offsets": {}}
        result.update(analysis_fields(rules, plan, data, "report"))
        if missing:
            why = f"data not collected from: {', '.join(missing)}"
            result["verdict_details"] = [*result.get("verdict_details", []), why]
        write_report(run_dir, result, raw)
        _atomic_json(
            run_dir / "run-state.json",
            {**state, "status": "recovered", "last_saved_utc": utc_now()},
        )
        self.result = result
        self._event(
            "ZL-CTL-003",
            "warn",
            f"Previous test {run_id} ended unexpectedly — recovery report made",
            run_id=run_id,
        )

    def reset(self) -> dict[str, Any]:
        """F11 Reset All (02 2-8): every registered device stops everything and reports
        a clean state (02 4 ``/v1/reset``). Only when all are clean the run is IDLE;
        a device that does not answer or is not clean keeps FAULT with what to do."""
        with self._lock:
            if self.state not in (RunState.IDLE, RunState.FAULT):
                raise ZlError("RUN-012", "Reset All is possible only between tests",
                              f"It is {self.state}.", "Stop the test first.")  # fmt: skip
            self._move(RunState.RESETTING)
            for w in self._watch.values():
                w.client.close()
            self._watch = {}
            self.generation = self._next_generation()  # old commands are refused from now
            with ThreadPoolExecutor(8, thread_name_prefix="reset") as pool:
                rows = list(pool.map(self._reset_revive, self.devices.all()))
            if self.own is not None:  # this PC's cards a test changed go back too
                rows.append(self._reset_one(self.own()[0]))
            # a node that was active (a broadcast) raised its own generation in its safe
            # stop: ours goes above it, or our next command would be "old" (DSC-011)
            self.generation = self._next_generation()
            self._end_broadcast("Reset All")  # every node stopped everything (F18)
            ok = all(r["clean"] for r in rows)
            self.reset_result = {"ok": ok, "updated_utc": utc_now(), "devices": rows}
            if not ok:
                self._move(RunState.FAULT)
                for r in rows:
                    if not r["answered"]:  # opens the power-cycle action (F22)
                        self._event("ZL-DSC-014", "error", f"{r['device']} did not answer the "
                                    "reset", r["device"])  # fmt: skip
                return self.reset_result
            self.fault = None
            self._move(RunState.IDLE)
            self._on_finished()  # the problems of the stopped test are resolved (F21)
            return self.reset_result

    def _reset_revive(self, dev: Device) -> dict[str, Any]:
        """2026-10-17 user ("바로 deinit든 재시작이 되게 깔끔하게 만드는게 리셋"): a PC
        whose node did not answer or is not clean has its node started (it was shut
        down) or restarted by its Deployer, and is reset once more."""
        row = self._reset_one(dev)
        if row["clean"] or self.revive is None:
            return row
        try:
            how = self.revive(dev.name)
        except ZlError as e:
            row["fix"] = (
                f"Neither the node nor the Deployer of {dev.name} answers ({e.what}): "
                "check that the PC is on and its cable is in, then press Reset All."
            )
            return row
        # the node is up on its PC; the way to it (e.g. the WAN-side PC's own DHCP for the
        # router WAN) takes a little longer: wait for it to answer before the reset
        now = self.devices.get(dev.name)
        end = time.monotonic() + REVIVE_WAIT_S
        while time.monotonic() < end and self._answering(now) is None:
            time.sleep(3.0)
        again = self._reset_one(now)  # as it is now
        again["revived"] = how
        return again

    def _addresses(self, dev: Device) -> list[str]:
        """The address in use, then the PC's cards as last seen (deploy.hosts)."""
        cards = [str(c.get("ip")) for c in self.devices.cards.get(dev.name) or [] if c.get("ip")]
        return [h for h in dict.fromkeys([dev.host, *cards]) if h]

    def _answering(self, dev: Device) -> str | None:
        """The first of the PC's addresses its node answers on, else None."""
        for host in self._addresses(dev):
            probe = NodeClient(host, dev.port, self.boot_id, dev.name)
            try:
                probe.get("/v1/health", timeout_s=3.0)
                return host
            except ZlError:
                continue
            finally:
                probe.close()
        return None

    def _reset_one(self, dev: Device) -> dict[str, Any]:
        # 2026-10-17: the sender's WAN card (its address in use) was down and its node
        # still ran an old test; it answered on its other card. Reset it there.
        host = self._answering(dev) or dev.host
        client = NodeClient(host, dev.port, self.boot_id, dev.name)
        try:
            r = client.command("/v1/reset", self.generation, None, timeout_s=RESET_TIMEOUT_S)
        except ZlError as e:
            return {"device": dev.name, "answered": False, "clean": False, "checks": [],
                    "why": e.what, "fix": f"On {dev.name}: restart the ZeroLoss service or "
                    "the PC, then press Reset All again."}  # fmt: skip
        finally:
            client.close()
        clean = bool(r.get("clean"))
        bad = [c for c in r.get("checks") or [] if not c.get("ok")]
        fix = None if clean else f"Restart the node on {dev.name} (Restart Agent)."
        why = "; ".join(f"{c['name']}: {c.get('detail')}" for c in bad) or None
        return {"device": dev.name, "answered": True, "clean": clean,
                "checks": r.get("checks") or [], "why": why, "fix": fix}  # fmt: skip

    @contextlib.contextmanager
    def between_tests(self, what: str) -> Iterator[None]:
        """F11 fault screen actions (Restart Agent, Shut Down All): IDLE or FAULT only."""
        with self._lock:
            if self.state not in (RunState.IDLE, RunState.FAULT):
                raise ZlError("RUN-012", f"{what} is possible only between tests",
                              f"It is {self.state}.", "Stop the test first.")  # fmt: skip
            yield

    def status(self) -> dict[str, Any]:
        elapsed = time.monotonic() - self._measure_start if self._measure_start else 0.0
        return {
            "state": self.state.value,
            "run_id": self.run_id,
            "generation": self.generation,
            "elapsed_s": round(elapsed, 1),
            "planned_s": getattr(self._plan, "duration_s", None),
            # 02 2-1 (GAP-105 4): what is running, for the Test panel during a test
            "plan": {
                k: self._raw_plan.get(k) for k in ("test_type", "quality", "duration_s", "settings")
            }
            if self.state is not RunState.IDLE and self._raw_plan
            else None,
            "cell_s": getattr(self._plan, "cell_s", None),
            "devices": {
                name: {
                    "suspect": w.suspect,
                    "age_s": round(time.monotonic() - w.last_ok, 1),
                    "module_data": w.module_data,
                    "links": w.links,
                    # 02 2-1 (GAP-105 1): the card the test uses; screens show only that one
                    "test_ip": None
                    if getattr(self._rules, "changes_links", False)  # its rows name the card
                    else (self._raw_plan.get("nics") or {}).get(name),
                    "link_checks": self._links.checks(name) if self._links else [],
                    "wifi_changes": list(w.wifi_changes),
                    "row": w.row or {"color": "grey", "why": "", "link_label": "", "items": []},
                }
                for name, w in self._watch.items()
            }
            | self._relayed_status(),
            "cells": {d: list(c) for d, c in self._cells.items()},
            "channels": self._channels_status(),
            "preparing": self.preparing,
            "checklist": self.checklist.to_dict() if self.checklist else None,
            "fault": self.fault,
            "reset": self.reset_result,
            "precheck": self.idle_check.to_dict() if self.idle_check else None,
            "result": self.result,
        }

    def live(self) -> dict[str, Any]:
        """live monitor (02 12 /live) (2026-10-08 GAP-119, user: "모니터링이 지금은 너무 빈약해"):
        each receiver's last ``LIVE_SECONDS`` 1-second records, with only the fields the
        module's ``live_view`` names, its row color, the cells so far and the newest
        problems. The page draws what the module describes; Core names no field."""
        view_of = getattr(self._rules, "live_view", None)
        view = view_of(self._plan) if view_of and self._plan is not None else {"columns": []}
        keys = {"e", *(str(c["key"]) for c in view.get("columns") or []),
                *(str(c["key"]) for c in view.get("wifi") or [])}  # fmt: skip
        receivers = []
        for (device, ch), held in sorted(dict(self._seconds).items()):
            kept = dict(held)  # a copy: the run thread adds a second while we read
            recs = [kept[e][1] for e in sorted(kept)[-LIVE_SECONDS:]]
            w = self._watch.get(device)
            relayed = None
            if w is None and self._watch.get(self._node(device)) is not None:
                relayed = self._watch[self._node(device)].relayed.get(device)  # a tablet
            test_ip = (self._raw_plan.get("nics") or {}).get(device)
            found = next((x for x in (w.links if w else []) if x.get("ip") == test_ip), None)
            card = None  # GAP-127: which card it receives on, wired or Wi-Fi
            if test_ip:
                wifi = test_ip == "wifi" or bool(found and is_wireless(found))
                card = {"ip": test_ip, **(found or {}), "wireless": wifi}
            form_of = getattr(self._rules, "stream_form", None)  # GAP-149: M2U or not
            arrives = form_of([kept[x][1] for x in sorted(kept)]) if form_of else None
            key = f"{device}|{ch}"
            episodes = [{**ep, "notes": self._episode_notes(key, ep)}
                        for ep in list(self._episodes.get((device, ch), []))]  # fmt: skip
            receivers.append({
                "key": key, "device": device, "ch": ch, "card": card, "episodes": episodes,
                "arrives": arrives,
                "records": [{k: v for k, v in r.items() if k in keys} for r in recs],
                "row": (w.row if w else relayed) or {"color": "grey", "why": "", "items": []},
                "age_s": round(time.monotonic() - w.last_ok, 1) if w else None,
                "e_shift": self._e_shift(key),
            })  # fmt: skip
        elapsed = time.monotonic() - self._measure_start if self._measure_start else 0.0
        return {
            "state": self.state.value, "run_id": self.run_id,
            "elapsed_s": round(elapsed, 1), "planned_s": getattr(self._plan, "duration_s", None),
            "quality": self._raw_plan.get("quality") if self._raw_plan else None,
            "view": view, "receivers": receivers,
            "senders": {n: (w.row or {}) for n, w in self._watch.items()
                        if not any(r["device"] == n for r in receivers)},
            "cells": {d: list(c) for d, c in self._cells.items()},
            "result": self.result,
        }  # fmt: skip

    def _e_shift(self, key: str) -> int:
        """Seconds to add to this receiver's ``e`` to put it on the page's clock (the run's
        measuring time): each receiver counts from its own start, so receivers side by
        side (tablets 1-3 and a PC, 2026-10-23) line up only through this (D39)."""
        samples = self._offsets.get(key) or []
        if not samples or self._run_t0 is None or self._measure_start is None:
            return 0
        return round(statistics.median(samples) - (self._measure_start - self._run_t0))

    def _channels_status(self) -> dict[str, Any] | None:
        """02 2-1 ``run.channels`` (F19): what the screen's channel box may offer."""
        auto = getattr(self._rules, "auto_channel", None)
        if auto is None or self._plan is None or self.state is not RunState.RUNNING:
            return None
        now = time.monotonic()
        return {
            "on_air": list(self._plan.on_air),
            "auto": auto(self._plan),
            "next_in_s": {d: round(max(0.0, t - now), 1) for d, t in self._zap_due.items()},
        }

    # ---------------------------------------------------------------- start helpers

    def _checks(self, cl: Checklist, plan: dict[str, Any], run_id: str | None) -> None:
        """F17 steps 1..cl.upto (at most 5). Raises the first failure's ZlError."""
        with cl.step(1) as s:
            self._route_to_wan()  # 13 1 / GAP-88: the way to a PC behind the router's NAT
            hosts = {d.name: d.host for d in self.devices.all()}  # 02 11-1: filled here
            # every PC's cards as last seen (a module that measures each one, 2026-10-16)
            cards = {d.name: list(self.devices.cards.get(d.name) or []) for d in self.devices.all()}
            # 2026-10-17 user: the address picked or typed in a PC's row is used as it is,
            # for control and for the test (no card is guessed, no Wi-Fi is joined)
            used = [
                str((plan.get("devices") or {}).get("sender") or ""),
                *(str(r.get("device")) for r in (plan.get("devices") or {}).get("receivers") or []),
            ]
            nics = {d: hosts[d] for d in used if hosts.get(d)}
            nics.update({k: v for k, v in (plan.get("nics") or {}).items() if v and v != "wifi"})
            if str(plan.get("module")) == "throughput" and self.own is not None:
                me, links = self.own()
                cards[me.name] = links
            plan = {**plan, "hosts": hosts, "cards": cards, "nics": nics,
                    **self._tablets_present(plan)}  # fmt: skip
            self._rules = load_run_entry(self.modules_dir, str(plan.get("module", "")))
            self._plan = self._rules.validate(plan)
            self._raw_plan = plan
            chosen = (plan.get("settings") or {}).get("S-54", CORE_DEFAULTS["S-54"])
            self._baseline = chosen == BASELINE  # 05 4.6.3
            self.run_id = cl.run_id = run_id
            infos = self._open_devices()
            self._fill_test_cards(infos)  # GAP-99 2: before the plan is used anywhere
            self._use_broadcast(str(plan.get("module", "")))
            s.detail = ", ".join(sorted(self._watch))
        with cl.step(2) as s:
            s.detail = self._check_tools(infos)
        with cl.step(3) as s:
            s.detail = self._check_network(cl, s, infos)
            if run_id is not None:  # a real Start: a popup once, the person decides
                self._sender_wifi_told = False
                # its cards: what it said at Start (/v1/info), else as last seen -- the
                # heartbeats that fill the watch's list begin only after the checks
                name = self._plan.sender
                seen = ((infos.get(name) or {}).get("links")
                         or list(self.devices.cards.get(name) or []))  # fmt: skip
                self._advise_sender_wifi({"links": seen}, "")
        with cl.step(4) as s:
            s.detail = self._check_disk_time(cl, s)
        if cl.upto >= 5:
            with cl.step(5) as s:
                s.detail = self._probe(s)

    def _open_devices(self) -> dict[str, dict[str, Any]]:
        """F17 step 1: every device answers, is the same PC, speaks our protocol, has
        our build and the plan's module. Returns each device's ``/v1/info``."""
        names = {self._plan.sender, *self._nodes()}
        module = str(self._raw_plan.get("module", ""))
        self._watch, infos = {}, {}
        for name in sorted(names):
            dev = self._device(name)
            client = NodeClient(dev.host, dev.port, self.boot_id, dev.name)
            self._watch[name] = w = DeviceWatch(dev, client)
            try:
                try:
                    health = client.get("/v1/health")
                except ZlError as e:
                    raise _unreachable(e, dev.host) from e
                w.boot_id, w.build = str(health.get("boot_id")), health.get("build")
                _same_node(name, dev.device_id, health, self.build)
                self._catch_up_generation(name, health)
                infos[name] = info = client.get("/v1/info")
            except ZlError as e:
                raise DeviceError(name, e) from e
            mod = next((m for m in info.get("modules") or [] if m.get("id") == module), None)
            if not mod or not mod.get("enabled"):
                why = str(mod.get("reason") or "disabled") if mod else "not installed"
                raise DeviceError(name, ZlError(
                    "INS-024", f"Module {module} cannot be used on {name}", why,
                    "Press Update PCs: it installs this program's modules on that PC too.",
                ))  # fmt: skip
        return infos

    def _check_tools(self, infos: dict[str, dict[str, Any]]) -> str:
        """F17 step 2 (INS-029): the tools this plan needs are on each device."""
        need_of = getattr(self._rules, "required_tools", None)
        if need_of is None:
            return "no tools listed by the module"
        seen = []
        for name in sorted(self._watch):
            tools = infos[name].get("tools") or {}
            need = list(need_of(self._plan, name))
            missing = [t for t in need if not tools.get(t)]
            if missing:
                raise DeviceError(name, ZlError(
                    "INS-029", f"{', '.join(missing)} is not installed on {name}",
                    "The test needs this program on that PC.",
                    "Install it (docs/15-tools.md), then press Start again.",
                ))  # fmt: skip
            seen += [f"{t} {tools[t].get('version') or '?'}" for t in need]
        return ", ".join(sorted(set(seen))) or "none needed"

    def _check_network(
        self, cl: Checklist, s: PrecheckStep, infos: dict[str, dict[str, Any]]
    ) -> str:
        """F17 step 3: the test Wi-Fi of a Wi-Fi receiver PC, link speed (NET-012) and
        whether a NAT sits between (07 4)."""
        wifi = self._join_wifi(cl, s)
        isp = self._isp_only_on_sender() if cl.kind == "start" else []
        self._check_test_ips()
        self._check_links()
        nat = [*isp]
        self._nat, self._wan_ip, self._wan_subnet = False, None, None
        self._wan_way_used = None
        for name, w in sorted(self._watch.items()):
            own = own_address(w.device.host, w.device.port)
            seen = infos[name].get("seen_from_ip")
            via_nat = bool(own and seen and own != seen)
            if via_nat and self._baseline:
                raise DeviceError(name, ZlError(
                    "NET-020",
                    "Baseline test needs the sender and receivers on one switch without the "
                    "router",
                    f"{name} is reached through a NAT (router WAN IP {seen}).",
                    "Take the router out and plug the sender PC and the receivers into one "
                    "switch, or set Test network (S-54) to Through the router.",
                ))  # fmt: skip
            if via_nat:
                nat.append(f"{name} through NAT (router WAN IP {seen})")  # 07 4
                self._nat, self._wan_ip = True, str(seen)
                self._same_subnet(name, str(seen), infos[name])
                nat.append(self._wan_way(name, w, str(seen)))
                nat.append(self._no_ip_conflict(name, w, infos[name]))
            err = other_network(
                name, w.links, infos[name].get("reached_ip"), own,
                test_ip=(self._raw_plan.get("nics") or {}).get(name),
                isp_ready=name == self._plan.sender and not self._baseline  # GAP-113: no router
                and self._isp_ready(w),
                via_nat=via_nat,
            )  # fmt: skip
            if err is not None:  # 09 NET-018 (20 W-3): a warning, the start goes on
                cl.warn(s, err, name)
                if cl.kind == "start":
                    self._event(err.code, "warn", f"{name}: {err.what}", name)
                nat.append(f"⚠ {err.what}")
        route = self._check_route(cl, s)
        return "; ".join([*wifi, "links ok", *nat, *route])

    def _fill_test_cards(self, infos: dict[str, dict[str, Any]]) -> None:
        """03 F17 (GAP-99 2): devices the plan names no test card for get the one the
        Controller can tell. When it cannot, the test still runs (user decision): NET-022,
        the report says so and a PASS becomes INCOMPLETE."""
        self._not_via, self._by_arrival, self._arrivals = [], {}, {}
        nics = dict(self._raw_plan.get("nics") or {})
        own = [d for d, _ in self._plan.receivers if self._node(d) == d]  # not tablets
        roles = [(self._plan.sender, "sender"), *((d, "receiver") for d in own)]
        filled = False
        missing = any(n not in nics and n in self._watch for n, _ in roles)
        isp = self._isp_ip() if missing else None  # asked once, only when a card is to be told
        for name, role in roles:
            if name in nics or name not in self._watch:
                continue
            w = self._watch[name]
            if not w.links:
                self._heartbeat(w)
            ip, why = test_card(role, infos[name].get("reached_ip"), w.links, isp,
                                baseline=self._baseline)  # fmt: skip
            if ip is not None:
                nics[name], filled = ip, True
                continue
            if role == "receiver":  # step 5 listens on every card and names the right one
                self._by_arrival[name] = why
                continue
            self._not_via.append(f"{name}: {why}")
            self._net022.add(name)
            self._event("ZL-NET-022", "warn", f"{name}: the test may not go through the router: "
                        f"{why}", name, run_id=self.run_id or "")  # fmt: skip
        if filled:
            self._raw_plan = {**self._raw_plan, "nics": nics}
            self._plan = self._rules.validate(self._raw_plan)
        for name, nic, ip in self._wireless_on_cable(nics):
            self._event("ZL-NET-026", "warn", f"{name} is set to Wireless but receives on its "
                        f"cable {nic} ({ip}): its Wi-Fi card has no address on the test "
                        "router", name, run_id=self.run_id or "")  # fmt: skip
        for name, _ in roles:  # 2026-10-09: the idle check's "may not go through the
            ip = nics.get(name)  # router" stayed open all run although the card was known
            if isinstance(ip, str) and ip != "wifi" and name in self._net022:
                self._net022.discard(name)  # only a warning that is open (no log every 30 s)
                self._event("ZL-INFO", "info", f"{name}: test card {ip}", name,
                            clears=["ZL-NET-022"])  # fmt: skip

    def _wireless_on_cable(self, nics: dict[str, Any]) -> list[tuple[str, str, str]]:
        """PC rows set to Wireless whose test card is a cable: (name, card, address)."""
        out = []
        for name in self._raw_plan.get("wireless_pcs") or []:
            w = self._watch.get(name)
            ip = nics.get(name)
            card = next((x for x in (w.links if w else []) if x.get("ip") == ip), None)
            if card is not None and not is_wireless(card):
                out.append((str(name), str(card.get("nic") or "?"), str(ip)))
        return out

    def _adopt_arrivals(self) -> None:
        """Step 5 (2026-10-08 GAP-115 ②): a receiver whose card the Controller could not
        tell by address listened on every card; the card the test packets came in on is
        its test card — the wired one for a wired receiver when several got them (a PC
        on the router LAN by cable and by the test Wi-Fi). None came in: NET-022 as
        before. Works at any site: nothing is assumed about the addresses there."""
        if not self._by_arrival:
            return
        nics = dict(self._raw_plan.get("nics") or {})
        for name, why in sorted(self._by_arrival.items()):
            got = self._arrivals.get(name) or {}
            w = self._watch.get(name)
            wireless = {str(x.get("nic")) for x in (w.links if w else []) if is_wireless(x)}
            cards = [c for c in got.get("arrived_cards") or [] if isinstance(c, dict)]
            wired = [c for c in cards if str(c.get("nic")) not in wireless and c.get("ip")]
            best = wired[0] if wired else got.get("arrived_on")
            if isinstance(best, dict) and best.get("ip"):
                nics[name] = str(best["ip"])
                self._net022.discard(name)
                self._event("ZL-INFO", "info", f"{name}: test card found by the multicast "
                            f"test: {best.get('nic')} {best['ip']}", name,
                            clears=["ZL-NET-022"])  # fmt: skip
                continue
            self._not_via.append(f"{name}: {why}")
            self._net022.add(name)
            self._event("ZL-NET-022", "warn", f"{name}: the test may not go through the router: "
                        f"{why}", name, run_id=self.run_id or "")  # fmt: skip
        self._by_arrival = {}
        if nics != (self._raw_plan.get("nics") or {}):
            self._raw_plan = {**self._raw_plan, "nics": nics}
            # the plan as step 5 left it (S-18 Auto may have made it primed): only the
            # cards change, never a fresh validation that would undo that
            if dataclasses.is_dataclass(self._plan) and hasattr(self._plan, "nics"):
                self._plan = dataclasses.replace(self._plan, nics=dict(nics))  # type: ignore[type-var]
            else:
                self._plan = self._rules.validate(self._raw_plan)

    def _isp_ip(self) -> str | None:
        """The sender's ISP address (07 3) when its ISP role is ready, else None."""
        w = self._watch.get(self._plan.sender)
        if w is None:
            return None
        try:
            wan = w.client.get("/v1/wan", timeout_s=2.0)
        except ZlError:
            return None  # an older node: the reached card decides
        return (
            str(wan["server_ip"]) if wan.get("state") == "ready" and wan.get("server_ip") else None
        )

    def _check_test_ips(self) -> None:
        """09 NET-021: every test card the plan names is really on that device. Else the
        test send fails later and points at the wrong cause (GAP-99)."""
        for name, ip in sorted((self._raw_plan.get("nics") or {}).items()):
            w = self._watch.get(str(name))
            if w is None or not isinstance(ip, str) or ip == "wifi" or ip.startswith("127."):
                continue  # loopback is always there and not in the links (one-PC tests)
            if not w.links:
                self._heartbeat(w)
            if any(x.get("ip") == ip for x in w.links):
                continue
            have = ", ".join(f"{x.get('nic')} {x.get('ip') or 'no IPv4'}" for x in w.links)
            raise DeviceError(name, ZlError(
                "NET-021", f"{name} has no network card with {ip}",
                f"The plan sends or receives on {ip}, but {name} has: {have or 'no cards'}.",
                "Check that PC's test card address (on the sender: did the ISP role put "
                "10.0.0.1 on it, SND-023), or fix nics in the plan.",
            ))  # fmt: skip

    def _check_route(self, cl: Checklist, s: PrecheckStep) -> list[str]:
        """09 NET-019 (13 1, 20 GAP-73 5): the card this PC reaches the test devices by
        must not carry its default route while another card exists. Asked once in
        Action Required with a [Fix] button; closed when a later check finds it fine."""
        test_nic = None
        for w in self._watch.values():
            own = own_address(w.device.host, w.device.port)
            if own and not own.startswith("127."):
                test_nic = self.nic_of(own)
                break
        ips = self.local_ips()
        others = sorted(n for n in ips if n != test_nic)
        err = controller_route(test_nic, self.default_route_nic(), others)
        if err is None:
            if self._route_told:
                self._route_told = False
                self._event("ZL-INFO", "info", "The test network card no longer carries the "
                            "default route", "", clears=["ZL-NET-019"])  # fmt: skip
            return []
        cl.warn(s, err, "Controller")
        if not self._route_told:  # every idle check sees it: ask once
            self._route_told = True
            fix = {"id": test_nic, "label": f"Fix {test_nic}: no default route, no DNS"}
            self._event(err.code, "warn", err.what, "", choose="test_route", choices=[fix])
        return [f"⚠ {err.what}"]

    def _isp_only_on_sender(self) -> list[str]:
        """07 3.2 B: the ISP role starts by itself on any PC's wired card that stays up
        with no DHCP answer. 2026-11-05 (user: "WAN물린 Sender가 앱에서 뭔짓 하나봐"): a new
        router resetting left the T460 receiver's LAN card without an answer for 20 s; it
        became 10.0.0.1 there and lent 10.0.0.100 to the Controller PC. At Start every PC
        that is not this test's sender is set to off (kept between tests), and said when it
        was serving; the sender set off is put back to auto (a receiver that sends now; a
        fixed-address sender, 07 3.2 A, is not touched by auto: its card has an address)."""
        said = []
        for name, w in sorted(self._watch.items()):
            sender = name == self._plan.sender
            try:
                st = w.client.get("/v1/wan", timeout_s=2.0)
                if sender and st.get("mode") == "off":
                    w.client.command("/v1/wan", self.generation, self.run_id, timeout_s=20.0,
                                     auto=True)  # fmt: skip
                if sender or st.get("mode") != "auto":
                    continue  # off already, or set on by hand
                w.client.command("/v1/wan", self.generation, self.run_id, timeout_s=20.0,
                                 on=False)  # fmt: skip
            except ZlError:
                continue  # an older node, or no answer: as before
            if st.get("state") == "ready":
                lent = (st.get("lease") or {}).get("mac") or "nobody"
                text = (
                    f"{name} had turned itself into the router's ISP on {st.get('nic')} "
                    f"(10.0.0.1, lent 10.0.0.100 to {lent}): turned off, it receives"
                )
                self._event("ZL-INFO", "info", text, name)
                said.append(text)
        return said

    def _isp_ready(self, w: DeviceWatch) -> bool:
        try:
            return w.client.get("/v1/wan", timeout_s=2.0).get("state") == "ready"
        except ZlError:
            return False  # an older node, or no answer: nothing to compare

    def _route_to_wan(self) -> None:
        """13 1 (GAP-88): the Controller's route to the router WAN side (B: 10.0.0.0/24)
        through its test NIC; told once when it had to be added."""
        done = self.wan_route()
        if "now through" in done:
            self._event("ZL-INFO", "info", done)
        elif "could not" in done:  # a button instead of a command (2026-10-22 user:
            # "사용자가 매뉴얼로 돌리기 힘들어 버튼으로 하는게 좋아")
            add = {"id": "add", "label": "Add the route (Windows asks once)"}
            self._event("ZL-NET-023", "warn", done, choose="wan_route", choices=[add])

    def _no_ip_conflict(self, name: str, w: DeviceWatch, info: dict[str, Any]) -> str:
        """09 SND-013: the address of the PC behind the router (the sender on the WAN
        side) must be its own — an ARP probe from that PC. Blocks the start."""
        ip = info.get("reached_ip")
        if not isinstance(ip, str):
            return "IP conflict not checked (address unknown)"
        try:
            r = w.client.command("/v1/arp_probe", self.generation, self.run_id,
                                 timeout_s=10.0, ip=ip)  # fmt: skip
        except ZlError as e:
            if e.code == "ZL-DSC-012" and "no such path" in e.detail:  # an older node
                return "IP conflict not checked"
            raise DeviceError(name, e) from e
        if not r.get("checked"):
            return f"IP conflict not checked ({r.get('why')})"
        macs = [str(m) for m in r.get("conflict_macs") or []]
        if macs:
            raise DeviceError(name, ZlError(
                "SND-013", f"Another machine uses {name}'s address {ip}",
                f"{', '.join(macs)} answered for {ip} on {r.get('nic')}.",
                "Give that machine or this PC another address in the router WAN subnet.",
            ))  # fmt: skip
        return f"{ip} used by {name} only"

    def _wan_way(self, name: str, w: DeviceWatch, wan_ip: str) -> str:
        """07 3.3: which way the router WAN got its address (never an error)."""
        try:
            st = w.client.get("/v1/wan")
        except ZlError:
            return "router WAN way unknown"
        lease = st.get("lease") or {}
        self._wan_way_used = "dhcp" if lease.get("ip") == wan_ip else "static"
        if self._wan_way_used == "dhcp":
            return f"router WAN {wan_ip} from {name} (built-in DHCP, ISP role)"
        return f"router WAN is static ({wan_ip})"

    def _same_subnet(self, name: str, wan_ip: str, info: dict[str, Any]) -> None:
        """07 3.3 / SND-010: a PC behind the router's NAT (the sender, the ISP role) must
        be in the router WAN subnet, or the router may drop its multicast (RPF)."""
        reached, prefix = info.get("reached_ip"), info.get("reached_prefix")
        if not isinstance(reached, str) or not isinstance(prefix, int):
            return  # the node cannot tell its subnet (older node, other OS)
        net = ipaddress.ip_network(f"{reached}/{prefix}", strict=False)
        self._wan_subnet = str(net)
        if ipaddress.ip_address(wan_ip) not in net:
            raise DeviceError(name, ZlError(
                "SND-010", f"Router WAN IP {wan_ip} is not in {name}'s subnet {net}",
                "The sender PC and the router WAN must be in one subnet (the router may "
                "drop multicast from outside it).",
                "Put the router WAN and the sender PC into one subnet (07 3), "
                "e.g. 10.0.0.1/24 and 10.0.0.2/24.",
            ))  # fmt: skip

    def _join_wifi(self, cl: Checklist, s: PrecheckStep) -> list[str]:
        """03 F17 "무선 수신 PC": a receiver PC with ``nics: "wifi"`` gets on the test
        Wi-Fi (S-51); its address then goes into the plan, which is checked again. The
        light idle check only reads. Returns the lines for the checklist."""
        self._wireless = {}
        nics = dict(self._raw_plan.get("nics") or {})
        wanted = sorted(n for n, v in nics.items() if v == "wifi" and n in self._watch)
        if not wanted:
            return []
        test = self._wifi()
        lines = []
        for name in wanted:
            w = self._watch[name]
            try:
                st = w.client.get("/v1/wifi")
            except ZlError as e:
                raise DeviceError(name, e) from e
            cards = [c for c in st.get("nics") or [] if isinstance(c, dict)]
            if not st.get("supported") or not cards:
                why = "no Wi-Fi network card" if st.get("supported") else "not supported yet"
                raise DeviceError(name, _no_wifi(f"{name} cannot receive over Wi-Fi", why))
            on = [c for c in cards if c.get("ip") and c.get("ssid")]
            use = next((c for c in on if test and c["ssid"] == test.ssid), None)
            joined = False
            # 2026-10-15 (GAP-159): a card already on the router under test (the LAN of the
            # address the Controller uses for this PC, not that address itself) is used as
            # it is — S-51 held the old router's SSID, and the join knocked the card off the
            # new router's Wi-Fi. The saved SSID is only for a card that is on nothing.
            # 2026-10-16: the PC's address WAS that card (picked in the IP box), so it was
            # skipped and the join knocked it off. A card that shares a network with one of
            # the PC's other cards (the cable into the router) is on the router under test.
            names = {str(c.get("nic")) for c in cards}
            wired = [str(x.get("ip")) for x in self.devices.cards.get(name) or []
                     if x.get("ip") and str(x.get("nic")) not in names]  # fmt: skip
            here = [c for c in on if any(same_network(str(c["ip"]), a) for a in wired)
                    or (str(c["ip"]) != w.device.host
                        and same_network(str(c["ip"]), w.device.host))]  # fmt: skip
            if use is None and here:
                use = here[0]
                if test is not None:
                    cl.warn(s, ZlError(
                        "RCV-023", f"{name} stays on {use['ssid']!r}: it is the router under test",
                        f"The saved test Wi-Fi (S-51) {test.ssid!r} is not used.",
                        "Nothing to do. Clear S-51 (zeroloss wifi clear) if it is an old router's.",
                    ), name)  # fmt: skip
            if use is None and test is None and on:
                use = on[0]
                cl.warn(s, ZlError(
                    "RCV-023", f"S-51 is empty: using the current Wi-Fi {use['ssid']!r}",
                    "No test Wi-Fi is set, so the one this PC is on is used.",
                    "Set the test Wi-Fi (zeroloss wifi set) to be sure.",
                ), name)  # fmt: skip
            if use is None and test is None:
                raise DeviceError(name, _no_wifi(
                    f"{name} is not on any Wi-Fi", "No test Wi-Fi (S-51) is set.",
                ))  # fmt: skip
            if use is None and cl.kind == "idle":  # read only: connect at Start
                assert test is not None
                cl.warn(s, ZlError(
                    "RCV-023", f"{name} connects to {test.ssid!r} at Start",
                    "It is not on the test Wi-Fi now.", "Nothing to do.",
                ), name)  # fmt: skip
                lines.append(f"{name} Wi-Fi: joins {test.ssid} at Start")
                continue
            if use is None:
                assert test is not None
                card, leaves = join_card(cards, w.device.host)
                if leaves:  # the only card left is on another network (e.g. the internet)
                    cl.warn(s, ZlError(
                        "RCV-023", f"{name}: {card} leaves Wi-Fi {leaves!r} to join {test.ssid!r}",
                        "No other Wi-Fi card is free or on the test network.",
                        "Add a second Wi-Fi card for the test if that network is needed.",
                    ), name)  # fmt: skip
                try:
                    use = w.client.command(
                        "/v1/wifi/join", self.generation, self.run_id,
                        timeout_s=WIFI_JOIN_S + 15, ssid=test.ssid, security=test.security,
                        password=test.password, nic=card,
                    )  # fmt: skip
                except ZlError as e:
                    raise DeviceError(name, e) from e
                joined = True
            nics[name] = str(use["ip"])
            self._wireless[name] = {"wifi_auto_connected": joined, "m2u": None}
            lines.append(
                f"{name} Wi-Fi {use['ssid']} {use['ip']}" + (" (joined)" if joined else "")
            )
        if any(v == "wifi" for v in nics.values()):
            return lines  # the light check leaves the plan as it is
        self._raw_plan = {**self._raw_plan, "nics": nics}
        self._plan = self._rules.validate(self._raw_plan)  # now with the Wi-Fi address
        return lines

    def _check_links(self) -> None:
        """F17 step 3 / NET-012: every test NIC must carry its load before anything
        starts. ``usual`` is taken before this run's readings update the device list."""
        needs = {n: dict(self._rules.link_need(self._plan, n)) for n in self._watch}
        usual = {n: dict(w.device.best_link_mbps) for n, w in self._watch.items()}
        # a module that sets link speeds itself (throughput) is not judged on them
        own = bool(getattr(self._rules, "changes_links", False))
        self._links = None if own else LinkJudge(needs, usual)
        for name, w in self._watch.items():
            try:
                self._heartbeat(w)
                for ev in precheck(name, w.links, needs[name]):
                    self.bus.publish(ev)
            except ZlError as e:
                raise DeviceError(name, e) from e

    def _heartbeat_tries(self, name: str, w: DeviceWatch) -> dict[str, Any]:
        """A start check's heartbeat: one late answer (1 s, over Wi-Fi) is asked again, up
        to three times (2026-10-22: Start stopped at "t460-rx: no answer in 1 s"; it
        answered in 0.05 s the next moment)."""
        last: ZlError | None = None
        for _ in range(3):  # a slow PC is not a lost one (2026-10-22 user: "더 오래 걸리는
            # 컴퓨터를 사용하면 어쩔려고"): a start check waits like a command, not 1 s
            try:
                return get_heartbeat(w.client, timeout_s=CHECK_TIMEOUT_S)
            except ZlError as e:
                last = e
                time.sleep(0.5)
        assert last is not None
        raise DeviceError(name, last)

    def _check_disk_time(self, cl: Checklist, s: PrecheckStep) -> str:
        """F17 step 4: room for this test above the RCV-008 line; clocks within 2 s."""
        need_of = getattr(self._rules, "disk_need_mb", None)
        worst = 0.0
        for name, w in sorted(self._watch.items()):
            before = datetime.now(UTC)
            hb = self._heartbeat_tries(name, w)
            offset = clock_offset_s(hb.get("ts_utc"), before, datetime.now(UTC))
            mem = (hb.get("resources") or {}).get("mem_free_pct")
            if isinstance(mem, int | float) and mem < MEM_WARN_PCT:  # SYS-004
                err = ZlError(
                    "SYS-004", f"Low memory on {name}: {mem:.0f}% free",
                    f"At least {MEM_WARN_PCT:.0f}% should be free for a long test"
                    f" (below {MEM_STOP_PCT:.0f}% it does not start).",
                    "Close programs on that PC, then check again.",
                )  # fmt: skip
                if mem < MEM_STOP_PCT:
                    raise DeviceError(name, err)
                cl.warn(s, err, name)
                if cl.kind == "start":
                    self._event(err.code, "warn", f"{name}: {err.what}", name)
            power = hb.get("power") or {}
            if power.get("sleep_guard") == "unavailable":  # SYS-005: warn, never stop
                err = ZlError(
                    "SYS-005", f"{name} may go to sleep during the test",
                    str(power.get("why") or "It cannot hold off sleep."),
                    "Install the node package (it allows this), or turn off sleep on that PC.",
                )  # fmt: skip
                cl.warn(s, err, name)
                if cl.kind == "start":
                    self._event(err.code, "warn", f"{name}: {err.what}", name)
            free = (hb.get("resources") or {}).get("disk_free_mb")
            need = float(need_of(self._plan, name)) if need_of else 0.0
            if isinstance(free, int) and free - need < DISK_WARN_MB:
                hours = most_hours(free, need, self._plan.duration_s)
                raise DeviceError(name, ZlError(
                    "RCV-008", f"Not enough disk on {name}: {free} MB free",
                    f"This test writes about {need:.0f} MB and {DISK_WARN_MB} MB must stay free.",
                    f"Free some space or run at most {hours:.1f} h.",
                ))  # fmt: skip
            if offset is not None:
                worst = max(worst, abs(offset))
                if abs(offset) > CLOCK_WARN_S:
                    if cl.kind == "start":  # Problems too (F21); not every idle check
                        self._event("ZL-DSC-009", "warn", f"{name} clock {offset:+.1f} s off", name)
                    ntp = (power.get("time_sync") or {}).get("ntp")
                    if ntp is False and name not in self._clock_told:  # a button, asked once
                        # (2026-10-31 user: "명령어로 나한테 시키지 말고 앱에 반영할수있나?")
                        self._clock_told.add(name)
                        on = {"id": name, "label": f"Turn on time sync on {name}"}
                        self._event("ZL-DSC-017", "warn", f"{name} clock {offset:+.1f} s off "
                                    "and its time sync is off", name, choose="time_sync",
                                    choices=[on])  # fmt: skip
                    cl.warn(s, ZlError(
                        "DSC-009", f"{name} clock is {offset:+.1f} s off",
                        "Its time differs from the Controller's; results use Controller time.",
                        "Nothing to do; set the PC clock if you want matching log times.",
                    ), name)  # fmt: skip
        return "; ".join([f"disk ok, clocks within {worst:.1f} s", *self._device_checks(cl, s)])

    def _device_checks(self, cl: Checklist, s: PrecheckStep) -> list[str]:
        """F17 step 4 "PC speed" (12 2 ``device_checks``): the module's checks per device;
        a ``warn`` in the answer is a warning, never a stop (RCV-015)."""
        checks_of = getattr(self._rules, "device_checks", None)
        lines = []
        for step in checks_of(self._plan, cl.kind == "idle") if checks_of else []:
            try:
                r = self._device_send(step)
            except DeviceError as e:
                self._failed_checks.add((step.device, e.code))  # cleared when it passes
                raise
            clears = [c for c in r.get("clears") or [] if (step.device, c) in self._failed_checks]
            if clears:  # 02 8: the problem is over, its Action Required closes by itself
                self._failed_checks -= {(step.device, c) for c in clears}
                self._event("ZL-INFO", "info", f"{step.device}: check passes again",
                            step.device, clears=clears)  # fmt: skip
            warn = r.get("warn")
            if isinstance(warn, dict) and warn.get("code"):
                err = ZlError(str(warn["code"]), str(warn.get("what") or ""),
                              str(warn.get("why") or ""), str(warn.get("fix") or ""))  # fmt: skip
                cl.warn(s, err, step.device)
                if cl.kind == "start":  # Problems too (F21), not on every idle check
                    self._event(
                        err.code, "warn", f"{step.device}: {err.what}", step.device, why=err.why
                    )  # the reason, not the catalog's (2026-10-16)
            speed = r.get("decode_speed")
            if speed is not None:
                lines.append(f"{step.device} decodes at {speed:g}x")
        return lines

    def _probe(self, s: PrecheckStep) -> str:
        """F17 step 5: the module's short test send reaches every receiver (NET-003)."""
        steps_of = getattr(self._rules, "probe_steps", None)
        if steps_of is None:
            s.state = "skipped"
            return "no test send for this module"
        if self._on_broadcast:  # the sender is busy sending; the warm-up checks the path
            s.state = "skipped"
            return "the broadcast is on: its stream is the test (F18)"
        try:
            got = self._probe_once(steps_of)
        except DeviceError as e:
            fallback = getattr(self._rules, "probe_fallback", None)
            later = fallback(self._plan) if fallback and e.code == "ZL-NET-003" else None
            if later is None or not self._nat:
                raise
            self._plan = later  # S-18 Auto: no multicast came through the NAT (iptv 4.5)
            self._event("ZL-INFO", "info", "No multicast through the router: trying primed")
            got = self._probe_once(steps_of)
        self._adopt_arrivals()
        path = self._path()
        if path and path.get("path") == "primed":
            for w in self._wireless.values():  # unicast: M2U means nothing here (K-14)
                w["m2u"] = {"state": "n/a", "why": "primed unicast"}
        return "packets: " + ", ".join(got) + (f"; {path['note']}" if path else "")

    def _mode_detected(self) -> str | None:
        """02 10 ``dut.mode_detected``: no router in the test (a LAN-side sender, or a
        baseline, GAP-113) has none."""
        if self._nat:
            return "NAT"
        return None if self._lan_sender or self._baseline else "Bridge"

    def _path(self) -> dict[str, Any] | None:
        if self._baseline:  # 05 4.6.3 (GAP-113): no router, so no IGMP proxy or priming
            return {"path": "direct", "note": "direct (baseline, no router)"}
        note_of = getattr(self._rules, "path_note", None)
        return dict(note_of(self._plan)) if note_of and self._plan is not None else None

    def _probe_once(self, steps_of: Any) -> list[str]:
        listen, send, result = steps_of(self._plan)
        replies = [self._device_send(step) for step in listen]
        for step in send:
            self._device_send(step)
        got = []
        for step, reply in zip(result, replies, strict=True):
            step = dataclasses.replace(step, params={**step.params, "probe_id": reply["probe_id"]})
            got_back = self._device_send(step)
            if step.device in self._by_arrival:  # where the packets came in (02 4, GAP-115 ②)
                self._arrivals[step.device] = got_back
            count = int(got_back.get("packets") or 0)
            m2u = got_back.get("m2u")
            if isinstance(m2u, dict) and step.device in self._wireless:
                self._wireless[step.device]["m2u"] = {k: m2u.get(k) for k in ("state", "why")}
            if count == 0:
                raise DeviceError(step.device, ZlError(
                    "NET-003", f"{step.device} received no test packets",
                    "The short multicast test stream did not arrive.",
                    "Check the router's IGMP setting and TTL; on Wi-Fi check the connection "
                    "and multicast-to-unicast.", detail=str(step.params),
                ))  # fmt: skip
            got.append(f"{step.device} {count}" + (
                f" (M2U {m2u['state']})" if isinstance(m2u, dict) else ""
            ))  # fmt: skip
        return got

    def _device_send(self, step: Any) -> dict[str, Any]:
        try:
            return self._send(step)
        except ZlError as e:
            if isinstance(e, DeviceError):
                raise
            raise DeviceError(step.device, e) from e

    @contextlib.contextmanager
    def while_idle(self, what: str) -> Iterator[None]:
        """Work that must not overlap a test (installs, 06 12): holds the command queue,
        so a Start waits until it is done; refused while a test runs (INS-022)."""
        with self._lock:
            if self.state is not RunState.IDLE:
                raise ZlError(
                    "INS-022", "A test is running", f"{what} now would disturb it.",
                    "Stop the test first.",
                )  # fmt: skip
            yield

    def prepare_ahead(self, params: dict[str, Any]) -> dict[str, Any]:
        """02 2-4: make a module's preparations before a test (``zeroloss prepare``)."""
        with self._lock:
            if self.state is not RunState.IDLE:
                raise ZlError(
                    "RUN-012", "Preparing is possible only between tests", f"It is {self.state}."
                )
            if self._bc is not None:  # making a video beside a live broadcast disturbs it
                raise ZlError("RUN-012", "Not while the broadcast is on",
                              "Making a video takes the sender PC's CPU.",
                              "Stop the broadcast first, or pick the movie later.")  # fmt: skip
            rules = load_run_entry(self.modules_dir, str(params.get("module", "")))
            ahead = getattr(rules, "prepare_ahead", None)
            if ahead is None:  # 2026-10-17: the throughput test makes nothing ahead
                return {"needed": False, "why": "this test makes nothing ahead"}
            step = ahead(params)
            client = self._client(step.device)
            result: dict[str, Any] = client.command(
                step.path, self.generation, None, timeout_s=step.timeout_s, **step.params
            )
            return result

    def prepare_state(self, module: str, device: str) -> dict[str, Any]:
        """02 2-4 GET: the device's preparation state as the module reports it."""
        rules = load_run_entry(self.modules_dir, module)
        hb = get_heartbeat(self._client(device))
        data = (hb.get("module_data") or {}).get(rules.heartbeat_key) or {}
        state: dict[str, Any] = rules.prepare_state(data)
        return state

    def _device(self, name: str) -> Device:
        """A registered PC, or this PC's own node (throughput, ``own``)."""
        if self.own is not None:
            me, _ = self.own()
            if name == me.name:
                return me
        return self.devices.get(name)

    def _client(self, name: str) -> NodeClient:
        dev = self._device(name)
        return NodeClient(dev.host, dev.port, self.boot_id, dev.name)

    def _prepare(self) -> None:
        """F17 step 5 first part: the module's preparations (IPTV: generated videos,
        iptv 1-1-2). Waits without a time limit (the node fails a stalled job, SND-020);
        Cancel (``stop``) or a lost device ends it with a rollback."""
        steps = getattr(self._rules, "prepare_steps", None)
        if steps is None:
            return
        todo = steps(self._plan)
        for step in todo:
            self._send(step)
        names = sorted({s.device for s in todo})
        try:
            while True:
                if self._cancel.is_set():
                    raise ZlError(
                        "USR-006",
                        "Start cancelled",
                        "Cancel was pressed while the videos were being made.",
                        "The sender PC goes on making them: Start again waits only for "
                        "what is left.",
                    )
                for w in list(self._watch.values()):  # every row stays fresh (GAP-123:
                    if w.device.name not in names:  # the receiver read "Last update 15 s
                        with contextlib.suppress(ZlError):  # ago" while a movie was made)
                            self._heartbeat(w)
                states = []
                for name in names:
                    self._asked(name)
                    states.append(
                        self._rules.prepare_progress(self._plan, self._watch[name].module_data)
                    )
                failed = next((s for s in states if s.get("error")), None)
                if failed:
                    raise ZlError(
                        "SND-020",
                        "Could not prepare the videos",
                        str(failed["error"]),
                        "Check the sender PC (disk space, ffmpeg) and start again.",
                    )
                busy = next((s for s in states if not s.get("done")), None)
                if busy is None:
                    return
                self.preparing = {k: busy.get(k) for k in ("percent", "left_s", "text")}
                self._cancel.wait(1.0)
        finally:
            self.preparing = None

    def _asked(self, name: str) -> dict[str, Any]:
        """A heartbeat during the start that names the PC when it fails: its Action
        Required row then closes when that PC answers again (2026-10-10: "Restart the PC"
        opened with no PC and stayed Waiting)."""
        try:
            return self._heartbeat(self._watch[name])
        except DeviceError:
            raise
        except ZlError as e:
            raise DeviceError(name, e) from e

    def module_command(self, device: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """A person's answer to a module's question during the test (Action Required)."""
        if self._plan is not None:
            device = self._node(device)  # a tablet's question goes to the PC it hangs on
        if self.state is not RunState.RUNNING or device not in self._watch:
            raise ZlError("RUN-012", "That test is no longer running", f"It is {self.state}.")
        step = SimpleNamespace(device=device, path=path, params=params, timeout_s=15.0)
        return self._send(step)

    def _send(self, step: Any) -> dict[str, Any]:
        w = self._watch[step.device]
        params = dict(step.params)
        bands = params.get("bands")
        if isinstance(bands, list):  # a band network's password, never kept in the plan
            params["bands"] = [self._with_password(b) for b in bands]
        try:
            result: dict[str, Any] = w.client.command(
                step.path, self.generation, self.run_id, timeout_s=step.timeout_s, **params
            )
        except DeviceError:
            raise
        except ZlError as e:  # names the PC: its Action Required row can close (GAP-116)
            raise DeviceError(step.device, e) from e
        return result

    def _with_password(self, band: Any) -> Any:
        """The password typed for that network; none typed = joined without one (an
        empty box means "no password": 2026-10-17 user, "빈칸이면 암호 없음을 의미")."""
        if not isinstance(band, dict) or band.get("security") in (None, "Open"):
            return band
        password = self.secret_of(str(band.get("ssid"))) if self.secret_of else None
        if not password:
            return {**band, "security": "Open"}
        return {**band, "password": password}

    def _wait_measuring(self) -> None:
        receivers = self._nodes()
        limit = time.monotonic() + self._plan.warmup_max_s + 5
        while time.monotonic() < limit:
            # the sender is asked too: it safe-stops after 10 s without a request, and
            # 2026-10-10 it got none for 7.5-10 s after its start (GAP-153; with a slow
            # receiver the test stopped before it began)
            for w in list(self._watch.values()):
                if w.device.name not in receivers:
                    with contextlib.suppress(ZlError):
                        self._heartbeat(w)
            done = True
            for name in receivers:
                hb = self._asked(name)
                data = (hb.get("module_data") or {}).get(self._rules.heartbeat_key) or {}
                done = done and bool(self._rules.measuring(data))
            if done:
                return
            time.sleep(0.5)
        reasons = "; ".join(
            f"{n}: {self._watch[n].last_error}"
            for n in sorted(receivers)
            if self._watch[n].last_error
        )
        ready, missing = self._warmup_split(receivers)
        # 16 S-66: no stable video in warm-up is a problem too (2026-11-05: the tablets got
        # nothing from a router without M2U; the test stops before any second is kept)
        self.router_logger.trigger("no video during warm-up", ", ".join(missing or receivers))
        if ready and missing:
            # 2026-11-05 user ("PC부터 해결해"): two tablets with no video called the start
            # off and the PC that played stopped too. The test goes on with the receivers
            # that have video; the others are named (NET-003, red: nothing received)
            for name in missing:
                self._event("ZL-NET-003", "warn", f"{name}: no video after warm-up — the "
                            "test goes on; it counts as nothing received", name)  # fmt: skip
            return
        # 2026-11-06 user ("앱 버그 해결해"): it said only "receivers did not finish
        # warm-up"; which ones had no video is what a person needs
        names = ", ".join(missing or sorted(receivers))
        raise ZlError(
            "RUN-009",
            f"Start failed: no stable video at {names}",
            f"No stable stream arrived in time at {names} (none of the receivers had one).",
            "Check that the router passes the channels to them (Problems has each "
            "receiver's reason), then press Start again.",
            detail=reasons,
        )

    def _warmup_split(self, receivers: set[str]) -> tuple[list[str], list[str]]:
        """Receivers that finished warm-up and those that did not, across the PCs."""
        of = getattr(self._rules, "warmup_split", None)
        ready: list[str] = []
        missing: list[str] = []
        for name in sorted(receivers):
            try:
                hb = self._asked(name)
            except ZlError:
                missing.append(name)  # the PC does not answer: none of its receivers counts
                continue
            data = (hb.get("module_data") or {}).get(self._rules.heartbeat_key) or {}
            if of is None:
                (ready if self._rules.measuring(data) else missing).append(name)
                continue
            got = of(data, name)
            ready, missing = ready + got[0], missing + got[1]
        return ready, missing

    def _rollback(self, err: ZlError) -> None:
        if err.code == "ZL-USR-006":  # the user's own Cancel is not a problem
            self._event(err.code, "info", "Start cancelled while the videos were being made")
        else:
            device = err.device if isinstance(err, DeviceError) else ""  # its Action row (F22)
            self._event(err.code, "error", f"Start failed: {err.what}", device)
        if self.state is RunState.PREPARING:
            self._move(RunState.STOPPING)
        if self._plan is not None and self._rules is not None and self._watch:
            self._stop_devices(keep_send=self._bc is not None)  # a broadcast that is on stays
        self._write_state("finished")
        self._move(RunState.IDLE)

    # ---------------------------------------------------------------- running

    def _run_loop(self) -> None:
        """Every second: heartbeat all devices, pull new summaries, end at planned time."""
        while True:
            # only the pause itself counts as "away" (the PC slept): a round that waited
            # on a slow PC is not (2026-10-22: "The Controller was away for 5 s" every 15 s
            # while the T460's Wi-Fi scanned and its answers came 4 s late)
            paused_at = time.monotonic()
            if self._halt.wait(1.0):
                return
            slept = time.monotonic() - paused_at
            if self._stop_wanted.is_set():
                continue  # a Stop is waiting for the lock: no new round of calls
            with self._lock:
                if self.state is not RunState.RUNNING:
                    return
                if slept > AWAY_S:  # this Controller was away (asleep), not the PCs
                    self._came_back(slept)
                self._doing = "asking each PC if it is there (heartbeat)"
                lost = self._check_devices()
                if lost:
                    self._safe_stop(lost)
                    return
                if self._stop_asked:  # a node asked for the test to end (RCV-008)
                    self._move(RunState.STOPPING)
                    self._stop_reason, self._stop_asked = self._stop_asked, ""
                    self._finish(safe=False)
                    return
                self._doing = "getting the 5-minute results"
                self._pull_summaries()
                self._doing = "changing channels"
                self._auto_changes()
                elapsed = time.monotonic() - (self._measure_start or time.monotonic())
                self._doing = "the module's next steps"
                if self._module_steps(elapsed):
                    self._move(RunState.STOPPING)
                    self._stop_reason = "all_cases_done"  # every case was done
                    self._finish(safe=False)
                    return
                self._doing = "between rounds"
                # a test of cases ends when its cases are done, never at the planned time
                # (2026-10-22 user: "왜 맘대로 중간에 멈추고 fail이지?"); only far past it
                cases = getattr(self._rules, "next_steps", None) is not None
                limit = self._plan.duration_s * (CASES_OVERRUN if cases else 1)
                if elapsed >= limit:
                    self._move(RunState.STOPPING)
                    self._stop_reason = "took_too_long" if cases else "planned_time_reached"
                    self._finish(safe=False)
                    return

    def _module_steps(self, elapsed: float) -> bool:
        """12 2 ``next_steps`` / ``finished`` (a module that runs its parts one after
        another, e.g. one PC after the other): send what the module asks for now; True
        when the module says the test is done. A step that fails is told, the run goes on
        (the module asks again on the next round)."""
        nxt = getattr(self._rules, "next_steps", None)
        done = getattr(self._rules, "finished", None)
        if nxt is None and done is None:
            return False
        data = {name: w.module_data for name, w in self._watch.items()}
        for step in nxt(self._plan, data, elapsed) if nxt else []:
            try:
                self._send(step)
            except ZlError as e:
                # asked again on the next round; a PC that stays away is the heartbeat's
                # (suspect, lost) -- not a "cannot reach" a person has to act on
                self._event("ZL-INFO", "info", f"{step.device}: {step.path.rsplit('/', 2)[-2]} "
                            f"{step.path.rsplit('/', 1)[-1]} not answered ({e.what}), "
                            "asked again", step.device)  # fmt: skip
        return bool(done and done(self._plan, data))

    def _tablets_present(self, plan: dict[str, Any]) -> dict[str, Any]:
        """2026-10-30 user ("고쳐"): a tablet ticked for the test but not on its PC's USB
        was measured anyway (every case "device not found") and stopped the test on an
        Action Required. Which tablets each PC has now, for the module to leave the
        others out (said in the result). Only PCs with a tablet ticked are asked."""
        ticked = (plan.get("settings") or {}).get("S-65")
        hosts = [
            d
            for d, cards in (ticked or {}).items()
            if isinstance(cards, dict) and any(str(k).startswith("tablet:") for k in cards)
        ]
        if not hosts:
            return {}
        present: dict[str, list[str]] = {}
        for name in hosts:
            try:
                dev = self.devices.get(name)
                got = NodeClient(dev.host, dev.port, self.boot_id, dev.name).get(
                    "/v1/tablets", timeout_s=15.0)  # fmt: skip
            except ZlError:
                continue  # not known: the module keeps its tablets (they say why later)
            present[name] = [
                str(t.get("serial"))
                for t in got.get("tablets") or []
                if isinstance(t, dict) and t.get("state") == "device"
            ]
        return {"tablets_present": present}

    def _came_back(self, away_s: float) -> None:
        """GAP-130: the Controller PC slept or stalled. The PCs went on under their hold
        (S-56); their silence while we were away is ours, so each gets WAKE_GRACE_S to
        answer again (the network comes back after a wake), then the usual rules."""
        grace = time.monotonic() + WAKE_GRACE_S - self.lost_s
        for w in self._watch.values():
            w.last_ok = max(w.last_ok, grace)
        self._event("ZL-INFO", "info", f"The Controller was away for {away_s:.0f} s; "
                    "taking the test up again", run_id=self.run_id or "")  # fmt: skip

    def _check_devices(self) -> str | None:
        """F11: 3 s suspect, 10 s lost; a new boot ID is lost at once. Returns a reason."""
        now = time.monotonic()
        for name, w in self._watch.items():
            try:
                hb = self._heartbeat(w)
            except ZlError:
                silent = now - w.last_ok
                # a module that slows the links itself (throughput: a 10 Mbps half-duplex
                # case filled the only way to the WAN-side PC, 2026-10-29 "no answer for
                # 10 s" stopped the test) gives its PCs longer before they count as lost
                longer = getattr(self._rules, "lost_after_s", None)
                limit = max(self.lost_s, float(longer(self._plan, name))) if longer else self.lost_s
                if silent >= limit:
                    return f"{name}: no answer for {silent:.0f} s"
                if silent >= self.suspect_s and not w.suspect:
                    w.suspect = True
                    self._event("ZL-DSC-014", "warn", f"{name} not answering (suspect)", name)
                continue
            if hb.get("boot_id") != w.boot_id:
                return f"{name}: restarted"
            waits = getattr(self._rules, "may_be_idle", None)  # 2026-10-17: its turn is later
            turn_later = bool(waits and waits(self._plan, name))
            if hb.get("state") == "IDLE" and not self._stop_asked and not turn_later:  # GAP-100
                # (a node that said why, e.g. disk full RCV-008, ends the run normally)
                return f"{name}: stopped its part of the test (node is IDLE)"
            if w.suspect:  # answering again: the problem is over (02 8 clears)
                self._event("ZL-INFO", "info", f"{name} answers again", name, clears=["ZL-DSC-014"])
            w.suspect = False
        return None

    def _hold_s(self) -> int | None:
        """S-56 (GAP-130): during a test, how long the PCs may go on without this
        Controller — to the planned end and 10 minutes more; None = the 10 s rule."""
        if self.state is not RunState.RUNNING or self._plan is None:
            return None
        chosen = (self._raw_plan.get("settings") or {}).get("S-56", CORE_DEFAULTS["S-56"])
        if chosen != CORE_DEFAULTS["S-56"]:
            return None
        elapsed = time.monotonic() - (self._measure_start or time.monotonic())
        return max(0, int(self._plan.duration_s - elapsed) + HOLD_MARGIN_S)

    def _heartbeat(self, w: DeviceWatch) -> dict[str, Any]:
        t0 = time.monotonic()
        # 1 s only while a test runs (a PC lost is told fast); the start's checks wait
        # like a command: a slow PC or a Wi-Fi that scans is not a lost one (2026-10-23:
        # Start stopped at step 3 "t460-rx: no answer in 1 s", reached over its Wi-Fi)
        wait = HEARTBEAT_TIMEOUT_S if self.state is RunState.RUNNING else CHECK_TIMEOUT_S
        # awake (and time sync held) only for a test, not for the idle check every few
        # seconds (2026-10-23 receiverPC: the T460's NTP was held 6 min before Start)
        testing = self.state in (RunState.PREPARING, RunState.RUNNING)
        hb = get_heartbeat(w.client, t0, self._hold_s(), timeout_s=wait, awake=testing)
        w.rtts_ms.append(round((time.monotonic() - t0) * 1000, 2))
        del w.rtts_ms[:-3600]  # keep the last hour
        w.last_ok = time.monotonic()
        w.module_data = (hb.get("module_data") or {}).get(
            getattr(self._rules, "heartbeat_key", ""), {}
        )
        links = [x for x in hb.get("links") or [] if isinstance(x, dict)]
        self._note_wifi(w, links)
        w.links = links
        if self._links is not None:
            elapsed = time.monotonic() - self._measure_start if self._measure_start else 0.0
            for ev in self._links.update(w.device.name, links, elapsed):
                ev.data["run_id"] = self.run_id or ""
                self.bus.publish(ev)
        if self._rules is not None and self._plan is not None:
            w.row = self._rules.live_row(self._plan, w.module_data, w.memo)
            rows_of = getattr(self._rules, "receiver_rows", None)
            w.relayed = rows_of(self._plan, w.module_data, w.memo) if rows_of else {}
            self._keep_seconds(w.device.name, w.module_data)
        self.devices.note_links(w.device.name, links)
        self._forward_events(w, hb.get("events") or [])
        return hb

    def _advise_sender_wifi(self, info: dict[str, Any], seen: str) -> None:
        """NET-025: the sender PC has a Wi-Fi card connected that the test does not use --
        a popup that advises turning it off; nothing is turned off by ZeroLoss (2026-10-22
        user: "팝업만 띄워 툴이나 너가 하지말고 사용자가 끄고 싶으면 끄게"). Once a test."""
        if getattr(self, "_sender_wifi_told", False) or self._plan is None:
            return
        test_ip = str((self._raw_plan or {}).get("nics", {}).get(self._plan.sender) or "")
        on = [x for x in info.get("links") or [] if isinstance(x, dict) and x.get("wifi")
              and x.get("ip") and str(x.get("ip")) != test_ip]  # fmt: skip
        if not on:
            return
        self._sender_wifi_told = True
        cards = ", ".join(f"{x.get('nic')} ({x.get('ip')})" for x in on)
        why = f" It already sent late: {seen}" if seen else ""
        self._event("ZL-NET-025", "warn", f"{self._plan.sender} has Wi-Fi on: {cards}. Turning "
                    f"it off during the test is advised (it can hold up sending).{why}",
                    self._plan.sender)  # fmt: skip

    def _forward_events(self, w: DeviceWatch, events: list[Any]) -> None:
        """The node's warnings and errors go on our bus too (02 5 ``events``, 20 GAP-40)."""
        # this PC's own node (throughput) shares our bus (12 4-1): its events are on it
        # already, and its heartbeat carries ours too. 2026-10-17: forwarding them again
        # put every PC's warning back on the bus as "this-pc", round after round (43,433).
        own = self.own is not None and w.device.name == self.own()[0].name
        for e in events:
            if not isinstance(e, dict):
                continue
            if own:
                if not e.get("device") and not (e.get("data") or {}).get("from_node"):
                    self._note_trouble(w.device.name, str(e.get("code", "")),
                                       e.get("data") or {})  # fmt: skip
                continue
            self._note_trouble(w.device.name, str(e.get("code", "")), e.get("data") or {})
            if e.get("code") == "ZL-SND-025" and w.device.name == self._plan.sender:
                self._advise_sender_wifi({"links": w.links or []}, str(e.get("text", "")))
            clears = (e.get("data") or {}).get("clears")
            if e.get("level") not in ("warn", "error") and not clears:
                continue
            level: Level = (
                "error" if e["level"] == "error" else "warn" if e["level"] == "warn" else "info"
            )
            text = str(e.get("text", ""))
            if e.get("code") == "ZL-NET-015" and getattr(self._rules, "changes_links", False):
                # the test sets the cards' speeds: a card goes down and up with each one
                level, text = "info", f"{text} (the test changes link speeds, only noted)"
            elif e.get("code") == "ZL-NET-015" and not self._on_test_nic(w.device.name, e):
                level, text = "info", f"{text} (not a test network card, only noted)"
            data = {**(e.get("data") or {}), "from_node": True, "run_id": self.run_id or ""}
            ev = Event(str(e.get("code", "ZL-INFO")), level, text, device=w.device.name, data=data)
            self.bus.publish(ev)
            if level == "error":
                w.last_error = f"{ev.code} {text}"
            if ev.code == "ZL-RCV-008" and level == "error":
                self._stop_asked = f"disk_low: {w.device.name}"  # F10 automatic stop
            if ev.code == "ZL-RCV-009" and level == "error":  # no file can be written
                self._stop_asked = f"file_write_failed: {w.device.name}"

    def _on_test_nic(self, device: str, event: dict[str, Any]) -> bool:
        """09 NET-015: a link event of a NIC the test does not use (the internet Wi-Fi of
        a PC) is no problem of the test. Unknown = ours, so nothing is hidden."""
        check = getattr(self._rules, "on_test_nic", None)
        if check is None or self._plan is None:
            return True
        return bool(check(self._plan, device, event.get("data") or {}))

    # ---------------------------------------------------------------- trouble windows (05 5.3)

    def _note_trouble(self, device: str, code: str, data: dict[str, Any]) -> None:
        """Open, extend or close this device's trouble windows (Controller time)."""
        of = getattr(self._rules, "trouble_of", None)
        if of is None or self._plan is None:
            return
        now = time.monotonic()
        mine = self._windows.setdefault(device, {})
        for cleared in data.get("clears") or []:
            for kind_of, ws in mine.items():
                for win in ws:
                    if win[1] is None and win[2] == cleared:
                        win[1] = now + (SENDER_AFTER_S if kind_of == "sender"
                                        else TROUBLE_AFTER_S)  # fmt: skip
        got = of(self._plan, device, code, data)
        if got is None:
            return
        kind, until_cleared = got
        before, after = ((SENDER_BEFORE_S, SENDER_AFTER_S) if kind == "sender"
                         else (TROUBLE_BEFORE_S, TROUBLE_AFTER_S))  # fmt: skip
        need = int((getattr(self._rules, "trouble_min_run", None) or {}).get(kind, 1))
        if need > 1 and not until_cleared:
            # 2026-10-09 (the RealTest reference: a floor counts after 3 windows in a row):
            # a kind the module marks counts only after ``need`` seconds in a row
            count, last_at = self._runs.get((device, kind), (0, -1e9))
            count = count + 1 if now - last_at <= RUN_STEP_S else 1
            self._runs[(device, kind)] = (count, now)
            if count < need:
                return
        ws = mine.setdefault(kind, [])
        if until_cleared:
            if not any(win[1] is None and win[2] == code for win in ws):
                ws.append([now - before, None, code])
            return
        last = ws[-1] if ws else None
        if last is not None and last[1] is not None and last[1] >= now - before:
            last[1] = now + after  # the same trouble, one more second
        else:  # a run of ``need`` seconds starts that many seconds back
            ws.append([now - before - (need - 1), now + after, code])
        del ws[:-2000]  # bounded for 100 h (11-ui 1.4)

    def _keep_seconds(self, device: str, module_data: dict[str, Any]) -> None:
        """Receivers' 1-second records with the time we got them (05 5.3 split)."""
        of = getattr(self._rules, "second_records", None)
        if of is None:
            return
        now = time.monotonic()
        t0 = self._run_t0
        for key, rec in of(self._plan, device, module_data):
            if t0 is not None:  # D39: this receiver's second e happened at about now
                samples = self._offsets.setdefault(f"{key[0]}|{key[1]}", [])
                samples.append(now - t0 - int(rec["e"]))
                del samples[:-OFFSET_SAMPLES]
            kept = self._seconds.setdefault(key, {})
            if int(rec["e"]) not in kept:
                self._note_episode(key, rec)
                self._note_logger(key[0], rec, now)
            kept.setdefault(int(rec["e"]), (now, rec))
            if len(kept) > SECONDS_KEPT:
                for e in sorted(kept)[: len(kept) - SECONDS_KEPT]:
                    del kept[e]

    def _note_logger(self, device: str, rec: dict[str, Any], now: float) -> None:
        """16 S-66..S-69: a second with a problem waits ``LOGGER_JUDGE_S`` (a sender's own
        trouble may be said a moment after its loss), then starts the Router Logger only
        when no sender, receiving-link, busy-PC or screen window covers it: the router's."""
        cfg = self.router_logger.cfg
        of = getattr(self._rules, "logger_reasons", None)
        if cfg is not None and of is not None and self.state is RunState.RUNNING:
            why = of(rec, float(cfg["jitter_ms"]))
            if why:
                self._logger_wait.append((now + LOGGER_JUDGE_S, now, device, why))
        due = [w for w in self._logger_wait if w[0] <= now]
        self._logger_wait = [w for w in self._logger_wait if w[0] > now][-100:]
        for _, at, dev, why in due:
            if self._routers(dev, at) and self.router_logger.trigger("; ".join(why), dev):
                break  # one start; the rest are counted while it logs

    def _routers(self, device: str, at: float) -> bool:
        """No trouble window says this second is the sender's or the receiving PC's."""
        node = self._node(device) if self._plan is not None else device
        sender = self._plan.sender if self._plan is not None else None
        for dev, kinds in self._windows.items():
            for kind, ws in kinds.items():
                if dev != node and not (dev == sender and kind.startswith("sender")):
                    continue
                if any(a <= at and (b is None or at <= b) for a, b, _ in ws):
                    return False
        return True

    def _note_episode(self, key: tuple[str, int], rec: dict[str, Any]) -> None:
        """GAP-138 (user: "loss 45515 for the whole test, but when? older than 10 minutes
        I can only guess"): a second is trouble by the module's columns (``bad``/``warn``
        above 0, ``bad_zero`` at 0, a ``text`` value not ``ok``); seconds within
        ``EPISODE_GAP_S`` make one stretch, kept for the whole run."""
        if self._trouble_cols is None:
            view_of = getattr(self._rules, "live_view", None)
            view = view_of(self._plan) if view_of and self._plan is not None else {}
            self._trouble_cols = [c for c in view.get("columns") or [] if isinstance(c, dict)]
        marks, values = [], {}
        for c in self._trouble_cols:
            v = rec.get(str(c.get("key")))
            if v is None:
                continue
            if c.get("text"):
                if c.get("ok") and v not in c["ok"]:
                    marks.append(f"{c.get('label')} {v}")
                continue
            if not isinstance(v, int | float) or isinstance(v, bool):
                continue
            values[str(c["key"])] = v
            if (c.get("bad") or c.get("warn")) and v > 0:
                marks.append(str(c.get("label")))
            elif c.get("bad_zero") and v == 0:
                marks.append(f"no {c.get('label')}")
        if not marks:
            return
        e = int(rec["e"])
        eps = self._episodes.setdefault(key, [])
        last = eps[-1] if eps else None
        if last is None or e - int(last["end_e"]) > EPISODE_GAP_S:
            last = {"start_e": e, "end_e": e, "seconds": 0, "what": [], "values": {}}
            eps.append(last)
            del eps[:-EPISODES_KEPT]
        last["end_e"], last["seconds"] = e, int(last["seconds"]) + 1
        last["what"] = sorted({*last["what"], *marks})
        evidence_of = getattr(self._rules, "second_evidence", None)  # GAP-149
        if evidence_of is not None:
            seen = list(last.get("evidence") or [])
            for line in evidence_of(rec):
                if line not in seen and len(seen) < EVIDENCE_KEPT:
                    seen.append(line)
            last["evidence"] = seen
        for c in self._trouble_cols:
            k, how = str(c.get("key")), c.get("cell")
            if k not in values:
                continue
            old = last["values"].get(k)
            v = values[k]
            last["values"][k] = (
                v if old is None
                else old + v if how == "sum"
                else max(old, v) if how == "max"
                else min(old, v) if how == "min"
                else v
            )  # fmt: skip

    def _episode_notes(self, key: str, ep: dict[str, Any]) -> list[str]:
        """Warnings and errors of the run around a stretch (±5 s), newest last."""
        samples = self._offsets.get(key) or []
        if not samples:
            return []
        off = statistics.median(samples)
        a, b = int(ep["start_e"]) + off - 5, int(ep["end_e"]) + off + 5
        with self._run_log_lock:
            near = [
                x for x in self._run_events
                if x.get("level") in ("warn", "error") and a <= float(x.get("run_s", -1)) <= b
            ]  # fmt: skip
        return [str(x.get("text", "")) for x in near][-4:]

    def _windows_for(self, device: str) -> dict[str, list[tuple[float, float]]]:
        """The windows that apply to this receiver: the sender's for everyone, its own
        link windows for itself. Open windows run to the end."""
        out: dict[str, list[tuple[float, float]]] = {}
        sender = getattr(self._plan, "sender", None)
        for owner, kinds in self._windows.items():
            for kind, ws in kinds.items():
                own = kind in ("receiver_link", "busy", "screen") and owner == device
                if not own and (kind in ("receiver_link", "screen") or owner != sender):
                    continue  # the sender's windows count for every receiver
                end = float("inf")  # still open
                out.setdefault(kind, []).extend(
                    (float(a), end if b is None else float(b)) for a, b, _ in ws
                )
        return out

    def _judge_pending(self, force: bool) -> None:
        """A summary waits GRACE_S for late trouble events, then its cell is split (05
        5.3), written to summaries.jsonl with ``trouble`` and judged."""
        assert self._run_dir is not None
        now = time.monotonic()
        ready = [x for x in self._pending if force or now - x[0] >= GRACE_S]
        if not ready:
            return
        self._pending = [x for x in self._pending if x not in ready]
        split = getattr(self._rules, "cell_trouble", None)
        with (self._run_dir / "summaries.jsonl").open("a", encoding="utf-8") as f:
            for _, name, s in ready:
                device = str(s.get("device", name))
                if split is not None:
                    key = (device, int(s.get("ch") or 0))
                    s = {**s, "trouble": split(s, self._seconds.get(key, {}),
                                               self._windows_for(device))}  # fmt: skip
                f.write(json.dumps({"node": name, **s}, ensure_ascii=False) + "\n")
                if not in_plan(s, self._plan.duration_s):
                    continue  # the stop's tail after the planned end: kept, not judged
                self._summaries.setdefault(device, []).append(s)
                color, why = self._rules.cell_color(s, self._plan.jitter_warn_ms)
                values_of = getattr(self._rules, "cell_values", None)
                self._cells.setdefault(device, []).append(
                    {
                        "index": s.get("index"),
                        "partial": s.get("partial"),
                        "color": color.value,
                        "reasons": list(why),
                        "start_elapsed_s": s.get("start_elapsed_s"),
                        "duration_s": s.get("duration_s"),
                        "values": values_of(s) if values_of else [],  # the box's own values
                    }
                )

    def _note_wifi(self, w: DeviceWatch, links: list[dict[str, Any]]) -> None:
        """Remember Wi-Fi channel/width/band/BSSID changes of this run (02 2-1)."""
        keys = ("channel", "width_mhz", "band", "bssid")
        for x in links:
            nic, b = str(x.get("nic")), x.get("wifi")
            if not isinstance(b, dict):
                continue  # a drop: compare the next reading with the one before it
            a = w.wifi_seen.get(nic)
            w.wifi_seen[nic] = b
            if a is None:
                continue
            old, new = {k: a.get(k) for k in keys}, {k: b.get(k) for k in keys}
            if old != new:
                elapsed = time.monotonic() - self._measure_start if self._measure_start else 0.0
                w.wifi_changes.append(
                    {
                        "elapsed_s": round(elapsed, 1),
                        "ts_utc": utc_now(),
                        "nic": x.get("nic"),
                        "from": old,
                        "to": new,
                    }
                )
                del w.wifi_changes[:-100]  # a fixed size for 100 h (11-ui 1.4)

    def _pull_summaries(self) -> None:
        assert self._run_dir is not None
        for name, w in self._watch.items():
            try:
                got = w.client.get("/v1/summaries", after=w.last_seq)
            except ZlError:
                continue  # stays on the node; next pull gets it
            items = [x for x in got.get("summaries") or [] if x.get("run_id") == self.run_id]
            now = time.monotonic()  # GAP-100: a node still keeps an old run's summaries
            self._pending += [(now, name, s) for s in items]
            w.last_seq = int(got.get("last_seq", w.last_seq))
        self._judge_pending(force=False)
        self._write_state("running")

    # ---------------------------------------------------------------- stopping

    def _safe_stop(self, reason: str) -> None:
        name = reason.split(":", 1)[0]
        w = self._watch.get(name)
        last_ok = None
        if w is not None:  # the monotonic clock of the last answer, as UTC
            ago = time.monotonic() - w.last_ok
            last_ok = (datetime.now(UTC) - timedelta(seconds=ago)).isoformat(
                timespec="milliseconds").replace("+00:00", "Z")  # fmt: skip
        self.fault = {"device": name if w else None, "reason": reason, "at_utc": utc_now(),
                      "last_ok_utc": last_ok, "run_id": self.run_id}  # fmt: skip
        self._event("ZL-DSC-014", "error", f"Device lost: {reason} — stopping everything")
        self._move(RunState.SAFE_STOP)
        self._stop_reason = f"device_lost: {reason}"
        self._finish(safe=True)

    def _finish(self, safe: bool) -> dict[str, Any]:
        self._halt.set()
        if self._loop is not None and self._loop is not threading.current_thread():
            self._lock.release()  # let the loop see the state change and exit
            try:
                self._loop.join(5)
            finally:
                self._lock.acquire()
        always_on = bool(getattr(self._rules, "always_on", lambda p: False)(self._plan))
        self._stop_devices(keep_send=always_on and not safe)  # F18: a safe stop ends it all
        if safe:
            self._end_broadcast("the test stopped safely")
        self._pull_summaries()
        self._judge_pending(force=True)  # nothing waits after the stop (05 5.3)
        self._move(RunState.REPORTING)
        measured = time.monotonic() - (self._measure_start or time.monotonic())
        completed = not safe and self._stop_reason in ("planned_time_reached", "all_cases_done")
        result = judge_run(
            self._rules,
            self._plan,
            self._summaries,
            planned_s=self._plan.duration_s,
            measured_s=measured,
            completed=completed,
            wireless=self._wireless,
        )
        if self._not_via and result.get("verdict") == "PASS":  # 09 NET-022: never a PASS
            result["verdict"] = "INCOMPLETE"
            result["verdict_details"] = [*result.get("verdict_details", []),
                                         "not through the router under test: "
                                         + "; ".join(self._not_via)]  # fmt: skip
        result.update(
            run_id=self.run_id,
            build=self.build,
            verdict_reason=self._stop_reason,
            devices_rtt_ms={
                n: (max(w.rtts_ms) if w.rtts_ms else None) for n, w in self._watch.items()
            },
            **self._report_fields(result),
        )
        assert self._run_dir is not None
        result.update(self._first_analysis())
        write_report(self._run_dir, result, self._raw_plan)
        self._write_state("finished")
        self.result = result
        self._event("ZL-INFO", "info", f"Run {self.run_id}: {result['verdict']}")
        self._measure_start = None
        self._move(RunState.FAULT if safe else RunState.IDLE)
        self._on_finished()
        self._on_report(str(self.run_id), list(self._watch))  # after the report, never before
        return result

    @property
    def _lan_sender(self) -> bool:
        """06 10.4 comparison test: the sender is on the router LAN side."""
        return (self._raw_plan or {}).get("sender_side") == "lan"

    def _sources_report(self, files: dict[str, Any]) -> dict[str, Any]:
        """02 10 ``source`` / ``source_fallback`` from the module (12 2 ``report_source``)."""
        of = getattr(self._rules, "report_source", None)
        if of is not None and self._plan is not None:
            data = {n: w.module_data for n, w in self._watch.items()}
            return dict(of(self._plan, data))
        return {"source": {str(ch): display_name(str(f)) for ch, f in files.items()},
                "source_fallback": []}  # fmt: skip

    def _report_fields(self, result: dict[str, Any]) -> dict[str, Any]:
        """summary.json fields beyond the verdict (02 10). Not measured yet -> null."""
        raw = self._raw_plan or {}
        files = raw.get("files") or {}
        problems: dict[str, Any] = {"problems": [], "alarm_stats": None, "human_actions": []}
        if self._problems_report is not None:
            problems = self._problems_report(str(self.run_id), float(result.get("actual_s") or 0))
        return {
            **problems,
            "recovered": False,  # a recovery report after a crash sets it (F10, CTL-003)
            "dut": {  # F14 input fields come with the settings screen (P3)
                "model": None,
                "firmware_build": None,
                "mode_user": None,
                "mode_detected": self._mode_detected(),
                "wan_ip": self._wan_ip,
                "wan_subnet": self._wan_subnet,
                "wan_ip_way": self._wan_way_used,
                "igmp_note": None,
                "m2u_note": None,
                "multicast_path": "lan" if self._lan_sender else (self._path() or {}).get("path"),
                "through_router": not self._not_via,  # 09 NET-022 (GAP-99 2)
                "not_through_router_why": "; ".join(self._not_via) or None,
            },
            **self._sources_report(files),
            "channel_change": self._channel_report(),
            "test_network": "baseline" if self._baseline else "router",
            "receiver_baseline": self._baseline_report(result),
            "sender_mode": "always_on"
            if getattr(self._rules, "always_on", lambda p: False)(self._plan)
            else "test_linked",
            "broadcast_since": self._bc_since,
            "settings_changed": dict(raw.get("settings") or {}),
            "results_web": self._web_traffic(str(self.run_id)) if self._web_traffic else None,
            "receiver_side_issues": sum(
                d["colors"].get("purple", 0) for d in result.get("devices", [])
            ),
            "measurement_trust": None,  # F25 (P3)
            "changes_since_previous": None,  # F24 (P3)
            "versions": {
                "controller": self.build,
                "agents": {n: w.build for n, w in self._watch.items()},
                "app": None,
                "tools": None,
            },
        }

    def _baseline_report(self, result: dict[str, Any]) -> dict[str, Any]:
        """02 10 ``receiver_baseline``: a baseline run records each receiver that ran to
        its planned end (05 4.6.3); every report shows the last one of its receivers."""
        of = getattr(self._rules, "baseline_of", None)
        if of is None or self._plan is None:
            return {}
        out: dict[str, Any] = {}
        done = self._stop_reason in ("planned_time_reached", "all_cases_done")  # no reason yet
        keep: dict[str, dict[str, Any]] = {}
        for device in self._rules.receiver_devices(self._plan):
            v = of(self._plan, device, self._summaries.get(device, []))
            if not self._baseline:
                out[device] = self.baselines.get(v["key"])
                continue
            rec = {
                "verified": result.get("verdict") == "PASS" and v["loss"] == 0
                and v["playback_issues"] in (0, None),
                "run_id": self.run_id, "date_utc": utc_now(), "player": v["player"],
                "loss": v["loss"], "playback_issues": v["playback_issues"],
            }  # fmt: skip
            out[device] = rec
            if done:  # an INCOMPLETE baseline proves nothing: not kept
                keep[v["key"]] = rec
        if keep:
            self.baselines.put(keep)
        return out

    def _channel_report(self) -> dict[str, Any] | None:
        of = getattr(self._rules, "channel_change", None)
        if of is None or self._plan is None:
            return None
        got: dict[str, Any] | None = of(
            self._plan, [x for v in self._summaries.values() for x in v]
        )
        return got

    def _stop_devices(self, keep_send: bool = False) -> None:
        """Stop order from the module; every step is tried even if one fails."""
        stages = (
            self._rules.stop_steps(self._plan, keep_send=True)
            if keep_send
            else self._rules.stop_steps(self._plan)
        )
        for stage in stages:
            for step in stage:
                if step.device not in self._watch:
                    continue
                try:
                    self._send(step)
                except ZlError as e:
                    if e.code.endswith("DSC-011"):  # it stopped by itself first (CTL-008): clean
                        self._event("ZL-INFO", "info", f"{step.device} had already stopped "
                                    "by itself", step.device)  # fmt: skip
                        continue
                    self._event(e.code, "error", f"Stop on {step.device} failed: {e.what}")

    # ---------------------------------------------------------------- helpers

    def _move(self, to: RunState) -> None:
        old = self.state
        if to not in ALLOWED[old]:
            raise ZlError("RUN-012", f"The test cannot go to {to} now", f"It is {old}.")
        self.state = to
        self._event("ZL-INFO", "info", f"run: {old} -> {to}")
        if self.on_change:
            self.on_change(old, to)

    def _write_state(self, status: str) -> None:
        """run-state.json (02 9): the next start finds an unfinished run here (F10)."""
        if self._run_dir is None:
            return
        state = {
            "run_id": self.run_id,
            "status": status,
            "planned_s": getattr(self._plan, "duration_s", None),
            "started_utc": self._started_utc,
            "last_saved_utc": utc_now(),
            "devices": sorted(self._watch),
            "settings": dict((self._raw_plan or {}).get("settings") or {}),
            "generation": self.generation,
            "sender_mode": "test_linked",  # always-on broadcast (F18) is P3
            "controller_boot_id": self.boot_id,
        }
        tmp = self._run_dir / "run-state.tmp"
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._run_dir / "run-state.json")

    def _next_generation(self, at_least: int = 0) -> int:
        """Grows on every Controller start and reset, so old commands are refused (F11)."""
        path = self.reports_dir / ".generation"
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        try:
            gen = int(path.read_text(encoding="ascii")) + 1
        except (OSError, ValueError):
            gen = 1
        gen = max(gen, at_least)
        path.write_text(str(gen), encoding="ascii")
        return gen

    def _catch_up_generation(self, name: str, health: dict[str, Any]) -> None:
        """02 4 (GAP-111, real case): a newly installed Controller, or one whose data moved,
        starts its count again while the nodes keep the highest they saw, so every command
        was refused as old (DSC-011). An idle node's count is taken over once; a busy one
        is never overtaken here (taking over a running test is F11, by a person)."""
        node_gen = health.get("generation")
        if (isinstance(node_gen, int) and not isinstance(node_gen, bool)
                and node_gen > self.generation and health.get("state") == "IDLE"):  # fmt: skip
            # ">" (2026-10-16): a node at our own count took it from our commands; ">="
            # stepped the count every idle check, 1,319 "goes on from" lines overnight
            self.generation = self._next_generation(at_least=node_gen + 1)
            self._event("ZL-INFO", "info", f"{name} had command generation {node_gen}: "
                        f"this Controller goes on from {self.generation}", name)  # fmt: skip

    def _step_done(self, step: Any) -> None:
        """iptv 2.4: a start step finished (a receiver joined at this run time)."""
        self._event("ZL-INFO", "info", f"{step.device}: {step.path.rsplit('/', 2)[-2]} started",
                    step.device, step=step.path, ch=step.params.get("ch"))  # fmt: skip

    def _log_run_event(self, e: Event) -> None:
        """02 8: the run folder's events.log, every event with the run clock ``run_s``.
        Kept in memory for the analysis until the run folder exists."""
        with self._run_log_lock:
            t0 = self._run_t0
            if t0 is None:
                return
            if len(self._run_events) < RUN_EVENTS_MAX:
                self._run_events.append({**e.to_dict(), "run_s": round(time.monotonic() - t0, 3)})
            if self._run_dir is None:
                return
            todo = self._run_events[self._events_written :]
            try:
                with (self._run_dir / "events.log").open("a", encoding="utf-8") as f:
                    for line in todo:
                        f.write(json.dumps(line, ensure_ascii=False) + "\n")
                self._events_written += len(todo)
            except OSError:
                pass  # the report still has everything in memory

    def _first_analysis(self) -> dict[str, Any]:
        """iptv 2.4 first pass (5-minute summaries and events), timeline.json for the
        second pass after the records come. Never changes the verdict."""
        assert self._run_dir is not None
        offsets = {k: round(statistics.median(v), 3) for k, v in self._offsets.items() if v}
        _atomic_json(self._run_dir / "timeline.json", offsets)
        data = {"summaries": self._summaries, "events": list(self._run_events), "seconds": None,
                "offsets": offsets}  # fmt: skip
        with self._run_log_lock:
            self._run_t0 = None  # the run's events end with its report
        return analysis_fields(self._rules, self._plan, data, "report")

    def _event(self, code: str, level: Level, text: str, device: str = "", **data: Any) -> None:
        payload = {"run_id": self.run_id or "", **data}
        self.bus.publish(Event(code, level, text, device=device, data=payload))


def _local_ips() -> dict[str, str]:
    """This PC's physical cards with an IPv4 address (NET-019)."""
    return {x.nic: x.ip for x in read_links(wifi_of=lambda nic: None) if x.ip}


MEM_WARN_PCT, MEM_STOP_PCT = 10.0, 5.0  # SYS-004 free memory (2026-09-26 user decision)
# 05 5.3 "문제 구간으로 나누기" (2026-09-26 사용자 결정)
TROUBLE_BEFORE_S = 1.0
TROUBLE_AFTER_S = 2.0
# the sender's own trouble (2026-10-31 R20261031-214605: a 3 % netem on its card was
# judged the sender's for 5 s, but 101-227 lost packets fell just outside and stayed red;
# user: "아직 해결안됐어?"): the sender reads its card once a second, so it can start up to
# a second before the first report, and the receivers count a loss 1-2 s after it (the
# reorder window, a tablet's pull), so its window reaches further each way
SENDER_BEFORE_S = 2.0
SENDER_AFTER_S = 4.0
GRACE_S = 3.0  # a cell waits this long for late trouble events before it is judged
LOGGER_JUDGE_S = GRACE_S  # a problem waits as long before it starts the Router Logger
SECONDS_KEPT = 900  # 1-second records per receiver: a 5-minute cell plus room
RUN_STEP_S = 1.5  # trouble events this close are seconds in a row (trouble_min_run)
STOP_WAIT_LOG_S = 5.0  # a Stop that waited longer says what for (2026-10-09)
EVIDENCE_KEPT = 6  # evidence lines per trouble stretch (GAP-149)
EPISODE_GAP_S = 2  # trouble seconds this close make one stretch (GAP-138)
EPISODES_KEPT = 1000  # per receiver, for 100 h (11-ui 1.4)
LIVE_SECONDS = 600  # the live monitor's charts: the last 10 minutes (GAP-119)
HOLD_MARGIN_S = 600  # S-56: the PCs go on 10 min past the planned end without us (GAP-130)
AWAY_S = 5.0  # a 1-second loop that took this long: the Controller was away (asleep)
CASES_OVERRUN = 4  # a test of cases stops only this many times past its estimate
CHECK_TIMEOUT_S = 10.0  # a start check's question to a PC: a slow PC still answers
AWAKE_ASK_S = 60  # a test's heartbeat asks each PC to stay awake this long (renewed every 1 s)
WAKE_GRACE_S = 60.0  # after we come back, time for the network to answer again


RESET_TIMEOUT_S = 60.0  # a node's safe stop + leftovers + clean check (F11 Reset All)
REVIVE_WAIT_S = 90.0  # a revived node: until the Controller can reach it again (Reset All)
WIFI_JOIN_S = 30.0  # the node waits this long for an address (03 F17)


def join_card(cards: list[dict[str, Any]], host: str) -> tuple[str, str | None]:
    """Which Wi-Fi card joins the test Wi-Fi (03 F17 "무선 수신 PC", GAP-102): a free card,
    else one already on the test network (the device's registered address /24), else
    the first card — then also the SSID it leaves (that PC loses that network)."""
    free = [c for c in cards if not c.get("ssid")]
    if free:
        return str(free[0]["nic"]), None
    try:
        net = ipaddress.ip_network(f"{host}/24", strict=False)
    except ValueError:  # a host name, not an address
        net = None
    for c in cards:
        try:
            if net is not None and c.get("ip") and ipaddress.ip_address(c["ip"]) in net:
                return str(c["nic"]), None
        except ValueError:
            continue
    return str(cards[0]["nic"]), str(cards[0].get("ssid"))


def _unreachable(e: ZlError, host: str) -> ZlError:
    """A PC that does not answer: when its address is on the router WAN side and this PC
    has no way there, the fix names the command to type (2026-10-22: after a restart the
    route was gone; the screen said only "check the cable")."""
    if not e.code.endswith("DSC-014") or not _in(host, WAN_B):
        return e
    fix = manual_fix(WAN_B)
    return ZlError(e.code, e.what, e.why, fix, e.detail) if fix else e


def _in(host: str, subnet: str) -> bool:
    try:
        return ipaddress.ip_address(host) in ipaddress.ip_network(subnet)
    except ValueError:
        return False


def _no_wifi(what: str, why: str) -> ZlError:
    return ZlError(
        "RCV-023", what, why,
        "Set the test Wi-Fi (zeroloss wifi set) or connect the PC by hand, then press Start.",
    )  # fmt: skip


def _same_node(name: str, device_id: str, health: dict[str, Any], build: str) -> None:
    """F17 step 1: the same PC (DSC-004), our protocol (DSC-013) and build (INS-015)."""
    if health.get("device_id") != device_id:
        raise ZlError(
            "DSC-004", f"{name} answers as a different device",
            "The IP now belongs to another PC.", "Add the device again.",
        )  # fmt: skip
    if health.get("protocol_version") != PROTOCOL_VERSION:
        raise ZlError(
            "DSC-013", f"{name} speaks protocol {health.get('protocol_version')}",
            f"This Controller speaks protocol {PROTOCOL_VERSION}.",
            f"Update {name} to build {build or 'the Controller build'}.",
        )  # fmt: skip
    theirs = health.get("build")
    if build and theirs and theirs != build:
        raise ZlError(
            "INS-015", f"{name} has build {theirs}", f"The Controller has build {build}.",
            f"Press Update PCs: it installs build {build} on {name} from this program's "
            "packages folder. If that fails, by hand on that PC: copy "
            "zeroloss-agent_*.deb from the packages folder (USB), open a terminal there and "
            "run: sudo apt install ./zeroloss-agent_*.deb",
        )  # fmt: skip


def get_heartbeat(
    client: NodeClient,
    t0: float | None = None,
    hold_s: int | None = None,
    timeout_s: float = HEARTBEAT_TIMEOUT_S,
    awake: bool = False,
) -> dict[str, Any]:
    """The one place that asks a node for its heartbeat (02 5; 20 G-14): ``seq`` is the
    Controller's monotonic ms, echoed back for the round trip. ``hold_s`` (GAP-130): how
    long the test may go on there without this Controller."""
    seq = int((time.monotonic() if t0 is None else t0) * 1000)
    extra: dict[str, Any] = {"hold_s": hold_s} if hold_s is not None else {}
    if awake:  # a test's heartbeat: every PC in it stays awake, its turn or not
        extra["awake_s"] = AWAKE_ASK_S
    hb: dict[str, Any] = client.get("/v1/heartbeat", timeout_s=timeout_s, seq=seq, **extra)
    return hb


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _wait_idle(client: NodeClient, limit_s: float = 30.0) -> None:
    """Until the node finished its safe stop (its summaries are then complete)."""
    end = time.monotonic() + limit_s
    while time.monotonic() < end:
        if client.get("/v1/health").get("state") in ("IDLE", "FAULT"):
            return
        time.sleep(0.2)
    raise ZlError("DSC-014", "The node did not finish stopping", "It stayed busy for 30 s.")


RUN_EVENTS_MAX = 100_000  # events kept for the analysis (100 h, bounded, 11-ui 1.4)
OFFSET_SAMPLES = 600  # heartbeat samples per receiver for its clock offset (D39)


def analysis_fields(rules: Any, plan: Any, data: dict[str, Any], stage: str) -> dict[str, Any]:
    """iptv 2.4 / 02 10: the module's cause candidates; a broken rule set is shown as a
    missing line, never a failed report."""
    of = getattr(rules, "analysis", None)
    if of is None:
        return {"analysis": [], "analysis_missing": [], "analysis_pass": stage}
    try:
        got = of(plan, data)
        return {"analysis": list(got["findings"]), "analysis_missing": list(got["missing"]),
                "analysis_pass": stage}  # fmt: skip
    except Exception as e:  # shown in the report, the verdict stands
        why = f"analysis failed: {e!r}"
        return {"analysis": [], "analysis_pass": stage,
                "analysis_missing": [{"rule": "all", "why": why}]}  # fmt: skip
