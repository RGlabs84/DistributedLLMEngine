package io.dlne.chat.ui

import io.dlne.chat.data.Conversation
import io.dlne.chat.net.ImageStatus
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/** Several pictures can be made at once: one per reply that asked the chat for one, keyed by the server's job id. */
class ImageJobsTest {
    private fun job(id: String?, chat: String = "chat-1", reply: String? = "reply-$id", prompt: String = "a fox") =
        ImageJobUi(conversationId = chat, prompt = prompt, size = "768x768", jobId = id, replyId = reply)

    @Test
    fun `several jobs run at once, each under its own job id`() {
        val jobs = ImageJobs().with(job("a")).with(job("b")).with(job("c", chat = "chat-2"))
        assertEquals(listOf("a", "b", "c"), jobs.ids)
        assertEquals("reply-b", jobs["b"]?.replyId)
        assertTrue("a" in jobs)
        assertFalse("z" in jobs)
        assertNull(jobs["z"])
        assertEquals(3, jobs.all.size)
    }

    @Test
    fun `a job's progress is its own, the others are left alone`() {
        val jobs = ImageJobs().with(job("a")).with(job("b"))
        val moved = jobs.changed("a") { it.copy(status = ImageStatus.GENERATING, elapsedSeconds = 41, etaSeconds = 80) }
        assertEquals(ImageStatus.GENERATING, moved["a"]?.status)
        assertEquals(41, moved["a"]?.elapsedSeconds)
        assertEquals(ImageStatus.QUEUED, moved["b"]?.status)
        assertEquals(0, moved["b"]?.elapsedSeconds)
        // Cancelling one card does not touch the other.
        assertTrue(moved.changed("b") { it.copy(cancelling = true) }["b"]!!.cancelling)
        assertFalse(moved.changed("b") { it.copy(cancelling = true) }["a"]!!.cancelling)
    }

    @Test
    fun `ending one job leaves the others running`() {
        val jobs = ImageJobs().with(job("a")).with(job("b")).with(job("c"))
        val after = jobs.without("b")
        assertEquals(listOf("a", "c"), after.ids)
        assertNull(after["b"])
        // Ending it twice, or ending one that never ran, changes nothing.
        assertEquals(after, after.without("b"))
        assertSame(after, after.without("zzz"))
    }

    @Test
    fun `a request still waiting for its job id is not kept`() {
        val jobs = ImageJobs().with(job(null, reply = null))
        assertTrue(jobs.all.isEmpty())
        assertTrue(jobs.ids.isEmpty())
    }

    @Test
    fun `an unknown id changes nothing`() {
        val jobs = ImageJobs().with(job("a"))
        assertSame(jobs, jobs.changed("nope") { it.copy(cancelling = true) })
        assertEquals(jobs, ImageJobs().with(job("a")))
    }

    @Test
    fun `the job of a reply is found by the reply, the jobs of a chat by the chat`() {
        val dialog = job("d", reply = null, prompt = "from the dialog")
        val jobs = ImageJobs().with(job("a", chat = "chat-1")).with(job("b", chat = "chat-2")).with(dialog)
        assertEquals("a", jobs.forReply("reply-a")?.jobId)
        assertEquals("b", jobs.forReply("reply-b")?.jobId)
        assertNull(jobs.forReply("reply-d")) // the dialog's job belongs to no reply
        assertNull(jobs.forReply("elsewhere"))
        assertEquals(listOf("a", "d"), jobs.inConversation("chat-1").map { it.jobId })
        assertEquals(listOf("b"), jobs.inConversation("chat-2").map { it.jobId })
        assertTrue(jobs.inConversation("chat-3").isEmpty())
        // The same lookup works on the list the screen is given.
        assertEquals("b", jobs.all.forReply("reply-b")?.jobId)
    }

    @Test
    fun `adding a job under an id already running replaces it, never duplicates it`() {
        val jobs = ImageJobs().with(job("a", prompt = "first")).with(job("a", prompt = "second"))
        assertEquals(listOf("a"), jobs.ids)
        assertEquals("second", jobs["a"]?.prompt)
    }

    @Test
    fun `the screen shows the pictures of the chat that is open, and none when no chat is`() {
        val a = job("a", chat = "chat-1")
        val b = job("b", chat = "chat-2")
        val open = Conversation("chat-1", "t", 1, 1, emptyList())
        assertEquals(listOf(a), ChatUiState(current = open, imageJobs = listOf(a, b)).imageJobsForCurrent)
        assertTrue(ChatUiState(current = null, imageJobs = listOf(a, b)).imageJobsForCurrent.isEmpty())
    }

    @Test
    fun `the state shows the dialog's picture apart from the ones the chat made`() {
        val request = job(null, reply = null)
        val inReply = job("a")
        val dialog = job("d", reply = null)
        val other = job("o", chat = "chat-2")
        val state = ChatUiState(imageJobs = listOf(request, inReply, dialog, other))
        assertEquals(request, state.imageJob) // the dialog makes one at a time: a request on its way counts
        assertNull(ChatUiState(imageJobs = listOf(inReply, other)).imageJob) // the chat's own pictures do not block it
        assertNull(ChatUiState().imageJob)
    }
}
