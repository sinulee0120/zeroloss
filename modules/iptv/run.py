"""IPTV rules the Controller uses through ``run_entry`` (docs/12-architecture.md 2).

Pure functions: plan check (docs/16-settings.md, 05 3.3), the commands for each role
in start order, and 5-minute summary -> cell input (05 5.3, minimum packets rule).
The Controller itself never names IPTV; it calls these through the module contract.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field, replace
from typing import Any, ClassVar

from core.blocks.color_judge import Color
from core.blocks.unicode_path import display_name
from core.protocol.envelope import ZlError
from core.self_check import Check
from modules.iptv import record_keys as k
from modules.iptv.cell_rules import MIN_BITRATE_BPS, MIN_PACKETS, CellInput, judge_cell
from modules.iptv.defaults import DEFAULTS
from modules.iptv.sender.commands import QUALITIES
from modules.iptv.sender.sources import safe_name

PREFIX = "/v1/modules/iptv"
DURATION_MIN_S, DURATION_MAX_S = 10, 200 * 3600
BITRATE_MBPS = {"UHD": (30, 40), "HD": (8, 20)}
DEFAULT_GROUPS = tuple(DEFAULTS["S-14"]["groups"])
CHANNELS = (1, 2, 3)
DEFAULT_PORT = int(DEFAULTS["S-14"]["port"])
# S-50 values as in the settings table (16), the window sizes come from these strings
WINDOW_VALUES = ("Small (640\u00d7360)", "Medium (1280\u00d7720)", "Full screen")


@dataclass(frozen=True)
class Step:
    device: str
    path: str
    params: dict[str, Any]
    timeout_s: float = 5.0


@dataclass(frozen=True)
class IptvPlan:
    test_type: str
    quality: str
    duration_s: int
    muxrate_bps: int
    sender: str
    receivers: tuple[tuple[str, int], ...]  # (device, channel)
    groups: dict[int, str]
    port: int
    ttl: int
    warmup_max_s: float
    jitter_warn_ms: float
    cell_s: int = 300
    files: dict[int, str] = field(default_factory=dict)
    nics: dict[str, str] = field(default_factory=dict)
    playback: dict[str, Any] | None = None  # S-26/S-25/S-50, None = S-49 Off
    not_run: tuple[str, ...] = ()  # "playback" when S-49 Off (K-29)
    prime_setting: str = str(DEFAULTS["S-18"])  # S-18 Auto / Off / On (iptv 4.5)
    sources: dict[int, str] = field(default_factory=dict)  # S-48 user files (iptv 1-1)
    whole_movie: bool = False  # S-55: the whole S-48 file, not its first 10 minutes
    primed: bool = False  # this plan sends primed unicast (On, or Auto after step 5)
    sender_ip: str | None = None  # where receivers send the prime (nics, else hosts)
    on_air: tuple[int, ...] = ()  # channels the sender sends (20 GAP-91)
    zap_every_s: float | None = None  # S-21 automatic channel change, None = Manual
    zap_stagger_s: float = 10.0  # S-22, 0 = all receivers at once
    success_timeout_s: float = 5.0  # S-47 (success rate only, never a color)
    always_on: bool = False  # S-13 Always-on: the sending outlives the test (F18)
    evidence: bool = False  # S-30 On: the picture of each trouble second (F23)
    buffers: dict[str, int] = field(default_factory=dict)  # player buffer per receiver PC
    # Tablet test (01-overview 4, D3): each tablet -> (the PC it hangs on by USB, its
    # serial). The tablet is the receiver; that PC only relays (it receives no video)
    tablets: dict[str, tuple[str, str]] = field(default_factory=dict)
    tablet_buffer_ms: int = int(DEFAULTS["S-25"])  # S-25 for tablets without their own
    tablet_wifi: dict[str, dict[str, str]] = field(default_factory=dict)  # {ssid, security}

    def node_of(self, device: str) -> str:
        """The PC a receiver's commands and heartbeat go through: a tablet's USB host."""
        return self.tablets[device][0] if device in self.tablets else device

    @property
    def nodes(self) -> tuple[str, ...]:
        """The receiving PCs to talk to (a tablet's host once, however many tablets)."""
        return tuple(sorted({self.node_of(d) for d, _ in self.receivers}))

    def playback_of(self, device: str) -> dict[str, Any] | None:
        """S-25 for this receiver: its own buffer when the plan gives one (2026-10-16
        user: "수신기마다 다를거아니냐"), else the test's."""
        if self.playback is None or device not in self.buffers:
            return self.playback
        return {**self.playback, "buffer_ms": self.buffers[device]}

    @property
    def flows(self) -> tuple[int, ...]:
        """Primed copies per receiver: analyzer, plus the player when playback is on."""
        return (0, 1) if self.playback is not None else (0,)


def _bad(setting: str, rule: str) -> ZlError:
    return ZlError(
        "RUN-014", f"{setting} must be {rule}", "The plan has a value outside the table."
    )


def _num(settings: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    value = settings.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool) or not lo <= value <= hi:
        raise _bad(key, f"{lo:g}..{hi:g}")
    return float(value)


def mac_of(group: str) -> int:
    """Ethernet multicast MAC keeps only the low 23 bits of the group (RFC 1112 6.4)."""
    return int(ipaddress.IPv4Address(group)) & 0x7FFFFF


def validate(plan: dict[str, Any]) -> IptvPlan:
    """Check a plan (02 11-1). Raises ZlError RUN-013 / RUN-014."""
    test_type = plan.get("test_type")
    if test_type not in ("pc", "tablet"):
        raise _bad("test_type", "pc or tablet")
    quality = plan.get("quality", "UHD")
    if quality not in QUALITIES:
        raise _bad("quality (S-03)", "UHD or HD")
    duration = plan.get("duration_s")
    if not isinstance(duration, int) or not DURATION_MIN_S <= duration <= DURATION_MAX_S:
        raise _bad("duration_s (S-05)", "10 s .. 200 h")
    s: dict[str, Any] = dict(plan.get("settings") or {})
    if s.get("S-10", DEFAULTS["S-10"]) != DEFAULTS["S-10"]:
        raise ZlError(  # sender and receiver only speak RTP in this build (16 3)
            "RUN-013", "MPEG-TS over UDP (S-10) is not available in this build", "Use RTP."
        )
    playback = _playback(s)
    sources = _sources(s)
    length = s.get("S-55", DEFAULTS["S-55"])
    if length not in ("10 min", "Whole movie"):
        raise _bad("S-55", "10 min or Whole movie")
    prime = s.get("S-18", DEFAULTS["S-18"])
    if prime not in ("Auto", "Off", "On"):
        raise _bad("S-18", "Auto, Off or On")
    lo, hi = BITRATE_MBPS[quality]
    mbps = _num(s, "S-11", DEFAULTS["S-11"][quality], lo, hi)
    if mbps != int(mbps):
        raise _bad("S-11", "whole Mbps")
    ttl = int(_num(s, "S-15", DEFAULTS["S-15"], 8, 64))
    warmup = _num(s, "S-28", DEFAULTS["S-28"], 5, 60)
    jitter_warn = _num(s, "S-23", DEFAULTS["S-23"], 10, 149)
    devices = plan.get("devices") or {}
    sender = devices.get("sender")
    receivers = devices.get("receivers") or []
    if not isinstance(sender, str) or not sender:
        raise _bad("devices.sender", "a device name")
    if test_type == "pc" and len(receivers) != 1:
        raise _bad("devices.receivers", "exactly one PC receiver in a PC test")
    # 2026-10-23 user: "수신기 pc 한대, 태블릿 두대" -- a tablet test may have the PC
    # receiver too, side by side with the tablets (it also relays them)
    n_tablets = sum(1 for r in receivers if isinstance(r, dict) and r.get("serial"))
    n_pcs = len(receivers) - n_tablets
    if test_type == "tablet" and (not 1 <= n_tablets <= 3 or n_pcs > 1):
        raise _bad(
            "devices.receivers", "one to three tablets (and one PC at most) in a tablet test"
        )
    pairs = []
    buffers = {}
    tablets: dict[str, tuple[str, str]] = {}
    tablet_wifi: dict[str, dict[str, str]] = {}
    for r in receivers:
        if test_type == "tablet" and r.get("serial") is not None:
            host, serial = r.get("host"), r.get("serial")
            if not isinstance(host, str) or not host or not isinstance(serial, str) or not serial:
                raise _bad("devices.receivers", "{device, ch, host, serial} for each tablet")
            if serial in {x for _, x in tablets.values()}:
                raise _bad("devices.receivers", "each tablet once (a serial twice)")
            tablets[str(r.get("device"))] = (host, serial)
            wifi = r.get("wifi")
            if isinstance(wifi, dict) and wifi.get("ssid"):
                if wifi.get("security") not in ("Open", "WPA2", "WPA3"):
                    raise _bad("devices.receivers wifi.security", "Open, WPA2 or WPA3")
                tablet_wifi[str(r.get("device"))] = {"ssid": str(wifi["ssid"]),
                                                     "security": str(wifi["security"])}  # fmt: skip
        ch = r.get("ch", 1)
        if not isinstance(ch, int) or not 1 <= ch <= 3 or not isinstance(r.get("device"), str):
            raise _bad("devices.receivers", "{device, ch 1..3}")
        pairs.append((r["device"], ch))
        ms = r.get("buffer_ms")
        if ms is not None:
            if not isinstance(ms, int) or not 300 <= ms <= 5000:
                raise _bad("devices.receivers buffer_ms (S-25)", "300 .. 5000 ms")
            buffers[r["device"]] = ms
    if len({d for d, _ in pairs}) != len(pairs):
        raise _bad("devices.receivers", "each receiver once")
    groups, port = _groups(s)
    every, stagger = _auto_change(s)
    if tablets:  # the app always plays (K-30); no automatic change on tablets yet
        every = None
    broadcast = s.get("S-13", DEFAULTS["S-13"])
    if broadcast not in ("Test-linked", "Always-on"):
        raise _bad("S-13", "Test-linked or Always-on")
    if every is not None and every < warmup + 30:  # K-10: no time left to measure
        raise _bad("S-21", f"at least the start-up wait (S-28) + 30 s = {warmup + 30:g} s")
    on_air = CHANNELS if every is not None else tuple(sorted({ch for _, ch in pairs}))
    return IptvPlan(
        test_type=str(test_type),
        quality=quality,
        duration_s=duration,
        muxrate_bps=int(mbps * 1_000_000),
        sender=sender,
        receivers=tuple(pairs),
        groups=groups,
        port=port,
        ttl=ttl,
        warmup_max_s=warmup,
        jitter_warn_ms=jitter_warn,
        files=_files(plan),
        nics={str(k): str(v) for k, v in (plan.get("nics") or {}).items()},
        playback=playback,
        not_run=() if playback else ("playback",),
        prime_setting=str(prime),
        sources=sources,
        whole_movie=length == "Whole movie",
        primed=prime == "On" and not tablets,  # tablets get the multicast (iptv 4.5: PCs)
        sender_ip=_sender_ip(plan, sender),
        on_air=on_air,
        always_on=broadcast == "Always-on",
        evidence=s.get("S-30", DEFAULTS["S-30"]) == "On",
        buffers=buffers,
        tablets=tablets,
        tablet_wifi=tablet_wifi,
        tablet_buffer_ms=int(_num(s, "S-25", DEFAULTS["S-25"], 300, 5000)),
        zap_every_s=every,
        zap_stagger_s=stagger,
        success_timeout_s=_num(s, "S-47", DEFAULTS["S-47"], 0.5, 30),
    )


def _auto_change(s: dict[str, Any]) -> tuple[float | None, float]:
    """S-21 ``Manual`` / ``Every <N> min`` (N 1..120) and S-22 ``On (<N> s)`` (N 5..60) /
    ``Off`` (16, 20 GAP-91) -> (seconds between changes or None, stagger seconds)."""
    raw = str(s.get("S-21", DEFAULTS["S-21"])).strip().lower()
    every = None
    if raw != "manual":
        m = re.fullmatch(r"every\s+(\d{1,3})\s*min", raw)
        if m is None or not 1 <= int(m.group(1)) <= 120:
            raise _bad("S-21", "Manual or Every N min (N 1..120)")
        every = int(m.group(1)) * 60.0
    stag = str(s.get("S-22", DEFAULTS["S-22"])).strip().lower()
    if stag == "off":
        return every, 0.0
    m = re.fullmatch(r"on\s*\(\s*(\d{1,2})\s*s\s*\)", stag)
    if m is None or not 5 <= int(m.group(1)) <= 60:
        raise _bad("S-22", "On (N s) with N 5..60, or Off")
    return every, float(m.group(1))


def _sources(s: dict[str, Any]) -> dict[int, str]:
    """S-48 per channel (iptv 1-1): a file name in the sender's media folder."""
    raw = s.get("S-48", DEFAULTS["S-48"])
    if raw == "Generated":
        return {}
    if not isinstance(raw, dict):
        raise _bad("S-48", "Generated or {channel: file name}")
    out = {}
    for ch, name in raw.items():
        if not str(ch).isdigit() or not 1 <= int(ch) <= 3:
            raise _bad("S-48", "channels 1..3")
        if name == "Generated":
            continue
        if not safe_name(name):
            raise _bad("S-48", "a file name in the media folder (no path)")
        out[int(ch)] = str(name)
    return out


def _sender_ip(plan: dict[str, Any], sender: str) -> str | None:
    """The sender PC's address a receiver primes (iptv 4.5): its test NIC, else the
    address the Controller has for it (02 11-1 ``hosts``)."""
    nics, hosts = plan.get("nics") or {}, plan.get("hosts") or {}
    for value in (nics.get(sender), hosts.get(sender)):
        if isinstance(value, str) and value and value != "wifi":
            return value
    return None


def prime_ports(port: int, ch: int, flow: int) -> tuple[int, int, int]:
    """(P, Q, R) of iptv 4.5: tsp sends from P, the sender hears primes on Q, the
    receiver listens on R. Distinct for every channel and flow, even on one PC."""
    base = port + 100 * ch + 10 * flow
    return base, base + 1, base + 2


def _playback(s: dict[str, Any]) -> dict[str, Any] | None:
    """S-49 On/Off, S-26 player, S-25 buffer, S-50 window (16-settings, iptv 3.2)."""
    on = s.get("S-49", DEFAULTS["S-49"])
    if on not in ("On", "Off"):
        raise _bad("S-49", "On or Off")
    if on == "Off":
        return None
    player = s.get("S-26", DEFAULTS["S-26"])
    if player not in ("mpv", "VLC"):
        raise _bad("S-26", "VLC or mpv")
    buffer_ms = int(_num(s, "S-25", DEFAULTS["S-25"], 300, 5000))
    window = s.get("S-50", DEFAULTS["S-50"])
    if window not in WINDOW_VALUES:
        raise _bad("S-50", " / ".join(WINDOW_VALUES))
    return {"player": player, "buffer_ms": buffer_ms, "window": window}


def _files(plan: dict[str, Any]) -> dict[int, str]:
    """Prepared files per channel (optional): a channel without one sends the generated
    video (iptv 1-1-2)."""
    raw = plan.get("files") or {}
    if not isinstance(raw, dict):
        raise _bad("files", "{channel: path}")
    files = {}
    for key, v in raw.items():
        if not str(key).isdigit() or not 1 <= int(key) <= 3 or not isinstance(v, str) or not v:
            raise _bad("files", "{1..3: path on the sender}")
        files[int(key)] = v
    return files


def _groups(s: dict[str, Any]) -> tuple[dict[int, str], int]:
    raw = s.get("S-14") or {}
    groups = list(raw.get("groups", DEFAULT_GROUPS))
    port = raw.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise _bad("S-14 port", "1024..65535")
    if len(groups) != 3:
        raise _bad("S-14 groups", "three addresses (CH1..CH3)")
    for g in groups:
        try:
            ip = ipaddress.IPv4Address(g)
        except ValueError:
            raise _bad("S-14 groups", "IPv4 addresses") from None
        if ip not in ipaddress.IPv4Network("239.0.0.0/8"):
            raise _bad("S-14 groups", "inside 239.0.0.0/8 (RFC 2365)")
        if mac_of(g) & 0x7FFF00 == 0:  # same MAC as 224.0.0.x: flooded to all ports
            raise _bad("S-14 groups", f"not sharing a MAC with 224.0.0.x ({g})")
    if len({mac_of(g) for g in groups}) != 3:
        raise _bad("S-14 groups", "three different Ethernet MACs (low 23 bits)")
    return {i + 1: g for i, g in enumerate(groups)}, port


# ---------------------------------------------------------------- steps


def start_steps(p: IptvPlan, skip_send: bool = False) -> list[list[Step]]:
    """Start order (05 2.1-2 rule 1): stage 1 senders (each waits for a stable rate),
    stage 2 receivers (join; warm-up is watched by heartbeat). Primed (iptv 4.5): the
    senders only open their prime ports; the stream starts when the receivers prime.
    ``skip_send``: the broadcast of this plan is on already (F18), receivers only."""
    if skip_send:
        return [(_primed_start_steps(p) if p.primed else start_steps(p))[-1]]
    if p.primed:
        return _primed_start_steps(p)
    channels = list(p.on_air)
    senders = [
        Step(
            p.sender,
            f"{PREFIX}/sender/start",
            {
                "ch": ch,
                "quality": p.quality,
                **({"file": p.files[ch]} if ch in p.files else {}),
                **({"source": p.sources[ch]} if ch in p.sources else {}),
                **({"length": "whole"} if ch in p.sources and p.whole_movie else {}),
                "dest_ip": p.groups[ch],
                "port": p.port,
                "muxrate_bps": p.muxrate_bps,
                "ttl": p.ttl,
                **({"local_address": p.nics[p.sender]} if p.sender in p.nics else {}),
            },
            timeout_s=15.0,
        )
        for ch in channels
    ]
    receivers = [
        _tablet_start(p, device, ch) if device in p.tablets else Step(
            device,
            f"{PREFIX}/receiver/start",
            {
                "ch": ch,
                "group": p.groups[ch],
                "port": p.port,
                "device": device,
                "cell_s": p.cell_s,
                "warmup_max_s": p.warmup_max_s,
                "playback": p.playback_of(device),
                "evidence": p.evidence,
                **({"interface_ip": p.nics[device]} if device in p.nics else {}),
            },
        )
        for device, ch in p.receivers
    ]  # fmt: skip
    return [senders, receivers]


def _tablet_start(p: IptvPlan, device: str, ch: int) -> Step:
    """A tablet receives through the PC it hangs on (02 4 ``tablet/start``); the app
    plays with this tablet's buffer, else S-25."""
    host, serial = p.tablets[device]
    params = {"device": device, "serial": serial, "ch": ch, "group": p.groups[ch],
              "port": p.port, "cell_s": p.cell_s, "play": True,
              "buffer_ms": p.buffers.get(device, p.tablet_buffer_ms)}  # fmt: skip
    if device in p.tablet_wifi:  # the Controller adds the band's password (never in the plan)
        params["bands"] = [p.tablet_wifi[device]]
    # joining a Wi-Fi waits up to 30 s (TAB-014) before the app starts
    return Step(host, f"{PREFIX}/tablet/start", params, timeout_s=75.0)


def _primed_start_steps(p: IptvPlan) -> list[list[Step]]:
    senders = [
        Step(
            p.sender,
            f"{PREFIX}/sender/start",
            {
                "ch": ch,
                "quality": p.quality,
                **({"file": p.files[ch]} if ch in p.files else {}),
                **({"source": p.sources[ch]} if ch in p.sources else {}),
                **({"length": "whole"} if ch in p.sources and p.whole_movie else {}),
                "muxrate_bps": p.muxrate_bps,
                "ttl": p.ttl,
                "prime": dict(zip(("p", "q"), prime_ports(p.port, ch, f)[:2], strict=True))
                | {"flow": f},
                **({"local_address": p.nics[p.sender]} if p.sender in p.nics else {}),
            },
            timeout_s=15.0,
        )
        for ch in _primed_channels(p)
        for f in p.flows
    ]
    receivers = [
        Step(
            device,
            f"{PREFIX}/receiver/start",
            {
                "ch": ch,
                "group": p.groups[ch],
                "port": p.port,
                "device": device,
                "cell_s": p.cell_s,
                "warmup_max_s": p.warmup_max_s,
                "playback": p.playback_of(device),
                "evidence": p.evidence,
                "prime": _receiver_prime_params(p, ch),
                **({"interface_ip": p.nics[device]} if device in p.nics else {}),
            },
        )
        for device, ch in p.receivers
    ]
    return [senders, receivers]


def _receiver_prime_params(p: IptvPlan, ch: int) -> dict[str, Any]:
    return {
        "sender_ip": p.sender_ip,
        "flows": [
            dict(zip(("p", "q", "r"), prime_ports(p.port, ch, f), strict=True)) | {"f": f}
            for f in p.flows
        ],
    }


def _primed_channels(p: IptvPlan) -> list[int]:
    """A primed flow sends to one receiver: one per receiver, plus the other channels on
    air that a receiver may change to (F19)."""
    mine = [ch for _, ch in p.receivers]
    return mine + [ch for ch in p.on_air if ch not in mine]


def stop_steps(p: IptvPlan, keep_send: bool = False) -> list[list[Step]]:
    """Stop order (rule 5): receivers close first, then senders. ``keep_send``: the test
    of an Always-on plan ends, the broadcast goes on (F18)."""
    checks = [Step(d, f"{PREFIX}/receiver/perfcheck/cancel", {}, timeout_s=30.0)  # RCV-015
              for d in sorted({d for d, _ in p.receivers if d not in p.tablets})]  # fmt: skip
    receivers = [
        Step(p.node_of(d), f"{PREFIX}/tablet/stop", {"device": d}, timeout_s=30.0)
        if d in p.tablets else Step(d, f"{PREFIX}/receiver/stop", {"ch": ch})
        for d, ch in p.receivers
    ]  # fmt: skip
    if keep_send:
        return [receivers, checks]
    senders, helpers = broadcast_stop_steps(p)
    return [receivers, senders, helpers + checks]


def broadcast_stop_steps(p: IptvPlan) -> list[list[Step]]:
    """The sending and its helpers only (F18 [Stop broadcast], and every full stop)."""
    return [  # no helper to stop: a video being made goes on, the next Start uses it (GAP-116)
        [Step(p.sender, f"{PREFIX}/sender/stop", {"ch": ch}) for ch in p.on_air],
        [],
    ]


def same_broadcast(running: IptvPlan, new: IptvPlan) -> IptvPlan | None:
    """F18: can the new test use the broadcast that runs? Same sender, channels, video
    quality, bitrate, addresses and sources. The new plan takes the running one's
    sending path (S-18 Auto found it then; its step 5 is not sent again). None: not
    the same — Stop the broadcast first (RUN-014)."""
    same = all(getattr(running, f) == getattr(new, f) for f in (
        "sender", "on_air", "quality", "muxrate_bps", "groups", "port", "ttl", "files",
        "sources",
    ))  # fmt: skip
    same = same and new.nics.get(new.sender) == running.nics.get(running.sender)
    if running.primed:  # a primed flow per receiver flow: the flows must match too
        same = same and running.flows == new.flows
    path_ok = new.prime_setting == "Auto" or (new.prime_setting == "On") == running.primed
    if not (same and new.always_on and path_ok):
        return None
    return replace(new, primed=running.primed)


def broadcast_info(p: IptvPlan) -> dict[str, Any]:
    """02 2-1 ``broadcast`` values of this plan."""
    return {"channels": list(p.on_air), "sender": p.sender}


# ---------------------------------------------------------------- videos (iptv 1-1-2)


def prepare_steps(p: IptvPlan) -> list[Step]:
    """Before sending: ask the sender to make the videos that are missing (returns at
    once; progress comes in the heartbeat, ``prepare_progress``)."""
    wanted = [{"ch": ch, "file": p.files.get(ch), "source": p.sources.get(ch)} for ch in p.on_air]
    params = {"quality": p.quality, "muxrate_bps": p.muxrate_bps, "channels": wanted,
              "only": True, **({"length": "whole"} if p.whole_movie else {})}  # fmt: skip
    return [Step(p.sender, f"{PREFIX}/sender/prepare", params, timeout_s=10.0)]


def prepare_ahead(params: dict[str, Any]) -> Step:
    """``zeroloss prepare`` (02 2-4): the same node command outside a test."""
    quality = params.get("quality")
    if quality not in QUALITIES:
        raise _bad("quality (S-03)", "UHD or HD")
    sender = params.get("sender")
    if not isinstance(sender, str) or not sender:
        raise _bad("sender", "a device name")
    lo, hi = BITRATE_MBPS[quality]
    mbps = _num(params, "bitrate_mbps", DEFAULTS["S-11"][quality], lo, hi)
    if mbps != int(mbps):
        raise _bad("bitrate_mbps", "whole Mbps")
    chans = params.get("channels", [1, 2, 3])
    if not isinstance(chans, list) or not chans or not all(c in (1, 2, 3) for c in chans):
        raise _bad("channels", "a list of 1..3")
    sources = params.get("sources") or {}  # S-48 per channel: a movie picked on the screen
    if not isinstance(sources, dict) or not all(
        isinstance(v, str) and safe_name(v) for v in sources.values()
    ):
        raise _bad("sources", 'file names per channel, e.g. {"1": "movie.mp4"}')
    wanted = [{"ch": c, "file": None, "source": sources.get(str(c))} for c in sorted(set(chans))]
    if params.get("length", "10 min") not in ("10 min", "whole"):
        raise _bad("length", "10 min or whole (S-55)")
    body = {"quality": quality, "muxrate_bps": int(mbps * 1_000_000), "channels": wanted,
            "only": bool(params.get("only", False)),
            **({"length": "whole"} if params.get("length") == "whole" else {})}  # fmt: skip
    return Step(sender, f"{PREFIX}/sender/prepare", body, timeout_s=10.0)


def prepare_progress(p: IptvPlan, module_data: dict[str, Any]) -> dict[str, Any]:
    """The sender's ``prepare`` (02 5) for this plan's videos -> done / error / progress
    for the status box (02 2-1 ``preparing``)."""
    key = {(ch, p.quality, p.muxrate_bps // 1_000_000) for ch in p.on_air}
    jobs = [
        j
        for j in (module_data.get("prepare") or {}).get("channels") or []
        if (j.get("ch"), j.get("quality"), j.get("mbps")) in key
    ]
    failed = [j for j in jobs if j.get("state") in ("failed", "cancelled")]
    if failed:
        j = failed[0]
        why = j.get("error") or "cancelled on the sender"
        return {"done": False, "error": f"CH{j.get('ch')}: {why}"}
    waiting = [j for j in jobs if j.get("state") in ("queued", "making", "checking")]
    if not waiting:
        return {"done": True, "error": None}
    percents = [100.0 if j.get("state") == "ready" else float(j.get("percent") or 0) for j in jobs]
    percent = round(sum(percents) / len(percents), 1)
    now = next((j for j in waiting if j.get("state") in ("making", "checking")), waiting[0])
    left = now.get("left_s")
    text = f"CH{now.get('ch')} {p.quality}"
    if now.get("state") == "making":
        text += f" {float(now.get('percent') or 0):.0f}%"
        if isinstance(left, int):
            text += f" \u00b7 about {max(1, round(left / 60))} min left"
    elif now.get("state") == "checking":
        text += " checking"
    else:
        text += " waiting"
    if len(waiting) > 1:
        text += f" (+{len(waiting) - 1} more)"
    return {"done": False, "error": None, "percent": percent, "left_s": left, "text": text}


def measuring(module_data: dict[str, Any]) -> bool:
    """Heartbeat says every receiver on the node finished warm-up."""
    recv = [*(module_data.get("receivers") or []), *(module_data.get("tablets") or [])]
    return bool(recv) and all(r.get("measuring") for r in recv)


# ---------------------------------------------------------------- channel change (03 F19)


def current_channels(module_data: dict[str, Any]) -> list[int]:
    """The channels the node's receivers get now (heartbeat ``ch``, 02 5)."""
    return [
        int(r["ch"]) for r in module_data.get("receivers") or [] if isinstance(r.get("ch"), int)
    ]


def auto_channel(p: IptvPlan) -> dict[str, float] | None:
    """S-21 / S-22 for the Controller's timer; None = Manual."""
    if p.zap_every_s is None:
        return None
    return {"every_s": p.zap_every_s, "stagger_s": p.zap_stagger_s}


def next_channel(p: IptvPlan, ch: int) -> int:
    """Automatic order CH1 -> CH2 -> CH3 -> CH1 over the channels on air."""
    if ch not in p.on_air:
        return p.on_air[0]
    return p.on_air[(p.on_air.index(ch) + 1) % len(p.on_air)]


def channel_step(
    p: IptvPlan, device: str, ch: int, now: dict[str, list[int]], mode: str = "manual"
) -> Step | None:
    """The node command that moves ``device`` to ``ch`` (02 4 ``receiver/channel``);
    None when it is on ``ch`` already. ``now``: every receiver device's channels now.
    Join method (RFC 3918 6.1/6.2): B when another receiver watches ``ch``, else A."""
    if all(d != device for d, _ in p.receivers):
        raise ZlError("RUN-014", f"{device} is not a receiver of this test")
    if ch not in p.on_air:
        raise ZlError(
            "RUN-014", f"CH{ch} is not on air in this test",
            "The sender sends only the channels the receivers started on (20 GAP-91).",
            "Put a receiver on it at Start, or turn on Auto channel change (S-21).",
        )  # fmt: skip
    mine = now.get(device) or []
    if len(mine) != 1:
        raise ZlError("RUN-012", f"{device} does not receive one channel now",
                      detail=f"channels now: {mine}")  # fmt: skip
    if mine[0] == ch:
        return None
    watched = any(ch in chs for d, chs in now.items() if d != device)
    params: dict[str, Any] = {
        "device": device,
        "from_ch": mine[0],
        "ch": ch,
        "group": p.groups[ch],
        "port": p.port,
        "join_method": "B" if watched else "A",
        "success_timeout_s": p.success_timeout_s,
        "mode": mode,
    }
    if p.primed:
        params["prime"] = _receiver_prime_params(p, ch)
    return Step(device, f"{PREFIX}/receiver/channel", params, timeout_s=15.0)


def channel_change(p: IptvPlan, summaries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """02 10 ``channel_change``: the run's sum of the 5-minute ``channel`` values; None
    when no channel was changed."""
    rows = [x.get("channel") or {} for x in summaries]
    changes = sum(int(r.get("changes") or 0) for r in rows)
    if not changes:
        return None
    zap = [r.get("zap_ms") or {} for r in rows]
    weighted = [
        (float(z["total_avg"]), int(r.get("changes") or 0))
        for z, r in zip(zap, rows, strict=True)
        if z.get("total_avg") is not None and r.get("changes")
    ]
    n = sum(w for _, w in weighted)
    maxes = [int(z["total_max"]) for z in zap if z.get("total_max") is not None]
    return {
        "changes": changes,
        "success": sum(int(r.get("success") or 0) for r in rows),
        "success_timeout_s": p.success_timeout_s,
        "total_ms_avg": round(sum(a * w for a, w in weighted) / n, 1) if n else None,
        "total_ms_max": max(maxes) if maxes else None,
        "join_failures": sum(int(r.get("join_failures") or 0) for r in rows),
        "leave_not_processed": sum(int(r.get("leave_not_processed") or 0) for r in rows),
    }


# ---------------------------------------------------------------- judgement


# ---------------------------------------------------------------- trouble windows (05 5.3)

# purple windows (SND-016 comes every second; SND-023: the sender's WAN address was gone)
# (SND-026: a shaper on the sender's own card, 2026-10-31)
SENDER_TROUBLE = ("ZL-SND-005", "ZL-SND-016", "ZL-SND-023", "ZL-SND-025", "ZL-SND-026")


def on_test_nic(p: IptvPlan, device: str, data: dict[str, Any]) -> bool:
    """09 NET-015: is this link event about the NIC the test uses on that PC? A Wi-Fi
    receiver's NIC is the wireless one; a wired one is told by its address (the event
    carries it before and after, a down link has none). A PC the plan gives no NIC
    for counts as ours."""
    after, before = data.get("after") or {}, data.get("before") or {}
    ip = p.nics.get(device)
    if ip == "wifi":
        return "wifi" in after or "wifi" in before
    return not ip or ip in (after.get("ip"), before.get("ip"))


def trouble_of(
    p: IptvPlan, device: str, code: str, data: dict[str, Any]
) -> tuple[str, bool] | None:
    """Which trouble window an event opens (12 2 ``trouble_of``): the sender's own
    trouble or test NIC link loss -> ``sender``; the sender slow -> ``sender_slow``; a
    wired receiver's receiving NIC down -> ``receiver_link``. A Wi-Fi receiver losing
    its AP stays under the normal rules (the AP may be at fault, 2026-09-26 decision).
    The second value: the window stays open until an event clears the code (a dead tsp,
    a link down); otherwise it is the event's own second (repeated every second)."""
    receivers = {d for d, _ in p.receivers}
    if code == "ZL-RCV-006" and device in receivers:
        return "screen", True  # until the Desktop says the screen is back
    if code == "ZL-SYS-003" and (device == p.sender or device in receivers):
        return "busy", True  # until "PC no longer busy" clears it
    if device == p.sender and code in SENDER_TROUBLE:
        return "sender", code == "ZL-SND-005"
    if device == p.sender and code == "ZL-SND-017":
        return "sender_slow", False
    if code != "ZL-NET-015" or not on_test_nic(p, device, data):
        return None
    after, ip = data.get("after") or {}, p.nics.get(device)
    until_up = data.get("kind") != "flap"  # a flap is down and up in one reading
    if device == p.sender and device not in receivers:
        return "sender", until_up
    if device in receivers and "wifi" not in after and ip != "wifi":
        return "receiver_link", until_up
    return None


def second_records(p: IptvPlan, device: str, module_data: dict[str, Any]
                   ) -> list[tuple[tuple[str, int], dict[str, Any]]]:  # fmt: skip
    """This device's receivers' last 1-second record (02 5 ``last_1s``), keyed like the
    5-minute summaries (device, ch), for splitting a cell by trouble windows."""
    out = []
    for r in [*(module_data.get("receivers") or []), *(module_data.get("tablets") or [])]:
        last = r.get("last_1s")
        if isinstance(last, dict) and isinstance(last.get("e"), int):
            out.append(
                ((str(r.get("id") or device), int(last.get("ch") or r.get("ch") or 0)), last)
            )
    return out


def second_evidence(rec: dict[str, Any]) -> list[str]:
    """What the receiving PC itself counted in one troubled second, in words (GAP-148/149,
    from the RealTest reference's host_path_notes / air_side_notes): evidence attached to
    a trouble stretch, never a judge — a counter must not fail a second the rules passed.
    Only unambiguous readings become a line; an absent counter says nothing (not 0)."""
    out = []
    nd, ne, ub = rec.get(k.NIC_DROPPED), rec.get(k.NIC_ERRORS), rec.get(k.UDP_RCVBUF_ERRORS)
    if isinstance(nd, int) and nd > 0:
        out.append(
            f"this PC's network card dropped {nd} packet(s): they died here, "
            "not on the air or in the router"
        )
    if isinstance(ne, int) and ne > 0:
        out.append(f"this PC's network card counted {ne} receive error(s)")
    if isinstance(ub, int) and ub > 0:
        out.append(f"this PC's UDP receive buffers overflowed {ub} time(s) (whole PC)")
    out += _player_side(rec)
    scans = rec.get(k.WIFI_SCANS)
    if isinstance(scans, int) and scans > 0:  # 2026-10-24: a tablet's own timer scan
        out.append(
            "this tablet scanned for Wi-Fi in this second: its radio left the channel, "
            "so the loss is the tablet's, not the router's (TAB-018)"
        )
    fm, fu = rec.get(k.M2U_MULTICAST), rec.get(k.M2U_UNICAST)
    if (
        isinstance(fm, int)
        and isinstance(fu, int)
        and fm == 0
        and fu == 0
        and rec.get(k.RECEIVED) == 0
    ):
        out.append(
            "no stream frame reached this PC's card: the packets were lost before "
            "this PC (on the air, in the router or at the sender)"
        )
    return out


def _player_side(rec: dict[str, Any]) -> list[str]:
    """2026-10-24 user ("누가 드랍인거야 … 공유기인지, 수신기 인지 알수가 없네"): a second
    whose picture had trouble while every packet came is the receiver's player, said so,
    with what the player itself says it dropped (decoder late or the picture's output)."""
    pb = rec.get(k.PLAYBACK)
    drops = int(rec.get(k.PLAY_DROPPED) or 0)
    trouble = drops > 0 or (pb is not None and pb not in ("ok", "buffering", "none"))
    counts = (rec.get(k.LOST), rec.get(k.CC_ERRORS), rec.get(k.P1_ERRORS))
    clean = all(int(x or 0) == 0 for x in counts) and int(rec.get(k.RECEIVED) or 0) > 0
    if not trouble or not clean:
        return []
    line = ("every packet came (0 lost, no CC or P1 error): the picture trouble is in this "
            "receiver's player, not the router")  # fmt: skip
    dec = rec.get(k.PLAY_DECODER_DROPPED)
    if isinstance(dec, int) and drops > 0:
        shown = drops - dec
        line += (f" ({dec} dropped by the decoder: too slow to decode in time"
                 if dec else " (none by the decoder") + (
            f", {shown} at the picture's output: shown late)" if shown else ")")  # fmt: skip
    return [line]


def stream_form(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    """How the stream reaches the receiving card (GAP-149, the reference's
    stream_frame_form): real multicast frames, or unicast frames to this PC (the AP or
    router converts multicast to unicast, M2U). None when the card does not report it."""
    fm = sum(int(r[k.M2U_MULTICAST]) for r in records if isinstance(r.get(k.M2U_MULTICAST), int))
    fu = sum(int(r[k.M2U_UNICAST]) for r in records if isinstance(r.get(k.M2U_UNICAST), int))
    seen = sum(1 for r in records if isinstance(r.get(k.M2U_MULTICAST), int))
    if not seen or fm + fu == 0:
        return None
    form = (
        "unicast"
        if fu >= STREAM_SHARE * (fm + fu)
        else "multicast"
        if fm >= STREAM_SHARE * (fm + fu)
        else "mixed"
    )
    return {"form": form, "multicast_frames": fm, "unicast_frames": fu, "seconds": seen}


STREAM_SHARE = 0.5  # the frame kind carrying at least half the stream names its form


Seconds = dict[int, tuple[float, dict[str, Any]]]  # elapsed second -> (time, 1 s record)


Windows = dict[str, list[tuple[float, float]]]  # kind -> [(from, to)] in Controller time


def _whole_bursts(kinds: dict[int, str | None], seconds: dict[int, Any]) -> None:
    """A run of trouble seconds (loss, P1, or no record) touching a sender or receiving
    link window is that trouble's, all of it (2026-11-01 R20261031-223311: an 11 s netem
    on the sender's card was the sender's for 11 s, but 72-143 lost packets of the same
    burst fell just past the window and kept the box red; user: "지금은 라우터 문제가
    아닌데"). One burst has one cause; a burst not touching a window stays as it is."""

    def bad(e: int) -> bool:
        rec = seconds[e][1] if e in seconds else None
        return rec is None or bool(int(rec.get("lost") or 0) or int(rec.get("p1") or 0))

    order = sorted(kinds)
    run: list[int] = []
    for e in [*order, None]:
        if e is not None and bad(e) and (not run or e == run[-1] + 1):
            run.append(e)
            continue
        owners = {kinds[x] for x in run} & {"sender", "receiver_link"}
        if len(owners) == 1:
            owner = owners.pop()
            for x in run:
                if kinds[x] is None:
                    kinds[x] = owner
        run = [e] if e is not None and bad(e) else []


def cell_trouble(
    summary: dict[str, Any], seconds: Seconds, windows: Windows
) -> dict[str, Any] | None:
    """05 5.3 "문제 구간으로 나누기" for one cell: what fell inside the trouble windows
    (Controller times), from the receiver's 1-second records. ``None`` when no window
    touches the cell. Seconds the heartbeat missed take their time from a neighbour;
    their loss (summary total - known seconds) goes to a window only if every missed
    second is inside one (else it stays with the network)."""
    start = int(summary.get("start_elapsed_s") or 0)
    dur = int(summary.get("duration_s") or 0)
    known = sorted(e for e in seconds if start <= e < start + dur)
    if dur <= 0 or not windows or not seconds:
        return None
    anchors = sorted(seconds)

    def time_of(e: int) -> float:
        near = min(anchors, key=lambda k: abs(k - e))
        return seconds[near][0] + (e - near)

    def kind_at(t: float) -> str | None:
        for kind in ("sender", "receiver_link", "sender_slow", "busy", "screen"):
            if any(t0 <= t <= t1 for t0, t1 in windows.get(kind, [])):
                return kind
        return None

    out: dict[str, Any] = {
        "sender_s": 0, "sender_lost": 0, "sender_p1": 0, "link_s": 0, "link_lost": 0,
        "link_p1": 0, "slow_s": 0, "busy_s": 0, "screen_s": 0, "jitter_outside_ms": None,
        "unknown_lost": 0,
        "unknown_in": None,
    }  # fmt: skip
    jit_out: list[float] = []
    jit_all: list[float] = []
    lost_known = 0
    missing_kinds: set[str | None] = set()
    kinds = {e: kind_at(time_of(e)) for e in range(start, start + dur)}
    _whole_bursts(kinds, seconds)
    for e in range(start, start + dur):
        kind = kinds[e]
        rec = seconds[e][1] if e in seconds else None
        if rec is None:
            missing_kinds.add(kind if kind in ("sender", "receiver_link") else None)
        lost = int((rec or {}).get("lost") or 0)
        p1 = int((rec or {}).get("p1") or 0)
        lost_known += lost
        if rec is not None and isinstance(rec.get("jit"), int | float):
            jit_all.append(float(rec["jit"]))
        if kind == "sender":
            out["sender_s"] += 1
            out["sender_lost"] += lost
            out["sender_p1"] += p1
        elif kind == "receiver_link":
            out["link_s"] += 1
            out["link_lost"] += lost
            out["link_p1"] += p1
        else:
            if kind == "sender_slow":
                out["slow_s"] += 1
            elif kind == "busy":
                out["busy_s"] += 1
            elif kind == "screen":
                out["screen_s"] += 1
            if rec is not None and isinstance(rec.get("jit"), int | float):
                jit_out.append(float(rec["jit"]))
    if not any(out[k] for k in ("sender_s", "link_s", "slow_s", "busy_s", "screen_s")):
        return None
    total = int((summary.get("loss") or {}).get("lost") or 0)
    unknown = max(0, total - lost_known)
    inside = missing_kinds - {None}
    out["unknown_lost"] = unknown
    if unknown and missing_kinds and None not in missing_kinds and len(inside) == 1:
        (where,) = inside
        out["unknown_in"] = "sender" if where == "sender" else "link"
        out["sender_lost" if where == "sender" else "link_lost"] += unknown
    if known and (out["sender_s"] or out["link_s"]):
        outside = max(jit_out) if jit_out else 0.0
        summary_max = float((summary.get("jitter") or {}).get("max_ms") or 0.0)
        if summary_max > max(jit_all, default=0.0) and None in missing_kinds:
            outside = max(outside, summary_max)  # the peak was in a missed second outside
        out["jitter_outside_ms"] = round(outside, 3)
    return out


def _pdv(jit: dict[str, Any]) -> float | None:
    """The cell's delay variation: the largest of its 10-second windows (no clock slews),
    else (older summaries) its whole 5 minutes."""
    if isinstance(jit.get("pdv_window_ms"), int | float):
        return float(jit["pdv_window_ms"])
    lo, hi = jit.get("delay_low_ms"), jit.get("delay_high_ms")
    return (hi - lo) if lo is not None and hi is not None else None


def cell_color(summary: dict[str, Any], jitter_warn_ms: float) -> tuple[Color, tuple[str, ...]]:
    """One 5-minute summary piece (02 6) -> color. Minimum packets only for full cells
    (05 2.1, decided 2026-09-24); partial cells use the 100 kbps rule. ``trouble`` (the
    Controller's split, 05 5.3) moves what fell inside trouble windows."""
    loss = summary.get("loss") or {}
    jit = summary.get("jitter") or {}
    tr = summary.get("trouble") or {}
    c = CellInput(
        has_data=summary.get("duration_s", 0) > 0,
        full_cell=not summary.get("partial", True),
        received=int(loss.get("received") or 0),
        bitrate_avg_bps=float((summary.get("bitrate_bps") or {}).get("avg") or 0.0),
        lost=int(loss.get("lost") or 0),
        host_drops=int((summary.get("receiver") or {}).get("host_drops") or 0),
        scan_lost=int((summary.get("receiver") or {}).get("scan_lost") or 0),
        jitter_max_ms=float(jit.get("max_ms") or 0.0),
        pdv_pp_ms=_pdv(jit), wireless=bool(summary.get("wifi")),
        p1_errors=int((summary.get("ts") or {}).get("p1_errors") or 0),
        sender_s=int(tr.get("sender_s") or 0), sender_lost=int(tr.get("sender_lost") or 0),
        sender_p1=int(tr.get("sender_p1") or 0), link_s=int(tr.get("link_s") or 0),
        link_lost=int(tr.get("link_lost") or 0), link_p1=int(tr.get("link_p1") or 0),
        slow_s=int(tr.get("slow_s") or 0), busy_s=int(tr.get("busy_s") or 0),
        screen_s=int(tr.get("screen_s") or 0),
        jitter_outside_ms=tr.get("jitter_outside_ms"),
        join_failures=int((summary.get("channel") or {}).get("join_failures") or 0),
        leave_not_processed=int((summary.get("channel") or {}).get("leave_not_processed") or 0),
        playback_issue_with_clean_packets=_playback_issue(summary),
    )  # fmt: skip
    r = judge_cell(c, jitter_warn_ms)
    return r.color, r.reasons


def _playback_issue(summary: dict[str, Any]) -> bool:
    """05 4.6 (GAP-152): the player froze, dropped, decoded damaged frames or was
    restarted while the packets were clean (no loss, no P1 error) — a receiver-side
    warning, never the router's fault."""
    pb = summary.get("playback") or {}
    clean = not int((summary.get("loss") or {}).get("lost") or 0) \
        and not int((summary.get("ts") or {}).get("p1_errors") or 0)  # fmt: skip
    issues = ("freezes", "frame_drops", "decoder_errors", "player_restarts")
    return clean and any(int(pb.get(x) or 0) > 0 for x in issues)


def receiver_minimum_met(summaries: list[dict[str, Any]]) -> tuple[bool, str]:
    """Whole-run minimum per receiver: 1,000 packets and 100 kbps (05 2.1)."""
    received = sum(int((s.get("loss") or {}).get("received") or 0) for s in summaries)
    seconds = sum(int(s.get("duration_s") or 0) for s in summaries)
    bits = sum(
        float((s.get("bitrate_bps") or {}).get("avg") or 0) * int(s.get("duration_s") or 0)
        for s in summaries
    )
    avg = bits / seconds if seconds else 0.0
    if received < MIN_PACKETS or avg < MIN_BITRATE_BPS:
        return False, f"too little data: {received} packets, {avg / 1000:.0f} kbps average"
    return True, ""


def device_totals(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-receiver totals for summary.json (02 10): the Controller does not read IPTV
    fields itself (layering rule)."""
    jit = [float((s.get("jitter") or {}).get("max_ms") or 0) for s in summaries]
    return {
        "loss_total": sum(int((s.get("loss") or {}).get("lost") or 0) for s in summaries),
        "jitter_max_ms": max(jit, default=None),
    }


def cell_values(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """One 5-minute box's values for the screens and the report, in the order shown, as
    the reference board's five-minute row (2026-10-09 user: "the reference board shows
    each 5 minutes' max jitter and loss"; RealTest iptv_quality_report):
    loss count and rate, MLR (RFC 4445: lost per measured second), jitter, bitrate avg and
    min, packets, and the sequence counters R/D/L/G/E. A value not measured is None, never
    0. The Controller shows them without knowing the fields (layering rule)."""
    loss, jit = summary.get("loss") or {}, summary.get("jitter") or {}
    br = summary.get("bitrate_bps") or {}
    ts = summary.get("ts") or {}
    lost, expected = loss.get("lost"), loss.get("expected")
    secs = float(summary.get("duration_s") or 0)

    def mbps(v: Any) -> float | None:
        return None if v is None else round(float(v) / 1e6, 2)

    def item(key: str, label: str, unit: str, value: Any, bad: bool = False) -> dict[str, Any]:
        return {"key": key, "label": label, "unit": unit, "value": value, "bad": bad}

    return [
        item("lost", "Lost", "", None if lost is None else int(lost), True),
        item(
            "loss_pct",
            "Loss",
            "%",
            None if lost is None or not expected else round(100 * int(lost) / int(expected), 4),
            True,
        ),
        item(
            "mlr",
            "MLR",
            "pkt/s",
            None if lost is None or secs <= 0 else round(int(lost) / secs, 4),
            True,
        ),
        item("jitter_max", "Jitter max", "ms", jit.get("max_ms")),
        item("jitter_avg", "Jitter avg", "ms", jit.get("avg_ms")),
        item("bitrate_avg", "Bitrate avg", "Mbps", mbps(br.get("avg"))),
        item("bitrate_min", "Bitrate min", "Mbps", mbps(br.get("min"))),
        item("received", "Packets", "", loss.get("received")),
        # sequence counters, as the reference board's "Seq R/D/L/G/E": reordered,
        # duplicates, late, gaps (loss events), TS continuity (CC) errors
        item("reordered", "Reordered", "", loss.get("reordered_recovered")),
        item("duplicates", "Duplicates", "", loss.get("duplicates")),
        item("late", "Late", "", loss.get("late_discarded"), True),
        item("gaps", "Gaps", "", loss.get("loss_events"), True),
        item("cc_errors", "CC errors", "", ts.get("cc_err")),
    ]


PLAYBACK_ISSUES = ("freezes", "frame_drops", "audio_underruns", "player_restarts")


def baseline_of(plan: IptvPlan, device: str, summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """05 4.6.3: the receiver and player a baseline proves, its loss and playback issues
    (5-min ``playback`` counts; None when playback was off)."""
    player = str(plan.playback["player"]) if plan.playback is not None else "none"
    issues: int | None = None
    if plan.playback is not None:
        issues = sum(
            int((s.get("playback") or {}).get(k) or 0) for s in summaries for k in PLAYBACK_ISSUES
        )
    loss = sum(int((s.get("loss") or {}).get("lost") or 0) for s in summaries)
    return {"key": f"{device}|{player}", "player": player, "loss": loss,
            "playback_issues": issues}  # fmt: skip


def link_need(plan: IptvPlan, device: str) -> dict[str, Any]:
    """What the device's test NIC must carry (05 1.8, decided 2026-09-24): the sender's
    WAN link all channels, a wired receiver's LAN link its own channels, the larger when
    one PC does both (full duplex)."""
    copies = len(plan.flows) if plan.primed else 1  # primed: a copy per receiver flow
    if plan.primed:
        send = len(plan.receivers) * copies if device == plan.sender else 0
    else:
        send = len(plan.on_air) if device == plan.sender else 0
    recv = sum(1 for d, _ in plan.receivers if d == device) * copies
    if not send and not recv:
        return {"bps": 0, "label": ""}
    label = "WAN" if send else "LAN"
    return {"bps": max(send, recv) * plan.muxrate_bps, "label": label}


# ---------------------------------------------------------------- precheck (03 F17)

# Disk per channel and second of test: 1-second record lines are ~150 bytes (measured
# 2026-09-25); x7 for event lines, player and tsp logs. The sender keeps a rate log.
RECEIVER_BYTES_PER_S = 1024
SENDER_BYTES_PER_S = 256
PROBE_SEND_S = 2  # per channel; channels are sent one after another


def required_tools(plan: IptvPlan, device: str) -> list[str]:
    """F17 step 2: the tools this plan needs on this device (subset of module.json)."""
    need: set[str] = set()
    channels = set(plan.on_air)
    if device == plan.sender:
        need.add("tsp")
        if any(ch not in plan.files for ch in channels):
            need.add("ffmpeg")  # the video is generated (iptv 1-1-2)
    if plan.playback is not None and any(d == device for d, _ in plan.receivers):
        need.add(str(plan.playback["player"]).lower())
        need.add("ffmpeg")  # the PC speed check makes and decodes a sample (RCV-015)
    if any(host == device for host, _ in plan.tablets.values()):
        need.add("adb")  # the tablets hang on this PC by USB (01-overview D3)
    return sorted(need)


def disk_need_mb(plan: IptvPlan, device: str) -> float:
    """F17 step 4: disk this test writes on the device (videos are checked by prepare)."""
    rx = sum(1 for d, _ in plan.receivers if plan.node_of(d) == device) * RECEIVER_BYTES_PER_S
    tx = len(plan.on_air) * SENDER_BYTES_PER_S if device == plan.sender else 0
    return (rx + tx) * plan.duration_s / (1024 * 1024)


def device_checks(p: IptvPlan, light: bool) -> list[Step]:
    """F17 step 4 (05 4.2-1): the speed of each PC receiver with playback on (RCV-015).
    ``light`` = the idle check: only what the node already measured."""
    devices = sorted({d for d, _ in p.receivers if d not in p.tablets})  # PCs only
    if p.playback is None or not devices:
        return []
    desktop = [  # 03 F20 / RCV-010: the player needs Agent Desktop on that PC
        Step(device, f"{PREFIX}/receiver/desktop", {"wait_s": 0.0 if light else 5.0},
             timeout_s=15.0)
        for device in devices
    ]  # fmt: skip
    return desktop + [
        Step(
            device,
            f"{PREFIX}/receiver/perfcheck",
            {"quality": p.quality, "muxrate_bps": p.muxrate_bps, "measure": not light},
            timeout_s=90.0,
        )
        for device in devices
    ]


def probe_steps(p: IptvPlan) -> list[list[Step]]:
    """F17 step 5, three stages: receivers listen (reply ``probe_id``) -> the sender sends
    a short null stream per channel -> each receiver's count (the Controller puts the
    listen reply's ``probe_id`` into the matching result step)."""
    if p.primed:
        return _primed_probe_steps(p)
    # PC receivers only: a tablet's Wi-Fi is not the PC's card (its warm-up checks it)
    pcs = [(d, ch) for d, ch in p.receivers if d not in p.tablets]
    if not pcs:
        return [[], [], []]
    channels = sorted({ch for _, ch in pcs})
    listen_s = PROBE_SEND_S * len(channels) + 3  # until the last channel is sent
    listen = [
        Step(
            device,
            f"{PREFIX}/receiver/probe",
            {
                "ch": ch,
                "group": p.groups[ch],
                "port": p.port,
                "seconds": listen_s,
                "m2u": True,  # the node looks only on a Wi-Fi NIC (wired: snooping only)
                **({"interface_ip": p.nics[device]} if device in p.nics else {}),
            },
        )
        for device, ch in pcs
    ]
    send = [
        Step(
            p.sender,
            f"{PREFIX}/sender/probe",
            {
                "ch": ch,
                "dest_ip": p.groups[ch],
                "port": p.port,
                "seconds": PROBE_SEND_S,
                "ttl": p.ttl,
                **({"local_address": p.nics[p.sender]} if p.sender in p.nics else {}),
            },
            timeout_s=PROBE_SEND_S + 10.0,
        )
        for ch in channels
    ]
    result = [
        Step(s.device, f"{PREFIX}/receiver/probe/result", {"ch": s.params["ch"]}, listen_s + 5.0)
        for s in listen
    ]
    return [listen, send, result]


def _primed_probe_steps(p: IptvPlan) -> list[list[Step]]:
    """The same three stages, primed (iptv 4.5): the receiver primes from its test port
    every 0.5 s while listening; the sender waits for it, then sends back to it."""
    channels = sorted({ch for _, ch in p.receivers})
    wait_s = 3
    listen_s = (PROBE_SEND_S + wait_s) * len(channels) + 3
    listen = []
    for device, ch in p.receivers:
        pp, q, r = prime_ports(p.port, ch, 0)
        listen.append(Step(device, f"{PREFIX}/receiver/probe", {
            "ch": ch, "group": p.groups[ch], "port": r, "seconds": listen_s,
            "prime": {"sender_ip": p.sender_ip, "p": pp, "q": q},
            **({"interface_ip": p.nics[device]} if device in p.nics else {}),
        }))  # fmt: skip
    send = [
        Step(
            p.sender,
            f"{PREFIX}/sender/probe",
            {
                "ch": ch,
                "seconds": PROBE_SEND_S,
                "ttl": p.ttl,
                "prime_wait_s": wait_s,
                "prime": dict(zip(("p", "q"), prime_ports(p.port, ch, 0)[:2], strict=True))
                | {"flow": 0},
                **({"local_address": p.nics[p.sender]} if p.sender in p.nics else {}),
            },
            timeout_s=PROBE_SEND_S + wait_s + 10.0,
        )
        for ch in channels
    ]
    result = [
        Step(s.device, f"{PREFIX}/receiver/probe/result", {"ch": s.params["ch"]}, listen_s + 5.0)
        for s in listen
    ]
    return [listen, send, result]


def report_source(p: IptvPlan, module_data: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """02 10 ``source`` per channel (prepared file name / S-48 file / ``generated``) and
    ``source_fallback``: channels the sender had to send as generated (SND-001)."""
    source = {}
    for ch in p.on_air:
        name = p.files.get(ch) or p.sources.get(ch)
        source[str(ch)] = display_name(str(name)) if name else "generated"
    sent = (module_data.get(p.sender) or {}).get("sender") or {}
    rows = [c for c in sent.get("channels") or [] if isinstance(c, dict)]
    fallback = sorted({int(c["ch"]) for c in rows if c.get("source") == "generated_fallback"})
    return {"source": source, "source_fallback": fallback}


def probe_fallback(p: IptvPlan) -> IptvPlan | None:
    """S-18 Auto (iptv 4.5): after a multicast test send reached nobody behind a NAT,
    the plan to try next — primed. None when there is nothing else to try."""
    if p.prime_setting != "Auto" or p.primed or p.sender_ip is None:
        return None
    return replace(p, primed=True)


def path_note(p: IptvPlan) -> dict[str, Any]:
    """How the stream reaches the receivers (02 10 ``dut.multicast_path``)."""
    if not p.primed:
        return {"path": "igmp", "note": "multicast (router IGMP proxy)"}
    why = "S-18 On" if p.prime_setting == "On" else "no multicast came through"
    return {"path": "primed", "note": f"primed unicast ({why}): no IGMP proxy on the DUT "
            "(development/SDK build?) — not the real IPTV path"}  # fmt: skip


# ---------------------------------------------------------------- live device row

PLAYING = ("ok", "drop", "audio_gap")  # pb values while frames still come (iptv 3.1)
_WORSE = ("green", "yellow", "purple", "red")  # red > purple > yellow > green (05 5.3)


def _item(
    label: str, value: float | int | None = None, unit: str = "", key: bool = False
) -> dict[str, Any]:
    return {"label": label, "value": value, "unit": unit, "key": key}


def _second_color(last: dict[str, Any], jitter_warn_ms: float) -> tuple[str, str]:
    lost = int(last.get(k.LOST) or 0)
    hd = last.get(k.HOST_DROPS)
    if lost:
        if isinstance(hd, int) and hd >= lost:
            return "purple", f"{lost} packets lost in the last second, all dropped by the receiver"
        return "red", f"{lost} packets lost in the last second"
    jit = float(last.get(k.JITTER_MS) or 0.0)
    if jit >= jitter_warn_ms:
        return "yellow", f"Jitter {jit:g} ms is at or above the warning level {jitter_warn_ms:g} ms"
    return "green", ""


def live_row(plan: IptvPlan, module_data: dict[str, Any], memo: dict[str, Any]) -> dict[str, Any]:
    """Device row for the screens (02 2-1 ``row``, 11-ui 3): the last second at once, and
    the worst color of the open 5-minute cell held until that cell ends. ``memo`` is the
    Controller's per-device dict for this module; only this function reads it."""
    items: list[dict[str, Any]] = []
    color, why = "green", ""
    link_label = "LAN"
    chans = [c for c in (module_data.get("sender") or {}).get("channels") or []]
    running = [c for c in chans if c.get("state") == "RUNNING"]
    if chans:
        link_label = "WAN"
        mbps = sum(float(c.get("tx_bitrate_bps") or 0) for c in running) / 1e6
        items.append(_item(f"Sending {len(running)} ch ·", round(mbps, 1), "Mbps"))
    for r in module_data.get("receivers") or []:
        items.append(_item(f"CH{r.get('ch')}"))
        last = r.get("last_1s")
        if not r.get("measuring") or not isinstance(last, dict):
            items.append(_item("Warming up"))
            continue
        items.append(_item("Jitter", last.get(k.JITTER_MS), "ms", key=True))
        items.append(_item("Loss", r.get("lost_total"), key=True))
        pb = last.get(k.PLAYBACK)
        if pb is not None:  # only the player's own report says it plays (11-ui 3)
            items.append(_item("▶ Playing" if pb in PLAYING else "■ Not playing"))
        now, now_why = _second_color(last, plan.jitter_warn_ms)
        cell = (str(r.get("id")), int(last.get(k.ELAPSED) or 0) // plan.cell_s)
        held = memo.get(cell[0])
        if held is None or held[0] != cell[1] or _WORSE.index(now) > _WORSE.index(held[1]):
            held = memo[cell[0]] = (cell[1], now, now_why)
        if _WORSE.index(held[1]) > _WORSE.index(color):
            color, why = held[1], held[2]
    tabs = module_data.get("tablets") or []
    if tabs:  # 03 F20: the receiving PC of a tablet test relays only
        away = [str(t.get("id")) for t in tabs if t.get("away")]
        items.append(_item(f"Relaying {len(tabs)} tablet{'s' if len(tabs) > 1 else ''}"))
        if away:
            color, why = "red", f"{', '.join(away)}: no answer over USB"
    recv = module_data.get("receivers") or []
    ch = recv[0].get("ch") if len(recv) == 1 and recv[0].get("measuring") else None
    return {"color": color, "why": why, "link_label": link_label, "items": items,
            "channel": ch if isinstance(ch, int) else None}  # fmt: skip


def receiver_rows(plan: IptvPlan, module_data: dict[str, Any], memo: dict[str, Any]
                  ) -> dict[str, dict[str, Any]]:  # fmt: skip
    """A row for each tablet this PC relays (11-ui 3: the tablets are the device rows),
    made the same way as a PC receiver's: the open cell's worst color held."""
    rows = {}
    for t in module_data.get("tablets") or []:
        one = live_row(plan, {"receivers": [t]}, memo)
        if t.get("away"):
            one = {**one, "color": "red", "why": "No answer over USB (cable out?)"}
        wifi = wifi_text(t.get("last_1s") or {})
        if wifi:  # 2026-10-24 user: no RSSI, band, width or 11ax shown for the tablets
            one = {
                **one,
                "items": [
                    *one.get("items", []),
                    {"label": wifi, "value": None, "unit": "", "key": False},
                ],
            }
        rows[str(t.get("id"))] = {**one, "link_label": "Wi-Fi"}
    return rows


# How a receiver's own seconds say the Wi-Fi it receives on (a tablet has no card in the
# plan): drawn by the live page and the tablet's row in this order
WIFI_PARTS: list[dict[str, Any]] = [
    {"key": k.WIFI_BAND}, {"key": k.WIFI_CH, "label": "ch"},
    {"key": k.WIFI_BW, "unit": "MHz"}, {"key": k.WIFI_STD},
    {"key": k.RSSI_DBM, "unit": "dBm"}, {"key": k.WIFI_MBPS, "unit": "Mbps"},
]  # fmt: skip


def wifi_text(rec: dict[str, Any]) -> str:
    """ "Wi-Fi 5GHz · ch 149 · 80 MHz · 11ax · -31 dBm · 1201 Mbps" from one second, or ""."""
    bits = []
    for p in WIFI_PARTS:
        v = rec.get(p["key"])
        if v is None or v == "":
            continue
        bits.append(" ".join(str(x) for x in (p.get("label"), v, p.get("unit")) if x))
    return "Wi-Fi " + " · ".join(bits) if rec.get(k.RSSI_DBM) is not None else ""


# Live monitor (02 12 /live, 2026-10-08 GAP-119): what the Controller's live page shows of a
# receiver's 1-second records, by name only — the page draws these, it never names a key.
# now = card of the last second; cell = how the open 5-minute cell sums it (sum / max /
# min / avg); total = whole-test card; chart = a 10-minute line (bars for "bad" counts);
# bad = any non-zero is trouble; scale turns the value into the unit shown; receiver = the
# trouble is this receiver's own (its player), never the router's: shown amber, not red
# (2026-10-31 user: red only for what the router lost).
LIVE_COLUMNS: list[dict[str, Any]] = [
    {"key": k.JITTER_MS, "label": "Jitter", "unit": "ms", "now": True, "cell": "max",
     "chart": True, "digits": 1},
    {"key": k.BITRATE_BPS, "label": "Bitrate", "unit": "Mbps", "scale": 1e-6, "now": True,
     "cell": "min", "chart": True, "digits": 2},
    {"key": k.RECEIVED, "label": "Packets/s", "unit": "", "now": True, "cell": "min",
     "bad_zero": True},  # a second with no packet is trouble (GAP-138)
    {"key": k.LOST, "label": "Lost", "unit": "", "now": True, "cell": "sum", "total": True,
     "bad": True, "chart": True},
    {"key": k.CC_ERRORS, "label": "CC errors", "unit": "", "cell": "sum", "total": True,
     "warn": True},
    {"key": k.P1_ERRORS, "label": "TR 101 290 P1", "unit": "", "cell": "sum", "total": True,
     "warn": True},
    {"key": k.DUPLICATES, "label": "Duplicates", "unit": "", "cell": "sum", "total": True},
    {"key": k.REORDERED, "label": "Reordered", "unit": "", "cell": "sum", "total": True},
    {"key": k.PLAYBACK, "label": "Playback", "unit": "", "now": True, "text": True,
     "ok": ["ok"], "receiver": True},
    {"key": k.PLAY_DROPPED, "label": "Dropped frames", "unit": "", "cell": "sum",
     "total": True, "bad": True, "receiver": True},
    {"key": k.RSSI_DBM, "label": "Wi-Fi RSSI", "unit": "dBm", "now": True, "cell": "min"},
]  # fmt: skip


def live_view(p: IptvPlan) -> dict[str, Any]:
    """live monitor (02 12 /live): the columns above, and the line drawn on the jitter chart
    (this test's jitter warning level, S-23)."""
    return {"columns": LIVE_COLUMNS, "cell_s": p.cell_s, "wifi": WIFI_PARTS,
            "lines": {k.JITTER_MS: [p.jitter_warn_ms]}}  # fmt: skip


class IptvRun:
    """What ``run_entry`` returns: the Controller calls only these."""

    validate = staticmethod(validate)
    start_steps = staticmethod(start_steps)
    stop_steps = staticmethod(stop_steps)
    prepare_steps = staticmethod(prepare_steps)
    prepare_ahead = staticmethod(prepare_ahead)
    prepare_progress = staticmethod(prepare_progress)
    live_view = staticmethod(live_view)
    prepare_state = staticmethod(
        lambda data: data.get("prepare") or {"busy": False, "channels": []}
    )
    measuring = staticmethod(measuring)
    cell_color = staticmethod(cell_color)
    trouble_of = staticmethod(trouble_of)
    on_test_nic = staticmethod(on_test_nic)
    second_records = staticmethod(second_records)
    cell_trouble = staticmethod(cell_trouble)
    receiver_minimum_met = staticmethod(receiver_minimum_met)
    device_totals = staticmethod(device_totals)
    second_evidence = staticmethod(second_evidence)
    stream_form = staticmethod(stream_form)
    # 2026-10-09: one second at -1.0 % made a box yellow ("sender slow for 3 s") twice in
    # a 40-minute run; as the RealTest reference's bitrate floor, 3 seconds in a row
    trouble_min_run: ClassVar[dict[str, int]] = {"sender_slow": 3}
    cell_values = staticmethod(cell_values)
    live_row = staticmethod(live_row)
    link_need = staticmethod(link_need)
    required_tools = staticmethod(required_tools)
    disk_need_mb = staticmethod(disk_need_mb)
    probe_steps = staticmethod(probe_steps)
    device_checks = staticmethod(device_checks)
    probe_fallback = staticmethod(probe_fallback)
    report_source = staticmethod(report_source)
    path_note = staticmethod(path_note)
    current_channels = staticmethod(current_channels)
    auto_channel = staticmethod(auto_channel)
    next_channel = staticmethod(next_channel)
    channel_step = staticmethod(channel_step)
    channel_change = staticmethod(channel_change)
    broadcast_stop_steps = staticmethod(broadcast_stop_steps)
    same_broadcast = staticmethod(same_broadcast)
    broadcast_info = staticmethod(broadcast_info)
    always_on = staticmethod(lambda p: bool(p.always_on))
    baseline_of = staticmethod(baseline_of)
    receiver_devices = staticmethod(lambda p: sorted({d for d, _ in p.receivers}))
    node_of = staticmethod(lambda p, d: p.node_of(d))  # a tablet -> the PC it hangs on
    receiver_rows = staticmethod(receiver_rows)

    @staticmethod
    def analysis(plan: IptvPlan | None, data: dict[str, Any]) -> dict[str, Any]:
        """Cause candidates (iptv 2.4, 12 2): never changes a verdict or a color."""
        from modules.iptv.analysis import analyze

        return analyze(data)

    @staticmethod
    def self_checks() -> list[Check]:  # zeroloss verify (12 2, 20 GAP-81)
        from modules.iptv.self_checks import checks  # the synthetic streams only when asked

        return checks()

    heartbeat_key = "iptv"
    sources_path = f"{PREFIX}/sender/sources"  # S-48 list (02 2-10)


def create() -> IptvRun:
    return IptvRun()
