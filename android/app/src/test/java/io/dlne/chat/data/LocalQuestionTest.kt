package io.dlne.chat.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class LocalQuestionTest {
    private var clock = 0L
    private fun user(text: String, image: ImageAttachment? = null) =
        ChatMessage(newId(), Role.USER, text, createdAt = clock++, image = image)
    private fun assistant(text: String) = ChatMessage(newId(), Role.ASSISTANT, text, createdAt = clock++)

    @Test
    fun `the server's own near-me phrases count`() {
        // engine/search.py _NEAR_ME: near me|nearby|near here|around here|close to me|around me
        for (q in listOf("pizza near me", "Is there a pharmacy NEARBY?", "anything near here", "cafes around here",
            "a park close to me", "what is around me")) {
            assertTrue(q, LocalQuestion.matches(q))
        }
    }

    @Test
    fun `weather and where-I-am wording counts`() {
        for (q in listOf("What's the weather?", "forecast for tomorrow", "Will it be raining later", "is it snowing",
            "what is my location", "where I am right now", "my city's mayor", "in my area")) {
            assertTrue(q, LocalQuestion.matches(q))
        }
    }

    @Test
    fun `other questions do not wake the GPS`() {
        for (q in listOf("Write a poem about autumn", "Explain recursion", "How far is the moon?", "Translate hello into French",
            "nearly done", "I live in a sunny place", "what is the terrain")) {
            assertFalse(q, LocalQuestion.matches(q))
        }
    }

    @Test
    fun `a follow-up to a local question still counts, an older one does not`() {
        val chat = listOf(user("weather near me"), assistant("Sunny, 20 C."), user("and tomorrow?"))
        assertTrue(LocalQuestion.asks(chat))
        val older = listOf(user("weather near me"), assistant("Sunny."), user("thanks"), assistant("Welcome."), user("tell me a joke"))
        assertFalse(LocalQuestion.asks(older))
    }

    @Test
    fun `weather words in a picture request are what to draw, not where you are`() {
        for (q in listOf("hey, can I get a picture of a fox in the snow?", "draw me a rainy street", "a painting of a stormy forecast",
            "make an image of snow falling on a cabin")) {
            assertFalse(q, LocalQuestion.matches(q))
        }
        for (q in listOf("a picture of the park near me", "is it snowing", "draw what the weather is like where I am")) {
            assertTrue(q, LocalQuestion.matches(q))
        }
    }

    @Test
    fun `an image request is not a question about where you are`() {
        val chat = listOf(user("[Image: the weather near me]", ImageAttachment("the weather near me")))
        assertEquals(false, LocalQuestion.asks(chat))
    }

    @Test
    fun `a local question with sharing off, in a chat that was not answered, asks first`() {
        assertTrue(LocalQuestion.shouldAsk(listOf(user("pizza near me")), sharing = false, declinedInChat = false))
        assertTrue(LocalQuestion.shouldAsk(listOf(user("What's the weather?")), sharing = false, declinedInChat = false))
    }

    @Test
    fun `with sharing on nothing is asked, the position simply goes along`() {
        assertFalse(LocalQuestion.shouldAsk(listOf(user("pizza near me")), sharing = true, declinedInChat = false))
        assertFalse(LocalQuestion.shouldAsk(listOf(user("pizza near me")), sharing = true, declinedInChat = true))
    }

    @Test
    fun `a chat answered Not now is not asked again, another chat still asks`() {
        val chat = listOf(user("pizza near me"), assistant("Which town?"), user("and a pharmacy nearby?"))
        assertFalse(LocalQuestion.shouldAsk(chat, sharing = false, declinedInChat = true))
        assertTrue(LocalQuestion.shouldAsk(chat, sharing = false, declinedInChat = false))
    }

    @Test
    fun `a question that is not local never asks, answered chat or not`() {
        for (declined in listOf(false, true)) {
            assertFalse(LocalQuestion.shouldAsk(listOf(user("Write a poem about autumn")), sharing = false, declinedInChat = declined))
        }
    }

    @Test
    fun `a follow-up to a local question asks, one long after it does not`() {
        val followUp = listOf(user("weather near me"), assistant("Sunny."), user("and tomorrow?"))
        assertTrue(LocalQuestion.shouldAsk(followUp, sharing = false, declinedInChat = false))
        val old = listOf(user("weather near me"), assistant("Sunny."), user("thanks"), assistant("Welcome."), user("tell me a joke"))
        assertFalse(LocalQuestion.shouldAsk(old, sharing = false, declinedInChat = false))
    }

    @Test
    fun `a picture request never asks, even one that mentions the weather`() {
        val chat = listOf(user("[Image: the weather near me]", ImageAttachment("the weather near me")))
        assertFalse(LocalQuestion.shouldAsk(chat, sharing = false, declinedInChat = false))
    }

    @Test
    fun `a message with no words asks nothing, whatever came before it`() {
        // Photos alone, or the text was deleted from the box while the card was up: nothing is held.
        val afterLocal = listOf(user("weather near me"), assistant("Sunny."), user(""))
        assertFalse(LocalQuestion.shouldAsk(afterLocal, sharing = false, declinedInChat = false))
        assertFalse(LocalQuestion.shouldAsk(listOf(user("   ")), sharing = false, declinedInChat = false))
        assertFalse(LocalQuestion.shouldAsk(emptyList(), sharing = false, declinedInChat = false))
        // The same chat with words in the new message still asks.
        assertTrue(LocalQuestion.shouldAsk(listOf(user("weather near me"), assistant("Sunny."), user("and tomorrow?")), sharing = false, declinedInChat = false))
    }

    @Test
    fun `the card goes away when the held text is no longer a local question`() {
        // What the view model asks on every change of the box while the card is up.
        fun asks(box: String) = LocalQuestion.shouldAsk(listOf(user(box)), sharing = false, declinedInChat = false)
        assertTrue(asks("pizza near me"))
        assertTrue("more words, still local", asks("pizza near me, open late"))
        assertFalse("the text was deleted", asks(""))
        assertFalse("or changed to something else", asks("a poem about autumn"))
    }
}
