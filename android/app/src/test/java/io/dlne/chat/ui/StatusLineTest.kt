package io.dlne.chat.ui

import io.dlne.chat.net.AppPool
import io.dlne.chat.net.ClusterStatus
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class StatusLineTest {
    private fun status(ready: Int?, model: String?) = ClusterStatus(appPool = AppPool(members = ready, membersReady = ready, activeModel = model))

    @Test
    fun `the engine's phone lanes count as one machine serving`() {
        assertEquals("Qwen3 4B Instruct · 1 machine serving", statusLineOf(status(1, "Qwen3 4B Instruct"), "none", "1 machine serving"))
    }

    @Test
    fun `no machine ready says so, and no status says nothing`() {
        assertEquals("none", statusLineOf(status(0, "Qwen3"), "none", ""))
        assertEquals("none", statusLineOf(status(null, null), "none", ""))
        assertNull(statusLineOf(null, "none", ""))
        assertNull(statusLineOf(ClusterStatus(), "none", ""))
    }

    @Test
    fun `the line follows the live model when the server switches it`() {
        val machines = "1 machine serving"
        assertEquals("Qwen3 4B Instruct · $machines", statusLineOf(status(1, "Qwen3 4B Instruct"), "none", machines))
        assertEquals("Qwen3.6 35B-A3B (IQ4_XS) · $machines", statusLineOf(status(1, "Qwen3.6 35B-A3B (IQ4_XS)"), "none", machines))
    }

    @Test
    fun `without a model name the count stands alone`() {
        assertEquals("2 machines serving", statusLineOf(status(2, " "), "none", "2 machines serving"))
    }
}
