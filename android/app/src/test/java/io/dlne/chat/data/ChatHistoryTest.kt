package io.dlne.chat.data

import io.dlne.chat.net.WireMessage
import org.junit.Assert.assertEquals
import org.junit.Test

class ChatHistoryTest {
    private var clock = 0L

    private fun user(text: String) = ChatMessage(newId(), Role.USER, text, createdAt = clock++)
    private fun assistant(text: String, notice: String? = null) =
        ChatMessage(newId(), Role.ASSISTANT, text, notice, createdAt = clock++)

    @Test
    fun `notices are not sent, only text`() {
        val wire = ChatHistory.toWire(listOf(user("Hi"), assistant("Hello", notice = "Stopped."), user("More")))
        assertEquals(
            listOf(WireMessage("user", "Hi"), WireMessage("assistant", "Hello"), WireMessage("user", "More")),
            wire,
        )
    }

    @Test
    fun `an answer that never arrived is dropped and the user turns are merged`() {
        val wire = ChatHistory.toWire(
            listOf(user("First try"), assistant("", notice = "No machine is free."), user("Second try"))
        )
        assertEquals(listOf(WireMessage("user", "First try\n\nSecond try")), wire)
    }

    @Test
    fun `history is trimmed from the oldest end and starts with a user turn`() {
        val messages = listOf(user("a".repeat(50)), assistant("b".repeat(50)), user("c".repeat(50)), assistant("d".repeat(50)), user("e".repeat(10)))
        val wire = ChatHistory.toWire(messages, budgetChars = 120)
        // e (10) + d (50) + c (50) = 110 fits; b would exceed. The list must start with a user turn.
        assertEquals(listOf("c".repeat(50), "d".repeat(50), "e".repeat(10)), wire.map { it.content })
        assertEquals("user", wire.first().role)
    }

    @Test
    fun `the newest message is always sent even if it alone exceeds the budget`() {
        val wire = ChatHistory.toWire(listOf(user("old"), assistant("reply"), user("x".repeat(500))), budgetChars = 100)
        assertEquals(listOf(WireMessage("user", "x".repeat(500))), wire)
    }

    @Test
    fun `titles come from the first non-blank line`() {
        assertEquals("How do I bake bread?", titleFrom("\n\n  How do I   bake bread?\nMore detail"))
        assertEquals(48, titleFrom("x".repeat(100)).length)
        assertEquals("New chat", titleFrom("   "))
    }
}
