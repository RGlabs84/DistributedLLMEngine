package io.dlne.chat.net

import java.util.Base64
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

/** The image calls (`/v1/app/images`) over real HTTP, and the status fields that switch the feature on. */
class ImageApiTest {
    private lateinit var server: MockWebServer
    private val api = DlneApi()
    private val baseUrl get() = server.url("/").toString()

    // The 8 bytes every PNG starts with, plus filler: enough for the signature check.
    private val png = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 1, 2, 3)
    private val b64 = Base64.getEncoder().encodeToString(png)

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun failure(block: suspend () -> Unit): ApiException.Http = runBlocking {
        try {
            block()
            fail("expected ApiException.Http")
            throw AssertionError()
        } catch (e: ApiException.Http) {
            e
        }
    }

    @Test
    fun `the status shows the button only when the server says image_generation is true`() {
        val json = Json { ignoreUnknownKeys = true; coerceInputValues = true }
        fun parse(text: String) = json.decodeFromString(ClusterStatus.serializer(), text)
        assertTrue(parse("""{"capabilities":{"image_generation":true}}""").imageGeneration)
        assertFalse(parse("""{"capabilities":{"image_generation":false}}""").imageGeneration)
        assertFalse("missing means false", parse("""{"capabilities":{}}""").imageGeneration)
        assertFalse("an older server", parse("""{"app_pool":{"members_ready":1}}""").imageGeneration)
    }

    @Test
    fun `the sizes and times come from the status, with the documented sizes as the fallback`() {
        val json = Json { ignoreUnknownKeys = true; coerceInputValues = true }
        val status = json.decodeFromString(
            ClusterStatus.serializer(),
            """{"capabilities":{"image_generation":true},"image_generation":{"queue":0,"busy":false,
               "eta_seconds":{"768x768":80,"1024x1024":153},"sizes":["768x768","1024x1024","evil"],"default_size":"1024x1024"}}""",
        )
        assertEquals(listOf("768x768", "1024x1024"), status.imageSizes())
        assertEquals("1024x1024", status.defaultImageSize())
        assertEquals(80, status.imageEtaSeconds("768x768"))
        assertNull(status.imageEtaSeconds("768x1024"))
        assertEquals(DEFAULT_IMAGE_SIZES, ClusterStatus().imageSizes())
        assertEquals("768x768", ClusterStatus().defaultImageSize())
    }

    @Test
    fun `creating an image posts the prompt, size and chat id and reads the ticket`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(202).setBody("""{"id":"abc_DEF-1","status":"queued","position":2,"eta_seconds":80}"""))
        val ticket = api.createImage(baseUrl, "a red fox", "768x768", "5f0c3c6e-0000-4000-8000-000000000000")

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/app/images", request.path)
        val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals(setOf("prompt", "size", "chat_id"), body.keys)
        assertEquals("a red fox", body["prompt"]!!.jsonPrimitive.content)
        assertEquals("768x768", body["size"]!!.jsonPrimitive.content)
        assertEquals(ImageTicket("abc_DEF-1", 2, 80), ticket)
    }

    @Test
    fun `an unset size and chat id are left out`() = runBlocking {
        server.enqueue(MockResponse().setResponseCode(202).setBody("""{"id":"x","status":"queued"}"""))
        api.createImage(baseUrl, "fox", null, null)
        val body = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        assertEquals(setOf("prompt"), body.keys)
    }

    @Test
    fun `a refusal shows the server's words and code`() {
        server.enqueue(
            MockResponse().setResponseCode(400)
                .setBody("""{"detail":"I can't make that one.","code":"prompt_refused","category":"violence"}"""),
        )
        val e = failure { api.createImage(baseUrl, "x", null, null) }
        assertEquals(400, e.code)
        assertEquals("I can't make that one.", e.detail)
        assertEquals("prompt_refused", e.errorCode)
    }

    @Test
    fun `the hourly cap gives 429 with Retry-After`() {
        server.enqueue(
            MockResponse().setResponseCode(429).setHeader("Retry-After", "600")
                .setBody("""{"detail":"Too many images from your network.","code":"image_quota"}"""),
        )
        val e = failure { api.createImage(baseUrl, "x", null, null) }
        assertEquals(429, e.code)
        assertEquals(600L, e.retryAfterSeconds)
        assertEquals("image_quota", e.errorCode)
    }

    @Test
    fun `unavailable gives 503 with its code`() {
        server.enqueue(MockResponse().setResponseCode(503).setBody("""{"detail":"Images are off.","code":"image_generation_unavailable"}"""))
        val e = failure { api.createImage(baseUrl, "x", null, null) }
        assertEquals(503, e.code)
        assertEquals("image_generation_unavailable", e.errorCode)
    }

    @Test
    fun `a running job reports its stage and times`() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"id":"j1","status":"generating","elapsed_seconds":12.4,"eta_seconds":80}"""))
        val progress = api.imageProgress(baseUrl, "j1")
        assertEquals("/v1/app/images/j1", server.takeRequest().path)
        assertEquals(ImageStatus.GENERATING, progress.status)
        assertEquals(12, progress.elapsedSeconds)
        assertEquals(80, progress.etaSeconds)
        assertNull(progress.image)
    }

    @Test
    fun `a finished job hands over the PNG bytes`() = runBlocking {
        server.enqueue(
            MockResponse().setBody("""{"id":"j1","status":"completed","elapsed_seconds":81,"image":{"b64_png":"$b64","width":768,"height":768,"seed":7}}"""),
        )
        val progress = api.imageProgress(baseUrl, "j1")
        assertEquals(ImageStatus.COMPLETED, progress.status)
        val image = progress.image!!
        assertTrue(png.contentEquals(image.png))
        assertEquals(768, image.width)
    }

    @Test
    fun `bytes that are not a PNG are dropped`() = runBlocking {
        val notPng = Base64.getEncoder().encodeToString("<html>nope</html>".toByteArray())
        server.enqueue(MockResponse().setBody("""{"id":"j1","status":"completed","image":{"b64_png":"$notPng","width":1,"height":1}}"""))
        assertNull(api.imageProgress(baseUrl, "j1").image)
        server.enqueue(MockResponse().setBody("""{"id":"j1","status":"completed","image":{"b64_png":"@@@not base64@@@","width":1,"height":1}}"""))
        assertNull(api.imageProgress(baseUrl, "j1").image)
    }

    @Test
    fun `a refusal after the job started carries the server's detail`() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"id":"j1","status":"refused","error":{"detail":"The picture broke a rule.","code":"result_refused"}}"""))
        val progress = api.imageProgress(baseUrl, "j1")
        assertEquals(ImageStatus.REFUSED, progress.status)
        assertTrue(progress.status.finished)
        assertEquals("The picture broke a rule.", progress.errorDetail)
    }

    @Test
    fun `every documented status is read, and an unknown one keeps waiting`() {
        for (s in listOf("queued", "checking", "generating", "reviewing")) assertFalse(s, ImageStatus.of(s).finished)
        for (s in listOf("completed", "refused", "failed", "cancelled", "expired")) assertTrue(s, ImageStatus.of(s).finished)
        assertEquals(ImageStatus.UNKNOWN, ImageStatus.of("teleporting"))
        assertFalse(ImageStatus.UNKNOWN.finished)
        assertEquals(ImageStatus.UNKNOWN, ImageStatus.of(null))
    }

    @Test
    fun `a job someone else made is a 404`() {
        server.enqueue(MockResponse().setResponseCode(404).setBody("""{"detail":"No such image."}"""))
        val e = failure { api.imageProgress(baseUrl, "other") }
        assertEquals(404, e.code)
        assertEquals("No such image.", e.detail)
    }

    @Test
    fun `cancelling posts to the cancel address and returns the status`() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"id":"j1","status":"cancelled"}"""))
        val status = api.cancelImage(baseUrl, "j1")
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/app/images/j1/cancel", request.path)
        assertEquals(ImageStatus.CANCELLED, status)
    }

    @Test
    fun `a garbled answer is a network error, not a crash`() {
        server.enqueue(MockResponse().setBody("not json"))
        try {
            runBlocking { api.imageProgress(baseUrl, "j1") }
            fail("expected ApiException.Network")
        } catch (e: ApiException.Network) {
            assertNotNull(e.message)
        }
    }
}
