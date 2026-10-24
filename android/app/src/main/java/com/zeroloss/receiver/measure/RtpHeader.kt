package com.zeroloss.receiver.measure

/** RTP fixed header (RFC 3550 5.1), as core/blocks/rtp_header.py. */
data class RtpHeader(
    val seq: Int,
    val timestamp: Long,
    val ssrc: Long,
    val payloadType: Int,
    val marker: Boolean,
    val payloadOffset: Int, // after CSRCs and header extension
    val payloadEnd: Int, // before padding
) {
    companion object {
        const val VERSION = 2
        const val FIXED_LEN = 12
        const val MP2T = 33 // RFC 3551 payload type of MPEG-TS

        /** Null when the datagram is not a valid RTP packet (counted by the caller). */
        fun parse(data: ByteArray, length: Int = data.size): RtpHeader? {
            val n = length
            if (n < FIXED_LEN || (data[0].toInt() and 0xFF) shr 6 != VERSION) return null
            val b0 = data[0].toInt() and 0xFF
            val padding = b0 and 0x20 != 0
            val extension = b0 and 0x10 != 0
            var offset = FIXED_LEN + 4 * (b0 and 0x0F)
            if (extension) {
                if (n < offset + 4) return null
                offset += 4 + 4 * u16(data, offset + 2)
            }
            var end = n
            if (padding) end -= data[n - 1].toInt() and 0xFF
            if (offset > end) return null
            return RtpHeader(
                seq = u16(data, 2),
                timestamp = u32(data, 4),
                ssrc = u32(data, 8),
                payloadType = (data[1].toInt() and 0x7F),
                marker = (data[1].toInt() and 0x80) != 0,
                payloadOffset = offset,
                payloadEnd = end,
            )
        }

        private fun u16(b: ByteArray, i: Int): Int =
            ((b[i].toInt() and 0xFF) shl 8) or (b[i + 1].toInt() and 0xFF)

        private fun u32(b: ByteArray, i: Int): Long =
            ((b[i].toLong() and 0xFF) shl 24) or ((b[i + 1].toLong() and 0xFF) shl 16) or
                ((b[i + 2].toLong() and 0xFF) shl 8) or (b[i + 3].toLong() and 0xFF)
    }
}
