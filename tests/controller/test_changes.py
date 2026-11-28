"""F24: what changed since the previous run on this router (03 F24 완료 기준)."""

from __future__ import annotations

import copy
import json
import random
from pathlib import Path
from typing import Any

from controller.changes import changes_since_previous, compare, line


def run(quality: str = "UHD", channel: int = 36, width: int | None = 80, rssi: float = -40.0,
        build: str = "20261128.0746", s05: Any = None) -> tuple[Any, Any, Any]:  # fmt: skip
    summary = {
        "test_type": "tablet", "quality": quality, "planned_s": 3600,
        "dut": {"model": None, "mode_detected": "NAT", "wan_ip_way": "dhcp",
                "multicast_path": "primed"},
        "source": {"1": "movie.mp4"},
        "settings_changed": {} if s05 is None else {"S-05": s05},
        "versions": {"controller": build, "agents": {"tx": build, "rx": build}, "app": None},
    }  # fmt: skip
    plan = {"devices": {"sender": "tx", "receivers": [{"device": "tab", "ch": 1, "serial": "X"}]}}
    cells = {"tab": [{"wifi": {"band": "5GHz", "channel": channel, "width_mhz": width,
                               "rssi_dbm_min": rssi}}]}  # fmt: skip
    return summary, plan, cells


def names(items: list[dict[str, Any]]) -> list[str]:
    return [x["name"] for x in items]


def test_four_changes_show_and_nothing_else() -> None:
    """F24 1: a Wi-Fi channel, one setting and a tool build changed (link speeds are not in
    the run files yet)."""
    got = compare(run(), run(channel=149, width=40, s05="x", build="20261129.0100"))
    assert names(got) == ["tab Wi-Fi", "Setting S-05", "Controller build", "rx build", "tx build"]
    wifi = got[0]
    assert wifi["before"] == "5GHz ch36/80 MHz" and wifi["after"] == "5GHz ch149/40 MHz"
    assert wifi["worse"]  # narrower
    assert all(x["worse"] for x in got[2:])  # another tool build


def test_nothing_changed_and_first_run(tmp_path: Path) -> None:
    assert compare(run(), run()) == []
    assert line({"run_id": "R20261127-100000", "items": []}) == (
        "Nothing changed since R20261127-100000"
    )
    assert line({"run_id": None, "items": []}) == "First run for this router"
    s, p, c = run()
    me = tmp_path / "R20261128-090000"
    me.mkdir()
    assert changes_since_previous(me, s, p, c) == {"run_id": None, "items": []}
    old = tmp_path / "R20261127-100000"
    old.mkdir()
    (old / "summary.json").write_text(json.dumps(s), encoding="utf-8")
    (old / "plan.json").write_text(json.dumps(p), encoding="utf-8")
    (old / "summaries.jsonl").write_text(
        json.dumps({"device": "tab", **c["tab"][0]}) + "\n", encoding="utf-8"
    )
    other = tmp_path / "R20261128-080000"  # another router (another sender): not compared
    other.mkdir()
    (other / "summary.json").write_text(json.dumps(s), encoding="utf-8")
    p2 = copy.deepcopy(p)
    p2["devices"]["sender"] = "another-pc"
    (other / "plan.json").write_text(json.dumps(p2), encoding="utf-8")
    assert changes_since_previous(me, s, p, c) == {"run_id": "R20261127-100000", "items": []}


def test_a_missing_width_or_a_small_rssi_change_is_not_a_change() -> None:
    assert compare(run(width=80), run(width=None)) == []  # the width read comes later
    assert compare(run(rssi=-40), run(rssi=-44)) == []  # the air, not a change
    weak = compare(run(rssi=-40), run(rssi=-55))
    assert names(weak) == ["tab lowest RSSI"] and weak[0]["worse"]


def test_the_test_wifi_password_never_shows() -> None:
    a, b = run(), run()
    a[0]["settings_changed"] = {"S-51": "ssid-a/WPA2/secret-one"}
    b[0]["settings_changed"] = {"S-51": "ssid-a/WPA2/secret-two"}
    (x,) = compare(a, b)
    assert x["name"] == "Setting S-51" and "secret" not in json.dumps(x)


def test_random_pairs_show_what_changed() -> None:
    """F24 4 (monkey): 200 random pairs, what was changed = what is shown."""
    rnd = random.Random(28)
    knobs: dict[str, tuple[str, list[Any]]] = {
        "Quality": ("quality", ["HD", "UHD"]),
        "tab Wi-Fi": ("channel", [1, 6, 36, 149]),
        "Setting S-05": ("s05", [None, "a", "b"]),
        "Controller build": ("build", ["20261128.0746", "20261129.0100"]),
    }
    for _ in range(200):
        a: dict[str, Any] = {key: rnd.choice(vals) for key, vals in knobs.values()}
        b = dict(a)
        flipped = rnd.sample(sorted(knobs), rnd.randint(0, len(knobs)))
        for name in flipped:
            key, vals = knobs[name]
            b[key] = rnd.choice([v for v in vals if v != a[key]])
        got = set(names(compare(run(**a), run(**b))))
        expect = set(flipped)
        if "Controller build" in flipped:
            expect |= {"tx build", "rx build"}
        assert got == expect, (a, b, got)


def test_tool_and_app_versions_are_recorded_and_compared() -> None:
    """versions.app and versions.tools were always null (2026-11-28), so F24 never saw a
    tool or tablet app change."""
    from controller.run import _versions_of

    ffmpeg = {"path": "/usr/bin/ffmpeg", "version": "6.1.1"}
    infos: dict[str, dict[str, Any]] = {
        "rx": {"tools": {"ffmpeg": ffmpeg, "vlc": {"path": None, "version": None}},
               "tablet_app": "20261128.0746"},
        "tx": {"tools": {"tsp": {"path": "/usr/bin/tsp", "version": "3.45"}}},
    }  # fmt: skip
    plan = {"devices": {"receivers": [{"device": "tab", "host": "rx", "serial": "X"}]}}
    got = _versions_of(infos, plan)
    assert got == {"app": {"rx": "20261128.0746"},
                   "tools": {"rx": {"ffmpeg": "6.1.1"}, "tx": {"tsp": "3.45"}}}  # fmt: skip
    a, b = run(), run()
    a[0]["versions"].update(got)
    newer = {"rx": {"ffmpeg": "7.0"}, "tx": {"tsp": "3.45"}}
    b[0]["versions"].update({"app": {"rx": "20261129.0100"}, "tools": newer})
    items = compare(a, b)
    assert names(items) == ["Tablet app (rx)", "rx ffmpeg"] and all(x["worse"] for x in items)
    assert compare(a, run()) == []  # an older run without them: nothing to compare


def test_a_slower_link_is_marked() -> None:
    """F24 1: the link speed (02 10 links_at_start, the wired cards at Start)."""
    from controller.run import _links_of

    links = [{"nic": "eth0", "up": True, "link_mbps": 2500},
             {"nic": "wlan0", "up": True, "link_mbps": None},
             {"nic": "eth1", "up": False, "link_mbps": 1000}]  # fmt: skip
    infos: dict[str, dict[str, Any]] = {"tx": {"links": links}, "rx": {"links": []}}
    assert _links_of(infos) == {"tx": {"eth0": 2500}}
    a, b = run(), run()
    a[0]["links_at_start"] = {"tx": {"eth0": 2500}}
    b[0]["links_at_start"] = {"tx": {"eth0": 1000, "eth9": 100}}  # eth9: only now
    (x,) = compare(a, b)
    assert (x["name"], x["before"], x["after"], x["worse"]) == (
        "tx eth0 link",
        "2500 Mbps",
        "1000 Mbps",
        True,
    )


def test_a_value_first_known_now_is_not_a_change() -> None:
    """R20261128-121753: "Router model - -> …" the first time the model was read."""
    a, b = run(), run()
    b[0]["dut"]["model"] = "Maker AX-1"
    assert compare(a, b) == []
    a[0]["dut"]["model"] = "Maker AX-0"
    assert names(compare(a, b)) == ["Router model"]
