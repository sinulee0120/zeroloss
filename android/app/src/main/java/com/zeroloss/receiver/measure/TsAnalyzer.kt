package com.zeroloss.receiver.measure

/**
 * MPEG-TS checks, ETSI TR 101 290 V1.4.1 (docs/05-standards.md 1.4, 2.1-2), a line-for-line
 * port of core/blocks/ts_analyzer.py so a tablet counts P1 errors exactly as a PC does.
 *
 * Priority 1: 1.1 TS_sync_loss, 1.2 Sync_byte, 1.3.a PAT_error_2, 1.4 CC (one repeat of the
 * same packet allowed, only its PCR may differ), 1.5.a PMT_error_2, 1.6 PID (a listed
 * video/audio PID missing > 5 s). Priority 2 counted only: 2.1 TEI, 2.2 CRC of PAT/PMT.
 * Nothing is judged before the first sync; time limits start at the first PAT/PMT/listing;
 * an overdue table counts one error per full limit.
 */
class TsAnalyzer(pidTimeoutS: Double = PID_TIMEOUT_S) {
    class Counters {
        var packets = 0L
        var syncLoss = 0L
        var syncByteErr = 0L
        var patErr = 0L
        var ccErr = 0L
        var pmtErr = 0L
        var pidErr = 0L
        var teiErr = 0L // 2.1, counted only
        var crcErr = 0L // 2.2 (PAT/PMT), counted only
        val p1Errors: Long get() = syncLoss + syncByteErr + patErr + ccErr + pmtErr + pidErr
    }

    private class Pid(var cc: Int, var last: ByteArray, var repeats: Int = 0)
    private class Collect(var buf: ByteArray = ByteArray(0), var active: Boolean = false)
    private class Program(val number: Int, var lastNs: Long? = null, var streams: Set<Int> = emptySet())

    val counters = Counters()
    var firstPatNs: Long? = null
        private set
    var firstPmtNs: Long? = null
        private set
    private val pidTimeoutNs = (pidTimeoutS * 1e9).toLong()
    private var inSync = false
    private var goodRun = 0
    private var badRun = 0
    private val pids = HashMap<Int, Pid>()
    private var lastPatNs: Long? = null
    private val programs = HashMap<Int, Program>() // PMT PID -> program
    private val streamSeen = HashMap<Int, Long>() // listed PID -> last seen (or listed)
    private val collect = HashMap<Int, Collect>()

    val patPmtSeen: Boolean get() = firstPatNs != null && firstPmtNs != null

    /** One datagram's TS payload (normally 7 x 188 bytes) at [offset, end). */
    fun feed(data: ByteArray, offset: Int, end: Int, arrivalNs: Long) {
        checkTimeouts(arrivalNs)
        val n = end - offset
        var off = offset
        while (off + TS_SIZE <= offset + n - n % TS_SIZE) {
            packet(data.copyOfRange(off, off + TS_SIZE), arrivalNs)
            off += TS_SIZE
        }
        if (n % TS_SIZE != 0) badSync() // a cut packet cannot be trusted
    }

    /** The stream was gone: the timers start again from now (not a missing table). */
    fun resync(nowNs: Long) {
        if (lastPatNs != null) lastPatNs = nowNs
        for (p in programs.values) if (p.lastNs != null) p.lastNs = nowNs
        for (k in streamSeen.keys) streamSeen[k] = nowNs
    }

    /** Also call once per second so a stream that stopped is still checked. */
    fun checkTimeouts(nowNs: Long) {
        lastPatNs?.let { val (n, l) = overdue(it, nowNs, SECTION_TIMEOUT_NS); counters.patErr += n; lastPatNs = l }
        for (p in programs.values) {
            p.lastNs?.let { val (n, l) = overdue(it, nowNs, SECTION_TIMEOUT_NS); counters.pmtErr += n; p.lastNs = l }
        }
        for ((pid, seen) in streamSeen.entries.toList()) {
            val (n, l) = overdue(seen, nowNs, pidTimeoutNs)
            counters.pidErr += n
            streamSeen[pid] = l
        }
    }

    private fun packet(pkt: ByteArray, now: Long) {
        if (u8(pkt, 0) != SYNC) {
            badSync()
            return
        }
        badRun = 0
        if (!inSync) {
            goodRun++
            if (goodRun >= SYNC_ACQUIRE) inSync = true
        }
        counters.packets++
        val pid = ((u8(pkt, 1) and 0x1F) shl 8) or u8(pkt, 2)
        if (pid == NULL_PID) return
        if (u8(pkt, 1) and 0x80 != 0) counters.teiErr++
        val pusi = u8(pkt, 1) and 0x40 != 0
        val scrambled = u8(pkt, 3) shr 6
        val afc = (u8(pkt, 3) shr 4) and 0x3
        val cc = u8(pkt, 3) and 0x0F
        var start = 4
        var discontinuity = false
        if (afc and 0x2 != 0) {
            val afLen = u8(pkt, 4)
            discontinuity = afLen > 0 && (u8(pkt, 5) and 0x80) != 0
            start = 5 + afLen
        }
        checkCc(pid, cc, pkt, hasPayload = afc and 0x1 != 0, discontinuity = discontinuity)
        if (pid in streamSeen) streamSeen[pid] = now
        if (pid == PAT_PID || pid in programs) {
            if (scrambled != 0) {
                if (pid == PAT_PID) counters.patErr++ else counters.pmtErr++
            } else if (afc and 0x1 != 0 && start < TS_SIZE) {
                for (sec in sections(pid, pkt.copyOfRange(start, TS_SIZE), pusi)) onSection(pid, sec, now)
            }
        }
    }

    private fun badSync() {
        goodRun = 0
        if (!inSync) return // before the first sync nothing is judged
        counters.syncByteErr++
        badRun++
        if (badRun >= SYNC_LOSE) {
            counters.syncLoss++
            inSync = false
            badRun = 0
        }
    }

    private fun checkCc(pid: Int, cc: Int, pkt: ByteArray, hasPayload: Boolean, discontinuity: Boolean) {
        val state = pids[pid]
        if (state == null || discontinuity) {
            pids[pid] = Pid(cc, pkt)
            return
        }
        if (!hasPayload) { // the counter does not move without payload
            if (cc != state.cc) counters.ccErr++
            pids[pid] = Pid(cc, pkt)
            return
        }
        if (cc == state.cc && sameButPcr(pkt, state.last)) {
            state.repeats++
            if (state.repeats > 1) counters.ccErr++ // one duplicate is allowed
            return
        }
        if (cc != ((state.cc + 1) and 0x0F)) counters.ccErr++
        pids[pid] = Pid(cc, pkt)
    }

    private fun sections(pid: Int, payload: ByteArray, pusi: Boolean): List<ByteArray> {
        val col = collect.getOrPut(pid) { Collect() }
        val out = ArrayList<ByteArray>()
        if (pusi) {
            val pointer = u8(payload, 0)
            if (col.active) { // the bytes before the pointer end the previous section
                col.buf += payload.copyOfRange(1, minOf(1 + pointer, payload.size))
                out += finish(col)
            }
            col.buf = if (1 + pointer <= payload.size) payload.copyOfRange(1 + pointer, payload.size) else ByteArray(0)
            col.active = true
        } else if (col.active) {
            col.buf += payload
        } else {
            return out // continuation without a start: nothing to read
        }
        out += finish(col)
        return out
    }

    private fun finish(col: Collect): List<ByteArray> {
        val out = ArrayList<ByteArray>()
        while (col.active && col.buf.size >= 3) {
            if (u8(col.buf, 0) == 0xFF) { // stuffing: no more sections in this packet
                col.buf = ByteArray(0)
                col.active = false
                break
            }
            val need = 3 + (((u8(col.buf, 1) and 0x0F) shl 8) or u8(col.buf, 2))
            if (col.buf.size < need) break
            out += col.buf.copyOfRange(0, need)
            col.buf = col.buf.copyOfRange(need, col.buf.size)
        }
        if (col.active && col.buf.isEmpty()) col.active = false
        return out
    }

    private fun onSection(pid: Int, sec: ByteArray, now: Long) {
        if (sec.size < 12 || crc32Mpeg2(sec) != 0L) {
            counters.crcErr++ // 2.2; the section is not used
            return
        }
        val body = sec.copyOfRange(0, sec.size - 4)
        if (pid == PAT_PID) {
            if (u8(sec, 0) != 0x00) {
                counters.patErr++ // 1.3.a: another table on PID 0
                return
            }
            if (lastPatNs == null) firstPatNs = now
            lastPatNs = now
            pat(body)
        } else if (u8(sec, 0) == 0x02) { // 1.5.a counts only a missing PMT
            val prog = programs.getValue(pid)
            if (prog.lastNs == null && firstPmtNs == null) firstPmtNs = now
            prog.lastNs = now
            pmt(prog, body, now)
        }
    }

    private fun pat(sec: ByteArray) {
        var i = 8
        while (i < sec.size - 3) {
            val program = (u8(sec, i) shl 8) or u8(sec, i + 1)
            val pmtPid = ((u8(sec, i + 2) and 0x1F) shl 8) or u8(sec, i + 3)
            if (program != 0 && pmtPid !in programs) programs[pmtPid] = Program(program)
            i += 4
        }
    }

    private fun pmt(prog: Program, sec: ByteArray, now: Long) {
        if (sec.size < 12) return
        var i = 12 + (((u8(sec, 10) and 0x0F) shl 8) or u8(sec, 11))
        val streams = HashSet<Int>()
        while (i + 5 <= sec.size) {
            val infoLen = ((u8(sec, i + 3) and 0x0F) shl 8) or u8(sec, i + 4)
            val desc = sec.copyOfRange(minOf(i + 5, sec.size), minOf(i + 5 + infoLen, sec.size))
            if (u8(sec, i) in VIDEO_AUDIO_TYPES && !specialAudio(desc)) {
                streams += ((u8(sec, i + 1) and 0x1F) shl 8) or u8(sec, i + 2)
            }
            i += 5 + infoLen
        }
        for (pid in streams - prog.streams) streamSeen.putIfAbsent(pid, now)
        for (pid in prog.streams - streams) streamSeen.remove(pid)
        prog.streams = streams
    }

    companion object {
        const val TS_SIZE = 188
        const val SYNC = 0x47
        const val NULL_PID = 0x1FFF
        const val PAT_PID = 0
        const val SECTION_TIMEOUT_NS = 500_000_000L
        const val SYNC_ACQUIRE = 5
        const val SYNC_LOSE = 2
        const val PID_TIMEOUT_S = 5.0
        const val ISO_639_TAG = 0x0A
        // ISO/IEC 13818-1 stream_type values that are always video or audio (0x06 is not)
        val VIDEO_AUDIO_TYPES = setOf(0x01, 0x02, 0x10, 0x1B, 0x24, 0x33, 0x42, 0x03, 0x04, 0x0F, 0x11, 0x1C, 0x81, 0x87)

        private val CRC = LongArray(256) { i ->
            var c = i.toLong() shl 24
            repeat(8) { c = if (c and 0x80000000L != 0L) ((c shl 1) xor 0x04C11DB7L) else (c shl 1) }
            c and 0xFFFFFFFFL
        }

        /** CRC-32/MPEG-2 (ISO/IEC 13818-1 annex A). A whole section with its CRC gives 0. */
        fun crc32Mpeg2(data: ByteArray): Long {
            var crc = 0xFFFFFFFFL
            for (b in data) crc = ((crc shl 8) and 0xFFFFFFFFL) xor CRC[(((crc shr 24) xor (b.toLong() and 0xFF)) and 0xFF).toInt()]
            return crc
        }

        private fun overdue(lastNs: Long, nowNs: Long, limitNs: Long): Pair<Long, Long> {
            if (nowNs - lastNs <= limitNs) return 0L to lastNs
            val n = (nowNs - lastNs - 1) / limitNs
            return n to lastNs + n * limitNs
        }

        /** 13818-1 2.4.3.3: a duplicate repeats every byte, except its PCR fields. */
        private fun sameButPcr(a: ByteArray, b: ByteArray): Boolean {
            if (a.contentEquals(b)) return true
            val hasPcr = (u8(a, 3) and 0x20) != 0 && u8(a, 4) >= 7 && (u8(a, 5) and 0x10) != 0
            return hasPcr && a.copyOfRange(0, 6).contentEquals(b.copyOfRange(0, 6)) &&
                a.copyOfRange(12, a.size).contentEquals(b.copyOfRange(12, b.size))
        }

        /** True when an ISO 639 descriptor says audio_type > 0 (e.g. commentary). */
        private fun specialAudio(d: ByteArray): Boolean {
            var j = 0
            while (j + 2 <= d.size) {
                val tag = u8(d, j)
                val len = u8(d, j + 1)
                if (tag == ISO_639_TAG) {
                    var k = j + 2 + 3
                    while (k < j + 2 + len) {
                        if (k < d.size && u8(d, k) > 0) return true
                        k += 4
                    }
                }
                j += 2 + len
            }
            return false
        }

        private fun u8(b: ByteArray, i: Int) = b[i].toInt() and 0xFF
    }
}
