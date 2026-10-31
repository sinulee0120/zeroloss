"""Throughput node side (docs/modules/throughput.md 0): iperf3 lines, results, the list."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from modules.throughput.client import ClientConfig, ClientJob
from modules.throughput.iperf import client_argv, parse, server_argv

TCP = json.dumps({"end": {"sum_sent": {"bits_per_second": 949.2e6, "retransmits": 3},
                          "sum_received": {"bits_per_second": 941.6e6}}})  # fmt: skip
UDP = json.dumps({"end": {"sum": {"bits_per_second": 900e6, "lost_percent": 0.01,
                                  "jitter_ms": 0.08}}})  # fmt: skip


def test_the_lan_pc_is_the_client_and_receive_is_reverse() -> None:
    a = client_argv("iperf3", "10.0.0.1", 30, receive=True, bind="192.168.45.177")
    assert a[:3] == ["iperf3", "-c", "10.0.0.1"] and "-R" in a and "-J" in a
    assert a[a.index("-B") + 1] == "192.168.45.177" and a[a.index("-t") + 1] == "30"
    assert "-R" not in client_argv("iperf3", "10.0.0.1", 30, receive=False)
    u = client_argv("iperf3", "10.0.0.1", 5, False, protocol="udp", udp_mbps=950)
    assert u[-3:] == ["-u", "-b", "950M"]
    with pytest.raises(ValueError):
        client_argv("iperf3", "10.0.0.1", 5, False, protocol="udp")
    assert server_argv("iperf3", 5211) == ["iperf3", "-s", "-p", "5211"]


def test_results_are_read_from_the_receiving_side() -> None:
    r = parse(TCP)
    assert r.ok and r.mbps == 941.6 and r.sender_mbps == 949.2 and r.retransmits == 3
    u = parse(UDP, "udp")
    assert u.ok and u.mbps == 900.0 and u.lost_pct == 0.01 and u.jitter_ms == 0.08


@pytest.mark.parametrize(("text", "why"), [
    ("", "no output"),
    ("iperf3: error - unable to connect to server", "unable to connect"),
    (json.dumps({"error": "the server is busy running a test"}), "busy"),
    (json.dumps({"end": {"sum_received": {"bits_per_second": 0}}}), "no received rate"),
])  # fmt: skip
def test_a_failed_run_is_never_a_zero_pass(text: str, why: str) -> None:
    r = parse(text)
    assert not r.ok and r.mbps is None and why in r.error


def _job(link: tuple[str, int | None, str | None], outputs: list[str],
         cases: int = 2) -> tuple[ClientJob, list[list[str]]]:  # fmt: skip
    ran: list[list[str]] = []

    def run(argv: list[str], limit: float) -> str:
        ran.append(argv)
        return outputs[(len(ran) - 1) % len(outputs)]

    cfg = ClientConfig("R1", "rx", "10.0.0.1", "192.168.45.177",
                       [{"label": f"case {i}", "speed_mbps": None} for i in range(cases)],
                       seconds=1, wait_s=0)  # fmt: skip
    return ClientJob(cfg, run, lambda bind: link), ran


def _finish(job: ClientJob) -> None:
    job.start()
    end = time.monotonic() + 5
    while job.status()["state"] in ("starting", "running") and time.monotonic() < end:
        time.sleep(0.01)


def test_each_case_measures_send_then_receive_and_leaves_one_record() -> None:
    job, ran = _job(("enp0s31f6", 1000, "full"), [TCP])
    _finish(job)
    st = job.status()
    assert st["state"] == "done" and st["done"] and st["cases"] == 2
    assert ["-R" in a for a in ran] == [False, True, False, True]
    recs = job.take_summaries()
    assert [r["index"] for r in recs] == [0, 1] and job.take_summaries() == []
    r = recs[0]
    assert r["case"] == {"label": "case 0", "requested_mbps": None, "negotiated_mbps": 1000,
                         "duplex": "full", "nic": "enp0s31f6", "kind": "wired"}  # fmt: skip
    assert r["send"]["mbps"] == 941.6 and r["receive"]["ok"] and r["not_run"] is None
    assert st["results"][0] == {"label": "case 0", "link": 1000, "send": 941.6,
                                "receive": 941.6, "not_run": None}  # fmt: skip


def test_a_down_link_is_not_run_with_its_reason_and_the_list_goes_on() -> None:
    job, ran = _job(("enp0s31f6", None, None), [TCP])
    _finish(job)
    recs = job.take_summaries()
    assert ran == [] and len(recs) == 2
    assert "down or its speed unknown" in recs[0]["not_run"] and recs[0]["send"] is None


def test_an_iperf_failure_is_kept_in_the_case() -> None:
    job, ran = _job(("enp0s31f6", 1000, "full"), ["iperf3: error - unable to connect"], cases=1)
    job.sleep = lambda s: False  # no real waiting
    _finish(job)
    (r,) = job.take_summaries()
    assert not r["send"]["ok"] and "unable to connect" in r["send"]["error"]
    assert job.status()["state"] == "done"
    assert len(ran) == 4  # each direction tried once more (2026-10-22: right after a join)


def test_a_connect_failure_is_tried_once_more() -> None:
    job, ran = _job(("wlx58", 174, "full"), ["iperf3: error - unable to connect", TCP], cases=1)
    job.sleep = lambda s: False
    _finish(job)
    (r,) = job.take_summaries()
    assert r["send"]["ok"] and r["receive"]["ok"] and len(ran) == 4  # each one: fail, then ok


def test_stop_ends_the_list_and_keeps_what_was_measured() -> None:
    gate: list[Any] = []

    def run(argv: list[str], limit: float) -> str:
        gate.append(1)
        time.sleep(0.05)
        return TCP

    cfg = ClientConfig("R1", "rx", "10.0.0.1", "192.168.45.177",
                       [{"label": str(i)} for i in range(50)], seconds=1, wait_s=0)  # fmt: skip
    job = ClientJob(cfg, run, lambda b: ("e", 1000, "full"))
    job.start()
    while len(gate) < 3:
        time.sleep(0.01)
    job.stop()
    assert job.status()["state"] == "stopped"
    assert 1 <= len(job.take_summaries()) < 50


# ---------------------------------------------------------------- every setting (2026-10-16)


class FakeCard:
    """A card that does 10/100 half/full and 1000 full; ``stuck`` = cannot be put back."""

    def __init__(self, stuck: bool = False) -> None:
        from core.blocks.nic_speed import Mode

        self.modes = [Mode(10, "full", "10baseT/Full"), Mode(100, "half", "100baseT/Half"),
                      Mode(1000, "full", "1000baseT/Full")]  # fmt: skip
        self.now: Any = None
        self.restored = 0
        self.stuck = stuck

    def supported(self, nic: str) -> list[Any]:
        return list(self.modes)

    def set_mode(self, nic: str, mode: Any) -> dict[str, Any]:
        self.now = mode
        return {"readback_ok": True, "link": True, "speed": mode.speed, "duplex": mode.duplex}

    def restore(self, nic: str) -> dict[str, Any]:
        self.restored += 1
        self.now = None
        return {"ok": not self.stuck, "why": "link did not come up" if self.stuck else ""}


def _phase_job(
    cases: list[dict[str, Any]], card: FakeCard | None = None
) -> tuple[ClientJob, list[list[str]]]:
    from modules.throughput.client import ClientConfig as Cfg

    ran: list[list[str]] = []

    def run(argv: list[str], limit: float) -> str:
        ran.append(argv)
        return TCP

    cfg = Cfg("R1", "rx", "10.0.0.1", "192.168.1.237", cases, seconds=1, wait_s=0,
              unsupported=["Wi-Fi band: one name per band on this router"])  # fmt: skip
    return ClientJob(cfg, run, lambda b: ("enp0s31f6", 1000, "full"), card=card), ran


def test_every_mode_of_this_pcs_card_then_back_as_it_was() -> None:
    from modules.throughput.client import OWN_MODES

    card = FakeCard()
    job, ran = _phase_job([{"label": "Auto"}, {"phase": OWN_MODES}], card)
    _finish(job)
    recs = job.take_summaries()
    assert job.status()["state"] == "done" and card.restored == 1 and card.now is None
    assert [r["case"]["label"] for r in recs] == [
        "Auto",
        "LAN card set to 10 Mbps full",
        "LAN card set to 100 Mbps half",
        "LAN card set to 1000 Mbps",
    ]
    assert [r["index"] for r in recs] == [0, 1, 2, 3] and len(ran) == 8  # 4 cases x 2 ways
    assert recs[2]["case"]["negotiated_mbps"] == 100  # the baseline is what it linked at
    assert recs[2]["case"]["setting"]["requested"] == "100 Mbps half"
    assert recs[0]["unsupported"] == ["Wi-Fi band: one name per band on this router"]


def test_a_card_that_cannot_be_put_back_stops_for_safety() -> None:
    from modules.throughput.client import OWN_MODES

    job, _ = _phase_job([{"phase": OWN_MODES}, {"label": "after"}], FakeCard(stuck=True))
    _finish(job)
    st = job.status()
    assert st["state"] == "failed" and st["error"].startswith("SAFETY")
    assert "after" not in [r["case"]["label"] for r in job.take_summaries()]
    assert st["put_back"] == [{"card": "enp0s31f6", "ok": False, "to": "Auto",
                               "why": "link did not come up"}]  # fmt: skip


def test_a_card_not_put_back_asks_the_person_and_goes_on() -> None:
    """2026-10-22 user: "자동으로 설정한게 제대로 안될 경우 사용자가 고쳐서라도 진행이 되게"."""
    from modules.throughput.client import OWN_MODES

    card = FakeCard(stuck=True)
    job, _ = _phase_job([{"phase": OWN_MODES}, {"label": "after"}], card)
    asked: list[tuple[str, int]] = []

    def tell(text: str, seq: int) -> None:
        asked.append((text, seq))
        card.stuck = False  # the person put it back by hand
        threading.Thread(target=lambda: job.resume(seq, {"choice": "retry"})).start()

    job.tell = tell
    _finish(job)
    st = job.status()
    assert st["state"] == "done" and len(asked) == 1 and "by hand" in asked[0][0]
    assert [x["ok"] for x in st["put_back"]] == [False, True]  # checked again: back
    assert "after" in [r["case"]["label"] for r in job.take_summaries()]


def test_every_put_back_is_shown() -> None:
    """2026-10-22 user: "설정 원복 확실히 하고 마무리" — each setting put back, plainly."""
    from modules.throughput.client import OWN_MODES

    job, _ = _phase_job([{"phase": OWN_MODES}], FakeCard())
    _finish(job)
    assert [(x["card"], x["ok"]) for x in job.status()["put_back"]] == [("enp0s31f6", True)]


def test_the_wan_side_modes_go_through_the_controller() -> None:
    from modules.throughput.client import WAN_MODES

    job, ran = _phase_job([{"phase": WAN_MODES}])
    job.start()
    answers: list[dict[str, Any]] = [
        {"mode": "100 Mbps full", "readback_ok": True, "link": True, "speed": 100},
        {"mode": "2500 Mbps full", "readback_ok": True, "link": True, "speed": 1000},
        {"done": True},
        {"ok": True},
    ]
    asked = []
    for a in answers:
        end = time.monotonic() + 5
        while job.status()["need"] is None and time.monotonic() < end:
            time.sleep(0.01)
        need = job.status()["need"]
        asked.append(need["wan"])
        assert job.resume(need["seq"], a) and not job.resume(need["seq"], a)  # once
    end = time.monotonic() + 5
    while job.status()["state"] == "running" and time.monotonic() < end:
        time.sleep(0.01)
    recs = job.take_summaries()
    assert asked == ["next", "next", "next", "restore"] and job.status()["state"] == "done"
    assert [r["case"]["negotiated_mbps"] for r in recs] == [100, 1000]  # 2500 asked, 1000 got
    assert recs[1]["case"]["setting"]["side"] == "WAN-side PC" and len(ran) == 4


def test_one_direction_only_and_each_result_keeps_its_command() -> None:
    """2026-10-17 user: "send와 receive를 각각 iperf, iperf -R로 테스트할건데 한번에
    테스트한건지도 모르겠고" (S-63), and the command shown with the result (reference)."""
    job, ran = _job(("enp0s31f6", 1000, "full"), [TCP], cases=1)
    job.cfg.directions = ("receive",)
    _finish(job)
    assert len(ran) == 1 and "-R" in ran[0]
    r = job.take_summaries()[0]
    assert r["send"] is None and r["receive"]["command"].startswith("iperf3 -c 10.0.0.1 -p ")
    assert r["receive"]["command"].endswith("-B 192.168.45.177 -R")


class FakeWifi:
    """The Wi-Fi card: joins a band network and gets an address there; goes back."""

    def __init__(self, back_ok: bool = True) -> None:
        self.on: str | None = "router-5g"
        self.calls: list[tuple[str, str | None, str]] = []
        self.back_ok = back_ok

    def connection_of(self, nic: str) -> str | None:
        return self.on

    def join_band(self, nic: str, ssid: str, bssid: str, security: str = "Open",
                  password: str = "") -> dict[str, Any]:  # fmt: skip
        self.calls.append(("join", ssid, bssid))
        if ssid == "far-6g":
            return {"why": "Cannot connect to Wi-Fi 'far-6g': not seen"}
        self.on = ssid
        return {"ip": "192.168.45.88", "ssid": ssid}

    def back(self, nic: str, connection: str | None) -> dict[str, Any]:
        self.calls.append(("back", connection, ""))
        self.on = connection
        if not self.back_ok:
            return {"back": False, "why": "gone"}
        return {"back": True, "ip": "192.168.45.87"}


def _wifi_job(wifi: FakeWifi) -> tuple[ClientJob, list[list[str]]]:
    ran: list[list[str]] = []

    def run(argv: list[str], limit: float) -> str:
        ran.append(argv)
        return TCP

    bands = [{"band": "2.4GHz", "ssid": "router-2g", "bssid": "02:00:5E:10:00:2C"},
             {"band": "6GHz", "ssid": "far-6g", "bssid": "02:00:5E:10:00:2E"}]  # fmt: skip
    cfg = ClientConfig("R1", "t460-rx", "10.0.0.1", "192.168.45.87",
                       [{"phase": "wifi_bands"}], seconds=1, wait_s=0, kind="wifi",
                       nic="wlx58", bands=bands)  # fmt: skip
    links = {"192.168.45.87": ("wlx58", 867, None), "192.168.45.88": ("wlx58", 144, None)}
    return ClientJob(cfg, run, lambda b: links.get(b, ("", None, None)), wifi=wifi), ran


def test_each_picked_band_is_measured_from_its_address_then_back() -> None:
    """2026-10-17 user: "WAN -> WLAN, WAN <- WLAN 을 WLAN설정만 바꿔서 테스트한다"."""
    wifi = FakeWifi()
    job, ran = _wifi_job(wifi)
    _finish(job)
    assert job.status()["state"] == "done"
    two, six = job.take_summaries()
    assert two["case"]["label"] == "Wi-Fi 2.4GHz · router-2g" and two["send"]["ok"]
    assert two["between"]["lan"] == "192.168.45.88" and "-B" in ran[0]
    assert ran[0][ran[0].index("-B") + 1] == "192.168.45.88"  # measured from the band's address
    assert six["not_run"].startswith("Cannot connect to Wi-Fi 'far-6g'")
    assert wifi.calls[-1] == ("back", "router-5g", "") and wifi.on == "router-5g"


def test_a_card_that_cannot_go_back_stops_the_list_for_safety() -> None:
    job, _ = _wifi_job(FakeWifi(back_ok=False))
    _finish(job)
    st = job.status()
    assert st["state"] == "failed" and st["error"].startswith("SAFETY: wlx58 could not be put back")


def test_the_card_rate_is_read_every_second_while_iperf_runs() -> None:
    """2026-10-22 user: "실시간 속도 넣어" — from the card's byte counters, any iperf3."""
    counters = iter([(0, 0), (125_000_000, 1_000), (250_000_000, 2_000)])
    seen: list[dict[str, Any]] = []

    def run_iperf(argv: list[str], limit: float) -> str:
        for _ in range(40):  # iperf3 runs: the sampler reads the card meanwhile
            if job.live:
                seen.append(dict(job.live))
                break
            time.sleep(0.1)
        return json.dumps({"end": {"sum_received": {"bits_per_second": 9.4e8},
                                   "sum_sent": {"bits_per_second": 9.4e8}}})  # fmt: skip

    cfg = ClientConfig("R1", "rx", "10.0.0.1", "192.168.45.177",
                       [{"label": "Auto", "speed_mbps": None}], seconds=1, wait_s=0,
                       directions=("receive",))  # fmt: skip
    job = ClientJob(cfg, run_iperf, lambda b: ("e", 1000, "full"),
                    read_bytes=lambda card: next(counters, None))  # fmt: skip
    job.start()
    assert job._thread is not None
    job._thread.join(10)
    assert seen and seen[0]["direction"] == "WAN→LAN" and 900 < seen[0]["mbps"] < 1100
    assert job.live is None  # nothing shown once iperf3 ended
    assert job.status()["live"] is None


def test_card_counters_on_linux(tmp_path: Path) -> None:
    from core.blocks.nic_bytes import bytes_of, rate_mbps

    stats = tmp_path / "enp1" / "statistics"
    stats.mkdir(parents=True)
    (stats / "rx_bytes").write_text("1000\n")
    (stats / "tx_bytes").write_text("2000\n")
    if sys.platform != "win32":
        assert bytes_of("enp1", tmp_path) == (1000, 2000)
    assert rate_mbps((0, 0), (125_000_000, 0), 1.0, received=True) == 1000.0
    assert rate_mbps((5, 0), (1, 0), 1.0, received=True) is None  # a counter reset


def test_reset_all_puts_back_the_cards_and_the_wifi_network(tmp_path: Path) -> None:
    """2026-10-22 user: "reset all누르면 모든 장비들 노트북 설정 원래대로 돌려놓도록 해"."""
    from types import SimpleNamespace

    from modules.throughput import module as tm

    class Card:
        def restore_all(self) -> dict[str, dict[str, Any]]:
            return {"enp0s31f6": {"ok": True}}

    class Wifi:
        now = "ZL_TEST_5G"

        def connection_of(self, nic: str) -> str:
            return self.now

        def back(self, nic: str, connection: str) -> dict[str, Any]:
            self.now = connection
            return {"back": True}

    m = object.__new__(tm.ThroughputModule)
    m.cards = Card()  # type: ignore[assignment]
    m.node = SimpleNamespace(wifi=Wifi())
    m.wifi_kept = tmp_path / "throughput-wifi.json"
    m.wifi_kept.write_text('{"wlx58": "home-5g"}', encoding="utf-8")
    got = m.restore_settings()
    assert sorted((x["what"], x["ok"]) for x in got) == [("enp0s31f6", True), ("wlx58", True)]
    assert m.node.wifi.now == "home-5g" and not m.wifi_kept.exists()


class FakeWifiSettings(FakeWifi):
    """A card with width and mode settings (throughput.md 1.2): 20 MHz and 11n on 2.4 GHz;
    the 11n case joins with 11ax anyway (the router or driver ignored it)."""

    def __init__(self, restore_ok: bool = True) -> None:
        super().__init__()
        self.restore_ok = restore_ok
        self.changed: str | None = None

    def options(self, nic: str, band: str) -> list[dict[str, Any]]:
        if band != "2.4GHz":
            return []
        return [{"axis": "width", "band": band, "mhz": 20, "set": [("w", "0")],
                 "label": "width capped at 20 MHz"},
                {"axis": "mode", "mode": "802.11n", "set": [("m", "1")],
                 "label": "mode capped at 802.11n"}]  # fmt: skip

    def set_option(self, nic: str, option: dict[str, Any],
                   net: dict[str, Any]) -> dict[str, Any]:  # fmt: skip
        self.calls.append(("set", option["label"], net["ip"]))
        self.changed = option["label"]
        if option["axis"] == "mode":
            return {"readback_ok": True, "link": True, "mode": "802.11ax",
                    "why": "it joined with 802.11ax, not 802.11n"}  # fmt: skip
        return {"readback_ok": True, "link": True, "ip": "192.168.45.89", "mode": "802.11ax",
                "rx_mbps": 286.7}  # fmt: skip

    def restore_option(self, nic: str) -> dict[str, Any]:
        self.calls.append(("restore", self.changed, ""))
        ok = self.restore_ok
        self.changed = None if ok else self.changed
        return {"ok": ok, **({} if ok else {"why": "the card still reads 20 MHz"})}


def test_each_wifi_setting_is_its_own_case_measured_only_when_joined_as_asked() -> None:
    """2026-10-29 user: "전에는 설정 변경 무선에서 안하고 그냥 1회 테스트하고 말았잖아",
    "그게 문제라고": per band the card's width caps and modes, one at a time, each back
    before the next; a setting the card did not join with is NOT RUN with why."""
    wifi = FakeWifiSettings()
    job, _ = _wifi_job(wifi)
    _finish(job)
    assert job.status()["state"] == "done"
    auto, width, mode, six = job.take_summaries()
    assert auto["case"]["label"] == "Wi-Fi 2.4GHz · router-2g"
    assert width["case"]["label"] == "Wi-Fi 2.4GHz · width capped at 20 MHz"
    assert width["send"]["ok"] and width["between"]["lan"] == "192.168.45.89"
    assert width["case"]["setting"]["rx_mbps"] == 286.7
    assert mode["not_run"] == "it joined with 802.11ax, not 802.11n" and mode["send"] is None
    assert six["not_run"].startswith("Cannot connect")
    order = [c[0] for c in wifi.calls]
    assert order[:6] == ["join", "set", "restore", "join", "set", "restore"]
    puts = [p for p in job.status()["put_back"] if p["to"] == "its own settings"]
    assert len(puts) == 2 and all(p["ok"] for p in puts)


def test_a_wifi_setting_not_put_back_stops_for_safety() -> None:
    job, _ = _wifi_job(FakeWifiSettings(restore_ok=False))
    _finish(job)
    st = job.status()
    assert st["state"] == "failed" and "SAFETY: wlx58 could not be put back" in st["error"]
