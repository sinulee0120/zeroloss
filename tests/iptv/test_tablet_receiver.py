"""modules/iptv/receiver/tablet.py with a fake tablet: its records are written and
summarized like a PC receiver's, from the first second with packets and the PAT."""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

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
        self.counters: dict[str, int] = {"hd": 0, "nd": 0, "ne": 0, "ue": 5, "ub": 5}
        self.ssid: str | None = "TV"
        self.held: list[str] = []
        self.rejoined: list[tuple[str, str, str]] = []

    def app_uids(self, serial: str) -> set[int]:
        return {10123}

    def wifi_status(self, serial: str) -> dict[str, Any]:
        return {"std": "11ax", "freq": 5745, "rx_mbps": 1201, "ssid": self.ssid}

    def screenshot(self, serial: str, out: Path) -> None:
        if self.away:
            raise TabletError("take a screenshot", "not answering")
        out.write_bytes(b"PNG fake screen")

    def hold_wifi(self, serial: str, ssid: str) -> list[str]:
        self.held.append(ssid)
        return ["ipreach disconnect off"]

    def release_wifi(self, serial: str | None = None) -> list[dict[str, Any]]:
        out = [{"what": f"{serial} ipreach disconnect", "ok": True, "detail": "put back"}]
        self.held = []
        return out

    def rejoin(self, serial: str, ssid: str, security: str, password: str) -> dict[str, Any]:
        self.rejoined.append((ssid, security, password))
        self.ssid = ssid
        return {"joined": True}

    def net_counters(self, serial: str, port: int, uids: set[int]) -> dict[str, int]:
        if self.away:
            raise TabletError("read the receive counters", "not answering")
        return dict(self.counters)

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


CFG = TabletConfig("R1", "tablet-1", "A1", 1, "239.1.1.1", 5004, "b", cell_s=10)


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


def test_the_tablets_own_drops_go_on_the_second_and_the_cell(tmp_path: Path) -> None:
    """iptv R-25 (2026-10-24, a production M2U router): one packet lost on a tablet. The
    tablet's socket and Wi-Fi counters, read with each pull, say whether it dropped it."""
    fake = FakeTablets()
    r = receiver(tmp_path, fake)
    r.start()
    fake.records = [rec(1), rec(2), rec(3)]
    r._pull()  # warm-up done; the counters' first read is the base
    fake.records.append(rec(4, lost=1))
    fake.counters = {**fake.counters, "hd": 1, "ub": 6}  # the app's socket overflowed
    r._pull()
    assert r.last_1s is not None
    assert (r.last_1s["lost"], r.last_1s["hd"], r.last_1s["ub"], r.last_1s["nd"]) == (1, 1, 1, 0)
    fake.records.append(rec(5))
    r._pull()
    assert r.last_1s is not None and r.last_1s["hd"] == 0  # written as 0: measured
    fake.counters = {**fake.counters, "hd": 0}  # the socket was made again: starts over
    fake.records.append(rec(6))
    r._pull()
    assert r.last_1s is not None and r.last_1s["hd"] == 0
    r.stop()
    cells = r.take_summaries()
    assert cells[0]["receiver"]["host_drops"] == 1


def test_the_wifi_standard_and_width_go_on_every_second(tmp_path: Path) -> None:
    """2026-10-24 user: "11ax로 붙었는지 bandwidth는 80Mhz인지 … 정보가 하나도 없네"."""
    fake = FakeTablets()
    r = receiver(tmp_path, fake)
    r.start()
    r._read_wifi()
    fake.records = [rec(1), rec(2), rec(3)]
    r._pull()
    assert r.last_1s is not None
    assert (r.last_1s["wst"], r.last_1s["wbw"]) == ("11ax", 80)
    r.stop()


def test_the_held_wifi_is_kept_watched_joined_again_and_put_back(tmp_path: Path) -> None:
    """TAB-019 (2026-10-31 user: "설정에서 내가 일부러 IPTV로 설정 바꿨으면 그대로 테스트동안
    유지하도록 adb로 고쳐"): kept for the test, off it said once and joined again when
    ZeroLoss joined it, put back at the stop."""
    from modules.iptv.receiver.tablet import HeldWifi

    fake = FakeTablets()
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(lambda e: seen.append(f"{e.code} {e.text}"))
    t = TabletReceiver(CFG, tmp_path, bus, fake, wait=lambda _s: True,  # type: ignore[arg-type]
                       held=HeldWifi("TV", "WPA2", "secret"))  # fmt: skip
    t.start()
    assert fake.held == ["TV"]
    fake.ssid = "Home"
    t._read_wifi()
    t._read_wifi()
    assert fake.rejoined == [("TV", "WPA2", "secret")]  # joined at once, back on the next read
    left = [x for x in seen if x.startswith("ZL-TAB-019")]
    assert len(left) == 1 and "now on Home" in left[0] and "joining it again" in left[0]
    assert any("back on Wi-Fi TV" in x for x in seen)
    t.stop()
    assert fake.held == [] and any("Wi-Fi setting put back" in x for x in seen)
    assert "secret" not in repr(HeldWifi("TV", "WPA2", "secret"))


def test_a_network_joined_by_hand_is_watched_but_not_joined_again(tmp_path: Path) -> None:
    from modules.iptv.receiver.tablet import HeldWifi

    fake = FakeTablets()
    bus = EventBus()
    seen: list[str] = []
    bus.subscribe(lambda e: seen.append(f"{e.code} {e.text}"))
    t = TabletReceiver(CFG, tmp_path, bus, fake, wait=lambda _s: True,  # type: ignore[arg-type]
                       held=HeldWifi("TV"))  # fmt: skip
    t.start()
    fake.ssid = None
    t._read_wifi()
    assert fake.rejoined == []
    assert any("left Wi-Fi TV, now on no network; put it back on TV on the tablet" in x
               for x in seen)  # fmt: skip
    t.stop()


def test_keep_holds_the_network_it_is_on_and_an_open_one_can_be_joined_again() -> None:
    """TAB-019 with "Wi-Fi: keep": the network at Start is held; an open one (the router's
    TV SSID) is joined again with no password, one with a password only watched."""
    from types import SimpleNamespace

    from modules.iptv.module import IptvModule

    class Keep(FakeTablets):
        kinds: ClassVar[dict[str, str]] = {"TV": "Open", "Home": "WPA2"}

        def saved_security(self, serial: str, ssid: str) -> str | None:
            return self.kinds.get(ssid)

    fake = Keep()
    m = IptvModule()
    m.node = SimpleNamespace(tablets=fake, bus=EventBus())
    held = m._join_wifi("tablet-1", "A1", None)
    assert held is not None and (held.ssid, held.security) == ("TV", "Open")
    fake.ssid = "Home"
    held = m._join_wifi("tablet-1", "A1", [])
    assert held is not None and (held.ssid, held.security) == ("Home", None)
    fake.ssid = None
    assert m._join_wifi("tablet-1", "A1", None) is None  # on no network: nothing to hold


def test_a_trouble_second_keeps_the_tablets_screen_as_evidence(tmp_path: Path) -> None:
    """F23 for tablets (2026-10-31 user: "태블릿 깨진거 스크린샷은 왜 없냐고"): the screen after the
    player's buffer, beside the records as the PCs' evidence; one a minute at most."""
    import json
    import threading

    from core.records.recorder import record_run

    fake = FakeTablets()
    r = receiver(tmp_path, fake)
    slept: list[float] = []
    r._sleep = slept.append  # type: ignore[assignment]
    r.start()
    fake.records = [rec(i) for i in range(1, 10)]
    fake.records[5]["lost"] = 3
    fake.records[7]["pb"] = "glitch"  # within a minute of the first: no second picture
    r._pull()
    for t in threading.enumerate():
        if t.name.endswith("-screen"):
            t.join(5)
    folder = tmp_path / "raw" / "tablet-1"
    shots = sorted(folder.glob("*_ev_e*"))
    assert [p.suffix for p in shots] == [".json", ".png"]
    assert all(record_run(p.name) == ("R1", None) for p in shots)  # the records index takes them
    info = json.loads(shots[0].read_text(encoding="utf-8"))
    assert info["why"] == ["lost 3"] and info["picture"] == shots[1].name
    assert slept and slept[0] == 3.5  # the 3 s buffer, then half a second
    r.stop()
