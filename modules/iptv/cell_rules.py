"""IPTV rule table for one 5-minute cell (docs/05-standards.md 5.3).

Fixed pass criteria (CLAUDE.md, never lowered): loss 0, sequence errors 0,
jitter <= 150 ms, at least 1,000 packets and 100 kbps.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.blocks.color_judge import Color
from modules.iptv.defaults import DEFAULTS

JITTER_LIMIT_MS = 150.0
MIN_PACKETS = 1000
MIN_BITRATE_BPS = 100_000
DEFAULT_JITTER_WARN_MS = float(DEFAULTS["S-23"])
PDV_WARN_MS = 40.0  # DVB TS 102 034 7.2.1.1: network jitter max 40 ms peak-to-peak


@dataclass(frozen=True)
class CellInput:
    has_data: bool
    full_cell: bool = True  # minimum 1,000 packets applies to full cells only (05 2.1)
    received: int = 0
    bitrate_avg_bps: float = 0.0
    lost: int = 0  # includes packets that arrived after the reorder window
    host_drops: int = 0  # receiver's own socket/NIC drops in the same cell
    jitter_max_ms: float = 0.0
    pdv_pp_ms: float | None = None  # transit delay range in the cell (RFC 5481 PDV)
    p1_errors: int = 0  # TR 101 290 priority 1, including missing PAT
    # 05 5.3 "문제 구간으로 나누기": what happened inside trouble windows (Controller-made)
    sender_s: int = 0  # seconds of this cell inside a sender trouble window (purple)
    sender_lost: int = 0
    sender_p1: int = 0
    link_s: int = 0  # seconds inside a wired receiver link-down window (grey)
    link_lost: int = 0
    link_p1: int = 0
    slow_s: int = 0  # seconds inside a sender-slow window (yellow)
    busy_s: int = 0  # seconds inside a PC-busy window, SYS-003 (yellow)
    screen_s: int = 0  # seconds the receiver PC's screen was locked, RCV-006 (yellow)
    jitter_outside_ms: float | None = None  # jitter max outside the windows (None: no split)
    playback_issue_with_clean_packets: bool = False
    join_failures: int = 0  # channel changes whose first packet did not come in 5 s (NET-003)
    leave_not_processed: int = 0  # old group kept coming after the Leave (NET-005)
    warnings: tuple[str, ...] = ()  # e.g. "tablet lock released", "non-video traffic"


@dataclass(frozen=True)
class CellResult:
    color: Color
    reasons: tuple[str, ...]
    network_loss: int  # loss attributed to the network after subtracting host drops
    receiver_loss: int


def judge_cell(c: CellInput, jitter_warn_ms: float = DEFAULT_JITTER_WARN_MS) -> CellResult:
    if not c.has_data:
        return CellResult(Color.GREY, ("no data",), 0, 0)

    # Inside trouble windows first (sender -> purple, wired receiver link -> grey), then
    # the receiver's own drops explain what is left; the rest is the network's (red).
    in_windows = min(c.lost, c.sender_lost + c.link_lost)
    rest = c.lost - in_windows
    receiver_loss = min(rest, c.host_drops)
    network_loss = rest - receiver_loss
    p1 = max(0, c.p1_errors - c.sender_p1 - c.link_p1)
    jitter = c.jitter_max_ms if c.jitter_outside_ms is None else c.jitter_outside_ms
    explained = c.sender_s > 0 or c.link_s > 0  # a short cell may be short because of it

    red = []
    if network_loss > 0:
        red.append(f"network loss {network_loss}")
    if jitter > JITTER_LIMIT_MS:
        red.append(f"jitter {jitter:.1f} ms > {JITTER_LIMIT_MS:.0f} ms")
    short = (c.full_cell and c.received < MIN_PACKETS) or c.bitrate_avg_bps < MIN_BITRATE_BPS
    if short and not explained:
        red.append("no reception (below 1,000 packets or 100 kbps)")

    purple = []
    if receiver_loss > 0:
        purple.append(f"receiver-side drops {receiver_loss}")
    if c.sender_s > 0 and (c.sender_lost or c.sender_p1 or short
                           or c.jitter_max_ms > JITTER_LIMIT_MS):  # fmt: skip
        purple.append(f"sender trouble for {c.sender_s} s (lost {c.sender_lost}, "
                      f"P1 {c.sender_p1})")  # fmt: skip
    grey = []
    if c.join_failures:  # the router did not send the new channel, unless something else did
        what = f"channel join failed {c.join_failures}x (no packet within 5 s)"
        if c.sender_s > 0:
            purple.append(f"{what} during sender trouble")
        elif c.link_s > 0:
            grey.append(f"{what} while the receiver link was down")
        else:
            red.append(what)
    if c.link_s > 0 and (c.link_lost or c.link_p1 or short):
        grey.append(f"receiver link down for {c.link_s} s (lost {c.link_lost}, P1 {c.link_p1})")

    # Problems explained by the sender or by the receiver itself are not the router's fault.
    if red and purple and network_loss == 0 and _only_loss_or_reception(red):
        return CellResult(Color.PURPLE, tuple(purple), network_loss, receiver_loss)
    if red:
        return CellResult(Color.RED, tuple(red + purple + grey), network_loss, receiver_loss)
    if purple:
        return CellResult(Color.PURPLE, tuple(purple + grey), network_loss, receiver_loss)
    if grey:
        return CellResult(Color.GREY, tuple(grey), network_loss, receiver_loss)

    yellow = list(c.warnings)
    if c.leave_not_processed:
        yellow.append(f"Leave not processed by router {c.leave_not_processed}x")
    if c.slow_s > 0:
        yellow.append(f"sender slow for {c.slow_s} s")
    if c.busy_s > 0:
        yellow.append(f"PC busy for {c.busy_s} s")
    if c.screen_s > 0:
        yellow.append(f"screen locked for {c.screen_s} s")
    if c.host_drops > 0:
        yellow.append(f"receiver-side drops {c.host_drops} without loss")
    if c.playback_issue_with_clean_packets:  # 2026-10-10 user (GAP-152): PASS stays
        yellow.append("playback issue with clean packets (receiver side)")
    if p1 > 0:  # pass is jitter and loss only (2026-10-09 user, GAP-132): a warning
        yellow.insert(0, f"TR 101 290 P1 errors {p1} without packet loss")
    if c.jitter_max_ms > jitter_warn_ms:
        yellow.insert(0, f"jitter {c.jitter_max_ms:.1f} ms > {jitter_warn_ms:.0f} ms warning")
    if c.pdv_pp_ms is not None and c.pdv_pp_ms > PDV_WARN_MS:
        what = f"delay variation {c.pdv_pp_ms:.1f} ms > {PDV_WARN_MS:.0f} ms (DVB)"
        if c.sender_s > 0:  # the sender PC itself sent late then: not the router's
            # (2026-10-23: the pacer stalled 171 ms at 11:40:23, SND-025 was told, the cell
            # still said "delay variation 174.5 ms" yellow -- user: "공유기 문제가 아닌데
            # 공유기 문제로 돌리지 않게")
            return CellResult(Color.PURPLE, (f"{what} during sender trouble for "
                                             f"{c.sender_s} s",), 0, 0)  # fmt: skip
        yellow.append(what)
    if yellow:
        return CellResult(Color.YELLOW, tuple(yellow), 0, 0)
    return CellResult(Color.GREEN, (), 0, 0)


def _only_loss_or_reception(red: list[str]) -> bool:
    return all(r.startswith(("network loss", "no reception")) for r in red)
