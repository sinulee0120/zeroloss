package com.zeroloss.receiver.play

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

/** The pipe never blocks the receiving side; the monitor closes seconds with the PC keys. */
class PlayTest {
    private val s = 1_000_000_000L

    @Test fun aFullPipeDropsTheOldestAndCountsIt() {
        val p = TsPipe(capacityChunks = 2)
        p.put(byteArrayOf(1)); p.put(byteArrayOf(2)); p.put(byteArrayOf(3))
        assertEquals(1L, p.overflowChunks.get())
        assertEquals(2, p.take(0)!![0].toInt())
        assertEquals(3, p.take(0)!![0].toInt())
        assertNull(p.take(0))
    }

    @Test fun aClosedPipeTakesNothingMore() {
        val p = TsPipe()
        p.close()
        p.put(byteArrayOf(1))
        assertNull(p.take(0))
    }

    @Test fun beforeTheFirstFrameItIsNoneWithoutCounts() {
        val m = PlaybackMonitor().apply { active = true }
        val r = m.second(s)!!
        assertEquals("none", r.getString("pb"))
        assertFalse(r.has("pdr"))
    }

    @Test fun anInactiveMonitorWritesNothing() {
        assertNull(PlaybackMonitor().second(s))
    }

    @Test fun noFrameForHalfASecondStartsOneFreeze() {
        val m = PlaybackMonitor().apply { active = true }
        m.onFrame() // at 0 in unit tests (SystemClock returns 0)
        val first = m.second(s)!!
        assertEquals("freeze", first.getString("pb"))
        assertEquals(1, first.getInt("pfs"))
        val next = m.second(2 * s)!!
        assertEquals("freeze", next.getString("pb"))
        assertFalse(next.has("pfs")) // the same freeze goes on
        assertEquals(0, next.getInt("pdr"))
        assertEquals(0, next.getInt("pau"))
    }
}
