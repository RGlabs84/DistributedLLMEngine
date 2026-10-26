package io.dlne.chat.data

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test

class ReportsAndStorageTest {
    private var clock = 0L
    private fun user(text: String) = ChatMessage("u${clock}", Role.USER, text, createdAt = clock++)
    private fun answer(text: String, notice: String? = null) =
        ChatMessage("a${clock}", Role.ASSISTANT, text, notice, createdAt = clock++)

    @Test
    fun `a report holds the answer and the question it answered, nothing else`() {
        val first = user("Old question")
        val firstAnswer = answer("Old answer")
        val question = user("  Is it raining in Paris?  ")
        val reply = answer("Yes, light rain [1].")
        val report = Reports.build(listOf(first, firstAnswer, question, reply), reply.id, ReportReason.WRONG, "  It was sunny. ")!!
        assertEquals("Is it raining in Paris?", report.question)
        assertEquals("Yes, light rain [1].", report.answer)
        assertEquals(ReportReason.WRONG, report.reason)
        assertEquals("It was sunny.", report.note)
    }

    @Test
    fun `questions asked again after a failed attempt are joined`() {
        val q1 = user("First try")
        val failed = answer("", notice = "No machine is free.")
        val q2 = user("Second try")
        val reply = answer("Here you go.")
        val report = Reports.build(listOf(q1, failed, q2, reply), reply.id, ReportReason.OTHER, "")!!
        assertEquals("First try\n\nSecond try", report.question)
        assertEquals("", report.note)
    }

    @Test
    fun `fields are capped without splitting an emoji`() {
        val question = user("q".repeat(5_000))
        val reply = answer("😀".repeat(5_000)) // 10,000 UTF-16 units
        val report = Reports.build(listOf(question, reply), reply.id, ReportReason.HARMFUL, "n".repeat(900))!!
        assertEquals(Reports.MAX_QUESTION_CHARS, report.question.length)
        assertEquals(Reports.MAX_ANSWER_CHARS, report.answer.length)
        assertFalse(report.answer.last().isHighSurrogate())
        assertEquals(Reports.MAX_NOTE_CHARS, report.note.length)
        assertEquals("ab", "ab😀".takeChars(3)) // never half an emoji
    }

    @Test
    fun `nothing to report for unknown ids, user messages or empty answers`() {
        val q = user("Hi")
        val empty = answer("", notice = "Stopped.")
        val messages = listOf(q, empty)
        assertNull(Reports.build(messages, "missing", ReportReason.WRONG, ""))
        assertNull(Reports.build(messages, q.id, ReportReason.WRONG, ""))
        assertNull(Reports.build(messages, empty.id, ReportReason.WRONG, ""))
    }

    @Test
    fun `the report body uses the API's field names and reason values`() {
        val body = Json.encodeToString(ReportRequest.serializer(), ReportRequest("q", "a", ReportReason.HARMFUL, ""))
        assertEquals("""{"question":"q","answer":"a","reason":"harmful","note":""}""", body)
        val reasons = ReportReason.entries.map { Json.encodeToString(ReportReason.serializer(), it) }
        assertEquals(listOf("\"harmful\"", "\"wrong\"", "\"offensive\"", "\"other\""), reasons)
    }

    // The same settings as ConversationStore.
    private val storeJson = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    @Test
    fun `saved chats keep their sources and lookup notes`() {
        val message = ChatMessage(
            id = "m1",
            role = Role.ASSISTANT,
            content = "Rain [1]",
            createdAt = 1,
            sources = listOf(Source(1, "Forecast", "https://w.example/f", "w.example", "1 hour ago"), Source(2, "No link")),
            searchNotes = listOf("Results from Open-Meteo."),
        )
        val file = ConversationFile(conversations = listOf(Conversation("c1", "t", 1, 1, listOf(message))))
        val text = storeJson.encodeToString(ConversationFile.serializer(), file)
        assertEquals(file, storeJson.decodeFromString(ConversationFile.serializer(), text))
    }

    @Test
    fun `chats saved by the previous version still load`() {
        val old = """{"version":1,"conversations":[{"id":"c","title":"t","createdAt":1,"updatedAt":1,"messages":[
            {"id":"m","role":"assistant","content":"hi","createdAt":1}]}]}"""
        val message = storeJson.decodeFromString(ConversationFile.serializer(), old).conversations.single().messages.single()
        assertEquals(emptyList<Source>(), message.sources)
        assertEquals(emptyList<String>(), message.searchNotes)
    }
}
