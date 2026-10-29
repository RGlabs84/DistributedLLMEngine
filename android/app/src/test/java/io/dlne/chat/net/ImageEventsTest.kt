package io.dlne.chat.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The chat's own picture events (`image_job`, `image_refused`), which the server sends only to a client that asks for them. */
class ImageEventsTest {

    private fun parse(json: String) = SseEventParser.parse("data: $json")

    private fun job(fields: String) = parse("""{"dlne":{"event":"image_job",$fields}}""")

    private fun events(vararg parts: String): List<StreamEvent> {
        val buffer = SseLineBuffer()
        val lines = parts.flatMap { buffer.feed(it.toByteArray()) } + buffer.finish()
        return lines.mapNotNull(SseEventParser::parse)
    }

    @Test
    fun `a job carries its id, description, size, place in line and estimate`() {
        assertEquals(
            StreamEvent.ImageJob("abc_DEF-1", "a red fox in the snow", "768x1024", 2, 111),
            job(""""id":"abc_DEF-1","prompt":"a red fox in the snow","size":"768x1024","position":2,"eta_seconds":111"""),
        )
    }

    @Test
    fun `place and estimate may be null, missing, whole or fractional`() {
        assertEquals(
            StreamEvent.ImageJob("j1", "fox", "768x768", null, null),
            job(""""id":"j1","prompt":"fox","size":"768x768","position":null,"eta_seconds":null"""),
        )
        assertEquals(StreamEvent.ImageJob("j1", "fox", null, null, null), job(""""id":"j1","prompt":"fox""""))
        // The server sends whole numbers, but 80 and 80.0 are the same figure.
        assertEquals(StreamEvent.ImageJob("j1", "fox", null, 0, 81), job(""""id":"j1","prompt":"fox","position":0.0,"eta_seconds":80.6"""))
        assertEquals(StreamEvent.ImageJob("j1", "fox", null, 3, null), job(""""id":"j1","prompt":"fox","position":"3""""))
    }

    @Test
    fun `figures that are not believable are left out, the job is kept`() {
        for (bad in listOf("-1", "1e12", "\"soon\"", "true", "[1]", "{}", "\"NaN\"", "\"Infinity\"")) {
            val event = job(""""id":"j1","prompt":"fox","position":$bad,"eta_seconds":$bad""") as StreamEvent.ImageJob
            assertNull("position $bad", event.position)
            assertNull("eta $bad", event.etaSeconds)
        }
    }

    @Test
    fun `a job needs an id of the shape the server makes`() {
        for (id in listOf("\"\"", "\" \"", "\"..\"", "\"a/b\"", "\"a b\"", "\"a?b=c\"", "\"é\"", "\"${"x".repeat(65)}\"", "42", "null", "[\"j1\"]")) {
            assertNull("id $id", job(""""id":$id,"prompt":"fox""""))
        }
        assertNull(job(""""prompt":"fox""""))
        // What secrets.token_urlsafe(16) makes is accepted, whole.
        val id = "Zm9vYmFyX2Jhei1fMTIzNA"
        assertEquals(id, (job(""""id":"$id","prompt":"fox"""") as StreamEvent.ImageJob).id)
    }

    @Test
    fun `a job with no description is still followed, and a size that is not a size is dropped`() {
        assertEquals("a picture", (job(""""id":"j1"""") as StreamEvent.ImageJob).prompt)
        assertEquals("a picture", (job(""""id":"j1","prompt":"  \n ","size":"huge"""") as StreamEvent.ImageJob).prompt)
        for (size in listOf("huge", "768", "768x", "x768", "76800x76800", "768*768", "768x768; rm", "")) {
            assertNull("size '$size'", (job(""""id":"j1","prompt":"fox","size":"$size"""") as StreamEvent.ImageJob).size)
        }
    }

    @Test
    fun `the description is one plain line and at most the server's own limit`() {
        // U+202E is a right-to-left override and U+200B a zero-width space: neither may reach the screen.
        val hostile = job(""""id":"j1","prompt":"a fox${"‮"}gnp.exe${"​"} <b>bold</b>\nline two\r\n\tTabbed   spaces"""") as StreamEvent.ImageJob
        assertEquals("a foxgnp.exe <b>bold</b> line two Tabbed spaces", hostile.prompt) // bidi override and zero-width space removed
        val long = job(""""id":"j1","prompt":"${"A".repeat(1500)}"""") as StreamEvent.ImageJob
        assertEquals(MAX_IMAGE_PROMPT_CHARS, long.prompt.length)
        assertTrue(long.prompt.endsWith("…"))
    }

    @Test
    fun `a refusal carries the server's code and words`() {
        assertEquals(
            StreamEvent.ImageRefused("image_quota", "Your free trial is used up. Pictures stay free on any network where a computer contributes to DLNE (now or in the last day)."),
            parse("""{"dlne":{"event":"image_refused","code":"image_quota","detail":"Your free trial is used up. Pictures stay free on any network where a computer contributes to DLNE (now or in the last day)."}}"""),
        )
    }

    @Test
    fun `a refusal with no words, or no code, is still a refusal`() {
        assertEquals(StreamEvent.ImageRefused("", ""), parse("""{"dlne":{"event":"image_refused"}}"""))
        assertEquals(StreamEvent.ImageRefused("prompt_refused", ""), parse("""{"dlne":{"event":"image_refused","code":"prompt_refused","detail":42}}"""))
        assertEquals(StreamEvent.ImageRefused("", "No."), parse("""{"dlne":{"event":"image_refused","code":null,"detail":"No."}}"""))
    }

    @Test
    fun `a refusal's text is one plain line, capped`() {
        val event = parse("""{"dlne":{"event":"image_refused","code":"x\ny","detail":"I can't${"‮"} make\nthat   one.\t${"B".repeat(600)}"}}""") as StreamEvent.ImageRefused
        assertEquals("x y", event.code)
        assertTrue(event.detail.startsWith("I can't make that one. BBB"))
        assertEquals(400, event.detail.length)
        assertTrue(event.detail.endsWith("…"))
    }

    @Test
    fun `picture events are never answer text, whatever else the line carries`() {
        assertEquals(
            StreamEvent.ImageJob("j1", "fox", null, null, null),
            parse("""{"dlne":{"event":"image_job","id":"j1","prompt":"fox"},"choices":[{"delta":{"content":"leaked"}}]}"""),
        )
        // A line that is dropped for a bad id does not turn into text either.
        assertNull(parse("""{"dlne":{"event":"image_job","id":"../x","prompt":"fox"},"choices":[{"delta":{"content":"leaked"}}]}"""))
        assertNull(parse("""{"dlne":{"event":"image_job"}}"""))
        assertNull(parse("""{"dlne":{"event":"image_jobs"}}"""))
    }

    @Test
    fun `a job arrives before the reply's text and survives any split of the network reads`() {
        val stream = """data: {"dlne":{"event":"image_job","id":"abc_DEF-1","prompt":"a red fox","size":"768x768","position":1,"eta_seconds":80}}""" + "\n\n" +
            """data: {"choices":[{"delta":{"content":"A fox is on its way."}}]}""" + "\n\n" +
            "data: [DONE]\n\n"
        val whole = events(stream)
        assertEquals(
            listOf(
                StreamEvent.ImageJob("abc_DEF-1", "a red fox", "768x768", 1, 80),
                StreamEvent.Content("A fox is on its way."),
                StreamEvent.Done,
            ),
            whole,
        )
        assertEquals(whole, events(*stream.map { it.toString() }.toTypedArray()))
        for (split in 1 until stream.length step 7) {
            assertEquals("split at $split", whole, events(stream.substring(0, split), stream.substring(split)))
        }
    }

    @Test
    fun `a refusal and a lookup can share a reply`() {
        val stream = """data: {"dlne":{"event":"searching","tool":"weather","query":"Paris"}}""" + "\n\n" +
            """data: {"dlne":{"event":"image_refused","code":"image_busy","detail":"Lots of people are making pictures right now. Please try again in a moment."}}""" + "\n\n" +
            """data: {"choices":[{"delta":{"content":"I couldn't make it just now."}}]}""" + "\n\n" +
            "data: [DONE]\n\n"
        assertEquals(
            listOf(
                StreamEvent.Searching("weather", "Paris"),
                StreamEvent.ImageRefused("image_busy", "Lots of people are making pictures right now. Please try again in a moment."),
                StreamEvent.Content("I couldn't make it just now."),
                StreamEvent.Done,
            ),
            events(stream),
        )
    }
}
