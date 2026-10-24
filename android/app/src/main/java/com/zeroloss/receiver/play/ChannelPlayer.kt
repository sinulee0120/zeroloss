package com.zeroloss.receiver.play

import android.content.Context
import android.net.Uri
import android.os.Handler
import android.os.Looper
import androidx.annotation.OptIn
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.DefaultLoadControl
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.ProgressiveMediaSource
import androidx.media3.extractor.DefaultExtractorsFactory
import androidx.media3.extractor.ts.DefaultTsPayloadReaderFactory

/**
 * Plays the channel the service measures (iptv 3.5): the TS comes from the measuring socket
 * through [TsPipe], so playback never opens a second socket. Owned by the service, not the
 * screen: turning or resizing the tablet only moves the picture, the player goes on.
 *
 * Fixed buffer (per-receiver buffer_ms, else S-25), no live speed control: a progressive
 * endless stream has no live configuration, so Media3 never speeds up or slows down. The
 * player's reports go to [monitor]; an error prepares the player again (prs).
 */
@OptIn(UnstableApi::class)
class ChannelPlayer(context: Context, private val pipe: TsPipe, bufferMs: Int, val monitor: PlaybackMonitor) {
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var released = false
    val player: ExoPlayer

    /**
     * 2026-10-24 run: after a loss burst the picture stayed frozen for minutes while clean
     * packets kept coming (the decoder waited for references that never came). No frame
     * for [STALL_RESTART_NS] with data still arriving -> the player starts again from the
     * newest data, at most every [RESTART_GAP_MS]; the second says prs (iptv 3.1).
     */
    private val watch = object : Runnable {
        private var lastRestartMs = 0L
        override fun run() {
            if (released) return
            val now = android.os.SystemClock.elapsedRealtime()
            val age = monitor.frameAgeNs(android.os.SystemClock.elapsedRealtimeNanos())
            val dataComing = now - pipe.lastPutMs < DATA_FRESH_MS
            if (age != null && age > STALL_RESTART_NS && dataComing && now - lastRestartMs > RESTART_GAP_MS) {
                lastRestartMs = now
                monitor.markRestarted()
                player.stop()
                pipe.clear()
                restart()
            }
            main.postDelayed(this, WATCH_EVERY_MS)
        }
    }

    init {
        val load = DefaultLoadControl.Builder()
            .setBufferDurationsMs(bufferMs, bufferMs + MAX_EXTRA_MS, bufferMs, bufferMs)
            .setPrioritizeTimeOverSizeThresholds(true)
            .build()
        val extractors = DefaultExtractorsFactory()
            .setTsExtractorFlags(DefaultTsPayloadReaderFactory.FLAG_ALLOW_NON_IDR_KEYFRAMES)
        val source = ProgressiveMediaSource.Factory({ PipeDataSource(pipe) }, extractors)
            .createMediaSource(MediaItem.fromUri(Uri.parse("zeroloss://channel")))
        player = ExoPlayer.Builder(context).setLoadControl(load).build()
        player.addAnalyticsListener(monitor)
        player.setVideoFrameMetadataListener { _, _, _, _ -> monitor.onFrame() }
        player.addListener(object : Player.Listener {
            override fun onPlayerError(error: PlaybackException) {
                main.postDelayed({ if (!released) restart() }, RESTART_DELAY_MS)
            }
        })
        player.setMediaSource(source)
        player.playWhenReady = true
        monitor.reset()
        monitor.active = true
        player.prepare()
        main.postDelayed(watch, WATCH_EVERY_MS)
    }

    private fun restart() {
        player.prepare() // the same endless source: reading goes on from the pipe's newest data
        player.playWhenReady = true
    }

    fun release() {
        released = true
        monitor.active = false
        pipe.close()
        main.post { player.release() }
    }

    companion object {
        const val MAX_EXTRA_MS = 1000 // room above the fixed buffer before loading pauses
        const val RESTART_DELAY_MS = 500L
        const val WATCH_EVERY_MS = 1000L
        const val STALL_RESTART_NS = 4_000_000_000L // a frozen picture for 4 s with data coming
        const val DATA_FRESH_MS = 2000L
        const val RESTART_GAP_MS = 10_000L
    }
}
