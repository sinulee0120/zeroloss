"""Trouble windows split a 5-minute cell (docs/05-standards.md 5.3 "문제 구간으로 나누기",
09 SND-016/017, NET-015, 20 GAP-68).

Loss, P1 errors and jitter inside a sender trouble window are the tool's (purple), inside
a wired receiver's link-down window cannot be judged (grey); outside they stay the
network's (red). The window is the event's second -1 s to its end +2 s.
"""

from __future__ import annotations

import dataclasses
import random
from typing import Any

import pytest

from core.blocks.color_judge import Color
from modules.iptv.run import IptvPlan, cell_color, cell_trouble, on_test_nic, trouble_of, validate

T0 = 1000.0  # Controller time of elapsed second 0


def summary(lost: int = 0, dur: int = 300, jit: float = 2.0, p1: int = 0, **kw: Any
            ) -> dict[str, Any]:  # fmt: skip
    rate = 3000
    return {
        "start_elapsed_s": 0, "duration_s": dur, "partial": dur < 300, "ch": 1,
        "device": "rx",
        "loss": {"received": rate * dur - lost, "lost": lost},
        "jitter": {"max_ms": jit, "delay_low_ms": 0.0, "delay_high_ms": 1.0},
        "ts": {"p1_errors": p1}, "bitrate_bps": {"avg": 35e6},
        "receiver": {"host_drops": kw.get("host_drops", 0)},
    }  # fmt: skip


def seconds(dur: int = 300, missing: set[int] | None = None, **by_e: dict[str, Any]
            ) -> dict[int, tuple[float, dict[str, Any]]]:  # fmt: skip
    out = {}
    for e in range(dur):
        if e in (missing or set()):
            continue
        rec = {"e": e, "lost": 0, "jit": 2.0, **by_e.get(f"e{e}", {})}
        out[e] = (T0 + e, rec)
    return out


def judged(s: dict[str, Any], secs: Any, windows: Any) -> tuple[Color, tuple[str, ...]]:
    s = {**s, "trouble": cell_trouble(s, secs, windows)}
    return cell_color(s, 50.0)


def test_loss_inside_a_sender_window_is_purple() -> None:
    w = {"sender": [(T0 + 49, T0 + 52)]}
    color, why = judged(summary(lost=5), seconds(e50={"lost": 5}), w)
    assert color is Color.PURPLE and "sender trouble for 4 s (lost 5" in why[0]


def test_loss_outside_stays_red_in_the_same_cell() -> None:
    """The user's choice: only the window, not the whole cell (a real DUT loss shows)."""
    w = {"sender": [(T0 + 49, T0 + 52)]}
    color, why = judged(summary(lost=6), seconds(e50={"lost": 5}, e200={"lost": 1}), w)
    assert color is Color.RED and why[0] == "network loss 1"
    assert any("sender trouble" in x for x in why)


def test_jitter_and_p1_inside_the_window() -> None:
    w = {"sender": [(T0 + 10, T0 + 12)]}
    secs = seconds(e11={"lost": 2, "jit": 400.0, "p1": 3})
    color, _ = judged(summary(lost=2, jit=400.0, p1=3), secs, w)
    assert color is Color.PURPLE  # 400 ms and P1 happened while the sender stalled
    secs = seconds(e11={"lost": 2, "jit": 400.0, "p1": 3}, e100={"jit": 180.0})
    color, why = judged(summary(lost=2, jit=400.0, p1=3), secs, w)
    assert color is Color.RED and "jitter 180.0 ms > 150 ms" in why


def test_a_short_cell_because_the_sender_stopped_is_not_no_reception() -> None:
    w = {"sender": [(T0 + 0, float("inf"))]}  # tsp died and never came back
    s = summary(dur=300) | {"loss": {"received": 10, "lost": 0}}
    assert judged(s, seconds(), w)[0] is Color.PURPLE


def test_wired_receiver_link_down_is_grey() -> None:
    w = {"receiver_link": [(T0 + 99, T0 + 110)]}
    color, why = judged(summary(lost=900), seconds(e100={"lost": 900}), w)
    assert color is Color.GREY and why[0].startswith("receiver link down for 12 s (lost 900")


def test_sender_slow_is_a_warning() -> None:
    w = {"sender_slow": [(T0 + 20, T0 + 23)]}
    color, why = judged(summary(), seconds(), w)
    assert color is Color.YELLOW and "sender slow for 4 s" in why


def test_no_window_no_trouble() -> None:
    assert cell_trouble(summary(), seconds(), {}) is None
    assert cell_trouble(summary(), seconds(), {"sender": [(T0 + 500, T0 + 510)]}) is None
    assert judged(summary(lost=1), seconds(e3={"lost": 1}), {})[0] is Color.RED


def test_missed_seconds() -> None:
    """Loss in seconds the heartbeat missed goes to a window only if every missed
    second is inside one."""
    w = {"sender": [(T0 + 49, T0 + 52)]}
    inside = judged(summary(lost=4), seconds(missing={50}), w)
    assert inside[0] is Color.PURPLE  # the only missed second is in the window
    both = judged(summary(lost=4), seconds(missing={50, 200}), w)
    assert both[0] is Color.RED  # cannot tell where the 4 were lost
    tr = cell_trouble(summary(lost=4), seconds(missing={50}), w)
    assert tr is not None and (tr["unknown_lost"], tr["unknown_in"]) == (4, "sender")


def test_monkey_never_hides_loss_outside_windows() -> None:
    """Random windows, losses and missed seconds: a known loss outside every window
    always makes the cell red; a cell whose only loss is inside sender windows is never
    red."""
    rnd = random.Random(68)
    for seed in range(300):
        rnd.seed(seed)
        wins = []
        for _ in range(rnd.randint(0, 3)):
            a = rnd.randint(0, 295)
            wins.append((T0 + a - 1, T0 + a + rnd.randint(0, 6) + 2))
        lost_at = {rnd.randint(0, 299): rnd.randint(1, 50) for _ in range(rnd.randint(0, 4))}
        by_e: dict[str, Any] = {f"e{e}": {"lost": n} for e, n in lost_at.items()}
        secs = seconds(**by_e)
        s = summary(lost=sum(lost_at.values()))
        color, _ = judged(s, secs, {"sender": wins})
        inside = {e for e in lost_at if any(a <= T0 + e <= b for a, b in wins)}
        if set(lost_at) - inside:
            assert color is Color.RED, seed
        elif lost_at:
            assert color is Color.PURPLE, seed


@pytest.fixture
def plan() -> IptvPlan:
    p = validate({
        "schema_version": 1, "module": "iptv", "test_type": "pc", "quality": "HD",
        "duration_s": 60,
        "devices": {"sender": "tx", "receivers": [{"device": "rx", "ch": 1}]},
        "nics": {"tx": "10.0.0.2", "rx": "192.168.1.10"},
        "settings": {"S-14": {"groups": ["239.1.1.1", "239.1.1.2", "239.1.1.3"], "port": 5004}},
    })  # fmt: skip
    # plus a Wi-Fi receiver PC (as a tablet-style multi-receiver plan would have)
    return dataclasses.replace(p, receivers=(("rx", 1), ("wrx", 2)),
                               nics={**p.nics, "wrx": "wifi"})  # fmt: skip


def test_which_events_open_which_window(plan: IptvPlan) -> None:
    down = {"nic": "eth0", "kind": "down", "after": {"ip": "192.168.1.10", "link_mbps": 0}}
    assert trouble_of(plan, "tx", "ZL-SND-005", {}) == ("sender", True)
    assert trouble_of(plan, "tx", "ZL-SND-016", {}) == ("sender", False)
    assert trouble_of(plan, "tx", "ZL-SND-025", {}) == ("sender", False)  # pacer late
    assert trouble_of(plan, "tx", "ZL-SND-017", {}) == ("sender_slow", False)
    assert trouble_of(plan, "rx", "ZL-SND-016", {}) is None  # only the sender's
    assert trouble_of(plan, "rx", "ZL-NET-015", down) == ("receiver_link", True)
    assert trouble_of(plan, "rx", "ZL-NET-015", down | {"kind": "flap"}) == (
        "receiver_link", False)  # fmt: skip
    other = down | {"after": {"ip": "172.16.0.5", "link_mbps": 0}}
    assert trouble_of(plan, "rx", "ZL-NET-015", other) is None  # not the test NIC
    tx_down = {"nic": "eth1", "kind": "down", "after": {"ip": "10.0.0.2"}}
    assert trouble_of(plan, "tx", "ZL-NET-015", tx_down) == ("sender", True)
    wifi = {"nic": "wlp4s0", "kind": "down", "after": {"ip": None, "wifi": {"bssid": None}}}
    assert trouble_of(plan, "wrx", "ZL-NET-015", wifi) is None  # AP drop: normal rules
    assert trouble_of(plan, "rx", "ZL-RCV-020", {}) is None


def test_pc_busy_is_a_warning_only(plan: IptvPlan) -> None:
    """09 SYS-003: a busy sender or receiver PC -> yellow, never hides a loss."""
    assert trouble_of(plan, "tx", "ZL-SYS-003", {}) == ("busy", True)
    assert trouble_of(plan, "rx", "ZL-SYS-003", {}) == ("busy", True)
    assert trouble_of(plan, "stranger", "ZL-SYS-003", {}) is None
    w = {"busy": [(T0 + 30, T0 + 45)]}
    color, why = judged(summary(), seconds(), w)
    assert color is Color.YELLOW and "PC busy for 16 s" in why
    assert judged(summary(lost=1), seconds(e35={"lost": 1}), w)[0] is Color.RED


def test_screen_locked_is_a_warning_for_that_receiver(plan: IptvPlan) -> None:
    """09 RCV-006: the receiver PC's screen locked -> yellow on its cells, never hides loss."""
    assert trouble_of(plan, "rx", "ZL-RCV-006", {}) == ("screen", True)
    assert trouble_of(plan, "tx", "ZL-RCV-006", {}) is None
    w = {"screen": [(T0 + 60, T0 + 69)]}
    color, why = judged(summary(), seconds(), w)
    assert color is Color.YELLOW and "screen locked for 10 s" in why
    assert judged(summary(lost=2), seconds(e61={"lost": 2}), w)[0] is Color.RED


def test_which_nic_is_the_test_nic(plan: IptvPlan) -> None:
    """09 NET-015: only the NIC the plan names for that PC is a problem of the test."""
    mine = {"nic": "eth0", "after": {"ip": None}, "before": {"ip": "192.168.1.10"}}
    other = {"nic": "wlp4s0", "after": {"ip": "192.168.0.13", "wifi": {}}}
    assert on_test_nic(plan, "rx", mine) and not on_test_nic(plan, "rx", other)
    wifi = {"nic": "wlp4s0", "after": {"ip": None, "wifi": {}}}
    assert on_test_nic(plan, "wrx", wifi)  # a Wi-Fi receiver: the wireless NIC is the one
    assert not on_test_nic(plan, "wrx", mine)  # ... its wired NIC is not
    assert on_test_nic(plan, "stranger", other)  # no NIC in the plan: nothing is hidden
