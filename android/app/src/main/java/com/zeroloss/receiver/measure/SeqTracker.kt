package com.zeroloss.receiver.measure

import kotlin.math.max
import kotlin.math.roundToLong

/**
 * Sequence tracker: loss, duplicates, reordering with a time window, late packets.
 * A line-for-line port of core/blocks/seq_tracker.py (docs/05-standards.md 2.1:
 * RFC 3550 A.3, RFC 7003 late packets, RFC 3357 bursts), so a tablet counts exactly as
 * the PC receiver does.
 *
 * - A gap is not lost at once. Missing numbers wait in a reorder window (default 1,000 ms).
 *   Arriving inside the window -> reorderedRecovered. Window expired -> lost. Arriving
 *   after that -> lateDiscarded (stays counted as lost).
 * - A new SSRC starts a new segment (sender restart); never counted as loss.
 * - Outages longer than one 16-bit cycle are resolved with the RTP timestamp and the
 *   recent packet rate.
 */
class SeqTracker(reorderWindowMs: Long = 1000) {
    class Stats {
        var received = 0L
        var lost = 0L
        var duplicates = 0L
        var reorderedRecovered = 0L
        var lateDiscarded = 0L
        var lossEvents = 0L
        var maxBurst = 0L
        var segmentRestarts = 0L
    }

    private class Gap(val first: Long, val last: Long, val deadlineNs: Long, val missing: MutableSet<Long>)

    val stats = Stats()
    private val windowNs = reorderWindowMs * NS_PER_MS
    private var periodMaxBurst = 0L
    private var ssrc: Long? = null

    private var maxExt: Long? = null
    private var maxTs = 0L
    private var seen = HashSet<Long>()
    private var lostFinal = HashSet<Long>()
    private val gaps = ArrayDeque<Gap>()
    private var rateRef: Pair<Long, Long>? = null // (ext_seq, ts_unwrapped)
    private var tsUnwrapped = 0L
    private var pktsPerTick: Double? = null

    private fun resetSegment() {
        maxExt = null
        maxTs = 0
        seen = HashSet()
        lostFinal = HashSet()
        gaps.clear()
        rateRef = null
        tsUnwrapped = 0
        pktsPerTick = null
    }

    /** Feed one RTP packet in arrival order. */
    fun onPacket(seq: Int, rtpTs: Long, ssrc: Long, arrivalNs: Long) {
        advance(arrivalNs)
        if (this.ssrc == null) {
            this.ssrc = ssrc
        } else if (ssrc != this.ssrc) {
            closeGaps()
            this.ssrc = ssrc
            resetSegment()
            stats.segmentRestarts++
        }
        val top = maxExt
        if (top == null) {
            start(seq, rtpTs)
            return
        }
        val maxSeq = Math.floorMod(top, SEQ_MOD)
        val delta = Math.floorMod(seq - maxSeq, SEQ_MOD)
        val tsStep = tsStep(rtpTs)
        val adv = longOutageAdvance(delta, tsStep)
        when {
            adv != null -> forward(adv, rtpTs, tsStep, arrivalNs, longOutage = true)
            delta == 0L -> oldPacket(top)
            delta < SEQ_MOD / 2 -> forward(delta, rtpTs, tsStep, arrivalNs, longOutage = false)
            else -> oldPacket(top - (SEQ_MOD - delta))
        }
    }

    /** Finalize gaps whose reorder window has expired. Call at least once per second. */
    fun advance(nowNs: Long) {
        while (gaps.isNotEmpty() && gaps.first().deadlineNs <= nowNs) finalize(gaps.removeFirst())
    }

    /** End of measurement: anything still missing is lost. */
    fun close() = closeGaps()

    /** Longest burst finalized since the last call (per-second records, 02 7 ``mb``). */
    fun takePeriodMaxBurst(): Long {
        val out = periodMaxBurst
        periodMaxBurst = 0
        return out
    }

    private fun start(seq: Int, rtpTs: Long) {
        maxExt = seq.toLong()
        maxTs = rtpTs
        tsUnwrapped = rtpTs
        rateRef = Pair(seq.toLong(), rtpTs)
        markReceived(seq.toLong())
    }

    private fun tsStep(rtpTs: Long): Long {
        val step = Math.floorMod(rtpTs - maxTs, TS_MOD)
        return if (step >= TS_MOD / 2) 0 else step
    }

    private fun longOutageAdvance(delta: Long, tsStep: Long): Long? {
        val rate = pktsPerTick ?: return null
        if (rate == 0.0) return null
        val expected = tsStep * rate
        if (expected < SEQ_MOD / 2) return null
        val cycles = ((expected - delta) / SEQ_MOD).roundHalfEven()
        return delta + max(0L, cycles) * SEQ_MOD
    }

    private fun forward(advance: Long, rtpTs: Long, tsStep: Long, arrivalNs: Long, longOutage: Boolean) {
        val top = maxExt ?: return
        val newExt = top + advance
        if (newExt > top + 1) {
            val first = top + 1
            val last = newExt - 1
            gaps.addLast(Gap(first, last, arrivalNs + windowNs, (first..last).toMutableSet()))
        }
        maxExt = newExt
        maxTs = rtpTs
        tsUnwrapped += tsStep
        updateRate(newExt, tsUnwrapped, longOutage)
        markReceived(newExt)
    }

    private fun updateRate(ext: Long, tsUnwrapped: Long, longOutage: Boolean) {
        val ref = rateRef ?: return
        if (longOutage) {
            rateRef = Pair(ext, tsUnwrapped) // keep the pre-outage rate, restart the sample
            return
        }
        val ticks = tsUnwrapped - ref.second
        if (ticks >= RTP_CLOCK_HZ) { // refresh about once per second
            pktsPerTick = (ext - ref.first).toDouble() / ticks
            rateRef = Pair(ext, tsUnwrapped)
        }
    }

    private fun oldPacket(ext: Long) {
        if (ext in seen) {
            stats.duplicates++
            return
        }
        for (gap in gaps) {
            if (ext in gap.first..gap.last && gap.missing.remove(ext)) {
                stats.reorderedRecovered++
                markReceived(ext)
                return
            }
        }
        if (lostFinal.remove(ext)) {
            stats.lateDiscarded++
            seen.add(ext)
        }
        // Older than anything tracked (before the join point): ignore, not an error.
    }

    private fun markReceived(ext: Long) {
        seen.add(ext)
        stats.received++
        prune()
    }

    private fun prune() {
        val top = maxExt ?: return
        if (seen.size < 2 * SEQ_MOD) return
        val floor = top - SEQ_MOD
        seen = seen.filterTo(HashSet()) { it >= floor }
        lostFinal = lostFinal.filterTo(HashSet()) { it >= floor }
    }

    private fun closeGaps() {
        while (gaps.isNotEmpty()) finalize(gaps.removeFirst())
    }

    private fun finalize(gap: Gap) {
        if (gap.missing.isEmpty()) return
        stats.lost += gap.missing.size
        lostFinal.addAll(gap.missing)
        var runLen = 0L
        var s = gap.first
        while (s <= gap.last + 1) {
            if (s <= gap.last && s in gap.missing) {
                runLen++
            } else if (runLen > 0) {
                addBurst(runLen)
                runLen = 0
            }
            s++
        }
    }

    private fun addBurst(length: Long) {
        stats.lossEvents++
        stats.maxBurst = max(stats.maxBurst, length)
        periodMaxBurst = max(periodMaxBurst, length)
    }

    companion object {
        const val SEQ_MOD = 1L shl 16
        const val TS_MOD = 1L shl 32
        const val NS_PER_MS = 1_000_000L
        const val RTP_CLOCK_HZ = 90_000L
    }
}

/** Python's round(): halves to even, as the PC tracker rounds. */
internal fun Double.roundHalfEven(): Long = Math.rint(this).roundToLong()
