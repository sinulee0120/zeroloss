package com.zeroloss.receiver.measure

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Test

class WifiKeysTest {
    @Test fun aSecondOnFiveGigahertz() {
        val r = JSONObject()
        WifiKeys().put(r, -45, 866, 5745)
        assertEquals(-45, r.getInt("rssi"))
        assertEquals(866, r.getInt("wlm"))
        assertEquals(149, r.getInt("wch"))
        assertEquals("5GHz", r.getString("wbd"))
        assertFalse(r.has("wcc"))
    }

    @Test fun aChannelChangeIsMarkedOnce() {
        val w = WifiKeys()
        w.put(JSONObject(), -50, 400, 5745)
        val moved = JSONObject().also { w.put(it, -55, 144, 2437) }
        assertEquals(6, moved.getInt("wch"))
        assertEquals("2.4GHz", moved.getString("wbd"))
        assertEquals(1, moved.getInt("wcc"))
        val same = JSONObject().also { w.put(it, -55, 144, 2437) }
        assertFalse(same.has("wcc"))
    }

    @Test fun noWifiNoKeys() {
        val r = JSONObject()
        WifiKeys().put(r, -127, 0, -1)
        assertEquals(0, r.length())
    }
}
