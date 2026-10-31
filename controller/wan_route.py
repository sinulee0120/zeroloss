"""The Controller's way to the router's WAN side (docs/13-ai-loop.md 1, 07 3.2 B, 20 GAP-88).

The test NIC never takes the default route (13 1, NET-019), so a PC behind the
router's NAT (the sender at 10.0.0.1, B) is only reachable through a route "10.0.0.0/24
via the router's LAN address". This puts that route on the test NIC's NetworkManager
connection (kept across reboots) and applies it, taking the router address from the
NIC's DHCP lease. Linux + NetworkManager.

Windows (2026-10-22: after a restart the route was gone and the screen only said "Cannot
reach probook-sender"): when 10.0.0.1 would leave through another card (the internet
Wi-Fi), the route goes through the router of the one wired card that has a router. Adding
it needs an administrator, so Windows asks the person once per Controller start; it is not
kept across restarts (``route add`` without -p). Declined or several wired cards: told
once, nothing guessed. Short OS calls with a time limit only.
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from core.blocks.discovery import default_route_nics
from core.blocks.link_monitor import ipv4_of
from core.blocks.process_runner import NO_WINDOW

WAN_B = "10.0.0.0/24"  # 07 3 table, B
NO_ROUTE_ENV = "ZEROLOSS_NO_ROUTE_CHANGES"  # automatic tests never touch the PC's network


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False,
                              creationflags=NO_WINDOW)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return done.returncode, (done.stdout + done.stderr).strip()


def lab_nics(sys_root: Path = Path("/sys/class/net")) -> list[str]:
    """Wired NICs with an IPv4 address that do not carry the internet route."""
    internet = default_route_nics()
    out = []
    for d in sorted(sys_root.iterdir()) if sys_root.is_dir() else []:
        wired = (d / "device").exists() and not (d / "wireless").exists()
        if wired and d.name not in internet and ipv4_of(d.name):
            out.append(d.name)
    return out


def router_of(nic: str) -> str | None:
    """The router address from the NIC's DHCP lease (NetworkManager)."""
    rc, out = _run(["nmcli", "-g", "DHCP4.OPTION", "device", "show", nic])
    for part in out.replace("|", "\n").splitlines() if rc == 0 else []:
        key, _, value = part.partition("=")
        if key.strip() == "routers" and value.strip():
            return value.strip().split()[0]
    return None


def via(target: str) -> str | None:
    """The NIC the kernel would use for ``target`` now."""
    rc, out = _run(["ip", "route", "get", target])
    words = out.split()
    return words[words.index("dev") + 1] if rc == 0 and "dev" in words else None


def ensure_wan_route(subnet: str = WAN_B) -> str:
    """Make ``subnet`` go through the test NIC's router. Returns what was done (never
    raises: a missing route shows up as the device not answering, DSC-014)."""
    if os.environ.get(NO_ROUTE_ENV) == "1":
        return "route changes are off (tests)"
    if sys.platform == "win32" and shutil.which("nmcli") is None:
        return _windows(subnet)
    if shutil.which("nmcli") is None:
        return "no NetworkManager: route to the router WAN side not set"
    probe = str(next(ipaddress.ip_network(subnet).hosts()))
    nics = lab_nics()
    if not nics:
        return "no separate test network card"
    if via(probe) in nics:
        return f"{subnet} already through {via(probe)}"
    nic = nics[0]
    router = router_of(nic)
    if router is None:
        return f"{nic} has no router address (DHCP) for {subnet}"
    rc, con = _run(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", nic])
    if rc != 0 or not con.strip():
        return f"{nic} has no NetworkManager connection"
    rc, out = _run(["nmcli", "connection", "modify", con.strip(), "+ipv4.routes",
                    f"{subnet} {router}"])  # fmt: skip
    if rc == 0:
        rc, out = _run(["nmcli", "device", "reapply", nic])
    if rc != 0 or via(probe) != nic:
        return f"could not route {subnet} through {nic}: {out[-200:]}"
    return f"{subnet} now through {router} on {nic}"


# ---------------------------------------------------------------------- Windows

WIN_READ = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ip = $env:ZL_PROBE
$r = @(Find-NetRoute -RemoteIPAddress $ip -ErrorAction SilentlyContinue |
       Where-Object { $_.NextHop -or $_.InterfaceIndex } | Select-Object -First 1)
$cards = @(Get-NetIPConfiguration | Where-Object {
    $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' -and
    $_.NetAdapter.PhysicalMediaType -eq '802.3' } | ForEach-Object {
  [pscustomobject]@{ nic = $_.InterfaceAlias; index = [int]$_.InterfaceIndex
                     router = [string]@($_.IPv4DefaultGateway.NextHop)[0] } })
[pscustomobject]@{ via = if ($r) { [int]$r[0].InterfaceIndex } else { 0 }
                   cards = $cards } | ConvertTo-Json -Compress -Depth 3
"""
ASK_WAIT_S = 60.0  # the person reads the Windows question
_asked: dict[str, str] = {}  # subnet -> what came of asking once in this process


def forget_asked() -> None:
    """The person pressed the button: Windows may ask again."""
    _asked.clear()


def _win_read(probe: str) -> dict[str, Any]:
    env = {**os.environ, "ZL_PROBE": probe}
    try:
        done = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                               WIN_READ], capture_output=True, encoding="utf-8", timeout=15,
                              check=False, env=env, creationflags=NO_WINDOW)  # fmt: skip
        got = json.loads(done.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def win_plan(seen: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """("ok" | "add" | "none", the wired card to route through) from what Windows shows."""
    cards = seen.get("cards") or []
    cards = [cards] if isinstance(cards, dict) else [c for c in cards if isinstance(c, dict)]
    if any(c.get("index") == seen.get("via") for c in cards):
        return "ok", next(c for c in cards if c.get("index") == seen.get("via"))
    if len(cards) != 1:
        return "none", None  # no wired card with a router, or several: nothing guessed
    return "add", cards[0]


def manual_fix(subnet: str = WAN_B) -> str | None:
    """What a person types to make ``subnet`` go through the router, when it does not
    now (2026-10-22 user: "AI없이도 사용자가 어떻게든 테스트가 되게"); None = the route is
    fine or cannot be told."""
    net = ipaddress.ip_network(subnet)
    if sys.platform == "win32" and shutil.which("nmcli") is None:
        state, card = win_plan(_win_read(str(next(net.hosts()))))
        if state == "ok":
            return None
        router = card["router"] if card else "<the router's LAN address, e.g. 192.168.45.1>"
        return (f"This PC sends {subnet} out through another card. Open Command Prompt as "
                f"administrator and run: route add {net.network_address} mask {net.netmask} "
                f"{router} — then wait: the check runs again by itself.")  # fmt: skip
    nics = lab_nics()
    probe = str(next(net.hosts()))
    if not nics or via(probe) in nics:
        return None
    router = router_of(nics[0]) or "<the router's LAN address>"
    return (
        f"This PC sends {subnet} out through {via(probe)}. Run: sudo ip route add "
        f"{subnet} via {router} dev {nics[0]} — then wait: the check runs again."
    )


def _elevated_route(subnet: str, card: dict[str, Any]) -> bool:
    """``route add`` as administrator (Windows asks the person); False = not started."""
    import ctypes  # Windows only

    net = ipaddress.ip_network(subnet)
    args = f"add {net.network_address} mask {net.netmask} {card['router']} if {card['index']}"
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    return int(shell32.ShellExecuteW(None, "runas", "route.exe", args, None, 0)) > 32


def _windows(subnet: str) -> str:
    probe = str(next(ipaddress.ip_network(subnet).hosts()))
    state, card = win_plan(_win_read(probe))
    if state == "ok" and card:
        return f"{subnet} already through {card['nic']}"
    if subnet in _asked:
        return _asked[subnet]  # asked once in this start: never again
    if state == "none" or card is None:
        _asked[subnet] = f"{subnet}: no single wired card with a router to route through"
        return f"could not route {subnet}: no single wired card with a router (set it by hand)"
    if not _elevated_route(subnet, card):
        _asked[subnet] = f"{subnet} not routed (Windows permission declined)"
        return f"could not route {subnet} through {card['nic']}: Windows permission declined"
    end = time.monotonic() + ASK_WAIT_S
    while time.monotonic() < end:
        if win_plan(_win_read(probe))[0] == "ok":
            _asked[subnet] = f"{subnet} already through {card['nic']}"
            return f"{subnet} now through {card['router']} on {card['nic']}"
        time.sleep(2)
    _asked[subnet] = f"{subnet} not routed (Windows permission not given)"
    return f"could not route {subnet} through {card['nic']}: Windows permission not given"
