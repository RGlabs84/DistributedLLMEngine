package io.dlne.chat.net

import io.dlne.chat.data.ChatHistory
import io.dlne.chat.data.ChatMessage
import io.dlne.chat.data.Photo
import io.dlne.chat.data.Role
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

/** Message content on the wire (contract 2.1): a plain string, or for a user turn with photos a list of parts. */
class PhotoWireTest {
    private val photoUrl = "data:image/jpeg;base64,/9j/4AAQSkZJRg=="

    private fun encode(message: WireMessage) = Json.encodeToString(WireMessage.serializer(), message)

    @Test
    fun `a turn without photos is written exactly as before, content a plain string`() {
        assertEquals("""{"role":"user","content":"Hi"}""", encode(WireMessage("user", "Hi")))
        assertEquals("""{"role":"assistant","content":"Hello"}""", encode(WireMessage("assistant", "Hello")))
        // an empty list of images is no images
        assertEquals("""{"role":"user","content":"Hi"}""", encode(WireMessage("user", "Hi", emptyList())))
    }

    @Test
    fun `a turn with a photo is a list of parts, the text first`() {
        assertEquals(
            """{"role":"user","content":[{"type":"text","text":"What is this?"},""" +
                """{"type":"image_url","image_url":{"url":"$photoUrl"}}]}""",
            encode(WireMessage("user", "What is this?", listOf(photoUrl))),
        )
    }

    @Test
    fun `photos alone have no text part`() {
        assertEquals(
            """{"role":"user","content":[{"type":"image_url","image_url":{"url":"$photoUrl"}}]}""",
            encode(WireMessage("user", "", listOf(photoUrl))),
        )
    }

    @Test
    fun `several photos keep their order, one image part each`() {
        val urls = listOf("data:image/jpeg;base64,AAAA", "data:image/jpeg;base64,BBBB", "data:image/jpeg;base64,CCCC")
        val content = Json.parseToJsonElement(encode(WireMessage("user", "Compare", urls))).jsonObject["content"]!!.jsonArray
        assertEquals(4, content.size)
        assertEquals("text", content[0].jsonObject["type"]!!.jsonPrimitive.content)
        assertEquals(
            urls,
            content.drop(1).map {
                assertEquals("image_url", it.jsonObject["type"]!!.jsonPrimitive.content)
                it.jsonObject["image_url"]!!.jsonObject["url"]!!.jsonPrimitive.content
            },
        )
    }

    @Test
    fun `text with quotes, line breaks and emoji is escaped, not mangled`() {
        val text = "He said \"hi\"\nand left 😀"
        val parsed = Json.parseToJsonElement(encode(WireMessage("user", text, listOf(photoUrl)))).jsonObject
        assertEquals(text, parsed["content"]!!.jsonArray[0].jsonObject["text"]!!.jsonPrimitive.content)
        assertEquals(text, Json.parseToJsonElement(encode(WireMessage("user", text))).jsonObject["content"]!!.jsonPrimitive.content)
    }

    @Test
    fun `the request is only written, never read back`() {
        try {
            Json.decodeFromString(WireMessage.serializer(), """{"role":"user","content":"Hi"}""")
            fail("expected SerializationException")
        } catch (_: SerializationException) {
            // as designed: reading a message back is not something the app does
        }
    }

    // ------------------------------------------------------------------ the status

    private val statusJson = Json { ignoreUnknownKeys = true; coerceInputValues = true }

    private fun status(text: String) = statusJson.decodeFromString(ClusterStatus.serializer(), text)

    @Test
    fun `the status says photos can be read only when app_vision is true`() {
        assertTrue(status("""{"capabilities":{"app_vision":true}}""").appVision)
        assertFalse(status("""{"capabilities":{"app_vision":false}}""").appVision)
        assertFalse("missing means false", status("""{"capabilities":{"image_generation":true}}""").appVision)
        assertFalse("an older server", status("""{"app_pool":{"members_ready":1}}""").appVision)
        assertFalse("null means false", status("""{"capabilities":{"app_vision":null}}""").appVision)
    }

    @Test
    fun `app_vision and image_generation are independent`() {
        val both = status("""{"capabilities":{"image_generation":true,"app_vision":true}}""")
        assertTrue(both.imageGeneration && both.appVision)
        val onlyImages = status("""{"capabilities":{"image_generation":true}}""")
        assertTrue(onlyImages.imageGeneration)
        assertFalse(onlyImages.appVision)
    }

    // ------------------------------------------------------------------ the whole request, over HTTP

    private lateinit var server: MockWebServer
    private val api = DlneApi()

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private val baseUrl get() = server.url("/").toString()

    @Test
    fun `the request carries a photo turn as parts and every other turn as a string`() = runBlocking {
        server.enqueue(MockResponse().setBody("data: [DONE]\n\n"))
        val chat = listOf(
            message(Role.USER, "Look", "a.jpg"),
            message(Role.ASSISTANT, "A cat."),
            message(Role.USER, "And this?", "b.jpg"),
            message(Role.USER, "Which is bigger?"),
        )
        val history = ChatHistory.toWire(chat) { photo -> "data:image/jpeg;base64,${photo.file}" }
        api.streamChat(baseUrl, CHAT_ID, history).toList()

        val body = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        // "features" (2.6.2): every chat request says the app shows pictures made in the chat.
        assertEquals(setOf("chat_id", "messages", "max_tokens", "stream", "features"), body.keys)
        val messages = body["messages"]!!.jsonArray
        assertEquals(3, messages.size)
        // the older photo is a mark in a plain string
        assertEquals(JsonPrimitive("Look\n\n[photo]"), messages[0].jsonObject["content"])
        assertEquals(JsonPrimitive("A cat."), messages[1].jsonObject["content"])
        // the newest photo turn, merged with the message sent right after it, is a list of parts
        val parts = messages[2].jsonObject["content"] as JsonArray
        assertEquals("And this?\n\nWhich is bigger?", parts[0].jsonObject["text"]!!.jsonPrimitive.content)
        assertEquals("data:image/jpeg;base64,b.jpg", parts[1].jsonObject["image_url"]!!.jsonObject["url"]!!.jsonPrimitive.content)
        assertEquals(2, parts.size)
    }

    @Test
    fun `a chat without photos sends the same body as before`() = runBlocking {
        server.enqueue(MockResponse().setBody("data: [DONE]\n\n"))
        api.streamChat(baseUrl, CHAT_ID, listOf(WireMessage("user", "Hi"))).toList()
        val text = server.takeRequest().body.readUtf8()
        assertTrue(text, text.contains(""""messages":[{"role":"user","content":"Hi"}]"""))
    }

    private var clock = 0L

    private fun message(role: Role, text: String, vararg photos: String) =
        ChatMessage("m${clock}", role, text, createdAt = clock++, photos = photos.map { Photo(it, 1, 1) })
}
