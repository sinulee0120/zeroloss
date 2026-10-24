"""A wired card's speed for a throughput case, and back as it was (core/blocks/nic_speed)."""

from __future__ import annotations

from pathlib import Path

from core.blocks import nic_speed

ETHTOOL = """Settings for enp0s31f6:
    Supported ports: [ TP ]
    Supported link modes:   10baseT/Half 10baseT/Full
                            100baseT/Half 100baseT/Full
                            1000baseT/Full
                            2500baseT/Full
    Supported pause frame use: No
    Supports auto-negotiation: Yes
    Supported FEC modes: Not reported
    Advertised link modes:  {adv}
    Advertised pause frame use: No
    Advertised auto-negotiation: Yes
    Speed: {speed}
    Duplex: {duplex}
    Auto-negotiation: on
    Port: Twisted Pair
    Link detected: {link}
"""
ALL = "10baseT/Half 10baseT/Full 100baseT/Half 100baseT/Full 1000baseT/Full 2500baseT/Full"


class FakeCard:
    """A card on a 1 Gbps router port: advertising 2500 alone still links at 1000."""

    def __init__(self) -> None:
        self.adv = ALL.split()
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv[:2] == ["ethtool", "-s"]:
            bits = int(argv[argv.index("advertise") + 1], 16) if "advertise" in argv else 0
            self.adv = [n for n, b in nic_speed.MODE_BITS.items() if bits >> b & 1
                        and n in ALL.split()]  # fmt: skip
            return 0, ""
        modes = [m for m in (nic_speed.mode_of(n) for n in self.adv) if m is not None]
        best = max(modes, key=lambda m: (min(m.speed, 1000), m.duplex == "full"))
        speed = min(best.speed, 1000)
        return 0, ETHTOOL.format(adv=" ".join(self.adv), speed=f"{speed}Mb/s",
                                 duplex=best.duplex.capitalize(), link="yes")  # fmt: skip


def test_what_the_card_can_do_is_read_from_ethtool() -> None:
    c = nic_speed.parse("enp0s31f6", ETHTOOL.format(adv=ALL, speed="1000Mb/s", duplex="Full",
                                                    link="yes"))  # fmt: skip
    assert [m.label for m in c.supported] == [
        "10 Mbps full", "10 Mbps half", "100 Mbps full", "100 Mbps half", "1000 Mbps",
        "2500 Mbps",
    ]  # fmt: skip
    assert (c.speed, c.duplex, c.autoneg, c.link) == (1000, "full", True, True)
    assert c.advertised == ALL.split()


def test_one_mode_advertised_then_back_as_it_was(tmp_path: Path) -> None:
    card = FakeCard()
    ns = nic_speed.NicSpeed(tmp_path / "custody.json", card, sleep=lambda s: None)
    got = ns.set_mode("enp0s31f6", nic_speed.Mode(100, "half", "100baseT/Half"))
    assert got["readback_ok"] and got["link"] and (got["speed"], got["duplex"]) == (100, "half")
    # 2500 asked, a 1 Gbps port: the link comes up at 1000 -- the baseline, not a failure
    got = ns.set_mode("enp0s31f6", nic_speed.Mode(2500, "full", "2500baseT/Full"))
    assert got["readback_ok"] and got["speed"] == 1000
    back = ns.restore("enp0s31f6")
    assert back["ok"] and card.adv == ALL.split()
    assert ns.restore("enp0s31f6")["why"] == "nothing was changed"


def test_a_restart_mid_test_still_puts_the_card_back(tmp_path: Path) -> None:
    card = FakeCard()
    nic_speed.NicSpeed(tmp_path / "c.json", card, sleep=lambda s: None).set_mode(
        "enp0s31f6", nic_speed.Mode(10, "full", "10baseT/Full")
    )
    assert card.adv == ["10baseT/Full"]
    again = nic_speed.NicSpeed(tmp_path / "c.json", card, sleep=lambda s: None)  # new node
    assert again.restore_all()["enp0s31f6"]["ok"] and card.adv == ALL.split()


def test_a_link_that_never_comes_up_says_so(tmp_path: Path) -> None:
    def down(argv: list[str]) -> tuple[int, str]:
        if argv[:2] == ["ethtool", "-s"]:
            return 0, ""
        return 0, ETHTOOL.format(adv="10baseT/Full", speed="Unknown!", duplex="Unknown!",
                                 link="no")  # fmt: skip

    t = [0.0]
    ns = nic_speed.NicSpeed(tmp_path / "c.json", down, sleep=lambda s: t.__setitem__(0, t[0] + s),
                            clock=lambda: t[0])  # fmt: skip
    got = ns.set_mode("eth0", nic_speed.Mode(10, "full", "10baseT/Full"))
    assert got["readback_ok"] and not got["link"] and "did not come up" in got["why"]


class StuckAt100(FakeCard):
    """2026-10-17 ProBook: after the speed cases the link came back at 100 Mbps with all
    modes advertised; it went to 1 Gbps again only when renegotiated (or re-plugged)."""

    def __init__(self, fixed_by: str) -> None:
        super().__init__()
        self.stuck, self.fixed_by = False, fixed_by

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        if argv[:2] == ["ethtool", "-s"] and "advertise" in argv and len(argv) > 6:
            self.stuck = int(argv[argv.index("advertise") + 1], 16).bit_count() > 1
        if " ".join(argv[:3]) == self.fixed_by:
            self.stuck = False
        out = super().__call__(argv)
        if self.stuck and argv[0] == "ethtool" and len(argv) == 2:
            return 0, ETHTOOL.format(adv=" ".join(self.adv), speed="100Mb/s", duplex="Full",
                                     link="yes")  # fmt: skip
        return out


def test_back_means_back_at_the_speed_it_had(tmp_path: Path) -> None:
    for fix in ("ethtool -r enp37s0", "ip link set"):
        card = StuckAt100(fix)
        ns = nic_speed.NicSpeed(tmp_path / f"{fix[:2]}.json", card, sleep=lambda s: None)
        ns.set_mode("enp37s0", nic_speed.Mode(10, "full", "10baseT/Full"))
        back = ns.restore("enp37s0")
        assert back["ok"] and back["speed"] == 1000, (fix, back)


def test_a_link_that_stays_slower_is_not_back(tmp_path: Path) -> None:
    card = StuckAt100("never")
    ns = nic_speed.NicSpeed(tmp_path / "c.json", card, sleep=lambda s: None)
    ns.set_mode("enp37s0", nic_speed.Mode(10, "full", "10baseT/Full"))
    back = ns.restore("enp37s0")
    assert not back["ok"] and "came back at 100 Mbps; it was 1000 Mbps" in back["why"]
    assert "enp37s0" in (tmp_path / "c.json").read_text()  # kept: tried again at the next start


def test_what_the_other_end_offers_is_read() -> None:
    """2026-10-17: every case at ~9.4 Mbps -- the link partner's modes say which end."""
    text = ETHTOOL.format(adv=ALL, speed="100Mb/s", duplex="Full", link="yes").replace(
        "    Advertised pause frame use: No\n",
        "    Advertised pause frame use: No\n    Link partner advertised link modes:  10baseT/Half"
        " 10baseT/Full\n                                         100baseT/Full\n",
    )
    assert nic_speed.parse("enp37s0", text).partner == [
        "10baseT/Half", "10baseT/Full", "100baseT/Full"
    ]  # fmt: skip


def test_a_speed_the_other_end_does_not_offer_is_not_tried(tmp_path: Path) -> None:
    """2026-10-17: the router port offers no 1000baseT/Half; trying it took the link away
    for 20 s each run. The card is not touched and the case says why."""
    card = FakeCard()
    partner = "    Link partner advertised link modes:  10baseT/Half 10baseT/Full\n"
    real = card.__call__

    def with_partner(argv: list[str]) -> tuple[int, str]:
        rc, out = real(argv)
        if argv[0] == "ethtool" and len(argv) == 2:
            out = out.replace("    Advertised pause frame use: No\n",
                              "    Advertised pause frame use: No\n" + partner)  # fmt: skip
        return rc, out

    ns = nic_speed.NicSpeed(tmp_path / "c.json", with_partner, sleep=lambda s: None)
    got = ns.set_mode("enp37s0", nic_speed.Mode(100, "half", "100baseT/Half"))
    assert got["skipped"] and not got["link"] and "does not offer 100 Mbps half" in got["why"]
    assert not [c for c in card.calls if c[:2] == ["ethtool", "-s"]]  # never touched
    assert ns.restore("enp37s0")["ok"]  # what it had, as it was
    assert ns.set_mode("enp37s0", nic_speed.Mode(10, "full", "10baseT/Full"))["link"]


def test_gigabit_and_up_is_one_mode() -> None:
    """2026-10-17 user: "1000Mbps부터는 full, half없다며 ... default하나 ... 그게 spec이야"."""
    text = ETHTOOL.format(adv=ALL, speed="1000Mb/s", duplex="Full", link="yes").replace(
        "1000baseT/Full", "1000baseT/Half 1000baseT/Full", 1
    )
    labels = [m.label for m in nic_speed.parse("enp37s0", text).supported]
    assert "1000 Mbps" in labels and not [x for x in labels if x.startswith("1000 Mbps ")]


def test_what_the_other_end_offers_is_read_before_any_change(tmp_path: Path) -> None:
    """2026-10-17 ("controller마지막 1000Mbps는 왜 값이 없어? ... 말이 안되잖아"): right
    after the 100 Mbps half case the card read the router port as not offering 1000 Mbps
    (a card that does not offer gigabit itself does not learn the other end's), and the
    1000 Mbps case was skipped. The reading from before the first change counts."""
    card = FakeCard()
    gig = "    Link partner advertised link modes:  100baseT/Half 100baseT/Full 1000baseT/Full\n"
    slow = "    Link partner advertised link modes:  100baseT/Half 100baseT/Full\n"
    real = card.__call__

    def reads(argv: list[str]) -> tuple[int, str]:
        rc, out = real(argv)
        if argv[0] == "ethtool" and len(argv) == 2:
            lp = gig if "1000baseT/Full" in card.adv else slow  # as the PHY reports it
            out = out.replace("    Advertised pause frame use: No\n",
                              "    Advertised pause frame use: No\n" + lp)  # fmt: skip
        return rc, out

    ns = nic_speed.NicSpeed(tmp_path / "c.json", reads, sleep=lambda s: None)
    assert ns.set_mode("enp37s0", nic_speed.Mode(100, "half", "100baseT/Half"))["link"]
    got = ns.set_mode("enp37s0", nic_speed.Mode(1000, "full", "1000baseT/Full"))
    assert not got.get("skipped") and got["link"] and got["speed"] == 1000
