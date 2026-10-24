package com.zeroloss.receiver.measure

import org.junit.Assert.assertEquals
import org.junit.Test

/** The same cases as tests for core/blocks/seq_tracker.py: a tablet counts as a PC does. */
class SeqTrackerTest {
    private val ms = 1_000_000L

    private fun feed(t: SeqTracker, seqs: List<Int>, startNs: Long = 0, stepNs: Long = ms) {
        seqs.forEachIndexed { i, s -> t.onPacket(s and 0xFFFF, (s * 90L) and 0xFFFFFFFFL, 1, startNs + i * stepNs) }
    }

    @Test fun inOrderHasNoLoss() {
        val t = SeqTracker()
        feed(t, (0 until 100).toList())
        t.close()
        assertEquals(100, t.stats.received)
        assertEquals(0, t.stats.lost)
    }

    @Test fun aGapIsLostOnlyAfterTheWindow() {
        val t = SeqTracker(reorderWindowMs = 1000)
        feed(t, listOf(0, 1, 2, 5, 6))
        t.advance(500 * ms)
        assertEquals(0, t.stats.lost) // still inside the reorder window
        t.advance(2000 * ms)
        assertEquals(2, t.stats.lost)
        assertEquals(1, t.stats.lossEvents)
        assertEquals(2, t.stats.maxBurst)
    }

    @Test fun lateInsideTheWindowIsReorderedNotLost() {
        val t = SeqTracker()
        feed(t, listOf(0, 1, 3, 2, 4))
        t.close()
        assertEquals(0, t.stats.lost)
        assertEquals(1, t.stats.reorderedRecovered)
    }

    @Test fun afterTheWindowItIsLateDiscardedAndStaysLost() {
        val t = SeqTracker(reorderWindowMs = 100)
        t.onPacket(0, 0, 1, 0)
        t.onPacket(2, 180, 1, 1 * ms)
        t.advance(500 * ms)
        t.onPacket(1, 90, 1, 600 * ms)
        assertEquals(1, t.stats.lost)
        assertEquals(1, t.stats.lateDiscarded)
    }

    @Test fun duplicatesAreCounted() {
        val t = SeqTracker()
        feed(t, listOf(0, 1, 1, 2, 2, 2))
        assertEquals(3, t.stats.received)
        assertEquals(3, t.stats.duplicates)
    }

    @Test fun theSequenceWrapsWithoutLoss() {
        val t = SeqTracker()
        feed(t, (65530 until 65546).toList())
        t.close()
        assertEquals(16, t.stats.received)
        assertEquals(0, t.stats.lost)
    }

    @Test fun aNewSourceIsNotLoss() {
        val t = SeqTracker()
        t.onPacket(100, 0, 1, 0)
        t.onPacket(101, 90, 1, ms)
        t.onPacket(5000, 0, 2, 2 * ms) // the sender restarted with another SSRC
        t.onPacket(5001, 90, 2, 3 * ms)
        t.close()
        assertEquals(0, t.stats.lost)
        assertEquals(1, t.stats.segmentRestarts)
    }
}
