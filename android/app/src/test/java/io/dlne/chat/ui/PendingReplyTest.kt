package io.dlne.chat.ui

import io.dlne.chat.data.Source
import io.dlne.chat.markdown.Markdown
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The reply being streamed in: what a server `retract` does to it (the view model applies
 * [PendingReply.retracted] to the current reply), and the links the app offers.
 */
class PendingReplyTest {
    private val source = Source(1, "City hall", "https://springfield.example/mayor", "springfield.example", null)

    private val streaming = PendingReply(
        conversationId = "chat-1",
        text = "The mayor is probably Smith.",
        notice = null,
        lookup = Lookup("web_search", "mayor of Springfield"),
        sources = listOf(source),
        searchNotes = listOf("One of two lookups answered."),
    )

    @Test
    fun `retract empties the text and drops the sources but keeps the bubble`() {
        val after = streaming.retracted()
        assertEquals("", after.text)
        assertTrue(after.sources.isEmpty())
        // Still the same reply in the same conversation, so the bubble stays where it is.
        assertEquals("chat-1", after.conversationId)
        assertEquals(streaming.lookup, after.lookup)
        assertEquals(streaming.searchNotes, after.searchNotes)
        assertEquals(null, after.notice)
        // Saved under the same id, so sources the reader unfolded stay unfolded.
        assertEquals(streaming.messageId, after.messageId)
    }

    @Test
    fun `each reply gets its own message id`() {
        assertTrue(PendingReply("chat-1").messageId != PendingReply("chat-1").messageId)
        assertTrue(PendingReply("chat-1").messageId.isNotBlank())
    }

    @Test
    fun `retract on a reply that holds no sources or text changes nothing`() {
        val bare = PendingReply("chat-1")
        assertEquals(bare, bare.retracted())
    }

    @Test
    fun `website and Discord links are the official ones and open like the other links`() {
        assertEquals("https://dlne.rossgilsonlabs.dev", WEBSITE_URL)
        assertEquals("https://discord.gg/RNxvxWn8cw", DISCORD_URL)
        // openInBrowser only follows what Markdown.safeUrl returns unchanged.
        assertEquals(WEBSITE_URL, Markdown.safeUrl(WEBSITE_URL))
        assertEquals(DISCORD_URL, Markdown.safeUrl(DISCORD_URL))
    }
}
