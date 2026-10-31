"""Controller node service (docs/12-architecture.md 1-0): the run controller lives here, so a
test keeps running when the screen app closes. The Desktop app and the CLI are only
windows onto it through 127.0.0.1:10994 (docs/02-data.md 2, 13-ai-loop 2).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from controller import wan_route
from controller.actions import ActionBoard
from controller.analysis import reanalyze
from controller.baselines import BaselineStore
from controller.deploy import Deploy
from controller.devices import DEPLOYER_PORT, Device, DeviceList
from controller.discover import discover
from controller.problems import ProblemBoard
from controller.records import collect as collect_records
from controller.run import LOST_S, RunController, RunState
from controller.setup import SetupStore, address_labels, plan_of, roles, sides
from controller.ssh_install import (
    KnownHosts,
    LinuxInstaller,
    LinuxRemover,
    Target,
    newest_deb,
    newest_modules,
    newest_tablet,
)
from controller.web import WEB_PORT, ResultsWeb, Traffic
from controller.wifi_store import BandPasswords, WifiStore, check
from core import self_check, self_checks
from core.blocks import diag_bundle, run_storage
from core.blocks.link_monitor import ipv4_netmask, read_links
from core.blocks.route_guard import never_default
from core.events.bus import Event
from core.modules.registry import discover as discover_modules
from core.modules.registry import load_run_entry
from core.node import CORE_PORT, LOCAL_PORT, Node
from core.protocol.client import NodeClient
from core.protocol.envelope import Request, ZlError
from core.protocol.http_server import Query
from core.records.experience import Context, ExperienceDb
from core.settings import load_schema

P = "/v1/controller"
GUIDE = "설치_및_사용_안내.html"  # tools/kit, and next to an installed program (06 4.1-2)
THIS_PC = "controller"  # the Controller PC as a LAN-side PC (throughput; user: not "this-pc")
RELOCATE_EVERY_S = 30.0  # a registered device that does not answer: look for it again
TABLET_APP_WAIT_S = 120.0  # Update PCs: a PC restarting on its new build, then its tablets
RUN_ID = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
STORAGE_TIMEOUT_S = 10.0  # per device (02 2-13)
STORAGE_WATCH_S = 60.0  # 03 F15: disk space every minute
WEB_RETRY_S = 30.0  # a busy results web port: try again (2026-10-09, a test held it)
AWAKE_WATCH_S = 2.0  # SYS-005: a test that starts holds the Controller PC awake at once
LOW_PCT = 20.0  # 09 SYS-010: below 20 % or 50 GB free is "Storage low"
LOW_MB = 50 * 1024
DISK_TIMEOUT_S = 3.0
LOOPBACK_S = (10, 3600)  # verify loopback length (02 2-13)
LOOPBACK_MARGIN_S = 300.0  # start checks, warm-up, stop and report on top of the length
RECORDS_JOIN_S = 30.0  # a file being fetched ends within its read timeout
# GAP-153: between tests every device is asked this often, so its Agent Desktop says
# "Controller: Connected" (its window is 3 s, core.desktop_jobs.LINK_S) and a device that
# moved or came back is found without anyone pressing a button
IDLE_LINK_S = 2.0
IDLE_LINK_TIMEOUT_S = 1.0
OWED_EVERY_S = 60.0  # records owed by a PC that answers again: asked at most this often
AUTO_RESET_EVERY_S = 30.0  # a Reset All that fails again is not retried every round
IDLE_WATCH_ENV = "ZEROLOSS_IDLE_WATCH"  # "0": off (automatic tests use their own devices)


class ControllerService:
    def __init__(
        self,
        data_dir: Path,
        reports_dir: Path,
        modules_dir: Path,
        host: str = "0.0.0.0",
        port: int = CORE_PORT,
        local_port: int = LOCAL_PORT,
        lost_s: float = LOST_S,
        web_port: int = WEB_PORT,
    ) -> None:
        self.node = Node(
            data_dir, "controller", host, port, modules_dir=modules_dir, local_port=local_port
        )
        self.data_dir = data_dir
        self.devices = DeviceList(data_dir / "devices.json")
        self._relocated_at = -RELOCATE_EVERY_S
        self._relocate_lock = threading.Lock()
        self._found: dict[str, list[str]] = {}  # GAP-155: where the search saw each device
        # GAP-156: run -> devices whose 1-second records did not come (the PC did not
        # answer at the end); fetched when it answers again, then analysed again
        self._owed: dict[str, set[str]] = {}
        self._owed_at = -OWED_EVERY_S
        self._auto_reset_at = -AUTO_RESET_EVERY_S
        self._no_wan_told = False  # 20 W-2: "move the sender cable" asked, not yet cleared
        self.fix_route: Callable[[str], tuple[bool, str]] = never_default  # NET-019 [Fix]
        self.reports_dir = reports_dir
        self._records_lock = threading.Lock()  # one retrieval at a time (02 2-13)
        self._records_thread: threading.Thread | None = None
        self._closing = threading.Event()
        self.quit_requested = threading.Event()  # 02 2-13 quit: the window closed while idle
        self.web_traffic = Traffic()  # 07 7: results web bytes during a test
        self.web: ResultsWeb | None = None
        self._web_busy = False  # CTL-006 told; retried every WEB_RETRY_S
        self._web_port = web_port
        self._low: dict[str, dict[str, Any]] = {}  # 09 SYS-010: device -> disk (02 2-1)
        self._low_lock = threading.Lock()
        self._low_checked = False  # the first check runs at once
        self._watch_thread: threading.Thread | None = None
        self.problems = ProblemBoard()
        self.node.bus.subscribe(self.problems)  # every event on one bus (12 4-1)
        # Experience DB (14 1): every event becomes a situation (20 GAP-46)
        self.experience = ExperienceDb(data_dir / "experience.db", Context(self.node.build))
        self.node.bus.subscribe(self._record)
        self.actions = ActionBoard(on_done=self._action_done)  # F22, apart from problems
        self.node.bus.subscribe(self.actions)
        self.wifi = WifiStore(data_dir / "test-wifi.json")  # S-51 (02 2-7)
        self.band_passwords = BandPasswords(data_dir / "band-wifi.json")  # S-65 bands
        self.modules_dir = modules_dir
        self.settings_schema = load_schema(modules_dir)  # 16 table as screen data
        defaults = {sid: s["value"] for sid, s in self.settings_schema.items()}
        self.setup = SetupStore(data_dir / "controller.json", defaults)  # F14 (02 11)
        self.run = RunController(
            self.devices,
            reports_dir,
            modules_dir,
            self.node.bus,
            build=self.node.build,
            lost_s=lost_s,
            wifi=self.wifi.secret,
            on_finished=self._test_ended,
            on_report=self._records_later,
            web_traffic=self.web_traffic.take,
            baselines=BaselineStore(data_dir / "baselines.json"),  # 05 4.6.3
            problems_report=lambda rid, dur: {
                **self.problems.run_report(rid, dur),
                "human_actions": self.actions.run_report(rid),
            },
        )
        self.run.revive = self._revive  # Reset All brings back a node that is off (F11)
        self.run.own = self._own  # this PC as a LAN-side PC for throughput (2026-10-17)
        self.run.secret_of = self.band_passwords.secret  # a band network's, only to join it
        loc = self.node.local
        loc.get(f"{P}/status", self.status)
        loc.get(f"{P}/live", lambda _: self.run.live())  # live monitor (02 12 /live) (GAP-119)
        loc.get(f"{P}/baselines", lambda _q: {"baselines": self.run.baselines.all()})  # 2-13
        loc.get(f"{P}/devices", self.devices_list)
        loc.get(f"{P}/report", self.report)
        loc.get(f"{P}/reports", self.reports)  # F15 S8 (20 GAP-83)
        loc.command(f"{P}/diagnostics", self.diagnostics)
        loc.get(f"{P}/storage", self.storage_list)  # F15 (20 GAP-79)
        loc.command(f"{P}/records", self.records)  # 02 2-13 (20 GAP-80)
        loc.command(f"{P}/channel", self.channel)  # 02 2-13 (03 F19)
        loc.command(f"{P}/broadcast", self.broadcast)  # 02 2-13 (03 F18)
        loc.command(f"{P}/verify", self.verify)  # 02 2-13 (20 GAP-81)
        loc.command(f"{P}/storage/delete", self.storage_delete)
        loc.get(f"{P}/problems", lambda _: self.problems.snapshot())
        loc.get(f"{P}/actions", lambda _: self.actions.snapshot())
        loc.command(f"{P}/actions/done", self.action_done)
        loc.command(f"{P}/actions/choose", self.action_choose)
        loc.command(f"{P}/devices/add", self.devices_add)
        loc.command(f"{P}/devices/remove", self.devices_remove)
        loc.command(f"{P}/devices/address", self.devices_address)  # GAP-155
        loc.command(f"{P}/devices/wan", self.devices_wan)
        loc.command(f"{P}/run", self.run_start)
        loc.command(f"{P}/stop", self.run_stop)
        loc.command(f"{P}/quit", self.quit)  # GAP-116
        loc.command(f"{P}/prepare", lambda req: self.run.prepare_ahead(dict(req.params)))
        loc.command(f"{P}/precheck", self.precheck)
        loc.command(f"{P}/reset", lambda _: self.run.reset())
        loc.command(f"{P}/discover", self.discover)
        loc.get(f"{P}/setup", self.setup_get)
        loc.command(f"{P}/setup", self.setup_set)
        loc.get(f"{P}/settings", lambda _: {"settings": self.settings_schema})
        loc.get(f"{P}/sources", self.sources)
        loc.get(f"{P}/wifi_scan", self.wifi_scan)  # S-65: what a PC's Wi-Fi card sees
        loc.command(f"{P}/wifi_password", self.wifi_password)  # S-65: a band network's
        loc.command(f"{P}/nodes", self.nodes)
        loc.get(f"{P}/wifi", lambda _: self.wifi.public())
        loc.command(f"{P}/wifi", self.wifi_set)
        loc.command(f"{P}/wifi/clear", lambda _: self.wifi.clear())
        loc.command(f"{P}/deploy", self.deploy_install)
        loc.command(f"{P}/update_pcs", self.update_pcs)  # the screen's Update PCs
        loc.get(f"{P}/modules", self.deploy_modules)
        loc.command(f"{P}/modules/remove", self.deploy_remove)
        loc.command(f"{P}/install/linux", self.install_linux)
        loc.command(f"{P}/remove/linux", self.remove_linux)  # 06 7 (the remove buttons)
        loc.command(f"{P}/made/fetch", self.made_fetch)  # the offline kit's movies
        loc.command(f"{P}/remove/tablet_app", self.remove_tablet_app)
        loc.get(f"{P}/prepare", self.prepare_state)

    def _record(self, e: Event) -> None:
        run_id = str((e.data or {}).get("run_id") or self.run.run_id or "")
        self.experience.context = replace(
            self.experience.context, run_id=run_id, stage=self.run.state.value
        )
        self.experience.record_event(e)

    def _action_done(self, action: dict[str, Any], actor: str) -> None:
        """14 1: a finished human action is an action row, resolved."""
        row = self.experience.start_action(
            str(action["action_id"]),
            "human" if actor == "human" else "auto",
            note=f"{action['device']}: {action['opened_by']}",
        )
        self.experience.finish_action(row, "resolved")

    def action_done(self, req: Request) -> dict[str, Any]:
        key = str(req.params.get("key", ""))
        row = self.actions.waiting_row(key)
        if row is not None and row.get("choose") == "throughput_fix":
            # Done on a test waiting for the person = "Fixed, check again" (2026-10-22: Done
            # closed it, no answer went to the test, it waited until Stop)
            return self.action_choose(Request("local", 0, None, {"key": key, "choice": "retry"}))
        if not self.actions.done(key):
            raise ZlError("RUN-012", f"No waiting action {key}", "It is done or unknown.")
        return {"done": key}

    def action_choose(self, req: Request) -> dict[str, Any]:
        """20 W-1 (02 2-3): the person pressed one of a waiting action's choices. Only
        choices the action itself offered are taken; what a choice does is by its kind."""
        key, choice = str(req.params.get("key", "")), str(req.params.get("choice", ""))
        row = self.actions.waiting_row(key)
        if row is None:
            raise ZlError("RUN-012", f"No waiting action {key}", "It is done or unknown.")
        if choice not in [c["id"] for c in row.get("choices") or []]:
            raise ZlError("DSC-012", "Request rejected",
                          detail=f"{choice!r} is not one of this action's choices")  # fmt: skip
        kind = row.get("choose")
        if kind == "wan_nic":
            result = self._wan(str(row["device"]), {"on": True, "nic": choice})  # = --nic
            if result.get("state") in ("ready", "waiting_nic"):  # taken; conflict stays open
                self.actions.done(key)
            return {"key": key, "choice": choice, "wan": result}
        if kind == "test_route":  # NET-019: this PC's own card (13 1)
            done, what = self.fix_route(choice)
            if not done:
                raise ZlError("NET-019", f"Could not change {choice}", what,
                              "Set it by hand: nmcli connection modify <connection> "
                              "ipv4.never-default yes ipv4.ignore-auto-dns yes")  # fmt: skip
            self.actions.done(key)
            return {"key": key, "choice": choice, "route": what}
        if kind == "time_sync":  # DSC-009: that PC's automatic time sync on (2026-10-31)
            dev = self.devices.get(choice)
            client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
            try:
                got = client.command("/v1/time/sync", self.run.generation, None, timeout_s=20)
            finally:
                client.close()
            if not got.get("ok"):
                raise ZlError("DSC-009", f"Time sync not turned on on {choice}",
                              str(got.get("why") or ""),
                              "On that PC: sudo timedatectl set-ntp true")  # fmt: skip
            self.actions.done(key)
            return {"key": key, "choice": choice, "time_sync": got}
        if kind == "wan_route":  # NET-023: this PC's way to the router WAN side
            wan_route.forget_asked()
            routed = self.run.wan_route()
            if "now through" not in routed and "already" not in routed:
                raise ZlError("NET-023", "The route was not added", routed,
                              "Press the button again; answer Yes in Windows.")  # fmt: skip
            self.actions.done(key)
            return {"key": key, "choice": choice, "route": routed}
        if kind == "throughput_fix":  # NET-024: a card not put back, fixed by hand
            ask = row.get("ask") or {}
            got = self.run.module_command(str(row["device"]), str(ask.get("path")),
                                          {"target": ask.get("target"), "seq": ask.get("seq"),
                                           "answer": {"choice": choice}})  # fmt: skip
            self.actions.done(key)
            return {"key": key, "choice": choice, "resumed": got.get("resumed")}
        raise ZlError("DSC-012", "Request rejected", detail=f"unknown choice kind {kind!r}")

    def start(self) -> None:
        self.node.start()
        if self.devices.unreadable:  # SYS-011: started without it, told once (GAP-117)
            self.node.bus.publish(Event("ZL-SYS-011", "error", self.devices.unreadable))
        # F10 / CTL-003: a run a crashed Controller left behind gets its report now
        threading.Thread(target=self.run.recover_unfinished, name="recovery", daemon=True).start()
        self._watch_thread = threading.Thread(target=self._storage_watch, name="storage-watch",
                                              daemon=True)  # fmt: skip
        self._watch_thread.start()
        threading.Thread(target=self._awake_watch, name="awake-watch", daemon=True).start()
        if os.environ.get(IDLE_WATCH_ENV, "1") != "0":
            threading.Thread(target=self._idle_watch, name="idle-watch", daemon=True).start()
        self._start_web()

    def stop(self) -> None:
        if self.run.state.value == "RUNNING":
            self.run.stop("controller_shutdown")
        self.run.close()  # F18: the broadcast too
        self._closing.set()  # a retrieval stops between files, the watch ends (G-11)
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=RECORDS_JOIN_S)
        if self._records_thread is not None:
            self._records_thread.join(timeout=RECORDS_JOIN_S)
        if self.web is not None:
            self.web.stop()
        self.node.stop()
        self.experience.close()

    # ---------------------------------------------------------------- routes

    def status(self, _: Query) -> dict[str, Any]:
        return {
            "build": self.node.build,
            "generation": self.node.guard.generation,
            "reports_dir": str(self.reports_dir),
            "run": self.run.status(),
            "storage_low": self.storage_low(),
            "broadcast": self.run.broadcast_status(),
        }

    def devices_list(self, _: Query) -> dict[str, Any]:
        return self.devices.to_dict()

    def devices_add(self, req: Request) -> dict[str, Any]:
        p = req.params
        dev = self.devices.add(
            str(p.get("name", "")),
            str(p.get("host", "")),
            int(p.get("port", CORE_PORT)),
            self.run.boot_id,
            int(p.get("deployer_port", DEPLOYER_PORT)),
        )
        return {"name": dev.name, "device_id": dev.device_id, "role": dev.role}

    def devices_address(self, req: Request) -> dict[str, Any]:
        """GAP-155: the address a person picked or typed for a registered PC. Used only
        when the PC that answers there is that PC (its device ID); another PC answering is
        refused with its name (DSC-016), no answer with DSC-014."""
        p = req.params
        dev = self.devices.get(str(p.get("name", "")))
        host = str(p.get("host", "")).strip()
        port = int(p.get("port") or dev.port)
        client = NodeClient(host, port, self.run.boot_id, dev.name)
        try:
            health = client.get("/v1/health", timeout_s=5.0)
        finally:
            client.close()
        got = str(health.get("device_id") or "")
        if got != dev.device_id:
            other = next((d.name for d in self.devices.all() if d.device_id == got), None)
            who = other or f"another PC ({health.get('host') or got[:8]})"
            raise ZlError("DSC-016", f"{who} answers at {host}, not {dev.name}",
                          f"The address belongs to {who}.",
                          f"Pick or type the address {dev.name} shows under My IP.")  # fmt: skip
        done = self.devices.move(dev.device_id, host, port)
        if done is not None:
            self._moved(dev.name, done[1], f"{host}:{port}")
        return {"name": dev.name, "host": host, "port": port}

    def devices_remove(self, req: Request) -> dict[str, Any]:
        self.devices.remove(str(req.params.get("name", "")))
        return {"removed": req.params.get("name")}

    def devices_wan(self, req: Request) -> dict[str, Any]:
        """02 2-9: read or set a node's ISP role (07 3.2 B). Setting only between tests."""
        return self._wan(str(req.params.get("name", "")), dict(req.params))

    def _wan(self, name: str, p: dict[str, Any]) -> dict[str, Any]:
        dev = self.devices.get(name)
        client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
        try:
            if p.get("auto") is True:
                with self.run.between_tests("Changing the ISP role"):
                    return dict(client.command("/v1/wan", self.run.generation, None,
                                               timeout_s=20, auto=True))  # fmt: skip
            if "on" not in p:
                return dict(client.get("/v1/wan"))
            with self.run.between_tests("Changing the ISP role"):
                return dict(client.command("/v1/wan", self.run.generation, None, timeout_s=20,
                                           on=bool(p["on"]), nic=p.get("nic")))  # fmt: skip
        finally:
            client.close()

    def run_start(self, req: Request) -> dict[str, Any]:
        """A plan file's plan, or the one the screen choice makes (F14)."""
        return {"run_id": self.run.start(self._plan(req.params.get("plan")))}

    def _plan(self, plan: Any) -> dict[str, Any]:
        if plan is None:
            return dict(self._setup_plan()["plan"])
        if not isinstance(plan, dict):
            raise ZlError("DSC-012", "Request rejected", detail="params.plan must be an object")
        return plan

    def _setup_plan(self, strict: bool = True) -> dict[str, Any]:
        """02 2-11: the screen choice, the roles found (06 10.4) and the plan they make.
        ``strict``: an unclear choice is an error (Start, checks); else ``plan`` is None."""
        setup = self._setup_now()
        found = sides(self.devices, self.run.boot_id, self._found, str(setup.get("test_type")))
        chosen = roles(setup, found)
        self._wan_side_notice(setup, found)
        found = {**found, THIS_PC: self._own_side(found)}  # shown, never a role to pick
        plan: dict[str, Any] | None = None
        if chosen["why"]:
            if strict:
                fix = "Choose them in the device rows (or zeroloss setup set)."
                raise ZlError("RUN-014", chosen["why"], "The test devices are not clear.", fix)
        else:
            plan = plan_of(setup, chosen)
            if chosen["lan_sender"]:
                plan["sender_side"] = "lan"  # comparison test (06 10.4)
        return {"setup": setup, "sides": found, "roles": chosen, "plan": plan,
                "wifi_passwords": self.band_passwords.ssids(),  # which have one; never the text
                "preview": self._preview(setup, plan, found)}  # fmt: skip

    def _preview(self, setup: dict[str, Any], plan: dict[str, Any] | None,
                 found: dict[str, Any]) -> list[str]:  # fmt: skip
        """What Start will run, in the module's words (the Test area shows it before
        Start). Empty when the module has none or the choice is not complete."""
        try:
            rules = load_run_entry(self.modules_dir, str(setup.get("module") or ""))
        except (ZlError, ImportError):
            return []
        of = getattr(rules, "preview", None)
        if of is None or plan is None:
            return []
        hosts = {n: str(v.get("host") or "") for n, v in found.items() if isinstance(v, dict)}
        try:
            return [str(x) for x in of({**plan, "hosts": hosts})]
        except (ZlError, ValueError, KeyError, TypeError):
            return []

    def _relocate(self) -> bool:
        """06 10.2: a registered device does not answer — look for it (broadcast + the
        sender addresses). Between tests only, at most every 30 s. 2026-10-15 (GAP-155,
        user: "the app doing it by itself is serious"): the addresses found are offered in
        its row to pick (``_found``), never saved by themselves — 10-12 a search saved
        t460-rx at the sender's address. True when something new was found."""
        if self.run.state not in (RunState.IDLE, RunState.FAULT):
            return False
        if not self._relocate_lock.acquire(blocking=False):
            return False  # one search at a time
        try:
            if time.monotonic() - self._relocated_at < RELOCATE_EVERY_S:
                return False
            self._relocated_at = time.monotonic()
            try:
                r = discover(self.devices, self.run.boot_id, save=False)
            except ZlError:
                return False
            before = dict(self._found)
            for row in r["found"]:
                if row.get("known"):
                    seen = self._found.setdefault(str(row["name"]), [])
                    for a in row.get("addresses") or [row["address"]]:
                        if a not in seen:
                            seen.insert(0, str(a))
            return self._found != before
        finally:
            self._relocate_lock.release()

    def _wan_side_notice(self, setup: dict[str, Any], found: dict[str, dict[str, Any]]) -> None:
        """20 W-2 (07 3.2 B): with the automatic ISP role nothing on the sender PC asks for
        its cable. No PC on the router WAN side while no sender is chosen by hand -> the
        Action Required "move the sender cable" (SND-022, no device: which PC is unknown);
        it closes by itself once a PC shows up on the WAN side."""
        reachable = [f for f in found.values() if f.get("reachable")]
        chosen = setup.get("sender") in found  # a LAN sender picked on purpose (comparison)
        missing = (
            bool(reachable) and not chosen and not any(f.get("side") == "wan" for f in reachable)
        )
        if missing and not self._no_wan_told:
            self._no_wan_told = True
            self.node.bus.publish(Event(
                "ZL-SND-022", "warn",
                "No PC on the router WAN side: connect the sender PC's cable to the router "
                "WAN port (it gives the router its WAN address by itself)",
                data={"from": "controller"},
            ))  # fmt: skip
        elif not missing and self._no_wan_told and any(f.get("side") == "wan" for f in reachable):
            self._no_wan_told = False
            self.node.bus.publish(Event("ZL-INFO", "info", "A PC is on the router WAN side now",
                                        data={"clears": ["ZL-SND-022"]}))  # fmt: skip

    def _moved(self, name: str, old: str, new: str) -> None:
        self.node.bus.publish(Event("ZL-INFO", "info", f"{name} moved from {old} to {new}",
                                    device=name, data={"from": old, "to": new}))  # fmt: skip

    def _setup_now(self) -> dict[str, Any]:
        """The saved choice; no module chosen yet = the first installed test module."""
        setup = self.setup.get()
        if not setup.get("module"):
            setup["module"] = next(
                (m.id for m in self.node.modules if m.manifest.get("run_entry")), None
            )
        return setup

    def setup_get(self, _: Query) -> dict[str, Any]:
        return self._setup_plan(strict=False)

    def setup_set(self, req: Request) -> dict[str, Any]:
        changes = req.params.get("changes")
        if not isinstance(changes, dict):
            raise ZlError("DSC-012", "Request rejected", detail="params.changes must be an object")
        with self.run.between_tests("Changing the test settings"):
            return {"setup": self.setup.update(changes)}

    # ---------------------------------------------------------------- deploy (02 2-5)

    def _deploy(self) -> Deploy:
        return Deploy(self.devices, self.run.boot_id, self.run.generation)

    def deploy_install(self, req: Request) -> dict[str, Any]:
        p = req.params
        with self.run.while_idle("Installing"):  # never during a test (INS-022)
            return self._deploy().install(str(p.get("node", "")), Path(str(p.get("package", ""))))

    def update_pcs(self, _: Request) -> dict[str, Any]:
        """The screen's Update PCs (2026-10-17: the fix said "zeroloss deploy", a command a
        person at the screen cannot run): every PC on another build gets this program's
        build from its packages folder -- the node package, then each module."""
        build = self.run.build
        folders = [self.data_dir / "packages", self.run.modules_dir.parent / "packages"]
        pk = next((f for f in folders if (f / f"zeroloss-node-agent-{build}.zlp").is_file()),
                  None)  # fmt: skip
        if pk is None:
            raise ZlError(
                "INS-012",
                f"No PC package of build {build} with this program",
                f"Looked in {folders[1]}.",
                f"Use the kit of build {build}: its packages folder has them.",
            )
        parts = [pk / f"zeroloss-node-agent-{build}.zlp",
                 *sorted(pk.glob(f"zeroloss-module-*-{build}.zlm"))]  # fmt: skip
        # the tablets' app and adb (01-overview D3) for the PCs tablets hang on: Linux ones
        tablet = pk / f"zeroloss-tablet-{build}.zlt"
        out = []
        with self.run.while_idle("Updating the PCs"):
            d = self._deploy()
            for dev in self.devices.all():
                client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
                try:
                    theirs = client.get("/v1/health", timeout_s=5).get("build")
                except ZlError:
                    theirs = None  # off or stuck: its Deployer may still take it
                finally:
                    client.close()
                row: dict[str, Any] = {"device": dev.name, "updated": False}
                if theirs == build:
                    row["why"] = "already this build"
                else:
                    try:
                        for part in parts:
                            d.install(dev.name, part)
                    except ZlError as e:
                        out.append({**row, "why": e.what})
                        continue
                    row.update({"updated": True, "from": theirs, "to": build})
                # the tablets' app and adb, also to a PC already on this build that has not
                # got them (2026-10-24: a fresh SSH install had none, and was skipped here)
                if tablet.is_file() and self._wants_tablet_app(dev, build):
                    try:
                        d.install(dev.name, tablet)
                        row["tablet_app"] = True
                    except ZlError as e:  # a PC that cannot take it (Windows) is still updated
                        row["tablet_app"] = e.what
                out.append(row)
            # 2026-10-31 (the tablet rows showed the old app until Start): the app goes onto
            # each tablet now, not at the next Start
            for dev in self.devices.all():
                got = self._tablet_apps(dev, build)
                for r in out:
                    if r["device"] == dev.name and got:
                        r["tablets"] = got
        done = ", ".join(f"{r['device']} {'updated' if r['updated'] else r['why']}" for r in out)
        self.node.bus.publish(Event("ZL-INFO", "info", f"Update PCs to {build}: {done}"))
        return {"build": build, "devices": out}

    def _tablet_apps(self, dev: Any, build: str) -> list[dict[str, Any]]:
        """This build's app on each tablet on the PC's USB, once the PC answers on this
        build (it may be restarting after its update). One row per tablet: the version
        it has now, or why not."""
        client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
        try:
            end = time.monotonic() + TABLET_APP_WAIT_S
            while True:
                try:
                    if client.get("/v1/health", timeout_s=5).get("build") == build:
                        found = client.get("/v1/tablets", timeout_s=30).get("tablets") or []
                        break
                except ZlError:
                    pass
                if time.monotonic() >= end:
                    return []  # off, or another build: its tablets get the app at Start
                time.sleep(3)
            rows = []
            for t in found:
                if t.get("state") != "device":
                    continue
                serial = str(t.get("serial"))
                try:
                    got = client.command("/v1/tablets/ensure_app", self.run.generation, None,
                                         timeout_s=180, serial=serial)  # fmt: skip
                    rows.append({"serial": serial, "version": got.get("version"),
                                 "installed": bool(got.get("installed"))})  # fmt: skip
                except ZlError as e:
                    rows.append({"serial": serial, "why": f"{e.what}: {e.why}".rstrip(": ")})
            return rows
        finally:
            client.close()

    def _wants_tablet_app(self, dev: Any, build: str) -> bool:
        """A Linux PC whose tablets' app is not this build (or none, or it does not say:
        an older node). Windows PCs drive no tablets (01-overview D3)."""
        client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
        try:
            for _ in range(10):  # an updated node is restarting: give it a little time
                try:
                    info = client.get("/v1/info", timeout_s=5)
                    break
                except ZlError:
                    time.sleep(3)
            else:
                return True  # cannot tell: send it, the Deployer says if it does not fit
        finally:
            client.close()
        if "linux" not in str(info.get("os") or "").lower():
            return False
        return info.get("tablet_app") != build

    def deploy_modules(self, q: Query) -> dict[str, Any]:
        node = str(q.params.get("node", ""))
        d = self._deploy()
        return {"node": node, **d.modules(node), "deployer": d.health(node)}

    def deploy_remove(self, req: Request) -> dict[str, Any]:
        p = req.params
        with self.run.while_idle("Removing"):
            return self._deploy().remove(
                str(p.get("node", "")), str(p.get("id", "")), bool(p.get("purge")),
                bool(p.get("dry_run")),
            )  # fmt: skip

    def install_linux(self, req: Request) -> dict[str, Any]:
        """06 11.3 first install over SSH (02 2-5). The password is used and dropped."""
        p = req.params
        packages = self.data_dir / "packages"
        # 2026-10-16 user (a kit for a person with no internet): the packages that come
        # with the program (next to its modules folder) when none were put here
        bundled = self.run.modules_dir.parent / "packages"
        if not list(packages.glob("zeroloss-agent_*_amd64.deb")) and bundled.is_dir():
            packages = bundled
        deb = Path(str(p["package"])) if p.get("package") else newest_deb(packages)
        mods = [Path(str(m)) for m in p.get("modules") or []] or newest_modules(packages)
        mods += newest_tablet(packages)  # tablets work on a new PC from the start
        target = Target(
            str(p.get("host", "")), str(p.get("user", "")), str(p.get("password", "")),
            name=str(p.get("name", "")), ssh_port=int(p.get("ssh_port", 22)),
            node_port=int(p.get("node_port", CORE_PORT)),
            deployer_port=int(p.get("deployer_port", DEPLOYER_PORT)),
            auto_login=p.get("auto_login") is True,
        )  # fmt: skip
        with self.run.while_idle("Installing"):  # INS-022
            inst = LinuxInstaller(
                KnownHosts(self.data_dir / "ssh-known-hosts.json"), deb, mods, self.devices,
                self.run.boot_id, self.run.generation, tools_dir=packages / "tools",
            )  # fmt: skip
            return inst.install(target, bool(p.get("accept_new_host_key")))

    def remove_linux(self, req: Request) -> dict[str, Any]:
        """06 7 / 2026-10-31: ZeroLoss off a registered Linux PC over SSH (its account and
        password once, used and dropped), then out of Devices. Test data stays unless
        ``purge`` (06 1 5)."""
        p = req.params
        dev = self.devices.get(str(p.get("name", "")))
        target = Target(dev.host, str(p.get("user", "")), str(p.get("password", "")),
                        name=dev.name, ssh_port=int(p.get("ssh_port", 22)))  # fmt: skip
        with self.run.while_idle("Removing ZeroLoss"):
            hosts = KnownHosts(self.data_dir / "ssh-known-hosts.json")
            out = LinuxRemover(hosts, self.devices).remove(dev.name, target,
                                                           p.get("purge") is True)  # fmt: skip
        self.node.bus.publish(Event("ZL-INFO", "info", f"ZeroLoss removed from {dev.name}"))
        return out

    def made_fetch(self, req: Request) -> dict[str, Any]:
        """The sender's finished 10-minute made files, each with its description, into a
        folder on this PC (2026-10-31 user: the kit carries "변환한거 10분짜리 3개"). Each
        row names the movie it was made from and its channel."""
        p = req.params
        dev = self.devices.get(str(p.get("node", "")))
        out = Path(str(p.get("out", "")))
        if not out.is_absolute():
            raise ZlError("DSC-012", "Request rejected", detail="out: a folder path on this PC")
        out.mkdir(parents=True, exist_ok=True)
        client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
        iptv = "/v1/modules/iptv/sender"
        try:
            listed = client.get(f"{iptv}/sources", timeout_s=120)
            rows = [{"movie": f.get("name"), "ch": m.get("ch"), "quality": m.get("quality"),
                     "file": m["file"]} for f in listed.get("files") or []
                    for m in f.get("made") or []
                    if m.get("file") and not m.get("whole") and not m.get("as_is")]  # fmt: skip
            for r in rows:
                ts = str(r["file"])
                for name in (ts, ts.removesuffix(".ts") + ".json"):
                    client.download(f"{iptv}/made_file", out / name, timeout_s=1800, name=name)
        finally:
            client.close()
        return {"node": dev.name, "out": str(out), "files": rows}

    def remove_tablet_app(self, req: Request) -> dict[str, Any]:
        """06 7: the ZeroLoss app off one tablet on a PC's USB (only between tests)."""
        p = req.params
        dev = self.devices.get(str(p.get("device", "")))
        client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
        try:
            with self.run.while_idle("Removing the tablet app"):
                got = client.command("/v1/tablets/uninstall_app", self.run.generation, None,
                                     timeout_s=120, serial=str(p.get("serial")))  # fmt: skip
                return dict(got)
        finally:
            client.close()

    def guide(self) -> str | None:
        """The install and use guide for the screen's Help (08 2, 2026-10-31): the copy that
        came with this program (next to its modules folder), else the source one."""
        for f in (self.run.modules_dir.parent / GUIDE,
                  Path(__file__).resolve().parents[1] / "tools" / "kit" / GUIDE):  # fmt: skip
            try:
                return f.read_text(encoding="utf-8").replace("{BUILD}", self.node.build)
            except OSError:
                continue
        return None

    def sources(self, q: Query) -> dict[str, Any]:
        """02 2-10: the module's source video list on a device (S-48)."""
        rules = load_run_entry(self.run.modules_dir, q.params.get("module", ""))
        path = getattr(rules, "sources_path", None)
        if path is None:
            raise ZlError("RUN-013", "This module has no source video list")
        dev = self.devices.get(q.params.get("node", ""))
        client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
        try:
            return dict(client.get(str(path), timeout_s=60))
        finally:
            client.close()

    def wifi_password(self, req: Request) -> dict[str, Any]:
        """A picked band network's password, typed in Devices next to its SSID ("" forgets
        it). Kept encrypted; never answered back, logged or put in a plan or report."""
        ssid, password = req.params.get("ssid"), req.params.get("password", "")
        if not isinstance(ssid, str) or not ssid or not isinstance(password, str):
            raise ZlError("DSC-012", "Request rejected", detail="params ssid and password")
        return self.band_passwords.save(ssid, password)

    def wifi_scan(self, q: Query) -> dict[str, Any]:
        """02 2-10: the networks one Wi-Fi card of a PC sees (S-65 "Wi-Fi to test"); read
        only, never during a test (a scan holds the card for seconds)."""
        if self.run.state is not RunState.IDLE:
            raise ZlError("RUN-012", "Wi-Fi can be scanned only between tests", "A test runs.")
        name = q.params.get("node", "")  # the Controller's own card: through its own node
        dev = self._own()[0] if name == THIS_PC else self.devices.get(name)
        client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
        try:
            return dict(client.get("/v1/wifi/scan", timeout_s=45, nic=q.params.get("nic", "")))
        finally:
            client.close()

    def discover(self, req: Request) -> dict[str, Any]:
        """02 4-1: find nodes (broadcast, sender addresses, a range). Never in a test."""
        p = req.params
        scan = p.get("scan")
        if scan is not None and not isinstance(scan, str):
            raise ZlError("DSC-012", "Request rejected", detail="params.scan must be text")
        with self.run.between_tests("Finding devices"):
            if bool(p.get("try_sender", True)):
                self.run.wan_route()  # 13 1: 10.0.0.1 is only reachable through the router
            return discover(self.devices, self.run.boot_id, int(p.get("port", CORE_PORT)),
                            scan, bool(p.get("try_sender", True)), on_move=self._moved)  # fmt: skip

    def _own_side(self, others: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """This PC as a row: its cards and addresses, on the router LAN side when one of
        its cards shares a network with a LAN-side PC."""
        links = [x.to_dict() for x in self.node.links]
        ips = [
            str(x.get("ip")) for x in links if x.get("ip") and not str(x["ip"]).startswith("127.")
        ]
        lan = {".".join(str(s.get("host") or "").split(".")[:3]) for s in others.values()
               if s.get("side") == "lan"}  # fmt: skip
        host = next((ip for ip in ips if ".".join(ip.split(".")[:3]) in lan), ips[0] if ips else "")
        return {"reachable": True, "side": "lan" if host else "unknown", "isp_role": False,
                "links": links, "host": host, "addresses": ips,
                "labels": address_labels(links), "this_pc": True}  # fmt: skip

    def _own(self) -> tuple[Device, list[dict[str, Any]]]:
        """This PC's node as a device (127.0.0.1) and its cards (Windows: win_links)."""
        me = Device(THIS_PC, "127.0.0.1", int(self.node.api.port), self.node.device_id,
                    "controller", "")  # fmt: skip
        return me, [x.to_dict() for x in self.node.links]

    def _revive(self, name: str) -> str:
        """Reset All (F11): start a node that was shut down, else restart it, through its
        Deployer; waits until the node answers (the Deployer confirms)."""
        dep = self._deploy()
        health = dep.health(name)
        action = "start" if health.get("node_stopped") else "restart"
        dep.node(name, action)
        self.node.bus.publish(Event("ZL-INFO", "info", f"{name}: ZeroLoss {action}ed by Reset All",
                                    device=name))  # fmt: skip
        return action

    def nodes(self, req: Request) -> dict[str, Any]:
        """02 2-8: fault screen actions over each PC's Deployer (10991): ``restart`` one
        node (Restart Agent), ``stop`` / ``start`` every node (Shut Down All / Start all).
        Between tests only. Every device gets its own row; one failure stops no other."""
        action = req.params.get("action")
        if action not in ("restart", "stop", "start"):
            raise ZlError("DSC-012", "Request rejected", detail="action: restart/stop/start")
        name = req.params.get("node")
        if action == "restart" and not isinstance(name, str):
            raise ZlError("DSC-012", "Request rejected", detail="restart needs params.node")
        targets = [name] if action == "restart" else [d.name for d in self.devices.all()]
        rows = []
        with self.run.between_tests({"restart": "Restart Agent", "stop": "Shut Down All",
                                     "start": "Start all"}[str(action)]):  # fmt: skip
            for n in targets:
                try:
                    self._deploy().node(str(n), str(action))
                    rows.append({"device": n, "done": True, "error": None})
                except ZlError as e:
                    rows.append({"device": n, "done": False, "error": e.to_dict()})
        return {"action": action, "ok": all(r["done"] for r in rows), "devices": rows}

    def wifi_set(self, req: Request) -> dict[str, Any]:
        """02 2-7: check (16 S-51) and keep the test Wi-Fi; the answer has no password."""
        p = req.params
        return self.wifi.save(check(p.get("ssid"), p.get("security"), p.get("password")))

    def precheck(self, req: Request) -> dict[str, Any]:
        """02 2-6: light (1-4) or full (1-5) check of a plan; never starts a test."""
        return self.run.precheck(self._plan(req.params.get("plan")),
                                 full=bool(req.params.get("full", False)))  # fmt: skip

    def prepare_state(self, q: Query) -> dict[str, Any]:
        return self.run.prepare_state(
            str(q.params.get("module", "iptv")), str(q.params.get("sender", ""))
        )

    def run_stop(self, _: Request) -> dict[str, Any]:
        return self.run.stop()

    def quit(self, _: Request) -> dict[str, Any]:
        """02 2-13: end the service when nothing runs (the portable window closed; GAP-116).
        A test, a start or a broadcast keeps it running: closing the screen never stops a
        test (12 1-0)."""
        if self.run.state is not RunState.IDLE or self.run.broadcast_status().get("on"):
            raise ZlError("RUN-012", "Not now: a test or the broadcast is running",
                          "Closing the screen never stops a test.",
                          "The service ends when the window closes after the test.")  # fmt: skip
        self.quit_requested.set()
        return {"quitting": True}

    def diagnostics(self, req: Request) -> dict[str, Any]:
        """02 2-13 (20 GAP-78): one file with every registered node's ``/v1/logs``, this
        PC's own bundle and the Controller's state; nodes that do not answer are noted,
        not a failure. Saved next to the run's report."""
        run = str(req.params.get("run") or self.run.run_id or self._last_run() or "")
        if run and not RUN_ID.match(run):
            raise ZlError("DSC-012", "Request rejected", detail="run must be a run ID")
        folder = self.reports_dir / (run or "diagnostics")
        folder.mkdir(parents=True, exist_ok=True)
        out = folder / f"diagnostics-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.zip"
        nodes: dict[str, str] = {}
        own = self.node.logs_bundle(Query({"run": run}, "127.0.0.1", "127.0.0.1"))
        try:
            with tempfile.TemporaryDirectory(prefix="zeroloss-diag-") as tmp:
                got: dict[str, Path] = {"controller.zip": own}
                for dev in self.devices.all():
                    client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
                    try:
                        got[f"{dev.name}.zip"] = client.download(
                            "/v1/logs", Path(tmp) / f"{dev.name}.zip", timeout_s=120.0, run=run
                        )
                        nodes[dev.name] = "ok"
                    except ZlError as e:
                        nodes[dev.name] = f"{e.code} {e.what}"
                    finally:
                        client.close()
                summary = self.reports_dir / run / "summary.json" if run else None
                state = {
                    "controller/devices.json": self.devices.to_dict(),
                    "controller/setup.json": self.setup.get(),
                    "controller/status.json": self.status(Query({}, "127.0.0.1", "127.0.0.1")),
                    "controller/problems.json": self.problems.snapshot(),
                    "controller/actions.json": self.actions.snapshot(),
                }
                if summary is not None and summary.is_file():
                    state["controller/summary.json"] = json.loads(summary.read_text("utf-8"))
                part = out.with_name(out.name + ".part")
                with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
                    for name, path in got.items():
                        z.write(path, name)
                    for name, value in state.items():
                        clean, _ = diag_bundle.redact(value)
                        z.writestr(name, json.dumps(clean, indent=1, ensure_ascii=False,
                                                    default=str))  # fmt: skip
                    z.writestr("manifest.json", json.dumps(
                        {"run": run or None, "build": self.node.build, "nodes": nodes,
                         "made_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                        indent=1))  # fmt: skip
                part.replace(out)
        finally:
            own.unlink(missing_ok=True)
        return {"path": str(out), "bytes": out.stat().st_size, "nodes": nodes}

    def _test_ended(self) -> None:
        self.problems.resolve_all("test ended")
        with self._low_lock:  # still low: told again at the next check (09 SYS-010)
            self._low = {}

    def storage_low(self) -> list[dict[str, Any]]:
        with self._low_lock:
            return [{"device": n, **d} for n, d in sorted(self._low.items())]

    def _awake_watch(self) -> None:
        """SYS-005 on the Controller PC (GAP-116): awake while a test or the broadcast
        runs — a sleeping Controller makes every PC safe-stop after 10 s (CTL-008)."""
        while not self._closing.wait(AWAKE_WATCH_S):
            busy = self.run.state is not RunState.IDLE or bool(
                self.run.broadcast_status().get("on")
            )
            self.node.want_awake("test", busy)
        self.node.want_awake("test", False)

    def _idle_watch(self) -> None:
        """GAP-153 (2026-10-10: "whenever I try alone without you, nothing starts"):
        between tests ask every registered device ``/v1/health`` every IDLE_LINK_S
        (a test asks every second itself). A device that does not answer is looked for
        (``_relocate``, at most every 30 s). A FAULT left only because devices did not
        answer Reset All is reset again once all of them answer."""
        clients: dict[str, NodeClient] = {}
        with ThreadPoolExecutor(8, thread_name_prefix="idle-link") as pool:
            while not self._closing.wait(IDLE_LINK_S):
                if self.run.state not in (RunState.IDLE, RunState.FAULT):
                    continue
                try:
                    answered = self._idle_round(clients, pool)
                    self._answers_again(answered)
                    self._fetch_owed(answered)
                    if (  # no search for PCs that do not answer: the person picks (2026-10-17)
                        answered
                        and all(answered.values())
                        and self._only_unanswered_fault()
                        and time.monotonic() - self._auto_reset_at >= AUTO_RESET_EVERY_S
                    ):
                        self._auto_reset_at = time.monotonic()
                        self.node.bus.publish(Event("ZL-INFO", "info", "Every PC answers again: "
                                                    "Reset All runs by itself"))  # fmt: skip
                        self.run.reset()
                except ZlError:
                    continue  # a test started meanwhile, or the reset failed again (FAULT)
                except Exception as e:  # the watch never dies: shown, tried again
                    self.node.bus.publish(
                        Event("ZL-SYS-008", "warn", f"Device check failed: {e!r}")
                    )
        for c in clients.values():
            c.close()

    def _idle_round(
        self, clients: dict[str, NodeClient], pool: ThreadPoolExecutor
    ) -> dict[str, bool]:
        devs = self.devices.all()
        for d in devs:
            c = clients.get(d.name)
            if c is None or (c.host, c.port) != (d.host, d.port):  # new, or moved
                if c is not None:
                    c.close()
                clients[d.name] = NodeClient(d.host, d.port, self.run.boot_id, d.name)
        for gone in set(clients) - {d.name for d in devs}:
            clients.pop(gone).close()

        def ask(name: str) -> bool:
            try:
                h = clients[name].get("/v1/health", timeout_s=IDLE_LINK_TIMEOUT_S)
                return str(h.get("device_id") or "") == ids[name]  # GAP-155: that PC
            except ZlError:
                return False

        names = [d.name for d in devs]
        ids = {d.name: d.device_id for d in devs}
        return dict(zip(names, pool.map(ask, names), strict=True))

    def _answers_again(self, answered: dict[str, bool]) -> None:
        """GAP-153 (2026-10-10, user: "still waiting"): a PC that did not answer
        (DSC-014: the start failed, its power-cycle action waits) and answers now clears
        it, so the problem and the "Restart the PC" action close by themselves."""
        silent = {
            str(p.get("device"))
            for p in self.problems.snapshot()["problems"]
            if p.get("active") and str(p.get("code", "")).endswith("DSC-014")
        }
        if "" in silent and answered and all(answered.values()):  # opened without a PC
            self.node.bus.publish(Event("ZL-INFO", "info", "Every PC answers again",
                                        data={"clears": ["ZL-DSC-014"]}))  # fmt: skip
        for name, ok in answered.items():
            if ok and name in silent:
                self.node.bus.publish(Event(
                    "ZL-INFO", "info", f"{name} answers again", device=name,
                    data={"clears": ["ZL-DSC-014"]},
                ))  # fmt: skip

    def _only_unanswered_fault(self) -> bool:
        """FAULT from the last Reset All (no test fault after it) where every failing
        device simply did not answer."""
        if self.run.state is not RunState.FAULT:
            return False
        r = self.run.reset_result or {}
        fault = self.run.fault or {}
        if fault and str(fault.get("at_utc", "")) > str(r.get("updated_utc", "")):
            return False  # a test fault came after it: the operator looks at that first
        bad = [x for x in r.get("devices") or [] if not x.get("clean")]
        return bool(bad) and not r.get("ok") and all(not x.get("answered") for x in bad)

    def _storage_watch(self) -> None:
        while not self._closing.wait(0 if not self._low_checked else STORAGE_WATCH_S):
            self._low_checked = True
            try:
                self.check_storage()
            except Exception as e:  # the watch never dies: shown, tried again next minute
                self.node.bus.publish(Event("ZL-SYS-008", "warn", f"Storage check failed: {e!r}"))

    def check_storage(self) -> None:
        """09 SYS-010: below 20 % or 50 GB free on any PC is told once per PC and
        cleared when it is back. A device that does not answer keeps its last state."""
        disks: dict[str, dict[str, Any]] = {"controller": self.node.disk(Query({}, "", ""))}
        names = {"controller"}
        for dev in self.devices.all():
            names.add(dev.name)
            client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
            try:
                disks[dev.name] = client.get("/v1/disk", timeout_s=DISK_TIMEOUT_S)
            except ZlError:
                continue  # not reachable: DSC-014 is about that
            finally:
                client.close()
        told: list[Event] = []
        with self._low_lock:
            for name in [n for n in self._low if n not in names]:  # removed device
                del self._low[name]
                told.append(Event("ZL-INFO", "info", f"{name} removed", device=name,
                                  data={"clears": ["ZL-SYS-010"]}))  # fmt: skip
            for name, d in disks.items():
                free, total = d.get("free_mb"), d.get("total_mb")
                if not isinstance(free, int) or not isinstance(total, int) or total <= 0:
                    continue
                low = free < LOW_MB or free * 100 < LOW_PCT * total
                if low and name not in self._low:
                    pct = 100 * free / total
                    told.append(Event("ZL-SYS-010", "warn",
                                      f"Storage low on {name}: {free / 1024:.1f} GB free "
                                      f"({pct:.0f}%)", device=name,
                                      data={"free_mb": free, "total_mb": total}))  # fmt: skip
                if low:
                    self._low[name] = {"free_mb": free, "total_mb": total}
                elif name in self._low:
                    del self._low[name]
                    told.append(Event("ZL-INFO", "info", f"Storage on {name} is enough again",
                                      device=name, data={"clears": ["ZL-SYS-010"]}))  # fmt: skip
        for e in told:
            self.node.bus.publish(e)

    def _setting(self, sid: str) -> Any:
        """A setting as chosen on the screen, else its default (16)."""
        chosen = (self._setup_now().get("settings") or {}).get(sid)
        return chosen if chosen is not None else (self.settings_schema.get(sid) or {}).get("value")

    def _start_web(self) -> None:
        """02 12 results web; S-44 Off keeps it closed, a busy port is CTL-006 (the test
        can still run)."""
        if str(self._setting("S-44")) == "Off":
            return
        web = ResultsWeb(
            self.reports_dir,
            listing=lambda: self.reports(Query({}, "127.0.0.1", "127.0.0.1")),
            live=lambda: self.status(Query({}, "127.0.0.1", "127.0.0.1")),
            live_data=self.run.live,
            guide=self.guide,
            access=lambda: str(self._setting("S-44") or "Same subnet"),
            networks=_local_networks,
            running=self._running_run,
            traffic=self.web_traffic,
            port=self._web_port,
        )  # fmt: skip
        try:
            web.start()
        except OSError as e:
            if not self._web_busy:
                why = f"Results web port {self._web_port} is in use: {e}"
                self.node.bus.publish(Event("ZL-CTL-006", "warn", why))
            self._web_busy = True
            if not self._closing.is_set():  # the port may come free; the test goes on
                retry = threading.Timer(WEB_RETRY_S, self._start_web)
                retry.daemon = True
                retry.start()
            return
        if self._closing.is_set():
            web.stop()
            return
        self.web = web
        if self._web_busy:
            self._web_busy = False
            text = f"Results web is open on port {self._web_port}"
            self.node.bus.publish(Event("ZL-INFO", "info", text, data={"clears": ["ZL-CTL-006"]}))

    def _records_later(self, run: str, devices: list[str]) -> None:
        """After a report (02 2-13): fetch the run's 1-second records in the background,
        so the report is never late."""
        if self._closing.is_set():
            return
        t = threading.Thread(target=self._collect, args=(run, devices), name="records",
                             daemon=True)  # fmt: skip
        self._records_thread = t
        t.start()

    def _collect(self, run: str, names: list[str]) -> dict[str, Any]:
        with self._records_lock:
            devices = [d for d in self.devices.all() if d.name in names]
            got = collect_records(self.reports_dir, run, devices, self.run.boot_id, self._closing,
                                  self.run.generation)  # fmt: skip
            # iptv 2.4: the cause candidates again, now with the 1-second records
            got["reanalyzed"] = bool(got["files"]) and reanalyze(self.reports_dir / run,
                                                                 self.modules_dir)  # fmt: skip
        owed = self._owed.setdefault(run, set())
        owed.difference_update(n for n in names if n not in got["missing"])
        for name, why in got["missing"].items():
            first = name not in owed
            owed.add(name)
            if first:  # said once; fetched by itself when the PC answers again (GAP-156)
                self.node.bus.publish(Event(
                    "ZL-RPT-005", "warn", f"Data missing from {name}: {why} (fetched by "
                    "itself when it answers again)", device=name,
                    data={"run_id": run, "why": why}))  # fmt: skip
        if not owed:
            self._owed.pop(run, None)
        return got

    def _fetch_owed(self, answered: dict[str, bool]) -> None:
        """GAP-156 (10-11: the T460 did not answer at the end, the loss had no cause):
        a PC that answers again gives the records it still owes, in the background."""
        if not self._owed or time.monotonic() - self._owed_at < OWED_EVERY_S:
            return
        for run, names in list(self._owed.items()):
            ready = [n for n in names if answered.get(n)]
            if ready and (self._records_thread is None or not self._records_thread.is_alive()):
                self._owed_at = time.monotonic()
                self._records_later(run, ready)
                return

    def broadcast(self, req: Request) -> dict[str, Any]:
        """02 2-13 ``POST /v1/controller/broadcast``: only ``{"on": false}``; Start of an
        Always-on test (S-13) turns it on."""
        if req.params.get("on") is not False:
            why = "params.on must be false (Start turns the broadcast on)"
            raise ZlError("DSC-012", "Request rejected", detail=why)
        return self.run.stop_broadcast()

    def channel(self, req: Request) -> dict[str, Any]:
        """02 2-13 ``POST /v1/controller/channel``: one receiver to another channel."""
        device, ch = req.params.get("device"), req.params.get("ch")
        if (
            not isinstance(device, str)
            or not device
            or not isinstance(ch, int)
            or isinstance(ch, bool)
        ):
            raise ZlError("DSC-012", "Request rejected", detail="params device (name), ch (number)")
        return self.run.channel(device, ch)

    def records(self, req: Request) -> dict[str, Any]:
        """02 2-13 ``POST /v1/controller/records``: fetch a run's 1-second records again
        from every registered device (the ones already here are kept)."""
        run = run_storage.check_run(str(req.params.get("run", "")))
        if run == self._running_run():
            raise ZlError("RUN-012", f"{run} is the test running now", f"It is {self.run.state}.",
                          "Fetch the records after the test.")  # fmt: skip
        return self._collect(run, [d.name for d in self.devices.all()])

    def verify(self, req: Request) -> dict[str, Any]:
        """02 2-13 ``POST /v1/controller/verify`` (20 GAP-81): the product's own checks,
        Core's and every module's (``run_entry.self_checks()``), and on request a short
        real test with the current choice."""
        suites = req.params.get("suites") or list(self_check.DEFAULT_SUITES)
        if not isinstance(suites, list) or not all(s in self_check.SUITES for s in suites):
            raise ZlError("DSC-012", "Request rejected",
                          detail=f"suites from {', '.join(self_check.SUITES)}")  # fmt: skip
        seconds = req.params.get("seconds", 60)
        if not isinstance(seconds, int) or not LOOPBACK_S[0] <= seconds <= LOOPBACK_S[1]:
            raise ZlError("DSC-012", "Request rejected", detail="seconds 10 .. 3600")
        if "loopback" in suites and self.run.state is not RunState.IDLE:
            raise ZlError("RUN-012", "The loopback check needs the Controller idle",
                          f"It is {self.run.state}.", "Wait for the test to end.")  # fmt: skip
        result = self_check.run(self._self_checks(), [s for s in suites if s != "loopback"])
        run_id = None
        if "loopback" in suites:
            rows, run_id = self._loopback(seconds)
            result["checks"] += rows
            result["passed"] += sum(1 for r in rows if r["ok"])
            result["failed"] += sum(1 for r in rows if not r["ok"])
        return {**result, "suites": suites, "run_id": run_id}

    def _self_checks(self) -> list[self_check.Check]:
        checks = self_checks.checks()
        for info in discover_modules(self.modules_dir):
            if not info.enabled or not info.manifest.get("run_entry"):
                continue
            try:
                entry = load_run_entry(self.modules_dir, info.id)
                own = getattr(entry, "self_checks", None)
                checks += own() if own is not None else []
            except Exception as e:  # shown as a failed line, never hidden

                def broken(e: Exception = e) -> str:
                    raise self_check.CheckFailed(f"the module's checks did not load: {e!r}")

                checks.append(("accuracy", f"module {info.id} checks", broken))
        return checks

    def _loopback(self, seconds: int) -> tuple[list[dict[str, Any]], str]:
        """One short real test with the current choice (2-11): one line per receiver."""
        plan = {**self._setup_plan()["plan"], "duration_s": seconds}
        started = time.monotonic()
        run_id = self.run.start(plan)
        deadline = started + seconds + LOOPBACK_MARGIN_S
        while self.run.state not in (RunState.IDLE, RunState.FAULT) or self.run.run_id != run_id:
            if time.monotonic() > deadline or self._closing.is_set():
                self.run.stop("stopped_by_user")
                break
            self._closing.wait(0.5)
        result = self.run.result if self.run.run_id == run_id else None
        ms = round((time.monotonic() - started) * 1000, 1)
        failed = {"suite": "loopback", "name": "short real test", "ok": False, "ms": ms}
        if not result or result.get("run_id") != run_id:
            return [{**failed, "detail": f"no result for {run_id} ({self.run.state})"}], run_id
        rows = []
        for dev in result.get("devices", []):
            colors = dev.get("colors", {})
            bad = {c: colors.get(c, 0) for c in ("red", "purple", "grey") if colors.get(c)}
            ok = result["verdict"] == "PASS" and not bad
            detail = f"{result['verdict']}, {seconds} s" + (f", {bad}" if bad else ", no bad cells")
            rows.append({"suite": "loopback", "name": f"short real test: {dev.get('device')}",
                         "ok": ok, "detail": detail, "ms": ms})  # fmt: skip
        return rows or [{**failed, "detail": f"{result['verdict']}: no receiver"}], run_id

    def _running_run(self) -> str | None:
        """The run of the test on now (not IDLE or FAULT), else None."""
        st = self.run.status()
        return None if st["state"] in (RunState.IDLE, RunState.FAULT) else st["run_id"]

    def storage_list(self, _: Query) -> dict[str, Any]:
        """02 2-13 ``GET /v1/controller/storage`` (F15): every run's size in the report
        folder, on this PC's node and on every device, biggest run first."""
        where: dict[str, dict[str, dict[str, int]]] = {}  # run -> place -> usage
        in_use: set[str] = set()
        running = self._running_run()

        def add(place: str, runs: dict[str, dict[str, int]]) -> None:
            for run, u in runs.items():
                where.setdefault(run, {})[place] = {
                    "bytes": u["bytes"],
                    "raw_bytes": u["raw_bytes"],
                }

        add("Reports", run_storage.list_runs(self.reports_dir))
        own = self.node.storage(Query({}, "127.0.0.1", "127.0.0.1"))
        add("controller", {r["run"]: r for r in own["runs"]})
        in_use |= {r["run"] for r in own["runs"] if r["in_use"]}
        nodes: dict[str, str] = {}
        free: dict[str, int | None] = {"controller": own["free_mb"]}
        for dev in self.devices.all():
            client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
            try:
                got = client.get("/v1/storage", timeout_s=STORAGE_TIMEOUT_S)
                add(dev.name, {r["run"]: r for r in got["runs"]})
                in_use |= {r["run"] for r in got["runs"] if r["in_use"]}
                free[dev.name] = got["free_mb"]
                nodes[dev.name] = "ok"
            except ZlError as e:
                nodes[dev.name] = f"{e.code} {e.what}"
            finally:
                client.close()
        runs: list[dict[str, Any]] = [{"run": run,
                 "bytes": sum(u["bytes"] for u in places.values()),
                 "raw_bytes": sum(u["raw_bytes"] for u in places.values()),
                 "in_use": run == running or run in in_use, "where": places}
                for run, places in where.items()]  # fmt: skip
        runs.sort(key=lambda x: (-int(x["bytes"]), str(x["run"])))
        return {"reports_dir": str(self.reports_dir), "free_mb": own["free_mb"], "runs": runs,
                "nodes": nodes, "free_mb_of": free}  # fmt: skip

    def storage_delete(self, req: Request) -> dict[str, Any]:
        """02 2-13 ``POST /v1/controller/storage/delete`` (F15): one run (or its raw data)
        everywhere; never the test on now (RUN-012). A device that does not answer is
        written down; the rest is removed."""
        run = run_storage.check_run(str(req.params.get("run", "")))
        raw_only = req.params.get("raw_only", False)
        if not isinstance(raw_only, bool):
            raise ZlError(
                "DSC-012", "Request rejected", detail="params.raw_only must be true/false"
            )
        if run == self._running_run():
            raise ZlError("RUN-012", f"{run} is the test running now", f"It is {self.run.state}.",
                          "Stop the test first, or choose another run.")  # fmt: skip
        row = self.experience.start_action("cli.storage.delete", "human",
                                           note=f"{run} raw_only={raw_only}")  # fmt: skip
        nodes: dict[str, str] = {}
        errors: list[str] = []
        done = run_storage.delete(self.reports_dir, run, raw_only)
        deleted = int(done["deleted_bytes"])
        errors += [f"Reports: {e}" for e in done["errors"]]
        nodes["Reports"] = "ok"
        try:
            own = self.node.storage_delete(
                Request("local", 0, None, {"run": run, "raw_only": raw_only})
            )
            deleted += int(own["deleted_bytes"])
            errors += [f"controller: {e}" for e in own["errors"]]
            nodes["controller"] = "ok"
        except ZlError as e:
            nodes["controller"] = f"{e.code} {e.what}"
        for dev in self.devices.all():
            client = NodeClient(dev.host, dev.port, self.run.boot_id, dev.name)
            try:
                got = client.command("/v1/storage/delete", self.run.generation, None,
                                     timeout_s=STORAGE_TIMEOUT_S, run=run,
                                     raw_only=raw_only)  # fmt: skip
                deleted += int(got["deleted_bytes"])
                errors += [f"{dev.name}: {e}" for e in got["errors"]]
                nodes[dev.name] = "ok"
            except ZlError as e:
                nodes[dev.name] = f"{e.code} {e.what}"
            finally:
                client.close()
        clean = not errors and all(v == "ok" for v in nodes.values())
        self.experience.finish_action(row, "resolved" if clean else "not_resolved",
                                      note=f"{deleted} bytes")  # fmt: skip
        return {"run": run, "raw_only": raw_only, "deleted_bytes": deleted, "nodes": nodes,
                "errors": errors}  # fmt: skip

    def _last_run(self) -> str | None:
        """The newest run folder that has a report."""
        runs = [d for d in self.reports_dir.glob("*/summary.json")]
        return max(runs, key=lambda p: p.stat().st_mtime).parent.name if runs else None

    def reports(self, _: Query) -> dict[str, Any]:
        """02 2-13 ``GET /v1/controller/reports``: past reports newest first (F15 S8).
        A folder without a readable ``summary.json`` is a row with the reason."""
        running = self._running_run()
        rows = []
        for d in self.reports_dir.iterdir() if self.reports_dir.is_dir() else []:
            summary, plan = d / "summary.json", d / "plan.json"
            if not d.is_dir() or not (summary.is_file() or plan.is_file()):
                continue  # e.g. diagnostics/
            row: dict[str, Any] = {"run": d.name, "started": _started(d),
                                   "report": str(d / "report.html"), "folder": str(d)}  # fmt: skip
            try:
                s = json.loads(summary.read_text(encoding="utf-8"))
                row |= {k: s.get(k) for k in REPORT_KEYS} | {"why": None}
            except (OSError, ValueError) as e:
                row |= dict.fromkeys(REPORT_KEYS) | {
                    "why": "Running" if d.name == running
                    else f"ZL-RPT-008 No report for {d.name}"
                    + (f" ({e.__class__.__name__})" if summary.is_file() else "")}  # fmt: skip
            rows.append(row)
        rows.sort(key=lambda r: (r["started"], r["run"]), reverse=True)
        return {"reports_dir": str(self.reports_dir), "reports": rows}

    def report(self, q: Query) -> dict[str, Any]:
        run_id = q.params.get("run", "")
        if not RUN_ID.match(run_id):
            raise ZlError("DSC-012", "Request rejected", detail="run must be a run ID")
        path = self.reports_dir / run_id / "summary.json"
        if not path.is_file():
            raise ZlError("RPT-008", f"No report for {run_id}", detail=str(path))
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return data


REPORT_KEYS = ("test_type", "quality", "planned_s", "actual_s", "verdict", "verdict_reason",
               "with_warnings")  # fmt: skip


def _started(folder: Path) -> str:
    """Start time from the run ID (``R%Y%m%d-%H%M%S``, this PC's clock), else the
    folder's time (02 2-13 ``reports``)."""
    try:
        when = datetime.strptime(folder.name, "R%Y%m%d-%H%M%S")
    except ValueError:
        when = datetime.fromtimestamp(folder.stat().st_mtime)
    return when.strftime("%Y-%m-%d %H:%M")


def _local_networks() -> list[str]:
    """This PC's IPv4 networks (``ip/mask``) for S-44 Same subnet."""
    out = []
    for link in read_links(wifi_of=lambda nic: None):
        mask = ipv4_netmask(link.nic) if link.ip else None
        if link.ip and mask:
            out.append(f"{link.ip}/{mask}")
    return out
