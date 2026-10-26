package io.dlne.chat.data

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** Why someone reports an answer. Serialized as the API's `reason` values. */
@Serializable
enum class ReportReason {
    @SerialName("harmful")
    HARMFUL,

    @SerialName("wrong")
    WRONG,

    @SerialName("offensive")
    OFFENSIVE,

    @SerialName("other")
    OTHER,
}

/** The body of `POST /v1/app/report`. Nothing identifies the person reporting. */
@Serializable
data class ReportRequest(
    val question: String,
    val answer: String,
    val reason: ReportReason,
    val note: String,
)

/**
 * Builds a report for one answer (Google Play's AI-generated content policy asks for a way
 * to flag answers from inside the app). Only that answer and the question it answered are
 * sent, never the rest of the conversation.
 */
object Reports {
    const val MAX_QUESTION_CHARS = 4_000
    const val MAX_ANSWER_CHARS = 8_000
    const val MAX_NOTE_CHARS = 500

    /**
     * The report for the assistant message [answerId] in [messages], or null if there is no
     * such answer or it has no text. The question is the user's message right before it; if
     * the person sent several in a row (say, after an attempt that got no answer), the
     * answer replied to all of them, so they are joined.
     */
    fun build(messages: List<ChatMessage>, answerId: String, reason: ReportReason, note: String): ReportRequest? {
        val index = messages.indexOfFirst { it.id == answerId }
        if (index < 0) return null
        val answer = messages[index]
        if (answer.role != Role.ASSISTANT || answer.content.isBlank()) return null

        val questionParts = ArrayDeque<String>()
        var i = index - 1
        while (i >= 0) {
            val message = messages[i]
            if (message.role == Role.ASSISTANT && message.content.isNotBlank()) break // an earlier answer
            if (message.role == Role.USER && message.content.isNotBlank()) questionParts.addFirst(message.content.trim())
            i--
        }

        return ReportRequest(
            question = questionParts.joinToString("\n\n").takeChars(MAX_QUESTION_CHARS),
            answer = answer.content.trim().takeChars(MAX_ANSWER_CHARS),
            reason = reason,
            note = note.trim().takeChars(MAX_NOTE_CHARS),
        )
    }
}
