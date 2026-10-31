"""Action Required (03 F22, 02 2-3): human actions opened by problem codes, done by
the problem clearing (auto) or by [Done] (human), kept apart from Problems."""

from __future__ import annotations

import random
import re

from controller.actions import CATALOG, ActionBoard
from controller.problems import CATALOG as PROBLEMS
from controller.ui.texts import BANNED
from core.events.bus import Event
from core.records.experience import ACTION_ID


def test_catalog_ids_texts_and_codes() -> None:
    """14 1: standard action_id names; F22: the representative actions; every opening
    code exists in 09 (via the Problems catalog); plain English screen text."""
    assert len(CATALOG) == 17  # 03 F22 + wifi.check, card.put_back, add_wan_route, sender wifi,
    # time_sync_on (DSC-017, 2026-10-31)
    for aid, a in CATALOG.items():
        assert ACTION_ID.match(aid), aid
        assert a["what"] and a["how"] and a["repeat"] in ("once", "every")
        for code in a["opened_by"]:
            assert code in PROBLEMS, (aid, code)
        for text in (a["what"], a["how"]):
            assert not re.search(r"[가-힣]", text)
            for word in BANNED:
                assert not re.search(rf"(?<!Wi-Fi )(?<!TV )\b{word}\b", text), (aid, text)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def ev(code: str, device: str = "tx", level: str = "error", **data: object) -> Event:
    return Event(code, level, f"{code} on {device}", device=device, data=dict(data))  # type: ignore[arg-type]


def test_problem_opens_an_action_and_its_clear_closes_it() -> None:
    done: list[tuple[str, str]] = []
    b = ActionBoard(grace=False, on_done=lambda a, actor: done.append((a["action_id"], actor)))
    b(ev("ZL-NET-013", run_id="R1"))
    b(ev("ZL-NET-015", run_id="R1"))  # the same action for the same device: one row
    s = b.snapshot()
    assert s["waiting"] == 1
    a = s["actions"][0]
    assert (a["action_id"], a["device"], a["state"], a["auto_done"]) == (
        "human.cable.check", "tx", "waiting", True,
    )  # fmt: skip
    assert a["why"] == "ZL-NET-013 on tx" and a["repeat"] == "every"
    b(Event("ZL-INFO", "info", "back", device="tx", data={"clears": ["ZL-NET-013"]}))
    a = b.snapshot()["actions"][0]
    assert (a["state"], a["done_by"]) == ("done", "auto") and a["done_utc"]
    assert done == [("human.cable.check", "auto")]


def test_manual_done_only_for_actions_that_cannot_be_detected() -> None:
    b = ActionBoard(grace=False)
    b(ev("ZL-SYS-006"))  # restart the PC: no automatic sign
    a = b.snapshot()["actions"][0]
    assert a["action_id"] == "human.pc.power_cycle" and not a["auto_done"]
    assert b.done(a["key"]) is True
    assert b.snapshot()["actions"][0]["done_by"] == "human"
    assert b.done(a["key"]) is False  # twice: nothing more
    assert b.done("nope|x") is False


def test_other_events_open_nothing() -> None:
    b = ActionBoard(grace=False)
    b(ev("ZL-RCV-020", level="warn"))  # a problem, but no human action
    b(ev("ZL-NET-013", level="info"))  # information is never an action
    assert b.snapshot() == {"waiting": 0, "actions": []}


def test_run_report_lists_human_actions() -> None:
    b = ActionBoard(grace=False)
    b(ev("ZL-NET-013", run_id="R1"))
    b(Event("ZL-INFO", "info", "back", device="tx", data={"clears": ["ZL-NET-013"]}))
    b(ev("ZL-SYS-006", device="rx", run_id="R1"))
    rep = b.run_report("R1")
    assert [(a["action_id"], a["done_by"]) for a in rep] == [
        ("human.cable.check", "auto"),
        ("human.pc.power_cycle", None),
    ]


def test_random_events_keep_invariants() -> None:
    """Monkey: one row per key, waiting first, waiting count right, done rows bounded."""
    rnd = random.Random(8)
    b = ActionBoard(grace=False)
    codes = [c for a in CATALOG.values() for c in a["opened_by"]]
    for _ in range(4000):
        dev = f"d{rnd.randrange(40)}"
        k = rnd.random()
        if k < 0.5:
            b(ev(rnd.choice(codes), device=dev))
        elif k < 0.85:
            b(Event("ZL-INFO", "info", "ok", device=dev, data={"clears": [rnd.choice(codes)]}))
        else:
            s = b.snapshot()
            if s["actions"]:
                b.done(rnd.choice(s["actions"])["key"])
        s = b.snapshot()
        keys = [a["key"] for a in s["actions"]]
        assert len(keys) == len(set(keys))
        states = [a["state"] == "waiting" for a in s["actions"]]
        assert states == sorted(states, reverse=True)
        assert s["waiting"] == sum(states)
        assert sum(not x for x in states) <= 200


def test_choices_come_from_the_event_and_are_kept_clean() -> None:
    """20 W-1: DSC-006 opens "choose the WAN card" with one choice per card; only
    well-formed choices are kept; an action opened without choices has none."""
    b = ActionBoard(grace=False)
    b(ev("ZL-DSC-006", level="warn", choose="wan_nic", choices=[
        {"id": "enp1s0", "label": "enp1s0 · 1000 Mbps · aa:bb:cc:00:00:01"},
        {"id": "enp2s0"}, {"label": "no id"}, "junk"]))  # fmt: skip
    (row,) = b.snapshot()["actions"]
    assert row["action_id"] == "human.pc.choose_wan_nic" and row["choose"] == "wan_nic"
    assert row["choices"] == [{"id": "enp1s0", "label": "enp1s0 · 1000 Mbps · aa:bb:cc:00:00:01"},
                              {"id": "enp2s0", "label": "enp2s0"}]  # fmt: skip
    assert b.waiting_row(row["key"]) == row and b.waiting_row("nope") is None
    b(ev("ZL-INFO", level="info", clears=["ZL-DSC-006"]))  # the role got ready
    assert b.waiting_row(row["key"]) is None
    b(ev("ZL-NET-013", level="warn"))
    assert b.snapshot()["actions"][0]["choices"] is None


def test_a_blip_that_closes_itself_asks_nobody() -> None:
    """2026-10-09 user: "Action Required is still there and Done cannot be clicked". A
    2 s "not answering" opened "Restart the PC", closed itself, and stayed on screen."""
    clk = Clock()
    b = ActionBoard()
    b.clock = clk
    b(ev("ZL-DSC-014", "rx", "warn", run_id="R1"))
    assert b.snapshot() == {"waiting": 0, "actions": []}  # not yet: it may come back
    clk.t += 2
    b(Event("ZL-INFO", "info", "back", device="rx", data={"clears": ["ZL-DSC-014"]}))
    clk.t += 300
    assert b.snapshot() == {"waiting": 0, "actions": []}  # never shown
    assert b.run_report("R1") == []
    b(ev("ZL-DSC-014", "rx", "warn", run_id="R1"))  # this time it stays away
    clk.t += 61
    s = b.snapshot()
    assert s["waiting"] == 1 and s["actions"][0]["action_id"] == "human.pc.power_cycle"
    assert "_waited_s" not in s["actions"][0]


def test_a_wifi_drop_asks_for_the_router_wifi_not_a_cable() -> None:
    b = ActionBoard(grace=False)
    before = {"nic": "wlx0", "wifi": {"channel": 153}}
    b(ev("ZL-NET-015", "rx", nic="wlx0", before=before, run_id="R1"))
    b(ev("ZL-NET-015", "tx", nic="eth0", before={"nic": "eth0", "wifi": None}, run_id="R1"))
    got = {a["device"]: a["action_id"] for a in b.snapshot()["actions"]}
    assert got == {"rx": "human.wifi.check", "tx": "human.cable.check"}


def test_a_pc_with_its_time_sync_off_gets_a_button() -> None:
    """DSC-017 (2026-10-31 user: "명령어로 나한테 시키지 말고 앱에 반영할수있나?"): the clock is off
    and its time sync is off; the action offers the button, the plain DSC-009 does not."""
    b = ActionBoard(grace=False)
    b(ev("ZL-DSC-009", device="t460-rx", level="warn"))
    assert b.snapshot()["actions"] == []  # a warning only
    b(ev("ZL-DSC-017", device="t460-rx", level="warn", choose="time_sync",
         choices=[{"id": "t460-rx", "label": "Turn on time sync on t460-rx"}]))  # fmt: skip
    (row,) = b.snapshot()["actions"]
    assert row["action_id"] == "human.pc.time_sync_on" and row["choose"] == "time_sync"
    assert row["choices"] == [{"id": "t460-rx", "label": "Turn on time sync on t460-rx"}]
