package io.dlne.chat.data

import java.util.Base64
import kotlin.math.floor
import kotlin.math.max
import kotlin.math.sqrt

/** A width and height in pixels. */
data class PhotoSize(val width: Int, val height: Int)

/** A photo ready to send and to keep: the JPEG and the pixels it holds. Not a data class (it holds a byte array). */
class EncodedPhoto(val jpeg: ByteArray, val width: Int, val height: Int)

/** Makes the JPEG of one photo at a size and a quality. The phone's own decoder is behind it (see [PhotoReader]). */
fun interface JpegEncoder {
    fun encode(width: Int, height: Int, quality: Int): ByteArray
}

/**
 * What a photo goes through before it is sent (contract 2.2), kept free of Android so it can be
 * checked on a computer: scaled so that width x height is at most [MAX_PIXELS] and its longest
 * side at most [MAX_SIDE], then written as a JPEG. Re-encoding drops the EXIF block, and with it
 * the GPS position. These are tighter than what the server accepts (4 MiB, 4096 px, 16.7 MP), so a
 * photo made here is never refused for its size.
 */
object PhotoEncoding {
    /** Photos in one message. */
    const val MAX_PHOTOS = 4
    const val MAX_PIXELS = 1_200_000
    const val MAX_SIDE = 2_048
    const val QUALITY = 85
    const val FALLBACK_QUALITY = 70

    /** What the server accepts for one image (decoded, so the JPEG itself). */
    const val MAX_BYTES = 4 * 1024 * 1024

    /**
     * The size to scale a [width] x [height] photo to: the same shape, never larger than the
     * original, within both limits. Rounding is always down, so a limit is never passed.
     */
    fun targetSize(width: Int, height: Int): PhotoSize {
        require(width > 0 && height > 0) { "A photo has at least one pixel each way." }
        val scale = scaleFor(width, height)
        var w = max(1, floor(width * scale).toInt())
        var h = max(1, floor(height * scale).toInt())
        // The scale is exact on paper; this makes the limits hold on the whole numbers too.
        while (w.toLong() * h > MAX_PIXELS || max(w, h) > MAX_SIDE) {
            if (w >= h) w-- else h--
        }
        return PhotoSize(w, h)
    }

    private fun scaleFor(width: Int, height: Int): Double =
        minOf(1.0, MAX_SIDE.toDouble() / max(width, height), sqrt(MAX_PIXELS.toDouble() / (width.toLong() * height)))

    /**
     * The largest power of two a decoder can skip pixels by (`inSampleSize`) while still leaving
     * at least [targetSize] pixels, so a 50-megapixel photo is never held in memory whole.
     */
    fun sampleSize(width: Int, height: Int): Int = sampleSizeFor(scaleFor(width, height))

    /** The same for any wanted scale (1.0 = as it is, 0.25 = a quarter of each side), for thumbnails too. */
    fun sampleSizeFor(scale: Double): Int {
        if (!(scale > 0.0) || scale >= 1.0) return 1
        val whole = floor(1.0 / scale).coerceAtMost(MAX_SAMPLE.toDouble()).toInt()
        return Integer.highestOneBit(max(1, whole))
    }

    private const val MAX_SAMPLE = 1 shl 16

    /**
     * Encodes a [width] x [height] photo: scaled by [targetSize], JPEG at [QUALITY]. When that is
     * over [MAX_BYTES] it is tried again at [FALLBACK_QUALITY], and then at half the size (each
     * side) again and again until it fits. [encoder] makes one attempt.
     */
    fun fit(width: Int, height: Int, encoder: JpegEncoder): EncodedPhoto {
        var size = targetSize(width, height)
        var jpeg = encoder.encode(size.width, size.height, QUALITY)
        if (jpeg.size > MAX_BYTES) {
            jpeg = encoder.encode(size.width, size.height, FALLBACK_QUALITY)
            // A 1 x 1 picture is the end of the line: the loop must stop even for an encoder that never fits.
            while (jpeg.size > MAX_BYTES && (size.width > 1 || size.height > 1)) {
                size = PhotoSize(max(1, size.width / 2), max(1, size.height / 2))
                jpeg = encoder.encode(size.width, size.height, FALLBACK_QUALITY)
            }
        }
        return EncodedPhoto(jpeg, size.width, size.height)
    }

    /** The JPEG as the `url` of an `image_url` part (contract 2.1). */
    fun dataUrl(jpeg: ByteArray): String = "data:image/jpeg;base64," + Base64.getEncoder().encodeToString(jpeg)
}

/**
 * EXIF orientation: a phone saves a portrait shot as landscape pixels and writes a number (1 to 8)
 * saying how to turn it upright. The numbers follow the EXIF specification, which says where the
 * stored picture's first row and first column end up when it is shown the right way up.
 */
object PhotoOrientation {
    const val NORMAL = 1

    /** Orientations 5 to 8 lay the picture on its side: shown upright, its width is the stored height. */
    fun swapsSides(orientation: Int): Boolean = orientation in 5..8

    /** The size of the photo once it is upright. */
    fun uprightSize(orientation: Int, width: Int, height: Int): PhotoSize =
        if (swapsSides(orientation)) PhotoSize(height, width) else PhotoSize(width, height)

    /**
     * Where each stored pixel goes: stored (x, y) of a [width] x [height] photo lands at upright
     * (X, Y) of the same photo scaled to [targetWidth] x [targetHeight]. An unknown number counts as 1.
     */
    fun transform(orientation: Int, width: Int, height: Int, targetWidth: Int, targetHeight: Int): PhotoTransform {
        val w = width.toDouble()
        val h = height.toDouble()
        val base = when (orientation) {
            2 -> PhotoTransform(-1.0, 0.0, w, 0.0, 1.0, 0.0) // mirrored left to right
            3 -> PhotoTransform(-1.0, 0.0, w, 0.0, -1.0, h) // turned 180 degrees
            4 -> PhotoTransform(1.0, 0.0, 0.0, 0.0, -1.0, h) // mirrored top to bottom
            5 -> PhotoTransform(0.0, 1.0, 0.0, 1.0, 0.0, 0.0) // transposed
            6 -> PhotoTransform(0.0, -1.0, h, 1.0, 0.0, 0.0) // turned 90 degrees clockwise
            7 -> PhotoTransform(0.0, -1.0, h, -1.0, 0.0, w) // transposed the other way
            8 -> PhotoTransform(0.0, 1.0, 0.0, -1.0, 0.0, w) // turned 90 degrees counterclockwise
            else -> PhotoTransform(1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
        }
        val upright = uprightSize(orientation, width, height)
        return base.scaled(targetWidth.toDouble() / upright.width, targetHeight.toDouble() / upright.height)
    }
}

/** X = a*x + b*y + tx and Y = c*x + d*y + ty: the six numbers of an Android `Matrix`, without Android. */
class PhotoTransform(val a: Double, val b: Double, val tx: Double, val c: Double, val d: Double, val ty: Double) {
    fun mapX(x: Double, y: Double): Double = a * x + b * y + tx
    fun mapY(x: Double, y: Double): Double = c * x + d * y + ty

    fun scaled(sx: Double, sy: Double) = PhotoTransform(a * sx, b * sx, tx * sx, c * sy, d * sy, ty * sy)

    /** In the order `Matrix.setValues` reads them (a 3 x 3 matrix, row by row). */
    fun matrixValues(): FloatArray =
        floatArrayOf(a.toFloat(), b.toFloat(), tx.toFloat(), c.toFloat(), d.toFloat(), ty.toFloat(), 0f, 0f, 1f)
}
