package io.dlne.chat.data

import java.io.File
import java.io.IOException
import java.nio.file.Files
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * What stays behind in the app's storage and is cleared out: a half-written file after a failed write,
 * photos and pictures no chat refers to, and the camera's raw shots in the cache.
 */
class StoreTidyTest {
    private val root = Files.createTempDirectory("dlne-tidy").toFile()
    private val dir = File(root, "images")
    private val store = ImageStore(dir)

    @After
    fun cleanUp() {
        root.deleteRecursively()
    }

    private val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 9, 8, 7, 0xFF.toByte(), 0xD9.toByte())
    private val hour = 60 * 60 * 1000L

    /** Old enough for the sweep: written an hour ago. */
    private fun aged(name: String, ago: Long = hour, bytes: ByteArray = jpeg): File {
        dir.mkdirs()
        return File(dir, name).also { it.writeBytes(bytes); assertTrue(it.setLastModified(System.currentTimeMillis() - ago)) }
    }

    private val orphanPhoto = "11111111-1111-4111-8111-111111111111.jpg"
    private val orphanPicture = "22222222-2222-4222-8222-222222222222.png"
    private val keptPhoto = "33333333-3333-4333-8333-333333333333.jpg"
    private val keptPicture = "44444444-4444-4444-8444-444444444444.png"
    private val keptImage = "55555555-5555-4555-8555-555555555555.png"

    private fun chat(vararg messages: ChatMessage) = Conversation("c", "t", 0, 0, messages.toList())

    private fun referring(): List<Conversation> = listOf(
        chat(
            ChatMessage("1", Role.USER, "look", createdAt = 0, photos = listOf(Photo(keptPhoto, 1, 1))),
            ChatMessage("2", Role.ASSISTANT, "A fox.", createdAt = 0, picture = ReplyPicture("a fox", keptPicture, 1, 1)),
            ChatMessage("3", Role.ASSISTANT, "[Image: a cat]", createdAt = 0, image = ImageAttachment("a cat", keptImage, 1, 1)),
        ),
    )

    // ------------------------------------------------------------------ a write that fails

    @Test
    fun `a write that fails leaves no half-written file behind`() {
        val full = ImageStore(dir) { file, bytes ->
            file.writeBytes(bytes.copyOf(2)) // the disk filled up part of the way
            throw IOException("No space left on device")
        }
        assertNull(full.savePhoto(jpeg))
        assertNull(full.save(byteArrayOf(1, 2, 3)))
        assertEquals(emptyList<String>(), dir.list().orEmpty().toList())
    }

    @Test
    fun `a write that works leaves the file and nothing else`() {
        val name = store.savePhoto(jpeg)!!
        assertEquals(listOf(name), dir.list().orEmpty().toList())
        assertNotNull(store.file(name))
    }

    // ------------------------------------------------------------------ files no chat refers to

    @Test
    fun `files no chat refers to are removed, the ones a chat uses stay`() {
        val files = listOf(orphanPhoto, orphanPicture, keptPhoto, keptPicture, keptImage).map { aged(it) }
        assertEquals(2, store.sweep(referring(), historyComplete = true))
        assertEquals(listOf(false, false, true, true, true), files.map { it.exists() })
    }

    @Test
    fun `a file written moments ago is left alone, its chat may not have it yet`() {
        val now = System.currentTimeMillis()
        val justSaved = aged(orphanPhoto, ago = 5_000)
        val old = aged(orphanPicture)
        assertEquals(1, store.sweep(emptyList(), historyComplete = true, now = now))
        assertTrue("a send or a job is about to put it in its chat", justSaved.exists())
        assertFalse(old.exists())
        // And it goes once it is old enough and still unreferenced.
        assertEquals(1, store.sweep(emptyList(), historyComplete = true, now = now + ImageStore.SWEEP_GRACE_MS + 10_000))
        assertFalse(justSaved.exists())
    }

    @Test
    fun `a half-written file a failed write left is removed when it is old, not while it is being written`() {
        val old = aged("$orphanPhoto.tmp")
        val writing = aged("$orphanPicture.tmp", ago = 1_000)
        assertEquals(1, store.sweep(emptyList(), historyComplete = true))
        assertFalse(old.exists())
        assertTrue(writing.exists())
    }

    @Test
    fun `nothing is removed when the history could not be read in full`() {
        // A chat that failed to load still refers to its files, which look unreferenced.
        val files = listOf(orphanPhoto, keptPhoto).map { aged(it) }
        assertEquals(0, store.sweep(emptyList(), historyComplete = false))
        assertEquals(0, store.sweep(referring(), historyComplete = false))
        assertTrue(files.all { it.exists() })
    }

    @Test
    fun `only names the store itself makes are ever touched`() {
        val strangers = listOf(
            aged("conversations.json"),
            aged("notes.txt"),
            aged("photo.jpg"),
            aged("11111111-1111-4111-8111-111111111111.gif"),
            aged("11111111-1111-4111-8111-111111111111.jpg.bak"),
            aged("11111111-1111-4111-8111-111111111111.jpg.tmp.old"),
            aged("UPPERCASE-1111-4111-8111-111111111111.jpg"),
        )
        val folder = File(dir, orphanPhoto).also { it.mkdirs() } // a folder with a matching name is not a file of ours
        assertEquals(0, store.sweep(emptyList(), historyComplete = true))
        assertTrue(strangers.all { it.exists() })
        assertTrue(folder.isDirectory)
    }

    @Test
    fun `a missing or empty folder is nothing to sweep`() {
        assertEquals(0, store.sweep(emptyList(), historyComplete = true))
        dir.mkdirs()
        assertEquals(0, store.sweep(referring(), historyComplete = true))
    }

    @Test
    fun `a file a chat refers to is kept however old, and a name two chats share is kept for both`() {
        val shared = aged(keptPhoto)
        val other = chat(ChatMessage("9", Role.USER, "again", createdAt = 0, photos = listOf(Photo(keptPhoto, 1, 1))))
        assertEquals(0, store.sweep(listOf(other), historyComplete = true, now = System.currentTimeMillis() + 365 * 24 * hour))
        assertTrue(shared.exists())
    }

    // ------------------------------------------------------------------ the camera's shots

    private val cache = File(root, "cache")

    private fun shot(name: String, ago: Long): File {
        val camera = File(cache, PhotoFiles.CAMERA_DIR)
        camera.mkdirs()
        return File(camera, name).also { it.writeBytes(jpeg); assertTrue(it.setLastModified(System.currentTimeMillis() - ago)) }
    }

    @Test
    fun `shots nobody collected are cleared when the app starts afresh`() {
        val old = shot("shot-1.jpg", ago = 30 * 60 * 1000)
        val older = shot("shot-2.jpg", ago = 5 * 60 * 1000)
        assertEquals(2, PhotoFiles.sweepCameraDir(cache))
        assertFalse(old.exists() || older.exists())
    }

    @Test
    fun `a shot that may still be on its way back is left alone`() {
        val recent = shot("shot-3.jpg", ago = 20 * 1000)
        assertEquals(0, PhotoFiles.sweepCameraDir(cache))
        assertTrue(recent.exists())
    }

    @Test
    fun `only the camera folder is swept, and no folder at all is nothing to do`() {
        assertEquals(0, PhotoFiles.sweepCameraDir(cache))
        val other = File(cache, "other").also { it.mkdirs() }
        val keep = File(other, "x.bin").also { it.writeBytes(jpeg); it.setLastModified(0) }
        shot("shot-4.jpg", ago = hour)
        assertEquals(1, PhotoFiles.sweepCameraDir(cache))
        assertTrue(keep.exists())
    }
}
