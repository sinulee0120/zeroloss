package com.zeroloss.receiver.measure

import org.json.JSONArray
import org.json.JSONObject
import java.io.DataInputStream
import java.io.DataOutputStream
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.charset.StandardCharsets
import java.security.SecureRandom
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/**
 * A TCP throughput run against the WAN-side PC's iperf3 server (docs/modules/throughput.md
 * T-4, 2026-10-29 user: "구현부터 다 해" — the tablets in the throughput test like every PC).
 * It speaks iperf3's own protocol (iperf_api.h), so the server is the same `iperf3 -s` the
 * PCs measure against: the 37-byte cookie, the parameters as length-prefixed JSON, one data
 * connection, the client's timer ends the test, results exchanged both ways.
 * `reverse` = the server sends and this tablet receives (iperf3 -R, WAN→LAN).
 *
 * [connect] makes each socket; the service binds it to the Wi-Fi network so a tablet with
 * mobile data still measures the Wi-Fi.
 */
class Iperf3Client(
    private val host: String,
    private val port: Int,
    private val seconds: Int,
    private val reverse: Boolean,
    private val connect: (InetSocketAddress) -> Socket = { a -> Socket().also { it.connect(a, CONNECT_MS) } },
) {
    /** Bytes moved so far by this tablet (sent, or received with reverse): the live rate. */
    val moved = AtomicLong()
    private val stop = AtomicBoolean(false)

    data class Result(val ok: Boolean, val bitsPerSecond: Double = 0.0, val bytes: Long = 0,
                      val seconds: Double = 0.0, val error: String? = null)

    fun cancel() = stop.set(true)

    fun run(): Result {
        val cookie = cookie()
        var data: Socket? = null
        try {
            connect(InetSocketAddress(host, port)).use { ctl ->
                ctl.soTimeout = (seconds + 30) * 1000
                ctl.tcpNoDelay = true
                val inp = DataInputStream(ctl.getInputStream())
                val out = DataOutputStream(ctl.getOutputStream())
                out.write(cookie)
                out.flush()
                var worker: Thread? = null
                var t0 = 0L
                var t1 = 0L
                var serverBytes = -1L
                while (true) {
                    when (val state = inp.readByte().toInt()) {
                        PARAM_EXCHANGE -> writeJson(out, params())
                        CREATE_STREAMS -> {
                            data = connect(InetSocketAddress(host, port)).also { s ->
                                s.getOutputStream().write(cookie)
                                s.getOutputStream().flush()
                            }
                        }
                        TEST_START -> Unit
                        TEST_RUNNING -> {
                            val s = data ?: return Result(false, error = "no data connection")
                            t0 = System.nanoTime()
                            worker = thread(name = "zl-iperf", isDaemon = true) {
                                try {
                                    if (reverse) drain(s.getInputStream()) else pump(s.getOutputStream())
                                } catch (_: Exception) {
                                    // the connection ends with the test
                                }
                            }
                            val end = t0 + seconds * 1_000_000_000L
                            while (System.nanoTime() < end && !stop.get()) Thread.sleep(50)
                            stop.set(true)
                            t1 = System.nanoTime()
                            if (!reverse) worker.join(2000) // the last block written before TEST_END
                            out.writeByte(TEST_END)
                            out.flush()
                        }
                        EXCHANGE_RESULTS -> {
                            val took = (t1 - t0) / 1e9
                            writeJson(out, results(moved.get(), took))
                            val theirs = readJson(inp)
                            serverBytes = theirs.optJSONArray("streams")?.optJSONObject(0)?.optLong("bytes", -1) ?: -1
                        }
                        DISPLAY_RESULTS -> {
                            out.writeByte(IPERF_DONE)
                            out.flush()
                            worker?.join(2000)
                            val took = (t1 - t0) / 1e9
                            // what arrived counts: the server's count when this tablet sent
                            val bytes = if (!reverse && serverBytes >= 0) serverBytes else moved.get()
                            if (took <= 0) return Result(false, error = "the test did not run")
                            return Result(true, bytes * 8 / took, bytes, took)
                        }
                        ACCESS_DENIED -> return Result(false, error = "the iperf3 server is busy (access denied)")
                        SERVER_ERROR -> {
                            val code = inp.readInt()
                            inp.readInt()
                            return Result(false, error = "the iperf3 server reported error $code")
                        }
                        SERVER_TERMINATE -> return Result(false, error = "the iperf3 server ended the test")
                        else -> return Result(false, error = "unexpected iperf3 state $state")
                    }
                }
            }
        } catch (e: Exception) {
            return Result(false, error = "unable to connect or measure: ${e.message ?: e.javaClass.simpleName}")
        } finally {
            stop.set(true)
            try { data?.close() } catch (_: Exception) {}
        }
    }

    private fun pump(out: OutputStream) {
        val block = ByteArray(BLOCK)
        SecureRandom().nextBytes(block)
        while (!stop.get()) {
            out.write(block)
            moved.addAndGet(BLOCK.toLong())
        }
        out.flush()
    }

    private fun drain(inp: InputStream) {
        val buf = ByteArray(BLOCK)
        while (true) {
            val n = inp.read(buf)
            if (n < 0) return
            if (!stop.get()) moved.addAndGet(n.toLong())
        }
    }

    private fun params(): JSONObject = JSONObject()
        .put("tcp", true).put("omit", 0).put("time", seconds).put("num", 0).put("blockcount", 0)
        .put("parallel", 1).put("len", BLOCK).put("pacing_timer", 1000)
        .put("client_version", "3.9").apply { if (reverse) put("reverse", true) }

    private fun results(bytes: Long, took: Double): JSONObject = JSONObject()
        .put("cpu_util_total", 0.0).put("cpu_util_user", 0.0).put("cpu_util_system", 0.0)
        .put("sender_has_retransmits", if (reverse) -1 else 0)
        .put("streams", JSONArray().put(JSONObject().put("id", 1).put("bytes", bytes)
            .put("retransmits", -1).put("jitter", 0).put("errors", 0).put("packets", 0)
            .put("start_time", 0).put("end_time", took)))

    companion object {
        const val TEST_START = 1
        const val TEST_RUNNING = 2
        const val TEST_END = 4
        const val PARAM_EXCHANGE = 9
        const val CREATE_STREAMS = 10
        const val SERVER_TERMINATE = 11
        const val EXCHANGE_RESULTS = 13
        const val DISPLAY_RESULTS = 14
        const val IPERF_DONE = 16
        const val ACCESS_DENIED = -1
        const val SERVER_ERROR = -2
        const val BLOCK = 128 * 1024
        const val CONNECT_MS = 10_000
        private const val COOKIE_CHARS = "abcdefghijklmnopqrstuvwxyz234567"

        fun cookie(): ByteArray {
            val r = SecureRandom()
            val b = ByteArray(37)
            for (i in 0 until 36) b[i] = COOKIE_CHARS[r.nextInt(COOKIE_CHARS.length)].code.toByte()
            return b // the 37th byte stays 0, as iperf3 sends it
        }

        fun writeJson(out: DataOutputStream, o: JSONObject) {
            val bytes = o.toString().toByteArray(StandardCharsets.UTF_8)
            out.writeInt(bytes.size)
            out.write(bytes)
            out.flush()
        }

        fun readJson(inp: DataInputStream): JSONObject {
            val n = inp.readInt()
            require(n in 0..(1 shl 20)) { "iperf3 results too long ($n)" }
            val b = ByteArray(n)
            inp.readFully(b)
            return JSONObject(String(b, StandardCharsets.UTF_8))
        }
    }
}
