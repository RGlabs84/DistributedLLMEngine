package io.dlne.chat.data

import android.content.ContentResolver
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.ImageDecoder
import android.graphics.Matrix
import android.graphics.Paint
import android.net.Uri
import android.os.Build
import androidx.annotation.RequiresApi
import androidx.exifinterface.media.ExifInterface
import java.io.ByteArrayOutputStream
import java.io.IOException

/**
 * Turns a photo the person chose (from the photo picker, or the camera app's file) into the JPEG
 * that is sent and kept. The decisions (size, quality, orientation) are made by the Android-free
 * [PhotoEncoding] and [PhotoOrientation], which have unit tests; this only does the decoding and
 * drawing that need the phone's own graphics code.
 */
object PhotoReader {

    /**
     * The photo at [uri] as an upright JPEG within the limits of [PhotoEncoding], or null when this
     * phone cannot read it (not a picture, a format it does not know, no more memory). Blocks:
     * call it off the main thread.
     */
    fun read(resolver: ContentResolver, uri: Uri): EncodedPhoto? = try {
        decode(resolver, uri)?.let { decoded ->
            try {
                encode(decoded)
            } finally {
                decoded.bitmap.recycle()
            }
        }
    } catch (_: IOException) {
        null
    } catch (_: RuntimeException) { // a decoder can throw almost anything at a broken file; the app must not crash on it
        null
    } catch (_: OutOfMemoryError) {
        null
    }

    /** The decoded pixels, and the EXIF orientation still to be applied to them ([PhotoOrientation.NORMAL] when already done). */
    private class Decoded(val bitmap: Bitmap, val orientation: Int)

    private fun decode(resolver: ContentResolver, uri: Uri): Decoded? =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) decodeUpright(resolver, uri) else decodeSideways(resolver, uri)

    /**
     * Android 9 and later: ImageDecoder turns the picture upright itself (the EXIF orientation of a
     * JPEG, the rotation of a HEIC), so nothing is left for [encode] to do, and it can skip pixels
     * while decoding. A software bitmap, because a hardware one cannot be drawn on.
     */
    @RequiresApi(Build.VERSION_CODES.P)
    private fun decodeUpright(resolver: ContentResolver, uri: Uri): Decoded {
        val bitmap = ImageDecoder.decodeBitmap(ImageDecoder.createSource(resolver, uri)) { decoder, info, _ ->
            decoder.allocator = ImageDecoder.ALLOCATOR_SOFTWARE
            decoder.setTargetSampleSize(PhotoEncoding.sampleSize(info.size.width, info.size.height))
        }
        return Decoded(bitmap, PhotoOrientation.NORMAL)
    }

    /** Android 8: BitmapFactory never applies the orientation, so it is read from the EXIF block and applied in [encode]. */
    private fun decodeSideways(resolver: ContentResolver, uri: Uri): Decoded? {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, bounds) }
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
        val options = BitmapFactory.Options().apply { inSampleSize = PhotoEncoding.sampleSize(bounds.outWidth, bounds.outHeight) }
        val bitmap = resolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, options) } ?: return null
        return Decoded(bitmap, exifOrientation(resolver, uri))
    }

    // androidx's ExifInterface, not the platform's: the platform one has known security bugs on the Android
    // versions that still need this path, and a photo can come from anyone.
    private fun exifOrientation(resolver: ContentResolver, uri: Uri): Int = try {
        resolver.openInputStream(uri)?.use {
            ExifInterface(it).getAttributeInt(ExifInterface.TAG_ORIENTATION, ExifInterface.ORIENTATION_NORMAL)
        } ?: PhotoOrientation.NORMAL
    } catch (_: IOException) {
        PhotoOrientation.NORMAL
    }

    private fun encode(decoded: Decoded): EncodedPhoto {
        val bitmap = decoded.bitmap
        val upright = PhotoOrientation.uprightSize(decoded.orientation, bitmap.width, bitmap.height)
        return PhotoEncoding.fit(upright.width, upright.height) { width, height, quality ->
            jpeg(bitmap, decoded.orientation, width, height, quality)
        }
    }

    /**
     * One JPEG of [source], turned upright and scaled to [width] x [height]. It is drawn onto white
     * because a JPEG has no transparency: a see-through PNG would otherwise come out black.
     */
    private fun jpeg(source: Bitmap, orientation: Int, width: Int, height: Int, quality: Int): ByteArray {
        val canvasBitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888)
        try {
            val canvas = Canvas(canvasBitmap)
            canvas.drawColor(Color.WHITE)
            val matrix = Matrix().apply {
                setValues(PhotoOrientation.transform(orientation, source.width, source.height, width, height).matrixValues())
            }
            canvas.drawBitmap(source, matrix, Paint(Paint.FILTER_BITMAP_FLAG))
            val out = ByteArrayOutputStream()
            if (!canvasBitmap.compress(Bitmap.CompressFormat.JPEG, quality, out)) throw IOException("The photo could not be written as a JPEG.")
            return out.toByteArray()
        } finally {
            canvasBitmap.recycle()
        }
    }
}
