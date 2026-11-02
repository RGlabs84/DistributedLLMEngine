package io.dlne.chat.voice

/**
 * What Conversation mode says aloud (docs/voice-contract.md, "Conversation mode"). Pure text work, shared
 * test vectors in SpeechTextTest. Nothing here touches Android.
 */
const val SPEAK_CODE = "There's code in the chat."
const val SPEAK_TABLE = "There's a table in the chat."
const val SPEAK_PICTURE = "I made a picture. It's in the chat."

private val HEADING = Regex("^#{1,6}\\s+")
private val QUOTE = Regex("^(>\\s?)+")
private val BULLET = Regex("^[-*+]\\s+")
private val NUMBERED = Regex("^\\d+[.)]\\s+")
private val RULE = Regex("^([-*_])(\\s*\\1){2,}$")
private val IMAGE = Regex("!\\[([^\\]]*)]\\([^)]*\\)")
private val LINK = Regex("\\[([^\\]]+)]\\([^)]*\\)")
private val CITATION = Regex("\\[\\d+]")
private val URL = Regex("https?://\\S*[^\\s.,;:!?)\\]\"']")
private val UNDERSCORES = Regex("(?<!\\w)_+|_+(?!\\w)")
private val SPACE_BEFORE_PUNCT = Regex("\\s+([.,;:!?])")
private val SPACES = Regex("\\s+")

private fun cleanLine(raw: String): String {
    var s = raw.trim()
    s = QUOTE.replace(s, "").trim()
    s = HEADING.replace(s, "")
    s = BULLET.replace(s, "")
    s = NUMBERED.replace(s, "")
    s = IMAGE.replace(s) { it.groupValues[1] }
    s = CITATION.replace(s, "")
    s = LINK.replace(s) { it.groupValues[1] }
    s = URL.replace(s, "")
    s = s.replace("`", "").replace("*", "").replace("~~", "")
    s = UNDERSCORES.replace(s, "")
    s = SPACES.replace(s, " ").trim()
    return SPACE_BEFORE_PUNCT.replace(s, "$1")
}

/**
 * The speech text of [markdown]: one line per paragraph, list item, code block or table (a line is always a
 * sentence boundary). Code blocks become [SPEAK_CODE] once per block, tables [SPEAK_TABLE] once per table.
 */
fun speakable(markdown: String): String {
    val out = ArrayList<String>()
    var inFence = false
    var fenceMark = ""
    var inTable = false
    for (raw in markdown.replace("\r\n", "\n").split('\n')) {
        val t = raw.trim()
        if (inFence) {
            if (t.startsWith(fenceMark)) inFence = false
            continue
        }
        val mark = if (t.startsWith("```")) "```" else if (t.startsWith("~~~")) "~~~" else null
        if (mark != null) {
            inFence = true
            fenceMark = mark
            inTable = false
            out += SPEAK_CODE
            continue
        }
        if (t.startsWith("|")) {
            if (!inTable) out += SPEAK_TABLE
            inTable = true
            continue
        }
        inTable = false
        if (RULE.matches(t)) continue
        val line = cleanLine(t)
        if (line.any { it.isLetterOrDigit() }) out += line
    }
    return out.joinToString("\n")
}

private val ABBREVIATIONS = setOf("e.g", "i.e", "vs", "mr", "mrs", "ms", "dr", "prof", "approx", "cf", "etc")

private fun isAbbreviation(wordBeforeDot: String): Boolean {
    val w = wordBeforeDot.trimStart('(', '"', '\'').lowercase()
    if (w in ABBREVIATIONS) return true
    if (w.length == 1 && w[0].isLetter()) return true // an initial: "J. Smith"
    return Regex("^(\\w\\.)+\\w$").matches(w)
}

private fun splitLine(line: String, lineEnded: Boolean): List<String> {
    val parts = ArrayList<String>()
    var start = 0
    var i = 0
    while (i < line.length) {
        val c = line[i]
        if (c == '.' || c == '!' || c == '?') {
            var end = i + 1
            while (end < line.length && (line[end] == '.' || line[end] == '!' || line[end] == '?')) end++
            while (end < line.length && line[end] in "\"')]") end++
            if (end < line.length && line[end].isWhitespace()) {
                val word = line.substring(start, i).substringAfterLast(' ')
                if (!(c == '.' && end == i + 1 && isAbbreviation(word))) {
                    parts += line.substring(start, end).trim()
                    start = end
                }
            }
            i = end
        } else {
            i++
        }
    }
    val tail = line.substring(start).trim()
    if (tail.isNotEmpty() && lineEnded) parts += tail
    return parts.filter { it.isNotEmpty() }
}

/**
 * The complete sentences of [markdown]. A sentence is complete once something follows its end mark (a space or
 * a new line); the last one is complete only when [final] (the answer is over).
 */
fun speakableSentences(markdown: String, final: Boolean = true): List<String> {
    val lines = speakable(markdown).split('\n').filter { it.isNotEmpty() }
    // Without a trailing newline the last line of a streaming answer is still being written.
    val lastOpen = !final && !markdown.endsWith("\n")
    return lines.flatMapIndexed { index, line ->
        // The marker lines are whole sentences once their line is whole.
        splitLine(line, lineEnded = !(lastOpen && index == lines.lastIndex))
    }
}

/**
 * Hands out the sentences of a streaming answer as they become complete, each once. [feed] takes the whole
 * answer so far (and [final] at the end) and returns only the sentences not given before.
 */
class SpeechFeed {
    private var given = 0

    /** The answer started over (the server retracted it): the next [feed] begins at the fresh answer's first sentence. */
    fun reset() {
        given = 0
    }

    fun feed(answerSoFar: String, final: Boolean = false): List<String> {
        val all = speakableSentences(answerSoFar, final)
        if (all.size <= given) return emptyList()
        val fresh = all.subList(given, all.size).toList()
        given = all.size
        return fresh
    }
}
