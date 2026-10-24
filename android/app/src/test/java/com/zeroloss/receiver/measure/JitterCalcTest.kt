package com.zeroloss.receiver.measure

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class JitterCalcTest {
    @Test fun evenArrivalsHaveNoJitter() {
        val j = JitterCalc()
        for (i in 0 until 1000) j.onPacket(i * 900L, 1, i * 10_000_000L) // 10 ms, 900 ticks
        assertEquals(0.0, j.jitterMs, 1e-9)
    }

    @Test fun oneLatePacketMatchesTheRfc3550Estimate() {
        val j = JitterCalc()
        for (i in 0 until 10) j.onPacket(i * 900L, 1, i * 10_000_000L)
        j.onPacket(10 * 900L, 1, 10 * 10_000_000L + 16_000_000L) // 16 ms late
        // |D| = 16 ms -> J = 16 / 16 = 1 ms
        assertEquals(1.0, j.jitterMs, 1e-6)
        assertTrue(j.takePeriodMaxMs() >= 1.0)
    }

    @Test fun aMarkerSkipsThePairButKeepsJ() {
        val j = JitterCalc()
        j.onPacket(0, 1, 0)
        j.onPacket(900, 1, 10_000_000L)
        j.onPacket(9_000_000, 1, 20_000_000L, discontinuity = true) // timestamp jumped on purpose
        assertEquals(0.0, j.jitterMs, 1e-9)
    }

    @Test fun timestampsWrap() {
        assertEquals(10, JitterCalc.signedTsDiff(5, (1L shl 32) - 5))
        assertEquals(-10, JitterCalc.signedTsDiff((1L shl 32) - 5, 5))
    }
}
