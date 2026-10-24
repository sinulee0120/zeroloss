"""m2u_check block (10 4 method A): destination MAC of the test frames tells M2U."""

from __future__ import annotations

import socket
import struct
from pathlib import Path

import pytest

from core.blocks import m2u_check
from core.blocks.m2u_check import frame_dest, is_wireless, verdict

OWN = "aa:bb:cc:00:11:22"
MCAST = "01:00:5e:7f:51:01"  # 239.255.81.1


def frame(dst: str, group: str = "239.255.81.1", port: int = 5004, proto: int = 17) -> bytes:
    eth = bytes.fromhex(dst.replace(":", "")) + bytes(6) + b"\x08\x00"
    ip = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 28 + 16, 0, 0, 1, proto, 0,
                     socket.inet_aton("10.0.0.2"), socket.inet_aton(group))  # fmt: skip
    udp = struct.pack("!HHHH", 40000, port, 8 + 16, 0)
    return eth + ip + udp + bytes(16)


def test_frame_dest_only_for_the_test_stream() -> None:
    assert frame_dest(frame(OWN), "239.255.81.1", 5004) == OWN
    assert frame_dest(frame(MCAST), "239.255.81.1", 5004) == MCAST
    assert frame_dest(frame(OWN, port=5005), "239.255.81.1", 5004) is None
    assert frame_dest(frame(OWN, group="239.255.81.2"), "239.255.81.1", 5004) is None
    assert frame_dest(frame(OWN, proto=6), "239.255.81.1", 5004) is None
    assert frame_dest(frame(OWN)[:30], "239.255.81.1", 5004) is None
    arp = bytes(12) + b"\x08\x06" + bytes(40)
    assert frame_dest(arp, "239.255.81.1", 5004) is None


def test_verdict() -> None:
    assert verdict([OWN] * 5, OWN.upper())["state"] == "active"
    assert verdict([MCAST] * 5, OWN)["state"] == "not_active"
    mixed = verdict([OWN, MCAST], OWN)
    assert mixed["state"] == "unknown" and "mixed" in mixed["why"]
    none = verdict([], OWN, "no permission to capture (CAP_NET_RAW)")
    assert none == {"state": "unknown", "why": "no permission to capture (CAP_NET_RAW)",
                    "frames": 0}  # fmt: skip
    assert verdict(["02:00:00:00:00:09"], OWN)["state"] == "unknown"


def test_wireless_only_and_capture_without_permission(tmp_path: Path) -> None:
    (tmp_path / "wlp4s0" / "wireless").mkdir(parents=True)
    (tmp_path / "enp0s31f6").mkdir()
    assert is_wireless("wlp4s0", tmp_path) and not is_wireless("enp0s31f6", tmp_path)
    try:
        socket.socket(socket.AF_PACKET, socket.SOCK_RAW).close()
    except PermissionError:
        got = m2u_check.capture("lo", "239.255.81.1", 5004, 0.2)
        assert got["state"] == "unknown" and "CAP_NET_RAW" in got["why"]
    else:
        pytest.skip("this user may capture; covered by the permission-less path elsewhere")


def run_bpf(prog: list[tuple[int, int, int, int]], pkt: bytes) -> int:
    """The classic BPF instructions stream_filter uses, as the kernel runs them; an
    out-of-range load drops the packet like the kernel does."""
    a = x = pc = 0
    while True:
        code, jt, jf, k = prog[pc]
        pc += 1
        try:
            if code == 0x28:  # ldh [k]
                (a,) = struct.unpack("!H", pkt[k : k + 2])
            elif code == 0x20:  # ld [k]
                (a,) = struct.unpack("!I", pkt[k : k + 4])
            elif code == 0x30:  # ldb [k]
                a = pkt[k]
            elif code == 0x48:  # ldh [x + k]
                (a,) = struct.unpack("!H", pkt[x + k : x + k + 2])
            elif code == 0xB1:  # ldxb 4*([k]&0xf)
                x = 4 * (pkt[k] & 0x0F)
            elif code == 0x15:  # jeq #k
                pc += jt if a == k else jf
            elif code == 0x45:  # jset #k
                pc += jt if a & k else jf
            elif code == 0x06:  # ret #k
                return k
            else:
                raise AssertionError(f"instruction {code:#x} not modelled")
        except (struct.error, IndexError):
            return 0


def test_the_kernel_filter_passes_only_the_stream_cut_short() -> None:
    prog = m2u_check.stream_filter("239.255.81.1", 5004)
    jumps = [(i, jt, jf) for i, (code, jt, jf, _) in enumerate(prog) if code & 0x07 == 0x05]
    assert all(i + 1 + max(jt, jf) < len(prog) for i, jt, jf in jumps)  # inside the program
    assert prog[-1] == (0x06, 0, 0, 0)  # the end drops
    assert run_bpf(prog, frame(MCAST)) == m2u_check.SNAP
    assert run_bpf(prog, frame(OWN)) == m2u_check.SNAP  # converted to unicast: same stream
    assert run_bpf(prog, frame(MCAST, port=5005)) == 0
    assert run_bpf(prog, frame(MCAST, group="239.255.81.2")) == 0
    assert run_bpf(prog, frame(MCAST, proto=6)) == 0  # TCP
    assert run_bpf(prog, frame(MCAST)[:30]) == 0  # too short
    arp = frame(MCAST)[:12] + b"\x08\x06" + frame(MCAST)[14:]
    assert run_bpf(prog, arp) == 0
    later = bytearray(frame(MCAST))
    later[20:22] = struct.pack("!H", 0x0005)  # fragment offset 5: no UDP header
    assert run_bpf(prog, bytes(later)) == 0
    opts = bytearray(frame(MCAST))  # IP header with 4 bytes of options (IHL 6)
    opts[14] = 0x46
    opts[34:34] = bytes(4)
    assert run_bpf(prog, bytes(opts)) == m2u_check.SNAP


class FakeView:
    """A packet socket: frames queued by the test, kernel drops set by the test."""

    def __init__(self) -> None:
        self.frames: list[tuple[bytes, int]] = []
        self.drops = 0
        self.closed = False

    def settimeout(self, s: float) -> None:
        pass

    def recvfrom(self, n: int) -> tuple[bytes, tuple[str, int, int]]:
        if self.closed:
            raise OSError("closed")
        if not self.frames:
            raise TimeoutError
        data, kind = self.frames.pop(0)
        return data[:n], ("wlan0", 0x0800, kind)

    def getsockopt(self, level: int, opt: int, size: int) -> bytes:
        drops, self.drops = self.drops, 0
        return struct.pack("II", 0, drops)

    def close(self) -> None:
        self.closed = True


def test_m2u_counter_splits_multicast_and_converted_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    monkeypatch.setattr(m2u_check, "own_mac", lambda nic: OWN)
    view = FakeView()
    c = m2u_check.M2uCounter("wlan0", "239.255.81.1", 5004, opener=lambda n, g, p: view)
    view.frames += [(frame(MCAST), socket.PACKET_MULTICAST)] * 3
    view.frames += [(frame(OWN), socket.PACKET_HOST)] * 2
    view.frames += [(frame("ff:ff:ff:ff:ff:ff"), socket.PACKET_BROADCAST)]  # neither
    view.frames += [(frame(MCAST), socket.PACKET_OUTGOING)]  # sent by this PC: not counted
    end = time.monotonic() + 2
    while view.frames and time.monotonic() < end:
        time.sleep(0.01)
    time.sleep(0.05)
    assert c.take() == {"fm": 3, "fu": 2}
    assert c.take() == {"fm": 0, "fu": 0}  # a quiet second is a measured 0
    view.frames += [(frame(OWN), socket.PACKET_HOST)]
    view.drops = 4  # the kernel dropped frames of the view: a partial count is not given
    time.sleep(0.3)
    assert c.take() == {}
    c.close()
    assert view.closed and not c._thread.is_alive()


def test_m2u_counter_without_permission_raises_oserror() -> None:
    def no(nic: str, group: str, port: int) -> FakeView:
        raise PermissionError("CAP_NET_RAW")

    with pytest.raises(OSError):
        m2u_check.M2uCounter("wlan0", "239.255.81.1", 5004, opener=no)
