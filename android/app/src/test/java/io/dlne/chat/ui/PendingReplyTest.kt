package io.dlne.chat.ui

import io.dlne.chat.data.ReplyPicture
import io.dlne.chat.data.Source
import io.dlne.chat.markdown.Markdown
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
    fun `a reply that asked for a picture holds it, and a retract does not take it back`() {
        val making = PendingReply("chat-1", text = "A fox is on its way.").withPictureJob("a red fox")
        assertEquals(ReplyPicture("a red fox"), making.picture)
        assertTrue(making.picture!!.pending)
        // The server takes back the model's text, not the picture job it already started.
        val after = making.retracted()
        assertEquals("", after.text)
        assertEquals(making.picture, after.picture)
    }

    @Test
    fun `a reply makes one picture, so a second job in it is ignored`() {
        val first = PendingReply("chat-1").withPictureJob("a red fox")
        assertEquals(first, first.withPictureJob("a blue fox"))
        assertEquals("a red fox", first.withPictureJob("a blue fox").picture?.prompt)
    }

    @Test
    fun `a refusal keeps the server's words and starts nothing`() {
        val refused = PendingReply("chat-1").withPictureRefused("Your free trial is used up.")
        assertEquals(ReplyPicture.refusal("Your free trial is used up."), refused.picture)
        assertFalse(refused.picture!!.created)
        assertFalse(refused.picture!!.pending)
        assertEquals("Your free trial is used up.", refused.picture!!.note)
        // Saying it twice (or a retract) leaves it as it was.
        assertEquals(refused, refused.withPictureRefused("Your free trial is used up."))
        assertEquals(refused, refused.retracted())
    }

    @Test
    fun `a job replaces a refusal that came first, but a refusal never replaces a job`() {
        val refusedThenJob = PendingReply("chat-1").withPictureRefused("Not that one.").withPictureJob("a red fox")
        assertEquals(ReplyPicture("a red fox"), refusedThenJob.picture)
        val jobThenRefusal = PendingReply("chat-1").withPictureJob("a red fox").withPictureRefused("Not that one.")
        assertEquals(ReplyPicture("a red fox"), jobThenRefusal.picture)
    }

    @Test
    fun `a picture that ends while the reply is still streaming is kept for when it is saved`() {
        val streaming = PendingReply("chat-1", text = "Here it comes").withPictureJob("a red fox")
        val made = streaming.withPicture(streaming.picture!!.made("f.png", 768, 1024))
        assertEquals(ReplyPicture("a red fox", "f.png", 768, 1024), made.picture)
        assertEquals("Here it comes", made.text)
        assertEquals(streaming.messageId, made.messageId)
        val failed = streaming.withPicture(streaming.picture!!.failed("Image cancelled."))
        assertEquals(ReplyPicture("a red fox", note = "Image cancelled."), failed.picture)
        assertFalse(failed.picture!!.created)
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
