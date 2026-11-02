package io.dlne.chat.net

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

/** `/v1/audio/transcriptions` over real HTTP, the status fields behind the mic button, and the plain-words errors. */
class VoiceApiTest {
    private lateinit var server: MockWebServer
    private val api = DlneApi()
    private val baseUrl get() = server.url("/").toString()
    private val wav = ByteArray(44 + 10) { 7 }

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
    }

    @After
    fun tearDown() {
        server.shutdown()
    }

    private fun failure(block: suspend () -> Unit): ApiException = runBlocking {
        try {
            block()
            fail("expected ApiException")
            throw AssertionError()
        } catch (e: ApiException) {
            e
        }
    }

    private fun error(code: Int, body: String, vararg headers: Pair<String, String>) {
        val r = MockResponse().setResponseCode(code).setBody(body)
        headers.forEach { r.addHeader(it.first, it.second) }
        server.enqueue(r)
    }

    @Test
    fun `the request is multipart with the wav file and response_format json`() {
        server.enqueue(MockResponse().setBody("""{"text":" hello there ","language":"en","duration_seconds":1.2}"""))
        val text = runBlocking { api.transcribe(baseUrl, wav) }
        assertEquals("hello there", text)
        val req = server.takeRequest()
        assertEquals("POST", req.method)
        assertEquals("/v1/audio/transcriptions", req.path)
        assertTrue(req.getHeader("Content-Type")!!.startsWith("multipart/form-data; boundary="))
        val asText = String(req.body.readByteArray(), Charsets.ISO_8859_1)
        assertTrue(asText.contains("name=\"file\"; filename=\"clip.wav\""))
        assertTrue(asText.contains("Content-Type: audio/wav"))
        assertTrue(asText.contains("name=\"response_format\""))
        assertTrue(asText.contains("json"))
        assertTrue("the clip bytes are in the body", asText.contains(String(wav, Charsets.ISO_8859_1)))
    }

    @Test
    fun `request building targets the endpoint under the server address`() {
        val req = DlneApi.transcriptionRequest("https://example.org/", wav)
        assertEquals("https://example.org/v1/audio/transcriptions", req.url.toString())
        assertEquals("POST", req.method)
    }

    @Test
    fun `quota is plain words`() {
        error(429, """{"detail":"too many","code":"voice_quota"}""", "Retry-After" to "600")
        val e = failure { api.transcribe(baseUrl, wav) } as ApiException.Http
        assertEquals("voice_quota", e.errorCode)
        assertEquals(600L, e.retryAfterSeconds)
        assertTrue(VoiceMessages.forError(e).startsWith("Voice is used up for this hour"))
    }

    @Test
    fun `paused and unavailable show the server's sentence`() {
        error(503, """{"detail":"Voice is paused while a picture takes the card.","code":"voice_paused"}""")
        val paused = failure { api.transcribe(baseUrl, wav) }
        assertEquals("Voice is paused while a picture takes the card.", VoiceMessages.forError(paused))
        error(503, """{"detail":"Voice is not set up on this server.","code":"voice_unavailable"}""")
        val off = failure { api.transcribe(baseUrl, wav) }
        assertEquals("Voice is not set up on this server.", VoiceMessages.forError(off))
    }

    @Test
    fun `busy and bad audio are explained`() {
        error(503, """{"detail":"queue full","code":"voice_busy"}""")
        assertEquals(VoiceMessages.BUSY, VoiceMessages.forError(failure { api.transcribe(baseUrl, wav) }))
        error(400, """{"detail":"the clip is longer than 60 s","code":"voice_bad_audio"}""")
        assertEquals(
            "That recording could not be used: the clip is longer than 60 s",
            VoiceMessages.forError(failure { api.transcribe(baseUrl, wav) }),
        )
    }

    @Test
    fun `an unknown code shows the server's detail and no body at all is still readable`() {
        error(500, """{"detail":"boom"}""")
        assertEquals("boom", VoiceMessages.forError(failure { api.transcribe(baseUrl, wav) }))
        error(502, "")
        assertEquals("The server answered 502.", VoiceMessages.forError(failure { api.transcribe(baseUrl, wav) }))
    }

    @Test
    fun `an unreadable answer and a dead server are network errors`() {
        server.enqueue(MockResponse().setBody("not json"))
        val bad = failure { api.transcribe(baseUrl, wav) }
        assertTrue(bad is ApiException.Network)
        assertEquals(VoiceMessages.OFFLINE, VoiceMessages.forError(bad))
        val url = baseUrl
        server.shutdown()
        assertTrue(failure { api.transcribe(url, wav) } is ApiException.Network)
    }

    @Test
    fun `the status shows the mic only when capabilities voice is true`() {
        val json = Json { ignoreUnknownKeys = true; coerceInputValues = true }
        fun parse(text: String) = json.decodeFromString(ClusterStatus.serializer(), text)
        assertTrue(parse("""{"capabilities":{"voice":true},"voice":{"queue":0,"busy":false,"max_seconds":30,"languages":"auto"}}""").voiceAvailable)
        assertFalse(parse("""{"capabilities":{"voice":false}}""").voiceAvailable)
        assertFalse("missing means false", parse("""{"capabilities":{"image_generation":true}}""").voiceAvailable)
        assertFalse("an older server", parse("""{"app_pool":{"members_ready":1}}""").voiceAvailable)
    }

    @Test
    fun `max seconds follows the status within the contract's limit`() {
        val json = Json { ignoreUnknownKeys = true; coerceInputValues = true }
        fun max(text: String) = json.decodeFromString(ClusterStatus.serializer(), text).voiceMaxSeconds()
        assertEquals(30, max("""{"capabilities":{"voice":true},"voice":{"max_seconds":30}}"""))
        assertEquals(60, max("""{"capabilities":{"voice":true},"voice":{"max_seconds":300}}"""))
        assertEquals(1, max("""{"capabilities":{"voice":true},"voice":{"max_seconds":0}}"""))
        assertEquals(60, max("""{"capabilities":{"voice":true}}"""))
        assertEquals(60, (null as ClusterStatus?).voiceMaxSeconds())
    }
}
