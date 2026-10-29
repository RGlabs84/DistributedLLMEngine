package io.dlne.chat.data

import io.dlne.chat.net.WireMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/** The history rule for photos (contract 2.2): only the newest message with photos sends them as pictures. */
class ChatHistoryPhotosTest {
    private var clock = 0L

    private fun user(text: String, vararg photos: String) =
        ChatMessage(newId(), Role.USER, text, createdAt = clock++, photos = photos.map { Photo("$it.jpg", 10, 10) })

    private fun assistant(text: String, notice: String? = null) =
        ChatMessage(newId(), Role.ASSISTANT, text, notice, createdAt = clock++)

    /** The data URL a stored photo would give; a photo named "gone" is no longer on the phone. */
    private val url = { photo: Photo -> if (photo.file == "gone.jpg") null else "data:image/jpeg;base64,${photo.file}" }

    private fun wire(vararg messages: ChatMessage, budgetChars: Int = ChatHistory.DEFAULT_BUDGET_CHARS) =
        ChatHistory.toWire(messages.toList(), budgetChars, url)

    private fun picture(name: String) = "data:image/jpeg;base64,$name.jpg"

    @Test
    fun `a chat without photos is sent exactly as before`() {
        val result = wire(user("Hi"), assistant("Hello"), user("More"))
        assertEquals(
            listOf(WireMessage("user", "Hi"), WireMessage("assistant", "Hello"), WireMessage("user", "More")),
            result,
        )
        assertTrue(result.all { it.images.isEmpty() })
    }

    @Test
    fun `a message with photos sends them as pictures, after its text`() {
        val result = wire(user("What is this?", "a", "b"))
        assertEquals(listOf(WireMessage("user", "What is this?", listOf(picture("a"), picture("b")))), result)
    }

    @Test
    fun `photos alone are a message with no text`() {
        assertEquals(listOf(WireMessage("user", "", listOf(picture("a")))), wire(user("", "a")))
        assertEquals(listOf(WireMessage("user", "", listOf(picture("a")))), wire(user("   ", "a")))
    }

    @Test
    fun `only the newest message with photos sends them, the older ones leave a mark per photo after their text`() {
        val result = wire(
            user("Look", "a", "b"),
            assistant("A cat and a dog."),
            user("And this?", "c"),
            assistant("A bird."),
            user("Which is bigger?"),
        )
        assertEquals(
            listOf(
                WireMessage("user", "Look\n\n[photo] [photo]"),
                WireMessage("assistant", "A cat and a dog."),
                WireMessage("user", "And this?", listOf(picture("c"))),
                WireMessage("assistant", "A bird."),
                WireMessage("user", "Which is bigger?"),
            ),
            result,
        )
    }

    @Test
    fun `a follow-up in words still sends the newest photos again`() {
        val result = wire(user("What is this?", "a"), assistant("A cat."), user("What colour is it?"))
        assertEquals(listOf(picture("a")), result.first().images)
        assertEquals(WireMessage("user", "What colour is it?"), result.last())
    }

    @Test
    fun `an older message of photos alone is only the marks`() {
        val result = wire(user("", "a", "b", "c"), assistant("Three photos."), user("", "d"))
        assertEquals(
            listOf(
                WireMessage("user", "[photo] [photo] [photo]"),
                WireMessage("assistant", "Three photos."),
                WireMessage("user", "", listOf(picture("d"))),
            ),
            result,
        )
    }

    @Test
    fun `no request holds more than four pictures`() {
        val result = wire(
            user("one", "a", "b", "c", "d"), assistant("ok"),
            user("two", "e", "f", "g", "h"), assistant("ok"),
            user("three", "i", "j", "k", "l"),
        )
        assertEquals(4, result.sumOf { it.images.size })
        assertEquals(listOf(picture("i"), picture("j"), picture("k"), picture("l")), result.last().images)
        assertEquals("one\n\n[photo] [photo] [photo] [photo]", result.first().content)
    }

    @Test
    fun `a photo whose file is gone is a mark, the others still go`() {
        val result = wire(user("Look", "a", "gone", "b"))
        assertEquals(listOf(WireMessage("user", "Look\n\n[photo]", listOf(picture("a"), picture("b")))), result)
    }

    @Test
    fun `when no photo can be read the message is only text and marks`() {
        assertEquals(listOf(WireMessage("user", "Look\n\n[photo]")), wire(user("Look", "gone")))
        assertEquals(listOf(WireMessage("user", "[photo]")), wire(user("", "gone")))
    }

    @Test
    fun `without a way to read photos every photo is a mark`() {
        val result = ChatHistory.toWire(listOf(user("Look", "a", "b")))
        assertEquals(listOf(WireMessage("user", "Look\n\n[photo] [photo]")), result)
    }

    @Test
    fun `an answer that never came between two messages merges them, photos and marks together`() {
        val result = wire(
            user("First", "a"),
            assistant("", notice = "No machine is free."),
            user("Second", "b"),
        )
        assertEquals(listOf(WireMessage("user", "First\n\n[photo]\n\nSecond", listOf(picture("b")))), result)
    }

    @Test
    fun `merging with a message of photos alone adds no stray blank lines`() {
        val first = wire(user("", "a"), assistant("", notice = "Stopped."), user("Second"))
        assertEquals(listOf(WireMessage("user", "Second", listOf(picture("a")))), first)
        val second = wire(user("First"), assistant("", notice = "Stopped."), user("", "b"))
        assertEquals(listOf(WireMessage("user", "First", listOf(picture("b")))), second)
        // an older message of photos alone leaves only its mark to join with the next message
        val marked = wire(user("", "a"), assistant("", notice = "Stopped."), user("Second", "b"))
        assertEquals(listOf(WireMessage("user", "[photo]\n\nSecond", listOf(picture("b")))), marked)
    }

    @Test
    fun `the budget counts text, not photos, and the newest message is always kept`() {
        // newest (1 char + a picture) and the answer (8) fit in 20; the older message (13 chars of text and mark) does not.
        val result = wire(
            user("aaaa", "x"),
            assistant("b".repeat(8)),
            user("c", "y"),
            budgetChars = 20,
        )
        assertEquals(listOf(WireMessage("user", "c", listOf(picture("y")))), result)
        // A photo alone has no text at all, and is still sent when the budget is nothing.
        assertEquals(listOf(WireMessage("user", "", listOf(picture("z")))), wire(user("", "z"), budgetChars = 0))
    }

    @Test
    fun `a message with photos that the budget trims away sends no pictures`() {
        val result = wire(user("old", "a"), assistant("reply"), user("x".repeat(500)), budgetChars = 100)
        assertEquals(listOf(WireMessage("user", "x".repeat(500))), result)
    }

    @Test
    fun `only the person's own messages carry photos`() {
        val odd = ChatMessage(newId(), Role.ASSISTANT, "Answer", createdAt = clock++, photos = listOf(Photo("a.jpg", 1, 1)))
        val result = wire(user("Hi"), odd)
        assertEquals(listOf(WireMessage("user", "Hi"), WireMessage("assistant", "Answer")), result)
    }

    @Test
    fun `a request for an image is not a message with photos`() {
        val request = ChatMessage(
            newId(), Role.USER, imagePlaceholder("a fox"), createdAt = clock++,
            image = ImageAttachment("a fox"), photos = listOf(Photo("a.jpg", 1, 1)),
        )
        assertEquals(listOf(WireMessage("user", "Hi")), wire(user("Hi"), request))
    }

    // ------------------------------------------------------------------ photos the server refused

    private fun refused(message: ChatMessage) = message.copy(photosRefused = true)

    @Test
    fun `photos the server refused go as marks, and the words after them go through with no picture`() {
        // The hour's photo allowance was used up: the message stays, and the next one is only words.
        val result = wire(refused(user("What is this?", "a", "b")), user("Never mind, what is a fox?"))
        assertEquals(listOf(WireMessage("user", "What is this?\n\n[photo] [photo]\n\nNever mind, what is a fox?")), result)
        assertTrue(result.all { it.images.isEmpty() })
    }

    @Test
    fun `a refused message of photos alone is only its marks`() {
        assertEquals(listOf(WireMessage("user", "[photo]\n\nWhat now?")), wire(refused(user("", "a")), user("What now?")))
        assertEquals(listOf(WireMessage("user", "[photo]")), wire(refused(user("", "a"))))
    }

    @Test
    fun `an older message with photos does not send its pictures in place of the refused one`() {
        // The refused message is still the newest with photos: sending the older ones again could be refused again.
        val result = wire(
            user("First", "a"),
            assistant("A cat."),
            refused(user("Second", "b", "c")),
            user("And in words?"),
        )
        assertEquals(
            listOf(
                WireMessage("user", "First\n\n[photo]"),
                WireMessage("assistant", "A cat."),
                WireMessage("user", "Second\n\n[photo] [photo]\n\nAnd in words?"),
            ),
            result,
        )
        assertTrue(result.all { it.images.isEmpty() })
    }

    @Test
    fun `a message that was not refused still sends its photos, and trying again sends refused ones as pictures once more`() {
        val kept = wire(user("Look", "a"), assistant("A cat."), user("And in words?"))
        assertEquals(listOf(picture("a")), kept.first().images)
        // Trying again clears the mark: the photos go as pictures once more.
        val retried = wire(refused(user("Look", "a")).copy(photosRefused = false))
        assertEquals(listOf(WireMessage("user", "Look", listOf(picture("a")))), retried)
    }

    @Test
    fun `the photo turn is the newest message of the person's with photos, refused or not`() {
        val a = user("one", "a")
        val b = refused(user("two", "b"))
        val c = user("three")
        assertEquals(b.id, ChatHistory.photoTurnId(listOf(a, assistant("ok"), b, c)))
        assertEquals(a.id, ChatHistory.photoTurnId(listOf(a, c)))
        assertEquals(null, ChatHistory.photoTurnId(listOf(user("words"), assistant("reply"))))
        val request = ChatMessage(newId(), Role.USER, "[Image: a fox]", createdAt = clock++, image = ImageAttachment("a fox"), photos = listOf(Photo("x.jpg", 1, 1)))
        assertEquals(a.id, ChatHistory.photoTurnId(listOf(a, request)))
    }

    @Test
    fun `marking and clearing touches only that message`() {
        val first = user("one", "a")
        val second = user("two", "b")
        val chat = listOf(first, assistant("ok"), second)
        val marked = chat.withPhotosRefused(second.id, true)
        assertEquals(listOf(false, false, true), marked.map { it.photosRefused })
        assertEquals(listOf(false, false, false), marked.withPhotosRefused(second.id, false).map { it.photosRefused })
        // unknown ids and a mark that is already as asked change nothing
        assertEquals(chat, chat.withPhotosRefused("nope", true))
        assertEquals(marked, marked.withPhotosRefused(second.id, true))
    }

    @Test
    fun `the mark is saved with the chat, and chats saved before it load without one`() {
        val json = kotlinx.serialization.json.Json { ignoreUnknownKeys = true; encodeDefaults = true }
        val message = refused(user("Look", "a"))
        val text = json.encodeToString(ChatMessage.serializer(), message)
        assertTrue(text, text.contains(""""photosRefused":true"""))
        assertEquals(message, json.decodeFromString(ChatMessage.serializer(), text))
        val old = """{"id":"m","role":"user","content":"hi","createdAt":1,"photos":[{"file":"a.jpg","width":1,"height":1}]}"""
        assertEquals(false, json.decodeFromString(ChatMessage.serializer(), old).photosRefused)
    }
}
