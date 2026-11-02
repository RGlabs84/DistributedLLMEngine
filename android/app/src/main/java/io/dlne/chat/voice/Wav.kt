package io.dlne.chat.voice

/**
 * The only audio format the server takes (docs/voice-contract.md): WAV, PCM 16-bit, mono, 16 kHz.
 * The phone records raw PCM; this puts the 44-byte RIFF header in front of it.
 */
object Wav {
    const val SAMPLE_RATE = 16_000
    const val CHANNELS = 1
    const val BITS_PER_SAMPLE = 16
    const val BYTES_PER_SECOND = SAMPLE_RATE * CHANNELS * BITS_PER_SAMPLE / 8
    const val HEADER_SIZE = 44

    /** The server's size limit for a clip (2 MB), header included. */
    const val MAX_FILE_BYTES = 2_000_000

    /** The header for [dataBytes] bytes of little-endian PCM16 mono samples at 16 kHz. */
    fun header(dataBytes: Int): ByteArray {
        require(dataBytes >= 0) { "negative length" }
        val out = ByteArray(HEADER_SIZE)
        fun ascii(at: Int, s: String) = s.forEachIndexed { i, c -> out[at + i] = c.code.toByte() }
        fun int32(at: Int, v: Int) {
            for (i in 0 until 4) out[at + i] = (v ushr (8 * i)).toByte()
        }
        fun int16(at: Int, v: Int) {
            out[at] = v.toByte()
            out[at + 1] = (v ushr 8).toByte()
        }
        ascii(0, "RIFF")
        int32(4, 36 + dataBytes)
        ascii(8, "WAVE")
        ascii(12, "fmt ")
        int32(16, 16) // PCM fmt chunk size
        int16(20, 1) // PCM
        int16(22, CHANNELS)
        int32(24, SAMPLE_RATE)
        int32(28, BYTES_PER_SECOND)
        int16(32, CHANNELS * BITS_PER_SAMPLE / 8) // block align
        int16(34, BITS_PER_SAMPLE)
        ascii(36, "data")
        int32(40, dataBytes)
        return out
    }

    /** A complete WAV file: the header, then the first [length] bytes of [pcm]. */
    fun wrap(pcm: ByteArray, length: Int = pcm.size): ByteArray {
        require(length in 0..pcm.size) { "length out of range" }
        return header(length) + pcm.copyOf(length)
    }

    /** The most PCM bytes a clip of [maxSeconds] may hold, also kept under the server's 2 MB file limit. */
    fun maxPcmBytes(maxSeconds: Int): Int {
        val bytes = (maxSeconds.coerceAtLeast(1).toLong() * BYTES_PER_SECOND)
            .coerceAtMost((MAX_FILE_BYTES - HEADER_SIZE).toLong()).toInt()
        return bytes - bytes % 2
    }
}
