package com.zeroloss.receiver.measure

import org.json.JSONObject

/**
 * One channel's measurement, closed once a second into a record with the same short keys
 * as the PC receiver (docs/02-data.md 7, modules/iptv/record_keys.py), so the receiving
 * Agent summarizes and judges a tablet with the same Python code as a PC.
 *
 * Arrival times come from the caller (SystemClock.elapsedRealtimeNanos right after the
 * socket read), so tests are exact. Not yet here (next steps): the clock-skew fit of
 * dlo/dhi, player readings (pb, p*), Wi-Fi keys.
 */
class StreamAnalyzer(val ch: Int, reorderWindowMs: Long = 1000) {
    val seq = SeqTracker(reorderWindowMs)
    val jitter = JitterCalc()
    val ts = TsAnalyzer()
    private var lastArrivalNs: Long? = null
    var notRtp = 0L
        private set
    var otherPt = 0L
        private set
    var firstPacketNs: Long? = null
        private set

    private var periodPackets = 0L
    private var periodBytes = 0L
    private var elapsed = 0
    private var last = Totals()

    // one-way delay relative to the first packet of this source (02 7 dlo/dhi, RFC 5481 PDV)
    private var transitSsrc: Long? = null
    private var transitBase: Pair<Long, Long>? = null // (arrival ns, unwrapped RTP ts)
    private var tsUnwrapped = 0L
    private var lastTs = 0L
    private var secondLo: Double? = null
    private var secondHi: Double? = null
    private val pdvWindow = ArrayDeque<Triple<Int, Double, Double>>() // (e, lo, hi)

    private data class Totals(
        val rx: Long = 0, val lost: Long = 0, val dup: Long = 0, val reo: Long = 0,
        val late: Long = 0, val lev: Long = 0, val cc: Long = 0, val p1: Long = 0,
        val sync: Long = 0, val sbe: Long = 0, val pate: Long = 0, val pmte: Long = 0,
        val pide: Long = 0, val tei: Long = 0, val crc: Long = 0,
    )

    fun onDatagram(data: ByteArray, length: Int, arrivalNs: Long) {
        periodPackets++
        periodBytes += length
        val h = RtpHeader.parse(data, length)
        if (h == null) {
            notRtp++
            return
        }
        if (h.payloadType != RtpHeader.MP2T) {
            otherPt++ // someone else's flow: never this channel's packets
            return
        }
        if (firstPacketNs == null) firstPacketNs = arrivalNs
        seq.onPacket(h.seq, h.timestamp, h.ssrc, arrivalNs)
        jitter.onPacket(h.timestamp, h.ssrc, arrivalNs, discontinuity = h.marker)
        transit(h.ssrc, h.timestamp, arrivalNs)
        val last = lastArrivalNs
        if (last != null && arrivalNs - last > STOPPED_NS) ts.resync(arrivalNs) // the stream was gone
        ts.feed(data, h.payloadOffset, h.payloadEnd, arrivalNs)
        lastArrivalNs = arrivalNs
    }

    private fun transit(ssrc: Long, ts: Long, arrivalNs: Long) {
        if (ssrc != transitSsrc || transitBase == null) {
            transitSsrc = ssrc
            tsUnwrapped = ts
            lastTs = ts
            transitBase = Pair(arrivalNs, ts)
            pdvWindow.clear() // a new source has its own baseline: no window across it
        } else {
            tsUnwrapped += JitterCalc.signedTsDiff(ts, lastTs)
            lastTs = ts
        }
        val base = transitBase ?: return
        val ms = (arrivalNs - base.first) / 1e6 - (tsUnwrapped - base.second) * 1000.0 / 90_000
        secondLo = secondLo?.let { minOf(it, ms) } ?: ms
        secondHi = secondHi?.let { maxOf(it, ms) } ?: ms
    }

    /** Close one second (the caller's 1 s tick) into a record; null before any packet. */
    fun second(nowNs: Long): JSONObject {
        seq.advance(nowNs)
        // TR 101 290 time limits by arrival; a stream that stopped is said by rx 0, not P1
        lastArrivalNs?.let { if (nowNs - it <= STOPPED_NS) ts.checkTimeouts(it) }
        val s = seq.stats
        val c = ts.counters
        val now = Totals(
            s.received, s.lost, s.duplicates, s.reorderedRecovered, s.lateDiscarded, s.lossEvents,
            c.ccErr, c.p1Errors, c.syncLoss, c.syncByteErr, c.patErr, c.pmtErr, c.pidErr, c.teiErr, c.crcErr,
        )
        val r = JSONObject()
        r.put("type", "s")
        r.put("e", elapsed)
        r.put("ch", ch)
        r.put("rx", now.rx - last.rx)
        r.put("lost", now.lost - last.lost)
        r.put("dup", now.dup - last.dup)
        r.put("reo", now.reo - last.reo)
        r.put("jit", Math.round(jitter.takePeriodMaxMs() * 1000) / 1000.0)
        r.put("br", periodBytes * 8)
        r.put("cc", now.cc - last.cc)
        r.put("p1", now.p1 - last.p1)
        r.put("pat", if (ts.patPmtSeen) 1 else 0)
        detail(r, "lev", now.lev - last.lev)
        detail(r, "late", now.late - last.late)
        detail(r, "mb", seq.takePeriodMaxBurst())
        detail(r, "sync", now.sync - last.sync)
        detail(r, "sbe", now.sbe - last.sbe)
        detail(r, "pate", now.pate - last.pate)
        detail(r, "pmte", now.pmte - last.pmte)
        detail(r, "pide", now.pide - last.pide)
        detail(r, "tei", now.tei - last.tei)
        detail(r, "crc", now.crc - last.crc)
        val lo = secondLo
        val hi = secondHi
        if (lo != null && hi != null) {
            r.put("dlo", round3(lo))
            r.put("dhi", round3(hi))
            r.put("dpw", round3(windowPdv(lo, hi)))
        }
        secondLo = null
        secondHi = null
        periodPackets = 0
        periodBytes = 0
        last = now
        elapsed++
        return r
    }

    /** Delay variation of the last 10 s (02 7 dpw): a clock slew stays out of it. */
    private fun windowPdv(lo: Double, hi: Double): Double {
        pdvWindow.addLast(Triple(elapsed, lo, hi))
        while (pdvWindow.isNotEmpty() && pdvWindow.first().first <= elapsed - PDV_WINDOW_S) {
            pdvWindow.removeFirst()
        }
        return pdvWindow.maxOf { it.third } - pdvWindow.minOf { it.second }
    }

    private fun detail(r: JSONObject, key: String, value: Long) {
        if (value != 0L) r.put(key, value) // detail keys only when not zero (02 7)
    }

    private fun round3(x: Double) = Math.round(x * 1000) / 1000.0

    companion object {
        const val PDV_WINDOW_S = 10
        const val STOPPED_NS = 2_000_000_000L // no packet for this long: the stream stopped
    }
}
