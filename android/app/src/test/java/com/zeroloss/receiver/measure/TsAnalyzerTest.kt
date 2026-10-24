package com.zeroloss.receiver.measure

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The cases of tests for core/blocks/ts_analyzer.py that matter most on a tablet. */
class TsAnalyzerTest {
    private val ms = 1_000_000L

    private fun pkt(pid: Int, cc: Int, pusi: Boolean = false, payload: ByteArray = ByteArray(0)): ByteArray {
        val p = ByteArray(188) { 0xFF.toByte() }
        p[0] = 0x47
        p[1] = ((if (pusi) 0x40 else 0) or (pid shr 8)).toByte()
        p[2] = pid.toByte()
        p[3] = (0x10 or (cc and 0x0F)).toByte() // payload only
        payload.copyInto(p, 4)
        return p
    }

    /** A PAT section (program 1 -> PMT PID 0x100) with its CRC, behind a pointer field 0. */
    private fun patPayload(): ByteArray {
        val body = byteArrayOf(0x00, 0xB0.toByte(), 13, 0, 1, 0xC1.toByte(), 0, 0, 0, 1, 0xE1.toByte(), 0x00)
        val crc = TsAnalyzer.crc32Mpeg2(body)
        val sec = body + byteArrayOf((crc shr 24).toByte(), (crc shr 16).toByte(), (crc shr 8).toByte(), crc.toByte())
        return byteArrayOf(0) + sec
    }

    private fun feed(t: TsAnalyzer, pkts: List<ByteArray>, at: Long) {
        val d = pkts.fold(ByteArray(0)) { a, b -> a + b }
        t.feed(d, 0, d.size, at)
    }

    @Test fun aCleanStreamHasNoP1Errors() {
        val t = TsAnalyzer()
        for (i in 0 until 100) feed(t, listOf(pkt(0x200, i)), i * ms)
        assertEquals(0, t.counters.p1Errors)
        assertEquals(100, t.counters.packets)
    }

    @Test fun aSkippedCounterIsOneCcError() {
        val t = TsAnalyzer()
        feed(t, listOf(pkt(0x200, 0), pkt(0x200, 1), pkt(0x200, 3)), 0)
        assertEquals(1, t.counters.ccErr)
    }

    @Test fun oneRepeatIsAllowedTwoAreNot() {
        val t = TsAnalyzer()
        feed(t, listOf(pkt(0x200, 0), pkt(0x200, 0)), 0)
        assertEquals(0, t.counters.ccErr)
        feed(t, listOf(pkt(0x200, 0)), ms)
        assertEquals(1, t.counters.ccErr)
    }

    @Test fun aPatLateByMoreThanHalfASecondIsAnError() {
        val t = TsAnalyzer()
        feed(t, listOf(pkt(0, 0, pusi = true, payload = patPayload())), 0)
        assertTrue(t.firstPatNs != null)
        t.checkTimeouts(400 * ms)
        assertEquals(0, t.counters.patErr)
        t.checkTimeouts(2100 * ms) // 2.1 s without a PAT: 4 errors
        assertEquals(4, t.counters.patErr)
    }

    @Test fun theMpeg2CrcOfAWholeSectionIsZero() {
        val body = patPayload().copyOfRange(1, 17)
        assertEquals(0L, TsAnalyzer.crc32Mpeg2(body))
    }
}
