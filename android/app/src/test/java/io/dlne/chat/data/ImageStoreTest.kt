package io.dlne.chat.data

import java.io.File
import java.nio.file.Files
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Test

class ImageStoreTest {
    private val dir = Files.createTempDirectory("dlne-images").toFile()
    private val store = ImageStore(File(dir, "images"))

    @After
    fun cleanUp() {
        dir.deleteRecursively()
    }

    @Test
    fun `an image is stored under a random name and read back`() {
        val png = byteArrayOf(1, 2, 3)
        val name = store.save(png)!!
        assertArrayEquals(png, store.file(name)!!.readBytes())
        assertFalse("the name says nothing about the prompt", name.contains("fox"))
    }

    @Test
    fun `only names this store made are read, never a path`() {
        val name = store.save(byteArrayOf(9))!!
        assertNotNull(store.file(name))
        assertNull(store.file(null))
        assertNull(store.file("../conversations.json"))
        assertNull(store.file("/etc/passwd"))
        assertNull(store.file("00000000-0000-0000-0000-000000000000.png")) // well formed, not there
    }

    @Test
    fun `deleting a chat's images removes the files and ignores the rest`() {
        val a = store.save(byteArrayOf(1))!!
        val b = store.save(byteArrayOf(2))!!
        store.delete(listOf(a, null, "../x"))
        assertNull(store.file(a))
        assertNotNull(store.file(b))
    }

    @Test
    fun `a conversation lists the image files it uses`() {
        val chat = Conversation(
            "c", "t", 0, 0,
            listOf(
                ChatMessage("1", Role.USER, "[Image: a]", createdAt = 0, image = ImageAttachment("a")),
                ChatMessage("2", Role.ASSISTANT, "[Image: a]", createdAt = 0, image = ImageAttachment("a", "f.png", 1, 1)),
            ),
        )
        assertEquals(listOf("f.png"), chat.imageFiles())
    }
}
