"""Card and host counters for the 1-second line (02 7, GAP-148): not read is not 0."""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from core.blocks.host_counters import Deltas, nic_counters, udp_counters

SNMP = """Ip: Forwarding DefaultTTL
Ip: 2 64
Udp: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors
Udp: 27267446 363763 19464 3402322 19464 1 0
UdpLite: InDatagrams NoPorts InErrors OutDatagrams RcvbufErrors SndbufErrors InCsumErrors
UdpLite: 0 0 0 0 0 0 0
"""


def card(root: Path, nic: str, wireless: bool, **values: str) -> None:
    stats = root / nic / "statistics"
    stats.mkdir(parents=True)
    if wireless:
        (root / nic / "phy80211").mkdir()
    for name, value in values.items():
        (stats / name).write_text(value)


def test_a_wired_card_counts_multicast_a_wifi_card_does_not(tmp_path: Path) -> None:
    card(tmp_path, "eth0", False, rx_packets="10\n", multicast="7\n", rx_dropped="0\n",
         rx_errors="0\n")  # fmt: skip
    card(tmp_path, "wlan0", True, rx_packets="10\n", multicast="0\n", rx_dropped="2\n",
         rx_errors="0\n")  # fmt: skip
    assert nic_counters("eth0", tmp_path) == {
        "rx_packets": 10, "multicast": 7, "rx_dropped": 0, "rx_errors": 0}  # fmt: skip
    # mac80211 and rtl8814au leave multicast at 0: that 0 is not a measurement
    assert nic_counters("wlan0", tmp_path) == {"rx_packets": 10, "rx_dropped": 2, "rx_errors": 0}


def test_what_cannot_be_read_is_left_out(tmp_path: Path) -> None:
    card(tmp_path, "eth0", False, rx_packets="12\n", rx_dropped="n/a\n")
    assert nic_counters("eth0", tmp_path) == {"rx_packets": 12}
    assert nic_counters("gone0", tmp_path) == {}
    assert udp_counters(tmp_path / "no-snmp") == {}


def test_host_udp_counters_from_snmp(tmp_path: Path) -> None:
    (tmp_path / "snmp").write_text(SNMP)
    assert udp_counters(tmp_path / "snmp") == {
        "InErrors": 19464, "RcvbufErrors": 19464, "NoPorts": 363763}  # fmt: skip
    (tmp_path / "old").write_text("Udp: InDatagrams NoPorts\nUdp: 5 1\n")
    assert udp_counters(tmp_path / "old") == {"NoPorts": 1}  # an older kernel: fewer fields


def test_deltas_first_nothing_then_growth_a_real_zero_stays() -> None:
    values = iter([{"a": 5, "b": 1}, {"a": 9, "b": 1}, {"a": 9}, {"a": 3, "b": 4}, {"a": 4}])
    d = Deltas(lambda: next(values))
    assert d.take() == {}  # nothing to compare with yet
    assert d.take() == {"a": 4, "b": 0}  # measured 0 is written as 0
    assert d.take() == {"a": 0}  # b could not be read: absent, not 0
    assert d.take() == {}  # a went down (reset), b has no reading before: both absent
    assert d.take() == {"a": 1}


@given(st.lists(st.dictionaries(st.sampled_from("abc"), st.integers(0, 10**12)), max_size=30))
def test_deltas_are_never_negative_and_only_of_read_counters(
    readings: list[dict[str, int]],
) -> None:
    it = iter(readings)
    d = Deltas(lambda: next(it))
    before: dict[str, int] | None = None
    for now in readings:
        got = d.take()
        assert all(v >= 0 for v in got.values())
        assert set(got) <= set(now) & set(before or {})
        before = now
