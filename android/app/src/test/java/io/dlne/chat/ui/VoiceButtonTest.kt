package io.dlne.chat.ui

import io.dlne.chat.net.Capabilities
import io.dlne.chat.net.ClusterStatus
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** When the mic is shown, the recording clock, and where the spoken text lands. */
class VoiceButtonTest {
    @Test
    fun `the mic is shown only while capabilities voice is true`() {
        assertTrue(voiceButtonVisible(ClusterStatus(capabilities = Capabilities(voice = true))))
        assertFalse(voiceButtonVisible(ClusterStatus(capabilities = Capabilities(voice = false, imageGeneration = true, appVision = true))))
        assertFalse(voiceButtonVisible(ClusterStatus(capabilities = Capabilities())))
        assertFalse(voiceButtonVisible(ClusterStatus()))
        assertFalse("no status yet", voiceButtonVisible(null))
    }

    @Test
    fun `the clock reads minutes and seconds`() {
        assertEquals("0:00", clockText(0))
        assertEquals("0:07", clockText(7))
        assertEquals("1:00", clockText(60))
        assertEquals(3, secondsRecorded(32_000 * 3 + 100))
    }

    @Test
    fun `text goes at the cursor with spaces only where words would touch`() {
        assertEquals(Inserted("hello", 5), insertAtCursor("", 0, 0, "hello"))
        assertEquals(Inserted("abc hello", 9), insertAtCursor("abc", 3, 3, "hello"))
        assertEquals(Inserted("abc hello def", 9), insertAtCursor("abc def", 3, 3, "hello"))
        assertEquals(Inserted("abc hello", 9), insertAtCursor("abc ", 4, 4, "hello"))
    }

    @Test
    fun `a selection is replaced and a backwards or out of range selection is safe`() {
        assertEquals(Inserted("a hello c", 7), insertAtCursor("a b c", 2, 3, "hello"))
        assertEquals(Inserted("a hello c", 7), insertAtCursor("a b c", 3, 2, "hello"))
        assertEquals(Inserted("ab hello", 8), insertAtCursor("ab", 50, 90, "hello"))
    }
}
