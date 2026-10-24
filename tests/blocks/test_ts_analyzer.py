"""TR 101 290 Priority 1 (docs/10-verification.md 1.1 TS rows, 05-standards 2.1-2)."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from core.blocks import rtp_header
from core.blocks.ts_analyzer import ISO_639_TAG, PID_TIMEOUT_S, TsAnalyzer, crc32_mpeg2
from core.blocks.ts_synth import (
    AUDIO_PID,
    MS,
    PMT_PID,
    VIDEO_PID,
    Stream,
    es,
    packet,
    pat,
    pat_body,
    pmt,
    pmt_body,
    raw_section,
    split_section,
)
from core.blocks.ts_synth import (
    crc32_mpeg2 as reference_crc,
)


def run(ticks: list[list[bytes]], pid_timeout_s: float = 5.0, step_ms: int = 100) -> TsAnalyzer:
    """Feed one tick of packets every ``step_ms`` (7 packets per datagram)."""
    a = TsAnalyzer(pid_timeout_s)
    for i, pkts in enumerate(ticks):
        data = b"".join(pkts)
        now = (i + 1) * step_ms * MS
        for off in range(0, len(data), 7 * 188):
            a.feed(data[off : off + 7 * 188], now)
        a.check_timeouts(now)
    return a


def clean(n: int, **kw: object) -> list[list[bytes]]:
    s = Stream(**kw)  # type: ignore[arg-type]
    return [s.tick() for _ in range(n)]


def test_clean_stream_has_no_errors() -> None:
    a = run(clean(100))
    assert a.counters.p1_errors == 0
    assert a.counters.packets == 100 * 43
    assert a.pat_pmt_seen


def test_mid_stream_join_first_cc_and_late_pat_are_not_errors() -> None:
    """Join mid-stream: first CC 9, PAT and PMT arrive only 2 s after the first packet."""
    s = Stream(start_cc=9)
    ticks = [s.tick(with_pat=False, with_pmt=False) for _ in range(20)]
    ticks += [s.tick() for _ in range(30)]
    a = run(ticks)
    assert a.counters.p1_errors == 0
    assert a.first_pat_ns == 21 * 100 * MS


def test_pat_missing_for_two_seconds() -> None:
    s = Stream()
    ticks = [s.tick() for _ in range(10)]
    ticks += [s.tick(with_pat=False) for _ in range(20)]
    ticks += [s.tick() for _ in range(10)]
    a = run(ticks)
    # 2.1 s without PAT: an error every 0.5 s after the last one
    assert a.counters.pat_err == 4
    assert a.counters.pmt_err == 0


def test_wrong_table_id_and_scrambled_pat_pmt() -> None:
    s = Stream()
    ticks = [s.tick() for _ in range(5)]
    ticks.append([pat(s.next(0), table_id=0x42), pat(s.next(0), scrambled=2)])
    ticks.append([pmt(s.next(0x1000), table_id=0x03)])
    ticks += [s.tick() for _ in range(3)]
    c = run(ticks).counters
    # PID 0: another table and a scrambled packet are errors (1.3.a). On the PMT PID
    # another table is not itself an error (1.5.a counts only a missing PMT).
    assert (c.pat_err, c.pmt_err) == (2, 0)


def test_pmt_missing() -> None:
    s = Stream()
    ticks = [s.tick() for _ in range(5)] + [s.tick(with_pmt=False) for _ in range(12)]
    assert run(ticks).counters.pmt_err == 2


def test_listed_pid_missing() -> None:
    s = Stream()
    ticks = [s.tick() for _ in range(5)] + [s.tick(with_audio=False) for _ in range(40)]
    c = run(ticks, pid_timeout_s=1.0).counters
    assert c.pid_err == 3  # 4 s of silence, one error per second
    assert c.cc_err == 0


def test_pid_dropped_from_pmt_is_no_longer_expected() -> None:
    s = Stream()
    ticks = [s.tick() for _ in range(5)]
    for _ in range(40):
        t = s.tick(with_pmt=False, with_audio=False)
        t.insert(1, pmt(s.next(0x1000), streams=(VIDEO_PID,)))
        ticks.append(t)
    assert run(ticks, pid_timeout_s=1.0).counters.pid_err == 0


def test_cc_loss_order_duplicates_and_discontinuity() -> None:
    v = [packet(VIDEO_PID, cc, b"v") for cc in range(16)]
    a = TsAnalyzer(5.0)

    def feed(*pkts: bytes) -> None:
        a.feed(b"".join(pkts), 1)

    feed(*[v[i] for i in (0, 1, 2)])  # clean
    feed(v[4])  # 3 lost -> 1 error
    feed(v[4])  # one duplicate is allowed
    feed(v[4])  # packet occurring more than twice -> error
    feed(v[6], v[5])  # out of order -> 2 errors
    assert a.counters.cc_err == 4
    feed(packet(VIDEO_PID, 11, b"v", afc=3, discontinuity=True))  # flagged jump
    feed(packet(VIDEO_PID, 11, afc=2))  # adaptation only: counter does not move
    feed(v[12])
    assert a.counters.cc_err == 4
    feed(packet(0x1FFF, 3), packet(0x1FFF, 9))  # null packets are ignored
    assert a.counters.cc_err == 4


def test_sync_byte_and_sync_loss() -> None:
    s = Stream()
    good = s.tick()
    a = TsAnalyzer(5.0)
    a.feed(b"".join(good[:7]), MS)
    a.feed(packet(VIDEO_PID, 0, sync=0x48), 2 * MS)  # one bad: byte error, still in sync
    a.feed(good[7], 2 * MS)
    assert (a.counters.sync_byte_err, a.counters.sync_loss) == (1, 0)
    bad = packet(VIDEO_PID, 0, sync=0x00)
    a.feed(bad + bad, 3 * MS)  # two bad in a row: sync lost
    assert (a.counters.sync_byte_err, a.counters.sync_loss) == (3, 1)
    a.feed(bad * 3, 4 * MS)  # out of sync: not counted again until re-acquired
    assert a.counters.sync_byte_err == 3


def test_garbage_before_first_sync_is_not_counted() -> None:
    a = TsAnalyzer(5.0)
    a.feed(bytes(188 * 3), MS)
    assert a.counters.p1_errors == 0


def test_rtp_header_parse() -> None:
    hdr = bytes([0x80, 33, 0x12, 0x34]) + (1000).to_bytes(4, "big") + (0xABCD).to_bytes(4, "big")
    h = rtp_header.parse(hdr + b"\x47" * 188)
    assert h is not None
    assert (h.seq, h.timestamp, h.ssrc, h.payload_type, h.payload_offset) == (
        0x1234,
        1000,
        0xABCD,
        33,
        12,
    )
    # 1 CSRC + header extension of 1 word
    fixed = bytes([0x91, 33, 0, 1]) + bytes(8)
    ext = fixed + bytes(4) + b"\xbe\xde\x00\x01" + bytes(4)
    h2 = rtp_header.parse(ext + b"\x47")
    assert h2 is not None and h2.payload_offset == 12 + 4 + 4 + 4
    padded = hdr[:1].replace(b"\x80", b"\xa0") + hdr[1:] + b"\x47" * 10 + b"\x00\x00\x03"
    h3 = rtp_header.parse(padded)
    assert h3 is not None and h3.payload_end == len(padded) - 3
    assert rtp_header.parse(b"\x47" * 188) is None  # plain TS over UDP
    assert rtp_header.parse(b"\x80") is None


@settings(max_examples=200, deadline=None)
@given(drops=st.lists(st.tuples(st.integers(0, 1500), st.integers(1, 40)), max_size=8))
def test_random_video_drops_counted_as_cc_errors(drops: list[tuple[int, int]]) -> None:
    """Invariant: each run of lost video packets is one CC error, except runs of a
    multiple of 16 which CC cannot see (the documented limit of CC-based counting)."""
    s = Stream()
    ticks = [s.tick() for _ in range(40)]
    lost: set[int] = set()
    for start, length in drops:
        lost |= set(range(start, start + length))
    idx = 0
    out = []
    for t in ticks:
        keep = []
        for p in t:
            if (p[1] & 0x1F) << 8 | p[2] == VIDEO_PID:
                if idx in lost:
                    idx += 1
                    continue
                idx += 1
            keep.append(p)
        out.append(keep)
    total_video = idx
    runs, run_len = [], 0
    for i in range(1, total_video):  # the very first packet is the reference
        if i in lost:
            run_len += 1
        elif run_len:
            runs.append(run_len)
            run_len = 0
    first_kept = min(set(range(total_video)) - lost, default=None)
    if first_kept is not None:
        # losses before the first received packet are a mid-stream join, not errors
        runs = []
        run_len = 0
        for i in range(first_kept + 1, total_video):
            if i in lost:
                run_len += 1
            elif run_len:
                runs.append(run_len)
                run_len = 0
    c = run(out).counters
    assert c.cc_err == sum(1 for r in runs if r % 16)
    assert c.pat_err == c.pmt_err == c.sync_byte_err == 0
    assert AUDIO_PID  # audio kept flowing: no PID error
    assert c.pid_err == 0


def test_pid_limit_is_5_s_for_video_and_audio_only() -> None:
    """TR 101 290 1.6: 5 s for video/audio; subtitles, data and ISO 639 audio_type > 0
    audio are excluded. They are listed in the PMT here but never sent."""
    extra = (
        es(0x06, 0x200)  # private PES: subtitles or data
        + es(0x05, 0x201)  # private sections (data)
        + es(0x0F, 0x202, bytes([ISO_639_TAG, 4]) + b"kor" + bytes([3]))  # commentary audio
    )
    s = Stream()
    ticks: list[list[bytes]] = []
    for _ in range(100):  # 10 s, audio stops after 1 s
        t = s.tick(with_pmt=False, with_audio=len(ticks) < 10)
        t.insert(1, pmt(s.next(0x1000), extra=extra))
        ticks.append(t)
    a = run(ticks, pid_timeout_s=PID_TIMEOUT_S)
    assert PID_TIMEOUT_S == 5.0 and TsAnalyzer()._pid_timeout_ns == 5 * 10**9
    assert a.counters.pid_err == 1  # only the AAC audio: silent 1.0 s -> 10.0 s


# ---------------------------------------------------------------- V1.4.1 (2026-09-25)


def test_crc32_mpeg2_check_value() -> None:
    """CRC-32/MPEG-2 check value; the analyzer's table and the test's bit-by-bit
    reference agree (ISO/IEC 13818-1 annex A)."""
    assert crc32_mpeg2(b"123456789") == reference_crc(b"123456789") == 0x0376E6E7
    sec = raw_section(0x00, pat_body())
    assert crc32_mpeg2(sec) == 0  # a whole section including its CRC gives 0


def test_bad_crc_pat_is_counted_and_is_no_pat() -> None:
    """2.2 CRC_error counted apart (not priority 1); a PAT with a bad CRC is not a PAT,
    so 1.3.a fires after 0.5 s."""
    s = Stream()
    ticks = [s.tick() for _ in range(5)]
    for _ in range(10):  # 1 s of PATs that all fail CRC
        t = s.tick(with_pat=False)
        ticks.append([pat(s.next(0), bad_crc=True), *t])
    ticks += [s.tick() for _ in range(3)]
    c = run(ticks).counters
    assert c.crc_err == 10
    assert c.pat_err >= 1
    assert c.p1_errors == c.pat_err  # CRC is not in the priority 1 sum


def test_pid0_packets_without_a_pat_section_are_a_pat_error() -> None:
    """1.3.a: the timer runs on PAT sections, not on PID 0 packets."""
    s = Stream()
    ticks = [s.tick() for _ in range(5)]
    for _ in range(10):  # PID 0 keeps coming, but only continuation bytes
        t = s.tick(with_pat=False)
        ticks.append([packet(0, s.next(0), b"\x12\x34"), *t])
    ticks += [s.tick() for _ in range(3)]
    assert run(ticks).counters.pat_err >= 1


def test_sections_over_two_packets_are_read() -> None:
    """A PAT and a PMT each split over two packets: no errors, the listed PIDs are
    known (PID 5 s limit works for them)."""
    s = Stream()
    ticks = []
    for _ in range(20):
        t = s.tick(with_pat=False, with_pmt=False)
        cc0, ccp = s.next(0), s.next(PMT_PID)
        s.next(0), s.next(PMT_PID)  # the second packet of each uses the next CC
        ticks.append(
            split_section(0, cc0, raw_section(0x00, pat_body()), 5)
            + split_section(PMT_PID, ccp, raw_section(0x02, pmt_body()), 9)
            + t
        )
    a = run(ticks)
    c = a.counters
    assert (c.pat_err, c.pmt_err, c.crc_err, c.pid_err, c.cc_err) == (0, 0, 0, 0, 0)
    assert a.pat_pmt_seen


def test_every_program_map_pid_is_checked() -> None:
    """1.5.a: each program_map_PID listed in the PAT needs its PMT every 0.5 s."""
    second = 0x1001
    s = Stream()
    ticks = []
    for i in range(20):
        t = s.tick(with_pat=False)
        extra = [pat(s.next(0), pmt_pids=(PMT_PID, second))]
        if i < 5:  # the second program's PMT stops after 0.5 s
            extra.append(pmt(s.next(second), pid=second))
        ticks.append(extra + t)
    c = run(ticks).counters
    assert c.pmt_err >= 2


def test_tei_is_counted_apart() -> None:
    """2.1 Transport_error: counted, not in the priority 1 sum."""
    s = Stream()
    ticks = [s.tick() for _ in range(5)]
    ticks.append([pat(s.next(0), tei=True), *s.tick(with_pat=False)])
    ticks += [s.tick() for _ in range(3)]
    c = run(ticks).counters
    assert c.tei_err == 1 and c.p1_errors == 0


def test_duplicate_may_carry_a_new_pcr() -> None:
    """13818-1 2.4.3.3: a duplicate repeats every byte except that the PCR fields, if
    present, carry a valid (new) value. That is still a duplicate, not a CC error."""

    def with_pcr(cc: int, pcr: int) -> bytes:
        head = bytes([0x47, VIDEO_PID >> 8, VIDEO_PID & 0xFF, 0x30 | cc])  # AF + payload
        af = bytes([7, 0x10]) + pcr.to_bytes(6, "big")  # PCR_flag set
        return (head + af + b"video").ljust(188, b"\xff")

    s = Stream()
    ticks = [s.tick() for _ in range(5)]
    cc = s.next(VIDEO_PID)
    ticks.append([with_pcr(cc, 1000), with_pcr(cc, 2000), *s.tick()])  # original + dup
    ticks += [s.tick() for _ in range(3)]
    assert run(ticks).counters.cc_err == 0
    # other bytes differ -> not a duplicate: same CC with other content is loss
    s = Stream()
    ticks = [s.tick() for _ in range(5)]
    cc = s.next(VIDEO_PID)
    other = bytearray(with_pcr(cc, 1000))
    other[20] ^= 1
    ticks.append([with_pcr(cc, 1000), bytes(other), *s.tick()])
    ticks += [s.tick() for _ in range(3)]
    assert run(ticks).counters.cc_err == 1
