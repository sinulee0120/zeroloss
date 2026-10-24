"""A wired card's link speed for a test, and back as it was (docs/modules/throughput.md 1.1).

The reference (RealTest, 18) changes the card's "Speed & Duplex" one value at a time,
checks the driver read it back exactly, takes the speed the link really came up at as
the baseline, and puts the card back afterwards; a card it cannot put back stops the
test (safety). Here, on Linux, with ``ethtool``:

- **What the card can do:** ``Supported link modes`` (10/100/1000/2500/5000/10000, half
  and full). A mode the card lacks is never tried.
- **One mode at a time:** auto-negotiation stays on and the card *advertises only that
  mode* (``ethtool -s <card> advertise <mask>``). The other end (router port, or the
  other PC) negotiates normally, so there is no duplex mismatch as with a forced
  setting; a router port that cannot do the mode gives the common lower speed, which
  is the baseline then (reference: requested 2500, negotiated 1000 -> baseline 1000).
- **Read back:** the advertised modes must be exactly the one asked for; then the link
  must come up within ``LINK_WAIT_S``, and its speed/duplex are what was negotiated.
- **Back as it was:** the advertised modes and auto-negotiation before the first change
  are kept in a file (``custody``) so a node that restarts mid-test still puts the card
  back. ``restore`` reads back again; a card that does not come back is the caller's
  safety stop.

Needs CAP_NET_ADMIN (the installed node has it). Test-type neutral.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

Run = Callable[[list[str]], tuple[int, str]]
LINK_WAIT_S = 20.0
GIGABIT = 1000  # Mbps: from here one mode, no half duplex
TIMEOUT_S = 10.0

# ethtool link mode bits (include/uapi/linux/ethtool.h ETHTOOL_LINK_MODE_*_BIT)
MODE_BITS = {
    "10baseT/Half": 0, "10baseT/Full": 1, "100baseT/Half": 2, "100baseT/Full": 3,
    "1000baseT/Half": 4, "1000baseT/Full": 5, "10000baseT/Full": 12,
    "2500baseT/Full": 47, "5000baseT/Full": 48, "2500baseX/Full": 15,
    "1000baseX/Full": 41, "10000baseKR/Full": 19,
}  # fmt: skip


@dataclass(frozen=True)
class Mode:
    speed: int  # Mbps
    duplex: str  # full / half
    name: str  # ethtool's name, e.g. 1000baseT/Full

    @property
    def label(self) -> str:
        # 1000 Mbps and up is one mode (full duplex only in practice): no "full" on it
        return f"{self.speed} Mbps" if self.speed >= GIGABIT else f"{self.speed} Mbps {self.duplex}"


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout + p.stderr).strip()


def _modes_after(title: str, text: str) -> list[str]:
    """The link mode names listed under ``title:`` (they wrap onto indented lines)."""
    m = re.search(rf"^\s*{re.escape(title)}:\s*(.*?)(?=^\s*[A-Z][\w -]+:)", text + "\nEnd: x",
                  re.M | re.S)  # fmt: skip
    if not m:
        return []
    return [w for w in m.group(1).split() if "/" in w]


def mode_of(name: str) -> Mode | None:
    m = re.fullmatch(r"(\d+)base\w+/(Half|Full)", name)
    return Mode(int(m.group(1)), m.group(2).lower(), name) if m else None


@dataclass
class Caps:
    nic: str
    supported: list[Mode]  # what can be tried, slowest first, full before half
    advertised: list[str]
    speed: int | None
    duplex: str | None
    autoneg: bool | None
    link: bool | None
    # what the other end of the cable offers (2026-10-17: which side of a slow link is it)
    partner: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["supported"] = [{"speed": m.speed, "duplex": m.duplex, "name": m.name}
                          for m in self.supported]  # fmt: skip
        return d


def parse(nic: str, text: str) -> Caps:
    sup = [m for m in (mode_of(n) for n in _modes_after("Supported link modes", text)) if m]
    # one name per speed+duplex (2500baseT and 2500baseX are the same test), known bits only
    seen: dict[tuple[int, str], Mode] = {}
    for m in sup:
        # 2026-10-17 user ("1000Mbps부터는 full, half없다며 ... 그게 spec이야"): 1000 Mbps
        # and up is one mode; a card listing 1000baseT/Half gets no half case
        if m.name in MODE_BITS and not (m.speed >= GIGABIT and m.duplex == "half"):
            seen.setdefault((m.speed, m.duplex), m)
    ordered = sorted(seen.values(), key=lambda m: (m.speed, m.duplex != "full"))
    speed = re.search(r"Speed:\s*(\d+)Mb/s", text)
    duplex = re.search(r"Duplex:\s*(\w+)", text)
    auto = re.search(r"Auto-negotiation:\s*(on|off)", text)
    link = re.search(r"Link detected:\s*(yes|no)", text)
    dup = duplex.group(1).lower() if duplex else None
    return Caps(
        nic, ordered, _modes_after("Advertised link modes", text),
        int(speed.group(1)) if speed else None,
        dup if dup in ("full", "half") else None,
        (auto.group(1) == "on") if auto else None,
        (link.group(1) == "yes") if link else None,
        _modes_after("Link partner advertised link modes", text),
    )  # fmt: skip


def caps(nic: str, run: Run = _run) -> Caps | None:
    rc, out = run(["ethtool", nic])
    return parse(nic, out) if rc == 0 or "Supported link modes" in out else None


def mask(names: list[str]) -> str:
    bits = 0
    for n in names:
        if n in MODE_BITS:
            bits |= 1 << MODE_BITS[n]
    return hex(bits)


class NicSpeed:
    """One card's changes during a test, with the way back kept on disk."""

    def __init__(self, custody: Path, run: Run = _run,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.custody, self.run, self.sleep, self.clock = custody, run, sleep, clock

    def _kept(self) -> dict[str, Any]:
        try:
            return dict(json.loads(self.custody.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return {}

    def _keep(self, data: dict[str, Any]) -> None:
        self.custody.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.custody.with_name(self.custody.name + ".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.custody)

    def set_mode(self, nic: str, mode: Mode) -> dict[str, Any]:
        """Advertise only ``mode``; wait for the link. Returns what happened:
        ``readback_ok`` (exactly that mode advertised), ``link`` up, the negotiated
        ``speed``/``duplex``, and ``why`` when something did not go."""
        now = caps(nic, self.run)
        if now is None:
            return {"readback_ok": False, "why": f"ethtool cannot read {nic}"}
        kept = self._kept()
        if nic not in kept:  # the state before the first change, once
            kept[nic] = {"advertised": now.advertised, "autoneg": now.autoneg,
                         "speed": now.speed if now.link else None,
                         "partner": now.partner}  # fmt: skip
            self._keep(kept)
        # what the other end offers, as read before the first change: a card advertising
        # only 100 Mbps half does not learn the other end's gigabit (2026-10-17: "1000 Mbps
        # not offered" right after the 100 half case -- "말이 안되잖아")
        partner = list(kept[nic].get("partner") or [])
        if partner and mode.name not in partner:
            # 2026-10-17 user ("1000Mbps부터는 full, half없다며"): the other end does not
            # offer it -- trying only took the link away for LINK_WAIT_S. The card is
            # not touched; the case says why it was not run.
            return {"readback_ok": False, "link": False, "skipped": True,
                    "why": f"the other end of the cable does not offer {mode.label}"}  # fmt: skip
        rc, out = self.run(["ethtool", "-s", nic, "autoneg", "on", "advertise", mask([mode.name])])
        if rc != 0:
            return {"readback_ok": False, "why": f"ethtool -s failed: {out}"}
        return self._settle(nic, [mode.name])

    def restore(self, nic: str) -> dict[str, Any]:
        """Back to the modes advertised before the first change; ``ok`` False = the
        caller's safety stop."""
        kept = self._kept()
        before = kept.get(nic)
        if before is None:
            return {"ok": True, "why": "nothing was changed"}
        names = [n for n in before.get("advertised") or [] if n in MODE_BITS]
        argv = ["ethtool", "-s", nic, "autoneg", "on" if before.get("autoneg", True) else "off"]
        if names:
            argv += ["advertise", mask(names)]
        rc, out = self.run(argv)
        if rc != 0:
            return {"ok": False, "why": f"ethtool -s failed: {out}"}
        got = self._settle(nic, names)
        ok = bool(got.get("readback_ok")) and bool(got.get("link"))
        was = before.get("speed")
        # 2026-10-17: the same modes advertised again, and the link came back at 100 Mbps
        # (1 Gbps before); every later case measured a slower path. Negotiate again, then
        # take the link down and up (as a re-plugged cable); still slower = not back.
        for again in (["ethtool", "-r", nic], ["ip", "link", "set", nic, "down"]):
            if not ok or not was or (got.get("speed") or 0) >= was:
                break
            self.run(again)
            if again[-1] == "down":
                self.sleep(1.0)
                self.run(["ip", "link", "set", nic, "up"])
            got = self._settle(nic, names)
            ok = bool(got.get("readback_ok")) and bool(got.get("link"))
        if ok and was and (got.get("speed") or 0) < was:
            ok = False
            got["why"] = (
                f"{nic} came back at {got.get('speed')} Mbps; it was {was} Mbps before "
                "the test. Unplug and plug its cable, then press Reset All."
            )
        if ok:
            del kept[nic]
            self._keep(kept)
        return {**got, "ok": ok}

    def restore_all(self) -> dict[str, dict[str, Any]]:
        """Node start: cards a test left changed (a restart mid-test) go back."""
        return {nic: self.restore(nic) for nic in list(self._kept())}

    def _settle(self, nic: str, names: list[str]) -> dict[str, Any]:
        end = self.clock() + LINK_WAIT_S
        last: Caps | None = None
        while True:
            last = caps(nic, self.run)
            if last is not None and last.link and last.speed:
                break
            if self.clock() >= end:
                break
            self.sleep(0.5)
        if last is None:
            return {"readback_ok": False, "link": False, "why": f"ethtool cannot read {nic}"}
        readback = sorted(last.advertised) == sorted(names) if names else True
        out = {"readback_ok": readback, "advertised": last.advertised, "link": bool(last.link),
               "speed": last.speed if last.link else None,
               "duplex": last.duplex if last.link else None,
               "partner": last.partner}  # fmt: skip
        if not readback:
            out["why"] = f"the card advertises {last.advertised}, not {names}"
        elif not last.link:
            out["why"] = f"the link did not come up within {LINK_WAIT_S:g} s"
        return out
