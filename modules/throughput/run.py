"""Throughput rules the Controller uses through ``run_entry`` (docs/modules/throughput.md).

Pure functions like the IPTV ones (12 2): plan check, start/stop order, one record per
case -> a box (color + values), the device row. The Controller never names throughput.

Roles: the WAN-side PC (the plan's ``sender``) runs the iperf3 server; the LAN-side PCs
(``receivers``) are the clients, measured one after another (1.3): all are started at
once, every one but the first ``queued``; ``next_steps`` says go to the next when the one
before is done, ``finished`` ends the test when all are.

Judgement (0, T-5): a wired case passes when send and receive each reach ``S-59`` % (90)
of the link speed the card actually got; a Wi-Fi case has no threshold (values only) —
it fails only when a direction could not be measured. A case not run is grey.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.blocks.color_judge import Color
from core.blocks.nic_speed import LINK_WAIT_S
from core.protocol.envelope import ZlError
from modules.throughput.client import OWN_MODES, WAN_MODES, WIFI_BANDS
from modules.throughput.defaults import DEFAULTS
from modules.throughput.iperf import PORT, client_argv

PREFIX = "/v1/modules/throughput"
TEST_TYPES = {"Throughput · All": "all", "Throughput · Wired": "wired",
              "Throughput · Wi-Fi": "wifi"}  # fmt: skip
NETWORK = {"wired": "Wired", "wifi": "Wireless", "all": ""}  # which cards each test takes
CASE_OVERHEAD_S = 5  # iperf3 start and end, per direction
WIFI_SETTINGS_PER_BAND = 4  # an estimate: a width cap and two or three modes per band
WIFI_JOIN_S = 40  # a Wi-Fi setting: the card joins with it, then back and joined again
SERVER_S = 10  # the server start
STARTED = ("queued", "running", "done", "failed")
OVER = ("done", "failed", "stopped")


@dataclass(frozen=True)
class Step:
    device: str
    path: str
    params: dict[str, Any]
    timeout_s: float = 10.0


@dataclass(frozen=True)
class Target:
    """One card to measure: a PC's address on the router LAN side, by cable or Wi-Fi
    (2026-10-16 user: "LAN이나 WIFI가 있는 IP가 발견되면 하나하나 추가하고 순차 테스트")."""

    device: str  # the PC it is measured through (a tablet: the PC its USB is on)
    ip: str
    kind: str  # wired / wifi / tablet
    nic: str = ""
    # Wi-Fi: (band, ssid, bssid, security) the person picked
    bands: tuple[tuple[str, str, str, str], ...] = ()
    serial: str = ""  # a tablet (T-4): its adb serial
    name: str = ""  # a tablet: what its row says (model)

    @property
    def id(self) -> str:
        return f"{self.device}@tablet:{self.serial}" if self.serial else f"{self.device}@{self.ip}"


@dataclass(frozen=True)
class ThroughputPlan:
    test_type: str  # wired / wifi / all
    duration_s: int  # the estimate; the test ends when every card is done
    sender: str  # the WAN-side PC: iperf3 server
    receivers: tuple[tuple[str, int], ...]  # the LAN-side PCs, (pc, 1)
    seconds: int
    wait_s: float
    threshold_pct: float
    protocol: str
    streams: int
    cases: tuple[dict[str, Any], ...]
    nics: dict[str, str] = field(default_factory=dict)
    server_ip: str | None = None
    targets: tuple[Target, ...] = ()
    every_speed: bool = True
    directions: tuple[str, ...] = ("send", "receive")  # S-63
    udp_pct: float = 95.0  # S-64
    quality: str | None = None
    jitter_warn_ms: float = 0.0  # not used by this module (the Controller passes it)
    warmup_max_s: float = 30.0
    cell_s: int = 0
    not_run: tuple[str, ...] = ()
    on_air: tuple[int, ...] = ()

    @property
    def clients(self) -> list[str]:
        return list(dict.fromkeys(t.device for t in self.targets))

    def node_of(self, device: str) -> str:
        """The PC a row is reached through: a tablet's is the PC its USB is on."""
        return next((t.device for t in self.targets if t.kind == "tablet"
                     and tablet_name(t.serial) == device), device)  # fmt: skip


def _setting(s: dict[str, Any], sid: str) -> Any:
    return s.get(sid, DEFAULTS[sid])


_UI = {
    k: v.get("ui") or {}
    for k, v in json.loads((Path(__file__).with_name("defaults.json")).read_text(encoding="utf-8"))[
        "settings"
    ].items()
}


def _ranged(s: dict[str, Any], sid: str) -> float:
    """A number setting inside its range from the settings data (16, one place)."""
    v, lo, hi = _setting(s, sid), _UI[sid]["min"], _UI[sid]["max"]
    if not isinstance(v, int | float) or isinstance(v, bool) or not lo <= v <= hi:
        raise ZlError("RUN-014", f"{sid} must be {lo:g}..{hi:g}", f"It is {v!r}.")
    return float(v)


def _net24(ip: str) -> str:
    return ".".join(ip.split(".")[:3]) if ip.count(".") == 3 else ""


def missing_tablets(plan: dict[str, Any]) -> tuple[str, ...]:
    """Tablets ticked in S-65 that were not on their PC's USB when Start was pressed."""
    ticked = _setting(dict(plan.get("settings") or {}), "S-65") or {}
    present = plan.get("tablets_present") or {}
    out = []
    for device, cards in ticked.items() if isinstance(ticked, dict) else []:
        seen = present.get(device)
        if seen is None or not isinstance(cards, dict):
            continue  # not asked: its tablets are kept
        for nic in cards:
            serial = str(nic)[len(TABLET) :]
            if str(nic).startswith(TABLET) and serial not in seen:
                out.append(f"tablet {serial} on {device}: not connected by USB at Start")
    return tuple(out)


def find_targets(plan: dict[str, Any], sender: str, kind: str) -> list[Target]:
    """The cards the person ticked in Devices (S-65 "Cards to test", 2026-10-17 user:
    "연결된 포트가 있으면 그만큼 row가 늘어나야지 그래야 사용자가 고르지"): a wired one on
    the router LAN side; a Wi-Fi one with the bands picked in its row (its Auto case when
    it is already on the router LAN). Cables first, then Wi-Fi, by PC name. Without the
    cards (an old plan) the picked card of each receiver of that kind."""
    cards = plan.get("cards") or {}
    hosts = plan.get("hosts") or {}
    nics = {str(k): str(v) for k, v in (plan.get("nics") or {}).items()}
    out: list[Target] = []
    if cards:

        def lan_of(device: str) -> set[str]:
            nets = {_net24(str(hosts.get(device) or "")), _net24(nics.get(device, ""))}
            return {n for n in nets if n and not n.startswith("127.")}

        others = {n for d in cards if d != sender for n in lan_of(d)}
        ticked = _setting(dict(plan.get("settings") or {}), "S-65") or {}
        for device in sorted(ticked) if isinstance(ticked, dict) else []:
            mine = ticked[device]
            if device == sender or not isinstance(mine, dict):
                continue
            lan = lan_of(device) or others  # this PC (127.0.0.1): the LAN the others are on
            for nic, want in sorted(mine.items()):
                if str(nic).startswith(TABLET) and isinstance(want, dict):
                    # T-4 (2026-10-29): a tablet on this PC's USB, its Wi-Fi as it is or the
                    # band networks picked in its row
                    serial = str(nic)[len(TABLET) :]
                    seen = (plan.get("tablets_present") or {}).get(str(device))
                    if seen is not None and serial not in seen:
                        continue  # not on its PC's USB at Start: left out (missing_tablets)
                    out.append(Target(str(device), str(want.get("ip") or ""), "tablet", serial,
                                      _bands(want), serial,
                                      str(want.get("name") or serial)))  # fmt: skip
                    continue
                card: dict[str, Any] = next(
                    (c for c in cards.get(device) or [] if c.get("nic") == nic), {}
                )
                ip = str(card.get("ip") or "")
                on_lan = bool(ip) and _net24(ip) in lan and not ip.startswith("127.")
                if "wifi" not in card:
                    if on_lan:
                        out.append(Target(str(device), ip, "wired", str(nic)))
                    continue
                bands = _bands(want)
                if on_lan or bands:
                    out.append(Target(str(device), ip if on_lan else "", "wifi", str(nic), bands))
    else:
        wanted = NETWORK.get(kind, "Wired")
        for r in (plan.get("devices") or {}).get("receivers") or []:
            d = str(r.get("device") or "")
            net = str(r.get("network") or "Wired")
            if d and d != sender and (kind == "all" or net == wanted):
                ip = nics.get(d, "")
                if ip and ip != "wifi":
                    out.append(Target(d, ip, "wifi" if net == "Wireless" else "wired"))
                else:  # the card is not known yet: start_steps says so
                    out.append(Target(d, "", "wifi" if net == "Wireless" else "wired"))
    if kind != "all":  # a tablet is measured over Wi-Fi
        out = [t for t in out if t.kind == kind or (kind == "wifi" and t.kind == "tablet")]
    order = {"wired": 0, "wifi": 1, "tablet": 2}  # cables, then Wi-Fi cards, then tablets
    # 2026-10-29 user ("다시 이 와이파이로 돌아와야할거야, 그래야 클로드 AI로 계속 확인하지"):
    # the Controller PC's Wi-Fi is its way to the internet; it goes last of all, so every
    # other result is in before it leaves its network (it comes back after)
    last = {(CONTROLLER, "wifi")}
    return sorted(out, key=lambda t: ((t.device, t.kind) in last, order[t.kind], t.device,
                                      t.ip, t.serial))  # fmt: skip


BANDS = ("2.4GHz", "5GHz", "6GHz")
TABLET = "tablet:"  # S-65: a tablet's entry under the PC it hangs on, "tablet:<serial>"
CONTROLLER = "controller"  # the Controller PC as a LAN-side PC (controller/service.py THIS_PC)


def _bands(want: Any) -> tuple[tuple[str, str, str, str], ...]:
    """(band, ssid, bssid, security) picked for a Wi-Fi card, 2.4 then 5 then 6 GHz."""
    got = want.get("bands") if isinstance(want, dict) else None
    chosen: dict[str, Any] = got if isinstance(got, dict) else {}
    return tuple(
        (b, str(n.get("ssid") or ""), str(n.get("bssid") or ""), str(n.get("security") or "Open"))
        for b in BANDS
        if isinstance(n := chosen.get(b), dict) and n.get("ssid")
    )


def validate(plan: dict[str, Any]) -> ThroughputPlan:
    s = dict(plan.get("settings") or {})
    kind = str(plan.get("test_type") or "wired")
    if kind not in NETWORK:
        raise ZlError("RUN-014", f"Unknown throughput test {kind!r}", "Wired, Wi-Fi or all.")
    devices = plan.get("devices") or {}
    sender = devices.get("sender")
    if not isinstance(sender, str) or not sender:
        raise ZlError("RUN-014", "No PC on the router WAN side",
                      "The WAN-side PC runs the iperf3 server.",
                      "Connect a PC to the router WAN port, or choose it as Sender.")  # fmt: skip
    targets = find_targets(plan, sender, kind)
    missing = missing_tablets(plan)
    if not targets:
        net = {"wired": "by cable", "wifi": "over Wi-Fi", "all": "by cable or Wi-Fi"}[kind]
        fix = "Tick the cards to test in Devices (a Wi-Fi card with its bands)."
        raise ZlError("RUN-014", f"No PC on the router LAN side {net}",
                      "Each LAN-side card is measured against the WAN-side PC.", fix)  # fmt: skip
    protocol = str(_setting(s, "S-60")).lower()
    if protocol not in ("tcp", "udp"):
        raise ZlError("RUN-014", "S-60 must be TCP or UDP", f"It is {protocol!r}.")
    seconds = int(_ranged(s, "S-58"))
    wait = _ranged(s, "S-57")
    every = _setting(s, "S-62") == "Every supported"
    directions = {"LAN→WAN only": ("send",), "WAN→LAN only": ("receive",)}.get(
        str(_setting(s, "S-63")), ("send", "receive")
    )
    cases = ({"label": "Auto", "speed_mbps": None},)
    # an estimate only (the end comes from the PCs): a cable ~6 modes, the WAN side ~6
    per_case = len(directions) * (seconds + CASE_OVERHEAD_S) + (wait if every else 0)
    n_cases = sum(1 + (10 if every and t.kind == "wired" else 0) for t in targets)
    # 2026-10-29: each picked band, and with S-62 Every supported each Wi-Fi setting
    wifi = sum(len(t.bands) * (1 + (WIFI_SETTINGS_PER_BAND if every else 0))
               for t in targets if t.kind == "wifi")  # fmt: skip
    wifi += sum(max(1, len(t.bands)) for t in targets if t.kind == "tablet")
    n_cases += wifi
    per_wifi = WIFI_JOIN_S + wait
    nics = {str(k): str(v) for k, v in (plan.get("nics") or {}).items()}
    hosts = plan.get("hosts") or {}
    server_ip = nics.get(sender) or hosts.get(sender)
    duration = SERVER_S + 2 * wait * len(targets) + per_case * n_cases + per_wifi * wifi
    return ThroughputPlan(
        test_type=kind, duration_s=int(duration) + 300, sender=sender,
        # the PCs, then each tablet as its own row (0: no TV channel), reached through its PC
        receivers=tuple((c, 1) for c in dict.fromkeys(t.device for t in targets))
        + tuple((tablet_name(t.serial), 0) for t in targets if t.kind == "tablet"),
        seconds=seconds, wait_s=wait,
        threshold_pct=_ranged(s, "S-59"), protocol=protocol,
        streams=int(_ranged(s, "S-61")), cases=cases, nics=nics,
        server_ip=str(server_ip) if server_ip and server_ip != "wifi" else None,
        targets=tuple(targets), every_speed=every, directions=directions,
        udp_pct=_ranged(s, "S-64"), not_run=missing,
    )  # fmt: skip


def target_cases(p: ThroughputPlan, t: Target) -> list[dict[str, Any]]:
    """Each wired LAN card: Auto, then only the WAN card through its speeds (back to
    Auto), then only this LAN card through its speeds (back to Auto) -- every LAN card,
    not the first one only (2026-10-17 user: "auto -> wan 설정 변경 테스트 -> wan default
    auto back -> LAN 설정 변경 테스트 -> LAN default auto back ... 위 1 2 3 을 연결된 모든
    LAN/WLAN에 대해서"; reference sweep: a WAN node and one node per LAN port)."""
    cases: list[dict[str, Any]] = [dict(c) for c in p.cases] if t.ip else []
    if p.every_speed and t.kind == "wired":
        cases.append({"phase": WAN_MODES})
        cases.append({"phase": OWN_MODES})
    if t.kind == "tablet":  # its bands (joined by adb, then back), or its Wi-Fi as it is
        return [{"phase": WIFI_BANDS}] if t.bands else [{"label": "Wi-Fi as connected",
                                                         "speed_mbps": None}]  # fmt: skip
    if t.kind == "wifi" and t.bands:  # only the Wi-Fi changes, band by band (then back)
        cases.append({"phase": WIFI_BANDS})
    return cases


UNSUPPORTED_WIFI = [
    "Wi-Fi channel and security: the router's, not changed (recorded in each case); "
    "width caps and modes: only those this card's driver offers (Windows: its advanced "
    "settings; Linux: wpa_supplicant options), 11b alone not on Linux",
]


UNSUPPORTED_TABLET = [
    "Tablet: its Wi-Fi width and mode cannot be changed by an app or adb (recorded as "
    "joined, with the band); TCP only",
]


# ---------------------------------------------------------------- start and stop


def _client_step(p: ThroughputPlan, t: Target, elapsed_s: float = 0.0) -> Step:
    if not t.ip and t.kind != "tablet" and not (t.kind == "wifi" and t.bands):
        raise ZlError("RUN-014", f"The test card of {t.device} is not known",
                      "Its address on the router LAN side is needed.",
                      "Pick its card in the device row (Card).")  # fmt: skip
    return Step(t.device, f"{PREFIX}/client/start", {
        "device": t.device, "target": t.id, "server": p.server_ip, "bind": t.ip,
        "cases": target_cases(p, t), "seconds": p.seconds, "wait_s": p.wait_s,
        "protocol": p.protocol, "streams": p.streams, "kind": t.kind,
        "directions": list(p.directions), "udp_pct": p.udp_pct, "nic": t.nic,
        "wifi_settings": p.every_speed,  # S-62: Every supported = the card's Wi-Fi settings too
        "bands": [{"band": b, "ssid": s, "bssid": x, "security": sec}
                  for b, s, x, sec in t.bands],
        "unsupported": UNSUPPORTED_WIFI if t.kind == "wifi" else (
            UNSUPPORTED_TABLET if t.kind == "tablet" else []),
        # a tablet's records are its own row's (the judge and report), asked through its PC
        **({"serial": t.serial, "who": f"tablet {t.name}", "kind": "wifi",
            "device": tablet_name(t.serial), "host": t.device}
           if t.kind == "tablet" else {}),
        "elapsed_s": round(elapsed_s, 1),
        "judged": {"wired": t.kind == "wired", "pct": p.threshold_pct,
                   "directions": list(p.directions)},
    })  # fmt: skip


def start_steps(p: ThroughputPlan, skip_send: bool = False) -> list[list[Step]]:
    """The server, then the first card; the rest follow one by one (``next_steps``)."""
    if not p.server_ip:
        raise ZlError("RUN-014", f"The address of {p.sender} is not known",
                      "The LAN-side PCs connect to it through the router.")  # fmt: skip
    for t in p.targets:
        if not t.ip and t.kind != "tablet":  # a tablet's address is read when it measures
            _client_step(p, t)  # says which card is not known
    server = [Step(p.sender, f"{PREFIX}/server/start", {"bind": p.server_ip}, 15.0)]
    return [server, [_client_step(p, p.targets[0])]]


def stop_steps(p: ThroughputPlan, keep_send: bool = False) -> list[list[Step]]:
    return [[Step(c, f"{PREFIX}/client/stop", {}, 70.0) for c in p.clients],
            [Step(p.sender, f"{PREFIX}/server/stop", {}, 60.0)]]  # fmt: skip


def _clients(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    c = data.get("clients")
    return {str(k): v for k, v in c.items() if isinstance(v, dict)} if isinstance(c, dict) else {}


def _target_state(p: ThroughputPlan, data: dict[str, dict[str, Any]], t: Target) -> str | None:
    st = _clients(data.get(t.device) or {}).get(t.id)
    return str(st.get("state")) if st else None


def measuring(module_data: dict[str, Any]) -> bool:
    """Nothing warms up here: a PC whose cards wait their turn is ready too (2026-10-17:
    the start gave up with "receivers did not finish warm-up" -- only the first card had
    begun, as it should)."""
    return True


def next_steps(p: ThroughputPlan, data: dict[str, dict[str, Any]],
               elapsed_s: float) -> list[Step]:  # fmt: skip
    """One card at a time (1.3): the WAN side when the running card asks for it, else
    the next card once the one before is over."""
    server = (data.get(p.sender) or {}).get("server") or {}
    link = server.get("link") if isinstance(server, dict) else None
    link = link if isinstance(link, dict) else {}
    for t in p.targets:
        st = _clients(data.get(t.device) or {}).get(t.id)
        if not st or st.get("state") != "running":
            continue
        need = st.get("need")
        if not isinstance(need, dict) or need.get("person"):
            return []  # nothing asked, or the person is asked (Action Required)
        seq = need.get("seq")
        if link.get("seq") == seq and link.get("target") == t.id and not link.get("busy"):
            answer = {k: v for k, v in link.items() if k not in ("seq", "target", "busy")}
            return [Step(t.device, f"{PREFIX}/client/resume",
                         {"target": t.id, "seq": seq, "answer": answer})]  # fmt: skip
        if link.get("seq") == seq and link.get("target") == t.id:
            return []  # the WAN side is at it
        return [Step(p.sender, f"{PREFIX}/server/link",
                     {"what": need.get("wan"), "seq": seq, "target": t.id}, 15.0)]  # fmt: skip
    for t in p.targets:
        if _target_state(p, data, t) is None:
            return [_client_step(p, t, elapsed_s)]
        if _target_state(p, data, t) not in OVER:
            return []
    return []


def may_be_idle(p: ThroughputPlan, device: str) -> bool:
    """A PC whose cards wait their turn has nothing started yet: its node is IDLE, and
    that is not a PC lost (2026-10-17: "this-pc: node is IDLE" stopped the test). The
    WAN-side PC serves from the start, so it is never idle."""
    return device != p.sender


def finished(p: ThroughputPlan, data: dict[str, dict[str, Any]]) -> bool:
    return all(_target_state(p, data, t) in OVER for t in p.targets)


# ---------------------------------------------------------------- judgement


def _rate(d: Any) -> float | None:
    return float(d["mbps"]) if isinstance(d, dict) and d.get("mbps") is not None else None


DIRECTION = {"send": "LAN→WAN", "receive": "WAN→LAN"}


def threshold_mbps(summary: dict[str, Any], pct: float) -> float | None:
    link = (summary.get("case") or {}).get("negotiated_mbps")
    if not isinstance(link, int | float) or not link:
        return None
    return round(float(link) * pct / 100, 1)


def judge_case(summary: dict[str, Any], wired: bool = True,
               pct: float = float(DEFAULTS["S-59"])) -> tuple[Color, tuple[str, ...]]:  # fmt: skip
    if summary.get("not_run"):
        return Color.GREY, (f"not run: {summary['not_run']}",)
    why = []
    measured = (summary.get("judged") or {}).get("directions") or ("send", "receive")
    for d in measured:
        r = summary.get(d)
        if not isinstance(r, dict) or not r.get("ok"):
            err = r.get("error") if isinstance(r, dict) else "not measured"
            why.append(f"{DIRECTION[d]}: {err}")
    if why:
        return Color.RED, tuple(why)
    limit = threshold_mbps(summary, pct) if wired else None
    if limit is not None:
        for d in ("send", "receive"):
            got = _rate(summary.get(d))
            if got is not None and got < limit:
                link = (summary.get("case") or {}).get("negotiated_mbps")
                why.append(f"{DIRECTION[d]} {got:.0f} Mbps, below {limit:g} "
                           f"({pct:g} % of {link} Mbps)")  # fmt: skip
    if why and _half(summary.get("case") or {}):
        # 2026-10-17 user: "half도 설정하고 결과는 확인요망으로 노란색으로 알려주면": half
        # duplex shares the wire both ways (collisions), so below the pass level it is a
        # yellow "check needed", not a red fail
        return Color.YELLOW, (f"check needed (half duplex): {'; '.join(why)}",)
    return (Color.RED, tuple(why)) if why else (Color.GREEN, ())


def _half(case: dict[str, Any]) -> bool:
    """Half duplex in that case: the card read so, or the setting asked for it (a WAN card
    set to half leaves the LAN card full; Windows does not say a card's duplex -- 2026-10-22
    the Controller's and the WAN card's half cases were red, not yellow)."""
    setting = case.get("setting")
    requested = setting.get("requested") if isinstance(setting, dict) else ""
    asked = f"{requested or ''} {case.get('label') or ''}".lower()
    return case.get("duplex") == "half" or "half" in asked.split()


def cell_color(
    summary: dict[str, Any], jitter_warn_ms: float = 0.0
) -> tuple[Color, tuple[str, ...]]:
    """The plan's threshold and kind travel in the record (``judged``): a box is judged
    the same wherever it is read again."""
    j = summary.get("judged") or {}
    return judge_case(summary, bool(j.get("wired", True)),
                      float(j.get("pct", DEFAULTS["S-59"])))  # fmt: skip


def _set_on(setting: dict[str, Any], case: dict[str, Any]) -> str:
    """Whose card the case's speed was set on: the WAN-side PC's, this LAN-side card, or
    none (Auto on both ends)."""
    side = setting.get("side")
    if side == "WAN-side PC":
        return f"WAN card {setting.get('nic') or ''}".strip()
    if side in ("LAN-side PC", "this PC"):  # "this PC": records of build 0033 and before
        return f"LAN card {case.get('nic') or ''}".strip()
    return "— (both Auto)"


def _between(summary: dict[str, Any]) -> str | None:
    """ "t460-rx 192.168.45.139 (LAN) ⇄ 10.0.0.1 (WAN)": both ends of the case."""
    b = summary.get("between")
    if not isinstance(b, dict) or not b.get("lan") or not b.get("wan"):
        return None
    return f"{summary.get('device', '')} {b['lan']} (LAN) ⇄ {b['wan']} (WAN)".strip()


def _shown_rate(mbps: float | None) -> float | None:
    """934.158 -> 934, 9.224 -> 9.2: what a person compares (the record keeps all)."""
    if mbps is None:
        return None
    return float(round(mbps)) if mbps >= 100 else round(mbps, 1)


def case_name(label: Any) -> str:
    """The case as a person reads it (2026-10-17 user: "아무말 대잔치 ... 누가 이해할까"):
    which card's speed was set, or Auto on both. Records of 0033 and before said "WAN
    side ..." and "This PC ..."."""
    text = str(label or "")
    if text == "Auto":
        return "Auto (both cards on Auto)"
    renames = (
        ("WAN side ", "WAN card set to "),
        ("This PC ", "LAN card set to "),
        ("WAN card ", "WAN card set to "),
        ("LAN card ", "LAN card set to "),
    )
    for old, new in renames:
        if text.startswith(old) and not text.startswith(new):
            return new + text[len(old) :]
    return text


def cell_values(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """The case's values. ``detail``: shown on hover and in the report, not as a column
    of the screens' tables (2026-10-17 user: the table was a pile of half-explained
    columns)."""
    case = summary.get("case") or {}
    j = summary.get("judged") or {}
    pct = float(j.get("pct", DEFAULTS["S-59"]))

    def item(key: str, label: str, unit: str, value: Any, bad: bool = False,
             detail: bool = False) -> dict[str, Any]:  # fmt: skip
        out = {"key": key, "label": label, "unit": unit, "value": value, "bad": bad}
        if detail:
            out["detail"] = True
        return out

    send, recv = summary.get("send") or {}, summary.get("receive") or {}
    found = case.get("setting")
    setting: dict[str, Any] = found if isinstance(found, dict) else {}
    kind = "Wi-Fi" if case.get("kind") == "wifi" else "wired"
    got_b = summary.get("between")
    b: dict[str, Any] = got_b if isinstance(got_b, dict) else {}
    card = " ".join(x for x in (kind, str(b.get("lan") or ""), f"({case.get('nic')})"
                                if case.get("nic") else "") if x)  # fmt: skip
    return [
        item("card", "LAN-side card", "", card),
        item("case", "Case", "", case_name(case.get("label"))),
        item("link", "Link", "Mbps", case.get("negotiated_mbps")),
        item("send", "LAN→WAN", "Mbps", _shown_rate(_rate(send))),
        item("receive", "WAN→LAN", "Mbps", _shown_rate(_rate(recv))),
        item("between", "Between", "", _between(summary), detail=True),
        item("set_on", "Speed set on", "", _set_on(setting, case), detail=True),
        item("readback", "Setting read back", "",
             None if not setting else ("yes" if setting.get("readback_ok") else "no"),
             bad=bool(setting) and not setting.get("readback_ok"), detail=True),
        item("threshold", f"Pass at ({pct:g} % of the link)", "Mbps",
             threshold_mbps(summary, pct) if j.get("wired", True) else None, detail=True),
        item("retransmits", "TCP retransmits (LAN→WAN)", "", send.get("retransmits"),
             detail=True),
        item("lost_pct", "UDP loss", "%", send.get("lost_pct"), detail=True),
        item("unsupported", "Not changeable on this PC", "",
             "; ".join(summary.get("unsupported") or []) or None, detail=True),
        item("command", "iperf3 command", "",
             " / ".join(str(x.get("command")) for x in (send, recv) if x.get("command"))
             or None, detail=True),
    ]  # fmt: skip


def receiver_minimum_met(summaries: list[dict[str, Any]]) -> tuple[bool, str]:
    measured = [s for s in summaries if not s.get("not_run")]
    if not measured:
        return False, "no case could be measured"
    return True, ""


def device_totals(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    colors = [cell_color(s)[0] for s in summaries]
    sends = [r for s in summaries if (r := _rate(s.get("send"))) is not None]
    recvs = [r for s in summaries if (r := _rate(s.get("receive"))) is not None]
    return {
        "cases": len(summaries), "pass": colors.count(Color.GREEN),
        "fail": colors.count(Color.RED), "not_run": colors.count(Color.GREY),
        "send_max_mbps": max(sends, default=None), "receive_max_mbps": max(recvs, default=None),
    }  # fmt: skip


# ---------------------------------------------------------------- screens


def tablet_name(serial: str) -> str:
    """A tablet's row in Devices during a throughput test."""
    return f"tablet {serial}"


def receiver_rows(plan: ThroughputPlan, module_data: dict[str, Any], memo: dict[str, Any]
                  ) -> dict[str, dict[str, Any]]:  # fmt: skip
    """2026-10-30 user ("start누르고 devices화면에 여전히 태블릿 정보는 안나오네"): each tablet
    this PC measures is a row of its own, as in the IPTV test: waits, measures (the case
    and the rate now), done."""
    rows = {}
    for target, c in _clients(module_data).items():
        if "@tablet:" not in target:
            continue
        serial = target.rpartition("@tablet:")[2]
        state = str(c.get("state") or "")
        items = [_item("Wi-Fi tablet")]
        color = {"done": "green", "failed": "red"}.get(state, "grey")
        if state == "running":
            items.append(_item(f"now: {_doing(str(c.get('doing') or ''))}"))
            rate = c.get("live") if isinstance(c.get("live"), dict) else None
            if rate and rate.get("mbps") is not None:
                items.append(_item(f"{rate.get('direction')} now", rate["mbps"], "Mbps", True))
            color = "green"
        elif state == "queued":
            items.append(_item("waits its turn"))
        elif state == "done":
            items.append(_item("done"))
        elif c.get("error"):
            items.append(_item(str(c["error"])[:120]))
        done = c.get("results") or []
        if done:
            last = done[-1]
            text = str(last.get("not_run") or "") or (
                f"LAN→WAN {last.get('send')} · WAN→LAN {last.get('receive')} Mbps")  # fmt: skip
            items.append(_item(f"last: {last.get('label')} · {text}"))
        rows[tablet_name(serial)] = {"color": color, "why": "", "items": items,
                                     "link_label": "Wi-Fi"}  # fmt: skip
    return rows


def _item(text: str, value: Any = None, unit: str = "", key: bool = False) -> dict[str, Any]:
    # "label" as every module's row (the Devices panel read it; "text" showed nothing)
    return {"label": text, "value": value, "unit": unit, "key": key}


def live_row(plan: ThroughputPlan, module_data: dict[str, Any],
             memo: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
    items: list[dict[str, Any]] = []
    color, why = "green", ""
    server = module_data.get("server")
    clients = _clients(module_data)
    # 2026-10-17 user ("devices에도 컴퓨터 이름만 ... 아무말 대잔치 ... 누가 이해할까"): what
    # the PC is, then what it does now, in words
    if isinstance(server, dict):
        link = server.get("link") if isinstance(server.get("link"), dict) else {}
        mode = link.get("mode") if link and not link.get("done") else None
        items.append(
            _item(f"WAN side · every LAN card measures to this PC · WAN card: {mode or 'Auto'}")
        )
    else:
        items.append(_item("LAN side"))
    kinds = {t.id: t.kind for t in plan.targets} if plan is not None else {}
    live = next((c for c in clients.values() if c.get("state") == "running"), None)
    c = live or (list(clients.values())[-1] if clients else None)
    if c is None and not isinstance(server, dict):
        items.append(_item("waits its turn"))
    if c is not None:
        target = str(c.get("target", ""))
        kind = "Wi-Fi" if kinds.get(target) == "wifi" else "wired"
        ip = target.rpartition("@")[2]
        done = c.get("results") or []
        if live:
            doing = str(c.get("doing") or "")
            items.append(_item(f"now: {kind} card {ip} · {_doing(doing)}"))
            rate = c.get("live") if isinstance(c.get("live"), dict) else None
            if rate and rate.get("mbps") is not None:  # every second while iperf3 runs
                items.append(_item(f"{rate.get('direction')} now", rate.get("mbps"), "Mbps"))
                items.append(_item(f"({rate.get('s')} s) ·"))
        else:
            items.append(_item(f"done: {kind} card {ip}"))
        back = [x for x in c.get("put_back") or [] if isinstance(x, dict)]
        if back:  # the last setting put back, plainly (2026-10-22 user: "설정 원복 확실히")
            b = back[-1]
            mark = f"✓ {b.get('to')}" if b.get("ok") else f"✖ NOT put back: {b.get('why')}"
            items.append(_item(f"put back: {b.get('card')} {mark} ·"))
            if not b.get("ok"):
                color, why = "red", f"{b.get('card')} not put back: {b.get('why')}"
        if done:
            last = done[-1]
            items.append(_item(f"last: {case_name(last.get('label'))} ·"))
            items.append(_item("LAN→WAN", _shown_rate(last.get("send")), "Mbps"))
            items.append(_item("WAN→LAN", _shown_rate(last.get("receive")), "Mbps"))
            if last.get("not_run"):
                color, why = "grey", str(last["not_run"])
    failed = [x for x in clients.values() if x.get("state") == "failed"]
    if failed:
        color, why = "red", str(failed[0].get("error") or "the list stopped")
    label = "WAN" if isinstance(server, dict) else "LAN"
    return {"color": color, "why": why, "link_label": label, "items": items, "channel": None}


# 2026-10-30 user ("tput값을 숫자로만 보여주네. live view에서 그래프로 나오거나 하면 좋겠어.
# 다음거 나오면 또 다음거 보여주고"): the rate every second while a case measures, one line
# per direction on the run's clock; the cases follow one another on it
LIVE_COLUMNS: list[dict[str, Any]] = [
    {"key": "tx", "label": "LAN→WAN", "unit": "Mbps", "now": True, "chart": True, "digits": 1},
    {"key": "rx", "label": "WAN→LAN", "unit": "Mbps", "now": True, "chart": True, "digits": 1},
]  # fmt: skip


def second_records(p: ThroughputPlan, device: str, module_data: dict[str, Any]
                   ) -> list[tuple[tuple[str, int], dict[str, Any]]]:  # fmt: skip
    """The rate each list measures now, as a second of its row (a tablet: its own)."""
    out = []
    for target, c in _clients(module_data).items():
        live = c.get("live")
        if not isinstance(live, dict) or not isinstance(live.get("e"), int):
            continue
        name = tablet_name(target.rpartition("@tablet:")[2]) if "@tablet:" in target else device
        key = "rx" if live.get("direction") == "WAN→LAN" else "tx"
        out.append(((name, 1), {"e": live["e"], key: live.get("mbps")}))
    return out


def _doing(text: str) -> str:
    """The list's step in words: "Auto: send" -> "Auto · measuring LAN→WAN"."""
    for raw, words in ((": send", " · measuring LAN→WAN"), (": receive", " · measuring WAN→LAN")):
        if text.endswith(raw):
            return case_name(text[: -len(raw)]) + words
    return {"waiting for the link to settle": "waiting for the link to settle",
            "waiting after the last case": "finishing"}.get(text, text)  # fmt: skip


def live_view(p: ThroughputPlan) -> dict[str, Any]:
    wan = f"{p.sender} (WAN side, {p.server_ip})" if p.server_ip else f"{p.sender} (WAN side)"
    return {"columns": LIVE_COLUMNS, "cell_s": 0, "lines": {}, "method": method(p),
            "summary": f"Every case: a LAN-side card ⇄ {wan}, through the router. "
                       "LAN→WAN = the LAN-side PC sends, WAN→LAN = it receives."}  # fmt: skip


def preview(plan: dict[str, Any]) -> list[str]:
    """What Start will run, from the screen's choice (no PCs checked yet): the same words
    as ``method`` (2026-10-17 user: "test type이 iptv가 아니면 ... iperf관련한 걸 보여주는게
    맞지")."""
    s = dict(plan.get("settings") or {})
    sender = str((plan.get("devices") or {}).get("sender") or "")
    nics = {str(k): str(v) for k, v in (plan.get("nics") or {}).items()}
    server = nics.get(sender) or (plan.get("hosts") or {}).get(sender)
    directions = {"LAN→WAN only": ("send",), "WAN→LAN only": ("receive",)}.get(
        str(_setting(s, "S-63")), ("send", "receive")
    )
    p = ThroughputPlan(
        test_type=str(plan.get("test_type") or "all"), duration_s=0, sender=sender,
        receivers=(), seconds=int(_ranged(s, "S-58")), wait_s=_ranged(s, "S-57"),
        threshold_pct=_ranged(s, "S-59"), protocol=str(_setting(s, "S-60")).lower(),
        streams=int(_ranged(s, "S-61")), cases=(), nics=nics,
        server_ip=str(server) if server and server != "wifi" else None,
        every_speed=_setting(s, "S-62") == "Every supported", directions=directions,
        udp_pct=_ranged(s, "S-64"),
    )  # fmt: skip
    return method(p)


def _directions_line(p: ThroughputPlan) -> str:
    send = f"LAN→WAN {p.seconds} s (the LAN-side PC sends to the WAN-side PC, iperf3)"
    recv = f"WAN→LAN {p.seconds} s (the WAN-side PC sends to the LAN-side PC, iperf3 -R)"
    if p.directions == ("send",):
        what = f"one iperf3 run: {send} only"
    elif p.directions == ("receive",):
        what = f"one iperf3 run: {recv} only"
    else:
        what = (
            "two separate iperf3 runs, one after the other (never both at once): "
            f"{send}, then {recv}"
        )
    return f"Each case is {what} (Advanced: Directions, Measure time per direction)."


def method(p: ThroughputPlan) -> list[str]:
    """How this test measures, in words and the iperf3 command itself, for the live page
    and the report (2026-10-17 user: "iperf option이 뭔지 안적혀있어", "몇초동안 테스트하는지도
    모르고", "몇초 대기하는지도 모르고 설정간에")."""
    udp = p.udp_pct if p.protocol == "udp" else None
    argv = client_argv("iperf3", p.server_ip or "<WAN-side PC>", p.seconds, False, PORT,
                       p.protocol, p.streams, udp, "<this card's address>")  # fmt: skip
    command = " ".join(argv)
    if udp is not None:
        command = command.replace(f"-b {udp:g}M", f"-b <{udp:g} % of the link>M")
    lines = [
        f"Tool: iperf3, {p.protocol.upper()}, {p.streams} connection(s) at a time "
        f"(Advanced: Protocol, Parallel connections).",
        _directions_line(p),
        f"Command: {command}   — receiving adds -R.",
        f"Waits: {p.wait_s:g} s before a PC's first case and after its last; after each "
        f"speed change up to {LINK_WAIT_S:g} s for the link to come up, then {p.wait_s:g} s "
        "more before measuring (Advanced: Wait before and after each PC).",
        f"Pass: a wired case passes when send and receive each reach {p.threshold_pct:g} % "
        "of the link speed the card got; Wi-Fi shows values only (Advanced: Pass threshold).",
    ]
    if p.every_speed:
        lines.append("Link speeds: Auto first, then every speed the WAN-side card supports "
                     "(first wired card only), then every speed of each wired card, one side "
                     "at a time; each card goes back to its own speed after (Advanced: "
                     "Link speeds).")  # fmt: skip
    else:
        lines.append("Link speeds: as the cards are now (Auto only) (Advanced: Link speeds).")
    return lines


def link_need(plan: ThroughputPlan, device: str) -> dict[str, Any]:
    return {"bps": 0, "label": ""}  # the test finds the link's capacity itself


# 2026-10-29 (R20261029-214745): the WAN card at 10 Mbps half duplex, filled by iperf3, was
# also the Controller's only way to the WAN-side PC: no heartbeat answer for 10 s, "device
# lost", the test stopped. The cases slow the links on purpose; a PC of the test counts as
# lost only after this long without an answer.
# (not S-42's default: a throughput rule of its own, for the PCs whose links it slows)
SLOWED_LINK_LOST_S = 120.0


def lost_after_s(plan: ThroughputPlan, device: str) -> float:
    return SLOWED_LINK_LOST_S if device == plan.sender or device in plan.clients else 0.0


def required_tools(plan: ThroughputPlan, device: str) -> list[str]:
    return ["iperf3"] if device == plan.sender or device in plan.clients else []


class ThroughputRun:
    """What ``run_entry`` returns: the Controller calls only these."""

    validate = staticmethod(validate)
    start_steps = staticmethod(start_steps)
    stop_steps = staticmethod(stop_steps)
    measuring = staticmethod(measuring)
    next_steps = staticmethod(next_steps)
    finished = staticmethod(finished)
    may_be_idle = staticmethod(may_be_idle)
    cell_color = staticmethod(cell_color)
    cell_values = staticmethod(cell_values)
    receiver_minimum_met = staticmethod(receiver_minimum_met)
    device_totals = staticmethod(device_totals)
    live_row = staticmethod(live_row)
    live_view = staticmethod(live_view)
    method = staticmethod(method)  # how it measures: live page and report
    preview = staticmethod(preview)  # what Start will run: the Test area
    link_need = staticmethod(link_need)
    required_tools = staticmethod(required_tools)
    receiver_rows = staticmethod(receiver_rows)
    second_records = staticmethod(second_records)
    node_of = staticmethod(lambda p, d: p.node_of(d))
    lost_after_s = staticmethod(lost_after_s)
    receiver_devices = staticmethod(lambda p: list(p.clients))
    current_channels = staticmethod(lambda data: list[int]())
    auto_channel = staticmethod(lambda p: None)
    next_channel = staticmethod(lambda p, ch: ch)
    broadcast_info = staticmethod(lambda p: {"channels": [], "sender": p.sender})
    prepare_state = staticmethod(lambda data: {"busy": False, "channels": []})
    prepare_progress = staticmethod(lambda p, data: {"done": True})
    always_on = staticmethod(lambda p: False)
    # it sets the cards' speeds itself (2026-10-17: "link at 10 Mbps ... often a cable or
    # port problem" for a 10 Mbps case it had set) and names each card in its own rows
    changes_links = True
    heartbeat_key = "throughput"
    test_types = TEST_TYPES


def create() -> ThroughputRun:
    return ThroughputRun()
