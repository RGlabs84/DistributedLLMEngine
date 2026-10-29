package io.dlne.chat.data

import java.io.File
import java.io.IOException
import java.util.UUID

/**
 * Generated images, and the photos people attach, kept in the app's private storage next to the
 * chat that shows them. Nothing is written to shared storage here (Save does that, on request);
 * files are named by a random id, so a name says nothing about the prompt or the picture, and are
 * removed with their chat. [writeFile] puts the bytes in a file (a seam for tests; it may throw
 * [IOException]).
 */
class ImageStore(
    private val dir: File,
    private val writeFile: (File, ByteArray) -> Unit = { file, bytes -> file.writeBytes(bytes) },
) {

    /** Writes [png] and returns its file name, or null when the phone could not store it. */
    fun save(png: ByteArray): String? = write(png, "png")

    /** Writes the JPEG of a photo the person attached (see [PhotoEncoding]); the name, or null when it could not be stored. */
    fun savePhoto(jpeg: ByteArray): String? = write(jpeg, "jpg")

    /** The stored photo as the `data:` URL the server takes, or null when it is gone or cannot be read. */
    fun photoDataUrl(photo: Photo): String? {
        val stored = file(photo.file) ?: return null
        return try {
            PhotoEncoding.dataUrl(stored.readBytes())
        } catch (_: IOException) {
            null
        }
    }

    private fun write(bytes: ByteArray, extension: String): String? {
        val name = "${UUID.randomUUID()}.$extension"
        val target = File(dir, name)
        val temp = File(dir, "$name.tmp")
        return try {
            dir.mkdirs()
            writeFile(temp, bytes)
            if (temp.renameTo(target)) name else null
        } catch (_: IOException) {
            null
        } finally {
            // Moved away on success; after a failed write (a full disk) or rename the half-written file must not stay.
            if (temp.exists()) temp.delete()
        }
    }

    /** The stored file, or null for a name that is not one of ours (never a path) or is gone. */
    fun file(name: String?): File? {
        if (name == null || !NAME.matches(name)) return null
        return File(dir, name).takeIf { it.isFile }
    }

    fun delete(names: Collection<String?>) {
        for (name in names) file(name)?.delete()
    }

    /**
     * Removes the files of this store that no chat refers to: a photo or picture whose message was never
     * saved (the app was closed in between), whose chat went while it was being written, or a half-written
     * ".tmp" a failed write left. Run once when the app starts, with the history [conversations] it has read.
     *
     * Careful by construction: nothing at all when the history could not be read in full ([historyComplete]
     * false), since a chat that failed to load still refers to its files; only names this store makes,
     * never anything else in the folder; and only files not written in the last [graceMs], so what a
     * running job or a send has just stored and not yet put into its chat is never taken. Returns how many
     * files it removed.
     */
    fun sweep(
        conversations: List<Conversation>,
        historyComplete: Boolean,
        now: Long = System.currentTimeMillis(),
        graceMs: Long = SWEEP_GRACE_MS,
    ): Int {
        if (!historyComplete) return 0
        val referenced = conversations.flatMapTo(HashSet()) { it.imageFiles() }
        var removed = 0
        for (file in dir.listFiles().orEmpty()) {
            val ours = NAME.matches(file.name) || TEMP.matches(file.name)
            if (!ours || !file.isFile || file.name in referenced || now - file.lastModified() < graceMs) continue
            if (file.delete()) removed++
        }
        return removed
    }

    companion object {
        /** A file this young is left alone by [sweep]: its chat may not have it yet. */
        const val SWEEP_GRACE_MS = 10L * 60 * 1000

        private const val UUID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"

        // png: a generated image; jpg: a photo the person attached.
        private val NAME = Regex("$UUID_PATTERN\\.(?:png|jpg)")

        // What [write] writes before it moves the file into place.
        private val TEMP = Regex("$UUID_PATTERN\\.(?:png|jpg)\\.tmp")
    }
}

/**
 * The private files a conversation refers to: the images it generated (Create image results and pictures
 * made inside replies) and the photos attached to its messages. Deleting the chat deletes these.
 */
fun Conversation.imageFiles(): List<String> =
    messages.flatMap { message -> listOfNotNull(message.image?.file, message.picture?.file) + message.photos.map { it.file } }
