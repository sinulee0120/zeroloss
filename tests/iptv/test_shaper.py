"""SND-026: a traffic shaper on the sender's own card is the sender's, not the router's
(2026-10-31 R20261031-205951: a 3 % netem there was judged red; user: "지금은 라우터 문제가
아닌데"). Read only; made-up card output."""

from __future__ import annotations

import pytest

from modules.iptv import run as rules
from modules.iptv.sender import shaper

ADDRS = "2: enp37s0    inet 10.0.0.1/24 brd 10.0.0.255 scope global enp37s0\n"


@pytest.fixture(autouse=True)
def linux_with_tc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("modules.iptv.sender.shaper.sys.platform", "linux")
    monkeypatch.setattr("modules.iptv.sender.shaper.shutil.which", lambda name: "/usr/sbin/" + name)


def test_the_card_of_the_sending_address() -> None:
    assert shaper.card_of("10.0.0.1", lambda argv: ADDRS) == "enp37s0"
    assert shaper.card_of("10.0.0.9", lambda argv: ADDRS) is None


def test_a_netem_is_seen_a_plain_card_is_not() -> None:
    netem = "qdisc netem 8003: root refcnt 2 limit 1000 loss 3%\n"
    plain = "qdisc fq_codel 0: root refcnt 2 limit 10240p flows 1024\n"
    assert (
        shaper.shaping("enp37s0", lambda argv: netem)
        == "netem 8003: root refcnt 2 limit 1000 loss 3%"
    )
    assert shaper.shaping("enp37s0", lambda argv: plain) is None
    assert shaper.shaping(None, lambda argv: netem) is None


def test_its_seconds_are_a_sender_trouble_window() -> None:
    assert "ZL-SND-026" in rules.SENDER_TROUBLE


def test_a_tablets_frozen_screen_is_found_in_freezedetects_lines() -> None:
    """2026-10-31 user ("같은 화면이 보였다는걸 어떻게 알려주면 좋을까"): the stretches where the
    tablet showed one picture, from ffmpeg's freezedetect (made-up lines in its shape)."""
    from modules.iptv.receiver.evidence import screen_freezes

    text = ("[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 2.97\n"
            "[freezedetect @ 0x1] lavfi.freezedetect.freeze_duration: 2.03\n"
            "[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 5\n"
            "[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 7.1\n")  # fmt: skip
    assert screen_freezes(text, 8.0) == [(2.97, 5.0), (7.1, 8.0)]  # the last one held to the end
    assert screen_freezes("", 8.0) == []
    pieces = " ".join(f"freeze_start: {a} freeze_end: {b}"
                      for a, b in ((1.24, 2.27), (2.27, 4.93), (4.93, 5.97)))  # fmt: skip
    assert screen_freezes(pieces, 8.0) == [(1.24, 5.97)]  # one freeze, in touching pieces
