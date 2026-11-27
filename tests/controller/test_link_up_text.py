"""The link-up line says something useful for a Wi-Fi card (09 NET-015 cleared)."""

from __future__ import annotations

from core.blocks.link_monitor import Change, Link, Wifi
from core.node import link_event


def test_a_wifi_link_up_says_its_channel_not_unknown() -> None:
    """2026-11-27 R20261127-011120: "Link wlp4s0 is up (unknown)"."""
    w = Link("wlp4s0", "192.0.2.13", True, None, None, 3, True, Wifi(channel=153, band="5GHz"))
    assert link_event(Change("wlp4s0", "up", None, w)).text == (
        "Link wlp4s0 is up (Wi-Fi channel 153, 5GHz)"
    )
    eth = Link("eth0", "192.0.2.14", True, 1000, "full", 2)
    assert link_event(Change("eth0", "up", None, eth)).text == "Link eth0 is up (1000 Mbps)"
    bare = Link("eth1", None, True, None, None, None)
    assert link_event(Change("eth1", "up", None, bare)).text == "Link eth1 is up (speed unknown)"
