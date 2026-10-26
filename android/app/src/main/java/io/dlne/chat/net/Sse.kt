package io.dlne.chat.net

import io.dlne.chat.data.Source
import java.io.ByteArrayOutputStream
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * One thing the chat stream told us. The stream is OpenAI-style Server-Sent Events:
 * `data: {json}` lines carrying text in `choices[0].delta.content`, ended by `data: [DONE]`.
 */
sealed interface StreamEvent {
    /** The next piece of the answer, to be appended as-is. */
    data class Content(val text: String) : StreamEvent

    /**
     * A message from the DLNE service itself rather than from the model, e.g.
     * "No machine in the CPU pool is free right now." The service sends these in-band as
     * content wrapped in square brackets; [text] has the brackets removed.
     */
    data class Notice(val text: String) : StreamEvent

    /** The stream carried an `error` object instead of an answer. */
    data class Failure(val message: String) : StreamEvent

    /**
     * The server is running a lookup for the model: [tool] is `weather`, `wikipedia`,
     * `news`, `web_search` or something newer (lower-case, may be empty), [query] what it
     * looks up (may be empty). An answer can make several; the latest one is shown.
     */
    data class Searching(val tool: String, val query: String) : StreamEvent

    /**
     * Every source so far, cited in the answer as `[n]`. May arrive more than once, each
     * time with the whole list, so it replaces the previous list.
     */
    data class Sources(val sources: List<Source>) : StreamEvent

    /** A short remark from the service about its lookups, shown under the sources. */
    data class SearchNote(val note: String) : StreamEvent

    /**
     * A lookup was not possible (e.g. the day's budget is used up); [reason] says why and
     * may be empty. The answer comes from the model's memory.
     */
    data class SearchUnavailable(val reason: String) : StreamEvent

    /**
     * The model began answering from memory while unsure, and the server takes that text
     * back to look the question up instead. The reply so far is cleared (the bubble stays,
     * and any sources it held are dropped); `searching`, `sources` and a fresh answer follow.
     */
    data object Retract : StreamEvent

    /** `data: [DONE]`: the answer is complete. */
    data object Done : StreamEvent
}

/**
 * Turns raw bytes from the network into complete text lines.
 *
 * A network read can end anywhere: in the middle of a `data:` line, between the `\r` and
 * `\n` of a line ending, or even inside a multi-byte UTF-8 character. Bytes are therefore
 * collected until a line ending arrives and only then decoded, so a split character is
 * never decoded in two halves. (A `\n` or `\r` byte can never occur inside a multi-byte
 * UTF-8 sequence, so splitting on them at the byte level is safe.)
 *
 * Line endings follow the SSE spec: `\n`, `\r\n` or a lone `\r`.
 */
class SseLineBuffer(private val maxLineBytes: Int = DEFAULT_MAX_LINE_BYTES) {
    private val current = ByteArrayOutputStream()
    private var lastWasCarriageReturn = false

    /** True while skipping the rest of a line that grew past [maxLineBytes]. */
    private var discarding = false

    /** Adds [length] bytes of [bytes] from [offset] and returns every line they completed. */
    fun feed(bytes: ByteArray, offset: Int = 0, length: Int = bytes.size - offset): List<String> {
        val lines = ArrayList<String>(2)
        for (index in offset until offset + length) {
            when (val byte = bytes[index]) {
                CR -> {
                    endLine(lines)
                    lastWasCarriageReturn = true
                }
                LF -> {
                    // The \n of a \r\n pair: the line already ended at the \r.
                    if (!lastWasCarriageReturn) endLine(lines)
                    lastWasCarriageReturn = false
                }
                else -> {
                    lastWasCarriageReturn = false
                    if (!discarding) {
                        current.write(byte.toInt())
                        // A line this long is not a chat event; drop it rather than buffer
                        // without limit (a broken or hostile server could send one forever).
                        if (current.size() > maxLineBytes) {
                            current.reset()
                            discarding = true
                        }
                    }
                }
            }
        }
        return lines
    }

    /** The stream ended. A final line that never got its line ending is still a line. */
    fun finish(): List<String> {
        val lines = ArrayList<String>(1)
        if (current.size() > 0) endLine(lines)
        discarding = false
        return lines
    }

    private fun endLine(into: MutableList<String>) {
        if (discarding) {
            discarding = false
        } else {
            into += String(current.toByteArray(), Charsets.UTF_8)
        }
        current.reset()
    }

    private companion object {
        const val CR: Byte = '\r'.code.toByte()
        const val LF: Byte = '\n'.code.toByte()
        const val DEFAULT_MAX_LINE_BYTES = 1 shl 20 // 1 MiB
    }
}

/**
 * Interprets one SSE line. Anything that is not a well-formed `data:` line carrying text
 * (comments, `event:`/`id:` fields, blank separators, broken JSON, role-only deltas) returns
 * null and is ignored, so one bad line never ends an otherwise good answer.
 */
object SseEventParser {
    private val json = Json { ignoreUnknownKeys = true }

    fun parse(line: String): StreamEvent? {
        if (!line.startsWith("data:")) return null
        val payload = line.substring("data:".length).trim()
        if (payload.isEmpty()) return null
        if (payload == "[DONE]") return StreamEvent.Done

        val root = try {
            json.parseToJsonElement(payload) as? JsonObject
        } catch (_: SerializationException) {
            null
        } catch (_: IllegalArgumentException) {
            null
        } ?: return null

        // The service's own events ({"dlne": {...}}, no "choices"). Whatever is in such a
        // line, it is never answer text: an unknown or malformed event is simply dropped.
        if ("dlne" in root) return (root["dlne"] as? JsonObject)?.let(SearchEvents::parse)

        errorMessage(root)?.let { return StreamEvent.Failure(it) }

        val choice = (root["choices"] as? JsonArray)?.firstOrNull() as? JsonObject ?: return null
        val delta = choice["delta"] as? JsonObject ?: return null
        val content = (delta["content"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        if (content.isNullOrEmpty()) return null
        val finishReason = (choice["finish_reason"] as? JsonPrimitive)?.takeIf { it.isString }?.content

        return if (isServiceNotice(content, finishReason)) {
            StreamEvent.Notice(content.trim().removePrefix("[").removeSuffix("]").trim())
        } else {
            StreamEvent.Content(content)
        }
    }

    /**
     * The DLNE service writes its own notices as a final content chunk of the form
     * `"\n\n[Some sentence.]"` with `finish_reason: "stop"` (engine/app_pool.py `_sse_notice`).
     * A chunk counts as a notice only when its whole text is one bracketed sentence AND it
     * either ends the answer or starts with a line break; ordinary model text such as a
     * Markdown link `[a](b)` or a list `[1, [2]]` mid-answer stays content.
     */
    internal fun isServiceNotice(content: String, finishReason: String?): Boolean {
        val text = content.trim()
        if (text.length < 3 || !text.startsWith('[') || !text.endsWith(']')) return false
        // Exactly one bracket pair, wrapping everything.
        if (text.indexOf('[', 1) >= 0 || text.indexOf(']') != text.length - 1) return false
        return finishReason == "stop" || content.startsWith("\n")
    }

    private fun errorMessage(root: JsonObject): String? {
        val error = root["error"] ?: return null
        return when (error) {
            is JsonPrimitive -> error.contentOrNull
            is JsonObject -> (error["message"] as? JsonPrimitive)?.contentOrNull ?: GENERIC_ERROR
            else -> GENERIC_ERROR
        }
    }

    private const val GENERIC_ERROR = "The service reported an error."
}
