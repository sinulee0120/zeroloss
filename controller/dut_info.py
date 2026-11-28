"""The router's model and firmware from its UPnP description (docs/03-features.md F14
"툴이 자동으로 알아내는 것": "DUT 정보 · 모델명·펌웨어 버전 (가능한 경우) · UPnP/SSDP 장치 설명
조회. 로그인하지 않음 · 보고서에 자동 기록").

2026-11-28 (user "계속해", after "안만든거 다 만들어"; dut.model and firmware_build were always
null): an SSDP search for an Internet gateway goes out on each of this PC's cards and the
answer's description (an HTTP GET of the XML it names) is read. Nothing is logged in to and
nothing is changed. A report must never carry another router's model (on the Controller PC
measured 2026-11-28 the house router answered on Wi-Fi and the router under test on the
wired card), so an answer counts only when it comes from the subnet of the card it came in
on and of one of the test's receiver PCs (they sit on the router under test's LAN), and the
description must be served from that same address. Two such routers: nothing is recorded.
The UDN (the device's own unique id) tells F24 it is the same router again.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import sys
import time
import urllib.request
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

SSDP = ("239.255.255.250", 1900)
SEARCH = (
    b'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\n'
    b"MX: 2\r\nST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n"
)
WAIT_S = 2.5
DESCRIBE_S = 3.0
MAX_XML = 256 * 1024
TAGS = ("manufacturer", "modelName", "modelNumber", "modelDescription", "friendlyName", "UDN",
        "firmwareVersion", "softwareVersion")  # fmt: skip


def local_cards() -> list[tuple[str, int]]:
    """(IPv4 address, prefix length) of this PC's cards that are up."""
    if sys.platform == "win32":
        from core.blocks.win_links import adapters

        return [(str(a["ip"]), int(a["prefix"])) for a in adapters()
                if a.get("up") and a.get("ip") and isinstance(a.get("prefix"), int)]  # fmt: skip
    from core.blocks.link_monitor import ipv4_netmask, read_links

    out = []
    for x in read_links():
        mask = ipv4_netmask(x.nic) if x.ip and x.up else None
        if x.ip and mask:
            out.append((str(x.ip), ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen))
    return out


def search(cards: list[tuple[str, int]], wait_s: float = WAIT_S) -> dict[str, tuple[str, str]]:
    """{answering address: (LOCATION, the card address it came in on)}."""
    socks = []
    for ip, _ in cards:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind((ip, 0))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            s.settimeout(0.2)
            s.sendto(SEARCH, SSDP)
            socks.append((ip, s))
        except OSError:
            continue
    found: dict[str, tuple[str, str]] = {}
    end = time.monotonic() + wait_s
    try:
        while time.monotonic() < end and socks:
            for ip, s in socks:
                try:
                    data, src = s.recvfrom(4096)
                except (TimeoutError, OSError):
                    continue
                loc = re.search(rb"(?im)^location:\s*(\S+)", data)
                if loc:
                    found.setdefault(src[0], (loc.group(1).decode("ascii", "replace"), ip))
    finally:
        for _, s in socks:
            s.close()
    return found


def describe(url: str, opener: Callable[..., Any] = urllib.request.urlopen) -> dict[str, str]:
    """The description's fields (no login; a few kB, read at most MAX_XML)."""
    with opener(url, timeout=DESCRIBE_S) as r:
        xml = r.read(MAX_XML).decode("utf-8", "replace")
    out = {}
    for tag in TAGS:
        m = re.search(rf"<{tag}>\s*([^<]*?)\s*</{tag}>", xml)
        if m and m.group(1):
            out[tag] = m.group(1)[:120]
    return out


def _same_net(a: str, b: str, prefix: int) -> bool:
    try:
        net = ipaddress.IPv4Network(f"{a}/{prefix}", strict=False)
        return ipaddress.IPv4Address(b) in net
    except ValueError:
        return False


Find = Callable[[list[tuple[str, int]]], dict[str, tuple[str, str]]]


def dut_info(receiver_hosts: list[str], cards: list[tuple[str, int]] | None = None,
             find: Find = search, read: Callable[[str], dict[str, str]] = describe,
             ) -> dict[str, Any] | None:  # fmt: skip
    """The router under test's description, or None when it cannot be told for sure."""
    cards = local_cards() if cards is None else cards
    prefix = dict(cards)
    hosts = [h for h in receiver_hosts if h]
    picked = []
    for src, (location, card) in find(cards).items():
        p = prefix.get(card)
        if p is None or not _same_net(card, src, p):
            continue  # not from the network of the card it came in on
        if not any(_same_net(src, h, p) for h in hosts):
            continue  # not the receivers' router (the house router on Wi-Fi, 2026-11-28)
        if urlsplit(location).hostname != src:
            continue  # a description elsewhere is not this router's
        picked.append((src, location))
    if len(picked) != 1:
        return None  # none, or two routers on the receivers' network: never guess
    src, location = picked[0]
    try:
        d = read(location)
    except (OSError, ValueError):
        return None
    model = " ".join(dict.fromkeys(v for k in ("manufacturer", "modelName", "modelNumber")
                                   if (v := d.get(k))))  # fmt: skip
    return {
        "address": src,
        "model": model or None,
        "firmware_build": d.get("firmwareVersion") or d.get("softwareVersion"),
        "description": d.get("modelDescription"),
        "name": d.get("friendlyName"),
        "udn": d.get("UDN"),
    }
