package io.dlne.chat.markdown

import io.dlne.chat.markdown.MdInline.Bold
import io.dlne.chat.markdown.MdInline.Code
import io.dlne.chat.markdown.MdInline.Italic
import io.dlne.chat.markdown.MdInline.Link
import io.dlne.chat.markdown.MdInline.Text
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class MarkdownTest {

    @Test
    fun `blank lines separate paragraphs and single line breaks are kept`() {
        assertEquals(
            listOf(
                MdBlock.Paragraph(listOf(Text("First line\nsecond line"))),
                MdBlock.Paragraph(listOf(Text("Next paragraph"))),
            ),
            Markdown.parse("First line\nsecond line\n\n\nNext paragraph"),
        )
    }

    @Test
    fun `bold, italic and inline code`() {
        assertEquals(
            listOf(Text("a "), Bold(listOf(Text("bold"))), Text(" and "), Italic(listOf(Text("it"))), Text(" and "), Code("x = 1")),
            Markdown.parseInline("a **bold** and *it* and `x = 1`"),
        )
    }

    @Test
    fun `code spans protect their content and support double backticks`() {
        assertEquals(listOf(Code("**not bold**")), Markdown.parseInline("`**not bold**`"))
        assertEquals(listOf(Code("a ` b")), Markdown.parseInline("`` a ` b ``"))
    }

    @Test
    fun `bold may contain code and links`() {
        assertEquals(
            listOf(Bold(listOf(Text("run "), Code("make")))),
            Markdown.parseInline("**run `make`**"),
        )
    }

    @Test
    fun `unclosed markers stay literal text`() {
        assertEquals(listOf(Text("**unfinished")), Markdown.parseInline("**unfinished"))
        assertEquals(listOf(Text("`open")), Markdown.parseInline("`open"))
        assertEquals(listOf(Text("2 * 3 * 4 = 24")), Markdown.parseInline("2 * 3 * 4 = 24"))
        assertEquals(listOf(Text("a * b")), Markdown.parseInline("a * b"))
    }

    @Test
    fun `backslash escapes markers`() {
        assertEquals(listOf(Text("*not italic*")), Markdown.parseInline("\\*not italic\\*"))
    }

    @Test
    fun `snake_case and dunder names are left alone`() {
        assertEquals(listOf(Text("call __init__ on my_var_name")), Markdown.parseInline("call __init__ on my_var_name"))
    }

    @Test
    fun `fenced code block keeps content verbatim with its language`() {
        val source = "Here:\n```kotlin\nval x = \"**not bold**\"\n\n// blank line above\n```\nAfter."
        assertEquals(
            listOf(
                MdBlock.Paragraph(listOf(Text("Here:"))),
                MdBlock.CodeBlock("kotlin", "val x = \"**not bold**\"\n\n// blank line above"),
                MdBlock.Paragraph(listOf(Text("After."))),
            ),
            Markdown.parse(source),
        )
    }

    @Test
    fun `an unterminated fence (still streaming) runs to the end`() {
        assertEquals(
            listOf(MdBlock.CodeBlock("", "line 1\nline 2")),
            Markdown.parse("```\nline 1\nline 2"),
        )
    }

    @Test
    fun `tilde fences work too`() {
        assertEquals(listOf(MdBlock.CodeBlock("sh", "ls -la")), Markdown.parse("~~~sh\nls -la\n~~~"))
    }

    @Test
    fun `bullet lists with dash, star and plus, nested by indentation`() {
        val parsed = Markdown.parse("Intro\n- one\n* two **bold**\n  + nested\n    continued\n- three\n\nAfter")
        assertEquals(
            listOf(
                MdBlock.Paragraph(listOf(Text("Intro"))),
                MdBlock.BulletList(
                    listOf(
                        BulletItem(0, listOf(Text("one"))),
                        BulletItem(0, listOf(Text("two "), Bold(listOf(Text("bold"))))),
                        BulletItem(1, listOf(Text("nested\ncontinued"))),
                        BulletItem(0, listOf(Text("three"))),
                    )
                ),
                MdBlock.Paragraph(listOf(Text("After"))),
            ),
            parsed,
        )
    }

    @Test
    fun `a line starting with bold is not a bullet`() {
        assertEquals(
            listOf(MdBlock.Paragraph(listOf(Bold(listOf(Text("Note:"))), Text(" text")))),
            Markdown.parse("**Note:** text"),
        )
    }

    @Test
    fun `headings and rules`() {
        assertEquals(
            listOf(
                MdBlock.Heading(listOf(Text("Title"))),
                MdBlock.Rule,
                MdBlock.Heading(listOf(Text("Sub "), Code("x"))),
            ),
            Markdown.parse("# Title\n---\n### Sub `x` ###"),
        )
    }

    @Test
    fun `http and https links become links`() {
        assertEquals(
            listOf(Text("See "), Link("the docs", "https://example.org/a?b=c"), Text(".")),
            Markdown.parseInline("See [the docs](https://example.org/a?b=c)."),
        )
        assertEquals(
            listOf(Link("wiki", "https://en.wikipedia.org/wiki/Foo_(bar)")),
            Markdown.parseInline("[wiki](https://en.wikipedia.org/wiki/Foo_(bar))"),
        )
        assertEquals(
            listOf(Link("x", "http://example.org")),
            Markdown.parseInline("[x](http://example.org \"title\")"),
        )
    }

    @Test
    fun `bare URLs are linked without trailing punctuation`() {
        assertEquals(
            listOf(Text("Go to "), Link("https://dlne.io/x", "https://dlne.io/x"), Text(". Then")),
            Markdown.parseInline("Go to https://dlne.io/x. Then"),
        )
        assertEquals(
            listOf(Text("("), Link("https://a.b/c", "https://a.b/c"), Text(")")),
            Markdown.parseInline("(https://a.b/c)"),
        )
    }

    @Test
    fun `other schemes never become links`() {
        assertEquals(listOf(Text("click")), Markdown.parseInline("[click](javascript:alert(1))"))
        assertEquals(listOf(Text("file")), Markdown.parseInline("[file](file:///etc/passwd)"))
        assertEquals(listOf(Text("mail")), Markdown.parseInline("[mail](mailto:a@b.c)"))
        assertEquals(listOf(Text("x")), Markdown.parseInline("[x](intent://scan/#Intent;end)"))
        assertEquals(listOf(Text("ftp://example.org and data:text/html,hi")), Markdown.parseInline("ftp://example.org and data:text/html,hi"))
        // Not at a word boundary: not a URL.
        assertEquals(listOf(Text("xhttps://a.b")), Markdown.parseInline("xhttps://a.b"))
    }

    @Test
    fun `safeUrl accepts only http and https with a host`() {
        assertEquals("https://a.b", Markdown.safeUrl("https://a.b"))
        assertEquals("HTTP://A.B/x", Markdown.safeUrl("HTTP://A.B/x"))
        assertNull(Markdown.safeUrl("https://"))
        assertNull(Markdown.safeUrl("https:///path"))
        assertNull(Markdown.safeUrl("javascript:alert(1)"))
        assertNull(Markdown.safeUrl("https://a.b/with space"))
        assertNull(Markdown.safeUrl("https://a.b/\u0000"))
    }

    @Test
    fun `brackets that are not links stay text`() {
        assertEquals(listOf(Text("[1] and [note] here")), Markdown.parseInline("[1] and [note] here"))
    }

    @Test
    fun `windows line endings`() {
        assertEquals(
            listOf(MdBlock.Paragraph(listOf(Text("a\nb"))), MdBlock.Paragraph(listOf(Text("c")))),
            Markdown.parse("a\r\nb\r\n\r\nc"),
        )
    }

    @Test
    fun `empty input has no blocks`() {
        assertEquals(emptyList<MdBlock>(), Markdown.parse(""))
        assertEquals(emptyList<MdBlock>(), Markdown.parse("\n\n  \n"))
    }
}
