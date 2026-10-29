package io.dlne.chat.data

import io.dlne.chat.net.WireMessage

/** Builds the `messages` list sent to the server from a stored conversation. */
object ChatHistory {
    /**
     * Roughly 3,000 tokens of history. The volunteer machines run a model with an 8k-token
     * window per request and answers can use 1,024 of it; slow CPUs also read a shorter
     * prompt much sooner. The newest messages are kept, the oldest dropped first.
     */
    const val DEFAULT_BUDGET_CHARS = 12_000

    /** What stands in a message's text for a photo that is not sent as a picture (contract 2.2). */
    const val PHOTO_MARK = "[photo]"

    /**
     * - Only the text written by people and the model is sent: service notices are not, and
     *   neither is anything about an image request (the `[Image: ...]` placeholders). A reply that
     *   made a picture ends with `[Picture created: <prompt>]` (see [textWithPicture]).
     * - Assistant messages with no text (an answer that never arrived) are left out, and
     *   any resulting run of same-role messages is merged so roles keep alternating, which
     *   some chat templates require.
     * - Photos: only the newest message of the person's that has photos sends them, as pictures
     *   (the `data:` URLs [photoUrl] gives; null means the file cannot be read). Every other photo,
     *   in older messages or unreadable, is sent as the text [PHOTO_MARK], one per photo after that
     *   message's text. A message may be photos alone. Photos never count against [budgetChars],
     *   which counts text.
     * - Photos the server refused ([ChatMessage.photosRefused]) are marks too, so a refusal does not
     *   come back with every message after it and text still goes through. That message still counts
     *   as the newest with photos: an older one does not send its pictures in its place.
     * - The history is trimmed from the oldest end to [budgetChars], always keeping the
     *   newest message, and always starts with a user message.
     */
    fun toWire(
        messages: List<ChatMessage>,
        budgetChars: Int = DEFAULT_BUDGET_CHARS,
        photoUrl: (Photo) -> String? = { null },
    ): List<WireMessage> {
        val newestWithPhotos = messages.indexOfLast { it.role == Role.USER && it.image == null && it.photos.isNotEmpty() }
        val merged = ArrayList<WireMessage>(messages.size)
        for ((index, message) in messages.withIndex()) {
            if (message.image != null) continue
            var text = message.textWithPicture()
            val pictures = ArrayList<String>()
            if (message.role == Role.USER && message.photos.isNotEmpty()) {
                var marks = 0
                for (photo in message.photos) {
                    val url = if (index == newestWithPhotos && !message.photosRefused) photoUrl(photo) else null
                    if (url != null) pictures += url else marks++
                }
                if (marks > 0) text = joinParagraphs(text, List(marks) { PHOTO_MARK }.joinToString(" "))
            }
            if (text.isEmpty() && pictures.isEmpty()) continue
            val role = when (message.role) {
                Role.USER -> "user"
                Role.ASSISTANT -> "assistant"
            }
            val last = merged.lastOrNull()
            if (last != null && last.role == role) {
                merged[merged.size - 1] = last.copy(content = joinParagraphs(last.content, text), images = last.images + pictures)
            } else {
                merged += WireMessage(role, text, pictures)
            }
        }

        val kept = ArrayDeque<WireMessage>()
        var used = 0
        for (message in merged.asReversed()) {
            if (kept.isNotEmpty() && used + message.content.length > budgetChars) break
            kept.addFirst(message)
            used += message.content.length
        }
        while (kept.isNotEmpty() && kept.first().role != "user") kept.removeFirst()
        return kept.toList()
    }

    /**
     * The id of the message whose photos a request sends as pictures: the newest of the person's that
     * has photos (see [toWire]), or null when no message has any. When the server refuses photos it is
     * this message that gets [ChatMessage.photosRefused].
     */
    fun photoTurnId(messages: List<ChatMessage>): String? =
        messages.lastOrNull { it.role == Role.USER && it.image == null && it.photos.isNotEmpty() }?.id

    /** Two pieces of text as paragraphs; a message that is photos alone has no text to put a gap after. */
    private fun joinParagraphs(first: String, second: String): String = when {
        first.isEmpty() -> second
        second.isEmpty() -> first
        else -> first + "\n\n" + second
    }
}
