"""The live monitor's data (02 12 /live, 02 2-1 ``live``, GAP-119)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from controller import live_page
from controller.devices import DeviceList
from controller.run import LIVE_SECONDS, RunController
from core.events.bus import EventBus

REPO = Path(__file__).resolve().parents[2]


class Rules:
    @staticmethod
    def live_view(p: Any) -> dict[str, Any]:
        return {"columns": [{"key": "jit", "label": "Jitter"}, {"key": "lost", "label": "Lost"}],
                "cell_s": 300}  # fmt: skip


def test_live_keeps_the_last_ten_minutes_and_only_named_fields(tmp_path: Path) -> None:
    rc = RunController(DeviceList(tmp_path / "devices.json"), tmp_path / "rep",
                       REPO / "modules", EventBus())  # fmt: skip
    rc._rules, rc._plan = Rules(), object()
    rc._seconds[("rx", 1)] = {e: (0.0, {"e": e, "jit": 2.0, "lost": 0, "secret": 1})
                              for e in range(900)}  # fmt: skip
    live = rc.live()
    (r,) = live["receivers"]
    assert r["key"] == "rx|1" and len(r["records"]) == LIVE_SECONDS
    assert r["records"][-1] == {"e": 899, "jit": 2.0, "lost": 0}  # nothing it does not name
    assert live["view"]["cell_s"] == 300 and live["state"] == "IDLE"


def test_page_puts_charts_beside_the_values_and_warns_in_amber() -> None:
    """GAP-131/132: wide screens get values left, big charts right; CC/P1 are amber."""
    html = live_page.page()
    assert ".split{grid-template-columns:minmax(380px,2fr) 3fr}" in html
    assert "function isWarn(" in html and "td.warn{" in html


class Iptv:
    @staticmethod
    def live_view(p: Any) -> dict[str, Any]:
        from modules.iptv.run import LIVE_COLUMNS

        return {"columns": LIVE_COLUMNS, "cell_s": 300}

    @staticmethod
    def second_records(plan: Any, device: str, data: dict[str, Any]) -> list[Any]:
        return [(("rx", 1), r) for r in data["recs"]]


def test_trouble_stretches_are_kept_for_the_whole_run(tmp_path: Path) -> None:
    """GAP-138, the first wireless run: Wi-Fi off from e=106, no packets until e=154,
    then 45,517 lost at once; a P1 second at e=900. Each stretch keeps its start, length,
    lost and what, long after its seconds left the 15-minute store."""
    rc = RunController(DeviceList(tmp_path / "devices.json"), tmp_path / "rep",
                       REPO / "modules", EventBus())  # fmt: skip
    rc._rules, rc._plan = Iptv(), object()

    def sec(e: int, rx: int = 950, lost: int = 0, p1: int = 0) -> dict[str, Any]:
        return {"e": e, "rx": rx, "lost": lost, "jit": 2.2, "cc": 0, "p1": p1, "pb": "ok"}

    recs = [sec(e) for e in range(106)] + [sec(106, rx=168, p1=2)]
    recs += [sec(e, rx=0) for e in range(107, 154)] + [sec(154, rx=868, lost=45517)]
    recs += (
        [sec(e) for e in range(155, 900)] + [sec(900, p1=4)] + [sec(e) for e in range(901, 2000)]
    )
    for r in recs:
        rc._keep_seconds("rx", {"recs": [r]})
    (r,) = rc.live()["receivers"]
    first, second = r["episodes"]
    assert (first["start_e"], first["end_e"], first["seconds"]) == (106, 154, 49)
    assert first["values"]["lost"] == 45517 and "no Packets/s" in first["what"]
    assert (second["start_e"], second["seconds"]) == (900, 1)
    assert second["what"] == ["TR 101 290 P1"]
    assert min(r["records"], key=lambda x: x["e"])["e"] > 154  # the seconds themselves are gone


def test_no_second_is_lost_between_two_rounds(tmp_path: Path) -> None:
    """2026-11-27 (R20261127-112905's live page): the round is ~1.1 s and a heartbeat
    carried only the last second, so one in ~9 never came (2147, 2139, 2132, ...). The
    recent ones come with each heartbeat; the offset is still from the newest only."""
    from modules.iptv.run import second_records as records_of

    rc = RunController(DeviceList(tmp_path / "devices.json"), tmp_path / "rep",
                       REPO / "modules", EventBus())  # fmt: skip

    class Rules:
        second_records = staticmethod(records_of)

    rc._rules, rc._plan, rc._run_t0 = Rules(), object(), 0.0

    def sec(e: int) -> dict[str, Any]:
        return {"e": e, "ch": 1, "rx": 950, "lost": 0, "jit": 1.0, "pb": "ok"}

    e = 0
    for step in [1, 1, 1, 1, 1, 1, 1, 1, 2] * 3:  # a second skipped every ninth round
        e += step
        recent = [sec(x) for x in range(max(0, e - 4), e + 1)]
        rc._keep_seconds("rx", {"receivers": [{"id": "rx", "ch": 1, "last_1s": recent[-1],
                                               "recent_1s": recent}]})  # fmt: skip
    kept = rc._seconds[("rx", 1)]
    assert sorted(kept) == list(range(0, e + 1))  # every second, none twice
    assert len(rc._offsets["rx|1"]) == 27  # one sample per round, from the newest second
    old = {"receivers": [{"id": "rx", "ch": 1, "last_1s": sec(e + 1)}]}  # an older node
    rc._keep_seconds("rx", old)
    assert e + 1 in rc._seconds[("rx", 1)]


def test_a_wifi_pc_says_its_band_and_channel_like_a_tablet() -> None:
    """2026-11-27 (R20261127-112905): "Receives on: Wi-Fi wlx… 172.30.1.11 · -31 dBm"
    beside a tablet's "5GHz · ch 153 · 11be · …": the PC's newest second has them too."""
    html = live_page.page()
    assert "function wifiBits(" in html
    assert "cd.wireless&&wifiBits(last,(d.view&&d.view.wifi)||[]).length" in html
