package com.zeroloss.receiver.net

import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.charset.StandardCharsets
import kotlin.concurrent.thread

/**
 * The app's API on 127.0.0.1:10995 (docs/02-data.md 1: the receiving Agent reaches it with
 * `adb forward tcp:… tcp:10995`). The same envelope as every ZeroLoss API (02 3):
 * {"ok": true, "data": …} or {"ok": false, "error": {"code", "what", "why", "fix"}}.
 * Commands are POST with {"command_id", "generation", "params"}, reads are GET with a query.
 * Plain sockets, one thread per connection: a handful of requests a second at most.
 */
class ApiServer(
    private val port: Int = PORT,
    private val handler: (method: String, path: String, query: Map<String, String>, params: JSONObject) -> JSONObject,
) {
    private var server: ServerSocket? = null

    fun start() {
        val s = ServerSocket(port, 16, InetAddress.getByName("127.0.0.1"))
        s.reuseAddress = true
        server = s
        thread(name = "zl-api", isDaemon = true) {
            while (!s.isClosed) {
                val c = try { s.accept() } catch (e: Exception) { break }
                thread(name = "zl-api-conn", isDaemon = true) { serve(c) }
            }
        }
    }

    fun stop() {
        server?.close()
        server = null
    }

    private fun serve(c: Socket) = c.use {
        c.soTimeout = 15_000
        val input = BufferedInputStream(c.getInputStream())
        val out = c.getOutputStream()
        while (true) { // keep-alive: one request after another
            val line = readLine(input) ?: return
            if (line.isEmpty()) continue
            val parts = line.split(" ")
            if (parts.size < 2) return
            val method = parts[0]
            val target = parts[1]
            var length = 0
            var close = false
            while (true) {
                val h = readLine(input) ?: return
                if (h.isEmpty()) break
                val i = h.indexOf(':')
                if (i <= 0) continue
                val name = h.substring(0, i).trim().lowercase()
                val value = h.substring(i + 1).trim()
                if (name == "content-length") length = value.toIntOrNull() ?: 0
                if (name == "connection" && value.equals("close", true)) close = true
            }
            val body = ByteArray(length)
            var got = 0
            while (got < length) {
                val n = input.read(body, got, length - got)
                if (n < 0) return
                got += n
            }
            val (status, answer) = answer(method, target, body)
            val bytes = answer.toString().toByteArray(StandardCharsets.UTF_8)
            val head = "HTTP/1.1 $status ${if (status == 200) "OK" else "Error"}\r\n" +
                "Content-Type: application/json\r\nContent-Length: ${bytes.size}\r\n" +
                (if (close) "Connection: close\r\n" else "") + "\r\n"
            out.write(head.toByteArray(StandardCharsets.US_ASCII))
            out.write(bytes)
            out.flush()
            if (close) return
        }
    }

    private fun answer(method: String, target: String, body: ByteArray): Pair<Int, JSONObject> {
        val path = target.substringBefore('?')
        val query = target.substringAfter('?', "").split('&').filter { it.contains('=') }
            .associate { it.substringBefore('=') to java.net.URLDecoder.decode(it.substringAfter('='), "UTF-8") }
        return try {
            val params = if (method == "POST" && body.isNotEmpty()) {
                JSONObject(String(body, StandardCharsets.UTF_8)).optJSONObject("params") ?: JSONObject()
            } else {
                JSONObject()
            }
            200 to JSONObject().put("ok", true).put("data", handler(method, path, query, params))
        } catch (e: ApiError) {
            e.status to fail(e.code, e.what, e.why, e.fix)
        } catch (e: Exception) {
            500 to fail("ZL-SYS-008", "Tablet app internal error", e.toString(), "")
        }
    }

    private fun fail(code: String, what: String, why: String, fix: String): JSONObject =
        JSONObject().put("ok", false).put(
            "error",
            JSONObject().put("code", code).put("what", what).put("why", why).put("fix", fix),
        )

    private fun readLine(input: InputStream): String? {
        val buf = ByteArrayOutputStream()
        while (true) {
            val b = input.read()
            if (b < 0) return if (buf.size() == 0) null else buf.toString("US-ASCII")
            if (b == '\n'.code) return buf.toString("US-ASCII").trimEnd('\r')
            buf.write(b)
            if (buf.size() > 8192) return null
        }
    }

    companion object {
        const val PORT = 10995
    }
}

class ApiError(val status: Int, val code: String, val what: String, val why: String = "", val fix: String = "") :
    Exception(what)
