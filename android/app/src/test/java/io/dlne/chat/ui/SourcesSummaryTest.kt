package io.dlne.chat.ui

import io.dlne.chat.data.Source
import java.io.File
import java.util.Locale
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Test
import org.w3c.dom.Element

/**
 * The folded line an answer's sources collapse into ("10 sources · patriots.com,
 * nytimes.com, bostonglobe.com +3"). The count wording is read from the app's own
 * strings.xml plurals, so these lines are the ones shown on screen.
 */
class SourcesSummaryTest {
    private fun source(n: Int, host: String?) =
        Source(n, "Page $n", host?.let { "https://$it/page-$n" }, host, null)

    /** R.plurals.sources_count as English picks it: "one" for 1, "other" otherwise. */
    private val countPlurals: Map<String, String> by lazy {
        val strings = File(System.getProperty("user.dir")!!, "src/main/res/values/strings.xml")
        val doc = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(strings)
        val all = doc.getElementsByTagName("plurals")
        val plurals = (0 until all.length).map { all.item(it) as Element }.single { it.getAttribute("name") == "sources_count" }
        val items = plurals.getElementsByTagName("item")
        (0 until items.length).map { items.item(it) as Element }.associate { it.getAttribute("quantity") to it.textContent }
    }

    private fun line(sources: List<Source>): String {
        val summary = sourcesSummary(sources)
        val countText = String.format(Locale.ROOT, countPlurals.getValue(if (summary.count == 1) "one" else "other"), summary.count)
        return summary.text(countText)
    }

    @Test
    fun `one source says source in the singular`() {
        assertEquals("1 source · cityhall.example", line(listOf(source(1, "cityhall.example"))))
    }

    @Test
    fun `three sources on three hosts name them all`() {
        val sources = listOf(source(1, "patriots.com"), source(2, "nytimes.com"), source(3, "bostonglobe.com"))
        assertEquals("3 sources · patriots.com, nytimes.com, bostonglobe.com", line(sources))
        assertEquals(0, sourcesSummary(sources).moreHosts)
    }

    @Test
    fun `ten sources on six distinct hosts name the first three and count the rest`() {
        val hosts = listOf(
            "patriots.com", "nytimes.com", "patriots.com", "bostonglobe.com", "espn.com",
            "nytimes.com", "nfl.com", "espn.com", "si.com", "patriots.com",
        )
        val sources = hosts.mapIndexed { i, host -> source(i + 1, host) }
        val summary = sourcesSummary(sources)
        assertEquals(10, summary.count)
        assertEquals(listOf("patriots.com", "nytimes.com", "bostonglobe.com"), summary.hosts)
        assertEquals(3, summary.moreHosts)
        assertEquals("10 sources · patriots.com, nytimes.com, bostonglobe.com +3", line(sources))
    }

    @Test
    fun `a source without a host is counted but names nothing`() {
        assertEquals("2 sources · weather.example", line(listOf(source(1, null), source(2, "weather.example"))))
        assertEquals("1 source", line(listOf(source(1, null))))
        assertEquals("", sourcesSummary(listOf(source(1, null), source(2, "  "))).hostsSuffix)
    }

    @Test
    fun `hosts beyond three but no repeats still show the remainder`() {
        val sources = listOf("a.example", "b.example", "c.example", "d.example").mapIndexed { i, h -> source(i + 1, h) }
        assertEquals("4 sources · a.example, b.example, c.example +1", line(sources))
    }
}
