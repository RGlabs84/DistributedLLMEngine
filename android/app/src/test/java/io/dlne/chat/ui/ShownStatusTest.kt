package io.dlne.chat.ui

import io.dlne.chat.net.AppPool
import io.dlne.chat.net.Capabilities
import io.dlne.chat.net.ClusterStatus
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/** A failed refresh must not blank the header and the "+" menu, and one server's status is never shown for another. */
class ShownStatusTest {
    private val home = "https://dlne.example"
    private val other = "https://mine.example"

    private fun status(model: String) = ClusterStatus(
        appPool = AppPool(members = 1, membersReady = 1, activeModel = model),
        capabilities = Capabilities(imageGeneration = true),
    )

    @Test
    fun `a fresh status replaces the one before it`() {
        val shown = ShownStatus().after(status("Qwen3 4B"), home).after(status("Qwen3.6 35B-A3B"), home)
        assertEquals("Qwen3.6 35B-A3B", shown.status?.appPool?.activeModel)
        assertEquals(home, shown.server)
    }

    @Test
    fun `a failed refresh keeps the last good status, so the header and the plus stay`() {
        val good = ShownStatus().after(status("Qwen3 4B"), home)
        val blip = good.after(null, home)
        assertEquals(good, blip)
        assertEquals("Qwen3 4B", blip.status?.appPool?.activeModel)
        assertEquals(true, blip.status?.imageGeneration) // the "+" menu reads the capabilities from the same status
        // Several in a row change nothing either, and the next good one takes over.
        val recovered = blip.after(null, home).after(null, home).after(status("Qwen3.6 35B-A3B"), home)
        assertEquals("Qwen3.6 35B-A3B", recovered.status?.appPool?.activeModel)
    }

    @Test
    fun `with no good status yet a failed refresh shows nothing`() {
        val shown = ShownStatus().after(null, home)
        assertNull(shown.status)
        assertNull(shown.server)
    }

    @Test
    fun `after the server was changed a failed refresh does not keep the other server's status`() {
        val good = ShownStatus().after(status("Qwen3 4B"), home)
        val changed = good.after(null, other)
        assertNull(changed.status)
        assertNull(changed.server)
        // And a good one from the new server is of course shown, under its own address.
        val fresh = good.after(status("Llama 3"), other)
        assertEquals("Llama 3", fresh.status?.appPool?.activeModel)
        assertEquals(other, fresh.server)
    }
}
