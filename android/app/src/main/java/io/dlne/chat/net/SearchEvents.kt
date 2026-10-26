package io.dlne.chat.net

import io.dlne.chat.data.Source
import io.dlne.chat.data.takeChars
import io.dlne.chat.markdown.Markdown
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * The service's own stream events, sent in-band as `data: {"dlne": {...}}` lines with no
 * `choices`. Today they report the lookups the DLNE server makes for the model (weather,
 * Wikipedia, news, web search); the phone itself never contacts those services:
 *
 *     {"dlne":{"event":"searching","tool":"weather","query":"Philadelphia"}}
 *     {"dlne":{"event":"sources","sources":[{"n":1,"title":"...","url":"https://...","host":"example.com","age":"2 days ago"}]}}
 *     {"dlne":{"event":"search_note","note":"..."}}
 *     {"dlne":{"event":"search_unavailable","reason":"The search budget for today is used up; ..."}}
 *     {"dlne":{"event":"retract"}}
 *
 * `retract` (server 2.3.5 and later) means the model began answering from memory while
 * unsure, and the server takes that text back to look the question up instead. The usual
 * `searching` and `sources` events follow, then a fresh answer. The app empties the reply
 * being shown, keeps its bubble, and drops any sources it already holds for it (normally
 * none); see [io.dlne.chat.ui.PendingReply.retracted].
 *
 * Titles, hosts and URLs come from arbitrary web pages, so everything is treated as
 * hostile: text is flattened to one plain line without control or bidi-override
 * characters and capped in length, only http(s) URLs survive, and the host shown for a
 * link is always the one the link really goes to. An event this version does not know is
 * dropped, never shown as text.
 */
internal object SearchEvents {
    const val MAX_SOURCES = 20
    const val MAX_SOURCE_NUMBER = 99
    private const val MAX_TITLE = 200
    private const val MAX_HOST = 80
    private const val MAX_AGE = 40
    private const val MAX_TOOL = 40
    private const val MAX_QUERY = 200
    private const val MAX_NOTE = 300
    private const val MAX_URL = 2048

    fun parse(event: JsonObject): StreamEvent? = when (string(event["event"])) {
        "searching" -> StreamEvent.Searching(
            tool = cleanText(string(event["tool"]).orEmpty(), MAX_TOOL).lowercase(),
            query = cleanText(string(event["query"]).orEmpty(), MAX_QUERY),
        )
        "sources" -> parseSources(event["sources"]).takeIf { it.isNotEmpty() }?.let { StreamEvent.Sources(it) }
        "search_note" -> cleanText(string(event["note"]).orEmpty(), MAX_NOTE).takeIf { it.isNotEmpty() }
            ?.let { StreamEvent.SearchNote(it) }
        "search_unavailable" -> StreamEvent.SearchUnavailable(cleanText(string(event["reason"]).orEmpty(), MAX_NOTE))
        "retract" -> StreamEvent.Retract
        else -> null
    }

    /**
     * Valid entries only: a number from 1 to [MAX_SOURCE_NUMBER] that has not been seen yet
     * (so `[n]` in the answer points at exactly one source), at most [MAX_SOURCES] of them.
     */
    fun parseSources(element: JsonElement?): List<Source> {
        val array = element as? JsonArray ?: return emptyList()
        val seen = HashSet<Int>()
        val sources = ArrayList<Source>()
        for (item in array) {
            if (sources.size >= MAX_SOURCES) break
            val entry = item as? JsonObject ?: continue
            val n = number(entry["n"]) ?: continue
            if (n !in 1..MAX_SOURCE_NUMBER || !seen.add(n)) continue

            val url = string(entry["url"])?.trim()?.takeIf { it.length <= MAX_URL }?.let(Markdown::safeUrl)
            // For a link, show where it really goes; the supplied host is used only as text
            // for a source that cannot be opened.
            val host = url?.let(::hostOf) ?: cleanText(string(entry["host"]).orEmpty(), MAX_HOST).ifEmpty { null }
            val title = cleanText(string(entry["title"]).orEmpty(), MAX_TITLE).ifEmpty { host ?: "Source $n" }
            val age = cleanText(string(entry["age"]).orEmpty(), MAX_AGE).ifEmpty { null }
            sources += Source(n = n, title = title, url = url, host = host, age = age)
        }
        return sources
    }

    /**
     * One plain line of at most [maxChars]: line breaks, tabs and other control characters
     * become single spaces; invisible "format" characters (right-to-left overrides, zero-width
     * spaces and joiners) are removed so a title cannot disguise itself.
     */
    fun cleanText(raw: String, maxChars: Int): String {
        val out = StringBuilder(minOf(raw.length, maxChars + 1))
        var pendingSpace = false
        for (char in raw) {
            val type = Character.getType(char)
            when {
                char.isWhitespace() || type == Character.CONTROL.toInt() -> pendingSpace = out.isNotEmpty()
                type == Character.FORMAT.toInt() -> Unit
                else -> {
                    if (pendingSpace) out.append(' ')
                    pendingSpace = false
                    out.append(char)
                }
            }
            if (out.length > maxChars) break
        }
        val text = out.toString()
        return if (text.length > maxChars) text.takeChars(maxChars - 1).trimEnd() + "…" else text
    }

    /**
     * The host a browser would actually open for an http(s) [url], lower-cased and without
     * "www.": userinfo is dropped, so `https://bank.com@evil.example/` shows "evil.example".
     */
    fun hostOf(url: String): String? {
        val authority = url.substringAfter("://").takeWhile { it != '/' && it != '?' && it != '#' && it != '\\' }
        val hostAndPort = authority.substringAfterLast('@')
        val host = if (hostAndPort.startsWith("[")) {
            hostAndPort.substringBefore("]") + "]"
        } else {
            hostAndPort.substringBefore(':')
        }
        return cleanText(host.lowercase().removePrefix("www."), MAX_HOST).ifEmpty { null }
    }

    private fun string(element: JsonElement?): String? =
        (element as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull

    /** A JSON number (or numeric string) that is a whole number. */
    private fun number(element: JsonElement?): Int? {
        val primitive = element as? JsonPrimitive ?: return null
        return primitive.contentOrNull?.trim()?.toIntOrNull()
    }
}
