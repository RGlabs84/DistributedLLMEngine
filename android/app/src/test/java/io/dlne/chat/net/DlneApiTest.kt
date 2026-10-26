package io.dlne.chat.net

import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

/**
 * The client end to end over real HTTP (a local MockWebServer): request shape, streaming
 * through deliberately fragmented network reads, and error handling.
 */
class DlneApiTest {
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

    private fun sse(vararg contents: String): String = contents.joinToString("") { content ->
        """data: {"choices":[{"index":0,"delta":{"content":"$content"},"finish_reason":null}]}""" + "\n\n"
    } + "data: [DONE]\n\n"

    @Test
    fun `streams an answer delivered in 7-byte network reads`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setHeader("Content-Type", "text/event-stream")
                .setBody(sse("Hel", "lo ", "wor", "ld"))
                .throttleBody(7, 5, TimeUnit.MILLISECONDS)
        )

        val events = api.streamChat(baseUrl, listOf(WireMessage("user", "Hi"))).toList()

        assertEquals(
            listOf("Hel", "lo ", "wor", "ld"),
            events.filterIsInstance<StreamEvent.Content>().map { it.text },
        )
        assertEquals(StreamEvent.Done, events.last())

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/app/chat/completions", request.path)
        val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals(1024, body["max_tokens"]!!.jsonPrimitive.int)
        val message = body["messages"]!!.jsonArray.single().jsonObject
        assertEquals("user", message["role"]!!.jsonPrimitive.content)
        assertEquals("Hi", message["content"]!!.jsonPrimitive.content)
    }

    @Test
    fun `a base URL with a path prefix and trailing slash is respected`() = runBlocking {
        server.enqueue(MockResponse().setBody(sse("x")))
        api.streamChat(server.url("/dlne/").toString(), listOf(WireMessage("user", "Hi"))).toList()
        assertEquals("/dlne/v1/app/chat/completions", server.takeRequest().path)
    }

    @Test
    fun `429 reports the detail and Retry-After`() = runBlocking {
        server.enqueue(
            MockResponse()
                .setResponseCode(429)
                .setHeader("Retry-After", "12")
                .setBody("""{"detail":"One answer at a time: wait for the current one to finish."}""")
        )
        try {
            api.streamChat(baseUrl, listOf(WireMessage("user", "Hi"))).toList()
            fail("expected ApiException.Http")
        } catch (e: ApiException.Http) {
            assertEquals(429, e.code)
            assertEquals("One answer at a time: wait for the current one to finish.", e.detail)
            assertEquals(12L, e.retryAfterSeconds)
        }
    }

    @Test
    fun `400 with a FastAPI validation list is readable`() = runBlocking {
        server.enqueue(
            MockResponse().setResponseCode(400)
                .setBody("""{"detail":[{"loc":["body"],"msg":"field required"}]}""")
        )
        try {
            api.streamChat(baseUrl, listOf(WireMessage("user", "Hi"))).toList()
            fail("expected ApiException.Http")
        } catch (e: ApiException.Http) {
            assertEquals(400, e.code)
            assertEquals("field required", e.detail)
            assertEquals(null, e.retryAfterSeconds)
        }
    }

    @Test
    fun `a dropped connection is a network error`() = runBlocking {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.DISCONNECT_AT_START))
        // No silent retry here, so the test sees the first failure instead of waiting on a
        // second response that is never queued.
        val noRetry = DlneApi(DlneApi.defaultHttpClient().newBuilder().retryOnConnectionFailure(false).build())
        try {
            noRetry.streamChat(baseUrl, listOf(WireMessage("user", "Hi"))).toList()
            fail("expected ApiException.Network")
        } catch (e: ApiException.Network) {
            assertTrue(e.cause is java.io.IOException)
        }
    }

    @Test
    fun `status is parsed and unknown fields are ignored`() = runBlocking {
        server.enqueue(
            MockResponse().setBody(
                """{"status":"ok","server_version":"2.3.0","coding_queue":{"x":1},
                   "app_pool":{"members":3,"members_ready":2,"active_tier":"qwen3-4b",
                   "active_model":"Qwen3 4B Instruct 2507","tiers":[],"requests_in_flight":0,
                   "median_answer_seconds":null}}"""
            )
        )
        val status = api.fetchStatus(baseUrl)
        assertEquals("2.3.0", status.serverVersion)
        assertEquals(2, status.appPool?.membersReady)
        assertEquals("Qwen3 4B Instruct 2507", status.appPool?.activeModel)
        assertEquals("/v1/cluster/status", server.takeRequest().path)
    }

    @Test
    fun `status from a server without the app pool has no pool`() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"status":"ok","server_version":"2.2.0"}"""))
        assertEquals(null, api.fetchStatus(baseUrl).appPool)
    }
}
