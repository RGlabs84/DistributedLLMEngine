package io.dlne.chat.ui

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

/** The header follows the live model: a fetch at once on every start, then every minute, and none while the app is away. */
@OptIn(ExperimentalCoroutinesApi::class)
class StatusRefresherTest {

    @Test
    fun `starting fetches at once, then once a minute`() = runTest {
        var fetches = 0
        val refresher = StatusRefresher(backgroundScope, 60_000L) { fetches++ }
        refresher.start()
        runCurrent()
        assertEquals(1, fetches)
        advanceTimeBy(59_999L)
        runCurrent()
        assertEquals(1, fetches)
        advanceTimeBy(1L)
        runCurrent()
        assertEquals(2, fetches)
        advanceTimeBy(60_000L)
        runCurrent()
        assertEquals(3, fetches)
    }

    @Test
    fun `coming back to the app fetches at once, and nothing is fetched while it is away`() = runTest {
        var fetches = 0
        val refresher = StatusRefresher(backgroundScope, 60_000L) { fetches++ }
        refresher.start()
        runCurrent()
        advanceTimeBy(20_000L) // twenty seconds on screen, then the person switches to another app
        refresher.stop()
        advanceTimeBy(10 * 60_000L) // ten minutes away: the server may have changed its model meanwhile
        runCurrent()
        assertEquals(1, fetches)

        refresher.start() // back in the app: the header does not wait for the next minute
        runCurrent()
        assertEquals(2, fetches)
        advanceTimeBy(60_000L)
        runCurrent()
        assertEquals(3, fetches)
    }

    @Test
    fun `starting twice does not run two loops`() = runTest {
        var fetches = 0
        val refresher = StatusRefresher(backgroundScope, 60_000L) { fetches++ }
        refresher.start()
        refresher.start()
        runCurrent()
        assertEquals(1, fetches)
        advanceTimeBy(60_000L)
        runCurrent()
        assertEquals(2, fetches)
    }

    @Test
    fun `stopping a loop that never started is harmless`() = runTest {
        var fetches = 0
        val refresher = StatusRefresher(backgroundScope, 60_000L) { fetches++ }
        refresher.stop()
        advanceTimeBy(120_000L)
        runCurrent()
        assertEquals(0, fetches)
    }
}
