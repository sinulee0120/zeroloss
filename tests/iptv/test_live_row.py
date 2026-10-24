"""IPTV device row (docs/11-ui.md 3, decided 2026-09-24): Jitter = last second, Loss =
running total, icon = last second at once + the worst color of the open 5-min cell."""

from __future__ import annotations

import random
from typing import Any

from modules.iptv.run import live_row, validate

PLAN = validate(
    {
        "schema_version": 1,
        "module": "iptv",
        "test_type": "pc",
        "devices": {"sender": "tx", "receivers": [{"device": "rx", "ch": 2}]},
        "quality": "HD",
        "duration_s": 600,
        "settings": {},
        "files": {"2": "/data/ch2.ts"},
    }
)
WARN = PLAN.jitter_warn_ms


def rx(
    e: int = 10, lost: int = 0, jit: float = 1.0, hd: int | None = 0, **over: Any
) -> dict[str, Any]:
    last: dict[str, Any] = {
        "type": "s",
        "e": e,
        "ch": 2,
        "rx": 900,
        "lost": lost,
        "jit": jit,
        "br": 10_000_000,
    }
    if hd is not None:
        last["hd"] = hd
    r = {"id": "rx", "ch": 2, "state": "RUNNING", "measuring": True, "lost_total": 0}
    r |= {"last_1s": last} | over
    return {"sender": {"channels": []}, "receivers": [r], "tablets": []}


def texts(row: dict[str, Any]) -> list[str]:
    return [f"{i['label']}={i['value']}" for i in row["items"]]


def test_receiver_row_values() -> None:
    row = live_row(PLAN, rx(jit=2.1, lost_total=7), {})
    assert texts(row) == ["CH2=None", "Jitter=2.1", "Loss=7"]
    assert [i["key"] for i in row["items"]] == [False, True, True]
    assert row["color"] == "green" and row["link_label"] == "LAN"


def test_sender_row_sums_running_channels() -> None:
    chans = [
        {"ch": c, "state": "RUNNING", "tx_bitrate_bps": 35_000_000, "restarts": 0}
        for c in (1, 2, 3)
    ]
    chans.append({"ch": 4, "state": "STOPPING", "tx_bitrate_bps": 1, "restarts": 0})
    data = {"sender": {"channels": chans}, "receivers": [], "tablets": []}
    row = live_row(PLAN, data, {})
    assert texts(row) == ["Sending 3 ch ·=105.0"]
    assert row["items"][0]["unit"] == "Mbps" and row["link_label"] == "WAN"


def test_loopback_node_shows_sender_then_receiver() -> None:
    data = rx()
    data["sender"]["channels"] = [{"ch": 2, "state": "RUNNING", "tx_bitrate_bps": 10_000_000}]
    assert texts(live_row(PLAN, data, {}))[:2] == ["Sending 1 ch ·=10.0", "CH2=None"]


def test_warming_up() -> None:
    row = live_row(PLAN, rx(measuring=False, last_1s=None, lost_total=None), {})
    assert texts(row) == ["CH2=None", "Warming up=None"]
    assert row["color"] == "green"


def test_colors_from_the_last_second() -> None:
    assert live_row(PLAN, rx(lost=3, hd=0), {})["color"] == "red"
    assert live_row(PLAN, rx(lost=3, hd=3), {})["color"] == "purple"  # all own drops
    assert live_row(PLAN, rx(lost=3, hd=None), {})["color"] == "red"
    assert live_row(PLAN, rx(jit=WARN), {})["color"] == "yellow"
    assert live_row(PLAN, rx(jit=WARN - 0.1), {})["color"] == "green"
    assert live_row(PLAN, rx(lost=1), {})["why"]


def test_bad_color_holds_until_the_cell_ends() -> None:
    memo: dict[str, Any] = {}
    assert live_row(PLAN, rx(e=10, lost=1), memo)["color"] == "red"
    assert live_row(PLAN, rx(e=11), memo)["color"] == "red"
    assert live_row(PLAN, rx(e=299), memo)["color"] == "red"
    assert live_row(PLAN, rx(e=300), memo)["color"] == "green"  # next cell
    assert live_row(PLAN, rx(e=301, jit=WARN), memo)["color"] == "yellow"
    assert live_row(PLAN, rx(e=302, lost=2, hd=2), memo)["color"] == "purple"  # worse
    assert live_row(PLAN, rx(e=303, jit=WARN), memo)["color"] == "purple"


def test_random_seconds_never_better_than_the_worst_in_the_cell() -> None:
    """Monkey: whatever the order, the shown color is the worst seen in the open cell."""
    order = ["green", "yellow", "purple", "red"]
    for seed in range(50):
        rnd = random.Random(seed)
        memo: dict[str, Any] = {}
        worst: dict[int, int] = {}
        e = 0
        for _ in range(400):
            e += rnd.choice([0, 1, 1, 1, 2, 40])  # repeats, gaps, jumps
            lost = rnd.choice([0, 0, 0, 0, 1, 5])
            hd = rnd.choice([0, lost, None])
            jit = rnd.choice([1.0, WARN, 200.0])
            got = live_row(PLAN, rx(e=e, lost=lost, jit=jit, hd=hd), memo)["color"]
            now = "red" if lost and (hd is None or hd < lost) else "purple" if lost else None
            now = now or ("yellow" if jit >= WARN else "green")
            cell = e // PLAN.cell_s
            worst[cell] = max(worst.get(cell, 0), order.index(now))
            assert got == order[worst[cell]], (seed, e)


def test_playing_only_from_the_players_own_report() -> None:
    for pb, text in [
        ("ok", "▶ Playing"),
        ("drop", "▶ Playing"),
        ("freeze", "■ Not playing"),
        ("none", "■ Not playing"),
        ("restart", "■ Not playing"),
    ]:
        data = rx()
        data["receivers"][0]["last_1s"]["pb"] = pb
        assert live_row(PLAN, data, {})["items"][-1]["label"] == text
    assert all("Playing" not in i["label"] for i in live_row(PLAN, rx(), {})["items"])


def test_cell_values_are_the_boxs_own_numbers() -> None:
    """GAP-146/147: what each 5-minute box shows, in the reference board's order (loss
    count and %, MLR, jitter, bitrate avg/min, packets, Seq R/D/L/G/E), without the
    Controller naming any field; a value not measured is None, never 0."""
    from modules.iptv.run import cell_values

    s = {"duration_s": 300, "loss": {"lost": 3, "expected": 285003, "received": 285000},
         "jitter": {"max_ms": 4.2, "avg_ms": 2.1},
         "bitrate_bps": {"avg": 10_090_000, "min": 9_910_000}}  # fmt: skip
    got = {v["key"]: v["value"] for v in cell_values(s)}
    assert got == {"lost": 3, "loss_pct": 0.0011, "mlr": 0.01, "jitter_max": 4.2,
                   "jitter_avg": 2.1, "bitrate_avg": 10.09, "bitrate_min": 9.91,
                   "received": 285000, "reordered": None, "duplicates": None, "late": None,
                   "gaps": None, "cc_errors": None}  # fmt: skip
    seq = {"loss": {"reordered_recovered": 1, "duplicates": 2, "late_discarded": 3,
                    "loss_events": 4}, "ts": {"cc_err": 5}}  # fmt: skip
    got = {v["key"]: v["value"] for v in cell_values(seq)}
    assert [got[k] for k in ("reordered", "duplicates", "late", "gaps", "cc_errors")] == [
        1, 2, 3, 4, 5]  # fmt: skip
    empty = {v["key"]: v["value"] for v in cell_values({})}
    assert empty["lost"] is None and empty["loss_pct"] is None and empty["mlr"] is None
    assert all("bad" in v for v in cell_values({}))


def test_a_troubled_second_says_what_this_pc_counted() -> None:
    """GAP-149 (the reference's host_path_notes): evidence lines, never a judge; an
    absent counter says nothing."""
    from modules.iptv.run import second_evidence, stream_form

    assert second_evidence({"rx": 900, "lost": 3}) == []  # nothing measured: nothing said
    lines = second_evidence({"rx": 0, "nd": 4, "ne": 1, "ub": 2, "fm": 0, "fu": 0})
    assert len(lines) == 4 and "dropped 4" in lines[0] and "before this PC" in lines[3]
    assert stream_form([{"rx": 1}]) is None  # the card did not say
    m2u = stream_form([{"fm": 0, "fu": 950}, {"fm": 3, "fu": 948}])
    assert m2u == {"form": "unicast", "multicast_frames": 3, "unicast_frames": 1898,
                   "seconds": 2}  # fmt: skip
    assert stream_form([{"fm": 950, "fu": 0}])["form"] == "multicast"  # type: ignore[index]
