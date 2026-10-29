package io.dlne.chat.data

import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/** A photo that never gets ready (a cloud photo with no connection) must not hold the message box. */
class PhotoPreparationTest {
    // Not the test's own scope: runBlocking would wait for a read that is still blocked.
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    /** Released at the end so no thread of a stuck read outlives the test. */
    private val stuck = CountDownLatch(1)

    @After
    fun cleanUp() {
        stuck.countDown()
        scope.cancel()
    }

    private fun <T : Any> prepared(timeoutMs: Long = 5_000, read: () -> T?): Prepared<T> =
        runBlocking { prepare(scope, Dispatchers.IO, timeoutMs, read) }

    @Test
    fun `a photo that is read comes back ready`() {
        val result = prepared { "the jpeg" }
        assertTrue(result is Prepared.Ready)
        assertEquals("the jpeg", (result as Prepared.Ready).value)
    }

    @Test
    fun `a photo the phone cannot read is unreadable, and so is one whose decoder throws`() {
        assertSame(Prepared.Unreadable, prepared<String> { null })
        assertSame(Prepared.Unreadable, prepared<String> { throw IllegalStateException("a broken file") })
    }

    @Test
    fun `a source that never answers is given up on after the timeout, and the caller is free`() {
        val started = System.nanoTime()
        val result = prepared<String>(timeoutMs = 200) {
            stuck.await()
            "late"
        }
        val tookMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - started)
        assertSame(Prepared.TooSlow, result)
        assertTrue("took $tookMs ms: the wait must not follow the read", tookMs < 3_000)
    }

    @Test
    fun `taking the photo out frees the waiting at once, though the read is still blocked`() {
        val waiting = scope.launch {
            prepare(scope, Dispatchers.IO, 60_000) {
                stuck.await()
                "late"
            }
        }
        Thread.sleep(150) // let it start waiting
        runBlocking { withTimeout(3_000) { waiting.cancelAndJoin() } }
        assertTrue(waiting.isCancelled)
    }

    @Test
    fun `a read that finishes just in time is used`() {
        val result = prepared(timeoutMs = 2_000) {
            Thread.sleep(50)
            "slow but there"
        }
        assertTrue(result is Prepared.Ready)
    }
}
