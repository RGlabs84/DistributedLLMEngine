package io.dlne.chat.ui

import io.dlne.chat.net.ClusterStatus
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * The status the header and the "+" menu read, with the server address it came from.
 *
 * A refresh that fails (a network blip, a restart in progress) must not blank them until the next
 * minute, so [after] keeps the last good status of the same server. It never keeps one from another
 * address: after the server was changed, a status that says nothing about the server in use is worse
 * than none.
 */
data class ShownStatus(val status: ClusterStatus? = null, val server: String? = null) {
    /** What to show after a refresh of [server] that found [fresh] (null when it failed). */
    fun after(fresh: ClusterStatus?, server: String): ShownStatus = when {
        fresh != null -> ShownStatus(fresh, server)
        this.server == server -> this
        else -> ShownStatus()
    }
}

/**
 * Keeps the header's model and machine count fresh while the app is on screen. [start] fetches at once
 * and then every [intervalMs] until [stop]. Because the first fetch is immediate, starting again when
 * the app comes back to the foreground shows the live model without waiting for the next minute (the
 * server may have switched models while the phone was away); and while the app is away nothing is fetched.
 */
class StatusRefresher(
    private val scope: CoroutineScope,
    private val intervalMs: Long,
    private val fetch: suspend () -> Unit,
) {
    private var job: Job? = null

    /** Starts the loop; one that is already running is left alone. */
    fun start() {
        if (job?.isActive == true) return
        job = scope.launch {
            while (isActive) {
                fetch()
                delay(intervalMs)
            }
        }
    }

    fun stop() {
        job?.cancel()
        job = null
    }
}
