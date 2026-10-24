package com.zeroloss.receiver

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.graphics.Color
import android.view.Gravity
import android.view.WindowManager
import android.widget.FrameLayout
import android.widget.TextView
import androidx.annotation.OptIn
import androidx.media3.common.util.UnstableApi
import androidx.media3.ui.PlayerView

/**
 * The screen: keeps itself on and in front (the low-latency Wi-Fi lock needs both, 05 4.5)
 * and shows the service's own values only (11-ui 6). The picture is the service's player
 * shown here: turning the tablet (configChanges) or leaving the screen never rebuilds it.
 */
@OptIn(UnstableApi::class)
class MainActivity : Activity() {
    private val ui = Handler(Looper.getMainLooper())
    private lateinit var text: TextView
    private lateinit var video: PlayerView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        video = PlayerView(this).apply { useController = false; setKeepContentOnPlayerReset(true) }
        text = TextView(this).apply {
            textSize = 16f
            setPadding(32, 32, 32, 32)
            setTextColor(Color.WHITE)
            setShadowLayer(4f, 0f, 0f, Color.BLACK)
        }
        val root = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        root.addView(video, FrameLayout.LayoutParams(-1, -1))
        root.addView(text, FrameLayout.LayoutParams(-2, -2, Gravity.TOP or Gravity.START))
        setContentView(root)
        startForegroundService(Intent(this, ReceiverService::class.java))
    }

    override fun onResume() {
        super.onResume()
        ReceiverService.inFront = true
        ui.post(refresh)
    }

    override fun onPause() {
        ui.removeCallbacks(refresh)
        video.player = null // the player goes on in the service; only the picture stops
        ReceiverService.inFront = false
        super.onPause()
    }

    private val refresh = object : Runnable {
        override fun run() {
            val s = ReceiverService.running
            val p = s?.channelPlayer?.player
            if (video.player !== p) video.player = p
            text.text = buildString {
                append("ZeroLoss receiver ${BuildInfo.versionName(this@MainActivity)}\n")
                append(if (s == null) "starting…" else "Ready on 127.0.0.1:10995 (adb forward)")
            }
            ui.postDelayed(this, 1000)
        }
    }
}
