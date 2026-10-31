"""Node Core (L1): identity, state, events, and the common API on TCP 10992.

docs/12-architecture.md 1-0/1-1, docs/02-data.md 3-5, docs/03-features.md F11.
Every PC runs the same node; roles (controller, test senders and receivers) are modules that
add their routes under /v1/modules/<id>/ and their heartbeat ``module_data``.

Safe stop (F11, CTL-008): while STARTING, RUNNING or FAULT, no request from the
Controller for ``lost_after_s`` (S-42, default 10 s) or a restarted Controller stops
every module, confirms, and returns to IDLE. The generation is raised so commands
sent before the stop are refused. The node never resumes the old run by itself.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import queue
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from core.blocks import diag_bundle, run_storage
from core.blocks.adb import Adb
from core.blocks.arp_probe import probe as arp_probe
from core.blocks.cpu_watch import CpuWatch, top_processes
from core.blocks.discovery import Responder
from core.blocks.link_monitor import (
    Change,
    Link,
    LinkWatch,
    WifiEvents,
    WifiReader,
    ipv4_netmask,
    nic_of,
    read_links,
    wifi_events_supported,
)
from core.blocks.process_runner import stop_leftovers
from core.blocks.resource_monitor import ResourceMonitor
from core.blocks.sleep_guard import SleepGuard
from core.blocks.tablets import TabletError, Tablets
from core.blocks.time_sync import TimeSyncGuard
from core.blocks.tool_locator import ToolLocator
from core.blocks.wan_isp import WanIsp
from core.blocks.wan_reach import WanReach
from core.blocks.wifi_join import JOIN_TIMEOUT_S, WifiJoin
from core.blocks.win_wifi import WinWifi
from core.desktop_jobs import LINK_S as DESKTOP_LINK_S
from core.desktop_jobs import DesktopJobs
from core.events.bus import Event, EventBus, EventLog, Level, RecentEvents, utc_now
from core.lifecycle.state_flow import CONTROLLER_RESTARTED, State, StateFlow
from core.modules.registry import ModuleInfo, discover
from core.protocol.envelope import PROTOCOL_VERSION, CommandGuard, Request, ZlError
from core.protocol.http_server import IDLE, ApiServer, Query
from core.records.recorder import DEVICE_NAME, record_run
from core.settings import CORE_DEFAULTS

CORE_PORT = 10992
LOCAL_PORT = 10994  # this PC only: Desktop app and CLI
STREAM_QUEUE = 1000  # events a slow screen may lag behind before it is disconnected
KEEP_ALIVE_S = 15.0
LOST_AFTER_S = float(CORE_DEFAULTS["S-42"]["lost_s"])
HOLD_MAX_S = 201 * 3600  # the longest test (200 h) and a margin (GAP-130)
AWAKE_MAX_S = 3600  # a heartbeat's awake_s: at most an hour
WATCH_INTERVAL_S = 0.5
DISK_CHECK_S = 60.0  # 09 RCV-008 "1분" (G-10: one statvfs a minute)
DISK_WARN_MB, DISK_STOP_MB = 1024, 200
ACTIVE = (State.STARTING, State.RUNNING, State.FAULT)
UNKNOWN_BUILD = "0.0"


def read_build(root: Path | None = None) -> str:
    """The build stamp from the bundled VERSION file (docs/06-install.md 8)."""
    base = root or Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    try:
        return (base / "VERSION").read_text(encoding="ascii").strip() or UNKNOWN_BUILD
    except OSError:
        return UNKNOWN_BUILD


def load_device_id(data_dir: Path) -> str:
    """UUID made once and kept, so the device is the same even when its IP changes."""
    path = data_dir / "device_id"
    try:
        value = path.read_text(encoding="ascii").strip()
        uuid.UUID(value)
        return value
    except (OSError, ValueError):
        value = str(uuid.uuid4())
        data_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value, encoding="ascii")
        tmp.replace(path)
        return value


ModuleData = Callable[[], Mapping[str, Any]]


KEEP_SUMMARIES = 20_000  # a restarted node reloads at most this many (100 h x 3 ch = 3,600)


FETCHED = ".fetched"  # in a run's raw/: the Controller has it all, removed at the next start
DIAG_SKIP = ("wifi.json",)  # 02 2-13: the Controller's test Wi-Fi password never leaves

# 02 4 /v1/tablets/<action>
TABLET_ACTIONS = ("install_app", "ensure_app", "uninstall_app", "start_app", "stop_app")


class Node:
    def __init__(
        self,
        data_dir: Path,
        role: str,
        host: str = "0.0.0.0",
        port: int = CORE_PORT,
        build: str | None = None,
        modules_dir: Path | None = None,
        lost_after_s: float = LOST_AFTER_S,
        local_port: int = LOCAL_PORT,
        player_host: str = "self",
    ) -> None:
        self.data_dir = data_dir
        self.role = role
        self.build = build or read_build()
        self.device_id = load_device_id(data_dir)
        self.boot_id = str(uuid.uuid4())  # new on every start: peers see a restart
        self.bus = EventBus()
        self.recent = RecentEvents()
        self.log = EventLog(data_dir / "logs" / "events.log")
        self.bus.subscribe(self.log)
        self.bus.subscribe(self.recent)
        # 03 F20: "self" = this node starts the PC player (P2, development); "desktop" =
        # the logged-in user's Agent Desktop does (the installed service has no screen).
        if player_host not in ("self", "desktop"):
            raise ValueError(f"player_host must be self or desktop, not {player_host!r}")
        self.player_host = player_host
        self.desktop = DesktopJobs(self.bus.publish)
        self._problems: deque[Event] = deque(maxlen=10)  # the status window's last few
        self.bus.subscribe(lambda e: self._problems.append(e) if e.level != "info" else None)
        self.flow = StateFlow("node", self._state_changed)
        self.guard = CommandGuard()
        self.resources = ResourceMonitor(data_dir)
        # RCV-008: disk under the data folder, once a minute during a test (09)
        self.disk_free_mb: Callable[[], int | None] = lambda: _free_mb(data_dir)
        self.disk_usage: Callable[[Path], Any] = shutil.disk_usage  # /v1/disk (tests swap it)
        self.disk_check_s = DISK_CHECK_S
        self._disk_level = "ok"
        self.module_data: dict[str, ModuleData] = {}
        # 5-minute summaries (02 6): modules add sources; the Controller pulls with
        # GET /v1/summaries?after=<seq>. Kept on the node so a lost pull loses nothing.
        self.summary_sources: list[Callable[[], list[dict[str, Any]]]] = []
        self._summaries: list[dict[str, Any]] = []
        self._summary_seq = 0
        self._summary_lock = threading.Lock()
        # F12: also on disk, so a restarted node still gives them with the same seq
        self._summary_file = data_dir / "summaries.jsonl"
        self._load_summaries()
        self.on_controller_restart: Callable[[], None] = self._controller_restarted
        self.last_controller_request = time.monotonic()
        self._controller_ever = False  # the status window says "not connected" until then
        # F11 (2026-10-09 GAP-130, user: "이어받기"): the Controller may say in each heartbeat
        # how long the test may go on without it (to the planned end); a silent Controller
        # (asleep, restarting its network) then does not stop the test before that
        self.hold_until = 0.0
        # the Controller's test: awake while it says so, waiting for its turn too
        # (2026-10-22 user: "iptv테스트할땐 sleep안들어가게 하는데 iperf일땐 sleep들어가는거같다")
        self.awake_until = 0.0
        self.lost_after_s = lost_after_s
        self._stop_lock = threading.Lock()
        self._closing = threading.Event()
        self._watch: threading.Thread | None = None
        # NIC links read every second (05 1.8-1.9): speed renegotiation, link down,
        # Wi-Fi channel/width/AP changes. Read off the request path; heartbeat copies it.
        self.links_reader: Callable[[], list[Link]] = read_links
        self.links: list[Link] = []
        self._link_watch = LinkWatch()
        self._links_thread: threading.Thread | None = None
        self._wifi_events: WifiEvents | None = None
        self._discovery: Responder | None = None
        self.api = ApiServer(
            host, port, self.guard, self.bus, self._restart_hook, self._controller_seen
        )
        self.api.get("/v1/health", self.health)
        self.api.get("/v1/heartbeat", self.heartbeat)
        self.api.get("/v1/info", self.info)
        self.api.get("/v1/summaries", self.summaries)
        self.api.get("/v1/wifi", lambda _: self.wifi.status())
        self.api.get("/v1/wifi/scan", self.wifi_scan)  # read only: what a card sees
        self.api.command("/v1/wifi/join", self.wifi_join)
        self.api.command("/v1/reset", self.reset)
        self.api.get("/v1/wan", lambda _: self.wan.status())
        self.api.file("/v1/logs", self.logs_bundle)  # 02 4 diagnostics (20 GAP-78)
        self.api.command("/v1/stop", lambda _: self.stop_here("the Controller"))  # 02 4
        self.api.get("/v1/disk", self.disk)  # 09 SYS-010 (20 GAP-82)
        self.api.get("/v1/storage", self.storage)  # F15 (20 GAP-79)
        self.api.command("/v1/storage/delete", self.storage_delete)
        self.api.get("/v1/records/index", self.records_index)  # 02 4 (20 GAP-80)
        self.api.file("/v1/records", self.records_file)
        self.api.command("/v1/records/fetched", self.records_fetched)  # then cleaned here
        self.api.command("/v1/time/sync", self.time_sync_on)  # the screen's button
        self.api.command("/v1/wan", self.wan_set)
        self.api.command("/v1/arp_probe", self.arp_probe)  # SND-013 (F17 step 3)
        # 01-overview D3: the tablets on this PC's USB, one to three, each by its serial
        self.api.get("/v1/tablets", lambda _: self.tablets.list())
        for action in TABLET_ACTIONS:
            self.api.command(f"/v1/tablets/{action}", self._tablet_action(action))
        # 10994 shares the guard (one command queue) but does not count as the
        # Controller being alive: a local screen must not keep an orphan run going.
        self.local = ApiServer("127.0.0.1", local_port, self.guard, self.bus)
        self.local.get("/v1/health", self.health)
        self.local.get("/v1/info", self.info)
        self.local.stream("/v1/stream", self.stream)
        self.local.get("/v1/desktop/status", self.desktop_status)  # 02 2-12
        self.local.get("/v1/desktop/jobs", self.desktop_jobs)
        self.local.report("/v1/desktop/report", self.desktop_report)
        # USR-007: `zeroloss agent stop` on this PC, without a Controller; stopping twice
        # gives the same answer, so no command ID or generation is needed
        self.local.report("/v1/stop", lambda _: self.stop_here("this PC (zeroloss agent stop)"))
        self.modules: list[ModuleInfo] = discover(modules_dir) if modules_dir else []
        self.tools = ToolLocator()
        # the app this build carries for tablets; Update PCs puts it here (06 11)
        self.tablets = Tablets(Adb(lambda: self.tools.path("adb")),
                               Path(data_dir) / "tablet" / "zeroloss-receiver.apk")  # fmt: skip
        # test Wi-Fi for a receiver PC (02 4); on Windows the same contract (win_wifi)
        self.wifi: Any = WinWifi() if sys.platform == "win32" else WifiJoin(tmp_dir=data_dir)
        self.wan_reach = WanReach()  # GAP-158: the way to the sender, for this test only
        # ISP role of a sender PC (07 3.2 B); starts by itself on a router WAN port, never
        # on the Controller PC (a receiver there too, 2026-09-26: its NICs are not touched)
        auto = role != "controller" and os.environ.get("ZEROLOSS_WAN_AUTO", "1") != "0"
        self.wan = WanIsp(data_dir, self._wan_event, auto_allowed=auto)
        # SYS-005: no sleep during a test. One worker applies the latest wish, in order.
        self.sleep_guard = SleepGuard(self.procs_dir)
        # SYS-012: no clock correction during a test (2026-10-22: NTP slews read as delay
        # variation); put back at the end, after a crash at the next start
        self.time_sync = TimeSyncGuard(Path(data_dir) / "time-sync-held.json")
        self.cpu_watch = CpuWatch()  # SYS-003, during a test
        self._awake_wanted = False
        self._awake_active = False  # this node is in a test
        self._awake_asked: set[str] = set()  # wishes from the role on top (want_awake)
        self._awake_changed = threading.Event()
        self._awake_thread: threading.Thread | None = None

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        self._stop_leftovers()  # before anything new is started (SYS-007, 12 4-3)
        threading.Thread(target=self.clean_fetched, name="clean-fetched", daemon=True).start()
        self._attach_modules()
        wanted = [t for m in self.modules if m.enabled for t in m.tools]
        threading.Thread(  # versions ready before the first /v1/info (F17 step 2)
            target=self.tools.find_all, args=(wanted,), name="tools", daemon=True
        ).start()
        self.api.start()
        self.local.start()
        self._start_discovery()
        self._awake_thread = threading.Thread(target=self._awake_loop, name="sleep-guard",
                                              daemon=True)  # fmt: skip
        self._awake_thread.start()
        self.wan.start()  # keeps the router WAN addressed across tests (07 3.2 B)
        self._watch = threading.Thread(target=self._watchdog, name="controller-watch", daemon=True)
        self._watch.start()
        if self.links_reader is read_links and wifi_events_supported():
            # Wi-Fi is event driven (one `iw event`), not two `iw` runs a second
            self._wifi_events = WifiEvents(self.procs_dir, self.data_dir / "logs" / "iw-event.log")
            self._wifi_events.start()
            reader = WifiReader(self._wifi_events)
            self.links_reader = lambda: read_links(wifi_of=reader)
        self._links_thread = threading.Thread(target=self._link_loop, name="links", daemon=True)
        self._links_thread.start()
        self.bus.publish(
            Event(
                "ZL-INFO",
                "info",
                f"Node started ({self.role}, build {self.build})",
                data={"boot_id": self.boot_id, "port": self.api.port},
            )
        )
        self._restart_note()

    def _start_discovery(self) -> None:
        """06 10.2: answer discovery on UDP, same number as the node port."""
        try:
            self._discovery = Responder(self.api.port, self._discovery_answer)
        except OSError as e:  # the node works without it; the Controller can scan
            self.bus.publish(Event("ZL-INS-003", "warn", f"Discovery port (UDP) busy: {e}"))
            return
        self._discovery.start()

    def _discovery_answer(self) -> dict[str, Any] | None:
        if self.flow.state is not State.IDLE:
            return None  # never during a test (07 7)
        return {
            "device_id": self.device_id, "host": socket.gethostname(), "role": self.role,
            "build": self.build, "protocol_version": PROTOCOL_VERSION,
            "port": self.api.port, "state": self.flow.state.value,
        }  # fmt: skip

    def module_dir(self, module_id: str, kind: str) -> Path:
        """A module's own folder (12 3, 06 12): ``settings`` stays when the module is
        removed, ``cache`` goes with it. Modules never build data paths themselves."""
        if kind not in ("settings", "cache"):
            raise ValueError("kind must be settings or cache")
        path = self.data_dir / "modules" / module_id / kind
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _restart_note(self) -> None:
        """SYS-009: the Deployer restarted this node because it stopped answering; the
        note it left becomes this node's first warning (then it is removed)."""
        note = self.data_dir / "restart-note.json"  # in the node's own folder (06 4.3)
        try:
            body = json.loads(note.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        note.unlink(missing_ok=True)
        why = str(body.get("reason", "not answering")) if isinstance(body, dict) else ""
        self.bus.publish(
            Event(
                "ZL-SYS-009",
                "warn",
                f"Service was not answering and was restarted by the Deployer ({why})",
                data=body if isinstance(body, dict) else {},
            )
        )

    def stop(self) -> None:
        self._closing.set()
        if self._watch is not None:
            self._watch.join(5)
        if self._links_thread is not None:
            self._links_thread.join(5)
        if self._wifi_events is not None:
            self._wifi_events.stop()
        self.safe_stop("node shutting down", code="ZL-INFO")
        self._awake_changed.set()  # _closing is set: the worker lets the guard go and ends
        if self._awake_thread is not None:
            self._awake_thread.join(10)
        if self._discovery is not None:
            self._discovery.stop()
        self.wan.close()
        self.local.stop()
        self.api.stop()
        self.bus.publish(Event("ZL-INFO", "info", "Node stopped"))
        self.log.close()

    # ---------------------------------------------------------------- API

    def health(self, _: Query | None = None) -> dict[str, Any]:
        return {
            "build": self.build,
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "boot_id": self.boot_id,
            "role": self.role,
            "state": self.flow.state.value,
            "generation": self.guard.generation,  # 02 4 (GAP-111): a new Controller catches up
        }

    def _link_loop(self) -> None:
        while not self._closing.is_set():
            try:
                links = self.links_reader()
                changes = self._link_watch.update(links)
                self.links = links
                for c in changes:
                    ev = link_event(c)
                    if self.role == "controller" and ev.level == "error":
                        # the Controller PC's own card (its internet Wi-Fi): its events
                        # reach the bus directly, not through the forwarding that softens
                        # a PC's non-test cards -- 2026-10-23 "Link Wi-Fi went down" errors
                        # 5x during a test run the Controller PC was not measured in
                        ev = Event(ev.code, "info", f"{ev.text} (this Controller PC, only noted)",
                                   data=ev.data)  # fmt: skip
                    self.bus.publish(ev)
                self._watch_cpu()
            except Exception as e:  # a bad reading must not stop the watch
                self.bus.publish(Event("ZL-SYS-008", "warn", f"Link reading failed: {e!r}"))
            self._closing.wait(1.0)

    def heartbeat(self, q: Query) -> dict[str, Any]:
        """Echo the Controller's ``seq`` so it can measure round trip and clock offset.
        ``hold_s`` (02 5, GAP-130): how long the test may go on without the Controller."""
        seq = q.int("seq")
        hold = q.int("hold_s", 0)
        if 0 <= hold <= HOLD_MAX_S:
            self.hold_until = time.monotonic() + hold
        awake = q.int("awake_s", 0)
        if 0 < awake <= AWAKE_MAX_S:  # a PC in a test stays awake, its turn or not
            self.awake_until = time.monotonic() + awake
            self.want_awake("in a test", True)
        return {
            "seq": seq,
            "device_id": self.device_id,
            "boot_id": self.boot_id,
            "generation": self.guard.generation,
            "state": self.flow.state.value,
            "mono_ns": time.monotonic_ns(),
            "ts_utc": utc_now(),
            "role": self.role,
            "resources": self.resources.read().to_dict(),
            "power": {**self.sleep_guard.status(), "time_sync": self.time_sync.status()},
            "links": [x.to_dict() for x in self.links],
            "module_data": {name: dict(fn()) for name, fn in self.module_data.items()},
            "events": [e.to_dict() for e in self.recent.take()],
            "summaries_ready": self._collect_summaries(),
        }

    def summaries(self, q: Query) -> dict[str, Any]:
        """Summaries with ``seq`` greater than ``after``, oldest first."""
        after = q.int("after", 0)
        self._collect_summaries()
        with self._summary_lock:
            items = [x for x in self._summaries if x["seq"] > after]
        return {"summaries": items, "last_seq": self._summary_seq}

    @property
    def procs_dir(self) -> Path:
        """The one folder with the run records of every program this node started."""
        return self.data_dir / "procs"

    @property
    def _own_helpers(self) -> frozenset[str]:
        """Records of programs this running node keeps on purpose (not test programs)."""
        own = {"iw-event"} if self._wifi_events is not None else set()
        if self.sleep_guard.held:  # held during a test on purpose (SYS-005)
            own.add("sleep-guard")
        return frozenset(own)

    def _test_records(self) -> list[Path]:
        return [p for p in self.procs_dir.glob("*.json") if p.stem not in self._own_helpers]

    def _stop_leftovers(self) -> None:
        for rec in stop_leftovers(self.procs_dir, self._own_helpers):
            self.bus.publish(
                Event(
                    "ZL-SYS-007",
                    "warn",
                    f"Leftover {rec.get('name', 'program')} process stopped",
                    data={"pid": rec.get("pid"), "name": rec.get("name")},
                )
            )

    def _load_summaries(self) -> None:
        if not self._summary_file.exists():
            return
        lines = self._summary_file.read_text(encoding="utf-8").splitlines()[-KEEP_SUMMARIES:]
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a line cut by a crash
            self._summaries.append(item)
            self._summary_seq = max(self._summary_seq, int(item.get("seq", 0)))

    def _collect_summaries(self) -> int:
        """Move new summaries from the modules into the numbered store (memory and
        disk, 02 4). Returns the count."""
        with self._summary_lock:
            for source in self.summary_sources:
                for item in source():
                    self._summary_seq += 1
                    numbered = {**item, "seq": self._summary_seq}
                    self._summaries.append(numbered)
                    self._summary_file.parent.mkdir(parents=True, exist_ok=True)
                    with self._summary_file.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(numbered, ensure_ascii=False) + "\n")
            return len(self._summaries)

    def info(self, q: Query) -> dict[str, Any]:
        """Discovery facts (F14). ``seen_from_ip`` tells the Controller whether a NAT
        sits between (the address differs from its own)."""
        return {
            **self.health(),
            "host": socket.gethostname(),
            "os": platform.platform(),
            "python": platform.python_version(),
            "pid": os.getpid(),
            "seen_from_ip": q.client_ip,
            # 07 3: the address the Controller reached here and its subnet (same-subnet check)
            "reached_ip": q.local_ip or None,
            "reached_prefix": _prefix_of(q.local_ip),
            "modules": [
                {"id": m.id, "enabled": m.enabled, "reason": m.reason} for m in self.modules
            ],
            # F17 step 2: the tools the enabled modules may need, found or None
            "tools": self.tools.find_all([t for m in self.modules if m.enabled for t in m.tools]),
            # 02 4 (GAP-121): the cards, for the screen's Card list between tests; a read
            # with no side effect (a heartbeat would take the node's recent events)
            "links": [x.to_dict() for x in self.links],
            # the tablets' app this PC carries (Update PCs sends it when it differs, also to a
            # PC already on this build: a fresh SSH install has none), None = none here
            "tablet_app": self._tablet_app_build(),
        }

    def _tablet_app_build(self) -> str | None:
        try:
            return (self.tablets.apk.parent / "build.txt").read_text(encoding="ascii").strip()
        except OSError:
            return None

    def reset(self, _: Request) -> dict[str, Any]:
        """02 4 ``POST /v1/reset`` (F11 Reset All): stop everything, stop what a crash left
        behind, then check the node is clean. ``clean`` is False when anything is left."""
        stopped = self.safe_stop("Reset All", code="ZL-INFO", level="info")
        self.awake_until = 0.0  # no test any more: sleep and time sync as they were
        self.want_awake("in a test", False)
        before = len(self._test_records())
        self._stop_leftovers()  # SYS-007 for each program that was still there
        left = [str(p.stem) for p in self._test_records()]
        busy: list[str] = []
        put: list[dict[str, Any]] = []  # settings a module changed, put back (F11)
        for m in self.modules:
            restore = getattr(m.instance, "restore_settings", None) if m.enabled else None
            if restore is not None:
                try:
                    put += list(restore())
                except Exception as e:  # a broken restore is itself not clean
                    put.append({"what": m.id, "ok": False, "detail": f"restore failed ({e!r})"})
            check = getattr(m.instance, "clean_check", None) if m.enabled else None
            if check is not None:
                try:
                    busy += [f"{m.id}: {x}" for x in check()]
                except Exception as e:  # a broken check is itself not clean
                    busy.append(f"{m.id}: check failed ({e!r})")
        checks = [
            {"name": "state", "ok": stopped and self.flow.state is State.IDLE,
             "detail": self.flow.state.value},
            {"name": "processes", "ok": not left,
             "detail": f"{before} leftover stopped" if before else "none left"},
            {"name": "modules", "ok": not busy, "detail": "; ".join(busy) or "all stopped"},
            {"name": "network settings", "ok": all(x.get("ok") for x in put),
             "detail": "; ".join(f"{x.get('what')}: {x.get('detail')}" for x in put)
             or "nothing changed by ZeroLoss"},
        ]  # fmt: skip
        return {"clean": all(c["ok"] for c in checks), "checks": checks, "boot_id": self.boot_id}

    def stop_here(self, who: str) -> dict[str, Any]:
        """02 4 ``POST /v1/stop`` (F10, USR-007): this node only — modules stopped in
        reverse order, their files closed. The answer lists the run folder's files left
        after the stop. A stop from this PC is a warning the Controller gets by heartbeat
        (a test it ran ends INCOMPLETE by its fault rules)."""
        local = who.startswith("this PC")
        level: Level = "warn" if local else "info"
        # safe_stop tells it (as USR-007 from this PC) only when something was running
        stopped = self.safe_stop(f"stopped by {who}", code="ZL-USR-007" if local else "ZL-INFO",
                                 level=level)  # fmt: skip
        runs = self.data_dir / "runs"
        newest = max((d for d in runs.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime,
                     default=None) if runs.is_dir() else None  # fmt: skip
        files = sorted(
            ({"name": f.relative_to(newest).as_posix(), "bytes": f.stat().st_size}
             for f in newest.rglob("*") if f.is_file()) if newest else [],
            key=lambda x: str(x["name"]),
        )  # fmt: skip
        return {"stopped": stopped, "state": self.flow.state.value,
                "run": newest.name if newest else None, "files": files}  # fmt: skip

    def time_sync_on(self, _: Request) -> dict[str, Any]:
        """02 4 ``POST /v1/time/sync``: this PC's automatic time sync on (DSC-009's button),
        only between tests: a test holds it off on purpose (SYS-012)."""
        if self.flow.state is not State.IDLE:
            raise ZlError("RUN-012", "Not during a test", "A test holds the time sync off.",
                          "Press it after the test.")  # fmt: skip
        return self.time_sync.turn_on()

    def records_fetched(self, req: Request) -> dict[str, Any]:
        """02 4 ``POST /v1/records/fetched``: the Controller has every record and evidence
        file of this run from here (its report has them); marked, so the next start of this
        node (or of a test) removes them here (2026-10-31 user: "다시 시작하면 기존 저장된
        캡춰화면이나 정보들 다 지워 메모리 쌓이지 않게")."""
        run = run_storage.check_run(str(req.params.get("run", "")))
        raw = self.data_dir / "runs" / run / "raw"
        if raw.is_dir() and not raw.is_symlink():
            (raw / FETCHED).write_text("", encoding="utf-8")
            return {"run": run, "marked": True}
        return {"run": run, "marked": False}

    def clean_fetched(self) -> list[str]:
        """The raw data (1-second records, evidence pictures, packet clips, screen videos)
        of every run the Controller has fetched, never the one in use; a run not fetched
        yet stays until it is. Returns the runs cleaned."""
        root = self.data_dir / "runs"
        done = []
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            if not (d / "raw" / FETCHED).is_file() or self._in_use(d.name):
                continue
            with contextlib.suppress(OSError, ValueError, ZlError):
                run_storage.delete(root, d.name, raw_only=True)
                done.append(d.name)
        if done:
            self.bus.publish(Event("ZL-INFO", "info", f"Removed the fetched data of "
                                   f"{len(done)} earlier test(s) here"))  # fmt: skip
        return done

    def _in_use(self, run: str) -> bool:
        """The run this node works for now: not IDLE and its last command named it."""
        return self.flow.state is not State.IDLE and run == self.guard.last_run_id

    def disk(self, _: Query) -> dict[str, Any]:
        """02 4 ``GET /v1/disk``: free and total space of the data folder (light)."""
        try:
            use = self.disk_usage(self.data_dir)
        except OSError:
            return {"free_mb": None, "total_mb": None}
        mb = 1024 * 1024
        return {"free_mb": use.free // mb, "total_mb": use.total // mb}

    def storage(self, _: Query) -> dict[str, Any]:
        """02 4 ``GET /v1/storage`` (F15): each run folder's size, biggest first."""
        runs: list[dict[str, Any]] = [{"run": r, **u, "in_use": self._in_use(r)}
                for r, u in run_storage.list_runs(self.data_dir / "runs").items()]  # fmt: skip
        runs.sort(key=lambda x: (-int(x["bytes"]), str(x["run"])))
        return {"free_mb": self.disk_free_mb(), "runs": runs}

    def storage_delete(self, req: Request) -> dict[str, Any]:
        """02 4 ``POST /v1/storage/delete`` (F15): one run, or only its raw data; never
        the run this node is working for (RUN-012)."""
        run = run_storage.check_run(str(req.params.get("run", "")))
        raw_only = req.params.get("raw_only", False)
        if not isinstance(raw_only, bool):
            raise ZlError(
                "DSC-012", "Request rejected", detail="params.raw_only must be true/false"
            )
        if self._in_use(run):
            raise ZlError("RUN-012", f"{run} is the test running now",
                          f"It is {self.flow.state.value}.",
                          "Stop the test first, or choose another run.")  # fmt: skip
        done = run_storage.delete(self.data_dir / "runs", run, raw_only)
        return {"run": run, "raw_only": raw_only, **done}

    def _records_dir(self, run: str) -> Path:
        return self.data_dir / "runs" / run_storage.check_run(run) / "raw"

    def records_index(self, q: Query) -> dict[str, Any]:
        """02 4 ``GET /v1/records/index``: the run's 1-second record files (02 7),
        channel change files (7-2) and evidence files (F23) — the last two ``hour`` null."""
        run = str(q.params.get("run", ""))
        raw = self._records_dir(run)
        files = []
        for f in sorted(raw.glob("*/*")) if raw.is_dir() else []:
            got = record_run(f.name)
            if got and got[0] == run and not f.is_symlink():
                files.append({"device": f.parent.name, "hour": got[1], "name": f.name,
                              "bytes": f.stat().st_size})  # fmt: skip
        return {"run": run, "files": files}

    def records_file(self, q: Query) -> Path:
        """02 4 ``GET /v1/records``: one record file, sent as a copy (the node keeps its
        own, F15). Not while this node still writes the run (RUN-012)."""
        run, device, name = (str(q.params.get(k, "")) for k in ("run", "device", "name"))
        raw = self._records_dir(run)
        got = record_run(name)
        bad_device = not DEVICE_NAME.match(device) or set(device) == {"."}
        if bad_device or not got or got[0] != run:
            raise ZlError("DSC-012", "Request rejected", detail="device and name as in the index")
        if self._in_use(run):
            raise ZlError("RUN-012", f"{run} is still being written", "The test is on.",
                          "Fetch the records after the test.")  # fmt: skip
        src = raw / device / name
        if not src.is_file() or src.is_symlink():
            raise ZlError("RPT-005", f"No record file {device}/{name}", detail=str(src))
        fd, tmp = tempfile.mkstemp(prefix="zeroloss-rec-", suffix=".jsonl")
        os.close(fd)
        try:
            shutil.copyfile(src, tmp)
        except OSError as e:
            Path(tmp).unlink(missing_ok=True)
            raise ZlError("RPT-005", f"Cannot read {device}/{name}", detail=str(e)) from e
        return Path(tmp)

    def logs_bundle(self, q: Query) -> Path:
        """02 4 ``GET /v1/logs`` (20 GAP-78): this node's logs, program records, the run's
        logs (the newest run when none is named), config and state, without secrets,
        1-second records or media; at most 50 MB (``diag_bundle``)."""
        run = str(q.params.get("run") or "")
        if run and not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", run):
            raise ZlError("DSC-012", "Request rejected", detail="run must be a run ID")
        runs = self.data_dir / "runs"
        if not run and runs.is_dir():
            newest = max((d for d in runs.iterdir() if d.is_dir()),
                         key=lambda d: d.stat().st_mtime, default=None)  # fmt: skip
            run = newest.name if newest is not None else ""
        patterns = ["logs/**/*", "procs/**/*", "*.json", "device_id", "modules/*/*.json"]
        if run:
            patterns.append(f"runs/{run}/**/*.log")
        state = {"health": self.health(), "wan": self.wan.status(),
                 "problems": [e.to_dict() for e in list(self._problems)]}  # fmt: skip
        head = {"device_id": self.device_id, "build": self.build, "role": self.role,
                "run": run or None}  # fmt: skip
        return diag_bundle.build(self.data_dir, patterns, {"state.json": state}, head,
                                 skip=DIAG_SKIP)  # fmt: skip

    def wan_set(self, req: Request) -> dict[str, Any]:
        """02 4 ``POST /v1/wan``: turn the ISP role on or off. Not during a test."""
        if self.flow.state in ACTIVE:
            raise ZlError("RUN-012", "The ISP role can change only between tests", "A test runs.")
        on, nic, auto = req.params.get("on"), req.params.get("nic"), req.params.get("auto")
        if auto is True and on is None and nic is None:
            return self.wan.set(None)  # back to automatic (07 3.2 B)
        if not isinstance(on, bool) or not (nic is None or isinstance(nic, str)):
            raise ZlError("DSC-012", "Request rejected", detail="params.on bool, params.nic text")
        return self.wan.set(on, nic or None)

    def _wan_event(self, code: str, level: str, text: str, data: dict[str, Any]) -> None:
        lv: Level = "error" if level == "error" else "warn" if level == "warn" else "info"
        self.bus.publish(Event(code, lv, text, data=data))

    def wifi_scan(self, q: Query) -> dict[str, Any]:
        """02 4 ``GET /v1/wifi/scan?nic=``: the networks one Wi-Fi card sees (SSID, BSSID,
        band, signal, security). Changes nothing."""
        nic = str(q.params.get("nic") or "")
        if nic not in {n.nic for n in self.wifi.nics()}:
            raise ZlError("DSC-012", "Request rejected", detail=f"no Wi-Fi card {nic!r} here")
        return {"nic": nic, "networks": self.wifi.scan(nic)}

    def wifi_join(self, req: Request) -> dict[str, Any]:
        """02 4 ``POST /v1/wifi/join``: only between tests (joining moves the Wi-Fi)."""
        if self.flow.state in ACTIVE:
            raise ZlError("RUN-012", "Wi-Fi can be joined only between tests", "A test runs.")
        p = req.params
        timeout = p.get("timeout_s", JOIN_TIMEOUT_S)
        if (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or not 1 <= timeout <= 120
        ):
            raise ZlError("DSC-012", "Request rejected", detail="params.timeout_s must be 1..120")
        return dict(self.wifi.join(
            str(p.get("ssid", "")), str(p.get("security", "")), str(p.get("password", "")),
            str(p["nic"]) if p.get("nic") else None, float(timeout),
        ))  # fmt: skip

    def stream(self, q: Query) -> Iterator[Any]:
        """Server-Sent Events for screens on this PC: first the whole current state, then
        every event and state change. A screen that falls behind is disconnected and
        gets the full state again when it reconnects (docs/11-ui.md 1.12)."""
        inbox: queue.Queue[Event] = queue.Queue(STREAM_QUEUE)
        too_slow = threading.Event()  # outside the queue: a full queue has no room for it

        def deliver(e: Event) -> None:
            try:
                inbox.put_nowait(e)
            except queue.Full:
                too_slow.set()

        unsubscribe = self.bus.subscribe(deliver)
        try:
            yield {"type": "snapshot", "health": self.health(), "info": self.info(q)}
            last_sent = time.monotonic()
            while not self._closing.is_set() and not too_slow.is_set():
                try:
                    item = inbox.get(timeout=WATCH_INTERVAL_S)  # also notices shutdown
                except queue.Empty:
                    if time.monotonic() - last_sent >= KEEP_ALIVE_S:
                        last_sent = time.monotonic()
                        yield None
                    else:
                        yield IDLE  # lets the server notice a closed screen
                    continue
                if too_slow.is_set():
                    return  # it missed events: drop it; reconnecting gives a fresh snapshot
                last_sent = time.monotonic()
                yield {"type": "event", "event": item.to_dict(), "state": self.flow.state.value}
        finally:
            unsubscribe()

    # ---------------------------------------------------------------- safe stop

    def reach_sender(self, target: str, bind_ip: str) -> dict[str, object]:
        """GAP-158: ``target`` (the sender on the router WAN side) leaves on the card
        that has ``bind_ip``, through the router it is plugged into now (07 6-1)."""
        nic = next((x.nic for x in self.links if x.ip == bind_ip), "")
        got = self.wan_reach.ensure(target, nic)
        if not got.get("ok"):
            self.bus.publish(Event("ZL-NET-023", "warn",
                                   f"Cannot route to the sender {target} on {nic or bind_ip}: "
                                   f"{got.get('why') or got.get('done')}",
                                   data=dict(got)))  # fmt: skip
        elif str(got.get("done", "")).startswith("route"):
            self.bus.publish(Event("ZL-INFO", "info", f"{got['done']}", data=dict(got)))
        return got

    def _release_reach(self) -> None:
        for left in self.wan_reach.release():
            self.bus.publish(Event("ZL-NET-023", "warn", f"Route not taken away: {left}"))

    def safe_stop(self, reason: str, code: str = "ZL-CTL-008", level: Level = "warn") -> bool:
        """Stop every module and confirm. True when the node ends in IDLE. Only one
        stop runs at a time. While the node is IDLE the modules are still asked, so work
        that does not make the node active (a module preparing files) stops too (12 2)."""
        with self._stop_lock:
            if self.flow.state not in ACTIVE:
                self._idle_stop(reason)
                return True
            try:
                self.flow.move(State.STOPPING, reason=reason)
            except ZlError:
                return self.flow.state is State.IDLE  # someone else changed it first
            self.bus.publish(Event(code, level, f"Safe stop: {reason}"))
            failed = []
            for m in reversed([m for m in self.modules if m.enabled]):
                try:
                    assert m.instance is not None
                    m.instance.safe_stop(reason)
                except Exception as e:  # keep stopping the others, report this one
                    failed.append(m.id)
                    self.bus.publish(
                        Event("ZL-SYS-008", "error", f"{m.id} safe stop failed: {e!r}")
                    )
            self.guard.bump_generation()
            if failed:
                self.flow.move(State.FAULT, reason=f"safe stop failed: {', '.join(failed)}")
                return False
            self.flow.move(State.IDLE, reason=reason)
            return True

    # ---------------------------------------------------------------- helpers

    def _restart_hook(self) -> None:
        self.on_controller_restart()

    def _controller_restarted(self) -> None:
        self.safe_stop(CONTROLLER_RESTARTED)

    def _tablet_action(self, action: str) -> Callable[[Request], dict[str, Any]]:
        """``POST /v1/tablets/<action>`` with ``params.serial`` (02 4). Installing or
        stopping the app takes a tablet out of a test: only between tests."""

        def run(req: Request) -> dict[str, Any]:
            serial = req.params.get("serial")
            if not isinstance(serial, str) or not serial:
                raise ZlError("DSC-012", "Request rejected", detail="params.serial is required")
            if action != "start_app" and self.flow.state in ACTIVE:
                raise ZlError("RUN-012", "Tablet apps can be changed only between tests",
                              "A test runs.")  # fmt: skip
            try:
                result: dict[str, Any] = getattr(self.tablets, action)(serial)
            except ValueError as e:
                raise ZlError("DSC-012", "Request rejected", detail=str(e)) from e
            except TabletError as e:
                raise ZlError("TAB-017", f"Tablet {serial}: {e.what}", e.why, e.fix) from e
            return {"serial": serial, **result}

        return run

    def arp_probe(self, req: Request) -> dict[str, Any]:
        """02 4: is this address of ours used by another machine too? (09 SND-013)"""
        ip = req.params.get("ip")
        if not isinstance(ip, str):
            raise ZlError("DSC-012", "Request rejected", detail="params.ip must be an address")
        return arp_probe(ip).to_dict()

    def _controller_seen(self) -> None:
        self.last_controller_request = time.monotonic()
        self._controller_ever = True

    # ---------------------------------------------------------------- Agent Desktop (02 2-12)

    def desktop_status(self, _: Query) -> dict[str, Any]:
        seen = time.monotonic() - self.last_controller_request
        wan = self.wan.status()
        return {
            "node": self.health(),
            "controller": {"connected": self._controller_ever and seen < DESKTOP_LINK_S,
                           "last_seen_s": round(seen, 1) if self._controller_ever else None},
            "my_ips": [{"nic": x.nic, "ip": x.ip, "wireless": x.wireless}
                       for x in self.links if x.ip],
            "wan": {k: wan[k] for k in ("mode", "state", "nic", "why")},  # 20 W-4
            "module_data": {name: dict(fn()) for name, fn in self.module_data.items()},
            "problems": [e.to_dict() for e in list(self._problems)],
        }  # fmt: skip

    def desktop_jobs(self, q: Query) -> dict[str, Any]:
        jobs = self.desktop.jobs(str(q.params.get("desktop_id", "")))
        return {"jobs": jobs, "node_boot_id": self.boot_id}

    def desktop_report(self, params: dict[str, Any]) -> dict[str, Any]:
        reports = params.get("reports")
        if not isinstance(reports, list):
            raise ZlError("DSC-012", "Request rejected", detail="reports must be a list")
        return {"accepted": self.desktop.report(str(params.get("desktop_id", "")), reports)}

    def _watchdog(self) -> None:
        next_disk = 0.0
        while not self._closing.wait(min(WATCH_INTERVAL_S, self.disk_check_s)):
            if self.awake_until and time.monotonic() >= self.awake_until:
                self.awake_until = 0.0
                self.want_awake("in a test", False)  # the Controller stopped saying so
            why = self.silence_stop(time.monotonic())
            if why:
                self.safe_stop(why)
            if time.monotonic() >= next_disk:
                next_disk = time.monotonic() + self.disk_check_s
                self._check_disk()

    def silence_stop(self, now: float) -> str | None:
        """F11 / CTL-008: the reason to stop because the Controller is silent, else None.
        Not while the Controller's hold lasts (S-56, GAP-130): it said the test may go on
        without it to the planned end."""
        silent = now - self.last_controller_request
        held = now < self.hold_until
        if self.flow.state in ACTIVE and silent > self.lost_after_s and not held:
            return f"no request from the Controller for {silent:.0f} s"
        return None

    def _check_disk(self) -> None:
        """RCV-008: warn below 1 GB (cleared when it grows), stop safely below 200 MB."""
        if self.flow.state not in ACTIVE:
            self._disk_level = "ok"
            return
        free = self.disk_free_mb()
        if free is None:
            return
        level = "stop" if free < DISK_STOP_MB else "low" if free < DISK_WARN_MB else "ok"
        if level == self._disk_level:
            return
        before, self._disk_level = self._disk_level, level
        where = str(self.data_dir)
        if level == "low" and before == "ok":
            self.bus.publish(
                Event(
                    "ZL-RCV-008", "warn", f"Disk almost full: {free} MB free", data={"path": where}
                )
            )
        elif level == "ok":
            self.bus.publish(
                Event(
                    "ZL-INFO",
                    "info",
                    f"Disk space back: {free} MB free",
                    data={"clears": ["ZL-RCV-008"]},
                )
            )
        elif level == "stop":
            self.bus.publish(
                Event(
                    "ZL-RCV-008",
                    "error",
                    f"Disk full: {free} MB free — stopping safely",
                    data={"path": where},
                )
            )
            self.safe_stop(f"disk space {free} MB, below {DISK_STOP_MB} MB", code="ZL-RCV-008")

    def _idle_stop(self, reason: str) -> None:
        for m in reversed([m for m in self.modules if m.enabled]):
            try:
                assert m.instance is not None
                m.instance.safe_stop(reason)
            except Exception as e:  # one module must not block the others
                self.bus.publish(Event("ZL-SYS-008", "error", f"{m.id} safe stop failed: {e!r}"))

    def _attach_modules(self) -> None:
        for m in self.modules:
            if m.enabled:
                try:
                    assert m.instance is not None
                    m.instance.attach(self)
                except Exception as e:
                    m.reason = f"attach failed: {e!r}"
            if not m.enabled:
                self.bus.publish(Event("ZL-INS-024", "warn", f"Module {m.id} disabled: {m.reason}"))

    def want_awake(self, why: str, on: bool) -> None:
        """SYS-005 for a role that is not a node state: the Controller keeps its PC awake
        while a test or the broadcast runs (GAP-116)."""
        if on:
            self._awake_asked.add(why)
        else:
            self._awake_asked.discard(why)
        self._awake_update()

    def _awake_update(self) -> None:
        wanted = self._awake_active or bool(self._awake_asked)
        if wanted != self._awake_wanted:
            self._awake_wanted = wanted
            self._awake_changed.set()

    def _state_changed(self, name: str, old: State, new: State, reason: str) -> None:
        self._awake_active = new in ACTIVE or new is State.STOPPING  # until the stop is done
        self._awake_update()
        if new not in ACTIVE:
            self.hold_until = 0.0  # a hold never carries over to the next test (GAP-130)
        if new is State.IDLE:
            threading.Thread(
                target=self._release_reach, name="reach-release", daemon=True
            ).start()  # GAP-158: the routes this test added go away
        self.bus.publish(
            Event(
                "ZL-INFO",
                "info",
                f"{name}: {old} -> {new}",
                data={"reason": reason} if reason else {},
            )
        )

    def _watch_cpu(self) -> None:
        """09 SYS-003: above 90% for more than 10 s during a test -> a warning with the
        top programs; below again (or the test over) -> cleared."""
        if self.flow.state in ACTIVE:
            change = self.cpu_watch.update(self.resources.read().cpu_pct)
        else:
            change = self.cpu_watch.reset()
        if change == "start":
            top = top_processes()
            names = ", ".join(f"{p['name']} {p['pct']:g}%" for p in top) or "unknown"
            self.bus.publish(Event("ZL-SYS-003", "warn",
                                   f"PC busy: CPU above 90% for 10 s (top: {names})",
                                   data={"top": top}))  # fmt: skip
        elif change == "end":
            self.bus.publish(Event("ZL-INFO", "info", "PC no longer busy",
                                   data={"clears": ["ZL-SYS-003"]}))  # fmt: skip

    def _awake_loop(self) -> None:
        """SYS-005: probe once, then hold the sleep inhibitor while in a test. SYS-012:
        the same for automatic time sync; one a crash left off goes back on first."""
        self.sleep_guard.probe()
        self._time_sync_back()
        while True:
            self._awake_changed.wait()
            self._awake_changed.clear()
            if self._closing.is_set():
                self.sleep_guard.release()
                self._time_sync_back()
                return
            if self._awake_wanted:
                why = self.sleep_guard.hold()
                if why:
                    text = f"Could not keep this PC awake during the test: {why}"
                    self.bus.publish(Event("ZL-SYS-005", "warn", text))
                why = self.time_sync.hold()
                if why and sys.platform != "win32":  # Windows: told in the status only
                    self.bus.publish(Event(
                        "ZL-SYS-012", "warn",
                        f"Could not hold this PC's time sync during the test: {why}",
                    ))  # fmt: skip
            else:
                self.sleep_guard.release()
                self._time_sync_back()

    def _time_sync_back(self) -> None:
        back = self.time_sync.release()
        if not back.get("ok"):
            self.bus.publish(Event(
                "ZL-SYS-012", "error",
                f"This PC's time sync was not turned back on: {back.get('why')}. "
                "By hand: sudo timedatectl set-ntp true",
            ))  # fmt: skip


def _mbps(x: Link | None) -> str:
    return f"{x.link_mbps} Mbps" if x and x.link_mbps else "unknown"


def link_event(c: Change) -> Event:
    """A link change as an event (09 NET-015/016/017). Speed changes stay ZL-INFO here:
    only the Controller knows the needed rate, so it decides NET-013 vs NET-014."""
    data: dict[str, Any] = {"nic": c.nic, "kind": c.kind, "after": c.after.to_dict()}
    if c.before is not None:
        data["before"] = c.before.to_dict()
    if c.kind == "speed":
        text = f"Link {c.nic} speed changed: {_mbps(c.before)} -> {_mbps(c.after)}"
        return Event("ZL-INFO", "warn", text, data=data)
    if c.kind in ("down", "flap"):
        what = "went down" if c.kind == "down" else "went down and up"
        return Event("ZL-NET-015", "error", f"Link {c.nic} {what}", data=data)
    if c.kind == "up":
        data["clears"] = ["ZL-NET-015"]  # the link is back (02 8, F21)
        return Event("ZL-INFO", "info", f"Link {c.nic} is up ({_mbps(c.after)})", data=data)
    a = c.before.wifi if c.before else None
    b = c.after.wifi
    if c.kind == "roam":
        text = f"{c.nic} moved to another access point: {a and a.bssid} -> {b and b.bssid}"
        return Event("ZL-NET-017", "warn", text, data=data)
    text = (
        f"{c.nic} Wi-Fi changed: channel {a and a.channel} -> {b and b.channel}, "
        f"{a and a.width_mhz} -> {b and b.width_mhz} MHz, {a and a.band} -> {b and b.band}"
    )
    return Event("ZL-NET-016", "warn", text, data=data)


def _free_mb(path: Path) -> int | None:
    try:
        return shutil.disk_usage(path).free // (1024 * 1024)
    except OSError:
        return None


def _prefix_of(ip: str) -> int | None:
    """Prefix length of the NIC that has ``ip`` (Linux), else None."""
    nic = nic_of(ip) if ip else None
    mask = ipv4_netmask(nic) if nic else None
    if mask is None:
        return None
    return bin(int.from_bytes(socket.inet_aton(mask), "big")).count("1")
