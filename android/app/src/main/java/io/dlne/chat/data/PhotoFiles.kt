package io.dlne.chat.data

import java.io.File
import java.io.IOException

/**
 * The camera's temporary files. The "+" menu's Camera asks the phone's camera app to write the
 * photo into one of these (through the app's FileProvider, so the app needs no camera permission);
 * the app reads it at once, keeps only the re-encoded copy (see [PhotoReader]) and deletes it. What
 * the camera wrote is the raw photo, with its EXIF and GPS position, so a shot nobody collected must
 * not linger: it is cleared when the app is opened afresh ([sweepCameraDir]) and before the next shot.
 */
object PhotoFiles {
    /** A folder in the app's cache. The same name is in res/xml/file_paths.xml, which is what lets the camera app write there. */
    const val CAMERA_DIR = "camera"

    /** A shot nobody collected (the app was closed while the camera was open) is cleared out after this long. */
    private const val STALE_MS = 60L * 60 * 1000

    /** At start a shot this young may still be on its way back from the camera app, and is left alone. */
    private const val STARTUP_GRACE_MS = 2L * 60 * 1000

    /**
     * A new, empty file in [cacheDir] for the camera app to write into. Leftovers of earlier shots
     * are removed first, so a photo never stays in the cache for long. Fails with [IOException]
     * when the phone cannot make the file.
     */
    fun newCameraFile(cacheDir: File, now: Long = System.currentTimeMillis()): File {
        val dir = File(cacheDir, CAMERA_DIR)
        dir.mkdirs()
        dir.listFiles()?.forEach { if (now - it.lastModified() > STALE_MS) it.delete() }
        return File.createTempFile("shot-", ".jpg", dir)
    }

    /**
     * Removes the shots a camera left behind, for when the app is opened afresh (nothing is waiting for
     * a camera then: a camera answer after Android restored the app comes with the app's saved state, and
     * that is not a fresh start). Only shots older than [STARTUP_GRACE_MS] go, so one still being handed
     * back to another window of the app is never taken. Returns how many it removed.
     */
    fun sweepCameraDir(cacheDir: File, now: Long = System.currentTimeMillis()): Int {
        var removed = 0
        for (file in File(cacheDir, CAMERA_DIR).listFiles().orEmpty()) {
            if (now - file.lastModified() >= STARTUP_GRACE_MS && file.delete()) removed++
        }
        return removed
    }
}
