"""Cause analysis rules (iptv 2.1 / 2.4, 10 row, 20 GAP-85): every rule with data that
matches and data that does not, rules without their data listed as missing, and the
findings never touch a verdict."""

from __future__ import annotations

from typing import Any

import pytest

from modules.iptv.analysis import NOT_FETCHED, analyze, losses


def cell(lost: int = 0, **over: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "loss": {"lost": lost, "duplicates": 0, "reordered_recovered": 0, "late_discarded": 0,
                 "host_drop_loss": 0},
        "jitter": {"max_ms": 2.0}, "wifi": None, "trouble": None, "start_elapsed_s": 0,
    }  # fmt: skip
    for k, v in over.items():
        s[k] = {**s[k], **v} if isinstance(s.get(k), dict) and isinstance(v, dict) else v
    return s


def secs(lost_at: dict[int, int], n: int = 900, **extra: Any) -> list[dict[str, Any]]:
    return [{"type": "s", "e": e, "lost": lost_at.get(e, 0), **extra} for e in range(n)]


def join(device: str, t: float, ch: int = 1) -> dict[str, Any]:
    return {"code": "ZL-INFO", "device": device, "run_s": t,
            "data": {"step": "/v1/modules/iptv/receiver/start", "ch": ch}}  # fmt: skip


def event(code: str, device: str, t: float) -> dict[str, Any]:
    return {"code": code, "device": device, "run_s": t, "data": {}}


def rules(result: dict[str, Any]) -> list[str]:
    return [f["rule"] for f in result["findings"]]


def missing(result: dict[str, Any]) -> dict[str, str]:
    return {m["rule"]: m["why"] for m in result["missing"]}


def test_summary_rules_match_and_do_not() -> None:
    clean = analyze({"summaries": {"rx": [cell()]}, "events": [], "seconds": None})
    assert rules(clean) == []
    own = analyze({"summaries": {"rx": [cell(10, loss={"host_drop_loss": 10})]}})
    assert rules(own) == ["R-07"] and own["findings"][0]["category"] == "Receiver"
    part = analyze({"summaries": {"rx": [cell(10, loss={"host_drop_loss": 4})]}})
    assert "R-07" not in rules(part)  # the network lost the rest
    sender = analyze({"summaries": {"rx": [cell(40, trouble={"sender_lost": 40})]}})
    assert rules(sender) == ["R-09"]
    dup = analyze({"summaries": {"rx": [cell(0, loss={"duplicates": 5})]}})
    assert rules(dup) == ["R-16"] and dup["findings"][0]["confidence"] == "medium"


def test_wireless_rules_need_both_kinds() -> None:
    wired_only = analyze({"summaries": {"rx": [cell(3)]}})
    assert {"R-03", "R-04", "R-12", "R-13"} <= set(missing(wired_only))
    strong = {"rssi_dbm_min": -50}
    both = {"pc": [cell(0)], "tab": [cell(20, wifi=strong)]}
    assert "R-03" in rules(analyze({"summaries": both}))
    both_lost = {"pc": [cell(1)], "tab": [cell(20, wifi=strong)]}
    assert "R-03" not in rules(analyze({"summaries": both_lost}))  # wired lost too
    weak = analyze({"summaries": {"pc": [cell(0)], "tab": [cell(9, wifi={"rssi_dbm_min": -75})]}})
    assert "R-04" in rules(weak) and "R-03" not in rules(weak)
    jumpy = {"pc": [cell(0)], "tab": [cell(0, wifi=strong, jitter={"max_ms": 80})]}
    assert rules(analyze({"summaries": jumpy})) == ["R-13"]
    late = {"pc": [cell(0)], "tab": [cell(0, wifi=strong, loss={"reordered_recovered": 3})]}
    assert rules(analyze({"summaries": late})) == ["R-12"]


def test_event_rules() -> None:
    for code, rule in (("ZL-NET-003", "R-01"), ("ZL-NET-005", "R-14"), ("ZL-NET-006", "R-15"),
                       ("ZL-RUN-009", "R-18")):  # fmt: skip
        r = analyze({"summaries": {}, "events": [event(code, "rx", 12.0)]})
        assert rules(r) == [rule] and r["findings"][0]["at_s"] == 12.0


def test_second_rules_wait_for_the_records() -> None:
    r = analyze({"summaries": {"rx": [cell()]}, "events": [], "seconds": None})
    assert missing(r)["R-02"] == NOT_FETCHED and missing(r)["R-05"] == NOT_FETCHED
    assert missing(r)["R-11"].startswith("later phase")


def test_loss_events_on_the_run_clock() -> None:
    got = losses({"rx|1": secs({10: 2, 11: 1, 14: 5})}, {"rx|1": 100.0})
    assert [(x.start_s, x.end_s, x.lost) for x in got] == [(110.0, 111.0, 3), (114.0, 114.0, 5)]
    assert losses({"rx|1": secs({10: 2})}, {}) == []  # no offset: left out, not guessed


def test_igmp_timeout_after_the_join() -> None:
    offsets = {"rx|1": 20.0}
    hit = analyze({"summaries": {}, "events": [join("rx", 10.0)],
                   "seconds": {"rx|1": secs({250: 30})}, "offsets": offsets})  # fmt: skip
    assert "R-02" in rules(hit)  # 20 + 250 - 10 = 260 s after the join
    miss = analyze({"summaries": {}, "events": [join("rx", 10.0)],
                    "seconds": {"rx|1": secs({100: 30})}, "offsets": offsets})  # fmt: skip
    assert "R-02" not in rules(miss)
    no_join = analyze({"summaries": {}, "events": [], "seconds": {"rx|1": secs({250: 3})},
                       "offsets": offsets})  # fmt: skip
    assert "R-02" in missing(no_join)


def test_receivers_losing_together_and_a_router_restart() -> None:
    seconds = {"a|1": secs({300: 5}), "b|2": secs({300: 5})}
    offsets = {"a|1": 10.0, "b|2": 10.4}
    r = analyze({"summaries": {}, "events": [], "seconds": seconds, "offsets": offsets})
    assert "R-05" in rules(r) and "R-05a" not in rules(r)
    down = [event("ZL-NET-015", "a", 309.0), event("ZL-NET-015", "b", 311.0)]
    r = analyze({"summaries": {}, "events": down, "seconds": seconds, "offsets": offsets})
    assert "R-05a" in rules(r)
    apart = analyze({"summaries": {}, "events": [], "offsets": offsets,
                     "seconds": {"a|1": secs({300: 5}), "b|2": secs({320: 5})}})  # fmt: skip
    assert "R-05" not in rules(apart)
    one = analyze({"summaries": {}, "events": [], "seconds": {"a|1": secs({1: 1})},
                   "offsets": {"a|1": 0.0}})  # fmt: skip
    assert missing(one)["R-05"] == "only one receiver"


def test_loss_when_another_receiver_joins() -> None:
    seconds = {"a|1": secs({400: 5}), "b|2": secs({})}
    offsets = {"a|1": 0.0, "b|2": 0.0}
    r = analyze({"summaries": {}, "events": [join("b", 401.0, 2)], "seconds": seconds,
                 "offsets": offsets})  # fmt: skip
    assert "R-06" in rules(r)
    r = analyze({"summaries": {}, "events": [join("b", 450.0, 2)], "seconds": seconds,
                 "offsets": offsets})  # fmt: skip
    assert "R-06" not in rules(r)


def test_playback_problem_with_clean_packets() -> None:
    lines = secs({}, pb="ok")
    lines[500] = {**lines[500], "pb": "freeze"}
    r = analyze({"summaries": {}, "events": [], "seconds": {"rx|1": lines},
                 "offsets": {"rx|1": 0.0}})  # fmt: skip
    assert "R-08" in rules(r)
    lines[501] = {**lines[501], "lost": 3}  # packets were bad too: not the receiver alone
    r = analyze({"summaries": {}, "events": [], "seconds": {"rx|1": lines},
                 "offsets": {"rx|1": 0.0}})  # fmt: skip
    assert "R-08" not in rules(r)
    off = analyze({"summaries": {}, "events": [], "seconds": {"rx|1": secs({})},
                   "offsets": {"rx|1": 0.0}})  # fmt: skip
    assert "R-08" in missing(off)


@pytest.mark.parametrize(("every", "hit"), [(90, True), (260, False), (125, False)])
def test_periodic_loss_that_is_not_igmp(every: int, hit: bool) -> None:
    lost = {every * k + 5: 2 for k in range(1, 5)}
    r = analyze({"summaries": {}, "events": [], "seconds": {"rx|1": secs(lost, n=every * 5 + 10)},
                 "offsets": {"rx|1": 0.0}})  # fmt: skip
    assert ("R-10" in rules(r)) is hit


def test_periodic_clean_playback_trouble_is_the_receiver() -> None:
    """GAP-105 2 (first real runs): 7 freezes every ~42 s with 0 loss. R-08 said
    receiver, R-10 said nothing. Now R-10 names the period and the receiver side."""
    lines = secs({}, n=310, pb="ok")
    for e in (35, 78, 118, 161, 203, 243, 286):  # the real run R20261003-164348
        lines[e] = {**lines[e], "pb": "freeze"}
    r = analyze({"summaries": {}, "events": [], "seconds": {"rx|1": lines},
                 "offsets": {"rx|1": 0.0}})  # fmt: skip
    found = [x for x in r["findings"] if x["rule"] == "R-10"]
    assert found and found[0]["category"] == "Receiver" and "every 42 s" in found[0]["text"]
    assert "R-08" in rules(r)
    uneven = secs({}, n=310, pb="ok")
    for e in (35, 50, 160, 170, 290):  # no steady interval: no R-10
        uneven[e] = {**uneven[e], "pb": "freeze"}
    r2 = analyze({"summaries": {}, "events": [], "seconds": {"rx|1": uneven},
                  "offsets": {"rx|1": 0.0}})  # fmt: skip
    assert "R-10" not in rules(r2)


def test_loss_after_a_link_or_wifi_change() -> None:
    seconds, offsets = {"rx|1": secs({300: 5})}, {"rx|1": 0.0}
    link = analyze({"summaries": {}, "events": [event("ZL-NET-013", "rx", 290.0)],
                    "seconds": seconds, "offsets": offsets})  # fmt: skip
    assert "R-19" in rules(link)
    before = analyze({"summaries": {}, "events": [event("ZL-NET-013", "rx", 310.0)],
                      "seconds": seconds, "offsets": offsets})  # fmt: skip
    assert "R-19" not in rules(before)
    wifi = analyze({"summaries": {}, "events": [event("ZL-NET-016", "rx", 305.0)],
                    "seconds": seconds, "offsets": offsets})  # fmt: skip
    assert "R-20" in rules(wifi)


def test_stream_waits_for_the_igmp_query_after_wifi_comes_back() -> None:
    """2026-10-09 first wireless run (GAP-134): off 9.5 s, stream back 39 s after."""
    wifi = {"channel": 153, "band": "5GHz"}
    down = {"code": "ZL-NET-015", "device": "rx", "run_s": 106.0,
            "data": {"nic": "wlx0", "before": {"nic": "wlx0", "wifi": wifi}}}  # fmt: skip
    up = {"code": "ZL-INFO", "device": "rx", "run_s": 115.0,
          "data": {"nic": "wlx0", "clears": ["ZL-NET-015"]}}  # fmt: skip
    data = {"summaries": {}, "events": [down, up], "seconds": {"rx|1": secs({154: 45517})},
            "offsets": {"rx|1": 0.0}}  # fmt: skip
    r = analyze(data)
    assert "R-21" in rules(r) and "R-19" not in rules(r)  # not a cable
    text = next(f["text"] for f in r["findings"] if f["rule"] == "R-21")
    assert text.startswith("Wi-Fi was off 9 s, then the stream came back 39 s after the link")
    quick = {**data, "seconds": {"rx|1": secs({116: 9000})}}  # back with the link
    assert "R-21" not in rules(analyze(quick))
    wired = {**down, "data": {"nic": "eth0", "before": {"nic": "eth0", "wifi": None}}}
    cable = analyze({**data, "events": [wired, {**up, "data": {"nic": "eth0",
                     "clears": ["ZL-NET-015"]}}]})  # fmt: skip
    assert "R-21" not in rules(cable) and "R-19" in rules(cable)


def test_order_strongest_then_earliest() -> None:
    events = [event("ZL-NET-005", "rx", 50.0), event("ZL-NET-003", "rx", 10.0)]
    r = analyze({"summaries": {"rx": [cell(0, loss={"duplicates": 1})]}, "events": events})
    assert rules(r) == ["R-01", "R-14", "R-16"]  # high by time, then medium


def test_single_packets_lost_over_the_air_are_named_from_the_boxes_alone() -> None:
    """GAP-156: 10-11 the 100 h run lost 1 packet in 4 boxes, the records were never
    fetched and no cause was found. From the 5-minute summaries: one at a time, the PC
    dropped none, the Wi-Fi did not change, the delay jumped -> over the air (R-22)."""

    def box(i: int, lost: int, hi: float, burst: int = 1, host: int = 0) -> dict[str, Any]:
        return {"index": i, "start_elapsed_s": i * 300, "duration_s": 300,
                "loss": {"lost": lost, "loss_events": lost, "max_burst": burst,
                         "host_drop_loss": host},
                "jitter": {"delay_high_ms": hi, "delay_low_ms": -1.0},
                "wifi": {"rssi_dbm_min": -38, "channel": 149, "channel_changes": 0,
                         "bssid_changes": 0}}  # fmt: skip

    boxes = [box(0, 0, 1.0), box(4, 1, 20.1), box(5, 1, 17.7), box(19, 1, -29.1)]
    r = analyze({"summaries": {"rx": boxes}, "events": [], "seconds": None, "offsets": {}})
    f = next(x for x in r["findings"] if x["rule"] == "R-22")
    assert f["category"] == "Router Wi-Fi" and f["at_s"] == 1200.0
    assert "3 packet(s) lost one at a time in 3 box(es)" in f["text"] and "-38 dBm" in f["text"]
    for spoil in (box(4, 1, 2.0), box(4, 1, 20.0, host=1), box(4, 3, 20.0, burst=3)):
        r2 = analyze({"summaries": {"rx": [spoil]}, "events": [], "seconds": None,
                      "offsets": {}})  # fmt: skip
        assert "R-22" not in rules(r2)  # no jump / the PC dropped it / a burst
    wired = [{**box(4, 1, 20.0), "wifi": None}]
    assert "R-22" not in rules(analyze({"summaries": {"rx": wired}, "events": [],
                                        "seconds": None, "offsets": {}}))  # fmt: skip


def test_wifi_that_held_the_packets_then_released_them() -> None:
    """2026-10-16 night run: np ~30/s for seconds, then 17~19k/s (6x) with the PC's own
    drops (hd). R-07 said "the receiver dropped them"; the cause was before the PC."""
    rows = secs({}, n=120, np=3300)
    for e in (50, 51, 52):
        rows[e] |= {"np": 40}
    rows[53] |= {"np": 19000, "hd": 5900, "lost": 5900}
    summaries = {"rx": [cell(lost=5900, loss={"host_drop_loss": 5900}, wifi={"rssi_dbm_min": -23})]}
    r = analyze({"summaries": summaries, "events": [], "seconds": {"rx|1": rows},
                 "offsets": {"rx|1": 0.0}})  # fmt: skip
    found = {f["rule"]: f for f in r["findings"]}
    assert "R-23" in found and "R-07" not in found
    assert "3 s" in found["R-23"]["text"] and "6x" in found["R-23"]["text"]
    # 2026-10-17: never "the router" alone -- the receiving card's driver holds the same way
    assert found["R-23"]["category"] == "Wi-Fi link" and "card" in found["R-23"]["text"]
    rows[51] |= {"nd": 39}  # the card dropped packets itself: the card first
    r2 = analyze({"summaries": summaries, "events": [], "seconds": {"rx|1": rows},
                  "offsets": {"rx|1": 0.0}})  # fmt: skip
    f2 = {f["rule"]: f for f in r2["findings"]}["R-23"]
    assert f2["category"] == "Receiver Wi-Fi card" and "dropped 39 packets itself" in f2["text"]
    rows[51] |= {"nd": 0}
    rows[53] |= {"hd": 0}  # a burst with no drops of its own: nothing to explain
    r = analyze({"summaries": summaries, "events": [], "seconds": {"rx|1": rows},
                 "offsets": {"rx|1": 0.0}})  # fmt: skip
    assert "R-23" not in {f["rule"] for f in r["findings"]}


def test_a_busy_receiver_pc_is_not_the_routers_wifi() -> None:
    """2026-10-17: the night run's last 64,038 drops came while the T460 was at 100 % CPU
    making evidence pictures (RCV-016 / SYS-003); the starved sampling looked like a
    Wi-Fi hold. With the PC busy then, R-23 stays quiet and R-07 (the PC) remains."""
    rows = secs({}, n=120, np=3300)
    for e in (50, 51, 52):
        rows[e] |= {"np": 40}
    rows[53] |= {"np": 19000, "hd": 5900, "lost": 5900}
    summaries = {"rx": [cell(lost=5900, loss={"host_drop_loss": 5900})]}
    busy = [{"code": "ZL-SYS-003", "device": "rx", "run_s": 40.0}]
    r = analyze({"summaries": summaries, "events": busy, "seconds": {"rx|1": rows},
                 "offsets": {"rx|1": 0.0}})  # fmt: skip
    rules = {f["rule"] for f in r["findings"]}
    assert "R-23" not in rules and "R-07" in rules


def test_one_receiver_loses_while_another_on_its_channel_gets_all() -> None:
    """R-24 (2026-10-24): a tablet lost a burst every ~35 min while the PC on the same
    channel and router got every packet: the tablet's side (its own Wi-Fi scan)."""
    pc = secs({}, rx=3300)
    tab = secs({300: 2200, 301: 2100}, rx=1200)
    tab[300]["wsc"] = 1
    other_ch = secs({300: 2000}, rx=1200)
    offsets = {"pc|1": 0.0, "tablet-2|1": 0.0, "tablet-1|2": 0.0}
    r = analyze({"summaries": {}, "events": [], "offsets": offsets,
                 "seconds": {"pc|1": pc, "tablet-2|1": tab, "tablet-1|2": other_ch}})  # fmt: skip
    hit = next(x for x in r["findings"] if x["rule"] == "R-24")
    assert hit["devices"] == ["tablet-2"] and hit["category"] == "Receiver"
    assert "scanned for Wi-Fi" in hit["text"] and "pc" in hit["text"]  # tablet-1: no peer
    same = {"a|1": secs({300: 5}, rx=3000), "b|1": secs({300: 5}, rx=3000)}
    both = analyze({"summaries": {}, "events": [], "offsets": {"a|1": 0.0, "b|1": 0.0},
                    "seconds": same})  # fmt: skip
    assert "R-24" not in rules(both)  # both lost: not one receiver's side
