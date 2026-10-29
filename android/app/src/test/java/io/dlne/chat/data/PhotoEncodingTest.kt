package io.dlne.chat.data

import java.util.Base64
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The size, quality and byte limits a photo is held to before it is sent (contract 2.2). */
class PhotoEncodingTest {
    private fun area(size: PhotoSize) = size.width.toLong() * size.height

    // ------------------------------------------------------------------ scaling

    @Test
    fun `a photo already within both limits is left as it is`() {
        assertEquals(PhotoSize(800, 600), PhotoEncoding.targetSize(800, 600))
        assertEquals(PhotoSize(1, 1), PhotoEncoding.targetSize(1, 1))
        assertEquals("exactly 1,200,000 pixels", PhotoSize(1500, 800), PhotoEncoding.targetSize(1500, 800))
        assertEquals("exactly 2,048 on the longest side, 1,198,080 pixels", PhotoSize(2048, 585), PhotoEncoding.targetSize(2048, 585))
    }

    @Test
    fun `a camera photo is scaled to at most 1,200,000 pixels and keeps its shape`() {
        assertEquals(PhotoSize(1264, 948), PhotoEncoding.targetSize(4000, 3000))
        assertEquals("portrait", PhotoSize(948, 1264), PhotoEncoding.targetSize(3000, 4000))
        assertEquals(PhotoSize(1264, 948), PhotoEncoding.targetSize(4032, 3024))
        assertEquals(PhotoSize(1341, 894), PhotoEncoding.targetSize(6000, 4000))
        assertEquals("a screenshot", PhotoSize(821, 1460), PhotoEncoding.targetSize(1080, 1920))
        assertEquals("exactly half of each side", PhotoSize(1200, 1000), PhotoEncoding.targetSize(2400, 2000))
    }

    @Test
    fun `a wide photo is held to 2,048 pixels on its longest side`() {
        assertEquals(PhotoSize(2048, 256), PhotoEncoding.targetSize(8000, 1000))
        assertEquals(PhotoSize(256, 2048), PhotoEncoding.targetSize(1000, 8000))
        assertEquals("one pixel over", PhotoSize(2048, 584), PhotoEncoding.targetSize(2049, 585))
    }

    @Test
    fun `a sliver keeps at least one pixel each way`() {
        assertEquals(PhotoSize(1, 2048), PhotoEncoding.targetSize(1, 10_000))
        assertEquals(PhotoSize(2048, 1), PhotoEncoding.targetSize(10_000, 1))
    }

    @Test
    fun `the limits hold for any size and a photo is never made larger`() {
        val sides = listOf(1, 2, 3, 17, 100, 640, 1000, 1199, 1200, 1201, 2047, 2048, 2049, 3000, 4032, 4096, 8000, 12_000)
        for (w in sides) for (h in sides) {
            val size = PhotoEncoding.targetSize(w, h)
            val label = "${w}x$h -> ${size.width}x${size.height}"
            assertTrue(label, area(size) <= PhotoEncoding.MAX_PIXELS)
            assertTrue(label, maxOf(size.width, size.height) <= PhotoEncoding.MAX_SIDE)
            assertTrue(label, size.width in 1..w && size.height in 1..h)
        }
    }

    @Test
    fun `the limits are tighter than what the server accepts`() {
        // engine/media.py: 4 MiB each, 4096 px a side, 16,777,216 pixels, 4 images.
        assertEquals(4 * 1024 * 1024, PhotoEncoding.MAX_BYTES)
        assertTrue(PhotoEncoding.MAX_SIDE <= 4096)
        assertTrue(PhotoEncoding.MAX_PIXELS <= 4096 * 4096)
        assertEquals(4, PhotoEncoding.MAX_PHOTOS)
    }

    // ------------------------------------------------------------------ decoding with pixels skipped

    @Test
    fun `a decoder skips pixels in powers of two`() {
        assertEquals("nothing to skip", 1, PhotoEncoding.sampleSize(800, 600))
        assertEquals(2, PhotoEncoding.sampleSize(4000, 3000))
        assertEquals(2, PhotoEncoding.sampleSize(8000, 1000))
        assertEquals(8, PhotoEncoding.sampleSize(12_000, 9000))
    }

    @Test
    fun `skipping pixels never leaves less than the final size`() {
        val sides = listOf(1, 2, 7, 100, 1000, 2047, 2049, 3000, 4000, 4032, 6000, 9248, 12_000)
        for (w in sides) for (h in sides) {
            val n = PhotoEncoding.sampleSize(w, h)
            val size = PhotoEncoding.targetSize(w, h)
            assertEquals("a power of two for ${w}x$h", 1, Integer.bitCount(n))
            // Decoders round the skipped size up, which is what keeps a one-pixel-wide sliver alive.
            assertTrue("${w}x$h at 1/$n is narrower than ${size.width}", (w + n - 1) / n >= size.width)
            assertTrue("${w}x$h at 1/$n is shorter than ${size.height}", (h + n - 1) / n >= size.height)
        }
    }

    @Test
    fun `a sample size for any wanted scale, thumbnails included`() {
        assertEquals(1, PhotoEncoding.sampleSizeFor(1.0))
        assertEquals(1, PhotoEncoding.sampleSizeFor(2.5))
        assertEquals(2, PhotoEncoding.sampleSizeFor(0.5))
        assertEquals(2, PhotoEncoding.sampleSizeFor(0.3))
        assertEquals(4, PhotoEncoding.sampleSizeFor(0.25))
        assertEquals(4, PhotoEncoding.sampleSizeFor(0.13))
        assertEquals(8, PhotoEncoding.sampleSizeFor(0.125))
    }

    @Test
    fun `a nonsense scale means no skipping`() {
        assertEquals(1, PhotoEncoding.sampleSizeFor(0.0))
        assertEquals(1, PhotoEncoding.sampleSizeFor(-1.0))
        assertEquals(1, PhotoEncoding.sampleSizeFor(Double.NaN))
    }

    // ------------------------------------------------------------------ JPEG quality, then size

    /** Records each attempt and answers with a size of its own choosing. */
    private class FakeEncoder(private val bytesFor: (width: Int, height: Int, quality: Int) -> Int) : JpegEncoder {
        val attempts = ArrayList<Triple<Int, Int, Int>>() // width, height, quality

        override fun encode(width: Int, height: Int, quality: Int): ByteArray {
            attempts += Triple(width, height, quality)
            return ByteArray(bytesFor(width, height, quality))
        }
    }

    @Test
    fun `a photo that fits is encoded once, at quality 85, at the scaled size`() {
        val encoder = FakeEncoder { _, _, _ -> 300_000 }
        val photo = PhotoEncoding.fit(4000, 3000, encoder)
        assertEquals(listOf(Triple(1264, 948, 85)), encoder.attempts)
        assertEquals(1264, photo.width)
        assertEquals(948, photo.height)
        assertEquals(300_000, photo.jpeg.size)
    }

    @Test
    fun `exactly 4 MiB is accepted, only more than that is over`() {
        val encoder = FakeEncoder { _, _, _ -> PhotoEncoding.MAX_BYTES }
        PhotoEncoding.fit(4000, 3000, encoder)
        assertEquals(1, encoder.attempts.size)
    }

    @Test
    fun `over 4 MiB it is tried again at quality 70 before the size is touched`() {
        val encoder = FakeEncoder { _, _, quality -> if (quality == 85) PhotoEncoding.MAX_BYTES + 1 else 3_000_000 }
        val photo = PhotoEncoding.fit(4000, 3000, encoder)
        assertEquals(listOf(Triple(1264, 948, 85), Triple(1264, 948, 70)), encoder.attempts)
        assertEquals(PhotoSize(1264, 948), PhotoSize(photo.width, photo.height))
    }

    @Test
    fun `still over at quality 70 the size is halved, each side, until it fits`() {
        // 40 bytes a pixel: 1264x948 and 632x474 are over 4 MiB, 316x237 (3.0 MB) is not.
        val encoder = FakeEncoder { w, h, _ -> w * h * 40 }
        val photo = PhotoEncoding.fit(4000, 3000, encoder)
        assertEquals(
            listOf(Triple(1264, 948, 85), Triple(1264, 948, 70), Triple(632, 474, 70), Triple(316, 237, 70)),
            encoder.attempts,
        )
        assertEquals(PhotoSize(316, 237), PhotoSize(photo.width, photo.height))
        assertTrue(photo.jpeg.size <= PhotoEncoding.MAX_BYTES)
    }

    @Test
    fun `an encoder that never fits ends at one pixel instead of looping for ever`() {
        val encoder = FakeEncoder { _, _, _ -> PhotoEncoding.MAX_BYTES + 1 }
        val photo = PhotoEncoding.fit(4000, 3000, encoder)
        assertEquals(PhotoSize(1, 1), PhotoSize(photo.width, photo.height))
        assertTrue("took ${encoder.attempts.size} attempts", encoder.attempts.size < 20)
    }

    // ------------------------------------------------------------------ what goes on the wire

    @Test
    fun `the data URL is a base64 JPEG the server reads back byte for byte`() {
        val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 1, 2, 3, 0xFF.toByte(), 0xD9.toByte())
        val url = PhotoEncoding.dataUrl(jpeg)
        assertTrue(url.startsWith("data:image/jpeg;base64,"))
        assertArrayEquals(jpeg, Base64.getDecoder().decode(url.removePrefix("data:image/jpeg;base64,")))
        assertFalse("one line, no wrapping", url.contains('\n') || url.contains('\r'))
    }
}
