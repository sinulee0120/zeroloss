"""RTP datagrams carrying 7 TS packets each, at a steady rate (docs/10 1.1).

For the IPTV self checks (``zeroloss verify``, 02 2-13) and the receiver tests.
"""

from __future__ import annotations

from core.blocks.ts_synth import Stream

NS_PER_S = 1_000_000_000


def rtp(seq: int, ts: int, ssrc: int, payload: bytes) -> bytes:
    head = bytes([0x80, 33]) + (seq % 65536).to_bytes(2, "big")
    return head + (ts % (1 << 32)).to_bytes(4, "big") + ssrc.to_bytes(4, "big") + payload


def datagrams(
    seconds: float,
    pps: int = 500,
    first_seq: int = 0,
    ssrc: int = 0x1234,
    pat_after_s: float = 0.0,
    start_cc: int = 0,
    start_ns: int = 0,
) -> list[tuple[int, bytes, int]]:
    """(index, datagram, arrival_ns). PAT/PMT every 100 ms once ``pat_after_s`` passed."""
    s = Stream(video_per_tick=5, start_cc=start_cc)
    pkts: list[bytes] = []
    out = []
    n = int(seconds * pps)
    per_tick = max(1, pps // 10)
    for i in range(n):
        t = i / pps
        if i % per_tick == 0:
            with_tables = t >= pat_after_s
            pkts += s.tick(with_pat=with_tables, with_pmt=with_tables)
        while len(pkts) < 7:
            pkts += s.tick(with_pat=False, with_pmt=False)
        payload, pkts = b"".join(pkts[:7]), pkts[7:]
        arrival = start_ns + i * NS_PER_S // pps
        out.append((i, rtp(first_seq + i, i * 90_000 // pps, ssrc, payload), arrival))
    return out
