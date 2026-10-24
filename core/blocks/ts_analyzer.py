"""MPEG-TS checks, ETSI TR 101 290 V1.4.1 (docs/05-standards.md 1.4, 2.1-2).

Priority 1:
1.1   TS_sync_loss  2 bad sync bytes in a row while in sync (sync = 5 good in a row)
1.2   Sync_byte     a packet whose first byte is not 0x47 (after first sync)
1.3.a PAT_error_2   no valid table_id 0x00 section on PID 0 for more than 0.5 s, a section
                    with another table_id on PID 0, or a scrambled PID 0 packet
1.4   CC            wrong continuity counter: lost, out of order, or repeated more than once
                    (a repeat is the same packet, only its PCR may differ, 13818-1 2.4.3.3;
                    same CC with other content is loss)
1.5.a PMT_error_2   no valid table_id 0x02 section on each program_map_PID of the PAT for
                    more than 0.5 s, or a scrambled packet on it
1.6   PID           a video or audio PID listed in the PMT missing for more than 5 s
                   (TR 101 290 upper limit; subtitle, data and ISO 639 audio_type > 0
                   audio are excluded, as the TR says)

Boundary rules (2.1-2): nothing is checked before the first sync; the first packet of a
PID is its CC reference; PAT/PMT/PID time limits start at the first PAT, first PMT and
first listing. A late first PAT after joining is warm-up, not an error.
Time limits are checked before each datagram's packets, and an overdue table counts one
error per full limit (2.1 s without PAT = 4 errors), whatever the check rate.
``feed`` keeps a view of the last packet per PID: pass immutable bytes, not a reused buffer.
Priority 2, counted only (not in the priority 1 sum, decided 2026-09-25): 2.1 TEI and
2.2 CRC of PAT/PMT (a section failing CRC-32/MPEG-2 is not used). PCR/PTS come later.
Sections may span packets; they are collected per PID from the PUSI pointer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

TS_SIZE = 188
SYNC = 0x47
NULL_PID = 0x1FFF
PAT_PID = 0
SECTION_TIMEOUT_NS = 500_000_000
SYNC_ACQUIRE = 5
SYNC_LOSE = 2
PID_TIMEOUT_S = 5.0
# ISO/IEC 13818-1 stream_type values that are always video or audio. 0x06 (private PES)
# may be subtitles or data, so it is not checked.
VIDEO_AUDIO_TYPES = frozenset(
    {0x01, 0x02, 0x10, 0x1B, 0x24, 0x33, 0x42, 0x03, 0x04, 0x0F, 0x11, 0x1C, 0x81, 0x87}
)
ISO_639_TAG = 0x0A


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else c << 1
        table.append(c & 0xFFFFFFFF)
    return table


_CRC = _crc_table()


def crc32_mpeg2(data: bytes | memoryview) -> int:
    """CRC-32/MPEG-2 (ISO/IEC 13818-1 annex A). A whole section with its CRC gives 0."""
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ b) & 0xFF]
    return crc


@dataclass
class TsCounters:
    packets: int = 0
    sync_loss: int = 0
    sync_byte_err: int = 0
    pat_err: int = 0
    cc_err: int = 0
    pmt_err: int = 0
    pid_err: int = 0
    tei_err: int = 0  # priority 2.1, counted only
    crc_err: int = 0  # priority 2.2 (PAT/PMT), counted only

    @property
    def p1_errors(self) -> int:
        return (
            self.sync_loss
            + self.sync_byte_err
            + self.pat_err
            + self.cc_err
            + self.pmt_err
            + self.pid_err
        )


@dataclass
class _Pid:
    cc: int
    last: memoryview
    repeats: int = 0


@dataclass
class _Collect:
    """A PSI section being collected over packets of one PID."""

    buf: bytearray = field(default_factory=bytearray)
    active: bool = False


@dataclass
class _Program:
    number: int = 0  # program_number from the PAT
    last_ns: int | None = None  # last PMT section
    streams: set[int] = field(default_factory=set)


def _overdue(last_ns: int, now_ns: int, limit_ns: int) -> tuple[int, int]:
    """Errors for a table or PID not seen since ``last_ns``; returns (count, new last)."""
    if now_ns - last_ns <= limit_ns:
        return 0, last_ns
    n = (now_ns - last_ns - 1) // limit_ns
    return n, last_ns + n * limit_ns


def _same_but_pcr(a: memoryview, b: memoryview) -> bool:
    """13818-1 2.4.3.3: a duplicate repeats every byte, except that its PCR fields may
    carry a new valid value (adaptation field with PCR_flag: bytes 6..11)."""
    if a == b:
        return True
    has_pcr = (a[3] & 0x20) and a[4] >= 7 and a[5] & 0x10
    return bool(has_pcr) and a[:6] == b[:6] and a[12:] == b[12:]


def _special_audio(descriptors: memoryview) -> bool:
    """True when an ISO 639 descriptor says audio_type > 0 (e.g. commentary)."""
    j = 0
    while j + 2 <= len(descriptors):
        tag, length = descriptors[j], descriptors[j + 1]
        if tag == ISO_639_TAG:
            for k in range(j + 2 + 3, j + 2 + length, 4):  # 3-byte language, 1-byte type
                if k < len(descriptors) and descriptors[k] > 0:
                    return True
        j += 2 + length
    return False


class TsAnalyzer:
    def __init__(self, pid_timeout_s: float = PID_TIMEOUT_S) -> None:
        self.counters = TsCounters()
        self.first_pat_ns: int | None = None
        self.first_pmt_ns: int | None = None
        self._pid_timeout_ns = int(pid_timeout_s * 1e9)
        self._in_sync = False
        self._good_run = 0
        self._bad_run = 0
        self._pids: dict[int, _Pid] = {}
        self._last_pat_ns: int | None = None
        self._programs: dict[int, _Program] = {}  # PMT PID -> program
        self._stream_seen: dict[int, int] = {}  # listed PID -> last seen (or listed) time
        self._collect: dict[int, _Collect] = {}  # PSI PID -> section in progress

    @property
    def pat_pmt_seen(self) -> bool:
        """For the warm-up gate: first PAT and first PMT have both arrived."""
        return self.first_pat_ns is not None and self.first_pmt_ns is not None

    def programs(self) -> dict[int, tuple[int, list[int]]]:
        """PMT PID -> (program_number, video/audio PIDs listed) as seen so far."""
        return {pid: (p.number, sorted(p.streams)) for pid, p in self._programs.items()}

    def feed(self, payload: bytes | memoryview, arrival_ns: int) -> None:
        """One datagram's TS payload (normally 7 x 188 bytes)."""
        self.check_timeouts(arrival_ns)
        view = memoryview(payload)
        n = len(view)
        for off in range(0, n - n % TS_SIZE, TS_SIZE):
            self._packet(view[off : off + TS_SIZE], arrival_ns)
        if n % TS_SIZE:
            self._bad_sync()  # a cut packet cannot be trusted

    def resync(self, now_ns: int) -> None:
        """The stream was gone (not a table missing in a running stream): the timers
        start again from now (2026-10-17: 'P1 4 every second' while nothing came)."""
        if self._last_pat_ns is not None:
            self._last_pat_ns = now_ns
        for prog in self._programs.values():
            if prog.last_ns is not None:
                prog.last_ns = now_ns
        for pid in self._stream_seen:
            self._stream_seen[pid] = now_ns

    def check_timeouts(self, now_ns: int) -> None:
        """Also call once per second so a stream that stopped is still checked."""
        c = self.counters
        if self._last_pat_ns is not None:
            n, self._last_pat_ns = _overdue(self._last_pat_ns, now_ns, SECTION_TIMEOUT_NS)
            c.pat_err += n
        for prog in self._programs.values():
            if prog.last_ns is not None:
                n, prog.last_ns = _overdue(prog.last_ns, now_ns, SECTION_TIMEOUT_NS)
                c.pmt_err += n
        for pid, seen in self._stream_seen.items():
            n, self._stream_seen[pid] = _overdue(seen, now_ns, self._pid_timeout_ns)
            c.pid_err += n

    # ---------------------------------------------------------------- packets

    def _packet(self, pkt: memoryview, now: int) -> None:
        if pkt[0] != SYNC:
            self._bad_sync()
            return
        self._bad_run = 0
        if not self._in_sync:
            self._good_run += 1
            if self._good_run >= SYNC_ACQUIRE:
                self._in_sync = True
        c = self.counters
        c.packets += 1
        pid = ((pkt[1] & 0x1F) << 8) | pkt[2]
        if pid == NULL_PID:
            return
        if pkt[1] & 0x80:
            c.tei_err += 1  # 2.1: the packet is still used (TR 101 290 counts only)
        pusi = pkt[1] & 0x40
        scrambled = pkt[3] >> 6
        afc = (pkt[3] >> 4) & 0x3
        cc = pkt[3] & 0x0F
        start = 4
        discontinuity = False
        if afc & 0x2:
            af_len = pkt[4]
            discontinuity = af_len > 0 and bool(pkt[5] & 0x80)
            start = 5 + af_len
        self._check_cc(pid, cc, pkt, has_payload=bool(afc & 0x1), discontinuity=discontinuity)
        if pid in self._stream_seen:
            self._stream_seen[pid] = now
        if pid == PAT_PID or pid in self._programs:
            if scrambled:
                if pid == PAT_PID:
                    c.pat_err += 1
                else:
                    c.pmt_err += 1
            elif afc & 0x1 and start < TS_SIZE:
                for sec in self._sections(pid, pkt[start:], bool(pusi)):
                    self._on_section(pid, sec, now)

    def _bad_sync(self) -> None:
        self._good_run = 0
        if not self._in_sync:
            return  # before the first sync nothing is judged
        self.counters.sync_byte_err += 1
        self._bad_run += 1
        if self._bad_run >= SYNC_LOSE:
            self.counters.sync_loss += 1
            self._in_sync = False
            self._bad_run = 0

    def _check_cc(
        self, pid: int, cc: int, pkt: memoryview, has_payload: bool, discontinuity: bool
    ) -> None:
        state = self._pids.get(pid)
        if state is None or discontinuity:
            self._pids[pid] = _Pid(cc, pkt)
            return
        if not has_payload:  # counter does not move without payload
            if cc != state.cc:
                self.counters.cc_err += 1
            self._pids[pid] = _Pid(cc, pkt)
            return
        if cc == state.cc and _same_but_pcr(pkt, state.last):
            state.repeats += 1
            if state.repeats > 1:  # one duplicate is allowed
                self.counters.cc_err += 1
            return
        if cc != (state.cc + 1) & 0x0F:
            self.counters.cc_err += 1
        self._pids[pid] = _Pid(cc, pkt)

    # ---------------------------------------------------------------- sections

    def _sections(self, pid: int, payload: memoryview, pusi: bool) -> list[bytes]:
        """Complete sections finished by this packet (13818-1 2.4.4: pointer field,
        sections may span packets and several may follow each other)."""
        col = self._collect.setdefault(pid, _Collect())
        out: list[bytes] = []
        if pusi:
            pointer = payload[0]
            if col.active:  # the bytes before the pointer end the previous section
                col.buf += payload[1 : 1 + pointer]
                out += self._finish(col)
            col.buf, col.active = bytearray(payload[1 + pointer :]), True
        elif col.active:
            col.buf += payload
        else:
            return out  # continuation without a start: nothing to read
        out += self._finish(col)
        return out

    @staticmethod
    def _finish(col: _Collect) -> list[bytes]:
        out = []
        while col.active and len(col.buf) >= 3:
            if col.buf[0] == 0xFF:  # stuffing: no more sections in this packet
                col.buf.clear()
                col.active = False
                break
            need = 3 + (((col.buf[1] & 0x0F) << 8) | col.buf[2])
            if len(col.buf) < need:
                break
            out.append(bytes(col.buf[:need]))
            del col.buf[:need]
        if col.active and not col.buf:
            col.active = False
        return out

    def _on_section(self, pid: int, sec: bytes, now: int) -> None:
        c = self.counters
        if len(sec) < 12 or crc32_mpeg2(sec) != 0:
            c.crc_err += 1  # 2.2; the section is not used
            return
        body = memoryview(sec)[: len(sec) - 4]
        if pid == PAT_PID:
            if sec[0] != 0x00:
                c.pat_err += 1  # 1.3.a: another table on PID 0
                return
            if self._last_pat_ns is None:
                self.first_pat_ns = now
            self._last_pat_ns = now
            self._pat(body)
        elif sec[0] == 0x02:  # 1.5.a counts only a missing PMT, not other tables
            prog = self._programs[pid]
            if prog.last_ns is None and self.first_pmt_ns is None:
                self.first_pmt_ns = now
            prog.last_ns = now
            self._pmt(prog, body, now)

    def _pat(self, sec: memoryview) -> None:
        for i in range(8, len(sec) - 3, 4):
            program = (sec[i] << 8) | sec[i + 1]
            pmt_pid = ((sec[i + 2] & 0x1F) << 8) | sec[i + 3]
            if program != 0 and pmt_pid not in self._programs:
                self._programs[pmt_pid] = _Program(program)

    def _pmt(self, prog: _Program, sec: memoryview, now: int) -> None:
        if len(sec) < 12:
            return
        i = 12 + (((sec[10] & 0x0F) << 8) | sec[11])
        streams = set()
        while i + 5 <= len(sec):
            info_len = ((sec[i + 3] & 0x0F) << 8) | sec[i + 4]
            if sec[i] in VIDEO_AUDIO_TYPES and not _special_audio(sec[i + 5 : i + 5 + info_len]):
                streams.add(((sec[i + 1] & 0x1F) << 8) | sec[i + 2])
            i += 5 + info_len
        for pid in streams - prog.streams:
            self._stream_seen.setdefault(pid, now)
        for pid in prog.streams - streams:
            self._stream_seen.pop(pid, None)
        prog.streams = streams
