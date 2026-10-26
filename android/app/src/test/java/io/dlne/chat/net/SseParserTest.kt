package io.dlne.chat.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SseParserTest {

    private fun chunk(content: String, finish: String? = null): String {
        val escaped = content.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")
        val finishJson = if (finish == null) "null" else "\"$finish\""
        return """data: {"choices":[{"index":0,"delta":{"content":"$escaped"},"finish_reason":$finishJson}]}"""
    }

    /** Feeds [parts] as separate network reads and parses every completed line. */
    private fun events(vararg parts: ByteArray): List<StreamEvent> {
        val buffer = SseLineBuffer()
        val lines = parts.flatMap { buffer.feed(it) } + buffer.finish()
        return lines.mapNotNull(SseEventParser::parse)
    }

    private fun events(vararg parts: String) = events(*parts.map { it.toByteArray() }.toTypedArray())

    @Test
    fun `a data line split across reads is reassembled`() {
        val line = chunk("Hello, world") + "\n\n"
        // Every possible split point, including inside "data:" and inside the JSON.
        for (split in 1 until line.length) {
            val result = events(line.substring(0, split), line.substring(split))
            assertEquals("split at $split", listOf(StreamEvent.Content("Hello, world")), result)
        }
    }

    @Test
    fun `many tiny reads produce the same events as one big read`() {
        val stream = chunk("Hel") + "\n\n" + chunk("lo") + "\n\n" + "data: [DONE]\n\n"
        val oneByteAtATime = events(*stream.map { it.toString() }.toTypedArray())
        assertEquals(
            listOf(StreamEvent.Content("Hel"), StreamEvent.Content("lo"), StreamEvent.Done),
            oneByteAtATime,
        )
    }

    @Test
    fun `a multi-byte UTF-8 character split between reads is decoded intact`() {
        val bytes = (chunk("café 🚀") + "\n").toByteArray(Charsets.UTF_8)
        val rocket = bytes.indexOfFirst { it == 0xF0.toByte() } // first byte of the 4-byte emoji
        val result = events(bytes.copyOfRange(0, rocket + 2), bytes.copyOfRange(rocket + 2, bytes.size))
        assertEquals(listOf(StreamEvent.Content("café 🚀")), result)
    }

    @Test
    fun `CRLF, LF and lone CR line endings all work, including CRLF split across reads`() {
        val result = events(chunk("a") + "\r", "\n" + chunk("b") + "\n" + chunk("c") + "\r" + "data: [DONE]\r\n")
        assertEquals(
            listOf(StreamEvent.Content("a"), StreamEvent.Content("b"), StreamEvent.Content("c"), StreamEvent.Done),
            result,
        )
    }

    @Test
    fun `DONE is recognised with or without a space after data colon`() {
        assertEquals(StreamEvent.Done, SseEventParser.parse("data: [DONE]"))
        assertEquals(StreamEvent.Done, SseEventParser.parse("data:[DONE]"))
    }

    @Test
    fun `the service notice format is reported as a notice, without brackets`() {
        // Exactly what engine/app_pool.py _sse_notice() sends.
        val stream = chunk("\n\n[No machine in the CPU pool is free right now. Please try again in a moment.]", "stop") +
            "\n\ndata: [DONE]\n\n"
        assertEquals(
            listOf(
                StreamEvent.Notice("No machine in the CPU pool is free right now. Please try again in a moment."),
                StreamEvent.Done,
            ),
            events(stream),
        )
    }

    @Test
    fun `a notice after part of an answer keeps the answer`() {
        val stream = chunk("The answer starts") + "\n\n" +
            chunk("\n\n[The machine answering this dropped out. Please send your message again.]", "stop") + "\n\n" +
            "data: [DONE]\n\n"
        val result = events(stream)
        assertEquals(StreamEvent.Content("The answer starts"), result[0])
        assertTrue(result[1] is StreamEvent.Notice)
        assertEquals(StreamEvent.Done, result[2])
    }

    @Test
    fun `bracketed model text mid-answer stays content`() {
        assertEquals(StreamEvent.Content("[1, 2, 3]"), SseEventParser.parse(chunk("[1, 2, 3]")))
        assertEquals(StreamEvent.Content("[docs](https://x.org)"), SseEventParser.parse(chunk("[docs](https://x.org)", "stop")))
        assertEquals(StreamEvent.Content("[a] and [b]"), SseEventParser.parse(chunk("[a] and [b]", "stop")))
    }

    @Test
    fun `malformed and irrelevant lines are ignored`() {
        val ignored = listOf(
            "",
            ": keep-alive comment",
            "event: message",
            "id: 42",
            "retry: 1000",
            "data:",
            "data: {not json",
            "data: \"just a string\"",
            "data: []",
            "data: {}",
            """data: {"choices":[]}""",
            """data: {"choices":[{"delta":{}}]}""",
            """data: {"choices":[{"delta":{"role":"assistant"}}]}""",
            """data: {"choices":[{"delta":{"content":""}}]}""",
            """data: {"choices":[{"delta":{"content":null}}]}""",
            """data: {"choices":[{"delta":{"content":42}}]}""",
            """data: {"choices":"nope"}""",
            "garbage line",
        )
        for (line in ignored) assertNull("should ignore: $line", SseEventParser.parse(line))

        // ...and they do not disturb the good lines around them.
        val stream = ": ping\n" + "data: {broken\n" + chunk("ok") + "\n" + "event: x\n" + "data: [DONE]\n"
        assertEquals(listOf(StreamEvent.Content("ok"), StreamEvent.Done), events(stream))
    }

    @Test
    fun `an in-stream error object becomes a failure`() {
        assertEquals(
            StreamEvent.Failure("model crashed"),
            SseEventParser.parse("""data: {"error":{"message":"model crashed","code":500}}"""),
        )
        assertEquals(StreamEvent.Failure("plain text error"), SseEventParser.parse("""data: {"error":"plain text error"}"""))
    }

    @Test
    fun `a final line without a newline is still delivered`() {
        val buffer = SseLineBuffer()
        assertEquals(emptyList<String>(), buffer.feed("data: [DONE]".toByteArray()))
        assertEquals(listOf("data: [DONE]"), buffer.finish())
    }

    @Test
    fun `an absurdly long line is dropped without disturbing the next one`() {
        val buffer = SseLineBuffer(maxLineBytes = 64)
        val lines = buffer.feed(("data: " + "x".repeat(500) + "\n" + "data: [DONE]\n").toByteArray())
        assertEquals(listOf("data: [DONE]"), lines)
    }

    @Test
    fun `whitespace and newlines inside content are preserved exactly`() {
        assertEquals(StreamEvent.Content("  indented\n"), SseEventParser.parse(chunk("  indented\n")))
        assertEquals(StreamEvent.Content(" "), SseEventParser.parse(chunk(" ")))
    }
}
