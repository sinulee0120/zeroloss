"""Link judgement on the Controller (docs/05-standards.md 1.8, 09 NET-012/013/014/015).

Nodes only read and report their test NICs (``links`` in the heartbeat); only the
Controller knows how much a NIC must carry (module ``link_need()``, 12 2), so it
decides: below needed x 1.1 is serious (NET-013, blocks the start as NET-012), slower
than the usual speed but enough is a warning (NET-014). Down is the node's NET-015
event; here it only shows. Verdicts never change (cause rule R-19 only).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.events.bus import Event, Level, utc_now
from core.protocol.envelope import ZlError

MARGIN = 1.1  # needed rate x 1.1 (09 NET-012, decided 2026-09-24)
FIX = "Check or replace the cable and port."


def _speed(mbps: int | None) -> str:
    if mbps is None:
        return "unknown"
    return f"{mbps / 1000:g} Gbps" if mbps >= 1000 else f"{mbps} Mbps"


def _wired(links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [x for x in links if isinstance(x, dict) and not isinstance(x.get("wifi"), dict)]


def level_of(link: dict[str, Any], needed_bps: int, usual_mbps: int) -> str:
    if not link.get("up", True):
        return "down"
    mbps = link.get("link_mbps")
    if not isinstance(mbps, int) or isinstance(mbps, bool) or mbps <= 0:
        return "unknown"
    if mbps * 1_000_000 < needed_bps * MARGIN:
        return "short"
    if mbps < usual_mbps:
        return "slow"
    return "ok"


def precheck(device: str, links: list[dict[str, Any]], need: dict[str, Any]) -> list[Event]:
    """Before the start (F17 step 3): a link below the needed rate blocks (raises
    NET-012); half duplex is only a warning (returned as events)."""
    bps = int(need.get("bps") or 0)
    label = str(need.get("label") or "")
    warnings: list[Event] = []
    for x in _wired(links):
        mbps = x.get("link_mbps")
        if level_of(x, bps, 0) == "short":
            raise ZlError(
                "NET-012",
                f"{device} {label} link is {_speed(mbps)}, needs {bps * MARGIN / 1e6:.0f} Mbps",
                "The network card and the router port agreed on a speed too low for this test.",
                FIX,
                detail=f"nic={x.get('nic')} ip={x.get('ip')}",
            )
        if x.get("duplex") == "half":
            warnings.append(
                Event(
                    "ZL-NET-012",
                    "warn",
                    f"{device} {label} link is half duplex ({_speed(mbps)})",
                    device=device,
                    data={"nic": x.get("nic"), "fix": FIX},
                )
            )
    return warnings


class LinkJudge:
    """Levels per device and NIC for one run; ``update`` returns an event only when the
    level really changes (no flood when a link flaps, 11-ui 1.13)."""

    def __init__(self, needs: dict[str, dict[str, Any]], usual: dict[str, dict[str, int]]) -> None:
        self.needs = needs  # device -> link_need()
        self.usual = {d: dict(v) for d, v in usual.items()}  # device -> ip -> Mbps
        self._state: dict[tuple[str, str], dict[str, Any]] = {}
        # a card once seen as Wi-Fi is never a wired link: down, it has no Wi-Fi values
        # and looked wired; back up it left the list and its "down" stayed for 8 h
        # (2026-10-22: the T460's home Wi-Fi wlp4s0 dropped 3 s at 03:00:09)
        self._wireless: set[tuple[str, str]] = set()

    def update(self, device: str, links: list[dict[str, Any]], elapsed_s: float) -> list[Event]:
        need = self.needs.get(device) or {}
        bps, label = int(need.get("bps") or 0), str(need.get("label") or "")
        usual = self.usual.setdefault(device, {})
        events: list[Event] = []
        for x in links:
            if isinstance(x, dict) and isinstance(x.get("wifi"), dict):
                self._wireless.add((device, str(x.get("nic"))))
        wired = [x for x in _wired(links) if (device, str(x.get("nic"))) not in self._wireless]
        for key in [k for k in self._state if k in self._wireless]:
            del self._state[key]  # a Wi-Fi card after all: no stale wired level (a wired
            # card that vanished keeps its "down": a USB adapter pulled out is real)
        for x in wired:
            nic, ip = str(x.get("nic")), str(x.get("ip"))
            mbps = x.get("link_mbps")
            key = (device, nic)
            first = key not in self._state
            if first and isinstance(mbps, int) and x.get("up", True):
                usual[ip] = max(usual.get(ip, 0), mbps)  # this run's start speed counts
            level = level_of(x, bps, usual.get(ip, 0))
            old = self._state.get(key)
            if old is None or old["level"] != level:
                before = old["link_mbps"] if old else usual.get(ip)
                ev = self._event(
                    device, label, nic, old["level"] if old else None, level, before, mbps, bps
                )
                if ev is not None:
                    events.append(ev)
                self._state[key] = {
                    "nic": nic,
                    "label": label,
                    "level": level,
                    "link_mbps": mbps,
                    "duplex": x.get("duplex"),
                    "needed_mbps": round(bps * MARGIN / 1e6, 1),
                    "usual_mbps": usual.get(ip) or None,
                    "since_elapsed_s": round(elapsed_s, 1),
                    "since_utc": utc_now(),
                }
            else:
                old["link_mbps"], old["duplex"] = mbps, x.get("duplex")
        return events

    def checks(self, device: str) -> list[dict[str, Any]]:
        return [dict(v) for (d, _), v in sorted(self._state.items()) if d == device]

    @staticmethod
    def _event(
        device: str,
        label: str,
        nic: str,
        old: str | None,
        new: str,
        before: int | None,
        mbps: Any,
        bps: int,
    ) -> Event | None:
        clock = datetime.now().strftime("%H:%M:%S")
        change = f"{_speed(before)} → {_speed(mbps)}"
        what = f"{device} ↔ router {label} link changed: {change} ({clock})"
        data: dict[str, Any] = {"nic": nic, "from": before, "to": mbps, "fix": FIX}
        code = ""
        level: Level = "info"
        if new == "short":
            code, level = "ZL-NET-013", "error"
            data["why"] = (
                f"The link renegotiated. This test needs about {bps * MARGIN / 1e6:.0f} Mbps, "
                "so packets will be lost."
            )
        elif new == "slow":
            code, level = "ZL-NET-014", "warn"
            data["why"] = (
                "Still enough for this test, but slower than usual — often a cable or port problem."
            )
        elif new == "ok" and old in ("short", "slow"):
            code = "ZL-INFO"
            what = f"{device} {label} link is back to {_speed(mbps)} ({clock})"
            data.pop("fix")
            data["clears"] = ["ZL-NET-013", "ZL-NET-014"]  # 02 8
        else:
            return None
        return Event(code, level, what, device=device, data=data)
