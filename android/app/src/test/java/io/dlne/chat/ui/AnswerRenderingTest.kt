package io.dlne.chat.ui

import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.style.BaselineShift
import io.dlne.chat.R
import io.dlne.chat.markdown.Markdown
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * How an answer's text is styled: citation superscripts, which of them are tappable, and
 * the lookup status wording. Runs on the JVM against the real annotate() used on screen.
 */
class AnswerRenderingTest {
    private val opened = mutableListOf<String>()
    private val styles = InlineStyles(
        code = SpanStyle(),
        link = TextLinkStyles(SpanStyle()),
        citation = SpanStyle(baselineShift = BaselineShift.Superscript),
        onLink = { opened += it },
    )

    private fun render(text: String, urls: Map<Int, String?>): AnnotatedString =
        annotate(Markdown.parseInline(text, urls.keys), styles, urls)

    private fun AnnotatedString.range(marker: String): IntRange {
        val start = text.indexOf(marker)
        assertTrue("$marker missing from \"$text\"", start >= 0)
        return start until start + marker.length
    }

    @Test
    fun `a citation with a web address is a tappable superscript`() {
        val text = render("Paris is sunny [1].", mapOf(1 to "https://weather.example/paris"))
        assertEquals("Paris is sunny [1].", text.text)
        val range = text.range("[1]")
        val link = text.getLinkAnnotations(range.first, range.last + 1).single()
        assertEquals(range.first, link.start)
        assertEquals(range.last + 1, link.end)
        val url = link.item as LinkAnnotation.Url
        assertEquals("https://weather.example/paris", url.url)
        assertEquals(BaselineShift.Superscript, url.styles?.style?.baselineShift)

        // Tapping goes through the app's own opener, never straight to the system.
        url.linkInteractionListener!!.onClick(url)
        assertEquals(listOf("https://weather.example/paris"), opened)
    }

    @Test
    fun `a citation without a usable address is a plain superscript`() {
        val text = render("A [1] and B [2].", mapOf(1 to null, 2 to "javascript:alert(1)"))
        for (marker in listOf("[1]", "[2]")) {
            val range = text.range(marker)
            assertTrue("$marker must not be a link", text.getLinkAnnotations(range.first, range.last + 1).isEmpty())
            assertTrue(
                "$marker must be raised",
                text.spanStyles.any {
                    it.start == range.first && it.end == range.last + 1 && it.item.baselineShift == BaselineShift.Superscript
                },
            )
        }
    }

    @Test
    fun `a number that is not a source is ordinary text`() {
        val text = render("See [3].", mapOf(1 to "https://a.example"))
        assertEquals("See [3].", text.text)
        assertTrue(text.spanStyles.isEmpty())
        assertTrue(text.getLinkAnnotations(0, text.length).isEmpty())
    }

    @Test
    fun `lookup status wording follows the tool`() {
        assertEquals(R.string.lookup_weather to R.string.lookup_weather_no_query, lookupStrings("weather"))
        assertEquals(R.string.lookup_wikipedia to R.string.lookup_wikipedia_no_query, lookupStrings("wikipedia"))
        assertEquals(R.string.lookup_news to R.string.lookup_news_no_query, lookupStrings("news"))
        assertEquals(R.string.lookup_web to R.string.lookup_web_no_query, lookupStrings("web_search"))
        assertEquals(R.string.lookup_other to R.string.lookup_other_no_query, lookupStrings("stock_prices"))
        assertEquals(R.string.lookup_other to R.string.lookup_other_no_query, lookupStrings(""))
    }
}
