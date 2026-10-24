"""modules/iptv/receiver/tablet.py with a fake tablet: its records are written and
summarized like a PC receiver's, from the first second with packets and the PAT."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from core.blocks.tablets import TabletError
from core.events.bus import EventBus
from core.protocol.envelope import ZlError
from modules.iptv.receiver.tablet import TabletConfig, TabletReceiver


class FakeTablets:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.records: list[dict[str, Any]] = []
        self.away = False
        self.refuse = False
        self.started_app = 0
        self.paused: list[str] = []
        self.in_front = True

    def pause_timer_scans(self, serial: str) -> list[str]:
        self.paused = ["com.samsung.android.mcfds"]
        return self.paused

    def resume_timer_scans(self, serial: str | None = None) -> list[dict[str, Any]]:
        self.paused = []
        return [{"what": "x", "ok": True, "detail": "put back"}]

    def ensure_app(self, serial: str) -> dict[str, Any]:
        return {"installed": False}

    def start_app(self, serial: str) -> dict[str, Any]:
        self.started_app += 1
        return {"started": True}

    def call(
        self, serial: str, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        self.calls.append((method, path, params))
        if self.refuse and path == "/v1/receiver/start":
            raise TabletError("tablet app", "params.group is required")
        if self.away:
            raise TabletError("tablet app", "not answering")
        if path.startswith("/v1/records"):
            after = int(path.split("after=")[1])
            rows = [r for r in self.records if r["seq"] > after]
            return {"records": rows, "last_seq": self.records[-1]["seq"] if self.records else 0}
        if path == "/v1/status":
            return {"in_front": self.in_front}
        return {}


def rec(seq: int, rx: int = 3000, pat: int = 1, lost: int = 0) -> dict[str, Any]:
    return {"seq": seq, "type": "s", "e": seq - 1, "ch": 1, "rx": rx, "lost": lost, "dup": 0,
            "reo": 0, "jit": 1.0, "br": 32_000_000, "cc": 0, "p1": 0, "pat": pat, "pb": "ok",
            "pdr": 0, "pau": 0}  # fmt: skip


def receiver(tmp_path: Path, fake: FakeTablets) -> TabletReceiver:
    cfg = TabletConfig("R1", "tablet-1", "A1", 1, "239.1.1.1", 5004, "b", cell_s=10, buffer_ms=3000)
    return TabletReceiver(cfg, tmp_path, EventBus(), fake, wait=lambda s: True)  # type: ignore[arg-type]


def test_start_tells_the_app_the_channel_and_buffer(tmp_path: Path) -> None:
    fake = FakeTablets()
    r = receiver(tmp_path, fake)
    r.start()
    method, path, params = next(c for c in fake.calls if c[0] == "POST")
    assert (method, path) == ("POST", "/v1/receiver/start")
    assert params == {"group": "239.1.1.1", "port": 5004, "ch": 1, "play": True, "buffer_ms": 3000}
    assert fake.started_app == 1  # the app in front first, so the picture shows
    assert fake.paused == ["com.samsung.android.mcfds"]  # TAB-018: paused for the test
    r.stop()


def test_measuring_starts_at_packets_and_pat_and_cells_close(tmp_path: Path) -> None:
    fake = FakeTablets()
    r = receiver(tmp_path, fake)
    r.start()
    fake.records = [rec(1, rx=0, pat=0), rec(2, pat=0)] + [rec(i) for i in range(3, 26)]
    fake.records[10]["lost"] = 4
    r._pull()
    assert r.measuring and r.last_1s is not None and r.last_1s["e"] == 21  # 2 ok s settle it
    assert r.lost_total == 4
    cells = r.take_summaries()
    assert [c["index"] for c in cells] == [0, 1]  # two full 10 s cells, the third still open
    assert cells[0]["loss"]["lost"] == 4
    r.stop()
    assert fake.paused == []  # put back at the stop
    assert [c["partial"] for c in r.take_summaries()] == [True]  # the open cell at stop
    assert list((tmp_path / "raw" / "tablet-1").glob("*.jsonl"))


def test_records_from_before_this_start_are_not_taken(tmp_path: Path) -> None:
    fake = FakeTablets()
    fake.records = [rec(i) for i in range(1, 6)]  # an earlier run on the same app
    r = receiver(tmp_path, fake)
    r.start()
    r._pull()
    assert not r.measuring
    r.stop()


def test_a_refused_start_is_tab_017(tmp_path: Path) -> None:
    fake = FakeTablets()
    fake.refuse = True
    with pytest.raises(ZlError) as e:
        receiver(tmp_path, fake).start()
    assert e.value.code == "ZL-TAB-017" and "group is required" in e.value.why


def test_an_app_behind_another_screen_is_brought_back_once(tmp_path: Path) -> None:
    """TAB-016: behind another screen the app measures on but shows no picture."""
    fake = FakeTablets()
    r = receiver(tmp_path, fake)
    r.start()
    fake.in_front = False
    r._keep_in_front()
    r._keep_in_front()
    assert fake.started_app == 2  # at start, then once when it went behind
    fake.in_front = True
    r._keep_in_front()
    fake.in_front = False
    r._keep_in_front()
    assert fake.started_app == 3  # went behind again: brought back again
    r.stop()


def test_start_waits_for_a_just_launched_app(tmp_path: Path) -> None:
    """2026-10-24: the app was updated and launched; its API answered a few seconds later."""
    fake = FakeTablets()
    late = {"n": 3}
    real = fake.call

    def call(serial: str, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if path == "/v1/health" and late["n"] > 0:
            late["n"] -= 1
            raise TabletError("tablet app", "not answering")
        return real(serial, method, path, params)

    fake.call = call  # type: ignore[method-assign]
    r = receiver(tmp_path, fake)
    r._sleep = lambda s: None
    r.start()
    assert late["n"] == 0 and r.state == "RUNNING"
    r.stop()


def test_measuring_starts_after_playback_settles(tmp_path: Path) -> None:
    """05 2.1-2 as on a PC: the player's start (none, then freeze while its buffer fills)
    is warm-up, not a playback problem (2026-10-24, both tablets' first cell)."""
    fake = FakeTablets()
    r = receiver(tmp_path, fake)
    r.start()
    pbs = ["none", "freeze", "freeze", "ok", "ok", "ok", "ok"]
    fake.records = [{**rec(i + 1), "pb": pb} for i, pb in enumerate(pbs)]
    r._pull()
    assert r.measuring and r.last_1s is not None
    assert r.last_1s["e"] == 2  # measured from the second "ok" in a row: e0..e2 = 3 s
    r.stop()


def test_a_player_that_never_settles_is_measured_after_5_s(tmp_path: Path) -> None:
    fake = FakeTablets()
    r = receiver(tmp_path, fake)
    r.start()
    fake.records = [{**rec(i + 1), "pb": "freeze"} for i in range(8)]
    r._pull()
    assert r.measuring  # the playback problem then shows in the cells
    r.stop()
