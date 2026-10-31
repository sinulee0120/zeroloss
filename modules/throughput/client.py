"""The LAN-side PC's own list of cases (docs/modules/throughput.md 0, 1.3).

Like the reference's measuring agent, the PC walks its whole list by itself: a link
that drops while a speed changes does not stop it, and the Controller only waits for
it to say it is done. Each case: read the link the card actually got (the effective
rate the judgement uses), then measure send and receive, one after the other, with
iperf3 (``iperf.py``). Every case leaves one record (``summary``) whatever happened;
a case that could not be measured says why (``not_run``) and the list goes on.

Wait ``wait_s`` before the first case and after the last (link settles, 1.3).

The PCs go one after another (1.3): every PC of the test is started at once but
``queued`` (its node is in the test, not IDLE), and the Controller says ``go`` to the
next one when the one before is done. ``elapsed_at_go``: the run's clock at that moment,
so the records of every PC sit on one time line.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.blocks.nic_bytes import rate_mbps
from core.blocks.nic_speed import Mode
from modules.throughput.iperf import PORT, IperfResult, client_argv, parse

DIRECTIONS = ("send", "receive")  # send: this PC -> the WAN side; receive: back (-R)

# (argv, seconds) -> the program's output text; raises OSError when it cannot start
RunIperf = Callable[[list[str], float], str]
# the test card's address -> (nic, link Mbps or None, duplex or None)
ReadLink = Callable[[str], tuple[str, int | None, str | None]]
# 2026-10-16 (every setting the PCs support, reference RealTest): phases in the case list
OWN_MODES = "own_modes"  # this PC's card, every speed/duplex it supports, then back
WAN_MODES = "wan_modes"  # the WAN-side PC's card, step by step through the Controller
# 2026-10-17 user ("WAN -> WLAN, WAN <- WLAN 을 WLAN설정만 바꿔서"): this PC's Wi-Fi card on
# each band the person picked (an SSID and access point per band), then back
WIFI_BANDS = "wifi_bands"


class CardControl(Protocol):
    """This PC's wired card (``core.blocks.nic_speed`` on the node; a fake in tests)."""

    def supported(self, nic: str) -> list[Mode]: ...

    def set_mode(self, nic: str, mode: Mode) -> dict[str, Any]: ...

    def restore(self, nic: str) -> dict[str, Any]: ...


class WifiControl(Protocol):
    """This PC's Wi-Fi card (``core.blocks.wifi_join`` on the node; a fake in tests)."""

    def connection_of(self, nic: str) -> str | None: ...

    def join_band(self, nic: str, ssid: str, bssid: str, security: str = "Open",
                  password: str = "") -> dict[str, Any]: ...  # fmt: skip

    def back(self, nic: str, connection: str | None) -> dict[str, Any]: ...


@dataclass
class ClientConfig:
    run_id: str
    device: str
    server: str  # the WAN-side PC's address this PC reaches (through the router)
    bind: str  # this PC's test card address
    cases: list[dict[str, Any]]  # {"label", "speed_mbps": None = the link as it is}
    seconds: int = 30
    wait_s: float = 10.0
    protocol: str = "tcp"
    streams: int = 1
    udp_pct: float = 95.0  # UDP target: this share of the effective link rate
    directions: tuple[str, ...] = DIRECTIONS  # S-63: both, or one of them
    port: int = PORT
    binary: str = "iperf3"
    queued: bool = False  # wait for go() (the PC before it is still measuring)
    judged: dict[str, Any] = field(default_factory=dict)  # how the boxes are judged (run.py)
    kind: str = "wired"  # wired / wifi: which card this list measures
    unsupported: list[str] = field(default_factory=list)  # settings this PC cannot change
    bands: list[dict[str, Any]] = field(default_factory=list)  # WIFI_BANDS: band, ssid, bssid
    nic: str = ""  # the card by name (a picked Wi-Fi card may have no address at the start)
    wifi_settings: bool = True  # S-62 Every supported: each band's width caps and modes too
    who: str = ""  # a tablet measured through this PC: its name before every case label


@dataclass
class ClientJob:
    cfg: ClientConfig
    run_iperf: RunIperf
    read_link: ReadLink
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], bool] | None = None  # (s) -> True when asked to stop
    card: CardControl | None = None
    wifi: WifiControl | None = None
    # the test card's (received, sent) bytes: a rate every second while iperf3 runs
    # (2026-10-22 user: "실시간 속도 넣어"); None = no live rate
    read_bytes: Callable[[str], tuple[int, int] | None] | None = None
    live: dict[str, Any] | None = None  # {"direction", "mbps", "s"} while measuring
    # every setting put back after its cases, shown (2026-10-22 user: "설정 원복 확실히 하고
    # 마무리", "설정 원복이 안되면 실패야"): {"card", "ok", "to", "why"}
    put_back: list[dict[str, Any]] = field(default_factory=list)
    # a setting that did not come back: tell the person (text, seq) and wait for a choice
    # (2026-10-22 user: "자동으로 설정한게 제대로 안될 경우 사용자가 고쳐서라도 진행이 되게");
    # None = stop for safety as before
    tell: Callable[[str, int], None] | None = None
    state: str = "starting"  # starting / queued / running / done / failed / stopped
    index: int = -1
    doing: str = ""
    error: str = ""
    _pending: list[dict[str, Any]] = field(default_factory=list)
    _done: list[dict[str, Any]] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None
    _t0: float = 0.0
    _offset: float = 0.0  # the run's clock when this list began
    _go: threading.Event = field(default_factory=threading.Event)
    need: dict[str, Any] | None = None  # asks the Controller for the WAN side (WAN_MODES)
    _answer: dict[str, Any] | None = None
    _answered: threading.Event = field(default_factory=threading.Event)
    _seq: int = 0
    _count: int = 0  # records so far (a phase makes several)
    _bind: str = ""  # the test card's address now ("" = cfg.bind; a Wi-Fi band may change it)
    _command: str = ""  # the last iperf3 command line (for the record)

    def start(self) -> None:
        if self.cfg.queued:
            self.state = "queued"
        else:
            self._go.set()
        self._thread = threading.Thread(target=self._run, name="throughput-client", daemon=True)
        self._thread.start()

    def go(self, elapsed_s: float = 0.0) -> bool:
        """The PC before this one is done: begin. False when it already began."""
        if self._go.is_set():
            return False
        self._offset = elapsed_s
        self._go.set()
        return True

    def resume(self, seq: int, answer: dict[str, Any]) -> bool:
        """The Controller's answer to ``need`` (the WAN side did step ``seq``)."""
        with self._lock:
            if self.need is None or self.need.get("seq") != seq:
                return False
            self.need, self._answer = None, dict(answer)
        self._answered.set()
        return True

    def stop(self, timeout_s: float = 60.0) -> None:
        self._stop.set()
        self._go.set()
        self._answered.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        with self._lock:
            if self.state in ("starting", "queued", "running"):
                self.state = "stopped"

    def take_summaries(self) -> list[dict[str, Any]]:
        with self._lock:
            out, self._pending = self._pending, []
            return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.state, "case": self.index, "cases": len(self.cfg.cases),
                "doing": self.doing, "error": self.error,
                "live": dict(self.live) if self.live else None,
                "put_back": [dict(x) for x in self.put_back],
                "results": [_short(r) for r in self._done],
                "done": self.state == "done", "need": dict(self.need) if self.need else None,
            }  # fmt: skip

    # ---------------------------------------------------------------- the list

    def _wait(self, s: float) -> bool:
        """True when asked to stop meanwhile."""
        if self.sleep is not None:
            return self.sleep(s)
        return self._stop.wait(s)

    def _put_back_or_ask(self, card: str, do_back: Callable[[], dict[str, Any] | None],
                         ok_key: str, fix: str,
                         to: str = "") -> tuple[bool, dict[str, Any]]:  # fmt: skip
        """Put a setting back; when it does not come back the person fixes it by hand and
        chooses: check again, go on anyway (noted), or stop (SAFETY). False = the list stops."""
        while True:
            back = do_back()
            if back is None:  # stopped meanwhile
                return False, {}
            ok = bool(back.get(ok_key))
            self._put_back(card, ok, to or _speed_text(back.get("speed")) or "Auto",
                           back.get("why"))  # fmt: skip
            if ok:
                return True, back
            why = str(back.get("why") or "not back")
            choice = self._ask_person(card, why, fix)
            if choice == "retry":
                continue
            if choice == "continue":
                return True, back
            self._set(state="failed", doing="",
                      error=f"SAFETY: {card} could not be put back ({why})")  # fmt: skip
            return False, back

    def _ask_person(self, card: str, why: str, fix: str) -> str:
        """retry / continue / stop, from the person (Action Required); stop without a way
        to ask or when the test stops meanwhile."""
        if self.tell is None:
            return "stop"
        with self._lock:
            self._seq += 1
            seq = self._seq
            self.need = {"person": card, "seq": seq}
            self._answered.clear()
        self._set(doing=f"waiting for you: {card} not put back - see Action Required")
        self.tell(f"{card} could not be put back as it was: {why}. {fix} Then press "
                  "Fixed, check again.", seq)  # fmt: skip
        while not self._answered.wait(0.5):
            if self._stop.is_set():
                return "stop"
        if self._stop.is_set():
            return "stop"
        return str((self._answer or {}).get("choice") or "stop")

    def _put_back(self, card: str, ok: Any, to: str, why: Any) -> None:
        with self._lock:
            self.put_back.append({"card": card, "ok": bool(ok), "to": to,
                                  "why": None if ok else str(why or "not back")})  # fmt: skip

    def _set(self, **kw: Any) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)

    def _run(self) -> None:
        self._go.wait()
        if self._stop.is_set():
            return
        self._t0 = self.clock()
        try:
            self._set(state="running", doing="waiting for the link to settle")
            if self._wait(self.cfg.wait_s):
                return
            for i, case in enumerate(self.cfg.cases):
                if self._stop.is_set():
                    return
                self._set(index=i)
                if case.get("phase") == OWN_MODES:
                    if not self._own_modes():
                        return
                elif case.get("phase") == WAN_MODES:
                    if not self._wan_modes():
                        return
                elif case.get("phase") == WIFI_BANDS:
                    if not self._wifi_bands():
                        return
                else:
                    self._keep(self._case(case))
            self._set(doing="waiting after the last case")
            if self._wait(self.cfg.wait_s):
                return
            self._set(state="done", doing="")
        except Exception as e:  # the list never dies silently: the screen says why
            self._set(state="failed", error=repr(e), doing="")

    def run_second(self) -> int:
        """The run's clock in whole seconds (every PC's live rate on one time line: the live
        graph, 2026-10-30 user "tput값을 숫자로만 보여주네 … 그래프로")."""
        return int(self._offset + self.clock() - self._t0)

    def _keep(self, record: dict[str, Any]) -> None:
        with self._lock:
            record["index"] = self._count
            self._count += 1
            self._pending.append(record)
            self._done.append(record)

    def _case(
        self,
        case: dict[str, Any],
        baseline: int | None = None,
        setting: dict[str, Any] | None = None,
        why: str | None = None,
    ) -> dict[str, Any]:
        """One case: ``baseline`` is the link speed the judgement uses (the changed
        side's negotiated speed); ``setting`` is what was asked and read back."""
        t_start = self.clock() - self._t0
        bind = self._bind or self.cfg.bind
        nic, mbps, duplex = self.read_link(bind)
        base = baseline if setting is not None else mbps
        record: dict[str, Any] = {
            "run_id": self.cfg.run_id,
            "device": self.cfg.device,
            # both ends of every case (2026-10-17 user: "LAN to LAN이야?" -- it is never)
            "between": {"lan": bind, "wan": self.cfg.server},
            "index": -1,
            "start_elapsed_s": round(self._offset + t_start, 1),
            "duration_s": 0.0,
            "partial": False,
            "case": {
                "label": " · ".join(x for x in (self.cfg.who, str(case.get("label") or "")) if x),
                "requested_mbps": case.get("speed_mbps"),
                "negotiated_mbps": base,
                "duplex": duplex,
                "nic": nic,
                "kind": self.cfg.kind,
                **({"setting": setting} if setting is not None else {}),
            },
            "send": None,
            "receive": None,
            "not_run": why,
            "judged": dict(self.cfg.judged),
        }
        if self._count == 0 and self.cfg.unsupported:  # said once, on the first case
            record["unsupported"] = list(self.cfg.unsupported)
        if why is None and not base:
            record["not_run"] = f"the link of {nic or bind} is down or its speed unknown"
        elif why is None and base:
            for direction in self.cfg.directions:
                if self._stop.is_set():
                    break
                self._set(doing=f"{case.get('label')}: {direction}")
                got = self._measure(direction == "receive", base).to_dict()
                # the exact command, kept with the result (reference IperfResult.command)
                record[direction] = {**got, "command": self._command}
        record["duration_s"] = round(self.clock() - self._t0 - t_start, 1)
        return record

    def _settled(self) -> bool:
        """After a speed change the link settles before measuring. False = stop."""
        self._set(doing="waiting for the link to settle")
        return not self._wait(self.cfg.wait_s)

    def _own_modes(self) -> bool:
        """This PC's card through every mode it supports, then back (reference: the
        measuring PC's card; a card that cannot be put back stops the test)."""
        card, (nic, _, _) = self.card, self.read_link(self.cfg.bind)
        if card is None or not nic or self.cfg.kind != "wired":
            return True
        try:
            for m in card.supported(nic):
                if self._stop.is_set():
                    break
                self._set(doing=f"{nic}: {m.label}")
                got = card.set_mode(nic, m)
                while "approval was declined" in str(got.get("why") or "") and hasattr(
                    card, "allow_again"
                ):  # 2026-10-29: refused or not answered in time -- the person chooses
                    choice = self._ask_person(
                        f"{nic} (administrator approval)", str(got["why"]),
                        "Press Fixed, check again and choose Yes when Windows asks "
                        "\"Allow changes?\".")  # fmt: skip
                    if choice != "retry":
                        break
                    card.allow_again()
                    got = card.set_mode(nic, m)
                setting = {"side": "LAN-side PC", "requested": m.label, **got}
                ok = got.get("readback_ok") and got.get("link")
                why = None if ok else (got.get("why") or "the card did not take the setting")
                if why is None and not self._settled():
                    break
                label = f"LAN card set to {m.label}"  # this LAN-side PC's own card
                self._keep(self._case({"label": label, "speed_mbps": m.speed},
                                      got.get("speed"), setting, why))  # fmt: skip
        finally:
            fix = (f"Set {nic}'s Speed & Duplex back to Auto by hand (Windows: Device Manager > "
                   f"the card > Advanced; Linux: sudo ethtool -s {nic} autoneg on), or unplug "
                   "and plug its cable.")  # fmt: skip
            done, _ = self._put_back_or_ask(nic, lambda: card.restore(nic), "ok", fix)
        return done

    def _wifi_bands(self) -> bool:
        """This PC's Wi-Fi card on each band the person picked: join that SSID on that
        access point (open, no password), measure both ways from the address it gets,
        then back on the network it was on. Not back = the list stops (SAFETY): the next
        cases would measure from a network nobody chose."""
        wifi = self.wifi
        nic = self.cfg.nic or self.read_link(self.cfg.bind)[0]
        if wifi is None or not nic or not self.cfg.bands:
            return True
        before = wifi.connection_of(nic)
        stopped = False  # a setting not put back (SAFETY) or a stop: the list ends
        try:
            for b in self.cfg.bands:
                if self._stop.is_set():
                    break
                band, ssid, bssid = str(b.get("band")), str(b.get("ssid")), str(b.get("bssid"))
                label = f"Wi-Fi {band} · {ssid}"
                self._set(doing=f"{nic}: joining {ssid} on {band}")
                got = wifi.join_band(nic, ssid, bssid, str(b.get("security") or "Open"),
                                     str(b.get("password") or ""))  # fmt: skip
                got.pop("password", None)  # a record never holds it
                setting = {"side": "LAN-side PC", "requested": f"{band} {ssid} ({bssid})", **got}
                why = None if got.get("ip") else (got.get("why") or "it did not join")
                if why is None:
                    self._bind = str(got["ip"])
                    if not self._settled():
                        break
                _, mbps, _ = self.read_link(self._bind) if why is None else ("", None, None)
                self._keep(self._case({"label": label, "speed_mbps": None, "band": band},
                                      mbps, setting, why))  # fmt: skip
                if why is None and not self._wifi_settings(nic, b, str(got["ip"])):
                    stopped = True
                    break
        finally:
            self._bind = ""
            self._set(doing=f"{nic}: back on {before or 'its network'}")
            fix = (f"Connect {nic} back to {before or 'its network'} by hand (the Wi-Fi menu, "
                   "or nmcli on Linux).")  # fmt: skip
            done, back = self._put_back_or_ask(nic, lambda: wifi.back(nic, before), "back",
                                               fix, to=before or "its network")  # fmt: skip
        if not done or stopped:
            return False
        if back.get("ip") and back["ip"] != self.cfg.bind:
            self._bind = str(back["ip"])  # the router gave it another address on the way back
        return self._settled()

    def _wifi_settings(self, nic: str, b: dict[str, Any], ip: str) -> bool:
        """On one band, this PC's own Wi-Fi settings one at a time (throughput.md 1.2,
        2026-10-29 user: "전에는 설정 변경 무선에서 안하고 그냥 1회 테스트하고 말았잖아"):
        each width cap and mode the card offers on that band, measured only when the card
        joined as asked, each put back before the next. False = stop."""
        wifi: Any = self.wifi  # options / set_option / restore_option: a card that has them
        offered = getattr(wifi, "options", None)
        if wifi is None or offered is None or not self.cfg.wifi_settings:
            return True
        band, ssid = str(b.get("band")), str(b.get("ssid"))
        net = {"ssid": ssid, "bssid": str(b.get("bssid") or ""), "ip": ip,
               "security": str(b.get("security") or "Open"),
               "password": str(b.get("password") or "")}  # fmt: skip
        for opt in offered(nic, band):
            if self._stop.is_set():
                return False
            label = f"Wi-Fi {band} · {opt['label']}"
            self._set(doing=f"{nic}: {band} {opt['label']}")
            got = dict(wifi.set_option(nic, opt, net))
            got.pop("password", None)
            setting = {"side": "LAN-side PC", "requested": f"{band} {opt['label']}", **got}
            ok = got.get("readback_ok") and got.get("link") and not got.get("why")
            why = None if ok else (got.get("why") or "the card did not take the setting")
            if why is None:
                self._bind = str(got.get("ip") or ip)
                if not self._settled():
                    return False
            _, mbps, _ = self.read_link(self._bind) if why is None else ("", None, None)
            self._keep(self._case({"label": label, "speed_mbps": None, "band": band},
                                  mbps or got.get("rx_mbps"), setting, why))  # fmt: skip
            fix = (f"Set {nic}'s Wi-Fi settings back by hand (Windows: Device Manager > the "
                   "card > Advanced: channel width and wireless modes as before; Linux: "
                   f"nmcli device set {nic} managed yes).")  # fmt: skip
            done, _ = self._put_back_or_ask(nic, lambda: wifi.restore_option(nic), "ok", fix,
                                            to="its own settings")  # fmt: skip
            if not done:
                return False
            again = wifi.join_band(nic, ssid, net["bssid"], net["security"], net["password"])
            if again.get("ip"):
                self._bind = str(again["ip"])
        return True

    def _ask(self, what: str) -> dict[str, Any] | None:
        """Ask the Controller to do ``what`` on the WAN-side PC; None = stopped."""
        with self._lock:
            self._seq += 1
            self.need = {"wan": what, "seq": self._seq}
            self._answered.clear()
        self._set(doing=f"waiting for the WAN-side PC ({what})")
        while not self._answered.wait(0.5):
            if self._stop.is_set():
                return None
        return None if self._stop.is_set() else self._answer

    def _wan_modes(self) -> bool:
        """The WAN-side PC's card through every mode it supports (this PC stays as it
        is): the Controller sets each one there and hands back what was read."""
        while True:
            got = self._ask("next")
            if got is None:
                return False
            if got.get("done"):
                break
            label = str(got.get("mode") or "?")
            setting = {"side": "WAN-side PC", "requested": label, **got}
            ok = got.get("readback_ok") and got.get("link")
            why = None if ok else (got.get("why") or "the WAN-side card did not take it")
            if why is None and not self._settled():
                return False
            name = f"WAN card set to {label}"
            self._keep(self._case({"label": name, "speed_mbps": got.get("speed")},
                                  got.get("speed"), setting, why))  # fmt: skip
        fix = ("On the WAN-side PC set its test card's speed back to Auto by hand (sudo "
               "ethtool -s <card> autoneg on), or unplug and plug its cable.")  # fmt: skip
        done, _ = self._put_back_or_ask("WAN card", lambda: self._ask("restore"), "ok", fix)
        return done and self._settled()

    def _measure(self, receive: bool, link_mbps: int) -> IperfResult:
        c = self.cfg
        udp = link_mbps * c.udp_pct / 100 if c.protocol == "udp" else None
        argv = client_argv(c.binary, c.server, c.seconds, receive, c.port, c.protocol,
                           c.streams, udp, self._bind or c.bind)  # fmt: skip
        self._command = " ".join(["iperf3", *argv[1:]])
        done = threading.Event()
        sampler = threading.Thread(target=self._sample, args=(receive, done),
                                   name="throughput-live", daemon=True)  # fmt: skip
        if self.read_bytes is not None:
            sampler.start()
        try:
            text = self.run_iperf(argv, c.seconds + 30)
            got = parse(text, c.protocol)
            # 2026-10-22: the first run right after a Wi-Fi join could not connect, the next
            # one did: a way not ready yet. Once more after a few seconds, and said so.
            if not got.ok and "unable to connect" in (got.error or "") and not self._wait(5.0):
                again = parse(self.run_iperf(argv, c.seconds + 30), c.protocol)
                got = again if again.ok else got
        except OSError as e:
            return IperfResult(False, error=f"iperf3 did not start: {e}")
        finally:
            done.set()
            if sampler.is_alive():
                sampler.join(2.0)
            self._set(live=None)
        return got

    def _sample(self, receive: bool, done: threading.Event) -> None:
        """The card's rate every second while iperf3 runs (received for WAN→LAN)."""
        read = self.read_bytes
        card = self.cfg.nic or self._bind or self.cfg.bind
        if read is None or not card:
            return
        direction = "WAN→LAN" if receive else "LAN→WAN"
        start = prev_t = time.monotonic()
        prev = read(card)
        while not done.wait(1.0) and not self._stop.is_set():
            now, cur = time.monotonic(), read(card)
            mbps = rate_mbps(prev, cur, now - prev_t, receive) if prev and cur else None
            if mbps is not None:
                self._set(live={"direction": direction, "mbps": round(mbps, 1),
                                "s": int(now - start), "e": self.run_second()})  # fmt: skip
            prev, prev_t = cur, now


def _speed_text(mbps: Any) -> str:
    return f"{mbps} Mbps" if isinstance(mbps, int | float) and mbps else ""


def _short(r: dict[str, Any]) -> dict[str, Any]:
    """A case in the heartbeat: what the screen shows while the list runs."""

    def rate(d: Any) -> float | None:
        return d.get("mbps") if isinstance(d, dict) else None

    return {
        "label": r["case"]["label"],
        "link": r["case"]["negotiated_mbps"],
        "send": rate(r["send"]),
        "receive": rate(r["receive"]),
        "not_run": r["not_run"],
    }
