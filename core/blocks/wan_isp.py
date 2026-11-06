"""The sender PC as the ISP of the router's WAN port (docs/07-network.md 3.2 B).

One job: while the WAN role is on, keep the router's WAN addressed without anyone
touching the router — pick the WAN NIC (the one wired NIC that is up and has no IPv4
address, or the one set), add 10.0.0.1/24 to it, and run the one-address DHCP server.
Nobody has to turn it on (2026-09-26, 20 GAP-75): with no choice saved (``auto``) it
starts by itself on a wired NIC that stays up without an address and where no DHCP
server answers — a router WAN port waits for an address, a router LAN port gives one.
A NIC that still holds the router-LAN lease it had before the cable moved (its gateway
no longer answers ARP) is taken over too (20 GAP-88). With NetworkManager the address
is a profile ``zeroloss-wan`` (it replaces the old lease; nothing to fight over).
It stays on across tests (the Controller reaches this PC only through that address);
it is undone only when the role is turned off (and at package removal). Only what we
added is taken away again. Linux for now (``ip``); Windows comes with V18.
"""

from __future__ import annotations

import ipaddress
import json
import random
import shutil
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.blocks.arp_probe import ProbeResult
from core.blocks.arp_probe import alive as arp_alive
from core.blocks.arp_probe import probe as arp_probe
from core.blocks.dhcp_server import OneLeaseServer, other_servers
from core.blocks.link_monitor import gateway_of, ipv4_of
from core.blocks.wifi_join import Runner, run_command

SERVER = "10.0.0.1/24"  # 07 3 table, B: the sender PC is the gateway
LEASE_IP = "10.0.0.100"
LOST_WINDOW_S = 600.0  # 09 SND-023: more than LOST_MAX losses in this window = stop
LOST_MAX = 3
RETRY_S = 10.0  # waiting for the cable to move from LAN to WAN (first install)
AUTO_SETTLE_S = 20.0  # auto: a router LAN hands out an address well within this
NO_REQUEST_S = 60.0  # SND-011
NM_PROFILE = "zeroloss-wan"  # 07 3.2 B, GAP-88
CONFIG = "agent.json"
LEASE_FILE = "wan-lease.json"

Emit = Callable[[str, str, str, dict[str, Any]], None]  # code, level, text, data


UNDO_TRIES = 2  # SND-014: the first try and one more


class WanIsp:
    def __init__(
        self,
        data_dir: Path,
        emit: Emit,
        run: Runner = run_command,
        sys_root: Path = Path("/sys/class/net"),
        ip_of: Callable[[str], str | None] = ipv4_of,
        probe: Callable[..., list[str] | None] = other_servers,
        server_factory: Callable[..., Any] = OneLeaseServer,
        retry_s: float = RETRY_S,
        no_request_s: float = NO_REQUEST_S,
        auto_allowed: bool = True,
        settle_s: float = AUTO_SETTLE_S,
        clock: Callable[[], float] = time.monotonic,
        arp: Callable[[str, str], ProbeResult] = lambda ip, nic: arp_probe(ip, nic=nic),
        backoff: Callable[[], float] = lambda: random.uniform(0.0, AUTO_SETTLE_S),
        nm: bool | None = None,
        gateway: Callable[[str], str | None] = gateway_of,
        alive: Callable[[str, str, str], bool | None] = arp_alive,
    ) -> None:
        self.nm = shutil.which("nmcli") is not None if nm is None else nm
        self.gateway, self.alive = gateway, alive
        self.data_dir, self.emit, self.run = data_dir, emit, run
        self.undo_wait_s = 1.0
        self.sys_root, self.ip_of, self.probe = sys_root, ip_of, probe
        self.server_factory, self.retry_s, self.no_request_s = server_factory, retry_s, no_request_s
        self.state, self.why = "off", ""
        self.nic: str | None = None
        self._server: Any = None
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._told: set[str] = set()
        self._lost_at: list[float] = []  # GAP-98: when another program took our address off
        self._fighting = False  # it keeps doing so: stop adding until set() or a restart
        self.auto_allowed, self.settle_s, self.clock = auto_allowed, settle_s, clock
        self.arp, self.backoff = arp, backoff
        self._free_since: dict[str, float] = {}  # auto: wired NIC -> up without address since
        self._lan: set[str] = set()  # auto: a DHCP server answered there (until the link drops)
        self._moved_told: str | None = None  # the NIC whose old-lease case was told

    # ---------------------------------------------------------------- config

    @property
    def config(self) -> dict[str, Any]:
        try:
            doc = json.loads((self.data_dir / CONFIG).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"on": None, "nic": None}
        w = (doc.get("wan_isp") if isinstance(doc, dict) else None) or {}
        on = w.get("on")  # None: auto (nothing chosen)
        return {"on": on if isinstance(on, bool) else None, "nic": w.get("nic")}

    def _save_config(self, on: bool | None, nic: str | None) -> None:
        path = self.data_dir / CONFIG
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {"schema_version": 1}
        doc["wan_isp"] = {"on": on, "nic": nic}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        tmp.replace(path)

    def _lease_doc(self) -> dict[str, Any]:
        try:
            doc = json.loads((self.data_dir / LEASE_FILE).read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write_lease_doc(self, **fields: Any) -> None:
        doc = {"schema_version": 1, **self._lease_doc(), **fields}
        path = self.data_dir / LEASE_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        tmp.replace(path)

    # ---------------------------------------------------------------- life

    def start(self) -> None:
        """At node start: bring the role up when it is on (keeps retrying in the back)."""
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="wan-isp", daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Node shutdown: stop answering; the added address stays (the role is still on,
        a restarted node goes on with the same address and lease)."""
        self._halt.set()
        if self._thread is not None:
            self._thread.join(3)
        with self._lock:
            self._stop_server()

    def set(self, on: bool | None, nic: str | None = None) -> dict[str, Any]:
        """02 4 ``POST /v1/wan``: turn the role on or off and keep the choice; ``None``
        goes back to automatic."""
        with self._lock:
            if on is None:
                self._save_config(None, nic)
                self._told.clear()
                self._lan.clear()
                return self.status()
            if not on:
                self._stop_server()
                # turned off, its lease too (receiverPC 2026-11-05: the T460's off kept the
                # Controller PC's 10.0.0.100 for when the role came on again); a restart
                # (close) keeps it, so the router WAN goes on with the same address
                self._write_lease_doc(lease=None)
                if self._undo_address():
                    self.state, self.why, self.nic = "off", "", None
            self._save_config(on, nic)
            self._told.clear()
            self._fighting, self._lost_at = False, []  # the user decided: try again
            if on:
                self._bring_up()
            return self.status()

    def status(self) -> dict[str, Any]:
        """Without the lock: a step may hold it for seconds (DHCP and ARP checks), and the
        Controller (2 s) and the status window (every second) must not wait for that."""
        server, state, nic, why = self._server, self.state, self.nic, self.why
        srv = server.status() if server is not None else {}
        mode = self.config["on"]
        auto = mode is None and self.auto_allowed
        return {"on": mode is True or (auto and state == "ready"),
                "mode": "on" if mode else "auto" if auto else "off",
                "state": state, "nic": nic,
                "server_ip": SERVER.split("/")[0] if state == "ready" else None,
                "lease": srv.get("lease"), "why": why or None}  # fmt: skip

    # ---------------------------------------------------------------- steps

    def _loop(self) -> None:
        while not self._halt.is_set():
            with self._lock:
                mode = self.config["on"]
                if self.state == "ready":
                    self._watch_link(auto=mode is None)
                if self.state == "ready":
                    self._watch_address()
                if self._fighting:
                    pass  # SND-023 said why; adding again would only fight the other program
                elif mode is True and self.state != "ready":
                    self._bring_up()
                elif mode is None and self.auto_allowed and self.state != "ready":
                    self._auto_step()
                self._check_requests()
            self._halt.wait(self.retry_s)

    def _auto_step(self) -> None:
        """Automatic: the wired NIC that stayed up without an address for ``settle_s``
        and where no DHCP server answers is the router WAN one; bring the role up there.
        Anything else (router LAN, no cable, a Controller) is left alone, silently."""
        if not sys.platform.startswith("linux"):
            return
        now = self.clock()
        free = self._candidates()
        self._free_since = {n: self._free_since.get(n, now) for n in free}
        self._lan &= set(free)  # the link dropped or got an address: ask again next time
        # our own address from before a restart: no need to wait (the router keeps its lease)
        ready = [n for n in free if self._ours(n) or now - self._free_since[n] >= self.settle_s]
        moved = None if free else self._moved_nic()  # the cable moved, the old lease stayed
        if moved is not None:
            nic = moved
        elif len(free) != 1 or len(ready) != 1 or ready[0] in self._lan:
            if len(ready) > 1:
                why = f"{len(ready)} network cards could be the WAN one: {', '.join(ready)}"
                self._set("waiting_nic", why)
                self._ask_nic(ready, why)
            return
        else:
            nic = ready[0]
        found = self.probe(nic, self._mac(nic))
        if found:  # a router LAN (or any network with its own DHCP): not ours
            self._lan.add(nic)
            if self._ours(nic) and self._undo_address():  # we were here before: moved to a LAN
                self._set("off", "")
                self.nic = None
                self.emit("ZL-INFO", "info", f"ISP role off: a DHCP server answers on {nic} now",
                          {"nic": nic})  # fmt: skip
            return
        # RFC 5227: is 10.0.0.1 free there? Another PC holding it, or asking for it at this
        # very moment (two PCs on one link without DHCP), means wait a random while and
        # look again; the address goes on right after this check (no gap to race in).
        server_ip = SERVER.split("/")[0]
        arp = self.arp(server_ip, nic)
        if not arp.checked:
            self._set("off", f"cannot check that {server_ip} is free on {nic}: {arp.why}")
            return
        if arp.conflict_macs:
            self._free_since[nic] = now + self.backoff()  # settle again, spread out
            if self._ours(nic):
                self._undo_address()  # someone else serves this link now
            self.emit("ZL-INFO", "info", f"ISP role waits: {server_ip} is used on {nic} by "
                      f"{', '.join(arp.conflict_macs)}", {"nic": nic})  # fmt: skip
            return
        self.emit("ZL-INFO", "info", f"ISP role starts by itself on {nic}: it stays without an "
                  "address and no DHCP server answers (router WAN port)", {"nic": nic})  # fmt: skip
        self._bring_up(nic, probed=True)

    def _watch_link(self, auto: bool) -> None:
        """While ready: the cable came out (or moved). Stop answering at once; when the
        link is back, ask again whether another DHCP server is there first."""
        nic = self.nic
        if nic is None or _read(self.sys_root / nic / "operstate") == "up":
            return
        self._stop_server()
        self._free_since.pop(nic, None)
        self._told.clear()
        self._set("waiting_nic", f"{nic} lost its link (cable out or moved)")
        self.emit("ZL-INFO", "info", f"ISP role paused: {nic} lost its link", {"nic": nic})
        if not auto:
            self._tell("ZL-SND-022", "warn", "Connect the sender PC cable to the router WAN port")

    def _bring_up(self, nic: str | None = None, probed: bool = False) -> None:
        if not sys.platform.startswith("linux"):
            self._set("error", "the ISP role works on Linux only for now")
            return
        nic = nic or self.config["nic"] or self._pick()
        if nic is None:
            return  # _pick said why (waiting_nic)
        if _read(self.sys_root / nic / "operstate") != "up":
            self._set("waiting_nic", f"{nic} has no link (cable in the router WAN port?)")
            self._tell("ZL-SND-022", "warn", "Connect the sender PC cable to the router WAN port")
            return
        mac = _read(self.sys_root / nic / "address")
        if not probed:  # every time, our own address too: the cable may be on a LAN now
            found = self.probe(nic, self._mac(nic))
            if found:
                self._set("conflict", f"another DHCP server answers on {nic}: {', '.join(found)}")
                self._tell("ZL-SND-012", "error", f"Another DHCP server on {nic}: {found}")
                return
        if not self._has_address(nic) and not self._add_address(nic):
            return  # the record is not enough: the address itself must be there (GAP-98)
        net = ipaddress.ip_interface(SERVER)
        try:
            self._server = self.server_factory(
                nic, str(net.ip), net.network.prefixlen, LEASE_IP, self.data_dir / LEASE_FILE,
                self._server_event, ignore_macs=frozenset({mac} if mac else ()),
                alive=self.alive,  # GAP-112: the address follows the cable to another device
            )  # fmt: skip
            self._server.start()
        except OSError as e:
            self._server = None
            self._set("error", f"cannot answer DHCP on {nic}: {e}")
            return
        self.nic = nic
        self._started_at = time.monotonic()
        self._set("ready", "")
        self.emit("ZL-INFO", "info", f"ISP role ready on {nic} ({SERVER})",
                  {"nic": nic, "clears": ["ZL-SND-022", "ZL-DSC-006", "ZL-SND-012"]})  # fmt: skip

    def _candidates(self) -> list[str]:
        """Wired NICs that are up and have no IPv4 address (or only our own)."""
        out = []
        for d in sorted(self.sys_root.iterdir()) if self.sys_root.is_dir() else []:
            wired = (d / "device").exists() and not (d / "wireless").exists()
            up = _read(d / "operstate") == "up"
            ip = self.ip_of(d.name)
            free = ip is None or ip.startswith("169.254.")  # link-local only = no DHCP there
            if wired and up and (free or self._has_address(d.name)):
                out.append(d.name)
        return out

    def _mac(self, nic: str) -> bytes:
        return bytes.fromhex((_read(self.sys_root / nic / "address") or "00" * 6).replace(":", ""))

    def _pick(self) -> str | None:
        """The one wired NIC that is up and has no IPv4 address (or our own address)."""
        candidates = self._candidates()
        if len(candidates) == 1:
            return candidates[0]
        if not candidates and (moved := self._moved_nic()):
            return moved
        if candidates:
            why = f"{len(candidates)} network cards could be the WAN one: {', '.join(candidates)}"
        else:
            why = "no wired network card is up without an address (cable in the router WAN port?)"
        self._set("waiting_nic", why)
        if candidates:
            self._ask_nic(candidates, why)
        else:  # first install: the cable is still on the router LAN (07 3.2 B)
            self._tell("ZL-SND-022", "warn", "Move the sender PC cable to the router WAN port")
        return None

    def _moved_nic(self) -> str | None:
        """GAP-88: the only wired NIC still holds its old router-LAN lease after the
        cable moved to the WAN port — its gateway no longer answers ARP. Only then (a
        live gateway means it is really on a LAN: never take that)."""
        wired = []
        for d in sorted(self.sys_root.iterdir()) if self.sys_root.is_dir() else []:
            if (d / "device").exists() and not (d / "wireless").exists():
                wired.append(d.name)
        if len(wired) != 1 or _read(self.sys_root / wired[0] / "operstate") != "up":
            return None
        nic = wired[0]
        ip, gw = self.ip_of(nic), self.gateway(nic)
        if not ip or not gw or self.alive(nic, ip, gw) is not False:
            return None  # no lease, cannot ask, or the LAN is really there
        if self._moved_told != nic:  # the check repeats every few seconds: tell once
            self._moved_told = nic
            self.emit(
                "ZL-INFO",
                "info",
                f"{nic} keeps {ip} from the router LAN, but its "
                f"gateway {gw} is gone: the cable moved to the WAN port",
                {"nic": nic},
            )
        return nic

    def _ours(self, nic: str) -> bool:
        """We added our address on this NIC before (the record; it may be gone since)."""
        doc = self._lease_doc()
        return doc.get("nic") == nic and doc.get("added_ip") == SERVER

    def _has_address(self, nic: str) -> bool:
        """The NIC really has our address now (07 3.2 B, GAP-98)."""
        return self.ip_of(nic) == SERVER.split("/")[0]

    def _watch_address(self) -> None:
        """While ready: another program may take our address off (NetworkManager's own
        profile retrying DHCP, GAP-98). Put it back and warn; when it keeps happening,
        stop and say what to change instead of fighting it (09 SND-023)."""
        nic = self.nic
        if nic is None or self._has_address(nic):
            return
        now = self.clock()
        self._lost_at = [t for t in self._lost_at if now - t < LOST_WINDOW_S] + [now]
        if len(self._lost_at) > LOST_MAX:
            self._stop_server()
            self._fighting = True
            why = f"another program keeps taking {SERVER} off {nic}"
            self._set("error", why)
            fix = (f"Stop the NIC's own NetworkManager profile from connecting by itself: nmcli "
                   f"connection modify '<profile on {nic}>' connection.autoconnect no")  # fmt: skip
            self.emit("ZL-SND-023", "error", f"Another program keeps taking {SERVER} off {nic}",
                      {"nic": nic, "times": len(self._lost_at), "fix": fix})  # fmt: skip
            return
        self.emit(
            "ZL-SND-023",
            "warn",
            f"{SERVER} was taken off {nic} by another program: added again",
            {"nic": nic},
        )
        if not self._add_address(nic):  # _add_address set error and why
            self._stop_server()

    def _add_address(self, nic: str) -> bool:
        if self.nm:  # NetworkManager owns the NIC: our profile replaces its old lease
            argv = ["nmcli", "connection", "add", "type", "ethernet", "ifname", nic,
                    "con-name", NM_PROFILE, "ipv4.method", "manual", "ipv4.addresses", SERVER,
                    "ipv4.never-default", "yes", "ipv6.method", "disabled",
                    "connection.autoconnect", "yes",
                    "connection.autoconnect-priority", "100"]  # fmt: skip
            self.run(["nmcli", "connection", "delete", NM_PROFILE], 10.0)  # a leftover one
            rc, out = self.run(argv, 10.0)
            if rc == 0:
                rc, out = self.run(["nmcli", "connection", "up", NM_PROFILE, "ifname", nic], 30.0)
            if rc != 0:
                self.run(["nmcli", "connection", "delete", NM_PROFILE], 10.0)
                self._set("error", f"cannot set {SERVER} on {nic}: {out[-200:]}")
                return False
            self._write_lease_doc(nic=nic, added_ip=SERVER, nm_profile=NM_PROFILE)
            return True
        rc, out = self.run(["ip", "addr", "add", SERVER, "dev", nic], 10.0)
        if rc != 0 and "File exists" not in out:
            self._set("error", f"cannot set {SERVER} on {nic}: {out[-200:]}")
            return False
        self._write_lease_doc(nic=nic, added_ip=SERVER, nm_profile=None)
        return True

    def _undo_address(self) -> bool:
        """Take the added address off again and confirm it. 09 SND-014: one more try,
        then ``error`` with the command to do it by hand. True when it is gone."""
        doc = self._lease_doc()
        nic, added = doc.get("nic"), doc.get("added_ip")
        if not nic or not added:
            return True
        out = ""
        profile = doc.get("nm_profile")
        for attempt in range(UNDO_TRIES):
            if profile:  # NetworkManager brings the NIC's own profile back by itself
                rc, out = self.run(["nmcli", "connection", "delete", str(profile)], 10.0)
                gone = rc == 0 or "not found" in out.lower() or "unknown connection" in out.lower()
            else:
                rc, out = self.run(["ip", "addr", "del", str(added), "dev", str(nic)], 10.0)
                gone = rc == 0 or "Cannot assign" in out  # gone now, or already gone
            if gone:
                self._write_lease_doc(nic=None, added_ip=None, nm_profile=None)
                return True
            if attempt + 1 < UNDO_TRIES:
                time.sleep(self.undo_wait_s)
        why = f"could not take {added} off {nic}: {out[-200:]}"
        self._set("error", why)
        fix = (f"nmcli connection delete {profile}" if profile
               else f"sudo ip addr del {added} dev {nic}")  # fmt: skip
        self.emit("ZL-SND-014", "error", f"Sender PC network setting not restored: {why}",
                  {"nic": nic, "fix": fix})  # fmt: skip
        return False

    def _stop_server(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None

    def _check_requests(self) -> None:
        if self.state != "ready" or self._server is None:
            return
        st = self._server.status()
        silent = time.monotonic() - self._started_at >= self.no_request_s
        if silent and not st.get("lease") and not st.get("requests"):
            self._tell("ZL-SND-011", "warn",
                       "The router asked for no WAN address: is its WAN set to DHCP?")  # fmt: skip

    def _server_event(self, code: str, level: str, data: dict[str, Any]) -> None:
        self.emit(code, level, str(data.get("text", "")), data)

    def _set(self, state: str, why: str) -> None:
        self.state, self.why = state, why

    def _tell(self, code: str, level: str, text: str, **data: Any) -> None:
        if code not in self._told:  # once per state, not every retry
            self._told.add(code)
            self.emit(code, level, text, {"nic": self.nic, **data})

    def _ask_nic(self, nics: list[str], why: str) -> None:
        """DSC-006 with the cards to choose from (20 W-1): the Action Required shows one
        button per card, pressing one is ``devices wan --nic`` (02 2-3 ``choices``)."""
        choices = []
        for nic in nics:
            speed = _read(self.sys_root / nic / "speed")
            mbps = f"{speed} Mbps" if speed and speed.isdigit() and int(speed) > 0 else "speed ?"
            mac = _read(self.sys_root / nic / "address") or "?"
            choices.append({"id": nic, "label": f"{nic} · {mbps} · {mac}"})
        self._tell("ZL-DSC-006", "warn", f"Which network card goes to the router WAN? {why}",
                   choose="wan_nic", choices=choices)  # fmt: skip


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None
