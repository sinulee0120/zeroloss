"""Find nodes before a test (docs/06-install.md 10.2 implementation rules, 07 5).

In order: broadcast (UDP, every NIC's subnet and 255.255.255.255), then the sender's
recommended addresses (a NAT keeps broadcasts off the WAN side), then an address range
when one is given (TCP ``/v1/health``). One row per ``device_id``; registered devices
are marked, new ones get a suggested name. The operator picks what to add.
A registered device found only at another address gets its new address saved (06 10.2,
GAP-73 ④: the sender PC moved to the router WAN port) — nobody registers it again.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from controller.devices import DeviceList
from core.blocks.discovery import ask, broadcast_addresses
from core.protocol.client import NodeClient
from core.protocol.envelope import ZlError

SENDER_TRY = ("10.0.0.1", "10.0.0.2")  # 07 3: DHCP way (B) and fixed IP way (A) defaults
SCAN_TIMEOUT_S = 0.3
SCAN_WORKERS = 64
WIDEST_SCAN = 22  # /22 = 1022 addresses at most


def _health(host: str, port: int, boot_id: str, timeout_s: float) -> dict[str, Any] | None:
    client = NodeClient(host, port, boot_id, host)
    try:
        h: dict[str, Any] = client.get("/v1/health", timeout_s=timeout_s)
        return h
    except ZlError:
        return None
    finally:
        client.close()


def discover(
    devices: DeviceList,
    boot_id: str,
    port: int = 10992,
    scan: str | None = None,
    try_sender: bool = True,
    targets: Callable[[], list[str]] = broadcast_addresses,
    sender_try: tuple[str, ...] = SENDER_TRY,
    on_move: Callable[[str, str, str], None] | None = None,
    save: bool = True,
) -> dict[str, Any]:
    """02 4-1 ``POST /v1/controller/discover``. ``save=False`` (the search between tests,
    GAP-155): registered devices found elsewhere are reported, never moved."""
    hosts: list[str] = []
    if scan:
        try:
            net = ipaddress.ip_network(scan, strict=False)
        except ValueError as e:
            raise ZlError("RUN-014", f"Scan range {scan!r} is not an address range", str(e)) from e
        if net.version != 4 or net.prefixlen < WIDEST_SCAN:
            raise ZlError("RUN-014", f"Scan range {scan} is too wide", "At most a /22 (IPv4).")
        hosts = [str(h) for h in net.hosts()]
    rows: dict[str, dict[str, Any]] = {}
    seen: dict[str, set[tuple[str, int]]] = {}  # every address a device answered at

    def keep(body: dict[str, Any], address: str, how: str) -> None:
        did = str(body.get("device_id") or "")
        if body.get("role") == "controller":
            return  # the Controller's own node (or another Controller): not a test device
        if did:
            seen.setdefault(did, set()).add((address, int(body.get("port") or port)))
        if did and did not in rows:
            rows[did] = {
                "device_id": did, "address": address, "port": int(body.get("port") or port),
                "host": body.get("host"), "role": body.get("role"), "build": body.get("build"),
                "how": how, "tcp_ok": None if how == "broadcast" else True,
            }  # fmt: skip

    for body, src in ask(targets(), port):
        keep(body, src, "broadcast")
    heard = [r for r in rows.values() if r["tcp_ok"] is None]
    if heard:  # 09 DSC-008: it answers on UDP; can we reach its control port (TCP)?
        with ThreadPoolExecutor(min(SCAN_WORKERS, len(heard))) as pool:
            oks = pool.map(lambda r: _health(r["address"], r["port"], boot_id, 2.0), heard)
            for r, h in zip(heard, oks, strict=True):
                r["tcp_ok"] = h is not None
    tried = "skipped"
    if try_sender:
        tried = "not found"
        for host in sender_try:
            h = _health(host, port, boot_id, 1.0)
            if h is not None:
                keep({**h, "port": port}, host, "try")
                tried = "found"
    if hosts:
        with ThreadPoolExecutor(SCAN_WORKERS) as pool:
            found = pool.map(lambda h: (h, _health(h, port, boot_id, SCAN_TIMEOUT_S)), hosts)
            for host, h in found:
                if h is not None:
                    keep({**h, "port": port}, host, "scan")
    for did, r in rows.items():  # GAP-155: every address it answered at, for the IP box
        r["addresses"] = sorted({a for a, _ in seen.get(did, set())})
    moved, kept = _moved(devices, rows, seen, on_move, sender_try) if save else ([], [])
    return {"found": _named(devices, list(rows.values())), "sender_try": tried,
            "codes": _codes(rows, tried), "moved": moved, "kept": kept}  # fmt: skip


def same_network(a: str, b: str, prefix: int = 24) -> bool:
    try:
        net = ipaddress.ip_network(f"{a}/{prefix}", strict=False)
        return ipaddress.ip_address(b) in net
    except ValueError:
        return False


def _moved(
    devices: DeviceList,
    rows: dict[str, dict[str, Any]],
    seen: dict[str, set[tuple[str, int]]],
    on_move: Callable[[str, str, str], None] | None,
    sender_try: tuple[str, ...] = SENDER_TRY,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Registered devices that answer only somewhere else now: save the new address.
    Not when its old address still answers too, nor when the new one is not reachable
    over TCP (a broadcast answer alone is not enough to control it).
    Only on the same network (the router gave it another address) or to the sender
    addresses on the router WAN side (GAP-73). 2026-10-10 (GAP-153): with this PC's test
    cable out, both PCs were found on the home Wi-Fi and saved there; the next test then
    went through the home Wi-Fi, not the router, and stopped. Such a find is ``kept``
    (reported, not saved)."""
    out, kept = [], []
    for d in devices.all():
        r = rows.get(d.device_id)
        if r is None or (d.host, d.port) in seen.get(d.device_id, set()) or not r["tcp_ok"]:
            continue
        if not same_network(d.host, r["address"]) and r["address"] not in sender_try:
            kept.append({"name": d.name, "at": d.host, "seen": r["address"]})
            continue
        done = devices.move(d.device_id, r["address"], r["port"])
        if done is not None:
            dev, old = done
            new = f"{dev.host}:{dev.port}"
            out.append({"name": dev.name, "from": old, "to": new})
            if on_move is not None:
                on_move(dev.name, old, new)
    return out, kept


def _named(devices: DeviceList, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``known``: already in the device list (same device_id); ``name``: its name, or
    the host name (with the address end when two new ones share it)."""
    by_id = {d.device_id: d.name for d in devices.all()}
    hosts = [str(r.get("host") or "pc") for r in rows]
    out = []
    for r, host in zip(rows, hosts, strict=True):
        known = r["device_id"] in by_id
        name = by_id.get(r["device_id"]) or host
        if not known and hosts.count(host) > 1:
            name = f"{host}-{r['address'].rsplit('.', 1)[-1]}"
        out.append({**r, "known": known, "name": name})
    return sorted(out, key=lambda r: (not r["known"], r["name"]))


def _codes(rows: dict[str, dict[str, Any]], tried: str) -> list[str]:
    codes = []
    if not rows:
        codes.append("ZL-DSC-001")
    hosts = [r.get("host") for r in rows.values()]
    if any(hosts.count(h) > 1 for h in hosts):
        codes.append("ZL-DSC-002")
    if tried == "not found":
        codes.append("ZL-DSC-010")
    if any(r.get("tcp_ok") is False for r in rows.values()):
        codes.append("ZL-DSC-008")  # found, but its control port is closed
    return codes
