"""Synthetic RTP packet streams with an exact number of injected problems (docs/10 1.1).

One job: packets whose right answer is known, for ``zeroloss verify`` (02 2-13) and the
tests. Never used on a real stream.
"""

from __future__ import annotations

from dataclasses import dataclass

NS_PER_S = 1_000_000_000
RTP_CLOCK_HZ = 90_000


@dataclass(frozen=True)
class Pkt:
    seq: int
    ts: int
    ssrc: int
    arrival_ns: int


def stream(
    count: int,
    pps: int = 3300,
    first_seq: int = 0,
    first_ts: int = 0,
    ssrc: int = 0x01ABCDEF,
    start_ns: int = 0,
) -> list[Pkt]:
    """Perfect CBR stream: seq +1, RTP ts and arrival advance in lockstep."""
    out = []
    for i in range(count):
        out.append(
            Pkt(
                seq=(first_seq + i) % 65536,
                ts=(first_ts + i * RTP_CLOCK_HZ // pps) % (1 << 32),
                ssrc=ssrc,
                arrival_ns=start_ns + i * NS_PER_S // pps,
            )
        )
    return out


def drop(pkts: list[Pkt], indexes: set[int]) -> list[Pkt]:
    return [p for i, p in enumerate(pkts) if i not in indexes]


def delay(pkts: list[Pkt], index: int, by_ns: int) -> list[Pkt]:
    """Move one packet later in time and re-sort by arrival (reordering)."""
    moved = [
        Pkt(p.seq, p.ts, p.ssrc, p.arrival_ns + by_ns) if i == index else p
        for i, p in enumerate(pkts)
    ]
    return sorted(moved, key=lambda p: p.arrival_ns)
