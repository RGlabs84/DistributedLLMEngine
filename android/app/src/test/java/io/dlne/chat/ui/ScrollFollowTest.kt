package io.dlne.chat.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Owner, 1.0.8: "if I scroll up on the answer to read something it constantly pushes what I'm trying to read". */
class ScrollFollowTest {
    @Test
    fun `only the newest end of the reversed list counts as following`() {
        assertTrue(atNewest(0, 0))
        assertFalse("scrolled up inside the answer", atNewest(0, 1))
        assertFalse("scrolled up to older messages", atNewest(3, 0))
    }

    @Test
    fun `a reader scrolled up into the growing answer keeps the same words in view`() {
        assertEquals(120, keepReaderInPlace(following = false, scrolling = false, firstIndex = 0, firstOffset = 400, oldSize = 1000, newSize = 1120))
    }

    @Test
    fun `a finger holding a spot keeps it while the answer streams, even before the scroll ends`() {
        // Owner, 1.1.1: "try to hold the spot while it's streaming a response". Following is decided when a scroll ends,
        // so during the hold it is still true; the hold itself must keep the words in place.
        assertEquals(64, keepReaderInPlace(following = true, scrolling = true, firstIndex = 0, firstOffset = 30, oldSize = 500, newSize = 564))
    }

    @Test
    fun `the answer turning into the saved message, with its buttons, does not move the words either`() {
        assertEquals(96, keepReaderInPlace(following = false, scrolling = false, firstIndex = 0, firstOffset = 900, oldSize = 2400, newSize = 2496))
        assertEquals("a redraw that shrinks it moves back the same way", -40,
            keepReaderInPlace(following = false, scrolling = false, firstIndex = 0, firstOffset = 900, oldSize = 2400, newSize = 2360))
    }

    @Test
    fun `nothing moves while following at the very end, while reading older messages, or when nothing changed`() {
        assertEquals("following at the end: the list stays at the newest end", 0,
            keepReaderInPlace(following = true, scrolling = false, firstIndex = 0, firstOffset = 0, oldSize = 1000, newSize = 1120))
        assertEquals("a finger at the very end follows too", 0,
            keepReaderInPlace(following = true, scrolling = true, firstIndex = 0, firstOffset = 0, oldSize = 1000, newSize = 1120))
        assertEquals("following and not touching: it follows", 0,
            keepReaderInPlace(following = true, scrolling = false, firstIndex = 0, firstOffset = 400, oldSize = 1000, newSize = 1120))
        assertEquals("reading older messages: the answer below does not move them", 0,
            keepReaderInPlace(following = false, scrolling = false, firstIndex = 2, firstOffset = 400, oldSize = 1000, newSize = 1120))
        assertEquals("the answer just appeared", 0,
            keepReaderInPlace(following = false, scrolling = false, firstIndex = 0, firstOffset = 400, oldSize = 0, newSize = 1120))
        assertEquals("no change", 0,
            keepReaderInPlace(following = false, scrolling = false, firstIndex = 0, firstOffset = 400, oldSize = 1120, newSize = 1120))
    }

    @Test
    fun `sending always goes to the newest message, an answer arriving only while following`() {
        assertTrue(jumpToNewest(following = false, justSent = true, scrolling = true))
        assertTrue(jumpToNewest(following = true, justSent = false, scrolling = false))
        assertFalse("the reader scrolled up: the finished answer stays out of the way", jumpToNewest(following = false, justSent = false, scrolling = false))
        assertFalse("mid-fling: never yanked back", jumpToNewest(following = true, justSent = false, scrolling = true))
    }
}
