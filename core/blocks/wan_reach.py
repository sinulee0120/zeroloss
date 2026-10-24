"""A LAN-side PC reaches the sender PC on the router WAN side, behind any router
(docs/07-network.md 6-1, 20 GAP-158).

2026-10-15 user: "every time the router changes, 10.0.0.1 has to be pointed at the new
router — impossible without the AI?", "what about testing 10 different routers?". A
module that sends from the LAN side to the sender (a receiver's prime, a throughput
client) sends to the sender's WAN address (10.0.0.x). The PC's default route is often
its own internet (home Wi-Fi), so those packets left on the wrong card; a static route
typed into the PC's profile once worked only until the router changed.

Now, at each start, for the sender's address and this PC's test card:
- the route already leaves on the test card through the router it is plugged into now:
  nothing is done;
- 2026-10-17: a route saved in the PC for an earlier router (10.0.0.0/24 via 192.168.1.1
  on a router whose LAN is now 192.168.45.x) left on the right card through a gateway
  that is gone; it counted as "already" and nothing reached the sender. A route through
  any other gateway gets one through the current one, preferred (``ROUTE_METRIC``);
- else the card's current gateway (the router it is plugged into now, from
  NetworkManager) carries a route to the sender's /24 — **runtime only**
  (``ip route``): nothing is saved in the PC's settings, and it is taken
  away at the stop. Changing the router needs nothing from anyone.
- 2026-10-17: ``nmcli device modify`` re-applied the card, and on a DHCP card that asked
  the router again: the T460 got a new address the moment a test started (.36 -> .139),
  and the test's own card address was gone. The route is set with ``ip route`` (the
  node has CAP_NET_ADMIN); NetworkManager leaves a route it does not own alone.
"""

from __future__ import annotations

import ipaddress
import re
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Runner = Callable[[list[str], float], tuple[int, str]]
CMD_TIMEOUT_S = 10.0
ROUTE_METRIC = 50  # below NetworkManager's card metrics: ours wins over a saved one


@dataclass(frozen=True)
class Added:
    nic: str
    net: str
    gateway: str


def _route_dev(run: Runner, target: str) -> tuple[str | None, str | None]:
    """(device, gateway) the kernel uses for ``target`` now."""
    rc, out = run(["ip", "-o", "route", "get", target], CMD_TIMEOUT_S)
    if rc != 0:
        return None, None
    dev = re.search(r"\bdev (\S+)", out)
    via = re.search(r"\bvia (\S+)", out)
    return (dev.group(1) if dev else None), (via.group(1) if via else None)


def gateway_of(run: Runner, nic: str) -> str | None:
    """The router this card is plugged into now: its gateway, else its DHCP router (a
    test card that keeps no default route still got one from the router's DHCP)."""
    rc, out = run(["nmcli", "-g", "IP4.GATEWAY,DHCP4.OPTION", "device", "show", nic],
                  CMD_TIMEOUT_S)  # fmt: skip
    if rc != 0:
        return None
    for line in out.splitlines():
        line = line.strip()
        m = re.search(r"\brouters = (\S+)", line)
        cand = m.group(1) if m else line
        try:
            ipaddress.IPv4Address(cand)
        except ValueError:
            continue
        return cand
    return None


class WanReach:
    def __init__(self, run: Runner | None = None, platform: str = sys.platform) -> None:
        from core.blocks.wifi_join import run_command

        self.run = run or run_command
        self.supported = platform.startswith("linux")
        self._added: list[Added] = []
        self._lock = threading.Lock()

    def ensure(self, target: str, nic: str) -> dict[str, Any]:
        """Make ``target`` leave on ``nic``. Returns what was found or done, never raises:
        a test that cannot get its route still runs and says why (the caller tells)."""
        if not self.supported or not nic or not target:
            return {"ok": True, "done": "nothing (not Linux or nothing to do)"}
        dev, via = _route_dev(self.run, target)
        gw = gateway_of(self.run, nic)
        if dev == nic and (via is None or via == gw):
            return {"ok": True, "done": f"{target} already leaves on {nic}"}
        if gw is None:
            why = f"{target} leaves on {dev}" + (f" via {via}" if via else "")
            return {"ok": False, "done": f"{nic} has no gateway from the router", "why": why}
        net = str(ipaddress.ip_network(f"{target}/24", strict=False))
        rc, out = self.run(["ip", "route", "replace", net, "via", gw, "dev", nic,
                            "metric", str(ROUTE_METRIC)], CMD_TIMEOUT_S)  # fmt: skip
        if rc != 0:
            return {"ok": False, "done": f"route {net} via {gw} not added", "why": out.strip()}
        with self._lock:
            self._added.append(Added(nic, net, gw))
        dev, via = _route_dev(self.run, target)
        ok = dev == nic and via in (None, gw)
        return {"ok": ok, "done": f"route {net} via {gw} on {nic} for this test",
                **({} if ok else {"why": f"{target} still leaves on {dev}"})}  # fmt: skip

    def release(self) -> list[str]:
        """The stop: every route this test added goes away (the PC is as it was)."""
        with self._lock:
            added, self._added = self._added, []
        left = []
        for a in added:
            rc, out = self.run(["ip", "route", "del", a.net, "via", a.gateway, "dev", a.nic,
                                "metric", str(ROUTE_METRIC)], CMD_TIMEOUT_S)  # fmt: skip
            # 2026-10-17: the card's link went down during the test and took the route
            # with it ("No such process"): it is gone, which is what the stop wants
            if rc != 0 and "No such process" not in out:
                left.append(f"{a.net} via {a.gateway} on {a.nic}: {out.strip()}")
        return left
