"""The WAN-side card is the Controller's way to the sender (modules/throughput/module.py).

2026-10-17: a WAN mode the router port did not take left the ProBook's card with no link;
the Controller could never say "next" or "restore" again and Reset All could not reach it.
"""

from __future__ import annotations

import itertools
from pathlib import Path

from core.blocks import nic_speed
from modules.throughput.module import ThroughputModule
from tests.core.test_nic_speed import ALL, ETHTOOL, FakeCard


class DeadAt10(FakeCard):
    """A router port that never links at 10 Mbps."""

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        out = super().__call__(argv)
        if argv[:2] != ["ethtool", "-s"] and all(a.startswith("10base") for a in self.adv):
            return 0, ETHTOOL.format(adv=" ".join(self.adv), speed="Unknown!",
                                     duplex="Unknown! (255)", link="no")  # fmt: skip
        return out


def test_a_mode_with_no_link_puts_the_card_back_at_once(tmp_path: Path) -> None:
    card = DeadAt10()
    tick = itertools.count(0, 1.0)
    m = ThroughputModule()
    m.wan_nic = "enp37s0"
    m.cards = nic_speed.NicSpeed(tmp_path / "c.json", card, sleep=lambda s: None,
                                 clock=lambda: float(next(tick)))  # fmt: skip
    m._wan_modes = [nic_speed.Mode(10, "half", "10baseT/Half")]
    m._wan_step(1, "t1", "next")
    assert m.link["mode"] == "10 Mbps half" and not m.link["link"]
    assert m.link["restored"] and card.adv == ALL.split()
    assert "enp37s0" not in (tmp_path / "c.json").read_text()  # nothing left to put back


def test_the_wan_card_goes_through_its_speeds_again_for_the_next_lan_card(
    tmp_path: Path,
) -> None:
    """After one LAN card's WAN phase ends (restore), the next one starts from the first
    speed again -- it used to get "done" at once."""
    card = FakeCard()
    m = ThroughputModule()
    m.wan_nic = "enp37s0"
    m.cards = nic_speed.NicSpeed(tmp_path / "c.json", card, sleep=lambda s: None)
    m._wan_modes = [nic_speed.Mode(10, "full", "10baseT/Full")]
    m._wan_step(1, "ctl@192.168.1.190", "next")
    assert m.link["mode"] == "10 Mbps full"
    m._wan_step(2, "ctl@192.168.1.190", "next")
    assert m.link.get("done")
    m._wan_step(3, "ctl@192.168.1.190", "restore")
    m._wan_modes = [nic_speed.Mode(10, "full", "10baseT/Full")]  # as caps() would read it
    m._wan_step(4, "t460@192.168.1.237", "next")
    assert m.link["mode"] == "10 Mbps full" and not m.link.get("done")
