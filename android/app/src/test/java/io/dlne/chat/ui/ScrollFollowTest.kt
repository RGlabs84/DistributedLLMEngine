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
        assertEquals(120, keepReaderInPlace(false, "pending", "pending", 400, 1000, 1120))
    }

    @Test
    fun `nothing moves while following, at the very end, above the answer, or when it did not grow`() {
        assertEquals("following: the list stays at the newest end", 0, keepReaderInPlace(true, "pending", "pending", 400, 1000, 1120))
        assertEquals("at the end of the answer", 0, keepReaderInPlace(false, "pending", "pending", 0, 1000, 1120))
        assertEquals("reading older messages: the answer below does not move them", 0, keepReaderInPlace(false, "m-1", "pending", 400, 1000, 1120))
        assertEquals("the answer just appeared", 0, keepReaderInPlace(false, "pending", "pending", 400, 0, 1120))
        assertEquals("it shrank (a redraw)", 0, keepReaderInPlace(false, "pending", "pending", 400, 1120, 1000))
    }

    @Test
    fun `sending always goes to the newest message, an answer arriving only while following`() {
        assertTrue(jumpToNewest(following = false, justSent = true, scrolling = true))
        assertTrue(jumpToNewest(following = true, justSent = false, scrolling = false))
        assertFalse("the reader scrolled up: the finished answer stays out of the way", jumpToNewest(following = false, justSent = false, scrolling = false))
        assertFalse("mid-fling: never yanked back", jumpToNewest(following = true, justSent = false, scrolling = true))
    }
}
