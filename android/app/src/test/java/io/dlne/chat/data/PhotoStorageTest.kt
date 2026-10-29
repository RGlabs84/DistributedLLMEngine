package io.dlne.chat.data

import java.io.File
import java.nio.file.Files
import javax.xml.parsers.DocumentBuilderFactory
import kotlinx.serialization.json.Json
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element

/** Photos are kept in private storage with their chat, and go when it goes (contract 2.2). */
class PhotoStorageTest {
    private val dir = Files.createTempDirectory("dlne-photos").toFile()
    private val store = ImageStore(File(dir, "images"))

    @After
    fun cleanUp() {
        dir.deleteRecursively()
    }

    private val jpeg = byteArrayOf(0xFF.toByte(), 0xD8.toByte(), 9, 8, 7, 0xFF.toByte(), 0xD9.toByte())

    // ------------------------------------------------------------------ the files

    @Test
    fun `a photo is stored as a jpg under a random name and read back`() {
        val name = store.savePhoto(jpeg)!!
        assertTrue(name, name.endsWith(".jpg"))
        assertArrayEquals(jpeg, store.file(name)!!.readBytes())
        assertNotEquals("every photo has a name of its own", name, store.savePhoto(jpeg))
    }

    @Test
    fun `a generated image is still a png, and both kinds are found by name`() {
        val png = store.save(byteArrayOf(1, 2))!!
        val photo = store.savePhoto(jpeg)!!
        assertTrue(png.endsWith(".png"))
        assertNotNull(store.file(png))
        assertNotNull(store.file(photo))
    }

    @Test
    fun `only names this store made are read, never a path`() {
        assertNull(store.file("../conversations.json"))
        assertNull(store.file("/etc/passwd"))
        assertNull(store.file("00000000-0000-0000-0000-000000000000.jpg")) // well formed, not there
        assertNull(store.file("00000000-0000-0000-0000-000000000000.gif")) // not a kind this store keeps
        assertNull(store.file("../00000000-0000-0000-0000-000000000000.jpg"))
    }

    @Test
    fun `the stored photo reads back as the data URL the server takes`() {
        val name = store.savePhoto(jpeg)!!
        val url = store.photoDataUrl(Photo(name, 10, 10))
        assertEquals(PhotoEncoding.dataUrl(jpeg), url)
        assertTrue(url!!.startsWith("data:image/jpeg;base64,"))
    }

    @Test
    fun `a photo whose file is gone, or a name that is not ours, has no data URL`() {
        val name = store.savePhoto(jpeg)!!
        store.delete(listOf(name))
        assertNull(store.photoDataUrl(Photo(name, 10, 10)))
        assertNull(store.photoDataUrl(Photo("../conversations.json", 1, 1)))
    }

    // ------------------------------------------------------------------ with the chat

    private fun message(role: Role, text: String, photos: List<Photo> = emptyList(), image: ImageAttachment? = null) =
        ChatMessage(newId(), role, text, createdAt = 0, image = image, photos = photos)

    @Test
    fun `a conversation lists its photos with its generated images`() {
        val chat = Conversation(
            "c", "t", 0, 0,
            listOf(
                message(Role.USER, "Look", listOf(Photo("a.jpg", 1, 1), Photo("b.jpg", 1, 1))),
                message(Role.ASSISTANT, "A cat."),
                message(Role.USER, "[Image: a fox]", image = ImageAttachment("a fox")),
                message(Role.ASSISTANT, "[Image: a fox]", image = ImageAttachment("a fox", "fox.png", 1, 1)),
            ),
        )
        assertEquals(listOf("a.jpg", "b.jpg", "fox.png"), chat.imageFiles())
    }

    @Test
    fun `deleting a chat deletes its photos and nobody else's`() {
        val mine = listOf(store.savePhoto(jpeg)!!, store.savePhoto(jpeg)!!)
        val generated = store.save(byteArrayOf(1))!!
        val other = store.savePhoto(jpeg)!!
        val chat = Conversation(
            "c", "t", 0, 0,
            listOf(
                message(Role.USER, "Look", mine.map { Photo(it, 1, 1) }),
                message(Role.ASSISTANT, "[Image: a fox]", image = ImageAttachment("a fox", generated, 1, 1)),
            ),
        )
        // What ChatViewModel.deleteConversation does: delete every file the chat refers to.
        store.delete(chat.imageFiles())
        mine.forEach { assertNull(store.file(it)) }
        assertNull(store.file(generated))
        assertNotNull("another chat's photo stays", store.file(other))
    }

    // The same settings as ConversationStore.
    private val storeJson = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    @Test
    fun `a chat with photos is saved and read back with them`() {
        val message = message(Role.USER, "", listOf(Photo("a.jpg", 1264, 948), Photo("b.jpg", 948, 1264)))
        val file = ConversationFile(conversations = listOf(Conversation("c1", "Photos", 1, 1, listOf(message))))
        val text = storeJson.encodeToString(ConversationFile.serializer(), file)
        assertEquals(file, storeJson.decodeFromString(ConversationFile.serializer(), text))
        assertTrue(text, text.contains(""""photos":[{"file":"a.jpg","width":1264,"height":948}"""))
    }

    @Test
    fun `chats saved before photos still load, with none`() {
        val old = """{"version":1,"conversations":[{"id":"c","title":"t","createdAt":1,"updatedAt":1,"messages":[
            {"id":"m","role":"user","content":"hi","createdAt":1}]}]}"""
        val loaded = storeJson.decodeFromString(ConversationFile.serializer(), old).conversations.single().messages.single()
        assertEquals(emptyList<Photo>(), loaded.photos)
    }

    // ------------------------------------------------------------------ the camera's file

    @Test
    fun `the camera gets a new empty file in a folder of the cache`() {
        val cache = File(dir, "cache")
        val first = PhotoFiles.newCameraFile(cache)
        val second = PhotoFiles.newCameraFile(cache)
        assertEquals(File(cache, PhotoFiles.CAMERA_DIR), first.parentFile)
        assertTrue(first.isFile && first.length() == 0L)
        assertNotEquals(first, second)
        assertTrue(first.name.endsWith(".jpg"))
    }

    @Test
    fun `a shot nobody collected is cleared out later, a recent one is not`() {
        val cache = File(dir, "cache")
        val old = PhotoFiles.newCameraFile(cache)
        val recent = PhotoFiles.newCameraFile(cache)
        val now = System.currentTimeMillis()
        assertTrue(old.setLastModified(now - 2 * 60 * 60 * 1000))
        assertTrue(recent.setLastModified(now - 5 * 60 * 1000))
        PhotoFiles.newCameraFile(cache, now)
        assertFalse(old.exists())
        assertTrue(recent.exists())
    }

    // Gradle runs unit tests with the module directory (android/app) as the working directory.
    private val filePaths = File(System.getProperty("user.dir")!!, "src/main/res/xml/file_paths.xml")

    private fun providerPaths(): Map<String, String> {
        val root = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(filePaths).documentElement
        val out = HashMap<String, String>()
        for (kind in listOf("files-path", "cache-path")) {
            val nodes = root.getElementsByTagName(kind)
            for (i in 0 until nodes.length) {
                val element = nodes.item(i) as Element
                out["$kind:${element.getAttribute("path")}"] = element.getAttribute("name")
            }
        }
        return out
    }

    @Test
    fun `the FileProvider shares the camera folder of the cache, which is what lets the camera app write the photo`() {
        // A folder name that does not match makes getUriForFile throw when the person taps Camera.
        val paths = providerPaths()
        assertEquals("camera", paths["cache-path:${PhotoFiles.CAMERA_DIR}/"])
    }

    @Test
    fun `the generated images folder is still shared for Share, and nothing else is`() {
        // AppGraph keeps images in filesDir/images.
        assertEquals(setOf("files-path:images/", "cache-path:camera/"), providerPaths().keys)
    }
}
