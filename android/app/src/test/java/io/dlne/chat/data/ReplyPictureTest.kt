package io.dlne.chat.data

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The picture a reply made with the chat's own picture tool: its states, where it is kept, and old chats. */
class ReplyPictureTest {
    private var clock = 0L
    private fun user(text: String) = ChatMessage("u${clock}", Role.USER, text, createdAt = clock++)
    private fun reply(id: String, text: String, picture: ReplyPicture? = null) =
        ChatMessage(id, Role.ASSISTANT, text, createdAt = clock++, picture = picture)

    // The same settings as ConversationStore.
    private val storeJson = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    @Test
    fun `a picture counts as created until something goes wrong with it`() {
        val making = ReplyPicture("a red fox")
        assertTrue(making.created)
        assertTrue(making.pending)

        val made = making.made("f.png", 768, 1024)
        assertTrue(made.created)
        assertFalse(made.pending)
        assertEquals("f.png", made.file)
        assertEquals(768 to 1024, made.width to made.height)

        val failed = making.failed("Image cancelled.")
        assertFalse(failed.created)
        assertFalse(failed.pending)
        assertNull(failed.file)
        assertEquals("Image cancelled.", failed.note)

        val refused = ReplyPicture.refusal("Your free trial is used up.")
        assertFalse(refused.created)
        assertFalse(refused.pending)
        assertTrue(refused.refused)
        assertEquals("", refused.prompt)
    }

    @Test
    fun `a picture that arrives after a failed note clears the note`() {
        assertEquals(ReplyPicture("fox", "f.png", 1, 1), ReplyPicture("fox").failed("Lost contact").made("f.png", 1, 1))
    }

    @Test
    fun `only a reply that made a picture says so in the conversation`() {
        assertEquals("Hi.", reply("a", "  Hi.  ").textWithPicture())
        assertEquals("Hi.\n\n[Picture created: fox]", reply("a", "Hi.", ReplyPicture("fox")).textWithPicture())
        assertEquals("[Picture created: fox]", reply("a", "", ReplyPicture("fox")).textWithPicture())
        assertEquals("Hi.", reply("a", "Hi.", ReplyPicture.refusal("No.")).textWithPicture())
        assertEquals("", reply("a", "", ReplyPicture.refusal("No.")).textWithPicture())
        assertEquals("What is 2+2?", user("What is 2+2?").textWithPicture())
    }

    @Test
    fun `the end of a picture goes into its own reply and no other`() {
        val chat = Conversation(
            "c", "t", createdAt = 1, updatedAt = 1,
            messages = listOf(
                user("a fox"), reply("r1", "One.", ReplyPicture("fox")),
                user("a cat"), reply("r2", "Two.", ReplyPicture("cat")),
            ),
        )
        val updated = chat.withReplyPicture("r2", now = 99) { it!!.made("cat.png", 768, 768) }!!
        assertEquals(ReplyPicture("fox"), updated.messages[1].picture)
        assertEquals(ReplyPicture("cat", "cat.png", 768, 768), updated.messages[3].picture)
        assertEquals(99L, updated.updatedAt)
        assertEquals(chat.messages.map { it.content }, updated.messages.map { it.content })
        assertEquals(1L, chat.updatedAt) // the original is untouched
    }

    @Test
    fun `a reply that is gone cannot take a picture`() {
        val chat = Conversation("c", "t", 1, 1, listOf(user("hi"), reply("r1", "Hello.")))
        assertNull(chat.withReplyPicture("deleted", now = 5) { ReplyPicture("x") })
        // A reply that had no picture entry yet starts one from what the job knew.
        val started = chat.withReplyPicture("r1", now = 5) { it ?: ReplyPicture("fox").failed("Lost") }!!
        assertEquals(ReplyPicture("fox", note = "Lost"), started.messages[1].picture)
    }

    @Test
    fun `a saved chat keeps its pictures and its Not now`() {
        val chat = Conversation(
            "c", "t", 1, 1,
            listOf(
                user("pizza near me"),
                reply("r1", "Which town?", ReplyPicture("fox", "f.png", 768, 1024)),
                reply("r2", "", ReplyPicture.refusal("Your free trial is used up.")),
                reply("r3", "Failed.", ReplyPicture("cat").failed("Image cancelled.")),
            ),
            locationDeclined = true,
        )
        val file = ConversationFile(conversations = listOf(chat))
        val text = storeJson.encodeToString(ConversationFile.serializer(), file)
        assertEquals(file, storeJson.decodeFromString(ConversationFile.serializer(), text))
        assertTrue(storeJson.decodeFromString(ConversationFile.serializer(), text).conversations.single().locationDeclined)
    }

    @Test
    fun `chats saved before pictures in replies and before the location card still load`() {
        val old = """{"version":1,"conversations":[{"id":"c","title":"t","createdAt":1,"updatedAt":1,"messages":[
            {"id":"m","role":"assistant","content":"hi","createdAt":1}]}]}"""
        val chat = storeJson.decodeFromString(ConversationFile.serializer(), old).conversations.single()
        assertNull(chat.messages.single().picture)
        assertFalse(chat.locationDeclined)
    }

    @Test
    fun `a chat made by a newer version with more fields still loads`() {
        val newer = """{"id":"m","role":"assistant","content":"hi","createdAt":1,
            "picture":{"prompt":"fox","file":"f.png","width":1,"height":2,"note":null,"refused":false,"seed":7}}"""
        assertEquals(ReplyPicture("fox", "f.png", 1, 2), storeJson.decodeFromString(ChatMessage.serializer(), newer).picture)
    }

    @Test
    fun `the files of a chat include the pictures made inside its replies`() {
        val chat = Conversation(
            "c", "t", 1, 1,
            listOf(
                user("[Image: a]").copy(image = ImageAttachment("a")),
                reply("r0", "[Image: a]").copy(image = ImageAttachment("a", "dialog.png", 1, 1)),
                reply("r1", "One.", ReplyPicture("fox", "inside.png", 1, 1)),
                reply("r2", "Two.", ReplyPicture("cat")), // still being made: no file yet
                reply("r3", "Three.", ReplyPicture.refusal("No.")),
            ),
        )
        assertEquals(listOf("dialog.png", "inside.png"), chat.imageFiles())
    }
}
