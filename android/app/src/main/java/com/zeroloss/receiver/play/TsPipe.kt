package com.zeroloss.receiver.play

import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

/**
 * The channel's MPEG-TS, from the measuring socket to the player: one socket for both, so
 * the player sees exactly the packets that were measured. The receiving thread never waits
 * here: when the player falls behind by more than the pipe holds, the oldest data goes and
 * is counted (a player-side drop, never a network loss).
 */
class TsPipe(capacityChunks: Int = 8192) {
    private val queue = ArrayBlockingQueue<ByteArray>(capacityChunks)
    @Volatile var closed = false
        private set
    val overflowChunks = AtomicLong()
    /** When data last came in (elapsedRealtime ms): the player stalls only with data here. */
    @Volatile var lastPutMs = 0L
        private set

    /** From the receiving thread: never blocks. */
    fun put(chunk: ByteArray) {
        if (closed) return
        lastPutMs = android.os.SystemClock.elapsedRealtime()
        while (!queue.offer(chunk)) {
            queue.poll()
            overflowChunks.incrementAndGet()
        }
    }

    /** From the player: the next chunk, or null after ``timeoutMs`` or when closed. */
    fun take(timeoutMs: Long): ByteArray? = if (closed && queue.isEmpty()) null else queue.poll(timeoutMs, TimeUnit.MILLISECONDS)

    /** A restart plays from what comes next, not from the data that stalled it. */
    fun clear() {
        queue.clear()
    }

    fun close() {
        closed = true
        queue.clear()
    }
}
