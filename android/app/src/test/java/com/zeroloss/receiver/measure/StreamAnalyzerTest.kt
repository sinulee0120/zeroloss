package com.zeroloss.receiver.measure

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class StreamAnalyzerTest {
    private val ms = 1_000_000L

    /** RTP, PT 33, one 188-byte TS packet. */
    private fun rtp(seq: Int, ts: Long, ssrc: Long = 7): ByteArray {
        val b = ByteArray(12 + 188)
        b[0] = 0x80.toByte()
        b[1] = 33
        b[2] = (seq shr 8).toByte(); b[3] = seq.toByte()
        for (i in 0..3) b[4 + i] = (ts shr (24 - 8 * i)).toByte()
        for (i in 0..3) b[8 + i] = (ssrc shr (24 - 8 * i)).toByte()
        b[12] = 0x47
        return b
    }

    @Test fun aSecondRecordHasThePcKeys() {
        val a = StreamAnalyzer(ch = 1)
        for (i in 0 until 100) {
            val d = rtp(i, i * 900L)
            a.onDatagram(d, d.size, i * 10 * ms)
        }
        val r = a.second(1000 * ms)
        assertEquals("s", r.getString("type"))
        assertEquals(0, r.getInt("e"))
        assertEquals(1, r.getInt("ch"))
        assertEquals(100, r.getLong("rx"))
        assertEquals(0, r.getLong("lost"))
        assertEquals(100L * 200 * 8, r.getLong("br"))
        assertTrue(r.has("dlo") && r.has("dhi") && r.has("dpw"))
        assertFalse(r.has("lev")) // detail keys only when not zero
        assertEquals(1, a.second(2000 * ms).getInt("e"))
    }

    @Test fun lostShowsInTheSecondItIsFinal() {
        val a = StreamAnalyzer(ch = 1)
        for (i in listOf(0, 1, 2, 6, 7)) {
            val d = rtp(i, i * 900L)
            a.onDatagram(d, d.size, i * 10 * ms)
        }
        assertEquals(0, a.second(500 * ms).getLong("lost")) // inside the reorder window
        val r = a.second(1500 * ms)
        assertEquals(3, r.getLong("lost"))
        assertEquals(1, r.getLong("lev"))
        assertEquals(3, r.getLong("mb"))
    }

    /** 2026-11-27 (R20261122-223747 tablet-2): one duplicate made one CC error. */
    @Test fun aDuplicatedDatagramIsNoCcError() {
        val a = StreamAnalyzer(ch = 1)
        // two TS packets of PID 0x100 per datagram (a real one has 7): a copy goes back in
        // CC, which one repeated TS packet alone would not
        fun pkt(i: Int): ByteArray {
            val one = rtp(i, i * 900L)
            val b = one.copyOf(12 + 2 * 188)
            for (k in 0..1) {
                val o = 12 + k * 188
                b[o] = 0x47; b[o + 1] = 0x01; b[o + 2] = 0x00
                b[o + 3] = (0x10 or ((2 * i + k) and 0x0F)).toByte()
            }
            return b
        }
        for (i in 0 until 100) {
            val d = pkt(i)
            a.onDatagram(d, d.size, i * 10 * ms)
            if (i == 50) a.onDatagram(d, d.size, i * 10 * ms + 1000) // the copy
        }
        val r = a.second(1000 * ms)
        assertEquals(1, r.getLong("dup"))
        assertEquals(0, r.getLong("cc"))
        assertEquals(0, r.getLong("lost"))
    }

    @Test fun notRtpIsCountedAndSkipped() {
        val a = StreamAnalyzer(ch = 1)
        val ts = ByteArray(188).also { it[0] = 0x47 }
        a.onDatagram(ts, ts.size, 0)
        assertEquals(1, a.notRtp)
        assertEquals(0, a.second(1000 * ms).getLong("rx"))
    }
}
