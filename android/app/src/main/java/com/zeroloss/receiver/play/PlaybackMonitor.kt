package com.zeroloss.receiver.play

import android.os.SystemClock
import androidx.annotation.OptIn
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.common.util.UnstableApi
import androidx.media3.exoplayer.analytics.AnalyticsListener
import org.json.JSONObject

/**
 * The player's own reports, closed once a second into the PC's keys (02 7, iptv 3.1/3.5):
 * pb = the worst state of the second (none > restart > freeze > glitch > drop > audio_gap
 * > buffering > ok), pdr = frames dropped and pau = audio underruns (written even when 0,
 * 02 7), pfs = a freeze started (no new frame for 0.5 s, iptv 3.6), prs = the player was
 * started again after an error, pde = decoder errors.
 */
@OptIn(UnstableApi::class)
class PlaybackMonitor : AnalyticsListener {
    private val lock = Any()
    private var dropped = 0L
    private var decoderErrors = 0L
    private var audioGaps = 0L
    private var restarted = false
    private var buffering = false
    private var bufferingThisSecond = false
    private var freezeStarted = false
    private var frozenThisSecond = false
    @Volatile private var lastFrameNs: Long? = null
    @Volatile var firstFrameNs: Long? = null
        private set
    @Volatile var active = false

    /** A new channel or a new player: the first frame is waited for again. */
    fun reset() {
        synchronized(lock) {
            dropped = 0; decoderErrors = 0; audioGaps = 0
            restarted = false; buffering = false; bufferingThisSecond = false
            freezeStarted = false; frozenThisSecond = false
        }
        lastFrameNs = null
        firstFrameNs = null
    }

    /** How long no frame has been shown (ns), null before the first one. */
    fun frameAgeNs(nowNs: Long): Long? = lastFrameNs?.let { nowNs - it }

    /** The player was started again by the stall watch (prs, like an error restart). */
    fun markRestarted() {
        synchronized(lock) { restarted = true }
        lastFrameNs = SystemClock.elapsedRealtimeNanos() // the new start's own wait begins now
    }

    /** Each rendered frame (VideoFrameMetadataListener in the activity). */
    fun onFrame() {
        val now = SystemClock.elapsedRealtimeNanos()
        if (firstFrameNs == null) firstFrameNs = now
        lastFrameNs = now
    }

    override fun onDroppedVideoFrames(eventTime: AnalyticsListener.EventTime, droppedFrames: Int, elapsedMs: Long) {
        synchronized(lock) { dropped += droppedFrames }
    }

    override fun onVideoCodecError(eventTime: AnalyticsListener.EventTime, videoCodecError: Exception) {
        synchronized(lock) { decoderErrors++ }
    }

    override fun onAudioUnderrun(eventTime: AnalyticsListener.EventTime, bufferSize: Int, bufferSizeMs: Long, elapsedSinceLastFeedMs: Long) {
        synchronized(lock) { audioGaps++ }
    }

    override fun onPlaybackStateChanged(eventTime: AnalyticsListener.EventTime, state: Int) {
        synchronized(lock) {
            buffering = state == Player.STATE_BUFFERING
            if (buffering) bufferingThisSecond = true
        }
    }

    override fun onPlayerError(eventTime: AnalyticsListener.EventTime, error: PlaybackException) {
        synchronized(lock) { restarted = true } // the activity prepares the player again
    }

    /** The playback keys of the second that ends now; null when nothing plays. */
    fun second(nowNs: Long): JSONObject? {
        if (!active) return null
        val r = JSONObject()
        synchronized(lock) {
            val last = lastFrameNs
            val stalled = last != null && nowNs - last > FREEZE_NS
            if (stalled && !frozenThisSecond) {
                freezeStarted = true
                frozenThisSecond = true
            } else if (!stalled) {
                frozenThisSecond = false
            }
            val pb = when {
                last == null && firstFrameNs == null -> "none"
                restarted -> "restart"
                stalled -> "freeze"
                decoderErrors > 0 -> "glitch"
                dropped > 0 -> "drop"
                audioGaps > 0 -> "audio_gap"
                bufferingThisSecond || buffering -> "buffering"
                else -> "ok"
            }
            r.put("pb", pb)
            if (pb != "none") { // nothing plays: not measured, so no counts (02 7)
                r.put("pdr", dropped)
                r.put("pau", audioGaps)
            }
            if (freezeStarted) r.put("pfs", 1)
            if (restarted) r.put("prs", 1)
            if (decoderErrors > 0) r.put("pde", decoderErrors)
            dropped = 0
            decoderErrors = 0
            audioGaps = 0
            restarted = false
            freezeStarted = false
            bufferingThisSecond = false
        }
        return r
    }

    companion object {
        const val FREEZE_NS = 500_000_000L // iptv 3.6: no new frame for 0.5 s
    }
}
