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

    @Test
    fun `image placeholders never reach the chat model`() {
        val request = ChatMessage(newId(), Role.USER, imagePlaceholder("a red fox"), createdAt = clock++, image = ImageAttachment("a red fox"))
        val result = ChatMessage(newId(), Role.ASSISTANT, imagePlaceholder("a red fox"), createdAt = clock++, image = ImageAttachment("a red fox", "x.png", 768, 768))
        val refused = ChatMessage(newId(), Role.ASSISTANT, "", "Not that.", clock++, image = ImageAttachment("no"))
        val wire = ChatHistory.toWire(listOf(user("Hi"), assistant("Hello"), request, result, user("Thanks"), refused))
        assertEquals(
            listOf(WireMessage("user", "Hi"), WireMessage("assistant", "Hello"), WireMessage("user", "Thanks")),
            wire,
        )
        assertEquals(emptyList<WireMessage>(), ChatHistory.toWire(listOf(request, result)))
    }

    private fun reply(text: String, picture: ReplyPicture?, notice: String? = null) =
        ChatMessage(newId(), Role.ASSISTANT, text, notice, createdAt = clock++, picture = picture)

    @Test
    fun `a reply that made a picture ends with the picture line, so make it bluer can reuse the description`() {
        val wire = ChatHistory.toWire(
            listOf(
                user("can I get a picture of a red fox"),
                reply("A fox is on its way.", ReplyPicture("a red fox in the snow, soft light, wide shot")),
                user("make it bluer"),
            ),
        )
        assertEquals(
            listOf(
                WireMessage("user", "can I get a picture of a red fox"),
                WireMessage("assistant", "A fox is on its way.\n\n[Picture created: a red fox in the snow, soft light, wide shot]"),
                WireMessage("user", "make it bluer"),
            ),
            wire,
        )
    }

    @Test
    fun `the picture line is there while the picture is being made and after it arrived`() {
        val line = "\n\n[Picture created: a red fox]"
        for (picture in listOf(ReplyPicture("a red fox"), ReplyPicture("a red fox", "f.png", 768, 768))) {
            val wire = ChatHistory.toWire(listOf(user("a fox please"), reply("Coming up.", picture)))
            assertEquals("Coming up.$line", wire.last().content)
        }
    }

    @Test
    fun `a refused, failed or cancelled picture adds nothing`() {
        val replies = listOf(
            ReplyPicture.refusal("Your free trial is used up."),
            ReplyPicture("a red fox").failed("Image cancelled."),
            ReplyPicture("a red fox").failed("The image couldn't be made. Please try again."),
        )
        for (picture in replies) {
            val wire = ChatHistory.toWire(listOf(user("a fox please"), reply("I couldn't make it.", picture)))
            assertEquals("I couldn't make it.", wire.last().content)
        }
    }

    @Test
    fun `a reply with only a picture says just the picture line`() {
        val wire = ChatHistory.toWire(listOf(user("a fox please"), reply("", ReplyPicture("a red fox")), user("thanks")))
        assertEquals(
            listOf(WireMessage("user", "a fox please"), WireMessage("assistant", "[Picture created: a red fox]"), WireMessage("user", "thanks")),
            wire,
        )
        // A refusal with no text at all says nothing, so the two user turns are one.
        val refused = ChatHistory.toWire(listOf(user("a fox please"), reply("", ReplyPicture.refusal("No.")), user("thanks")))
        assertEquals(listOf(WireMessage("user", "a fox please\n\nthanks")), refused)
    }

    @Test
    fun `a notice never goes with the picture line, and replies without pictures are as before`() {
        val wire = ChatHistory.toWire(
            listOf(
                user("a fox please"),
                reply("Stand by.", ReplyPicture("a red fox"), notice = "Stopped."),
                user("and a cat"),
                reply("A cat, then.", null),
            ),
        )
        assertEquals("Stand by.\n\n[Picture created: a red fox]", wire[1].content)
        assertEquals("A cat, then.", wire[3].content)
    }

    @Test
    fun `a saved chat from before images still reads`() {
        val json = kotlinx.serialization.json.Json { ignoreUnknownKeys = true }
        val old = """{"id":"a","role":"user","content":"hi","createdAt":1}"""
        assertEquals(null, json.decodeFromString(ChatMessage.serializer(), old).image)
    }
}
