"""Synthetic MPEG-TS packets: PAT, one PMT, video and audio PIDs (docs/10 1.1).

One job: TS packets whose right answer is known, for ``zeroloss verify`` (02 2-13) and
the tests. Never used on a real stream.
"""

from __future__ import annotations

PMT_PID = 0x1000
VIDEO_PID = 0x100
AUDIO_PID = 0x101


def crc32_mpeg2(data: bytes) -> int:
    """Bit-by-bit reference (ISO/IEC 13818-1 annex A: poly 0x04C11DB7, init all ones,
    no reflection, no final XOR). Written apart from the analyzer's table version."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) if crc & 0x80000000 else crc << 1
            crc &= 0xFFFFFFFF
    return crc


def _crc(data: bytes) -> bytes:
    return crc32_mpeg2(data).to_bytes(4, "big")


def packet(
    pid: int,
    cc: int,
    payload: bytes = b"",
    pusi: bool = False,
    scrambled: int = 0,
    afc: int = 1,
    discontinuity: bool = False,
    sync: int = 0x47,
) -> bytes:
    head = bytes(
        [sync, (0x40 if pusi else 0) | (pid >> 8), pid & 0xFF, scrambled << 6 | afc << 4 | cc]
    )
    body = b""
    if afc & 2:
        af = bytes([0x80 if discontinuity else 0x00])
        body = bytes([len(af)]) + af
    body += payload
    return (head + body).ljust(188, b"\xff")


def raw_section(table_id: int, body: bytes, bad_crc: bool = False) -> bytes:
    length = len(body) + 5 + 4  # 5 bytes after length, 4 CRC
    head = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF, 0, 1, 0xC1, 0, 0])
    crc = crc32_mpeg2(head + body) ^ (1 if bad_crc else 0)
    return head + body + crc.to_bytes(4, "big")


def section(table_id: int, body: bytes, bad_crc: bool = False) -> bytes:
    return b"\x00" + raw_section(table_id, body, bad_crc)  # pointer field 0


def pat_body(pmt_pids: tuple[int, ...] = (PMT_PID,)) -> bytes:
    body = b""
    for n, pid in enumerate(pmt_pids, start=1):
        body += bytes([0, n, 0xE0 | (pid >> 8), pid & 0xFF])
    return body


def pat(
    cc: int,
    table_id: int = 0x00,
    scrambled: int = 0,
    bad_crc: bool = False,
    pmt_pids: tuple[int, ...] = (PMT_PID,),
    tei: bool = False,
) -> bytes:
    pkt = packet(0, cc, section(table_id, pat_body(pmt_pids), bad_crc), pusi=True,
                 scrambled=scrambled)  # fmt: skip
    return bytes([pkt[0], pkt[1] | (0x80 if tei else 0)]) + pkt[2:]


def split_section(pid: int, cc: int, sec: bytes, first: int) -> list[bytes]:
    """One section over two packets: ``first`` bytes in the first (PUSI) packet."""
    # The first packet's payload must end exactly where the section continues, so it is
    # shortened with adaptation-field stuffing (13818-1 2.4.3.5), never with 0xFF bytes.
    payload = b"\x00" + sec[:first]
    af_len = 184 - 1 - len(payload)
    head = bytes([0x47, 0x40 | (pid >> 8), pid & 0xFF, 0x30 | cc])
    a = head + bytes([af_len, 0x00]) + b"\xff" * (af_len - 1) + payload
    b = packet(pid, (cc + 1) & 0x0F, sec[first:])
    assert len(a) == 188
    return [a, b]


def es(stream_type: int, pid: int, info: bytes = b"") -> bytes:
    return bytes([stream_type, 0xE0 | (pid >> 8), pid & 0xFF, 0xF0, len(info)]) + info


def pmt(
    cc: int,
    streams: tuple[int, ...] = (VIDEO_PID, AUDIO_PID),
    table_id: int = 0x02,
    extra: bytes = b"",
    pid: int = PMT_PID,
) -> bytes:
    """Video is HEVC (0x24), audio is AAC (0x0F); ``extra`` adds raw ES entries."""
    return packet(pid, cc, section(table_id, pmt_body(streams, extra)), pusi=True)


def pmt_body(streams: tuple[int, ...] = (VIDEO_PID, AUDIO_PID), extra: bytes = b"") -> bytes:
    body = bytes([0xE0 | (VIDEO_PID >> 8), VIDEO_PID & 0xFF, 0xF0, 0])  # PCR PID, no info
    for pid in streams:
        body += es(0x24 if pid == VIDEO_PID else 0x0F, pid)
    return body + extra


class Stream:
    """A clean stream: every 100 ms one PAT and one PMT, video and audio in between."""

    def __init__(self, video_per_tick: int = 40, start_cc: int = 0) -> None:
        self.cc: dict[int, int] = {}
        self.video_per_tick = video_per_tick
        self.start_cc = start_cc
        self.frames = 0

    def next(self, pid: int) -> int:
        cc = self.cc.get(pid, self.start_cc - 1) + 1 & 0x0F
        self.cc[pid] = cc
        return cc

    def tick(
        self, with_pat: bool = True, with_pmt: bool = True, with_audio: bool = True
    ) -> list[bytes]:
        out = []
        if with_pat:
            out.append(pat(self.next(0)))
        if with_pmt:
            out.append(pmt(self.next(PMT_PID)))
        self.frames += 1
        out += [
            packet(
                VIDEO_PID, self.next(VIDEO_PID), b"v" + (self.frames * 1000 + i).to_bytes(4, "big")
            )
            for i in range(self.video_per_tick)
        ]
        if with_audio:
            out.append(packet(AUDIO_PID, self.next(AUDIO_PID), b"a"))
        return out


MS = 1_000_000
