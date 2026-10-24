"""One primed sending flow (docs/modules/iptv.md 4.5, S-18, SND-021).

A development/SDK router has no IGMP proxy, so multicast from the WAN side never
reaches the LAN. The receiver sends a small "prime" from its receive port to our port
Q (and to P, to open port-restricted NATs); we send the channel as unicast RTP from
port P (tsp ``--local-port``) to the address:port the prime came from, which is the
router's WAN address and the port its NAT picked. No prime for 30 s stops the flow; a
new source address moves it. tsp itself is the plain SenderSession.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from core.events.bus import Event, EventBus, Level
from core.lifecycle.state_flow import State, StateFlow
from core.protocol.envelope import ZlError
from modules.iptv.sender.session import SenderConfig, SenderSession

PRIME_LOST_S = 30.0  # iptv 4.5: no prime this long stops the flow
RETRY_S = 10.0  # a failed tsp start is tried again at most this often
PRIME_TAG = b"ZLPRIME"
STOP_TAG = b"STOP"


@dataclass(frozen=True)
class PrimeFlow:
    run_id: str
    flow: int  # 0 analyzer, 1 player
    p: int  # tsp sends from here
    q: int  # primes arrive here


def prime_payload(run_id: str, ch: int, flow: int) -> bytes:
    return f"ZLPRIME {run_id} {ch} {flow}".encode("ascii")


def stop_payload(run_id: str, ch: int, flow: int) -> bytes:
    """The receiver left the channel (03 F19): stop now, not after PRIME_LOST_S."""
    return prime_payload(run_id, ch, flow) + b" " + STOP_TAG


def _parse(parts: list[bytes]) -> tuple[str, int, int] | None:
    try:
        return parts[1].decode("ascii"), int(parts[2]), int(parts[3])
    except (UnicodeDecodeError, ValueError):
        return None


def parse_prime(data: bytes) -> tuple[str, int, int] | None:
    parts = data.split()
    if len(parts) != 4 or parts[0] != PRIME_TAG:
        return None
    return _parse(parts)


def parse_stop(data: bytes) -> tuple[str, int, int] | None:
    parts = data.split()
    if len(parts) != 5 or parts[0] != PRIME_TAG or parts[4] != STOP_TAG:
        return None
    return _parse(parts)


class PrimedSender:
    def __init__(
        self,
        cfg: SenderConfig,
        prime: PrimeFlow,
        data_dir: Path,
        bus: EventBus,
        procs_dir: Path | None = None,
        lost_s: float = PRIME_LOST_S,
    ) -> None:
        self.cfg = replace(cfg, local_port=prime.p, tag=f"_f{prime.flow}")
        self.prime = prime
        self.bus = bus
        self.lost_s = lost_s
        self.flow = StateFlow(f"primed sender ch{cfg.ch} f{prime.flow}", self._changed)
        self.dest: tuple[str, int] | None = None
        self.last_prime: float | None = None
        self.primes = 0
        self._data_dir = data_dir
        self._procs = procs_dir
        self._session: SenderSession | None = None
        self._sock: socket.socket | None = None
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_try = 0.0

    @property
    def ssrc(self) -> int:
        return self._session.ssrc if self._session else 0

    def start(self) -> None:
        """Open Q and return at once: the stream starts when the first prime comes."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind((self.cfg.local_address or "0.0.0.0", self.prime.q))
        except OSError as e:
            sock.close()
            raise ZlError(
                "SND-021", f"CH{self.cfg.ch} cannot wait for primes on port {self.prime.q}",
                str(e), "Another program uses the port; stop it and start again.",
            ) from e  # fmt: skip
        sock.settimeout(0.5)
        self._sock = sock
        self.flow.move(State.STARTING, reason="waiting for the receiver's prime")
        self._halt.clear()
        self._thread = threading.Thread(target=self._loop, name="iptv-prime", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.flow.move(State.STOPPING, reason="stop")
        self._halt.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(self.cfg.start_limit_s + 10)  # a tsp start in progress ends
        ok = self._stop_session()
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.flow.move(State.IDLE if ok else State.FAULT, reason="stopped")

    def status(self) -> dict[str, Any]:
        s = self._session
        base = s.status() if s else {
            "ch": self.cfg.ch, "group": None, "state": self.flow.state.value,
            "tx_bitrate_bps": None, "restarts": 0, "tx_packets": None,
            "interval_dev_ms": None, "source": self.cfg.source,
        }  # fmt: skip
        age = round(time.monotonic() - self.last_prime, 1) if self.last_prime else None
        return {
            **base,
            "state": self.flow.state.value,
            "prime": {
                "flow": self.prime.flow,
                "dest": _text(self.dest),
                "last_prime_s": age,
                "primes": self.primes,
            },
        }

    # ---------------------------------------------------------------- loop

    def _loop(self) -> None:
        sock = self._sock
        assert sock is not None
        while not self._halt.is_set():
            try:
                data, src = sock.recvfrom(512)
            except TimeoutError:
                data, src = b"", None
            except OSError:
                return  # closed by stop()
            mine = (self.prime.run_id, self.cfg.ch, self.prime.flow)
            if src is not None and parse_prime(data) == mine:
                self.last_prime = time.monotonic()
                self.primes += 1
                self._follow((str(src[0]), int(src[1])))
            elif src is not None and parse_stop(data) == mine:
                self._left((str(src[0]), int(src[1])))
            self._check_lost()
            self._check_fault()

    def _left(self, src: tuple[str, int]) -> None:
        """The receiver changed channel: its flow ends now, no SND-021 later."""
        if src != self.dest:
            return  # not the address we send to: an old or a stray packet
        self.dest = None
        self.last_prime = None
        self._stop_session()
        self._event("ZL-INFO", "info", f"CH{self.cfg.ch} receiver left, flow stopped")

    def _follow(self, src: tuple[str, int]) -> None:
        running = self._session is not None and self._session.flow.state is State.RUNNING
        if src == self.dest and running:
            return
        if src != self.dest and self.dest is not None:
            moved = f"prime now from {_text(src)} (was {_text(self.dest)})"
            self._event("ZL-SND-021", "warn", f"CH{self.cfg.ch} {moved}")
        if src == self.dest and time.monotonic() < self._next_try:
            return  # a failed start is not tried again on every prime
        self._stop_session()
        self.dest = src
        session = SenderSession(
            replace(self.cfg, dest_ip=src[0], port=src[1]), self._data_dir, self.bus, self._procs
        )
        try:
            session.start()
        except ZlError:
            self._next_try = time.monotonic() + RETRY_S
            return  # SND-019 / SND-001 already published by the session
        self._session = session  # stop() waits for this thread, then stops it
        self.last_prime = time.monotonic()  # the start took seconds: that is not silence
        if self._halt.is_set():
            return
        if self.flow.state is State.STARTING:
            self.flow.move(State.RUNNING, reason=f"sending to {_text(src)}")

    def _check_lost(self) -> None:
        if self._session is None or self.last_prime is None:
            return
        silent = time.monotonic() - self.last_prime
        if silent >= self.lost_s:
            self._event("ZL-SND-021", "error",
                        f"CH{self.cfg.ch} no prime for {silent:.0f} s, flow stopped")  # fmt: skip
            self._stop_session()
            self.dest = None

    def _check_fault(self) -> None:
        s = self._session
        if s is not None and s.flow.state is State.FAULT and self.flow.state is State.RUNNING:
            self.flow.move(State.FAULT, reason="tsp failed")

    def _stop_session(self) -> bool:
        s, self._session = self._session, None
        if s is None or s.flow.state not in (State.STARTING, State.RUNNING, State.FAULT):
            return True
        s.stop()
        after: State = s.flow.state  # stop() moved it (mypy keeps the narrowing above)
        return after is State.IDLE

    def _event(self, code: str, level: Level, text: str) -> None:
        data = {"module": "iptv", "ch": self.cfg.ch, "flow": self.prime.flow}
        self.bus.publish(Event(code, level, text, data=data))

    def _changed(self, name: str, old: State, new: State, reason: str) -> None:
        self._event("ZL-INFO", "info", f"{name}: {old} -> {new} ({reason})")


def _text(addr: tuple[str, int] | None) -> str | None:
    return f"{addr[0]}:{addr[1]}" if addr else None
