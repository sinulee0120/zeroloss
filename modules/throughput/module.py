"""Throughput module on a node (docs/modules/throughput.md, docs/12-architecture.md 2).

Routes under /v1/modules/throughput/: the WAN-side PC runs the iperf3 server, a LAN-side
PC runs its list of cases (``client.py``). The heartbeat carries where the list is; one
record per case goes to the node's store like the IPTV 5-minute summaries. The node is
RUNNING from the start of a list until the Controller stops it — a finished list stays
"done" (not IDLE) so the Controller reads the end, not a PC that left the test.
"""

from __future__ import annotations

import json
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.blocks.link_monitor import nic_of
from core.blocks.nic_bytes import bytes_of
from core.blocks.nic_speed import Mode, NicSpeed, caps
from core.blocks.process_runner import ProcessRunner, ProcState, StartError, StopError
from core.blocks.win_nic_speed import WinNicSpeed
from core.events.bus import Event
from core.lifecycle.state_flow import State
from core.protocol.envelope import Request, ZlError
from modules.throughput.client import ClientConfig, ClientJob
from modules.throughput.iperf import PORT, server_argv

PREFIX = "/v1/modules/throughput"


def _iperf3() -> str:
    """The PC's iperf3; on Windows the one that comes with the program (next to it)."""
    found = shutil.which("iperf3")
    if found:
        return found
    beside = Path(sys.executable).with_name("iperf3.exe")
    return str(beside) if beside.is_file() else "iperf3"


FIX_CHOICES = [
    {"id": "retry", "label": "Fixed, check again"},
    {"id": "continue", "label": "Continue anyway"},
    {"id": "stop", "label": "Stop the test"},
]


class ThroughputModule:
    def __init__(self) -> None:
        self.node: Any = None
        self.server: ProcessRunner | None = None
        self.clients: dict[str, ClientJob] = {}  # target id -> its list (one runs at a time)
        # speed changes, with the way back on disk: ethtool (Linux) or the driver's
        # Speed & Duplex through an administrator helper (Windows, the Controller PC)
        self.cards: NicSpeed | WinNicSpeed | None = None
        self.wan_nic: str | None = None  # the WAN-side card (server)
        self.link: dict[str, Any] = {}  # the WAN-side card's last step (heartbeat)
        self._wan_modes: list[Mode] = []
        self._wan_next = 0
        self._pending: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._runs = 0

    def attach(self, node: Any) -> None:
        self.node = node
        node.api.command(f"{PREFIX}/server/start", self.server_start)
        node.api.command(f"{PREFIX}/server/stop", self.server_stop)
        node.api.command(f"{PREFIX}/client/start", self.client_start)
        node.api.command(f"{PREFIX}/client/stop", self.client_stop)
        node.api.command(f"{PREFIX}/client/go", self.client_go)
        node.api.command(f"{PREFIX}/client/resume", self.client_resume)
        node.api.command(f"{PREFIX}/server/link", self.server_link)
        node.api.get(f"{PREFIX}/card", self.card)  # read only: what ethtool says now
        node.module_data["throughput"] = self.status
        node.summary_sources.append(self._take_summaries)
        custody = Path(node.data_dir) / "throughput-cards.json"
        cards = WinNicSpeed(custody) if sys.platform == "win32" else NicSpeed(custody)
        self.cards = cards
        # the Wi-Fi network each card was on before its bands (a restart or Reset All
        # puts it back; 2026-10-22 user: "reset all누르면 모든 장비들 설정 원래대로")
        self.wifi_kept = Path(node.data_dir) / "throughput-wifi.json"
        # a restart mid-test: the cards it left changed go back
        threading.Thread(target=self._restore_at_start, name="throughput-restore",
                         daemon=True).start()  # fmt: skip

    def safe_stop(self, reason: str) -> None:
        with self._lock:
            self._stop_client()
            self._stop_server()

    def _restore_at_start(self) -> None:
        assert self.cards is not None
        self._told(self.cards.restore_all())
        self._told(self._wifi_restore())

    def restore_settings(self) -> list[dict[str, Any]]:
        """Reset All (node /v1/reset): every card setting a test changed goes back as it
        was -- wired speed and duplex, the Wi-Fi network -- and says so."""
        out = []
        results = dict(self.cards.restore_all()) if self.cards is not None else {}
        results.update(self._wifi_restore())
        for nic, back in results.items():
            ok = bool(back.get("ok"))
            why = "put back" if ok else str(back.get("why") or "not back")
            out.append({"what": nic, "ok": ok, "detail": why})
        if isinstance(self.cards, WinNicSpeed):
            self.cards.close()
        return out

    def _wifi_kept_now(self) -> dict[str, str]:
        try:
            got = json.loads(self.wifi_kept.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): str(v) for k, v in got.items()} if isinstance(got, dict) else {}

    def _wifi_keep(self, kept: dict[str, str]) -> None:
        try:
            if kept:
                self.wifi_kept.write_text(json.dumps(kept), encoding="utf-8")
            else:
                self.wifi_kept.unlink(missing_ok=True)
        except OSError:
            pass

    def _wifi_restore(self) -> dict[str, dict[str, Any]]:
        """Each Wi-Fi card a test moved to a band network, back on the one it was on."""
        kept = self._wifi_kept_now()
        card = getattr(self.node, "wifi", None)
        if not kept or card is None:
            return {}
        out: dict[str, dict[str, Any]] = {}
        for nic, before in list(kept.items()):
            try:
                if card.connection_of(nic) == before:
                    back: dict[str, Any] = {"back": True}
                else:
                    back = dict(card.back(nic, before))
            except (ZlError, OSError) as e:
                back = {"back": False, "why": str(e)}
            out[nic] = {"ok": bool(back.get("back")), "why": back.get("why")}
            if back.get("back"):
                del kept[nic]
        self._wifi_keep(kept)
        return out

    def clean_check(self) -> list[str]:
        left = []
        if self.server is not None and self.server.poll() is not ProcState.IDLE:
            left.append("iperf3 server still running")
        if any(c.state in ("starting", "running") for c in self.clients.values()):
            left.append("throughput list still running")
        return left

    # ---------------------------------------------------------------- routes

    def server_start(self, req: Request) -> dict[str, Any]:
        port = int(req.params.get("port", PORT))
        bind = req.params.get("bind")
        self.wan_nic = nic_of(str(bind)) if isinstance(bind, str) and bind else None
        self.link, self._wan_modes, self._wan_next = {}, [], 0
        with self._lock:
            self._stop_server()
            argv = server_argv(_iperf3(), port)
            runner = ProcessRunner(f"iperf3_server_{port}", argv, self.node.procs_dir,
                                   self._log("server"))  # fmt: skip
            try:
                t0 = time.monotonic()  # alive for a moment = listening (a probe would
                runner.start(ready=lambda: time.monotonic() - t0 > 0.5)  # count as a test)
            except StartError as e:
                raise ZlError(
                    "INS-029",
                    "The iperf3 server did not start",
                    str(e),
                    "Install iperf3 on this PC (sudo apt install iperf3).",
                ) from e
            self.server = runner
            self._busy()
        return {"port": port, "pid": runner.pid}

    def server_stop(self, req: Request) -> dict[str, Any]:
        with self._lock:
            stopped = self._stop_server()
            self._settle()
        return {"stopped": stopped}

    def client_start(self, req: Request) -> dict[str, Any]:
        p = req.params
        cases = p.get("cases")
        if not isinstance(cases, list) or not cases:
            raise ZlError("DSC-012", "Request rejected", detail="params.cases must be a list")
        # a picked Wi-Fi card may be on no network at the start: its name and bands do
        wifi_only = bool(p.get("nic")) and bool(p.get("bands"))
        for key in ("server",) if wifi_only else ("server", "bind"):
            if not isinstance(p.get(key), str) or not p.get(key):
                raise ZlError("DSC-012", "Request rejected", detail=f"params.{key} must be str")
        cfg = ClientConfig(
            run_id=req.run_id or "R-unknown", device=str(p.get("device", "pc")),
            server=str(p["server"]), bind=str(p.get("bind") or ""),
            cases=[dict(c) for c in cases],
            seconds=int(p.get("seconds", 30)), wait_s=float(p.get("wait_s", 10)),
            protocol=str(p.get("protocol", "tcp")), streams=int(p.get("streams", 1)),
            udp_pct=float(p.get("udp_pct", 95)), port=int(p.get("port", PORT)),
            directions=tuple(x for x in ("send", "receive")
                             if x in (p.get("directions") or ("send", "receive"))),
            binary=_iperf3(), queued=bool(p.get("queued", False)),
            judged=dict(p["judged"]) if isinstance(p.get("judged"), dict) else {},
            kind=str(p.get("kind", "wired")),
            unsupported=[str(x) for x in p.get("unsupported") or []],
            bands=[dict(b) for b in p.get("bands") or [] if isinstance(b, dict)],
            nic=str(p.get("nic") or ""),
        )  # fmt: skip
        target = str(p.get("target") or cfg.bind)
        with self._lock:
            if target in self.clients:  # asked again on the Controller's next round
                return {"cases": len(cfg.cases), "again": True}
            self.node.reach_sender(cfg.server, cfg.bind)  # GAP-158: iperf3 -c over the router
            job = ClientJob(cfg, self._run_iperf, self._read_link, card=self._card_control(),
                            wifi=self._wifi_control(cfg.server), read_bytes=bytes_of,
                            tell=self._teller(cfg.device, target))  # fmt: skip
            job._offset = float(p.get("elapsed_s", 0.0))
            self.clients[target] = job
            job.start()
            self._busy()
        return {"cases": len(cfg.cases)}

    def client_resume(self, req: Request) -> dict[str, Any]:
        """The Controller's answer to a list waiting for the WAN side (client.WAN_MODES)."""
        p = req.params
        job = self.clients.get(str(p.get("target")))
        answer = p.get("answer")
        if job is None or not isinstance(answer, dict):
            raise ZlError("DSC-012", "Request rejected", detail="no such list or no answer")
        return {"resumed": job.resume(int(p.get("seq", -1)), answer)}

    def server_link(self, req: Request) -> dict[str, Any]:
        """The WAN-side card, one step a call (``what``: next / restore), done in the
        background; the result shows in the heartbeat (``server.link``) with ``seq``."""
        p = req.params
        seq, what, target = int(p.get("seq", -1)), str(p.get("what")), str(p.get("target"))
        with self._lock:
            if self.link.get("seq") == seq and self.link.get("target") == target:
                return {"again": True}
            self.link = {"seq": seq, "target": target, "busy": True}
        threading.Thread(target=self._wan_step, args=(seq, target, what),
                         name="throughput-wan-card", daemon=True).start()  # fmt: skip
        return {"started": True}

    def card(self, q: Any) -> dict[str, Any]:
        """A wired card as ethtool reads it now, with what the other end of the cable
        offers (2026-10-17: a path stuck at 10 Mbps -- which end is it?). Changes nothing."""
        nic = str(q.params.get("nic") or self.wan_nic or "")
        if nic not in {x.nic for x in self.node.links}:
            raise ZlError("DSC-012", "Request rejected", detail=f"no card {nic!r} on this PC")
        found = caps(nic)
        if found is None:
            raise ZlError("DSC-012", "Request rejected", detail=f"ethtool cannot read {nic}")
        return found.to_dict()

    def _wan_step(self, seq: int, target: str, what: str) -> None:
        out: dict[str, Any]
        nic, cards = self.wan_nic, self.cards
        if not nic or cards is None:
            out = {"done": True, "ok": True, "why": "the WAN-side card is not known"}
        elif what == "restore":
            out = cards.restore(nic)
            self._wan_modes, self._wan_next = [], 0  # the next LAN card goes through all again
        else:
            if not self._wan_modes and self._wan_next == 0:
                found = caps(nic)
                self._wan_modes = list(found.supported) if found else []
            if self._wan_next >= len(self._wan_modes):
                out = {"done": True}
            else:
                m = self._wan_modes[self._wan_next]
                self._wan_next += 1
                out = {"mode": m.label, "nic": nic, **cards.set_mode(nic, m)}
                if not out.get("link") and not out.get("skipped"):
                    # 2026-10-17: this card is the Controller's way to this PC; a mode the
                    # router port does not take left it with no link and the Controller
                    # could never say "next" or "restore" again. Back at once.
                    back = cards.restore(nic)
                    out["restored"] = bool(back.get("ok"))
                    if not back.get("ok"):
                        out["why"] = f"SAFETY: {nic} could not be put back ({back.get('why')})"
        with self._lock:
            self.link = {**out, "seq": seq, "target": target, "busy": False}

    def _wifi_control(self, server: str) -> Any:
        """This PC's Wi-Fi card for the bands the person picked (client.WifiControl). After
        each join the way to the WAN-side PC is set again on that card (a reconnect drops
        the route the test added)."""
        node = self.node
        wifi = getattr(node, "wifi", None)
        if wifi is None or not getattr(wifi, "supported", False):
            return None
        card: Any = wifi  # known here (the nested class does not see the narrowing)

        module = self

        class Wifi:
            def connection_of(self, nic: str) -> str | None:
                before: str | None = card.connection_of(nic)
                kept = module._wifi_kept_now()
                if before and nic not in kept:  # the network to go back to, kept on disk
                    module._wifi_keep({**kept, nic: before})
                return before

            def join_band(self, nic: str, ssid: str, bssid: str, security: str = "Open",
                          password: str = "") -> dict[str, Any]:  # fmt: skip
                try:
                    got = dict(card.join(ssid, security, password, nic, bssid=bssid or None))
                except ZlError as e:
                    return {"why": f"{e.what}: {e.why}".strip(": ")}
                reach = node.wan_reach.ensure(server, nic)
                return {**got, "route": reach.get("done")}

            def back(self, nic: str, connection: str | None) -> dict[str, Any]:
                got = dict(card.back(nic, connection))
                if got.get("back"):
                    node.wan_reach.ensure(server, nic)
                    kept = module._wifi_kept_now()
                    kept.pop(nic, None)
                    module._wifi_keep(kept)
                return got

        return Wifi()

    def _card_control(self) -> Any:
        """This PC's wired card for the list (client.CardControl)."""
        cards = self.cards
        if cards is None:
            return None

        if isinstance(cards, WinNicSpeed):
            return cards  # the same supported / set_mode / restore

        class Card:
            def supported(self, nic: str) -> list[Mode]:
                found = caps(nic)
                return list(found.supported) if found else []

            def set_mode(self, nic: str, mode: Mode) -> dict[str, Any]:
                assert cards is not None
                return cards.set_mode(nic, mode)

            def restore(self, nic: str) -> dict[str, Any]:
                assert cards is not None
                return cards.restore(nic)

        return Card()

    def client_go(self, req: Request) -> dict[str, Any]:
        """The PC before this one is done (throughput.md 1.3)."""
        with self._lock:
            if not self.clients:
                raise ZlError("DSC-012", "Request rejected", detail="no throughput list here")
            began = any(c.go(float(req.params.get("elapsed_s", 0.0)))
                        for c in self.clients.values())  # fmt: skip
        return {"began": began}

    def client_stop(self, req: Request) -> dict[str, Any]:
        with self._lock:
            stopped = self._stop_client()
            self._settle()
        return {"stopped": stopped}

    def status(self) -> dict[str, Any]:
        """Heartbeat ``module_data.throughput`` (02 5)."""
        server: dict[str, Any] | None = None
        if self.server is not None:
            server = {"state": self.server.poll().value, "pid": self.server.pid}
        if server is not None and self.link:
            server["link"] = dict(self.link)
        clients = {k: {**c.status(), "target": k} for k, c in self.clients.items()}
        return {"server": server, "clients": clients}

    # ---------------------------------------------------------------- helpers

    def _take_summaries(self) -> list[dict[str, Any]]:
        for c in list(self.clients.values()):
            self._pending.extend(c.take_summaries())
        out, self._pending = self._pending, []

        return out

    def _log(self, what: str) -> Path:
        logs = Path(self.node.data_dir) / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        return logs / f"iperf3-{what}.log"

    def _run_iperf(self, argv: list[str], limit_s: float) -> str:
        """One iperf3 client run: started and ended through process_runner (12 4-3)."""
        self._runs += 1
        out = Path(self.node.data_dir) / "logs" / f"iperf3-client-{self._runs % 4}.json"
        out.unlink(missing_ok=True)  # one run per file: a reused file was read as garbage
        runner = ProcessRunner("iperf3_client", argv, self.node.procs_dir, out)
        runner.start()
        end = time.monotonic() + limit_s
        while runner.poll() is ProcState.RUNNING and time.monotonic() < end:
            time.sleep(0.2)
        try:
            runner.stop()
        except StopError as e:
            raise OSError(str(e)) from e
        text: str = out.read_text(encoding="utf-8", errors="replace")
        return text

    def _read_link(self, bind: str) -> tuple[str, int | None, str | None]:
        for x in self.node.links:
            if x.ip == bind:
                rate = x.link_mbps
                if rate is None and x.wifi is not None:  # Wi-Fi: the rate the card reports
                    rate = int(x.wifi.tx_mbps) if x.wifi.tx_mbps else None
                return x.nic, rate, x.duplex
        return "", None, None

    def _teller(self, device: str, target: str) -> Any:
        """A card not put back: an Action Required with three buttons; the choice comes
        back through client/resume (2026-10-22 user: "사용자가 직접 고쳐서 어떻게든")."""

        def tell(text: str, seq: int) -> None:
            self.node.bus.publish(Event(
                "ZL-NET-024", "error", text, device=device,
                data={"choose": "throughput_fix", "choices": FIX_CHOICES,
                      "ask": {"path": f"{PREFIX}/client/resume", "target": target, "seq": seq}},
            ))  # fmt: skip

        return tell

    def _told(self, results: dict[str, dict[str, Any]]) -> None:
        """A card the test changed and could not put back is a failure, told at once
        (NET-024; 09 rule 5; 2026-10-22 user: "설정 원복이 안되면 실패야")."""
        for nic, back in results.items():
            if not back.get("ok"):
                why = str(back.get("why") or "not back")
                self.node.bus.publish(Event(
                    "ZL-NET-024", "error", f"{nic} could not be put back as it was: {why}",
                    data={"nic": nic, "why": why},
                ))  # fmt: skip

    def _stop_server(self) -> bool:
        if self.wan_nic and self.cards is not None:  # a test stopped mid-way: card back
            self._told({self.wan_nic: self.cards.restore(self.wan_nic)})
        if self.server is None:
            return False

        if self.server.poll() in (ProcState.RUNNING, ProcState.FAULT):
            self.server.stop()
        self.server = None
        return True

    def _stop_client(self) -> bool:
        if not self.clients:
            return False
        for c in self.clients.values():
            self._pending.extend(c.take_summaries())
            c.stop()
            self._pending.extend(c.take_summaries())
        self.clients = {}
        if self.cards is not None:  # a list stopped mid-phase: its card back as it was
            self._told(self.cards.restore_all())
            if isinstance(self.cards, WinNicSpeed):
                self.cards.close()  # the administrator helper ends with the list
        return True

    def _busy(self) -> None:
        flow = self.node.flow
        if flow.state is State.IDLE:
            flow.move(State.STARTING, reason="throughput starting")
        if flow.state is State.STARTING:
            flow.move(State.RUNNING, reason="throughput running")

    def _settle(self) -> None:
        """Nothing of this module left: the node goes back to IDLE."""
        if self.server is None and not self.clients:
            flow = self.node.flow
            if flow.state in (State.STARTING, State.RUNNING):
                flow.move(State.STOPPING, reason="throughput stopped")
                flow.move(State.IDLE, reason="throughput stopped")


def create() -> ThroughputModule:
    return ThroughputModule()
