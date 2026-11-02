package io.dlne.chat.ui

import io.dlne.chat.net.MaintenanceInfo
import org.junit.Assert.assertEquals
import org.junit.Test

class MaintenanceCardTest {
    @Test
    fun `the server's message is shown`() {
        assertEquals("Upgrading.", maintenanceMessage(MaintenanceInfo(message = "  Upgrading. "), "fallback"))
    }

    @Test
    fun `a missing or blank message falls back to the default line`() {
        assertEquals("fallback", maintenanceMessage(MaintenanceInfo(), "fallback"))
        assertEquals("fallback", maintenanceMessage(MaintenanceInfo(message = "  "), "fallback"))
    }
}
