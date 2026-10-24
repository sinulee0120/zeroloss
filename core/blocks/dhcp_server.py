"""A DHCP server for one NIC and one address (docs/07-network.md 3.2 B, RFC 2131/2132).

One job: be the ISP for the router's WAN port. It answers only on the NIC it is bound
to, lends exactly one address (to the first client that takes it; the same client gets
it again), with a router option = this PC and no DNS. The lease is kept in a file so a
restart gives the same address. When another device asks while the lease is held, the
address is checked first (RFC 2131 4.4.1): if its holder no longer answers ARP on the
card, it goes to the new device (the cable moved, GAP-112). ``other_servers`` asks once
whether another DHCP server is already on that NIC (SND-012).
"""

from __future__ import annotations

import json
import os
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MAGIC = b"\x63\x82\x53\x63"
DISCOVER, OFFER, REQUEST, DECLINE, ACK, NAK, RELEASE, INFORM = range(1, 9)
SERVER_PORT, CLIENT_PORT = 67, 68
LEASE_S = 7 * 24 * 3600  # 07 3.2: longer than any test
SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
PROBE_EVERY_S = 10.0  # 07 3.2 B: one ARP check of the held address per 10 s at most


@dataclass(frozen=True)
class Message:
    op: int
    xid: int
    flags: int
    ciaddr: str
    chaddr: bytes  # client MAC (6 bytes)
    kind: int  # option 53
    requested: str | None  # option 50
    server_id: str | None  # option 54

    @property
    def mac(self) -> str:
        return ":".join(f"{b:02x}" for b in self.chaddr)


def parse(data: bytes) -> Message | None:
    if len(data) < 240 or data[236:240] != MAGIC:
        return None
    op, _htype, hlen = data[0], data[1], data[2]
    (xid,) = struct.unpack("!I", data[4:8])
    (flags,) = struct.unpack("!H", data[10:12])
    opts = _options(data[240:])
    kind = opts.get(53, b"\0")[0]
    return Message(
        op, xid, flags, socket.inet_ntoa(data[12:16]), data[28 : 28 + min(hlen, 16)][:6],
        kind, _ip(opts.get(50)), _ip(opts.get(54)),
    )  # fmt: skip


def _options(raw: bytes) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    i = 0
    while i < len(raw):
        code = raw[i]
        if code == 255:
            break
        if code == 0:
            i += 1
            continue
        if i + 1 >= len(raw):
            break
        n = raw[i + 1]
        out[code] = raw[i + 2 : i + 2 + n]
        i += 2 + n
    return out


def _ip(raw: bytes | None) -> str | None:
    return socket.inet_ntoa(raw) if raw and len(raw) == 4 else None


def packet(
    op: int, xid: int, flags: int, chaddr: bytes, kind: int, yiaddr: str = "0.0.0.0",
    siaddr: str = "0.0.0.0", options: dict[int, bytes] | None = None,
) -> bytes:  # fmt: skip
    head = struct.pack("!BBBBIHH4s4s4s4s", op, 1, 6, 0, xid, 0, flags,
                       socket.inet_aton("0.0.0.0"), socket.inet_aton(yiaddr),
                       socket.inet_aton(siaddr), socket.inet_aton("0.0.0.0"))  # fmt: skip
    body = chaddr.ljust(16, b"\0") + bytes(64) + bytes(128) + MAGIC
    opts = bytes([53, 1, kind])
    for code, value in (options or {}).items():
        opts += bytes([code, len(value)]) + value
    return head + body + opts + b"\xff"


@dataclass
class Lease:
    mac: str
    ip: str
    since_utc: str
    expires_utc: str

    def expired(self) -> bool:
        return datetime.fromisoformat(self.expires_utc.replace("Z", "+00:00")) < datetime.now(UTC)


class OneLeaseServer:
    def __init__(
        self,
        nic: str,
        server_ip: str,
        prefix: int,
        lease_ip: str,
        state_file: Path,
        on_event: Callable[[str, str, dict[str, Any]], None],
        lease_s: int = LEASE_S,
        server_port: int = SERVER_PORT,
        client_port: int = CLIENT_PORT,
        bind_device: bool = True,
        reply_to: str = "255.255.255.255",
        ignore_macs: frozenset[str] = frozenset(),
        alive: Callable[[str, str, str], bool | None] | None = None,
        run_async: Callable[[Callable[[], None]], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.nic, self.server_ip, self.prefix, self.lease_ip = nic, server_ip, prefix, lease_ip
        self.state_file, self.on_event, self.lease_s = state_file, on_event, lease_s
        self.server_port, self.client_port = server_port, client_port
        self.bind_device, self.reply_to = bind_device, reply_to
        # this PC's own NIC: its DHCP client must never take the router's address
        self.ignore_macs = frozenset(m.lower() for m in ignore_macs)
        self.lease: Lease | None = self._load()
        self.requests = 0
        # GAP-112: does the held address still answer ARP? (nic, own ip, address) -> bool,
        # None = cannot check. Without it the one address never moves (as before).
        self.alive, self.clock = alive, clock
        self.run_async = run_async or _in_thread
        self._probing, self._probe_at = False, float("-inf")
        self._gone_mac: str | None = None  # the holder found not answering
        self._warned: set[tuple[str, str]] = set()
        self._sock: socket.socket | None = None
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            if self.bind_device:  # never answer on another NIC (07 3.2)
                sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, self.nic.encode() + b"\0")
            sock.bind(("", self.server_port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.5)
        self._sock = sock
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="dhcp", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._halt.set()
        if self._thread is not None:
            self._thread.join(2)
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            lease = asdict(self.lease) if self.lease and not self.lease.expired() else None
        return {"nic": self.nic, "server_ip": self.server_ip, "lease": lease,
                "requests": self.requests}  # fmt: skip

    # ---------------------------------------------------------------- protocol

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._halt.is_set():
            try:
                data, _ = self._sock.recvfrom(1500)
            except TimeoutError:
                continue
            except OSError:
                return
            msg = parse(data)
            if msg is None or msg.op != 1:
                continue
            reply = self.handle(msg)
            if reply is not None:
                dest = msg.ciaddr if msg.ciaddr != "0.0.0.0" else self.reply_to
                try:
                    self._sock.sendto(reply, (dest, self.client_port))
                except OSError as e:
                    self.on_event("ZL-SND-011", "warn", {"text": f"DHCP reply not sent: {e}"})

    def handle(self, msg: Message) -> bytes | None:
        """One request -> the reply to send (or None). Only one address exists."""
        if msg.mac in self.ignore_macs:
            return None
        with self._lock:
            self.requests += 1
            reply, check = self._answer(msg)
        if check is not None:  # outside the lock: the check waits for ARP answers
            held_mac, new_mac = check
            self.run_async(lambda: self._check_holder(held_mac, new_mac))
        return reply

    def _answer(self, msg: Message) -> tuple[bytes | None, tuple[str, str] | None]:
        held = self.lease if self.lease and not self.lease.expired() else None
        gone = held is not None and held.mac == self._gone_mac
        ours = held is None or held.mac == msg.mac or gone
        if msg.kind == REQUEST and msg.server_id not in (None, self.server_ip):
            return None, None  # the client took another server's offer
        check = None
        if held is not None and not ours and msg.kind in (DISCOVER, REQUEST):
            check = (held.mac, msg.mac) if self._probe_due() else None
        if msg.kind == DISCOVER:
            return (self._reply(msg, OFFER) if ours else None), check
        if msg.kind == REQUEST:
            wanted = msg.requested or (msg.ciaddr if msg.ciaddr != "0.0.0.0" else None)
            if wanted == self.lease_ip and ours:
                self._grant(msg.mac)
                return self._reply(msg, ACK), None
            return self._reply(msg, NAK, nak=True), check
        if msg.kind in (RELEASE, DECLINE) and held is not None and held.mac == msg.mac:
            self.lease = None
            self._save()
            self.on_event("ZL-INFO", "info", {"text": f"DHCP lease given back by {msg.mac}"})
        return None, None

    def _probe_due(self) -> bool:
        """One ARP check at a time and one per PROBE_EVERY_S (no flood while a client
        keeps asking). Called under the lock."""
        if self.alive is None or self._probing or (
                self.clock() - self._probe_at < PROBE_EVERY_S):  # fmt: skip
            return False
        self._probing, self._probe_at = True, self.clock()
        return True

    def _check_holder(self, held_mac: str, new_mac: str) -> None:
        """07 3.2 B (GAP-112): the held address still answers -> keep it (SND-024 once);
        nobody answers -> the next request of the new device gets it."""
        answer: bool | None = None
        why = "no packet socket"
        try:
            assert self.alive is not None
            answer = self.alive(self.nic, self.server_ip, self.lease_ip)
        except OSError as e:
            why = str(e)
        with self._lock:
            self._probing = False
            held = self.lease if self.lease and not self.lease.expired() else None
            if held is None or held.mac != held_mac:
                return  # the lease changed meanwhile: this answer is old
            if answer is False:
                self._gone_mac = held_mac
                return
            if (held_mac, new_mac) in self._warned:
                return
            self._warned.add((held_mac, new_mac))
        reason = f"{held_mac} still answers ARP" if answer else f"cannot check: {why}"
        self.on_event("ZL-SND-024", "warn", {
            "text": f"Another device holds {self.lease_ip} on {self.nic}",
            "why": f"{reason}; {new_mac} asked for the address and got none.",
            "holder": held_mac, "asking": new_mac})  # fmt: skip

    def _grant(self, mac: str) -> None:
        now = datetime.now(UTC)
        renew = self.lease is not None and self.lease.mac == mac
        old = self.lease.mac if self.lease and not self.lease.expired() and not renew else None
        self._gone_mac = None
        self._warned.clear()
        self.lease = Lease(
            mac, self.lease_ip, _utc(now), _utc(now + timedelta(seconds=self.lease_s))
        )
        self._save()
        if old is not None:  # GAP-112: the cable moved to another device
            self.on_event("ZL-INFO", "info", {
                "text": f"Router WAN address moved from {old} to {mac}",
                "lease": asdict(self.lease), "clears": ["ZL-SND-024"]})  # fmt: skip
            return
        text = f"Router WAN {'renewed' if renew else 'got'} {self.lease_ip} ({mac})"
        self.on_event("ZL-INFO", "info", {"text": text, "lease": asdict(self.lease)})

    def _reply(self, msg: Message, kind: int, nak: bool = False) -> bytes:
        sid = socket.inet_aton(self.server_ip)
        if nak:
            return packet(2, msg.xid, msg.flags, msg.chaddr, NAK, options={54: sid})
        mask = struct.pack("!I", (0xFFFFFFFF << (32 - self.prefix)) & 0xFFFFFFFF)
        t = self.lease_s
        options = {54: sid, 51: struct.pack("!I", t), 58: struct.pack("!I", t // 2),
                   59: struct.pack("!I", t * 7 // 8), 1: mask, 3: sid}  # fmt: skip
        return packet(2, msg.xid, msg.flags, msg.chaddr, kind, self.lease_ip, self.server_ip,
                      options)  # fmt: skip

    def _load(self) -> Lease | None:
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8")).get("lease")
            return Lease(**d) if isinstance(d, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _save(self) -> None:
        try:
            doc = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {"schema_version": 1}
        doc["lease"] = asdict(self.lease) if self.lease else None
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc), encoding="utf-8")
        os.replace(tmp, self.state_file)


def _utc(t: datetime) -> str:
    return t.isoformat(timespec="seconds").replace("+00:00", "Z")


def other_servers(
    nic: str, mac: bytes, wait_s: float = 3.0, server_port: int = SERVER_PORT,
    client_port: int = CLIENT_PORT, bind_device: bool = True, to: str = "255.255.255.255",
) -> list[str] | None:  # fmt: skip
    """SND-012: ask once on ``nic`` whether any DHCP server answers. None when we cannot
    ask (port 68 taken by a DHCP client on this PC, no rights)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if bind_device:
            sock.setsockopt(socket.SOL_SOCKET, SO_BINDTODEVICE, nic.encode() + b"\0")
        sock.bind(("", client_port))
    except OSError:
        sock.close()
        return None
    xid = int.from_bytes(os.urandom(4), "big")
    found: list[str] = []
    try:
        sock.sendto(packet(1, xid, 0x8000, mac, DISCOVER), (to, server_port))
        end = time.monotonic() + wait_s
        while (left := end - time.monotonic()) > 0:
            sock.settimeout(left)
            try:
                data, src = sock.recvfrom(1500)
            except TimeoutError:
                break
            m = parse(data)
            if m is not None and m.op == 2 and m.xid == xid:
                found.append(m.server_id or str(src[0]))
    finally:
        sock.close()
    return sorted(set(found))


def _in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="dhcp-arp", daemon=True).start()
