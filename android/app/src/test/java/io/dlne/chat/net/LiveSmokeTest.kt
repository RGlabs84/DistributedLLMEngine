package io.dlne.chat.net

import kotlinx.coroutines.runBlocking
import org.junit.Assume.assumeTrue
import org.junit.Test

/**
 * One real question to the real DLNE service, printing the answer as it streams.
 * Skipped unless asked for, so ordinary test runs never touch the network:
 *
 *     ./gradlew :app:testDebugUnitTest --tests '*LiveSmokeTest*' -Pdlne.smoke=true
 *
 * Add -Pdlne.smoke.url=https://your.server to try a self-hosted server instead.
 */
class LiveSmokeTest {
    @Test
    fun `stream one short answer from the live service`() = runBlocking {
        assumeTrue("live smoke test not requested", System.getProperty("dlne.smoke") == "true")
        val baseUrl = System.getProperty("dlne.smoke.url").orEmpty().ifBlank { ServerUrl.DEFAULT }
        val api = DlneApi()

        val status = runCatching { api.fetchStatus(baseUrl) }
        println("SMOKE status: " + status.fold({ "server ${it.serverVersion}, app_pool=${it.appPool}" }, { "failed: $it" }))

        val started = System.currentTimeMillis()
        var firstTokenAt = 0L
        var tokens = 0
        val answer = StringBuilder()
        try {
            api.streamChat(baseUrl, listOf(WireMessage("user", "In one short sentence: what is a volunteer computing network?")), maxTokens = 64)
                .collect { event ->
                    when (event) {
                        is StreamEvent.Content -> {
                            if (firstTokenAt == 0L) firstTokenAt = System.currentTimeMillis()
                            tokens++
                            answer.append(event.text)
                            print(event.text)
                            System.out.flush()
                        }
                        is StreamEvent.Notice -> println("\nSMOKE notice: ${event.text}")
                        is StreamEvent.Failure -> println("\nSMOKE failure: ${event.message}")
                        is StreamEvent.Searching -> println("SMOKE lookup: ${event.tool} \"${event.query}\"")
                        is StreamEvent.Sources -> println("SMOKE sources: " + event.sources.joinToString { "[${it.n}] ${it.title} (${it.host})" })
                        is StreamEvent.SearchNote -> println("SMOKE search note: ${event.note}")
                        is StreamEvent.SearchUnavailable -> println("SMOKE search unavailable: ${event.reason}")
                        StreamEvent.Done -> println("\nSMOKE [DONE]")
                    }
                }
            println("SMOKE chunks=$tokens first-token=${if (firstTokenAt > 0) firstTokenAt - started else -1}ms total=${System.currentTimeMillis() - started}ms")
            println("SMOKE answer: ${answer.toString().trim().lineSequence().firstOrNull().orEmpty()}")
        } catch (e: ApiException.Http) {
            println("SMOKE http error: ${e.code} ${e.detail} retry-after=${e.retryAfterSeconds}")
        } catch (e: ApiException.Network) {
            println("SMOKE network error: ${e.message}")
        }
    }
}
