"""IPTV warm-up conditions (docs/05-standards.md 2.1-2 rules 2 and 4)."""

from __future__ import annotations

from core.blocks.warmup_gate import Condition, WarmupGate
from modules.iptv.cell_rules import JITTER_LIMIT_MS
from modules.iptv.defaults import DEFAULTS

CLEAN_SECONDS = 3
START_MAX_S = float(DEFAULTS["S-28"])
CHANNEL_CHANGE_MAX_S = 5.0
JOIN_WAIT_S = 5.0  # first packet after a channel change, else NET-003 (05 2.1-2 rule 4)
STABILIZE_MAX_S = 5.0  # after the first packet: then measuring starts, conditions or not


def make_gate(max_s: float, playback: bool) -> WarmupGate:
    """``playback`` is False when PC playback is off (S-49): no first-frame condition."""
    conditions = [
        Condition("first_packet"),
        Condition("pat_pmt"),
        Condition("clean", stable_s=CLEAN_SECONDS),
    ]
    if playback:
        conditions.append(Condition("first_frame"))
    return WarmupGate(conditions, max_s)


def observe(
    packets: int, lost: int, jitter_max_ms: float, pat_pmt: bool, first_frame: bool | None
) -> dict[str, bool]:
    """Turn one second of receiver values into gate observations.

    ``first_frame`` is None when playback is off.
    """
    seen = {
        "first_packet": packets > 0,
        "pat_pmt": pat_pmt,
        "clean": packets > 0 and lost == 0 and jitter_max_ms <= JITTER_LIMIT_MS,
    }
    if first_frame is not None:
        seen["first_frame"] = first_frame
    return seen
