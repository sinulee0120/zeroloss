"""Tablets in the throughput test (throughput.md T-4, 2026-10-29 user: "구현부터 다 해",
"연결된 wifi만 하면 돼", "2G 5G 6G변경도 해야하네", "open으로 할거야"): the app measures
against the same iperf3 server, through the PC its USB is on, after the PCs' cards."""

from __future__ import annotations

import json
from typing import Any

from core.blocks.tablets import TabletError
from modules.throughput.client import ClientConfig, ClientJob
from modules.throughput.run import find_targets
from modules.throughput.tablet import TabletIperf, iperf_args


class FakeAdb:
    def __init__(self, tabs: FakeTablets) -> None:
        self.tabs = tabs

    def shell(self, serial: str, args: list[str], timeout: float = 10.0) -> str:
        return f'Wifi is connected to "{self.tabs.on}"'


class FakeTablets:
    def __init__(self) -> None:
        self.on = "home-5g"
        self.calls: list[tuple[str, Any]] = []
        self.runs = 0
        self.adb = FakeAdb(self)

    def ensure_app(self, serial: str) -> dict[str, Any]:
        return {}

    def start_app(self, serial: str) -> dict[str, Any]:
        return {}

    def wifi_status(self, serial: str) -> dict[str, Any]:
        return {"std": "11ax", "freq": 5745, "rx_mbps": 1201}

    def saved_networks(self, serial: str) -> list[str]:
        return ["home-5g"]

    def join_wifi(
        self, serial: str, ssid: str, security: str, password: str = ""
    ) -> dict[str, Any]:
        self.calls.append(("join", ssid))
        if ssid == "far-6g":
            raise TabletError("join the Wi-Fi", "not on 'far-6g' after 30 s")
        self.on = ssid
        return {"joined": True, "ssid": ssid, "ip": "192.168.45.120"}

    def back_wifi(self, serial: str, ssid: str | None, added: list[str]) -> dict[str, Any]:
        self.calls.append(("back", (ssid, tuple(added))))
        self.on = ssid or ""
        return {"back": True, "ssid": ssid, "ip": "192.168.45.110"}

    def call(self, serial: str, method: str, path: str, params: Any = None) -> Any:
        self.calls.append((path, params))
        if path == "/v1/throughput/start":
            self.runs = 0
            return {"started": True}
        if path == "/v1/throughput/status":
            self.runs += 1
            if self.runs < 2:
                return {"state": "running", "moved_bytes": 50_000_000}
            return {"state": "done", "result": {"ok": True, "bits_per_second": 4.2e8,
                                                "bytes": 525_000_000, "seconds": 10.0}}  # fmt: skip
        return {}


def test_the_iperf3_command_becomes_the_apps_run() -> None:
    a = iperf_args(["iperf3", "-c", "10.0.0.1", "-p", "5211", "-t", "10", "-J", "-R"])
    assert a == {"server": "10.0.0.1", "port": 5211, "seconds": 10, "reverse": True,
                 "udp": False}  # fmt: skip
    tabs = FakeTablets()
    tab = TabletIperf(tabs, "T1", sleep=lambda s: None)  # type: ignore[arg-type]
    got = json.loads(tab.run(["iperf3", "-c", "10.0.0.1", "-p", "5211", "-t", "10"], 60))
    assert got["end"]["sum_received"]["bits_per_second"] == 4.2e8  # iperf3's own shape
    assert ("/v1/throughput/start", {"server": "10.0.0.1", "port": 5211, "seconds": 10,
                                     "reverse": False}) in tabs.calls  # fmt: skip
    udp = json.loads(tab.run(["iperf3", "-c", "10.0.0.1", "-u", "-t", "10"], 60))
    assert udp["error"].startswith("UDP is not measured on a tablet")


def _job(bands: list[dict[str, Any]]) -> tuple[ClientJob, FakeTablets]:
    tabs = FakeTablets()
    tab = TabletIperf(tabs, "T1", sleep=lambda s: None)  # type: ignore[arg-type]
    cases = [{"phase": "wifi_bands"}] if bands else [{"label": "Wi-Fi as connected"}]
    cfg = ClientConfig("R1", "t460-rx", "10.0.0.1", "", cases, seconds=1, wait_s=0,
                       kind="wifi", nic="T1", bands=bands, who="tablet SM-X930")  # fmt: skip
    return ClientJob(cfg, tab.run, tab.read_link, wifi=tab if bands else None), tabs


def _finish(job: ClientJob) -> None:
    job.start()
    assert job._thread is not None
    job._thread.join(20)


def test_a_tablet_measures_its_wifi_as_connected_both_ways() -> None:
    job, tabs = _job([])
    _finish(job)
    (r,) = job.take_summaries()
    assert r["case"]["label"] == "tablet SM-X930 · Wi-Fi as connected"
    assert r["send"]["mbps"] == 420.0 and r["receive"]["mbps"] == 420.0
    assert r["case"]["negotiated_mbps"] == 1201  # its Wi-Fi receive rate, values only
    reverse = [p["reverse"] for path, p in tabs.calls if path == "/v1/throughput/start"]
    assert reverse == [False, True]  # TX then RX


def test_a_tablet_goes_through_its_bands_then_back_on_its_own_network() -> None:
    bands = [{"band": "2.4GHz", "ssid": "ZL_OPEN_2G", "security": "Open"},
             {"band": "6GHz", "ssid": "far-6g", "security": "Open"}]  # fmt: skip
    job, tabs = _job(bands)
    _finish(job)
    assert job.status()["state"] == "done"
    two, six = job.take_summaries()
    assert (
        two["case"]["label"] == "tablet SM-X930 · Wi-Fi 2.4GHz · ZL_OPEN_2G" and two["send"]["ok"]
    )
    assert two["case"]["setting"]["mode"] == "11ax"  # what it joined with, recorded
    assert six["not_run"].startswith("join the Wi-Fi")
    assert ("back", ("home-5g", ("ZL_OPEN_2G", "far-6g"))) in tabs.calls
    assert tabs.on == "home-5g"  # only the networks the test added are forgotten


def test_ticked_tablets_come_after_the_pcs_cards() -> None:
    plan = {
        "cards": {"t460-rx": [{"nic": "enp0s31f6", "ip": "192.168.45.94"}],
                  "probook-sender": [{"nic": "enp37s0", "ip": "10.0.0.1"}]},
        "hosts": {"t460-rx": "192.168.45.94", "probook-sender": "10.0.0.1"},
        "settings": {"S-65": {"t460-rx": {
            "enp0s31f6": {}, "tablet:T1": {},
            "tablet:T2": {"bands": {"5GHz": {"ssid": "ZL_OPEN_5G", "security": "Open"}}}}}},
    }  # fmt: skip
    got = find_targets(plan, "probook-sender", "all")
    assert [(t.kind, t.serial) for t in got] == [("wired", ""), ("tablet", "T1"), ("tablet", "T2")]
    assert got[1].id == "t460-rx@tablet:T1" and got[1].device == "t460-rx"
    assert [t.kind for t in find_targets(plan, "probook-sender", "wired")] == ["wired"]
    assert [t.kind for t in find_targets(plan, "probook-sender", "wifi")] == ["tablet", "tablet"]


def test_the_controller_pcs_wifi_goes_last_of_all() -> None:
    """2026-10-29 user: the Controller PC's Wi-Fi is its internet ("다시 이 와이파이로
    돌아와야할거야, 그래야 클로드 AI로 계속 확인하지"): every other result first."""
    wifi = {"nic": "Wi-Fi", "ip": "192.168.0.20", "wifi": {"band": "5GHz"}}
    plan = {
        "cards": {"controller": [wifi], "t460-rx": [{"nic": "wlx58", "ip": "", "wifi": {}}]},
        "hosts": {"t460-rx": "192.168.45.94"},
        "settings": {"S-65": {
            "controller": {"Wi-Fi": {"bands": {"5GHz": {"ssid": "ZL_OPEN_5G"}}}},
            "t460-rx": {"wlx58": {"bands": {"5GHz": {"ssid": "ZL_OPEN_5G"}}}, "tablet:T1": {}}}},
    }  # fmt: skip
    got = find_targets(plan, "probook-sender", "wifi")
    assert [(t.device, t.kind) for t in got] == [
        ("t460-rx", "wifi"),
        ("t460-rx", "tablet"),
        ("controller", "wifi"),
    ]


def test_a_tablet_off_usb_has_nothing_to_put_back() -> None:
    """2026-10-29 R20261029-215851: the tablet was off USB, every join failed "device not
    found", then putting it back asked the person twice."""
    tabs = FakeTablets()
    tab = TabletIperf(tabs, "T1", sleep=lambda s: None)  # type: ignore[arg-type]

    def gone(*a: Any, **k: Any) -> dict[str, Any]:
        raise TabletError("join the Wi-Fi", "adb: device 'T1' not found")

    tabs.join_wifi = gone  # type: ignore[method-assign]
    assert "not found" in tab.join_band("T1", "ZL_OPEN_5G", "", "Open")["why"]
    assert tab.back("T1", "home-5g") == {"back": True, "why": "nothing was changed"}
    assert not any(c[0] == "back" for c in tabs.calls)


def test_a_ticked_tablet_not_on_usb_at_start_is_left_out_and_said() -> None:
    """2026-10-30 user ("고쳐"): a ticked tablet off USB was measured anyway and stopped
    the test on an Action Required."""
    from modules.throughput.run import missing_tablets

    plan = {
        "cards": {"t460-rx": [{"nic": "enp0s31f6", "ip": "192.168.45.94"}]},
        "hosts": {"t460-rx": "192.168.45.94"},
        "settings": {"S-65": {"t460-rx": {"enp0s31f6": {}, "tablet:T1": {}, "tablet:T2": {}}}},
        "tablets_present": {"t460-rx": ["T2"]},
    }  # fmt: skip
    got = find_targets(plan, "probook-sender", "all")
    assert [(t.kind, t.serial) for t in got] == [("wired", ""), ("tablet", "T2")]
    assert missing_tablets(plan) == ("tablet T1 on t460-rx: not connected by USB at Start",)
    assert missing_tablets({**plan, "tablets_present": {}}) == ()  # not asked: kept as before


def test_a_tablet_is_its_own_row_reached_through_its_pc() -> None:
    """2026-10-30 user ("start누르고 devices화면에 여전히 태블릿 정보는 안나오네")."""
    from modules.throughput.run import receiver_rows, validate

    plan = validate({
        "test_type": "all", "devices": {"sender": "probook-sender"},
        "cards": {"t460-rx": [{"nic": "enp0s31f6", "ip": "192.168.45.94"}],
                  "probook-sender": [{"nic": "enp37s0", "ip": "10.0.0.1"}]},
        "hosts": {"t460-rx": "192.168.45.94", "probook-sender": "10.0.0.1"},
        "nics": {"probook-sender": "10.0.0.1"},
        "settings": {"S-65": {"t460-rx": {"enp0s31f6": {}, "tablet:T1": {}}}},
    })  # fmt: skip
    assert ("tablet T1", 0) in plan.receivers and plan.node_of("tablet T1") == "t460-rx"
    data = {
        "clients": {
            "t460-rx@tablet:T1": {
                "state": "running",
                "doing": "case: send",
                "live": {"direction": "LAN→WAN", "mbps": 420.0},
            }
        }
    }
    rows = receiver_rows(plan, data, {})
    labels = [i["label"] for i in rows["tablet T1"]["items"]]
    assert labels[0] == "Wi-Fi tablet" and "LAN→WAN now" in labels


def test_the_rate_every_second_is_a_live_graph_line() -> None:
    """2026-10-30 user: "tput값을 숫자로만 보여주네. live view에서 그래프로"."""
    from modules.throughput.run import LIVE_COLUMNS, second_records

    data = {"clients": {
        "t460-rx@192.168.45.94": {"state": "running",
                                  "live": {"direction": "WAN→LAN", "mbps": 677.0, "e": 812}},
        "t460-rx@tablet:T1": {"state": "queued", "live": None}}}  # fmt: skip
    got = second_records(None, "t460-rx", data)  # type: ignore[arg-type]
    assert got == [(("t460-rx", 1), {"e": 812, "rx": 677.0})]
    tablet = {"clients": {"t460-rx@tablet:T1": {"live": {"direction": "LAN→WAN", "mbps": 420.0,
                                                         "e": 900}}}}  # fmt: skip
    assert second_records(None, "t460-rx", tablet) == [(("tablet T1", 1), {"e": 900, "tx": 420.0})]  # type: ignore[arg-type]
    assert [c["key"] for c in LIVE_COLUMNS if c.get("chart")] == ["tx", "rx"]
