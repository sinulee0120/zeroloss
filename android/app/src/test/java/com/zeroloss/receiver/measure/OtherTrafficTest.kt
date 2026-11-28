package com.zeroloss.receiver.measure

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class OtherTrafficTest {
    private fun run(totals: List<Long>, own: List<Long>): List<Long?> {
        val t = ArrayDeque(totals)
        val o = ArrayDeque(own)
        val other = OtherTraffic({ t.removeFirst() }, { o.removeFirst() })
        return totals.indices.map { other.second() }
    }

    @Test
    fun theStreamCountedLateIsNotOtherTraffic() {
        // R20261128-121753: the per-app counter came 1-2 s after the total (4.5 MB = 1 s)
        val s = 4_500_000L
        val totals = (0..12).map { it * s }
        val own = listOf(0L, 0L, 2 * s, 3 * s, 3 * s, 5 * s, 6 * s, 7 * s, 7 * s, 7 * s,
            10 * s, 11 * s, 12 * s)
        val got = run(totals, own)
        assertEquals(List(3) { null }, got.take(3)) // filling the window
        assertNull(got[3]) // the first floor only
        assertEquals(List(9) { 0L }, got.drop(4)) // the stream only: never "other"
    }

    @Test
    fun trafficThatStaysIsCountedAFewSecondsLater() {
        val totals = listOf(0L, 100L, 200L, 300L, 400L, 1_400L, 1_500L, 1_600L, 1_700L, 1_800L)
        val own = listOf(0L, 100L, 200L, 300L, 400L, 500L, 600L, 700L, 800L, 900L)
        val got = run(totals, own)
        assertEquals(900L, got.filterNotNull().sum()) // 900 bytes for another app, once
        assertEquals(900L, got[8]) // seen when it stayed for the whole window
    }

    @Test
    fun unsupportedOrResetCountersStartAgain() {
        val got = run(listOf(-1L, 10L, 20L, 30L, 40L, 50L, 5L), listOf(0L, 0L, 0L, 0L, 0L, 0L, 0L))
        assertNull(got[0]) // UNSUPPORTED
        assertEquals(listOf(null, null, null, null, 10L, null), got.drop(1))
    }
}
