"""The tablet test in the IPTV rules (01-overview 4, D3): one to three tablets, same or
different channels, each started and stopped through the PC it hangs on by USB."""

from __future__ import annotations

from typing import Any

import pytest

from core.protocol.envelope import ZlError
from modules.iptv.run import (
    measuring,
    probe_steps,
    receiver_rows,
    required_tools,
    second_records,
    start_steps,
    stop_steps,
    validate,
)


def tab(device: str, ch: int, serial: str, **more: Any) -> dict[str, Any]:
    return {"device": device, "ch": ch, "host": "rx-pc", "serial": serial, **more}


def plan(*tablets: dict[str, Any], **settings: Any) -> dict[str, Any]:
    return {"schema_version": 1, "module": "iptv", "test_type": "tablet", "quality": "UHD",
            "duration_s": 600, "settings": settings,
            "devices": {"sender": "tx", "receivers": list(tablets)}}  # fmt: skip


THREE = (tab("tablet-1", 1, "A1"), tab("tablet-2", 1, "B2", buffer_ms=2000),
         tab("tablet-3", 2, "C3"))  # fmt: skip


def test_three_tablets_two_on_one_channel() -> None:
    p = validate(plan(*THREE))
    assert p.receivers == (("tablet-1", 1), ("tablet-2", 1), ("tablet-3", 2))
    assert p.nodes == ("rx-pc",) and p.node_of("tablet-2") == "rx-pc"
    assert p.on_air == (1, 2) and not p.primed


def test_start_goes_through_the_usb_pc_with_each_buffer() -> None:
    p = validate(plan(*THREE, **{"S-25": 1500}))
    senders, receivers = start_steps(p)
    assert [s.params["ch"] for s in senders] == [1, 2]
    assert {s.device for s in receivers} == {"rx-pc"}
    assert all(s.path.endswith("/tablet/start") for s in receivers)
    by = {s.params["device"]: s.params for s in receivers}
    assert by["tablet-1"]["serial"] == "A1" and by["tablet-1"]["buffer_ms"] == 1500  # S-25
    assert by["tablet-2"]["buffer_ms"] == 2000  # its own
    assert by["tablet-3"]["group"] == "239.1.1.2" and by["tablet-3"]["play"] is True


def test_stop_each_tablet_by_name_and_no_pc_checks() -> None:
    steps = stop_steps(validate(plan(*THREE)))
    first = steps[0][0]
    assert (first.device, first.path.endswith("/tablet/stop"), first.params) == (
        "rx-pc", True, {"device": "tablet-1"})  # fmt: skip
    assert not any("perfcheck" in s.path for stage in steps for s in stage)


def test_the_usb_pc_needs_adb_and_no_test_send() -> None:
    p = validate(plan(*THREE))
    assert required_tools(p, "rx-pc") == ["adb"]
    assert probe_steps(p) == [[], [], []]


@pytest.mark.parametrize("bad", [
    [],  # none
    [tab("tablet-1", 1, "A1")] * 2,  # the same tablet twice
    [tab(f"tablet-{i}", 1, f"S{i}") for i in range(1, 5)],  # four
    [{"device": "tablet-1", "ch": 1}],  # no host / serial
])  # fmt: skip
def test_refused(bad: list[dict[str, Any]]) -> None:
    with pytest.raises(ZlError) as e:
        validate(plan(*bad))
    assert e.value.code == "ZL-RUN-014"


def test_heartbeat_tablets_are_records_measuring_and_rows() -> None:
    p = validate(plan(*THREE[:2]))
    last = {"e": 7, "ch": 1, "rx": 3300, "lost": 0, "jit": 1.0, "pb": "ok"}
    data = {"receivers": [], "tablets": [
        {"id": "tablet-1", "ch": 1, "measuring": True, "lost_total": 0, "last_1s": last},
        {"id": "tablet-2", "ch": 1, "measuring": False, "away": True, "last_1s": None},
    ]}  # fmt: skip
    assert [k for k, _ in second_records(p, "rx-pc", data)] == [("tablet-1", 1)]
    assert not measuring(data)
    rows = receiver_rows(p, data, {})
    assert rows["tablet-1"]["color"] == "green" and rows["tablet-2"]["color"] == "red"


def test_a_tablets_wifi_goes_in_its_start_without_a_password() -> None:
    wifi = {"ssid": "ZL_WIFI_IPTV_TEST", "security": "WPA2"}
    p = validate(plan(tab("tablet-1", 1, "A1", wifi=wifi), tab("tablet-2", 2, "B2")))
    by = {s.params["device"]: s.params for s in start_steps(p)[1]}
    assert by["tablet-1"]["bands"] == [wifi] and "password" not in str(by)
    assert "bands" not in by["tablet-2"]  # none picked: it stays on its Wi-Fi
    with pytest.raises(ZlError):
        validate(plan(tab("tablet-1", 1, "A1", wifi={"ssid": "x", "security": "WEP"})))


def test_the_pc_receiver_and_two_tablets_one_on_another_channel() -> None:
    """2026-10-23 user: "수신기 pc 한대, 태블릿 두대 ... 한대는 다른 영화로": the PC
    receives CH1 as in a PC test (its player, its checks), the tablets through it."""
    pc = {"device": "rx-pc", "ch": 1}
    p = validate(plan(pc, tab("tablet-1", 1, "A1"), tab("tablet-2", 2, "B2"),
                      **{"S-48": {"1": "a.mp4", "2": "b.mp4"}}))  # fmt: skip
    assert p.on_air == (1, 2) and p.sources == {1: "a.mp4", 2: "b.mp4"}
    rx = start_steps(p)[1]
    assert sorted(s.path.rsplit("/", 2)[-2] for s in rx) == ["receiver", "tablet", "tablet"]
    assert p.playback is not None and required_tools(p, "rx-pc") == ["adb", "ffmpeg", "mpv"]
    listen, send, _ = probe_steps(p)
    assert [s.device for s in listen] == ["rx-pc"] and [s.params["ch"] for s in send] == [1]
    with pytest.raises(ZlError):  # two PCs
        validate(plan(pc, {"device": "pc2", "ch": 1}, tab("tablet-1", 1, "A1")))


def test_every_tool_the_rules_ask_for_is_listed_in_module_json() -> None:
    """2026-10-24 first tablet Start: INS-029 "adb is not installed" with adb there: a
    node reports only the tools its modules list (02 4 /v1/info ``tools``)."""
    import json
    from pathlib import Path

    listed = json.loads((Path(__file__).parents[2] / "modules/iptv/module.json").read_text())
    known = {t for tools in listed["tools"].values() for t in tools}
    pc = {"device": "rx-pc", "ch": 1}
    p = validate(plan(pc, tab("tablet-1", 2, "A1"), **{"S-26": "VLC"}))
    asked = {t for d in ("tx", "rx-pc") for t in required_tools(p, d)}
    asked |= {t for t in required_tools(validate(plan(pc, tab("tablet-1", 1, "A1"))), "rx-pc")}
    assert asked <= known, asked - known


def test_a_tablet_scan_second_says_the_loss_is_the_tablets() -> None:
    from modules.iptv.run import second_evidence

    said = second_evidence({"e": 5, "rx": 1100, "lost": 2200, "wsc": 1})
    assert any("tablet scanned for Wi-Fi" in x and "not the router" in x for x in said)
    assert not any("scanned" in x for x in second_evidence({"e": 5, "rx": 3300}))


def test_a_drop_with_every_packet_is_said_to_be_the_players() -> None:
    from modules.iptv.run import second_evidence

    clean = {"e": 212, "rx": 3336, "lost": 0, "cc": 0, "p1": 0, "pb": "drop", "pdr": 1, "pdd": 0}
    said = " ".join(second_evidence(clean))
    assert "receiver's player, not the router" in said and "at the picture's output" in said
    dec = " ".join(second_evidence({**clean, "pdd": 1}))
    assert "dropped by the decoder" in dec
    assert not any("player" in x for x in second_evidence({**clean, "lost": 30}))  # a network loss
