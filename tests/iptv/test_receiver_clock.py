"""The receiver's arrival clock and the sender's priority on every OS (2026-11-21 review:
Windows' monotonic clock moves in 15.6 ms steps, and a Windows sender stayed at the
normal priority class)."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import pytest

from core.blocks.udp_receiver import clock_ns


def test_the_arrival_clock_resolves_well_under_a_millisecond() -> None:
    steps: list[int] = []
    last = clock_ns()
    while len(steps) < 50:
        now = clock_ns()
        if now != last:
            steps.append(now - last)
            last = now
    assert min(steps) < 1_000_000, steps[:5]  # jitter and delay are counted in ms


@pytest.mark.skipif(sys.platform != "win32", reason="Windows consoles")
def test_a_windows_stop_without_a_shared_console_is_at_once(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-11-21 review: the windowed Controller has no console, its children get a
    hidden one and never see CTRL_BREAK; every stop waited 15 s (a tsp restart sent
    nothing for ~20 s). Now it is ended at once and nothing is left."""
    import time

    from core.blocks import process_runner as pr

    monkeypatch.setattr(pr, "_has_console", lambda: False)  # as the windowed Controller
    r = pr.ProcessRunner("sleeper", [sys.executable, "-c", "import time; time.sleep(60)"],
                         tmp_path)  # fmt: skip
    r.start()
    pid = r.pid
    t0 = time.monotonic()
    r.stop()
    assert time.monotonic() - t0 < 5
    gone = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                          text=True).stdout  # fmt: skip
    assert str(pid) not in gone


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows priority class")
def test_a_windows_player_gives_way_when_the_pc_is_busy(tmp_path: Any) -> None:
    """RCV-016 on Windows (2026-11-21 review: it never gave way there)."""
    import ctypes

    from core.blocks.process_runner import ProcessRunner
    from modules.iptv.receiver.player import VlcPlayer

    r = ProcessRunner("sleeper", [sys.executable, "-c", "import time; time.sleep(60)"],
                      tmp_path)  # fmt: skip
    r.start()
    try:
        assert VlcPlayer._nice(r)
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(0x1000, False, r.pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            assert k.GetPriorityClass(h) == 0x4000  # BELOW_NORMAL_PRIORITY_CLASS
        finally:
            k.CloseHandle(h)
    finally:
        r.stop()


def _closed_port() -> int:
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


def test_a_send_to_a_closed_port_does_not_end_receiving() -> None:
    """Windows (2026-11-21 review): a prime sent before the sender's port was open made the
    next receive fail (WSAECONNRESET) and ended the receiver's thread (RCV-018)."""
    from core.blocks.udp_receiver import UdpReceiver

    with UdpReceiver(0, None, "127.0.0.1") as rx:
        rx.send_to(b"ZLPRIME R1 1 0", "127.0.0.1", _closed_port())
        assert rx.receive(0.3) is None  # no packet, no error
        rx.send_to(b"hello", "127.0.0.1", rx.local_port)
        got = rx.receive(1.0)
        assert got is not None and got.data == b"hello"  # and it receives on


def test_a_card_whose_network_is_gone_gives_no_packet_not_an_error() -> None:
    """2026-11-21 review: a cable out or a Wi-Fi gone for a moment can fail the receive
    itself (Windows: WSAENETDOWN); it ended the receive thread (RCV-018) and the test.
    Now it is no packet: the silent seconds say it and the group is joined again."""
    import errno

    from core.blocks.udp_receiver import UdpReceiver

    class _Gone:
        def __init__(self, err: OSError) -> None:
            self.err = err

        def settimeout(self, t: float) -> None:
            return

        def recv_into(self, buf: bytearray) -> int:
            raise self.err

        def recvmsg_into(self, bufs: list[bytearray], anc: int) -> tuple[int, list[Any], int, Any]:
            raise self.err

    with UdpReceiver(0, None, "127.0.0.1") as rx:
        real = rx._sock
        for err in (OSError(errno.ENETDOWN, "Network is down"), OSError(10050, "WSAENETDOWN")):
            rx._sock = _Gone(err)  # type: ignore[assignment]
            assert rx.receive(0.1) is None
        rx._sock = _Gone(OSError(errno.EBADF, "Bad file descriptor"))  # type: ignore[assignment]
        with pytest.raises(OSError):
            rx.receive(0.1)  # anything else is still an error
        rx._sock = real


def test_discovery_goes_on_past_a_target_without_zeroloss() -> None:
    """Windows (2026-11-21 review): a target that answered "port unreachable" made the
    search fail as a whole."""
    import socket
    import threading

    from core.blocks import discovery

    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    srv.settimeout(2)

    def answer() -> None:
        data, src = srv.recvfrom(256)
        nonce = discovery.nonce_of(data)
        srv.sendto(f'{{"zl": "discover", "v": 1, "nonce": "{nonce}"}}'.encode(), src)

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    closed = _closed_port()
    got = discovery.ask(["127.0.0.1"], closed, wait_s=0.5, tries=1)  # nobody: no error
    assert got == []
    port = srv.getsockname()[1]
    got = discovery.ask(["127.0.0.1"], port, wait_s=1.0, tries=1)
    assert [src for _, src in got] == ["127.0.0.1"]
    srv.close()


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows card list")
def test_a_windows_card_says_its_prefix_for_the_subnet_check() -> None:
    """SND-010 on Windows (2026-11-21 review: the prefix was unknown there, so a Windows
    sender behind the NAT was never checked for the router WAN subnet)."""
    from core.blocks import win_links

    assert win_links.prefix_of("127.0.0.1") == 8
    for a in win_links.adapters():
        if a["ip"]:
            assert 1 <= a["prefix"] <= 32, a
            assert win_links.prefix_of(a["ip"]) == a["prefix"]


@pytest.mark.skipif(sys.platform != "win32", reason="mpv's console wrapper is Windows'")
def test_the_windows_player_is_mpv_exe_not_its_console_wrapper() -> None:
    import shutil

    from modules.iptv.receiver.player import mpv_binary

    if not shutil.which("mpv.exe"):
        pytest.skip("needs mpv")
    assert mpv_binary().lower().endswith("mpv.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows priority class")
def test_a_windows_sender_gets_the_high_priority_class() -> None:
    code = "from core.blocks import priority; print(priority.first())"
    got = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, timeout=30)  # fmt: skip
    assert got.stdout.strip() == "high (Windows priority class)", got
