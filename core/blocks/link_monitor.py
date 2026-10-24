"""NIC link state, read every second (docs/05-standards.md 1.8, 09 NET-013/014/015).

The sender PC's NIC and the DUT WAN port share one link, so the NIC's negotiated speed
is the WAN port's speed, learned without logging in to the DUT. A link can renegotiate
during a test (2.5 G -> 1 G, 1 G -> 100 M or 10 M); this block only reads and reports
changes. Whether a change is serious (below the needed rate) is the Controller's call.

Wireless receivers also report Wi-Fi channel, width, band and BSSID every second: routers
scan and move channels often (auto channel, DFS, band steering), and that is the first
thing to check when a wireless receiver loses packets (05 1.9, NET-016/017).

Cost (measured on a T460, 2026-09-24): the wired reading is sysfs files only, 0.7 ms CPU,
and sends nothing on the wire. Running ``iw`` twice every second would cost 4 ms CPU and
two new processes a second, so Wi-Fi is event driven instead: one long-running
``iw event`` reports channel switches and (re)connections as they happen; the full
``iw dev <nic> link/info`` reading runs only after an event and every 10 s as a safety
net. RSSI comes every second from /proc/net/wireless (a file read, no process).
Windows and Android are P1 item V16.
"""

from __future__ import annotations

import re
import shutil
import socket
import struct
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from core.blocks.process_runner import ProcessRunner, ProcState, StartError

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B
IW_TIMEOUT_S = 1.0
WIFI_REFRESH_S = 10.0  # full Wi-Fi reading at least this often, events or not


@dataclass(frozen=True)
class Wifi:
    bssid: str | None = None
    band: str | None = None
    channel: int | None = None
    width_mhz: int | None = None
    center_mhz: int | None = None
    freq_mhz: int | None = None
    rssi_dbm: float | None = None
    rx_mbps: float | None = None
    tx_mbps: float | None = None
    dtim: int | None = None


def channel_of(freq_mhz: int | None) -> int | None:
    """IEEE 802.11 channel of a centre frequency, for drivers whose ``iw info`` gives no
    channel line (GAP-104: rtl8814au)."""
    if freq_mhz is None:
        return None
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz <= 2472:
        return (freq_mhz - 2407) // 5
    if 5955 <= freq_mhz <= 7115:
        return (freq_mhz - 5950) // 5
    if 4910 <= freq_mhz <= 5895:
        return (freq_mhz - 5000) // 5
    return None


def band_of(freq_mhz: int | None) -> str | None:
    if freq_mhz is None:
        return None
    if 2400 <= freq_mhz < 2500:
        return "2.4GHz"
    if 5925 <= freq_mhz <= 7125:
        return "6GHz"
    if 4900 <= freq_mhz < 5925:
        return "5GHz"
    return None


def _num(pattern: str, text: str) -> float | None:
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def _int_or_none(v: float | None) -> int | None:
    return int(v) if v is not None else None


def parse_iw(link_out: str, info_out: str) -> Wifi | None:
    """``iw dev X link`` + ``iw dev X info`` -> Wifi; None when not connected."""
    m = re.search(r"Connected to ([0-9a-f:]{17})", link_out)
    if not m:
        return None
    freq = _int_or_none(_num(r"freq:\s*([\d.]+)", link_out))
    ch = re.search(
        r"channel (\d+) \((\d+) MHz\)(?:, width: (\d+) MHz)?(?:, center1: (\d+))?", info_out
    )
    width = int(ch.group(3)) if ch and ch.group(3) else None
    if width is None:  # fall back to the width in the bitrate line
        width = _int_or_none(_num(r"bitrate:.*?\b(\d+)MHz", link_out))
    return Wifi(
        bssid=m.group(1),
        band=band_of(freq or (int(ch.group(2)) if ch else None)),
        channel=int(ch.group(1)) if ch else channel_of(freq),
        width_mhz=width,
        center_mhz=int(ch.group(4)) if ch and ch.group(4) else None,
        freq_mhz=freq,
        rssi_dbm=_num(r"signal:\s*(-?[\d.]+)", link_out),
        rx_mbps=_num(r"rx bitrate:\s*([\d.]+)", link_out),
        tx_mbps=_num(r"tx bitrate:\s*([\d.]+)", link_out),
        dtim=_int_or_none(_num(r"dtim period:\s*(\d+)", link_out)),
    )


def read_wifi(nic: str) -> Wifi | None:
    try:
        link = subprocess.run(
            ["iw", "dev", nic, "link"], capture_output=True, text=True, timeout=IW_TIMEOUT_S
        ).stdout
        info = subprocess.run(
            ["iw", "dev", nic, "info"], capture_output=True, text=True, timeout=IW_TIMEOUT_S
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_iw(link, info)


STATION_FIELDS = {"tx retries": "tx_retries", "tx failed": "tx_failed",
                  "beacon loss": "beacon_loss", "rx packets": "rx_packets"}  # fmt: skip


def parse_station(out: str) -> dict[str, int]:
    """``iw dev <nic> station dump`` of a connected card: its one AP entry's counters.
    A field the driver does not give is left out (rtl8814au gives an empty answer)."""
    got = {}
    for line in out.splitlines():
        name, _, value = line.strip().partition(":")
        key = STATION_FIELDS.get(name.strip())
        if key is not None and value.strip().isdigit():
            got[key] = int(value.strip())
    return got


def read_station(nic: str) -> dict[str, int]:
    """02 7 ``wr``/``wf``/``wbl``/``wrp`` (GAP-148): read only with the 10 s full read
    (05 1.9: no ``iw`` every second)."""
    try:
        out = subprocess.run(["iw", "dev", nic, "station", "dump"], capture_output=True,
                             text=True, timeout=IW_TIMEOUT_S).stdout  # fmt: skip
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_station(out)


@dataclass(frozen=True)
class Link:
    nic: str
    ip: str | None
    up: bool
    link_mbps: int | None  # None: unknown (down, or Wi-Fi without a rate reading)
    duplex: str | None
    carrier_changes: int | None
    wireless: bool = False
    wifi: Wifi | None = None

    def to_dict(self) -> dict[str, Any]:
        """02-data 5 ``links``: wired NICs carry link_mbps/duplex, wireless ones ``wifi``."""
        d = asdict(self)
        d.pop("wireless")
        if self.wireless:
            d.pop("link_mbps")
            d.pop("duplex")
        else:
            d.pop("wifi")
        return d


@dataclass(frozen=True)
class Change:
    nic: str
    kind: str  # "speed" | "down" | "up" | "flap" | "wifi" (channel/width/band) | "roam"
    before: Link | None
    after: Link


def _text(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:  # e.g. EINVAL for speed on a down or virtual link
        return None


def _int(p: Path) -> int | None:
    t = _text(p)
    try:
        return int(t) if t is not None else None
    except ValueError:
        return None


def _ioctl_ipv4(nic: str, request: int) -> str | None:
    """Linux SIOCGIF*; None where there is no ``fcntl`` (Windows, V18) or no address."""
    try:
        import fcntl  # Linux only: importing it at the top stopped the Windows Controller
    except ImportError:
        return None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", nic.encode()[:15])
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), request, req)[20:24])
    except OSError:
        return None
    finally:
        s.close()


def ipv4_of(nic: str) -> str | None:
    return _ioctl_ipv4(nic, SIOCGIFADDR)


def ipv4_netmask(nic: str) -> str | None:
    return _ioctl_ipv4(nic, SIOCGIFNETMASK)


def nic_of(ip: str, sys_root: Path = Path("/sys/class/net")) -> str | None:
    """The NIC name that has this IPv4 address (Linux), e.g. for a player's join NIC.
    None when not found or on another OS (Windows name format is V1)."""
    if not ip or ip == "0.0.0.0" or not sys_root.is_dir():
        return None
    for d in sorted(sys_root.iterdir()):
        if ipv4_of(d.name) == ip:
            return d.name
    return _nic_with_extra_address(ip) if sys_root == Path("/sys/class/net") else None


def _nic_with_extra_address(ip: str) -> str | None:
    """A NIC holding ``ip`` as a second address (the ISP role adds 10.0.0.1 next to an
    old lease, 20 GAP-88); the ioctl above only sees the first one."""
    try:
        out = subprocess.run(["ip", "-o", "-4", "addr", "show", "to", ip], capture_output=True,
                             text=True, timeout=2.0, check=False).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    fields = out.split()
    return fields[1] if len(fields) > 1 else None


def wireless_rssi(proc: Path = Path("/proc/net/wireless")) -> dict[str, float]:
    """Signal level (dBm) per wireless NIC; a plain file read."""
    out: dict[str, float] = {}
    for line in (_text(proc) or "").splitlines()[2:]:
        name, _, rest = line.partition(":")
        parts = rest.split()
        if len(parts) >= 3:
            try:
                out[name.strip()] = float(parts[2].rstrip("."))
            except ValueError:
                continue
    return out


class WifiEvents:
    """One ``iw event`` process for the node's lifetime (dies with it: ProcessRunner).
    Its output goes to a log file, each line with its time (``-t``, 2026-10-09); ``new_lines``
    reads only what was added."""

    def __init__(
        self, record_dir: Path, log_path: Path, argv: tuple[str, ...] = ("iw", "event", "-t")
    ) -> None:
        self.log_path = log_path
        self.runner = ProcessRunner("iw-event", argv, record_dir, log_path=log_path)
        self._offset = 0

    def start(self) -> bool:
        try:
            self._offset = self.log_path.stat().st_size if self.log_path.exists() else 0
            self.runner.start()
        except (StartError, OSError, RuntimeError):
            return False
        return True

    @property
    def running(self) -> bool:
        return self.runner.poll() is ProcState.RUNNING

    def new_lines(self) -> list[str]:
        try:
            size = self.log_path.stat().st_size
            if size <= self._offset:
                return []
            with self.log_path.open("rb") as f:
                f.seek(self._offset)
                data = f.read(size - self._offset)
        except OSError:
            return []
        self._offset = size
        return data.decode(errors="replace").splitlines()

    def stop(self) -> None:
        if self.runner.state is not ProcState.IDLE:
            self.runner.stop()


class WifiReader:
    """``wifi_of`` for ``read_links``: cached full reading, refreshed after an event for
    that NIC or every ``refresh_s``; RSSI updated every call. Without a running event
    process it reads fully every call (heavier, never blind)."""

    def __init__(
        self,
        events: WifiEvents | None,
        full_read: Any = read_wifi,
        rssi_read: Any = wireless_rssi,
        refresh_s: float = WIFI_REFRESH_S,
        clock: Any = time.monotonic,
    ) -> None:
        self.events = events
        self.full_read = full_read
        self.rssi_read = rssi_read
        self.refresh_s = refresh_s
        self.clock = clock
        self._cache: dict[str, tuple[float, Wifi | None]] = {}
        self._dirty: set[str] = set()
        self.full_reads = 0

    def __call__(self, nic: str) -> Wifi | None:
        live = self.events is not None and self.events.running
        if live and self.events is not None:
            for line in self.events.new_lines():
                for name in [*self._cache, nic]:
                    if line.startswith(name + " ") or f" {name} " in line or f"{name}:" in line:
                        self._dirty.add(name)
        now = self.clock()
        cached = self._cache.get(nic)
        if not live or cached is None or nic in self._dirty or now - cached[0] >= self.refresh_s:
            self._cache[nic] = (now, self.full_read(nic))
            self._dirty.discard(nic)
            self.full_reads += 1
        wifi = self._cache[nic][1]
        rssi = self.rssi_read().get(nic)
        if wifi is None or rssi is None or rssi >= 0:  # GAP-104: some drivers put a quality
            return wifi  # number, not dBm, in /proc/net/wireless; keep iw's dBm then
        return replace(wifi, rssi_dbm=rssi)


def wifi_events_supported(sys_root: Path = Path("/sys/class/net")) -> bool:
    """Linux with ``iw`` and at least one wireless NIC."""
    if not sys.platform.startswith("linux") or shutil.which("iw") is None:
        return False
    return sys_root.is_dir() and any((d / "wireless").exists() for d in sys_root.iterdir())


def read_links(
    sys_root: Path = Path("/sys/class/net"),
    ip_of: Any = ipv4_of,
    wifi_of: Any = read_wifi,
) -> list[Link]:
    """Physical NICs only (virtual ones have no ``device`` entry). Windows: its own
    adapter list (win_links, 2026-10-16), no Wi-Fi details."""
    if sys.platform == "win32" and sys_root == Path("/sys/class/net"):
        from core.blocks.win_links import adapters

        return [Link(nic=a["nic"], ip=a["ip"], up=a["up"], link_mbps=a["link_mbps"],
                     duplex=None, carrier_changes=None, wireless=a["wireless"])
                for a in adapters()]  # fmt: skip
    links = []
    for d in sorted(sys_root.iterdir()) if sys_root.is_dir() else []:
        if not (d / "device").exists():
            continue
        up = _text(d / "operstate") == "up"
        speed = _int(d / "speed")
        wireless = (d / "wireless").exists()
        duplex = _text(d / "duplex")
        links.append(
            Link(
                nic=d.name,
                ip=ip_of(d.name),
                up=up,
                link_mbps=speed if up and speed and speed > 0 else None,
                duplex=duplex if up and duplex in ("full", "half") else None,
                carrier_changes=_int(d / "carrier_changes"),
                wireless=wireless,
                wifi=wifi_of(d.name) if wireless and up else None,
            )
        )
    return links


class LinkWatch:
    """Remembers the last reading and reports what changed since."""

    def __init__(self) -> None:
        self._last: dict[str, Link] = {}
        self._wifi: dict[str, Link] = {}  # last reading with Wi-Fi values, across a drop

    def update(self, links: list[Link]) -> list[Change]:
        changes = []
        for now in links:
            before = self._last.get(now.nic)
            if before is not None:
                if before.up and not now.up:
                    changes.append(Change(now.nic, "down", before, now))
                elif not before.up and now.up:
                    changes.append(Change(now.nic, "up", before, now))
                elif (
                    now.carrier_changes is not None
                    and before.carrier_changes is not None
                    and now.carrier_changes > before.carrier_changes
                ):
                    changes.append(Change(now.nic, "flap", before, now))  # down+up within 1 s
                # a cable's speed; a Wi-Fi rate moves all the time (2026-10-17: the Controller
                # PC's Wi-Fi logged "speed changed" twice a second)
                if now.up and before.up and now.link_mbps != before.link_mbps and not now.wireless:
                    changes.append(Change(now.nic, "speed", before, now))
            # 2026-10-09 wireless run: the router moved 153 -> 40 while the card was off the
            # air for 9 s, so the readings around it had no Wi-Fi; compare with the last one
            seen = self._wifi.get(now.nic)
            a, b = (seen.wifi if seen else None), now.wifi
            if seen is not None and a and b:
                if a.bssid != b.bssid:
                    changes.append(Change(now.nic, "roam", seen, now))
                if (a.channel, a.width_mhz, a.band) != (b.channel, b.width_mhz, b.band):
                    changes.append(Change(now.nic, "wifi", seen, now))
            self._last[now.nic] = now
            if now.wifi:
                self._wifi[now.nic] = now
        return changes


def gateway_of(nic: str, route: Path = Path("/proc/net/route")) -> str | None:
    """The gateway this NIC's routes use (its DHCP lease's router), or None."""
    try:
        lines = route.read_text().splitlines()[1:]
    except OSError:
        return None
    for f in (x.split() for x in lines):
        if len(f) > 3 and f[0] == nic and f[2] != "00000000":
            return socket.inet_ntoa(struct.pack("<I", int(f[2], 16)))
    return None
