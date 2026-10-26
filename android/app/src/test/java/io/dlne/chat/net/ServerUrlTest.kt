package io.dlne.chat.net

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ServerUrlTest {
    @Test
    fun `the default is accepted unchanged`() {
        assertEquals(ServerUrl.DEFAULT, ServerUrl.normalize(ServerUrl.DEFAULT))
    }

    @Test
    fun `the public server is dlne dot duckdns dot org`() {
        assertEquals("https://dlne.duckdns.org", ServerUrl.DEFAULT)
    }

    @Test
    fun `the old public address is still accepted as input`() {
        assertEquals(ServerUrl.LEGACY_DEFAULT, ServerUrl.normalize("https://73-215-216-195.sslip.io"))
        assertEquals(ServerUrl.LEGACY_DEFAULT, ServerUrl.normalize("73-215-216-195.sslip.io/"))
    }

    @Test
    fun `an old saved public address is upgraded at start-up, however it was written`() {
        assertEquals(ServerUrl.DEFAULT, ServerUrl.startupAddress("https://73-215-216-195.sslip.io"))
        assertEquals(ServerUrl.DEFAULT, ServerUrl.startupAddress("https://73-215-216-195.sslip.io/"))
        assertEquals(ServerUrl.DEFAULT, ServerUrl.startupAddress("  HTTPS://73-215-216-195.SSLIP.IO "))
        assertEquals(ServerUrl.DEFAULT, ServerUrl.startupAddress("73-215-216-195.sslip.io"))
        assertTrue(ServerUrl.isLegacyDefault("https://73-215-216-195.sslip.io/"))
    }

    @Test
    fun `other saved addresses are not touched at start-up`() {
        assertEquals("https://dlne.example.org", ServerUrl.startupAddress("https://dlne.example.org"))
        assertEquals("https://example.org:8443/dlne", ServerUrl.startupAddress("https://example.org:8443/dlne/"))
        // A different host that merely looks similar, or the old host under a path, is someone's own server.
        assertEquals("https://73-215-216-196.sslip.io", ServerUrl.startupAddress("https://73-215-216-196.sslip.io"))
        assertEquals("https://73-215-216-195.sslip.io/dlne", ServerUrl.startupAddress("https://73-215-216-195.sslip.io/dlne"))
        assertEquals(ServerUrl.DEFAULT, ServerUrl.startupAddress(ServerUrl.DEFAULT))
        assertFalse(ServerUrl.isLegacyDefault("https://dlne.example.org"))
        assertFalse(ServerUrl.isLegacyDefault(ServerUrl.DEFAULT))
        assertFalse(ServerUrl.isLegacyDefault(null))
    }

    @Test
    fun `nothing saved, or something unusable, starts on the default`() {
        assertEquals(ServerUrl.DEFAULT, ServerUrl.startupAddress(null))
        assertEquals(ServerUrl.DEFAULT, ServerUrl.startupAddress(""))
        assertEquals(ServerUrl.DEFAULT, ServerUrl.startupAddress("http://insecure.example.org"))
    }

    @Test
    fun `addresses are cleaned up`() {
        assertEquals("https://dlne.example.org", ServerUrl.normalize("  dlne.example.org/ "))
        assertEquals("https://dlne.example.org", ServerUrl.normalize("HTTPS://DLNE.example.org/"))
        assertEquals("https://example.org:8443/dlne", ServerUrl.normalize("https://example.org:8443/dlne/?x=1#y"))
    }

    @Test
    fun `unusable addresses are refused`() {
        assertNull(ServerUrl.normalize(""))
        assertNull(ServerUrl.normalize("http://insecure.example.org"))
        assertNull(ServerUrl.normalize("ftp://example.org"))
        assertNull(ServerUrl.normalize("https://user:pass@example.org"))
        assertNull(ServerUrl.normalize("https://exa mple.org"))
        assertNull(ServerUrl.normalize("https://"))
    }
}
