"""One channel change of the PC receiver (docs/02-data.md 7-2, 03 F19, 20 GAP-90).

Times are seconds on the receiver's ``e`` clock; the Controller moves them to its own
clock with the timeline offset. The zap time is measured to the basis time: normal
playback with PC playback on, the first packet with it off (S-49).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from modules.iptv import record_keys as k

GIVE_UP_S = 30.0  # no normal playback this long after the command: the zap ends without it


def _ms(a: float | None, b: float | None) -> int | None:
    return None if a is None or b is None else max(0, round((b - a) * 1000))


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


@dataclass
class Zap:
    device: str
    from_ch: int
    to_ch: int
    join_method: str
    mode: str
    timeout_s: float
    playback: bool
    t_cmd: float
    t_leave: float | None = None
    t_join: float | None = None
    t_first_packet: float | None = None
    t_first_frame: float | None = None
    t_normal_play: float | None = None
    join_failed: bool = False
    leave_check: str | None = None  # None while the check runs
    t_last_old_packet: float | None = None
    end: str | None = None
    joined: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def basis(self) -> str:
        return "normal_play" if self.playback else "first_packet"

    @property
    def basis_time(self) -> float | None:
        return self.t_normal_play if self.playback else self.t_first_packet

    @property
    def total_ms(self) -> int | None:
        return _ms(self.t_cmd, self.basis_time)

    @property
    def success(self) -> bool | None:
        if self.join_failed:
            return False
        total = self.total_ms
        if total is not None:
            return total <= self.timeout_s * 1000
        return False if self.end == "done" else None

    def ready_to_end(self, now_e: float) -> bool:
        """The line can be written: joined (or failed), Leave checked, basis known."""
        if not self.joined.is_set() or self.leave_check is None:
            return False
        return self.join_failed or self.basis_time is not None or now_e - self.t_cmd >= GIVE_UP_S

    def keys(self) -> dict[str, int]:
        """For the first 1-second line after the zap (02 7); zero values are left out."""
        out = {k.ZAP_COUNT: 1}
        if self.success:
            out[k.ZAP_OK] = 1
        if self.join_failed:
            out[k.ZAP_JOIN_FAILED] = 1
        if self.leave_check == "not_processed":
            out[k.ZAP_LEAVE_NOT_PROCESSED] = 1
        for key, value in (
            (k.ZAP_TOTAL_MS, self.total_ms),
            (k.ZAP_JOIN_MS, _ms(self.t_join, self.t_first_packet)),
            (
                k.ZAP_PLAY_MS,
                _ms(self.t_first_packet, self.t_normal_play) if self.playback else None,
            ),
        ):
            if value is not None:
                out[key] = value
        return out

    def to_line(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "from": self.from_ch,
            "to": self.to_ch,
            "join_method": self.join_method,
            "mode": self.mode,
            "t_cmd": _r(self.t_cmd),
            "t_leave": _r(self.t_leave),
            "t_join": _r(self.t_join),
            "t_first_packet": _r(self.t_first_packet),
            "t_first_frame": _r(self.t_first_frame),
            "t_normal_play": _r(self.t_normal_play),
            "basis": self.basis,
            "timeout_s": self.timeout_s,
            "success": self.success,
            "join_failed": self.join_failed,
            "leave_check": self.leave_check,
            "t_last_old_packet": _r(self.t_last_old_packet),
            "end": self.end,
        }
