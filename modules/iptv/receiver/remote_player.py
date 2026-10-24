"""The PC player run by Agent Desktop (docs/03-features.md F20 구현 규칙, 02 2-12).

The installed node service has no screen (D38), so the player (``VlcPlayer``, the same
part as in P2, 20 GAP-39) runs in the logged-in user's Agent Desktop. This stand-in
gives the receiver session the same calls as ``VlcPlayer``: it puts a job on the
node's screen-job list, and reads back what the Desktop reports each second. A
Desktop that stops reporting reads as "not playing" (RCV-020), never as fine.
State flow IDLE -> STARTING -> RUNNING -> STOPPING -> IDLE, FAULT when the Desktop did
not confirm the player gone.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from core.desktop_jobs import DesktopJobs
from core.lifecycle.state_flow import State, StateFlow
from modules.iptv.receiver.player import PlayerConfig, PlaySecond

STOP_WAIT_S = 30.0  # as VlcPlayer.stop: the process must be confirmed gone (12 4-3)
# the worst of several seconds taken at once (a late or doubled report)
_ORDER = ("ok", "audio_gap", "drop", "glitch", "restart", "freeze", "none")


def job_config(cfg: PlayerConfig) -> dict[str, Any]:
    """What the Desktop needs to start the same player (its own folders, 02 2-12)."""
    return {"url": cfg.url, "nic": cfg.nic, "buffer_ms": cfg.buffer_ms, "window": cfg.window,
            "headless": cfg.headless, "lowered": False, "flowing": None,
            "player": cfg.kind}  # fmt: skip


class RemotePlayer:
    def __init__(self, cfg: PlayerConfig, job_id: str, jobs: DesktopJobs,
                 on_event: Callable[[str, str, str], None],
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.cfg = cfg
        self.job_id = job_id
        self._jobs = jobs
        self._event = on_event
        self._clock = clock
        self.flow = StateFlow("player", lambda *a: None)
        self.lowered = False
        self._frozen = False
        self._was_running = False  # the Desktop reported the player running at least once
        self._last_pb = "none"  # a second without a report repeats the last one
        self._flowing: bool | None = None

    def start(self) -> None:
        self.flow.move(State.STARTING, reason="ask Agent Desktop for the player")
        self._jobs.want(self.job_id, "iptv", "player", job_config(self.cfg))
        self.flow.move(State.RUNNING, reason="player job listed")

    def stop(self) -> None:
        self.flow.move(State.STOPPING, reason="stop")
        self._jobs.drop(self.job_id)
        gone = self._jobs.wait_gone(self.job_id, STOP_WAIT_S)
        self._jobs.forget(self.job_id)
        self.flow.move(State.IDLE if gone else State.FAULT, reason="stopped")

    @property
    def first_frame(self) -> bool:
        s = self._jobs.state(self.job_id)
        return bool(s and s.first_frame)

    def data_flowing(self, flowing: bool) -> None:
        """GAP-181: told to the Desktop's player only when it changes (02 2-12 flowing)."""
        if flowing != self._flowing:
            self._flowing = flowing
            self._jobs.change(self.job_id, flowing=flowing)

    def lower_priority(self) -> bool:
        """RCV-016: the Desktop lowers its player; False when no Desktop is there."""
        self.lowered = True
        self._jobs.change(self.job_id, lowered=True)
        return self._jobs.connected

    def take_second(self) -> PlaySecond:
        seconds = self._jobs.take_seconds(self.job_id)
        s = self._jobs.state(self.job_id)
        if s is None or s.state not in ("STARTING", "RUNNING"):  # Desktop gone or no player
            if self._was_running:
                self._was_running = False
                self._event("ZL-RCV-020", "error", "Agent Desktop stopped reporting the player")
            return self._stalled()
        self._was_running = self._was_running or s.state == "RUNNING"
        if not seconds:  # a late report: the same as the last second, counts not measured
            return PlaySecond(self._last_pb)
        pb = max((str(x.get("playback", "none")) for x in seconds),
                 key=lambda v: _ORDER.index(v) if v in _ORDER else len(_ORDER))  # fmt: skip
        if pb not in _ORDER:
            pb = "none"
        out = PlaySecond(
            pb,
            _sum(x.get("dropped") for x in seconds),
            _sum(x.get("audio_gaps") for x in seconds),
            any(bool(x.get("stall_started")) for x in seconds),
            any(bool(x.get("restarted")) for x in seconds),
            _sum(x.get("decode_errors") for x in seconds),
            _sum(x.get("decoder_dropped") for x in seconds),
        )
        self._frozen = pb in ("freeze", "none")
        self._last_pb = pb
        return out

    def _stalled(self) -> PlaySecond:
        started = not self._frozen
        self._frozen = True
        self._last_pb = "none"
        return PlaySecond("none", None, None, started, False)


def _sum(values: Any) -> int | None:
    got = [int(v) for v in values if isinstance(v, int) and not isinstance(v, bool)]
    return sum(got) if got else None
