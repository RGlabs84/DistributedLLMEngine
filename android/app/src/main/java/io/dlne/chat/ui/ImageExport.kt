package io.dlne.chat.ui

import android.content.ClipData
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.MediaStore
import androidx.core.content.FileProvider
import io.dlne.chat.R
import java.io.File
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Saving into Pictures/DLNE through MediaStore needs no storage permission, but only from Android 10 on. */
val canSaveToPictures: Boolean get() = Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q

/** Copies [file] into Pictures/DLNE. True when it is saved; call it off the main thread. */
fun saveToPictures(context: Context, file: File): Boolean {
    if (!canSaveToPictures) return false
    val resolver = context.contentResolver
    val stamp = SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(Date())
    val values = ContentValues().apply {
        put(MediaStore.Images.Media.DISPLAY_NAME, "DLNE-$stamp.png")
        put(MediaStore.Images.Media.MIME_TYPE, "image/png")
        put(MediaStore.Images.Media.RELATIVE_PATH, "Pictures/DLNE")
        put(MediaStore.Images.Media.IS_PENDING, 1)
    }
    val uri = resolver.insert(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, values) ?: return false
    return try {
        val out = resolver.openOutputStream(uri) ?: throw IOException("no output stream")
        out.use { file.inputStream().use { input -> input.copyTo(it) } }
        val done = ContentValues().apply { put(MediaStore.Images.Media.IS_PENDING, 0) }
        resolver.update(uri, done, null, null)
        true
    } catch (_: IOException) {
        resolver.delete(uri, null, null) // no half-written picture left in the gallery
        false
    } catch (_: SecurityException) {
        resolver.delete(uri, null, null)
        false
    }
}

/** Opens the share sheet for [file]; the picture is handed over through a FileProvider, read-only. */
fun shareImage(context: Context, file: File) {
    val uri = FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
    val send = Intent(Intent.ACTION_SEND).apply {
        type = "image/png"
        putExtra(Intent.EXTRA_STREAM, uri)
        clipData = ClipData.newRawUri(null, uri)
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }
    context.startActivity(Intent.createChooser(send, context.getString(R.string.image_share_title)))
}
