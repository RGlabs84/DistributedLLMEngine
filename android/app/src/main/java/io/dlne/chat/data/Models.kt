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
 *
 * [image] marks a message of an image request or result; such a message is shown but never
 * sent to the model as part of the conversation.
 *
 * [picture] is the picture a reply made when the person just asked the chat for one (see
 * [ReplyPicture]). Unlike [image], such a reply is an ordinary answer: its text is sent back to
 * the model, followed by a `[Picture created: ...]` line.
 *
 * [photos] are the photos the person attached to a message of their own (see [Photo]). They are
 * part of what the person asked, so unlike [image] they do go to the model, by the rules in
 * [ChatHistory.toWire].
 *
 * [photosRefused] is set when a request that carried this message's photos was turned down for
 * them (the hour's photo allowance, a photo the server will not take). Later requests then send
 * those photos as `[photo]` marks instead of pictures, so the refusal does not follow every
 * message after it; Retry clears it, being the person's own choice to send them again.
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
    /** Set on the messages of a "Create image" request: the placeholder is never sent to the chat model. */
    val image: ImageAttachment? = null,
    /** The picture this reply made (or why it made none), shown inside the reply. */
    val picture: ReplyPicture? = null,
    /** Photos attached to this user message, at most [PhotoEncoding.MAX_PHOTOS]; chats saved before photos have none. */
    val photos: List<Photo> = emptyList(),
    /** The server refused this message's photos; they are sent as marks, not pictures, until Retry. */
    val photosRefused: Boolean = false,
)

/** These messages with the photos of [messageId] marked refused, or allowed again (the person tried again). Others are left as they are. */
fun List<ChatMessage>.withPhotosRefused(messageId: String, refused: Boolean): List<ChatMessage> =
    map { if (it.id == messageId && it.photosRefused != refused) it.copy(photosRefused = refused) else it }

/**
 * A photo the person attached to a message: the JPEG that was sent, kept in the app's private
 * storage (see [ImageStore]) for as long as the chat is. [file] is its name there; [width] and
 * [height] are its pixels.
 */
@Serializable
data class Photo(
    val file: String,
    val width: Int = 0,
    val height: Int = 0,
)

/**
 * The image part of a conversation. The text of such a message is the placeholder
 * `[Image: <prompt>]` (see [imagePlaceholder]); [file] names the PNG in the app's private
 * storage (see [ImageStore]) and is null on the request itself and when no image was made.
 */
@Serializable
data class ImageAttachment(
    val prompt: String,
    val file: String? = null,
    val width: Int = 0,
    val height: Int = 0,
)

fun imagePlaceholder(prompt: String): String = "[Image: $prompt]"

/**
 * A conversation as stored on the phone. Messages are in the order they were written.
 * [locationDeclined]: asked to share the location in this chat, the person said "Not now" (or Android
 * refused it), so the chat does not ask again. Another chat still asks.
 */
@Serializable
data class Conversation(
    val id: String,
    val title: String,
    val createdAt: Long,
    val updatedAt: Long,
    val messages: List<ChatMessage>,
    val locationDeclined: Boolean = false,
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
