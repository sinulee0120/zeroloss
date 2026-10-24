package com.zeroloss.receiver.measure

import org.json.JSONObject

/**
 * The receiving Wi-Fi in a second's record, with the PC's keys (02 7, GAP-104 3): rssi
 * (dBm), wlm (receive link Mbps), wch (channel), wbd (band), wcc = 1 when the channel or
 * band differ from the second before. 2026-10-24 user: the live view showed RSSI for the
 * laptop only. Readings come from the caller (WifiManager), so this is testable.
 */
class WifiKeys {
    private var lastChannel: Int? = null
    private var lastBand: String? = null

    fun put(r: JSONObject, rssiDbm: Int?, rxMbps: Int?, freqMhz: Int?) {
        if (freqMhz == null || freqMhz <= 0) return // not on a Wi-Fi network: no keys
        if (rssiDbm != null && rssiDbm > -127 && rssiDbm < 0) r.put("rssi", rssiDbm)
        if (rxMbps != null && rxMbps > 0) r.put("wlm", rxMbps)
        val ch = channelOf(freqMhz)
        val band = bandOf(freqMhz)
        if (ch != null) r.put("wch", ch)
        r.put("wbd", band)
        if (lastChannel != null && (ch != lastChannel || band != lastBand)) r.put("wcc", 1)
        lastChannel = ch
        lastBand = band
    }

    companion object {
        fun bandOf(f: Int): String = when {
            f < 3000 -> "2.4GHz"
            f < 5925 -> "5GHz"
            else -> "6GHz"
        }

        fun channelOf(f: Int): Int? = when {
            f == 2484 -> 14
            f in 2412..2472 -> (f - 2407) / 5
            f in 5160..5885 -> (f - 5000) / 5
            f in 5955..7115 -> (f - 5950) / 5
            else -> null
        }
    }
}
