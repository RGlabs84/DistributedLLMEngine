package io.dlne.chat.markdown

/**
 * A deliberately small, safe Markdown subset for model answers.
 *
 * Supported: paragraphs (single line breaks are kept, as chat answers rely on them),
 * `#` headings (shown as bold lines), `**bold**`, `*italic*`, `` `inline code` ``, fenced
 * code blocks (``` or ~~~, also while still streaming without the closing fence), bullet
 * lists (`-`, `*`, `+`, nested by indentation), horizontal rules, and links: `[text](url)`
 * and bare URLs, but only for http and https. Everything else is plain text. There is no
 * HTML, no images and no other link schemes, so an answer can never make the app load or
 * open anything the person did not tap.
 *
 * Citations: `[n]` becomes [MdInline.Citation] only when n is in `citations` (the numbers
 * of the answer's web sources); any other `[n]` stays plain text.
 */
object Markdown {

    fun parse(source: String, citations: Set<Int> = emptySet()): List<MdBlock> {
        val lines = source.replace("\r\n", "\n").replace('\r', '\n').split('\n')
        val blocks = ArrayList<MdBlock>()
        val paragraph = ArrayList<String>()

        fun flushParagraph() {
            if (paragraph.isNotEmpty()) {
                blocks += MdBlock.Paragraph(parseInline(paragraph.joinToString("\n"), citations))
                paragraph.clear()
            }
        }

        var i = 0
        while (i < lines.size) {
            val line = lines[i]

            val fence = FENCE.matchEntire(line)
            if (fence != null) {
                flushParagraph()
                val marker = fence.groupValues[1]
                val language = fence.groupValues[2]
                val code = ArrayList<String>()
                i++
                // Without a closing fence (the answer is still streaming) the block runs to the end.
                while (i < lines.size && !isClosingFence(lines[i], marker)) {
                    code += lines[i]
                    i++
                }
                i++ // the closing fence itself
                blocks += MdBlock.CodeBlock(language, code.joinToString("\n"))
                continue
            }

            if (line.isBlank()) {
                flushParagraph()
                i++
                continue
            }

            if (RULE.matches(line)) {
                flushParagraph()
                blocks += MdBlock.Rule
                i++
                continue
            }

            val heading = HEADING.matchEntire(line)
            if (heading != null) {
                flushParagraph()
                blocks += MdBlock.Heading(parseInline(heading.groupValues[1], citations))
                i++
                continue
            }

            if (BULLET.matches(line)) {
                flushParagraph()
                val items = ArrayList<Pair<Int, StringBuilder>>()
                while (i < lines.size) {
                    val current = lines[i]
                    val bullet = BULLET.matchEntire(current)
                    if (bullet != null && !RULE.matches(current)) {
                        items += indentLevel(bullet.groupValues[1]) to StringBuilder(bullet.groupValues[2])
                    } else if (items.isNotEmpty() && current.isNotBlank() && current.startsWith("  ") &&
                        FENCE.matchEntire(current) == null
                    ) {
                        // An indented line under an item continues that item.
                        items.last().second.append('\n').append(current.trim())
                    } else {
                        break
                    }
                    i++
                }
                blocks += MdBlock.BulletList(
                    items.map { (level, text) -> BulletItem(level, parseInline(text.toString(), citations)) }
                )
                continue
            }

            paragraph += line
            i++
        }
        flushParagraph()
        return blocks
    }

    fun parseInline(text: String, citations: Set<Int> = emptySet()): List<MdInline> {
        val out = ArrayList<MdInline>()
        val plain = StringBuilder()

        fun flushPlain() {
            if (plain.isNotEmpty()) {
                out += MdInline.Text(plain.toString())
                plain.setLength(0)
            }
        }

        var i = 0
        while (i < text.length) {
            val c = text[i]
            when {
                c == '\\' && i + 1 < text.length && text[i + 1] in ESCAPABLE -> {
                    plain.append(text[i + 1])
                    i += 2
                }

                c == '`' -> {
                    val run = runLength(text, i, '`')
                    val close = findCodeClose(text, i + run, run)
                    if (close >= 0) {
                        flushPlain()
                        out += MdInline.Code(trimCodeSpan(text.substring(i + run, close)))
                        i = close + run
                    } else {
                        plain.append(text, i, i + run)
                        i += run
                    }
                }

                text.startsWith("**", i) -> {
                    val close = text.indexOf("**", i + 2)
                    if (close > i + 2) {
                        flushPlain()
                        out += MdInline.Bold(parseInline(text.substring(i + 2, close), citations))
                        i = close + 2
                    } else {
                        plain.append("**")
                        i += 2
                    }
                }

                c == '*' && i + 1 < text.length && !text[i + 1].isWhitespace() -> {
                    val close = findItalicClose(text, i + 1)
                    if (close > 0) {
                        flushPlain()
                        out += MdInline.Italic(parseInline(text.substring(i + 1, close), citations))
                        i = close + 1
                    } else {
                        plain.append(c)
                        i++
                    }
                }

                c == '[' -> {
                    val link = matchLink(text, i) ?: matchCitation(text, i, citations)
                    if (link != null) {
                        flushPlain()
                        out += link.first
                        i = link.second
                    } else {
                        plain.append(c)
                        i++
                    }
                }

                (c == 'h' || c == 'H') && startsBareUrl(text, i) -> {
                    val end = bareUrlEnd(text, i)
                    val url = safeUrl(text.substring(i, end))
                    if (url != null) {
                        flushPlain()
                        out += MdInline.Link(url, url)
                    } else {
                        plain.append(text, i, end)
                    }
                    i = end
                }

                else -> {
                    plain.append(c)
                    i++
                }
            }
        }
        flushPlain()
        return out
    }

    /** Returns [raw] if it is an http(s) URL with a host and no spaces or control characters. */
    fun safeUrl(raw: String): String? {
        val url = raw.trim()
        val schemeLength = when {
            url.startsWith("https://", ignoreCase = true) -> 8
            url.startsWith("http://", ignoreCase = true) -> 7
            else -> return null
        }
        if (url.length <= schemeLength) return null
        if (url.any { it.isWhitespace() || it.isISOControl() }) return null
        val first = url[schemeLength]
        if (first == '/' || first == '?' || first == '#' || first == '@') return null
        return url
    }

    // ---------------------------------------------------------------------------------
    // Helpers

    private val FENCE = Regex("""^ {0,3}(`{3,}|~{3,})\s*([^\s`]*).*$""")
    private val HEADING = Regex("""^ {0,3}#{1,6}\s+(.*?)\s*#*\s*$""")
    private val BULLET = Regex("""^([ \t]*)[-*+][ \t]+(.*)$""")
    private val RULE = Regex("""^ {0,3}([-*_])([ \t]*\1){2,}[ \t]*$""")
    private const val ESCAPABLE = "\\`*_{}[]()#+-.!|~<>"

    private fun isClosingFence(line: String, marker: String): Boolean {
        val trimmed = line.trim()
        return trimmed.length >= marker.length && trimmed.all { it == marker[0] }
    }

    /** Nesting level from leading whitespace: two spaces (or one tab) per level, at most 3. */
    private fun indentLevel(indent: String): Int {
        var width = 0
        for (c in indent) width += if (c == '\t') 4 else 1
        return (width / 2).coerceAtMost(3)
    }

    private fun runLength(text: String, start: Int, char: Char): Int {
        var end = start
        while (end < text.length && text[end] == char) end++
        return end - start
    }

    /** Finds a closing backtick run of exactly [run] backticks, as CommonMark requires. */
    private fun findCodeClose(text: String, from: Int, run: Int): Int {
        var i = from
        while (i < text.length) {
            if (text[i] == '`') {
                val length = runLength(text, i, '`')
                if (length == run) return i
                i += length
            } else {
                i++
            }
        }
        return -1
    }

    /** `` ` a ` `` shows as "a": one space on each side is padding, not content. */
    private fun trimCodeSpan(code: String): String =
        if (code.length >= 2 && code.startsWith(' ') && code.endsWith(' ') && code.isNotBlank()) {
            code.substring(1, code.length - 1)
        } else {
            code
        }

    /** A closing `*` must follow a non-space, must not be part of `**`, and stays on one line. */
    private fun findItalicClose(text: String, from: Int): Int {
        var i = from + 1
        while (i < text.length) {
            val c = text[i]
            if (c == '\n') return -1
            if (c == '*') {
                val partOfDouble = (i + 1 < text.length && text[i + 1] == '*')
                if (!text[i - 1].isWhitespace() && !partOfDouble) return i
                if (partOfDouble) i++
            }
            i++
        }
        return -1
    }

    /**
     * `[label](target)` starting at [start]. Returns the inline and the index after it, or
     * null if this is not link syntax. A target that is not http(s) keeps only the label,
     * as plain text, so `javascript:` and similar can never become tappable.
     */
    private fun matchLink(text: String, start: Int): Pair<MdInline, Int>? {
        val closeBracket = text.indexOf(']', start + 1)
        if (closeBracket < 0 || closeBracket + 1 >= text.length || text[closeBracket + 1] != '(') return null
        val label = text.substring(start + 1, closeBracket)
        if (label.isBlank() || '\n' in label || '[' in label) return null

        // The target ends at the ')' that balances the '(' (URLs may contain parentheses).
        var depth = 0
        var closeParen = -1
        var k = closeBracket + 2
        while (k < text.length) {
            val c = text[k]
            if (c == '\n') return null
            if (c == '(') depth++
            if (c == ')') {
                if (depth == 0) {
                    closeParen = k
                    break
                }
                depth--
            }
            k++
        }
        if (closeParen < 0) return null

        val target = text.substring(closeBracket + 2, closeParen).trim()
            .substringBefore(' ') // drop an optional "title"
            .removePrefix("<").removeSuffix(">")
        val url = safeUrl(target)
        val inline = if (url != null) MdInline.Link(label, url) else MdInline.Text(label)
        return inline to closeParen + 1
    }

    /** `[n]` at [start] with n one of [citations] (at most three digits). */
    private fun matchCitation(text: String, start: Int, citations: Set<Int>): Pair<MdInline, Int>? {
        if (citations.isEmpty()) return null
        var end = start + 1
        while (end < text.length && end - start <= 3 && text[end].isAsciiDigit()) end++
        if (end == start + 1 || end >= text.length || text[end] != ']') return null
        val n = text.substring(start + 1, end).toInt()
        return if (n in citations) MdInline.Citation(n) to end + 1 else null
    }

    private fun Char.isAsciiDigit() = this in '0'..'9'

    private fun startsBareUrl(text: String, i: Int): Boolean {
        val atBoundary = i == 0 || !text[i - 1].isLetterOrDigit()
        return atBoundary && (text.startsWith("https://", i, ignoreCase = true) ||
            text.startsWith("http://", i, ignoreCase = true))
    }

    /** End of a bare URL: whitespace or a delimiter, minus trailing sentence punctuation. */
    private fun bareUrlEnd(text: String, start: Int): Int {
        var end = start
        while (end < text.length && !text[end].isWhitespace() && text[end] !in "<>\"`") end++
        while (end > start) {
            val last = text[end - 1]
            val unbalancedParen = last == ')' &&
                text.substring(start, end).count { it == '(' } < text.substring(start, end).count { it == ')' }
            if (last in ".,;:!?'*_~" || unbalancedParen) end-- else break
        }
        return end
    }
}

/** A block-level element of an answer. */
sealed interface MdBlock {
    data class Paragraph(val inlines: List<MdInline>) : MdBlock
    data class Heading(val inlines: List<MdInline>) : MdBlock
    data class BulletList(val items: List<BulletItem>) : MdBlock
    data class CodeBlock(val language: String, val code: String) : MdBlock
    data object Rule : MdBlock
}

/** One bullet; [level] 0 is the outermost list. */
data class BulletItem(val level: Int, val inlines: List<MdInline>)

/** Text-level styling inside a block. */
sealed interface MdInline {
    data class Text(val text: String) : MdInline
    data class Bold(val children: List<MdInline>) : MdInline
    data class Italic(val children: List<MdInline>) : MdInline
    data class Code(val text: String) : MdInline
    data class Link(val text: String, val url: String) : MdInline

    /** `[n]`: a reference to web source number [n] of this answer. */
    data class Citation(val n: Int) : MdInline
}
