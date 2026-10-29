package io.dlne.chat.net

import io.dlne.chat.data.ReportReason
import io.dlne.chat.data.ReportRequest
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test

/** `POST /v1/app/report` and a chat stream with several lookups, over real HTTP. */
class ReportAndLookupApiTest {
    private lateinit var server: MockWebServer
    private val api = DlneApi()
    private val report = ReportRequest("Is it raining?", "Yes [1].", ReportReason.WRONG, "It was sunny")

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

    private fun reportFails(): ApiException.Http = runBlocking {
        try {
            api.sendReport(baseUrl, report)
            fail("expected ApiException.Http")
            throw AssertionError()
        } catch (e: ApiException.Http) {
            e
        }
    }

    @Test
    fun `a report is posted as JSON and accepted on ok true`() = runBlocking {
        server.enqueue(MockResponse().setBody("""{"ok":true}"""))
        api.sendReport(baseUrl, report)

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/v1/app/report", request.path)
        assertTrue(request.getHeader("Content-Type")!!.startsWith("application/json"))
        val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals(setOf("question", "answer", "reason", "note"), body.keys)
        assertEquals("Is it raining?", body["question"]!!.jsonPrimitive.content)
        assertEquals("Yes [1].", body["answer"]!!.jsonPrimitive.content)
        assertEquals("wrong", body["reason"]!!.jsonPrimitive.content)
        assertEquals("It was sunny", body["note"]!!.jsonPrimitive.content)
    }

    @Test
    fun `too many reports gives 429 with Retry-After`() {
        server.enqueue(
            MockResponse().setResponseCode(429).setHeader("Retry-After", "30")
                .setBody("""{"detail":"Too many reports."}""")
        )
        val e = reportFails()
        assertEquals(429, e.code)
        assertEquals(30L, e.retryAfterSeconds)
        assertEquals("Too many reports.", e.detail)
    }

    @Test
    fun `a server without reporting answers 404`() {
        server.enqueue(MockResponse().setResponseCode(404).setBody("""{"detail":"Not Found"}"""))
        assertEquals(404, reportFails().code)
    }

    @Test
    fun `a 200 that is not ok true is not taken as success`() {
        server.enqueue(MockResponse().setBody("""{"ok":false,"detail":"rejected"}"""))
        assertEquals("rejected", reportFails().detail)
        server.enqueue(MockResponse().setBody("<html>proxy page</html>"))
        assertEquals(200, reportFails().code)
    }

    @Test
    fun `a stream with several lookups delivers each event, sources replacing the list`() = runBlocking {
        fun dlne(json: String) = "data: {\"dlne\":$json}\n\n"
        val body = dlne("""{"event":"searching","tool":"weather","query":"Philadelphia"}""") +
            dlne("""{"event":"sources","sources":[{"n":1,"title":"Open-Meteo","url":"https://open-meteo.com/x"}]}""") +
            dlne("""{"event":"searching","tool":"news","query":"Philadelphia storm"}""") +
            dlne("""{"event":"sources","sources":[{"n":1,"title":"Open-Meteo","url":"https://open-meteo.com/x"},{"n":2,"title":"GDELT story","url":"https://news.example/s"}]}""") +
            dlne("""{"event":"search_note","note":"News results may be a day old."}""") +
            dlne("""{"event":"brand_new_event","text":"must not show"}""") +
            """data: {"choices":[{"delta":{"content":"Rain [1], storm warning [2]."}}]}""" + "\n\n" +
            "data: [DONE]\n\n"
        server.enqueue(MockResponse().setBody(body).throttleBody(5, 2, TimeUnit.MILLISECONDS))

        val events = api.streamChat(baseUrl, CHAT_ID, listOf(WireMessage("user", "Weather in Philadelphia?"))).toList()

        assertEquals(
            listOf("Searching", "Sources", "Searching", "Sources", "SearchNote", "Content", "Done"),
            events.map { it::class.simpleName },
        )
        assertEquals(StreamEvent.Searching("news", "Philadelphia storm"), events[2])
        assertEquals(listOf(1, 2), (events[3] as StreamEvent.Sources).sources.map { it.n })
        assertEquals(StreamEvent.Content("Rain [1], storm warning [2]."), events[5])
    }
}
