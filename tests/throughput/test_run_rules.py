"""Throughput Controller rules (docs/modules/throughput.md 0): plan, order, judgement."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from core.blocks.color_judge import Color
from core.protocol.envelope import ZlError
from modules.throughput import run


def plan(kind: str = "wired", **settings: Any) -> dict[str, Any]:
    return {
        "module": "throughput", "test_type": kind, "settings": settings,
        "devices": {"sender": "probook", "receivers": [
            {"device": "t460", "ch": 1, "network": "Wired"},
            {"device": "ctl", "ch": 1, "network": "Wired"},
            {"device": "tab", "ch": 1, "network": "Wireless"}]},
        "nics": {"probook": "10.0.0.1", "t460": "192.168.45.177", "ctl": "192.168.45.58"},
    }  # fmt: skip


def test_the_wan_pc_serves_and_the_lan_pcs_of_that_kind_measure() -> None:
    p = run.validate(plan())
    assert p.sender == "probook" and p.clients == ["ctl", "t460"] and p.server_ip == "10.0.0.1"
    assert (p.seconds, p.wait_s, p.threshold_pct, p.protocol, p.streams) == (30, 10, 90, "tcp", 1)
    assert run.validate(plan("wifi")).clients == ["tab"]  # an old plan: no card list
    assert p.duration_s > 2 * (2 * 10 + 2 * 30)  # an estimate: the end comes from the PCs


def cards_plan(kind: str = "all", **settings: Any) -> dict[str, Any]:
    """2026-10-16 user: every LAN address found is a target, one after another; the Wi-Fi
    card the person picked (S-65, 2026-10-17) too."""
    # S-65 "Cards to test": the cards the person ticked in Devices
    settings.setdefault("S-65", {"t460": {"enp0s31f6": {}, "wlx58": {}},
                                 "this-pc": {"Ethernet": {}}, "ctl": {"이더넷": {}}})  # fmt: skip
    return {**plan(kind, **settings), "hosts": {"probook": "10.0.0.1", "t460": "192.168.1.114"},
            "cards": {
                "probook": [{"nic": "enp37s0", "ip": "10.0.0.1"}],
                "t460": [{"nic": "enp0s31f6", "ip": "192.168.1.237"},
                         {"nic": "wlp4s0", "ip": "192.168.0.13", "wifi": {"band": "5GHz"}},
                         {"nic": "wlx58", "ip": "192.168.1.114", "wifi": {"band": "5GHz"}}],
            }}  # fmt: skip


def test_every_lan_card_is_a_target_cables_first() -> None:
    p = run.validate(cards_plan())
    assert [(t.device, t.ip, t.kind) for t in p.targets] == [
        ("t460", "192.168.1.237", "wired"),
        ("t460", "192.168.1.114", "wifi"),
    ]  # not home
    assert [t.ip for t in run.validate(cards_plan("wifi")).targets] == ["192.168.1.114"]
    wired, wifi = p.targets
    assert run.target_cases(p, wired) == [
        {"label": "Auto", "speed_mbps": None},
        {"phase": "wan_modes"},
        {"phase": "own_modes"},
    ]
    assert run.target_cases(p, wifi) == [{"label": "Auto", "speed_mbps": None}]
    flat = run.validate(cards_plan(**{"S-62": "As it is"}))
    assert run.target_cases(flat, flat.targets[0]) == [{"label": "Auto", "speed_mbps": None}]


@pytest.mark.parametrize(("change", "why"), [
    ({"devices": {"sender": None, "receivers": []}}, "No PC on the router WAN side"),
    ({"devices": {"sender": "probook", "receivers": []}}, "No PC on the router LAN side"),
    ({"settings": {"S-58": 1}}, "S-58 must be 5..600"),
    ({"settings": {"S-60": "QUIC"}}, "S-60 must be TCP or UDP"),
])  # fmt: skip
def test_a_plan_that_cannot_run_says_why(change: dict[str, Any], why: str) -> None:
    with pytest.raises(ZlError) as e:
        run.validate({**plan(), **change})
    assert why in e.value.what


def test_server_first_then_the_first_card() -> None:
    p = run.validate(cards_plan())
    server, clients = run.start_steps(p)
    assert [(s.device, s.path) for s in server] == [("probook", f"{run.PREFIX}/server/start")]
    (c,) = clients
    assert c.params["target"] == "t460@192.168.1.237" and c.params["bind"] == "192.168.1.237"
    assert c.params["server"] == "10.0.0.1" and c.params["kind"] == "wired"
    assert c.params["judged"] == {"wired": True, "pct": 90.0, "directions": ["send", "receive"]}
    stop = run.stop_steps(p)
    assert [s.device for s in stop[0]] == ["t460"] and stop[1][0].device == "probook"


def test_a_client_without_a_known_card_cannot_start() -> None:
    p = run.validate({**plan(), "nics": {"probook": "10.0.0.1"}})
    with pytest.raises(ZlError) as e:
        run.start_steps(p)
    assert "test card of" in e.value.what


def hb(**targets: str) -> dict[str, dict[str, Any]]:
    return {"t460": {"clients": {k: {"state": v, "target": k} for k, v in targets.items()}}}


def test_the_next_card_goes_when_the_one_before_is_over() -> None:
    p = run.validate(cards_plan())
    wired, wifi = (t.id for t in p.targets)
    assert run.next_steps(p, hb(**{wired: "running"}), 50) == []
    (go,) = run.next_steps(p, hb(**{wired: "done"}), 95.5)
    assert go.path.endswith("/client/start") and go.params["target"] == wifi
    assert run.next_steps(p, hb(**{wired: "done", wifi: "running"}), 99) == []
    assert not run.finished(p, hb(**{wired: "done", wifi: "running"}))
    assert run.finished(p, hb(**{wired: "done", wifi: "failed"}))
    assert run.measuring({"clients": {"x": {"state": "running"}}}) and run.measuring(
        {}
    )  # waits its turn


def test_the_wan_side_steps_when_the_card_asks() -> None:
    p = run.validate(cards_plan())
    wired = p.targets[0].id
    asking = hb(**{wired: "running"})
    asking["t460"]["clients"][wired]["need"] = {"wan": "next", "seq": 3}
    (s,) = run.next_steps(p, asking, 10)
    assert (s.device, s.path, s.params) == (
        "probook",
        f"{run.PREFIX}/server/link",
        {"what": "next", "seq": 3, "target": wired},
    )
    busy = {**asking, "probook": {"server": {"link": {"seq": 3, "target": wired, "busy": True}}}}
    assert run.next_steps(p, busy, 11) == []  # the WAN card is changing
    done = {
        **asking,
        "probook": {
            "server": {
                "link": {
                    "seq": 3,
                    "target": wired,
                    "busy": False,
                    "mode": "100 Mbps full",
                    "speed": 100,
                }
            }
        },
    }
    (r,) = run.next_steps(p, done, 30)
    assert r.path.endswith("/client/resume") and r.params["seq"] == 3
    assert r.params["answer"] == {"mode": "100 Mbps full", "speed": 100}


def case(link: int | None, send: float | None, recv: float | None, wired: bool = True,
         **extra: Any) -> dict[str, Any]:  # fmt: skip
    def r(v: float | None) -> dict[str, Any]:
        return {"ok": v is not None, "mbps": v, "error": "" if v else "iperf3: refused"}

    return {"case": {"label": "x", "requested_mbps": None, "negotiated_mbps": link},
            "send": r(send), "receive": r(recv), "not_run": None,
            "judged": {"wired": wired, "pct": 90}, **extra}  # fmt: skip


def test_a_wired_case_passes_at_90_percent_of_the_link_it_got() -> None:
    assert run.cell_color(case(1000, 949.2, 941.6))[0] is Color.GREEN
    color, why = run.cell_color(case(1000, 897.9, 941.6))  # the reference's LAN2 FAIL
    assert color is Color.RED and why == ("LAN→WAN 898 Mbps, below 900 (90 % of 1000 Mbps)",)
    assert run.cell_color(case(100, 94.9, 94.8))[0] is Color.GREEN  # 2500 asked, 100 got
    assert run.cell_color(case(1000, None, 941.6))[0] is Color.RED  # a direction failed
    grey = case(None, None, None, not_run="the link is down")
    assert run.cell_color(grey) == (Color.GREY, ("not run: the link is down",))


def test_wifi_has_no_threshold_values_only() -> None:
    assert run.cell_color(case(866, 300.0, 280.0, wired=False))[0] is Color.GREEN
    vals = {v["key"]: v["value"] for v in run.cell_values(case(866, 300.0, 280.0, wired=False))}
    assert vals["threshold"] is None and vals["send"] == 300.0


def test_box_values_and_totals() -> None:
    vals = {v["key"]: v["value"] for v in run.cell_values(case(1000, 949.2, 941.6))}
    assert vals["link"] == 1000 and vals["threshold"] == 900.0 and vals["receive"] == 942.0  # shown
    t = run.device_totals([case(1000, 949.2, 941.6), case(1000, 800.0, 941.6),
                           case(None, None, None, not_run="down")])  # fmt: skip
    assert (t["cases"], t["pass"], t["fail"], t["not_run"]) == (3, 1, 1, 1)
    assert t["send_max_mbps"] == 949.2
    assert run.receiver_minimum_met([case(None, None, None, not_run="down")])[0] is False


def test_the_row_shows_the_card_and_the_last_rates() -> None:
    p = run.validate(cards_plan())
    row = run.live_row(p, {"clients": {"t460@192.168.1.237": {
        "state": "running", "target": "t460@192.168.1.237", "doing": "Auto: send",
        "results": [{"label": "Auto", "send": 941.6, "receive": 940.1, "not_run": None}]}}},
        {})  # fmt: skip
    rates = [(i["label"], i["value"]) for i in row["items"] if i["value"] is not None]
    assert rates == [("LAN→WAN", 942.0), ("WAN→LAN", 940.0)]  # plain text, rounded
    # what the PC is, then what it does now, in words (2026-10-17 user: "아무말 대잔치")
    assert [i["label"] for i in row["items"][:3]] == [
        "LAN side", "now: wired card 192.168.1.237 · Auto (both cards on Auto) · measuring "
        "LAN→WAN", "last: Auto (both cards on Auto) ·"
    ]  # fmt: skip
    server = run.live_row(p, {"server": {"state": "RUNNING", "link": {"mode": "100 Mbps half"}}},
                          {})  # fmt: skip
    assert server["items"][0]["label"] == (
        "WAN side · every LAN card measures to this PC · WAN card: 100 Mbps half"
    )
    later = run.live_row(p, {}, {})  # a PC before its turn
    assert [i["label"] for i in later["items"]] == ["LAN side", "waits its turn"]


def test_the_controller_pc_is_a_lan_card_too() -> None:
    """2026-10-17 user: the reference ran the Controller as a LAN agent too -- "그래야 모든
    LAN포트 검증을 하지". This PC (reached at 127.0.0.1) takes the LAN the others are on."""
    p = cards_plan()
    p["cards"]["this-pc"] = [{"nic": "Ethernet", "ip": "192.168.1.232"},
                             {"nic": "Wi-Fi", "ip": "192.168.0.10", "wifi": None}]  # fmt: skip
    p["hosts"]["this-pc"] = "127.0.0.1"
    got = run.validate(p)
    assert [(t.device, t.ip, t.kind) for t in got.targets] == [
        ("t460", "192.168.1.237", "wired"), ("this-pc", "192.168.1.232", "wired"),
        ("t460", "192.168.1.114", "wifi")]  # fmt: skip


def test_a_pc_waiting_its_turn_may_be_idle() -> None:
    """2026-10-17: the Controller stopped the test because this-pc was IDLE -- its card's
    turn had not come. The WAN-side PC serves all along: never idle."""
    p = run.validate(cards_plan())
    assert run.may_be_idle(p, "t460") and not run.may_be_idle(p, "probook")


def test_how_it_measures_is_said_with_the_command_and_the_times() -> None:
    """2026-10-17 user: "iperf option이 뭔지 안적혀있어", "몇초동안 테스트하는지도 모르고",
    "몇초 대기하는지도 모르고 설정간에"."""
    lines = run.method(run.validate(plan()))
    text = "\n".join(lines)
    assert "iperf3 -c 10.0.0.1 -p 5211 -t 30 -J -P 1" in text and "-R" in text
    assert "never both at once" in text and "LAN→WAN 30 s" in text and "WAN→LAN 30 s" in text
    assert "10 s before a PC's first case" in text and "up to 20 s" in text
    chosen: dict[str, Any] = {"S-63": "LAN→WAN only", "S-60": "UDP", "S-64": 80}
    one = run.validate(plan(**chosen))
    text = "\n".join(run.method(one))
    assert (
        "LAN→WAN 30 s" in text and "WAN→LAN" not in text.split("Command")[0].split("Each case")[1]
    )
    assert "-u -b <80 % of the link>M" in text and one.udp_pct == 80
    assert run.live_view(one)["method"] == run.method(one)


def test_a_case_with_one_direction_is_judged_on_that_one() -> None:
    s = {"case": {"negotiated_mbps": 1000}, "send": {"ok": True, "mbps": 940.0},
         "receive": None, "not_run": None,
         "judged": {"wired": True, "pct": 90, "directions": ["send"]}}  # fmt: skip
    assert run.cell_color(s) == (Color.GREEN, ())


def test_the_screen_shows_what_start_will_run_before_start() -> None:
    """2026-10-17 user: the Test area shows the iperf3 side, not Video quality."""
    raw = {**plan(), "hosts": {}, "settings": {"S-58": 20, "S-63": "WAN→LAN only"}}
    text = "\n".join(run.preview(raw))
    assert "iperf3 -c 10.0.0.1 -p 5211 -t 20" in text and "WAN→LAN 20 s" in text
    assert "only" in text and "LAN→WAN 20 s" not in text


def test_the_rules_the_controller_loads_say_how_it_measures() -> None:
    """2026-10-17: method and preview were module functions but not on the object the
    Controller loads; the Test area preview and the report's "How it was measured" were
    empty in the real app."""
    rules = run.create()
    p = run.validate(plan())
    assert rules.method(p) == run.method(p)
    assert rules.preview({**plan(), "hosts": {}}) == run.preview({**plan(), "hosts": {}})


def test_each_case_names_both_ends() -> None:
    """2026-10-17 user: "t460-rx card를 왜 내 컴퓨터랑 속도를 재니? LAN to LAN이야?" -- every case
    is a LAN-side card and the WAN-side PC; the table says so."""
    s = {"device": "t460-rx", "between": {"lan": "192.168.45.139", "wan": "10.0.0.1"},
         "case": {"label": "Auto", "nic": "enp0s31f6"}, "judged": {}}  # fmt: skip
    vals = {v["key"]: v["value"] for v in run.cell_values(s)}
    assert vals["between"] == "t460-rx 192.168.45.139 (LAN) ⇄ 10.0.0.1 (WAN)"
    assert vals["card"] == "wired 192.168.45.139 (enp0s31f6)"
    detail = {v["key"] for v in run.cell_values(s) if v.get("detail")}
    assert "between" in detail and "command" in detail  # on hover, not a column
    shown = [v["label"] for v in run.cell_values(s) if not v.get("detail")]
    assert shown == ["LAN-side card", "Case", "Link", "LAN→WAN", "WAN→LAN"]


def test_the_table_says_which_card_the_speed_was_set_on() -> None:
    """2026-10-17 user: "모두 Card값에 enp0s31f6 cable만 나오잖아" -- the measuring card is
    always the LAN-side one; the speed was set on the WAN card or on that LAN card."""
    case = {"label": "WAN card 100 Mbps half", "nic": "enp0s31f6", "kind": "wired"}
    wan = {"case": {**case, "setting": {"side": "WAN-side PC", "nic": "enp37s0"}}}
    lan = {"case": {**case, "label": "LAN card set to 10 Mbps full",
                    "setting": {"side": "LAN-side PC"}}}  # fmt: skip
    auto = {"case": {**case, "label": "Auto"}}

    def vals(s: dict[str, Any]) -> dict[str, Any]:
        return {v["label"]: v["value"] for v in run.cell_values({**s, "judged": {}})}

    assert vals(wan)["LAN-side card"] == "wired (enp0s31f6)"
    assert vals(wan)["Case"] == "WAN card set to 100 Mbps half"
    assert vals(auto)["Case"] == "Auto (both cards on Auto)"
    assert run.case_name("This PC 10 Mbps full") == "LAN card set to 10 Mbps full"  # 0033
    assert vals(wan)["Speed set on"] == "WAN card enp37s0"
    assert vals(lan)["Speed set on"] == "LAN card enp0s31f6"
    assert vals(auto)["Speed set on"] == "— (both Auto)"


def test_half_duplex_below_the_pass_level_is_yellow_check_needed() -> None:
    """2026-10-17 user: "half도 설정하고 결과는 확인요망으로 노란색으로 알려주면 될거같고"."""
    s = {"case": {"negotiated_mbps": 100, "duplex": "half"},
         "send": {"ok": True, "mbps": 48.2}, "receive": {"ok": True, "mbps": 91.3},
         "not_run": None, "judged": {"wired": True, "pct": 90}}  # fmt: skip
    color, why = run.cell_color(s)
    assert color is Color.YELLOW and why[0].startswith("check needed (half duplex): LAN→WAN 48")
    full = {**s, "case": {"negotiated_mbps": 100, "duplex": "full"}}
    assert run.cell_color(full)[0] is Color.RED
    failed = {**s, "send": {"ok": False, "error": "iperf3 did not start"}}
    assert run.cell_color(failed)[0] is Color.RED  # a run that did not happen stays red


def test_the_module_says_it_sets_link_speeds_itself() -> None:
    """The Controller does not judge links the test sets ("link at 10 Mbps ... often a cable
    or port problem" for a 10 Mbps case) nor show an IPTV test card on its rows."""
    assert run.create().changes_links is True


def test_every_wired_card_gets_the_wan_speeds_then_its_own() -> None:
    """2026-10-17 user: "auto -> wan 설정 변경 -> wan auto back -> LAN 설정 변경 -> LAN auto
    back ... 연결된 모든 LAN/WLAN에 대해서": the second wired card got no WAN speeds."""
    plan_ = {**cards_plan("wired"), "cards": {
        "probook": [{"nic": "enp37s0", "ip": "10.0.0.1"}],
        "ctl": [{"nic": "이더넷", "ip": "192.168.45.58"}],
        "t460": [{"nic": "enp0s31f6", "ip": "192.168.1.237"}]}}  # fmt: skip
    p = run.validate(plan_)
    assert [t.device for t in p.targets] == ["ctl", "t460"]
    for t in p.targets:
        assert run.target_cases(p, t) == [
            {"label": "Auto", "speed_mbps": None}, {"phase": "wan_modes"}, {"phase": "own_modes"}
        ]  # fmt: skip


def test_only_the_ticked_cards_are_measured() -> None:
    """2026-10-17 user: "연결된 포트가 있으면 그만큼 row가 늘어나야지 그래야 사용자가 고르지"."""
    ticks: dict[str, Any] = {"S-65": {"t460": {"wlx58": {"bands": {
        "2.4GHz": {"ssid": "R-2G", "bssid": "AA:00:00:00:00:01"}}}}}}  # fmt: skip
    p = run.validate(cards_plan(**ticks))
    (t,) = p.targets
    assert (t.device, t.ip, t.kind, t.nic) == ("t460", "192.168.1.114", "wifi", "wlx58")
    assert t.bands == (("2.4GHz", "R-2G", "AA:00:00:00:00:01", "Open"),)
    assert run.target_cases(p, t) == [{"label": "Auto", "speed_mbps": None},
                                      {"phase": "wifi_bands"}]  # fmt: skip
    with pytest.raises(ZlError) as e:
        none: dict[str, Any] = {"S-65": {}}
        run.validate(cards_plan(**none))
    assert "Tick the cards to test in Devices" in e.value.fix


def test_the_row_shows_the_rate_every_second_while_measuring() -> None:
    """2026-10-22 user: "실시간 속도 넣어" — the card's rate while iperf3 runs."""
    p = run.validate(cards_plan())
    row = run.live_row(p, {"clients": {"t460@192.168.1.237": {
        "state": "running", "target": "t460@192.168.1.237", "doing": "Auto: receive",
        "live": {"direction": "WAN→LAN", "mbps": 687.4, "s": 12}, "results": []}}},
        {})  # fmt: skip
    assert [(i["label"], i["value"]) for i in row["items"][2:4]] == [
        ("WAN→LAN now", 687.4), ("(12 s) ·", None)]  # fmt: skip


def test_the_row_says_a_setting_was_put_back_or_not() -> None:
    """2026-10-22 user: "설정 원복이 안되면 실패야"."""
    p = run.validate(cards_plan())

    def row(back: dict[str, Any]) -> dict[str, Any]:
        return run.live_row(p, {"clients": {"t460@192.168.1.237": {
            "state": "running", "target": "t460@192.168.1.237", "doing": "Auto: send",
            "put_back": [back], "results": []}}}, {})  # fmt: skip

    ok = row({"card": "WAN card", "ok": True, "to": "1000 Mbps", "why": None})
    assert "put back: WAN card ✓ 1000 Mbps ·" in [i["label"] for i in ok["items"]]
    assert ok["color"] == "green"
    bad = row({"card": "enp0s31f6", "ok": False, "to": "", "why": "no link"})
    assert bad["color"] == "red" and "not put back" in str(bad["why"])


def test_a_half_duplex_setting_is_check_needed_wherever_it_was_set() -> None:
    """2026-10-22: the WAN card's and the Controller's half cases were red, not yellow."""
    slow = {"ok": True, "mbps": 41.0}

    def case(label: str, duplex: str | None) -> dict[str, Any]:
        return {"case": {"label": label, "negotiated_mbps": 100, "duplex": duplex,
                         "setting": {"requested": label.rpartition("to ")[2]}},
                "send": slow, "receive": slow}  # fmt: skip

    assert run.judge_case(case("WAN card set to 100 Mbps half", "full"))[0] is Color.YELLOW
    assert run.judge_case(case("LAN card set to 100 Mbps half", None))[0] is Color.YELLOW
    assert run.judge_case(case("LAN card set to 100 Mbps full", None))[0] is Color.RED


def test_the_pcs_of_a_throughput_test_get_longer_before_they_count_as_lost() -> None:
    """2026-10-29 R20261029-214745: the WAN card at 10 Mbps half duplex, filled by iperf3,
    was the Controller's only way to the WAN-side PC; 10 s of silence stopped the test."""
    from modules.throughput.run import SLOWED_LINK_LOST_S, ThroughputRun, lost_after_s

    p = SimpleNamespace(sender="probook-sender", clients=["t460-rx", "controller"])
    assert lost_after_s(p, "probook-sender") == SLOWED_LINK_LOST_S == 120.0  # type: ignore[arg-type]
    assert lost_after_s(p, "t460-rx") == SLOWED_LINK_LOST_S  # type: ignore[arg-type]
    assert lost_after_s(p, "other-pc") == 0.0  # type: ignore[arg-type]
    assert ThroughputRun.lost_after_s is not None
