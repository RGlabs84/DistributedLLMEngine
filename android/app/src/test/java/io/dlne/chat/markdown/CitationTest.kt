package io.dlne.chat.markdown

import io.dlne.chat.markdown.MdInline.Bold
import io.dlne.chat.markdown.MdInline.Citation
import io.dlne.chat.markdown.MdInline.Code
import io.dlne.chat.markdown.MdInline.Link
import io.dlne.chat.markdown.MdInline.Text
import org.junit.Assert.assertEquals
import org.junit.Test

/** `[n]` source markers: recognised only for the answer's own source numbers. */
class CitationTest {
    private val known = setOf(1, 2, 12)

    @Test
    fun `known numbers become citations`() {
        assertEquals(
            listOf(Text("Rain today"), Citation(1), Text(" and wind"), Citation(2), Citation(12), Text(".")),
            Markdown.parseInline("Rain today[1] and wind[2][12].", known),
        )
    }

    @Test
    fun `numbers outside the list stay plain text`() {
        assertEquals(listOf(Text("See [3] and [0] and [1234]")), Markdown.parseInline("See [3] and [0] and [1234]", known))
        assertEquals(listOf(Text("No sources [1]")), Markdown.parseInline("No sources [1]"))
    }

    @Test
    fun `links, code and formatting keep their meaning`() {
        assertEquals(listOf(Link("1", "https://x.example")), Markdown.parseInline("[1](https://x.example)", known))
        assertEquals(listOf(Code("arr[1]")), Markdown.parseInline("`arr[1]`", known))
        assertEquals(listOf(Bold(listOf(Text("see "), Citation(2)))), Markdown.parseInline("**see [2]**", known))
        assertEquals(listOf(Text("[ 1] [1 ] [a]")), Markdown.parseInline("[ 1] [1 ] [a]", known))
    }

    @Test
    fun `citations work in every block type but not in code blocks`() {
        val blocks = Markdown.parse("# Weather [1]\n- wind [2]\n\nText [1]\n```\nlist[1]\n```", known)
        assertEquals(
            listOf(
                MdBlock.Heading(listOf(Text("Weather "), Citation(1))),
                MdBlock.BulletList(listOf(BulletItem(0, listOf(Text("wind "), Citation(2))))),
                MdBlock.Paragraph(listOf(Text("Text "), Citation(1))),
                MdBlock.CodeBlock("", "list[1]"),
            ),
            blocks,
        )
    }
}
