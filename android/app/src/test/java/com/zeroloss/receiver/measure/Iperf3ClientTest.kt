package com.zeroloss.receiver.measure

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Test
import java.io.File
import java.net.ServerSocket

/**
 * The tablet's iperf3 client against a real `iperf3 -s` on this PC (the repo's Windows
 * iperf3, or one on PATH): both directions measure, the bytes move, the server agrees.
 * Skipped where no iperf3 is found.
 */
class Iperf3ClientTest {
    private fun iperf3(): String? {
        val vendored = File("../../vendor/iperf3-win/iperf3.exe")
        if (System.getProperty("os.name").lowercase().contains("win") && vendored.isFile) return vendored.path
        return System.getenv("PATH")?.split(File.pathSeparator)?.map { File(it, "iperf3") }
            ?.firstOrNull { it.canExecute() }?.path
    }

    private fun freePort(): Int = ServerSocket(0).use { it.localPort }

    private fun measure(reverse: Boolean): Iperf3Client.Result {
        val bin = iperf3()
        assumeTrue("no iperf3 here", bin != null)
        val port = freePort()
        val server = ProcessBuilder(bin, "-s", "-1", "-p", port.toString()).redirectErrorStream(true).start()
        try {
            Thread.sleep(800) // the server listens
            return Iperf3Client("127.0.0.1", port, 2, reverse).run()
        } finally {
            server.destroy()
        }
    }

    @Test fun sendsToTheServer() {
        val r = measure(reverse = false)
        assertTrue(r.error ?: "", r.ok)
        assertTrue(r.bytes > 1_000_000 && r.bitsPerSecond > 1e7)
        assertTrue(r.seconds in 1.5..3.5)
    }

    @Test fun receivesFromTheServerWithReverse() {
        val r = measure(reverse = true)
        assertTrue(r.error ?: "", r.ok)
        assertTrue(r.bytes > 1_000_000 && r.bitsPerSecond > 1e7)
    }

    @Test fun noServerSaysWhy() {
        val r = Iperf3Client("127.0.0.1", freePort(), 1, false).run()
        assertFalse(r.ok)
        assertTrue(r.error!!.startsWith("unable to connect"))
    }

    @Test fun theCookieIsIperf3s() {
        val c = Iperf3Client.cookie()
        assertEquals(37, c.size)
        assertEquals(0, c[36].toInt())
    }
}
