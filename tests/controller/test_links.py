"""Controller link judgement (docs/05-standards.md 1.8, 09 NET-012/013/014/015):
needed rate x 1.1, usual speed, one event per change, never a flood."""

from __future__ import annotations

import random
from typing import Any

import pytest

from controller.links import LinkJudge, precheck
from core.protocol.envelope import ZlError

NEED = {"tx": {"bps": 105_000_000, "label": "WAN"}, "rx": {"bps": 35_000_000, "label": "LAN"}}


def wired(
    mbps: int | None = 2500, up: bool = True, duplex: str = "full", nic: str = "eth0"
) -> dict[str, Any]:
    return {"nic": nic, "ip": "10.0.0.2", "up": up, "link_mbps": mbps, "duplex": duplex}


def judge(usual: int = 2500) -> LinkJudge:
    return LinkJudge(NEED, {"tx": {"10.0.0.2": usual}, "rx": {}})


def levels(j: LinkJudge, dev: str) -> list[str]:
    return [c["level"] for c in j.checks(dev)]


@pytest.mark.parametrize(
    ("mbps", "level"),
    [
        (2500, "ok"),
        (1000, "slow"),
        (116, "slow"),
        (115, "short"),
        (100, "short"),
        (10, "short"),
        (None, "unknown"),
    ],
)
def test_levels(mbps: int | None, level: str) -> None:
    j = judge()
    j.update("tx", [wired(mbps)], 1.0)
    assert levels(j, "tx") == [level]  # 105 Mbps x 1.1 = 115.5


def test_down_and_wireless() -> None:
    j = judge()
    j.update(
        "tx",
        [wired(up=False), {"nic": "wlan0", "ip": "x", "up": True, "wifi": {"channel": 36}}],
        1.0,
    )
    assert levels(j, "tx") == ["down"]  # Wi-Fi is not judged here


def test_usual_is_the_start_speed_when_higher() -> None:
    j = judge(usual=1000)
    j.update("tx", [wired(2500)], 0.0)  # first reading of the run
    j.update("tx", [wired(1000)], 5.0)
    assert levels(j, "tx") == ["slow"]
    assert j.checks("tx")[0]["usual_mbps"] == 2500


def test_one_event_per_change_with_what_why_fix() -> None:
    j = judge()
    assert j.update("tx", [wired(2500)], 0.0) == []
    ev = j.update("tx", [wired(100)], 12.0)
    assert [(e.code, e.level) for e in ev] == [("ZL-NET-013", "error")]
    assert "2.5 Gbps → 100 Mbps" in ev[0].text and ev[0].device == "tx"
    assert "needs about 116 Mbps" in ev[0].data["why"] and ev[0].data["fix"]
    assert j.update("tx", [wired(100)], 13.0) == []  # same level: nothing again
    ev = j.update("tx", [wired(1000)], 20.0)
    assert [e.code for e in ev] == ["ZL-NET-014"]
    assert "Still enough" in ev[0].data["why"]
    ev = j.update("tx", [wired(2500)], 30.0)
    assert [(e.code, e.level) for e in ev] == [("ZL-INFO", "info")]  # back to normal
    c = j.checks("tx")[0]
    assert c["since_elapsed_s"] == 30.0 and c["needed_mbps"] == 115.5


def test_down_is_left_to_the_node() -> None:
    """The node already sends NET-015; the Controller only shows it (no duplicate)."""
    j = judge()
    j.update("tx", [wired()], 0.0)
    assert j.update("tx", [wired(up=False)], 1.0) == []
    assert levels(j, "tx") == ["down"]


def test_precheck_blocks_short_and_warns_half_duplex() -> None:
    with pytest.raises(ZlError) as e:
        precheck("tx", [wired(100)], NEED["tx"])
    assert e.value.code == "ZL-NET-012" and "needs 116 Mbps" in e.value.what
    assert precheck("tx", [wired(2500)], NEED["tx"]) == []
    warn = precheck("tx", [wired(1000, duplex="half")], NEED["tx"])
    assert [w.code for w in warn] == ["ZL-NET-012"]
    assert precheck("tx", [wired(None)], NEED["tx"]) == []  # unknown speed: not blocked


def test_random_flapping_never_floods() -> None:
    """Monkey: an event exactly when the level becomes short / slow, or goes back to ok
    from them; at most one per reading; checks always follow the last reading."""
    for seed in range(20):
        rnd = random.Random(seed)
        j = judge()
        last: str | None = None
        for t in range(500):
            link = wired(rnd.choice([2500, 2500, 1000, 100, 10, None]), up=rnd.random() > 0.1)
            events = j.update("tx", [link], float(t))
            (level,) = levels(j, "tx")
            want = level != last and (
                level in ("short", "slow") or (level == "ok" and last in ("short", "slow"))
            )
            assert len(events) == int(want), (seed, t, last, level)
            last = level


def test_wifi_change_across_a_drop_is_kept_for_the_screen() -> None:
    """2026-10-09 wireless run: 153 -> (9 s off the air) -> 40 showed no change."""
    from types import SimpleNamespace
    from typing import cast

    from controller.run import DeviceWatch, RunController

    def link(ch: int | None) -> list[dict[str, Any]]:
        wifi = None if ch is None else {"channel": ch, "width_mhz": None, "band": "5GHz",
                                        "bssid": "b"}  # fmt: skip
        return [{"nic": "wlx0", "ip": "192.168.45.190", "up": ch is not None, "wifi": wifi}]

    w = DeviceWatch(device=cast(Any, None), client=cast(Any, None))
    me = cast(RunController, SimpleNamespace(_measure_start=None))
    for ch in (153, None, None, 40):
        RunController._note_wifi(me, w, link(ch))
    assert [(c["from"]["channel"], c["to"]["channel"]) for c in w.wifi_changes] == [(153, 40)]


def test_a_wifi_card_that_dropped_is_not_a_wired_link_left_down() -> None:
    """2026-10-22: the T460's home Wi-Fi dropped 3 s at 03:00:09; down it looked wired and
    "LAN link down since 03:00:09" stayed on the screen for 8 hours."""
    from controller.links import LinkJudge

    j = LinkJudge({"t460": {"bps": 35_000_000, "label": "LAN"}}, {})
    wifi = {"nic": "wlp4s0", "ip": "192.168.0.13", "up": True, "wifi": {"band": "5GHz"}}
    j.update("t460", [wifi], 0.0)
    j.update("t460", [{"nic": "wlp4s0", "ip": None, "up": False}], 1.0)  # down: no Wi-Fi keys
    assert j.checks("t460") == []
    j.update("t460", [wifi], 4.0)
    assert j.checks("t460") == []
