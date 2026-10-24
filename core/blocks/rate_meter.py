"""Packets and bitrate per 1-second record (docs/12-architecture.md 1-2)."""

from __future__ import annotations


class RateMeter:
    def __init__(self) -> None:
        self._packets = 0
        self._bytes = 0
        self.total_packets = 0
        self.total_bytes = 0

    def on_packet(self, size_bytes: int) -> None:
        self._packets += 1
        self._bytes += size_bytes
        self.total_packets += 1
        self.total_bytes += size_bytes

    def take_second(self, period_s: float = 1.0) -> tuple[int, int]:
        """Return (packets, bits per second) for the period that just ended, then reset."""
        packets, bits = self._packets, self._bytes * 8
        self._packets = self._bytes = 0
        return packets, round(bits / period_s) if period_s > 0 else 0
