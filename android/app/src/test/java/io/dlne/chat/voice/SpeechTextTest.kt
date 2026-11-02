package io.dlne.chat.voice

import org.junit.Assert.assertEquals
import org.junit.Test

/** The shared vectors of docs/voice-contract.md, "Conversation mode", plus streaming. */
class SpeechTextTest {
    private fun say(md: String) = speakableSentences(md)

    @Test fun `bold and citation marks`() =
        assertEquals(listOf("Paris is the capital."), say("**Paris** is the capital [1]."))

    @Test fun `code blocks become one sentence`() =
        assertEquals(
            listOf("Run this:", "There's code in the chat.", "Done."),
            say("Run this:\n\n```bash\nls -la\n```\n\nDone."),
        )

    @Test fun `links keep text and bare urls go`() =
        assertEquals(listOf("See the docs or."), say("See [the docs](https://x.test/a) or https://y.test/b."))

    @Test fun `list items are sentences`() = assertEquals(listOf("one", "two"), say("- one\n- two"))

    @Test fun `numbers and abbreviations do not split`() =
        assertEquals(
            listOf("It costs 3.5 dollars, e.g. today.", "Really!"),
            say("It costs 3.5 dollars, e.g. today. Really!"),
        )

    @Test fun `a table is one sentence`() =
        assertEquals(listOf("There's a table in the chat."), say("| a | b |\n|---|---|\n| 1 | 2 |"))

    @Test fun `inline code keeps its text`() = assertEquals(listOf("Use git status first."), say("Use `git status` first."))

    @Test fun `headings quotes and numbered items lose their marks`() =
        assertEquals(listOf("Title", "quoted", "first"), say("# Title\n> quoted\n1. first"))

    @Test fun `empty gives nothing`() = assertEquals(emptyList<String>(), say("  \n"))

    @Test fun `streaming gives each sentence once, when complete`() {
        val feed = SpeechFeed()
        assertEquals(emptyList<String>(), feed.feed("Hello the"))
        assertEquals(emptyList<String>(), feed.feed("Hello there."))
        assertEquals(listOf("Hello there."), feed.feed("Hello there. How"))
        assertEquals(emptyList<String>(), feed.feed("Hello there. How are you"))
        assertEquals(listOf("How are you?"), feed.feed("Hello there. How are you?", final = true))
        assertEquals(emptyList<String>(), feed.feed("Hello there. How are you?", final = true))
    }

    @Test fun `a number being streamed is not cut`() {
        val feed = SpeechFeed()
        assertEquals(emptyList<String>(), feed.feed("It costs 3."))
        assertEquals(listOf("It costs 3.5 dollars."), feed.feed("It costs 3.5 dollars.\n"))
    }

    @Test fun `a new line completes a line`() {
        val feed = SpeechFeed()
        assertEquals(listOf("Run this:"), feed.feed("Run this:\n"))
        assertEquals(emptyList<String>(), feed.feed("Run this:\n\n```bash\nls"))
        assertEquals(listOf("There's code in the chat."), feed.feed("Run this:\n\n```bash\nls\n```\n"))
    }

    @Test fun `after a retract only the fresh answer is spoken`() {
        val feed = SpeechFeed()
        assertEquals(listOf("I think it is sunny."), feed.feed("I think it is sunny. Maybe"))
        feed.reset() // the server retracted the text
        assertEquals(emptyList<String>(), feed.feed(""))
        assertEquals(listOf("It is 12 degrees."), feed.feed("It is 12 degrees. Rain"))
        assertEquals(listOf("Rain later."), feed.feed("It is 12 degrees. Rain later.", final = true))
    }

    @Test fun `etc is an abbreviation`() =
        assertEquals(listOf("Bring pens, paper, etc. to class."), say("Bring pens, paper, etc. to class."))

    @Test fun `final flushes the tail`() {
        val feed = SpeechFeed()
        assertEquals(listOf("No end mark"), feed.feed("No end mark", final = true))
    }
}
