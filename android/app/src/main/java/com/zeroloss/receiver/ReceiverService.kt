package com.zeroloss.receiver

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.Process
import android.os.SystemClock
import android.provider.Settings
import android.os.Handler
import android.os.Looper
import com.zeroloss.receiver.measure.Iperf3Client
import com.zeroloss.receiver.measure.RtpHeader
import com.zeroloss.receiver.measure.StreamAnalyzer
import com.zeroloss.receiver.play.ChannelPlayer
import com.zeroloss.receiver.play.PlaybackMonitor
import com.zeroloss.receiver.play.TsPipe
import com.zeroloss.receiver.net.ApiError
import com.zeroloss.receiver.net.ApiServer
import org.json.JSONArray
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.MulticastSocket
import java.net.NetworkInterface
import java.net.SocketTimeoutException
import kotlin.concurrent.thread

/**
 * The tablet's measuring part (05-standards 4.5): a foreground service of type
 * mediaPlayback that keeps the Wi-Fi awake (MulticastLock + low-latency Wi-Fi lock), joins
 * the channel's multicast group, measures every datagram and closes a 1-second record a
 * second. The receiving Agent reads the records over adb (ApiServer, 127.0.0.1:10995) and
 * summarizes and judges them with the same code as a PC receiver.
 *
 * Each tablet is its own receiver of one channel; up to three tablets take part in a test,
 * on the same channel or on different ones (01-overview 4), each through its own adb
 * forward on the receiving Agent. Playback (S-49 On) reads the same socket's TS.
 */
class ReceiverService : Service() {
    @Volatile private var job: Receiving? = null
    private var api: ApiServer? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private val records = ArrayDeque<Pair<Long, JSONObject>>() // (seq, record), the last hour
    private var recordSeq = 0L
    private val main = Handler(Looper.getMainLooper())
    private val monitor = PlaybackMonitor()
    /** The player of the channel now received; the screen shows it, never owns it. */
    @Volatile var channelPlayer: ChannelPlayer? = null
        private set

    override fun onBind(intent: Intent?): IBinder? = null

    /** Wi-Fi scan results this second, whoever asked for the scan (TAB-018 evidence). */
    private val scanSeen = object : android.content.BroadcastReceiver() {
        override fun onReceive(c: Context?, i: Intent?) {
            wifiScans.incrementAndGet()
        }
    }

    override fun onCreate() {
        super.onCreate()
        registerReceiver(scanSeen, android.content.IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION))
        running = this
        goForeground()
        api = ApiServer(handler = ::handle).also { it.start() }
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int = START_STICKY

    override fun onDestroy() {
        runCatching { unregisterReceiver(scanSeen) }
        stopReceiving()
        api?.stop()
        running = null
        super.onDestroy()
    }

    private fun goForeground() {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "ZeroLoss receiver", NotificationManager.IMPORTANCE_LOW))
        val n = Notification.Builder(this, CHANNEL)
            .setContentTitle("ZeroLoss receiver")
            .setContentText("Ready for a test")
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    // ---------------------------------------------------------------- API (02 3)

    private fun handle(method: String, path: String, query: Map<String, String>, params: JSONObject): JSONObject =
        when ("$method $path") {
            "GET /v1/health" -> health()
            "GET /v1/status" -> status()
            "GET /v1/records" -> records(query["after"]?.toLongOrNull() ?: 0L)
            "POST /v1/receiver/start" -> start(params)
            "POST /v1/receiver/stop" -> JSONObject().put("stopped", stopReceiving())
            "POST /v1/throughput/start" -> throughputStart(params)
            "GET /v1/throughput/status" -> throughputStatus()
            "POST /v1/throughput/stop" -> JSONObject().put("stopped", throughputCancel())
            else -> throw ApiError(404, "ZL-DSC-012", "Request rejected", "no such path: $path")
        }

    // ---------------------------------------------------------------- throughput (T-4)

    @Volatile private var iperf: Iperf3Client? = null
    @Volatile private var iperfResult: JSONObject? = null
    @Volatile private var iperfT0 = 0L

    /**
     * One direction against the WAN-side PC's iperf3 server ({server, port, seconds,
     * reverse}), in the background; the node polls throughputStatus. Over the Wi-Fi
     * network only (a tablet with mobile data must not measure that), the Wi-Fi kept awake.
     */
    private fun throughputStart(p: JSONObject): JSONObject {
        if (iperf != null) throw ApiError(409, "ZL-DSC-012", "Request rejected", "a throughput run is going on")
        val server = p.optString("server")
        if (server.isEmpty()) throw ApiError(400, "ZL-DSC-012", "Request rejected", "params.server is required")
        val wifi = wifiNetwork() ?: throw ApiError(409, "ZL-NET-002", "The tablet is not on Wi-Fi",
            "no Wi-Fi network is up on the tablet")
        val c = Iperf3Client(server, p.optInt("port", 5201), p.optInt("seconds", 10), p.optBoolean("reverse"),
            connect = { a -> wifi.socketFactory.createSocket().also { it.connect(a, Iperf3Client.CONNECT_MS) } })
        holdWifi()
        iperf = c
        iperfResult = null
        iperfT0 = SystemClock.elapsedRealtime()
        thread(name = "zl-throughput", isDaemon = true) {
            val r = c.run()
            iperfResult = JSONObject().put("ok", r.ok).put("bits_per_second", r.bitsPerSecond)
                .put("bytes", r.bytes).put("seconds", r.seconds).put("error", r.error ?: JSONObject.NULL)
            iperf = null
            if (job == null) releaseWifi()
        }
        return JSONObject().put("started", true)
    }

    private fun throughputStatus(): JSONObject {
        val c = iperf
        val s = (SystemClock.elapsedRealtime() - iperfT0) / 1000.0
        return JSONObject().put("state", if (c != null) "running" else if (iperfResult != null) "done" else "idle")
            .put("moved_bytes", c?.moved?.get() ?: JSONObject.NULL)
            .put("elapsed_s", if (c != null) s else JSONObject.NULL)
            .put("result", iperfResult ?: JSONObject.NULL)
    }

    private fun throughputCancel(): Boolean {
        val c = iperf ?: return false
        c.cancel()
        return true
    }

    private fun wifiNetwork(): android.net.Network? {
        val cm = getSystemService(android.net.ConnectivityManager::class.java)
        return cm.allNetworks.firstOrNull { n ->
            cm.getNetworkCapabilities(n)?.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) == true
        }
    }

    private fun releaseWifi() {
        wifiLock?.let { if (it.isHeld) it.release() }
        multicastLock?.let { if (it.isHeld) it.release() }
    }

    private fun health(): JSONObject = JSONObject()
        .put("app", "com.zeroloss.receiver")
        .put("build", BuildInfo.versionName(this))
        .put("device_id", Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID))
        .put("model", Build.MODEL)
        .put("android", Build.VERSION.RELEASE)
        .put("sdk", Build.VERSION.SDK_INT)
        .put("state", if (job != null) "RUNNING" else "IDLE")

    private fun status(): JSONObject {
        val j = job
        return health()
            .put("ch", j?.analyzer?.ch ?: JSONObject.NULL)
            .put("group", j?.group ?: JSONObject.NULL)
            .put("lost_total", j?.analyzer?.seq?.stats?.lost ?: JSONObject.NULL)
            .put("playback", channelPlayer != null)
            .put("in_front", inFront) // no picture behind another screen (TAB-016)
            .put("buffer_ms", j?.bufferMs ?: JSONObject.NULL)
            .put("pipe_overflow", j?.pipe?.overflowChunks?.get() ?: JSONObject.NULL)
            .put("not_rtp", j?.analyzer?.notRtp ?: JSONObject.NULL)
            .put("locks", JSONObject()
                .put("multicast", multicastLock?.isHeld == true)
                .put("wifi_low_latency", wifiLock?.isHeld == true))
    }

    private fun records(after: Long): JSONObject {
        val out = JSONArray()
        synchronized(records) {
            for ((seq, r) in records) if (seq > after) out.put(JSONObject(r.toString()).put("seq", seq))
        }
        return JSONObject().put("records", out).put("last_seq", recordSeq)
    }

    private fun start(params: JSONObject): JSONObject {
        val group = params.optString("group").ifEmpty {
            throw ApiError(400, "ZL-DSC-012", "Request rejected", "params.group is required")
        }
        val port = params.optInt("port", 5004)
        val ch = params.optInt("ch", 1)
        val play = params.optBoolean("play", true) // S-49
        val bufferMs = params.optInt("buffer_ms", DEFAULT_BUFFER_MS)
        if (bufferMs !in BUFFER_RANGE_MS) {
            throw ApiError(400, "ZL-DSC-012", "Request rejected", "buffer_ms must be 300..5000, got $bufferMs")
        }
        stopReceiving()
        // a new test: the earlier records go (2026-10-31 user: "메모리 쌓이지 않게"); the seq
        // numbers go on, so the receiving PC still takes only what comes after this start
        synchronized(records) { records.clear() }
        holdWifi()
        val j = Receiving(group, port, StreamAnalyzer(ch), if (play) TsPipe() else null, bufferMs, ::keep, monitor,
            ::wifiReading)
        job = j
        j.start()
        j.pipe?.let { pipe -> main.post { if (job === j) channelPlayer = ChannelPlayer(this, pipe, bufferMs, monitor) } }
        return JSONObject().put("receiving", true).put("group", group).put("port", port).put("ch", ch)
            .put("play", play).put("buffer_ms", bufferMs)
    }

    private fun stopReceiving(): Boolean {
        val j = job ?: return false
        job = null
        j.stop()
        main.post {
            channelPlayer?.release()
            channelPlayer = null
        }
        multicastLock?.let { if (it.isHeld) it.release() }
        wifiLock?.let { if (it.isHeld) it.release() }
        return true
    }

    /** (rssi dBm, receive link Mbps, frequency MHz) of the Wi-Fi now, null when on none. */
    @Suppress("DEPRECATION") // connectionInfo: the one reading that needs no location permission
    private fun wifiReading(): Triple<Int?, Int?, Int?>? {
        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val info = wm.connectionInfo ?: return null
        if (info.networkId == -1 && info.frequency <= 0) return null
        val rx = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) info.rxLinkSpeedMbps else info.linkSpeed
        return Triple(info.rssi, rx, info.frequency)
    }

    private fun keep(record: JSONObject) {
        synchronized(records) {
            recordSeq++
            records.addLast(recordSeq to record)
            while (records.size > KEEP_RECORDS) records.removeFirst()
        }
    }

    /** Android drops multicast without a MulticastLock; the low-latency lock keeps the
     *  Wi-Fi out of power save while the screen is on and the app in front (05 4.5). */
    private fun holdWifi() {
        val wm = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        multicastLock = (multicastLock ?: wm.createMulticastLock("zeroloss").apply { setReferenceCounted(false) })
            .also { if (!it.isHeld) it.acquire() }
        wifiLock = (wifiLock ?: wm.createWifiLock(WifiManager.WIFI_MODE_FULL_LOW_LATENCY, "zeroloss")
            .apply { setReferenceCounted(false) })
            .also { if (!it.isHeld) it.acquire() }
    }

    /** One channel: a socket thread that measures, a tick thread that closes seconds. */
    private class Receiving(
        val group: String,
        val port: Int,
        val analyzer: StreamAnalyzer,
        val pipe: TsPipe?,
        val bufferMs: Int,
        private val keep: (JSONObject) -> Unit,
        private val monitor: PlaybackMonitor,
        private val wifiNow: (() -> Triple<Int?, Int?, Int?>?)? = null,
    ) {
        private val wifi = com.zeroloss.receiver.measure.WifiKeys()
        @Volatile private var stopped = false
        private var socket: MulticastSocket? = null
        private val lock = Any()

        fun start() {
            val s = MulticastSocket(InetSocketAddress(port))
            s.receiveBufferSize = 8 shl 20
            s.soTimeout = 500
            val nif = wifiInterface()
            s.joinGroup(InetSocketAddress(InetAddress.getByName(group), port), nif)
            socket = s
            thread(name = "zl-receive", isDaemon = true) {
                Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO) // receiving first
                val buf = ByteArray(2048)
                val p = DatagramPacket(buf, buf.size)
                while (!stopped) {
                    try {
                        s.receive(p)
                    } catch (e: SocketTimeoutException) {
                        continue
                    } catch (e: Exception) {
                        if (stopped) break else continue
                    }
                    val now = SystemClock.elapsedRealtimeNanos()
                    synchronized(lock) { analyzer.onDatagram(buf, p.length, now) }
                    pipe?.let { toPlayer(it, buf, p.length) }
                }
            }
            thread(name = "zl-tick", isDaemon = true) {
                var next = SystemClock.elapsedRealtimeNanos() + NS_PER_S
                while (!stopped) {
                    val wait = (next - SystemClock.elapsedRealtimeNanos()) / 1_000_000
                    if (wait > 0) Thread.sleep(wait)
                    val r = synchronized(lock) { analyzer.second(next) }
                    wifiNow?.invoke()?.let { (rssi, mbps, freq) -> wifi.put(r, rssi, mbps, freq) }
                    val scans = wifiScans.getAndSet(0)
                    if (scans > 0) r.put("wsc", scans) // the tablet scanned: radio off channel (TAB-018)
                    if (pipe != null) monitor.second(next)?.let { pb -> pb.keys().forEach { k -> r.put(k, pb.get(k)) } }
                    keep(r)
                    next += NS_PER_S
                }
            }
        }

        fun stop() {
            stopped = true
            socket?.close()
            pipe?.close()
        }

        /** The datagram's TS payload, copied: the receive buffer is reused at once. */
        private fun toPlayer(pipe: TsPipe, buf: ByteArray, length: Int) {
            val h = RtpHeader.parse(buf, length) ?: return
            if (h.payloadType != RtpHeader.MP2T || h.payloadEnd <= h.payloadOffset) return
            pipe.put(buf.copyOfRange(h.payloadOffset, h.payloadEnd))
        }

        private fun wifiInterface(): NetworkInterface? =
            NetworkInterface.getNetworkInterfaces().toList().firstOrNull { it.name.startsWith("wlan") && it.isUp }

        companion object {
            const val NS_PER_S = 1_000_000_000L
        }
    }

    companion object {
        const val CHANNEL = "zeroloss"
        const val NOTIFICATION_ID = 1
        const val KEEP_RECORDS = 3600
        const val DEFAULT_BUFFER_MS = 1000 // S-25
        val BUFFER_RANGE_MS = 300..5000
        @Volatile var running: ReceiverService? = null
            private set
        /** The screen is in front: the picture has a surface (set by MainActivity). */
        @Volatile var inFront = false
        val wifiScans = java.util.concurrent.atomic.AtomicInteger()
    }
}

object BuildInfo {
    fun versionName(c: Context): String =
        c.packageManager.getPackageInfo(c.packageName, 0).versionName ?: "?"
}
