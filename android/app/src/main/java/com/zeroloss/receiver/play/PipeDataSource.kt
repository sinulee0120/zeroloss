package com.zeroloss.receiver.play

import android.net.Uri
import androidx.annotation.OptIn
import androidx.media3.common.C
import androidx.media3.common.util.UnstableApi
import androidx.media3.datasource.BaseDataSource
import androidx.media3.datasource.DataSpec

/** Media3 reads the channel's TS from the pipe as one endless stream. */
@OptIn(UnstableApi::class)
class PipeDataSource(private val pipe: TsPipe) : BaseDataSource(/* isNetwork = */ true) {
    private var uri: Uri? = null
    private var chunk: ByteArray? = null
    private var pos = 0

    override fun open(dataSpec: DataSpec): Long {
        uri = dataSpec.uri
        transferInitializing(dataSpec)
        transferStarted(dataSpec)
        return C.LENGTH_UNSET.toLong()
    }

    override fun read(buffer: ByteArray, offset: Int, length: Int): Int {
        if (length == 0) return 0
        var c = chunk
        while (c == null || pos >= c.size) {
            if (pipe.closed) return C.RESULT_END_OF_INPUT
            c = pipe.take(100) // the loader thread waits; the receiving thread never does
            pos = 0
            chunk = c
        }
        val n = minOf(length, c.size - pos)
        System.arraycopy(c, pos, buffer, offset, n)
        pos += n
        bytesTransferred(n)
        return n
    }

    override fun getUri(): Uri? = uri

    override fun close() {
        if (uri != null) {
            uri = null
            transferEnded()
        }
    }
}
