package io.dlne.chat.data

import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.TimeoutCancellationException
import kotlinx.coroutines.async
import kotlinx.coroutines.withTimeout

/** How getting one chosen photo ready ended. */
sealed interface Prepared<out T> {
    class Ready<T>(val value: T) : Prepared<T>

    /** The phone could not read it (not a picture, a format it does not know, no more memory). */
    data object Unreadable : Prepared<Nothing>

    /** Nothing came back in time: the source never answered. */
    data object TooSlow : Prepared<Nothing>
}

/**
 * Runs [read] (which blocks: it decodes a photo, or waits on a cloud provider) on [io] and waits at
 * most [timeoutMs] for it. A source that never answers keeps its thread, but nobody waits for it any
 * more: the caller is free after the timeout, or at once when it is cancelled (the person took the
 * photo back out), and whatever the read finally returns is dropped.
 *
 * [read] returns null for a photo it cannot read, which is [Prepared.Unreadable] (so does one that
 * throws, which a decoder can do for a broken file).
 */
suspend fun <T : Any> prepare(scope: CoroutineScope, io: CoroutineDispatcher, timeoutMs: Long, read: () -> T?): Prepared<T> {
    val work = scope.async(io) {
        try {
            read()
        } catch (_: RuntimeException) {
            null
        }
    }
    try {
        val value = withTimeout(timeoutMs) { work.await() }
        return if (value != null) Prepared.Ready(value) else Prepared.Unreadable
    } catch (_: TimeoutCancellationException) {
        return Prepared.TooSlow
    } finally {
        work.cancel() // already done: nothing; still blocked: nobody waits for it any more
    }
}
