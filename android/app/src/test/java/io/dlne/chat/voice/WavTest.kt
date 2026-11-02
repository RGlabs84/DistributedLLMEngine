package io.dlne.chat.voice

import java.nio.ByteBuffer
import java.nio.ByteOrder
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The WAV the server takes: PCM 16-bit, mono, 16 kHz, 44-byte header, and under 2 MB. */
class WavTest {
    private fun buf(bytes: ByteArray) = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN)

    @Test
    fun `header says PCM mono 16 kHz 16 bit and the data length`() {
        val h = buf(Wav.header(32_000))
        assertEquals(44, h.capacity())
        assertEquals("RIFF", String(h.array(), 0, 4, Charsets.US_ASCII))
        assertEquals(36 + 32_000, h.getInt(4))
        assertEquals("WAVE", String(h.array(), 8, 4, Charsets.US_ASCII))
        assertEquals("fmt ", String(h.array(), 12, 4, Charsets.US_ASCII))
        assertEquals(16, h.getInt(16))
        assertEquals(1, h.getShort(20).toInt()) // PCM
        assertEquals("mono", 1, h.getShort(22).toInt())
        assertEquals("rate", 16_000, h.getInt(24))
        assertEquals("bytes per second", 32_000, h.getInt(28))
        assertEquals("block align", 2, h.getShort(32).toInt())
        assertEquals("bits", 16, h.getShort(34).toInt())
        assertEquals("data", String(h.array(), 36, 4, Charsets.US_ASCII))
        assertEquals(32_000, h.getInt(40))
    }

    @Test
    fun `wrap puts the header before exactly the samples given`() {
        val pcm = ByteArray(100) { it.toByte() }
        val wav = Wav.wrap(pcm, 60)
        assertEquals(44 + 60, wav.size)
        assertEquals(60, buf(wav).getInt(40))
        assertEquals(36 + 60, buf(wav).getInt(4))
        assertEquals(pcm.copyOf(60).toList(), wav.copyOfRange(44, wav.size).toList())
    }

    @Test
    fun `an empty clip is a valid header alone`() {
        val wav = Wav.wrap(ByteArray(0))
        assertEquals(44, wav.size)
        assertEquals(0, buf(wav).getInt(40))
    }

    @Test
    fun `sixty seconds fit the 2 MB limit and the cap is whole samples`() {
        val max = Wav.maxPcmBytes(60)
        assertEquals(1_920_000, max)
        assertTrue(Wav.wrap(ByteArray(max)).size <= Wav.MAX_FILE_BYTES)
        assertEquals(32_000 * 5, Wav.maxPcmBytes(5))
        assertTrue("never above the file limit", Wav.maxPcmBytes(600) + Wav.HEADER_SIZE <= Wav.MAX_FILE_BYTES)
        assertEquals(0, Wav.maxPcmBytes(600) % 2)
    }
}
