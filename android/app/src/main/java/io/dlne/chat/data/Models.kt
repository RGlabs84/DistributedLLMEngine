package io.dlne.chat.data

import java.util.UUID
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Who wrote a message. Serialized with the same names the chat API uses. */
@Serializable
enum class Role {
    @SerialName("user")
    USER,

    @SerialName("assistant")
    ASSISTANT,
}

/**
 * One web page the answer was written from, as listed by the service's `sources` event.
 * The answer cites it as `[n]`.
 *
 * Every field is already cleaned when this is built (see `io.dlne.chat.net.SearchEvents`):
 * [title], [host] and [age] are plain single-line text, and [url] is either an http(s)
 * address or null. A null [url] means the source is shown but cannot be opened.
 */
@Serializable
data class Source(
    val n: Int,
    val title: String,
    val url: String? = null,
    val host: String? = null,
    val age: String? = null,
)

/**
 * One message in a conversation.
 *
 * [notice] holds a line from the DLNE service itself (for example "No machine is free right
 * now" or "Stopped") rather than from the model. It is shown under the message but never
 * sent back to the model as part of the conversation.
 *
 * [sources] are the pages an answer cites (from the server's lookups: weather, Wikipedia,
 * news, web search). [searchNotes] are the service's short remarks about those lookups:
 * a `search_note`, or why a lookup could not be made (`search_unavailable`). Both are kept
 * with the saved chat and, like notices, never sent back to the model. Older saved chats
 * simply have none.
 */
@Serializable
data class ChatMessage(
    val id: String,
    val role: Role,
    val content: String,
    val notice: String? = null,
    val createdAt: Long,
    val sources: List<Source> = emptyList(),
    val searchNotes: List<String> = emptyList(),
)

/** A conversation as stored on the phone. Messages are in the order they were written. */
@Serializable
data class Conversation(
    val id: String,
    val title: String,
    val createdAt: Long,
    val updatedAt: Long,
    val messages: List<ChatMessage>,
)

/** The whole history file. [version] allows the format to change later without data loss. */
@Serializable
data class ConversationFile(
    val version: Int = 1,
    val conversations: List<Conversation> = emptyList(),
)

fun newId(): String = UUID.randomUUID().toString()

/**
 * The first [maxChars] UTF-16 units of this string, never ending on half of a surrogate
 * pair (which would turn an emoji at the cut into an invalid character).
 */
fun String.takeChars(maxChars: Int): String {
    if (length <= maxChars) return this
    val end = if (maxChars > 0 && this[maxChars - 1].isHighSurrogate()) maxChars - 1 else maxChars
    return substring(0, end)
}

/** A short title from the first message: its first line, at most [maxLength] characters. */
fun titleFrom(text: String, maxLength: Int = 48): String {
    val firstLine = text.trim().lineSequence().firstOrNull { it.isNotBlank() }.orEmpty()
    val collapsed = firstLine.replace(Regex("\\s+"), " ").trim()
    return when {
        collapsed.isEmpty() -> "New chat"
        collapsed.length <= maxLength -> collapsed
        else -> collapsed.take(maxLength - 1).trimEnd() + "…"
    }
}
