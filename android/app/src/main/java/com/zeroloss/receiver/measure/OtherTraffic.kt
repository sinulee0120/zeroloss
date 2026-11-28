package com.zeroloss.receiver.measure

/**
 * Bytes the tablet received this second for other apps (02 7 `orx`, iptv R-17).
 *
 * 2026-11-28 (user "안만든거 다 만들어"; R-17 was "later phase"): a tablet's own background
 * work (a Play Store update, a cloud sync) shares its Wi-Fi with the stream. The kernel's
 * counters give every app's bytes and ours: other = total - ours.
 *
 * 2026-11-28 R20261128-121753 (both tablets): Android updates the per-app counter a second
 * or two after the total, so the stream itself showed as "other" for a second (4.5 MB = one
 * second of UHD, then 0, again and again). So the running total - ours is kept, and a
 * second's other bytes are how much its floor (the lowest of the last [window] readings) rose:
 * a lag of up to window-1 seconds never raises the floor; traffic that stays does, later
 * by up to window-1 seconds. The first readings only fill the window; a counter that is not
 * available (-1, TrafficStats.UNSUPPORTED) or that went back (a reset) starts it again.
 */
class OtherTraffic(
    private val totalRx: () -> Long,
    private val ownRx: () -> Long,
    private val window: Int = WINDOW,
) {
    private val recent = ArrayDeque<Long>()
    private var lastTotal = -1L
    private var lastOwn = -1L
    private var lastFloor = -1L

    fun second(): Long? {
        val total = totalRx()
        val own = ownRx()
        if (total < 0 || own < 0 || total < lastTotal || own < lastOwn) {
            recent.clear()
            lastFloor = -1L
            lastTotal = total
            lastOwn = own
            return null
        }
        lastTotal = total
        lastOwn = own
        recent.addLast(total - own)
        while (recent.size > window) recent.removeFirst()
        if (recent.size < window) return null
        val floor = recent.min()
        val prev = lastFloor
        lastFloor = maxOf(prev, floor)
        return if (prev < 0) null else maxOf(0L, floor - prev)
    }

    companion object {
        const val WINDOW = 4 // readings: a per-app counter late by up to 3 s is not traffic
    }
}
