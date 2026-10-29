package io.dlne.chat.data

import kotlinx.serialization.Serializable

/**
 * The picture a reply made with the chat's own picture tool: when someone just asks for a picture,
 * the model calls `create_image` and the server starts a job (the `image_job` stream event). This is
 * not the Create image dialog, whose requests and pictures are messages of their own (see
 * [ImageAttachment]); this one lives inside the reply that made it ([ChatMessage.picture]).
 *
 * It is in one of four states:
 *  - being made: [created] and no [file] yet. While the app runs, a job card shows it. If the app has
 *    no job for it any more (it was closed meanwhile), the reply says the picture was not finished;
 *  - made: [file] names the PNG in the app's private storage (see [ImageStore]);
 *  - failed after it started (cancelled, refused by the picture check, expired, contact lost): [note]
 *    says why, in the server's own words when it had some;
 *  - not started ([refused], from `image_refused`): [note] is the server's reason, [prompt] is empty.
 */
@Serializable
data class ReplyPicture(
    val prompt: String = "",
    val file: String? = null,
    val width: Int = 0,
    val height: Int = 0,
    val note: String? = null,
    val refused: Boolean = false,
) {
    /** The server started a picture for this reply and nothing has gone wrong with it so far. */
    val created: Boolean get() = !refused && note == null

    /** Started and not finished: it is being made, or its job was lost. */
    val pending: Boolean get() = created && file == null

    fun made(file: String, width: Int, height: Int): ReplyPicture =
        copy(file = file, width = width, height = height, note = null)

    fun failed(note: String): ReplyPicture = copy(file = null, note = note)

    companion object {
        /** The picture was not started; [detail] is why. */
        fun refusal(detail: String): ReplyPicture = ReplyPicture(note = detail, refused = true)
    }
}

/**
 * This conversation with the picture of the reply [replyId] replaced by [change] of the current one
 * (null when it has none), and [Conversation.updatedAt] set to [now]; null when there is no such reply.
 */
fun Conversation.withReplyPicture(replyId: String, now: Long, change: (ReplyPicture?) -> ReplyPicture): Conversation? {
    if (messages.none { it.id == replyId }) return null
    return copy(updatedAt = now, messages = messages.map { if (it.id == replyId) it.copy(picture = change(it.picture)) else it })
}

/** The line a reply that made a picture ends with in the conversation sent to the server. */
fun pictureLine(prompt: String): String = "[Picture created: $prompt]"

/**
 * What this message says in the conversation the server reads: its text and, for a reply that made
 * a picture, the line `[Picture created: <prompt>]` after a blank line, so that "make it bluer"
 * can reuse the description. A picture that was refused, failed or was cancelled adds nothing.
 */
fun ChatMessage.textWithPicture(): String {
    val line = picture?.takeIf { it.created }?.let { pictureLine(it.prompt) }
    return listOfNotNull(content.trim().takeIf { it.isNotEmpty() }, line).joinToString("\n\n")
}
