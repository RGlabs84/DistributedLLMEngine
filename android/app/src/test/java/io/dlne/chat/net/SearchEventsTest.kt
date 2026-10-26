package io.dlne.chat.net

import io.dlne.chat.data.Source
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The service's in-band `{"dlne": ...}` lookup events, including hostile content. */
class SearchEventsTest {

    private fun parse(json: String) = SseEventParser.parse("data: $json")

    private fun sources(entries: String): List<Source> {
        val event = parse("""{"dlne":{"event":"sources","sources":[$entries]}}""")
        return (event as? StreamEvent.Sources)?.sources ?: emptyList()
    }

    private fun events(vararg parts: String): List<StreamEvent> {
        val buffer = SseLineBuffer()
        val lines = parts.flatMap { buffer.feed(it.toByteArray()) } + buffer.finish()
        return lines.mapNotNull(SseEventParser::parse)
    }

    @Test
    fun `searching carries the tool and query`() {
        assertEquals(
            StreamEvent.Searching("weather", "Philadelphia"),
            parse("""{"dlne":{"event":"searching","tool":"weather","query":"Philadelphia"}}"""),
        )
        assertEquals(StreamEvent.Searching("web_search", ""), parse("""{"dlne":{"event":"searching","tool":"Web_Search"}}"""))
        assertEquals(StreamEvent.Searching("", "x"), parse("""{"dlne":{"event":"searching","query":"x"}}"""))
    }

    @Test
    fun `sources, search_note and search_unavailable`() {
        assertEquals(
            StreamEvent.Sources(listOf(Source(1, "Weather today", "https://example.com/w", "example.com", "2 days ago"))),
            parse("""{"dlne":{"event":"sources","sources":[{"n":1,"title":"Weather today","url":"https://example.com/w","host":"example.com","age":"2 days ago"}]}}"""),
        )
        assertEquals(StreamEvent.SearchNote("Results are from Open-Meteo."), parse("""{"dlne":{"event":"search_note","note":"Results are from Open-Meteo."}}"""))
        assertEquals(
            StreamEvent.SearchUnavailable("The search budget for today is used up."),
            parse("""{"dlne":{"event":"search_unavailable","reason":"The search budget for today is used up."}}"""),
        )
        assertEquals(StreamEvent.SearchUnavailable(""), parse("""{"dlne":{"event":"search_unavailable"}}"""))
    }

    @Test
    fun `retract is recognised, whatever else the line carries`() {
        assertEquals(StreamEvent.Retract, parse("""{"dlne":{"event":"retract"}}"""))
        // Extra fields from a newer server are ignored.
        assertEquals(StreamEvent.Retract, parse("""{"dlne":{"event":"retract","reason":"unsure"}}"""))
        // Like every dlne line it is never answer text: nothing from "choices" leaks out.
        assertEquals(
            StreamEvent.Retract,
            parse("""{"dlne":{"event":"retract"},"choices":[{"delta":{"content":"leaked"}}]}"""),
        )
        // Surrounding whitespace and a split data field are handled like any other dlne line.
        assertEquals(StreamEvent.Retract, SseEventParser.parse("""data:   {"dlne":{"event":"retract"}}  """))
    }

    @Test
    fun `a retract in the middle of a stream arrives in order and survives split reads`() {
        // The model starts from memory, the server takes it back and looks the question up.
        val stream = """data: {"choices":[{"delta":{"content":"The mayor is "}}]}""" + "\n\n" +
            """data: {"choices":[{"delta":{"content":"probably Smith."}}]}""" + "\n\n" +
            """data: {"dlne":{"event":"retract"}}""" + "\n\n" +
            """data: {"dlne":{"event":"searching","tool":"web_search","query":"mayor of Springfield"}}""" + "\n\n" +
            """data: {"dlne":{"event":"sources","sources":[{"n":1,"title":"City hall","url":"https://springfield.example/mayor"}]}}""" + "\n\n" +
            """data: {"choices":[{"delta":{"content":"The mayor is Jones [1]."}}]}""" + "\n\n" +
            "data: [DONE]\n\n"
        val whole = events(stream)
        assertEquals(
            listOf(
                StreamEvent.Content("The mayor is "),
                StreamEvent.Content("probably Smith."),
                StreamEvent.Retract,
                StreamEvent.Searching("web_search", "mayor of Springfield"),
                StreamEvent.Sources(listOf(Source(1, "City hall", "https://springfield.example/mayor", "springfield.example", null))),
                StreamEvent.Content("The mayor is Jones [1]."),
                StreamEvent.Done,
            ),
            whole,
        )
        assertEquals(whole, events(*stream.map { it.toString() }.toTypedArray()))
        for (split in 1 until stream.length step 5) {
            assertEquals("split at $split", whole, events(stream.substring(0, split), stream.substring(split)))
        }
    }

    @Test
    fun `unknown or malformed dlne events are dropped and never become text`() {
        val dropped = listOf(
            """{"dlne":{"event":"thinking","text":"secret plan"}}""",
            """{"dlne":{"event":"sources","sources":"not a list"}}""",
            """{"dlne":{"event":"sources","sources":[]}}""",
            """{"dlne":{"event":"search_note","note":"   "}}""",
            """{"dlne":{"event":42}}""",
            """{"dlne":{}}""",
            """{"dlne":"searching"}""",
            """{"dlne":null}""",
            // Even with choices, a dlne line is never answer text.
            """{"dlne":{"event":"new_thing"},"choices":[{"delta":{"content":"leaked"}}]}""",
        )
        for (line in dropped) assertNull("should drop: $line", parse(line))
    }

    @Test
    fun `lookup events split across network reads arrive intact and in order`() {
        val stream = """data: {"dlne":{"event":"searching","tool":"news","query":"super bowl"}}""" + "\n\n" +
            """data: {"dlne":{"event":"sources","sources":[{"n":1,"title":"Report","url":"https://news.example/a","host":"news.example"}]}}""" + "\n\n" +
            """data: {"dlne":{"event":"search_note","note":"Two of three lookups answered."}}""" + "\n\n" +
            """data: {"dlne":{"event":"something_new"}}""" + "\n\n" +
            """data: {"choices":[{"delta":{"content":"It was [1]."}}]}""" + "\n\n" +
            "data: [DONE]\n\n"
        val whole = events(stream)
        assertEquals(
            listOf(
                StreamEvent.Searching("news", "super bowl"),
                StreamEvent.Sources(listOf(Source(1, "Report", "https://news.example/a", "news.example", null))),
                StreamEvent.SearchNote("Two of three lookups answered."),
                StreamEvent.Content("It was [1]."),
                StreamEvent.Done,
            ),
            whole,
        )
        // One byte per read, and every two-way split, give exactly the same events.
        assertEquals(whole, events(*stream.map { it.toString() }.toTypedArray()))
        for (split in 1 until stream.length step 7) {
            assertEquals("split at $split", whole, events(stream.substring(0, split), stream.substring(split)))
        }
    }

    @Test
    fun `hostile titles become one plain capped line`() {
        val list = sources(
            """
            {"n":1,"title":"Safe‮gnp.exe​ title","url":"https://a.example"},
            {"n":2,"title":"Line one\nLine two\r\n\tTabbed   spaces","url":"https://b.example"},
            {"n":3,"title":"<script>alert(1)</script> **bold** [x](javascript:alert(1))","url":"https://c.example"},
            {"n":4,"title":"${"A".repeat(500)}","url":"https://d.example"},
            {"n":5,"title":"   ","url":"https://e.example/page"},
            {"n":6}
            """
        )
        assertEquals("Safegnp.exe title", list[0].title) // bidi override and zero-width space removed
        assertEquals("Line one Line two Tabbed spaces", list[1].title)
        // Markup stays literal text: titles are never rendered as Markdown or HTML.
        assertEquals("<script>alert(1)</script> **bold** [x](javascript:alert(1))", list[2].title)
        assertEquals(200, list[3].title.length)
        assertTrue(list[3].title.endsWith("…"))
        assertEquals("e.example", list[4].title) // blank title falls back to the host
        assertEquals("Source 6", list[5].title)
        assertNull(list[5].url)
    }

    @Test
    fun `only http and https URLs survive, and the host shown is where the link goes`() {
        val list = sources(
            """
            {"n":1,"title":"js","url":"javascript:alert(1)","host":"trusted.example"},
            {"n":2,"title":"data","url":"data:text/html,<b>hi</b>"},
            {"n":3,"title":"file","url":"file:///sdcard/secret"},
            {"n":4,"title":"intent","url":"intent://scan/#Intent;scheme=zxing;end"},
            {"n":5,"title":"spoof","url":"https://bank.example@evil.example/login","host":"bank.example"},
            {"n":6,"title":"caps","url":"HTTPS://WWW.Example.COM:8443/a?b=c"},
            {"n":7,"title":"spaces","url":"https://a.example/with space"},
            {"n":8,"title":"long","url":"https://a.example/${"x".repeat(3000)}"},
            {"n":9,"title":"number","url":12345},
            {"n":10,"title":"empty host","url":"https:///path"},
            {"n":11,"title":"plain http","url":"http://old.example/page"}
            """
        )
        val byN = list.associateBy { it.n }
        for (n in listOf(1, 2, 3, 4, 7, 8, 9, 10)) assertNull("source $n must not be a link", byN.getValue(n).url)
        assertEquals("trusted.example", byN.getValue(1).host) // supplied host is only text when there is no link
        assertEquals("https://bank.example@evil.example/login", byN.getValue(5).url)
        assertEquals("evil.example", byN.getValue(5).host) // not the supplied "bank.example"
        assertEquals("example.com", byN.getValue(6).host)
        assertEquals("http://old.example/page", byN.getValue(11).url)
    }

    @Test
    fun `invalid and duplicate source numbers are dropped, and the list is capped`() {
        val list = sources(
            """
            {"n":0,"title":"zero"}, {"n":-1,"title":"negative"}, {"n":100,"title":"too big"},
            {"n":"x","title":"not a number"}, {"n":1.5,"title":"fraction"}, {"title":"missing"},
            "not an object", {"n":"2","title":"numeric string"}, {"n":3,"title":"first three"},
            {"n":3,"title":"second three"}
            """
        )
        assertEquals(listOf(2 to "numeric string", 3 to "first three"), list.map { it.n to it.title })

        val many = (1..30).joinToString(",") { """{"n":$it,"title":"t$it","url":"https://s$it.example"}""" }
        assertEquals(SearchEvents.MAX_SOURCES, sources(many).size)
    }

    @Test
    fun `hostOf ignores userinfo, ports and www`() {
        assertEquals("evil.example", SearchEvents.hostOf("https://user:pw@evil.example:444/x"))
        assertEquals("example.org", SearchEvents.hostOf("https://www.example.org"))
        assertEquals("[2001:db8::1]", SearchEvents.hostOf("https://[2001:db8::1]:8080/"))
    }
}
