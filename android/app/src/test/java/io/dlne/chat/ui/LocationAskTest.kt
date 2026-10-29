package io.dlne.chat.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** What Android's answer to the card's permission request means for the message that waits. */
class LocationAskTest {
    private val fine = "android.permission.ACCESS_FINE_LOCATION"
    private val coarse = "android.permission.ACCESS_COARSE_LOCATION"

    @Test
    fun `precise or approximate is a yes, whatever else was refused`() {
        assertEquals(LocationAnswer.GRANTED, locationAnswerOf(mapOf(fine to true, coarse to true)) { false })
        // Android 12+: the person chose "Approximate", so the precise one is refused and the other allowed.
        assertEquals(LocationAnswer.GRANTED, locationAnswerOf(mapOf(fine to false, coarse to true)) { false })
        assertEquals(LocationAnswer.GRANTED, locationAnswerOf(mapOf(fine to true, coarse to false)) { true })
    }

    @Test
    fun `a refusal Android will let us ask about again is refused, not blocked`() {
        // After the first "Don't allow" Android still offers to explain and ask again.
        assertEquals(LocationAnswer.REFUSED, locationAnswerOf(mapOf(fine to false, coarse to false)) { true })
        assertEquals(LocationAnswer.REFUSED, locationAnswerOf(mapOf(fine to false, coarse to false)) { it == coarse })
    }

    @Test
    fun `a refusal after which Android never asks again is blocked, and the card then offers the settings page`() {
        assertEquals(LocationAnswer.BLOCKED, locationAnswerOf(mapOf(fine to false, coarse to false)) { false })
    }

    @Test
    fun `a request that was interrupted decides nothing, so the card stays`() {
        // Android 8 to 10 cancel the request when the person leaves the app (a call, the Home button) and answer
        // with empty arrays: that is not a refusal, and the message must not go without a location because of it.
        assertNull(locationAnswerOf(emptyMap()) { false })
        assertNull(locationAnswerOf(emptyMap()) { true })
    }
}
