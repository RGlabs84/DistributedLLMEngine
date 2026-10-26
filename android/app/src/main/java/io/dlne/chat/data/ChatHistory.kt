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

    /**
     * - Only the text written by people and the model is sent: service notices are not.
     * - Assistant messages with no text (an answer that never arrived) are left out, and
     *   any resulting run of same-role messages is merged so roles keep alternating, which
     *   some chat templates require.
     * - The history is trimmed from the oldest end to [budgetChars], always keeping the
     *   newest message, and always starts with a user message.
     */
    fun toWire(messages: List<ChatMessage>, budgetChars: Int = DEFAULT_BUDGET_CHARS): List<WireMessage> {
        val merged = ArrayList<WireMessage>(messages.size)
        for (message in messages) {
            val text = message.content.trim()
            if (text.isEmpty()) continue
            val role = when (message.role) {
                Role.USER -> "user"
                Role.ASSISTANT -> "assistant"
            }
            val last = merged.lastOrNull()
            if (last != null && last.role == role) {
                merged[merged.size - 1] = last.copy(content = last.content + "\n\n" + text)
            } else {
                merged += WireMessage(role, text)
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
}
