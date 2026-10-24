"""RTP fixed header (RFC 3550 5.1): sequence, timestamp, SSRC and where the payload starts."""

from __future__ import annotations

from dataclasses import dataclass

RTP_VERSION = 2
FIXED_LEN = 12


@dataclass(frozen=True)
class RtpHeader:
    seq: int
    timestamp: int
    ssrc: int
    payload_type: int
    marker: bool
    payload_offset: int  # after CSRCs and header extension
    payload_end: int  # before padding


def parse(data: bytes) -> RtpHeader | None:
    """None when the datagram is not a valid RTP packet (counted by the caller)."""
    n = len(data)
    if n < FIXED_LEN or data[0] >> 6 != RTP_VERSION:
        return None
    padding = data[0] & 0x20
    extension = data[0] & 0x10
    offset = FIXED_LEN + 4 * (data[0] & 0x0F)
    if extension:
        if n < offset + 4:
            return None
        offset += 4 + 4 * int.from_bytes(data[offset + 2 : offset + 4], "big")
    end = n
    if padding:
        end -= data[-1]
    if offset > end:
        return None
    return RtpHeader(
        seq=int.from_bytes(data[2:4], "big"),
        timestamp=int.from_bytes(data[4:8], "big"),
        ssrc=int.from_bytes(data[8:12], "big"),
        payload_type=data[1] & 0x7F,
        marker=bool(data[1] & 0x80),
        payload_offset=offset,
        payload_end=end,
    )
