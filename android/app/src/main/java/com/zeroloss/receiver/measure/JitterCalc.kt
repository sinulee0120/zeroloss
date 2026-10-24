package com.zeroloss.receiver.measure

import kotlin.math.abs
import kotlin.math.max

/**
 * RFC 3550 6.4.1 interarrival jitter, as core/blocks/jitter_calc.py (05-standards 2.2):
 *     D(i,j) = (Rj - Ri) - (Sj - Si)   R = arrival, S = RTP timestamp, both in RTP ticks
 *     J      = J + (|D| - J) / 16
 * A new SSRC restarts the estimator; the MPEG-TS marker bit (RFC 2250 2.1) says the
 * timestamp jumped on purpose and that pair is skipped. Values in milliseconds.
 */
class JitterCalc(private val clockHz: Long = 90_000) {
    private var ssrc: Long? = null
    private var lastArrivalNs: Long? = null
    private var lastTs = 0L
    private var jitterTicks = 0.0
    private var periodMaxTicks = 0.0

    fun onPacket(rtpTs: Long, ssrc: Long, arrivalNs: Long, discontinuity: Boolean = false) {
        if (ssrc != this.ssrc) {
            this.ssrc = ssrc
            lastArrivalNs = null
            jitterTicks = 0.0
        }
        val last = lastArrivalNs
        if (last != null && !discontinuity) {
            val arrivalTicks = (arrivalNs - last).toDouble() * clockHz / NS_PER_S
            val d = arrivalTicks - signedTsDiff(rtpTs, lastTs)
            jitterTicks += (abs(d) - jitterTicks) / 16
            periodMaxTicks = max(periodMaxTicks, jitterTicks)
        }
        lastArrivalNs = arrivalNs
        lastTs = rtpTs
    }

    val jitterMs: Double get() = toMs(jitterTicks)

    /** Maximum jitter since the last call (the 1-second record), then reset. */
    fun takePeriodMaxMs(): Double {
        val value = toMs(max(periodMaxTicks, jitterTicks))
        periodMaxTicks = jitterTicks
        return value
    }

    private fun toMs(ticks: Double) = ticks * 1000 / clockHz

    companion object {
        private const val NS_PER_S = 1_000_000_000L
        private const val TS_MOD = 1L shl 32

        /** a - b for 32-bit RTP timestamps, correct across wraparound. */
        fun signedTsDiff(a: Long, b: Long): Long {
            val diff = Math.floorMod(a - b, TS_MOD)
            return if (diff >= TS_MOD / 2) diff - TS_MOD else diff
        }
    }
}
