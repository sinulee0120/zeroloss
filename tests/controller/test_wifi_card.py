"""Which Wi-Fi card joins the test Wi-Fi (03 F17 "무선 수신 PC", 20 GAP-102)."""

from __future__ import annotations

from controller.run import join_card

INTERNET = {"nic": "wlp4s0", "ssid": "HOMEWIFI_000000_5G", "ip": "192.168.0.13"}
TEST_NET = {"nic": "wlx02005e000001", "ssid": "ZL_TEST_5G", "ip": "192.168.45.190"}
FREE = {"nic": "wlx0011", "ssid": None, "ip": None}


def test_the_card_on_the_test_network_not_the_internet_one() -> None:
    """Real case: the internal card is the PC's internet, a USB card is on the router."""
    assert join_card([INTERNET, TEST_NET], "192.168.45.177") == ("wlx02005e000001", None)


def test_a_free_card_comes_first() -> None:
    assert join_card([INTERNET, TEST_NET, FREE], "192.168.45.177") == ("wlx0011", None)


def test_only_another_network_says_which_one_it_leaves() -> None:
    assert join_card([INTERNET], "192.168.45.177") == ("wlp4s0", "HOMEWIFI_000000_5G")
    assert join_card([INTERNET], "rx-pc.local") == ("wlp4s0", "HOMEWIFI_000000_5G")
