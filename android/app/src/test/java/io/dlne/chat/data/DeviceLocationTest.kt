package io.dlne.chat.data

import android.location.LocationManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

/** The cached-position choice of [DeviceLocation], with fake providers instead of a LocationManager. */
class DeviceLocationTest {
    private val gps = LocationManager.GPS_PROVIDER
    private val network = LocationManager.NETWORK_PROVIDER
    private val passive = LocationManager.PASSIVE_PROVIDER
    private val max = 2 * 60 * 1000L

    private fun fix(ageMs: Long) = CachedFix(40.0, -74.0, ageMs)

    @Test
    fun `with only the approximate permission the GPS and passive providers are never read`() {
        assertEquals(listOf(network), lastKnownProviders(fine = false))
        assertEquals(listOf(gps, network, passive), lastKnownProviders(fine = true))
    }

    @Test
    fun `a provider the permission cannot read does not cancel the others`() {
        // What Android does to an approximate-only app that asks the passive provider.
        val readable = fix(20_000)
        val asked = mutableListOf<String>()
        val found = newestFix(listOf(passive, network, gps), max) { provider ->
            asked += provider
            if (provider == network) readable else throw SecurityException("\"$provider\" provider requires ACCESS_FINE_LOCATION")
        }
        assertSame(readable, found)
        assertEquals("every provider was still tried", listOf(passive, network, gps), asked)
    }

    @Test
    fun `coarse only finds the network position, and finds none without throwing when nothing is cached`() {
        val readable = fix(5_000)
        val coarseOnly = newestFix(lastKnownProviders(fine = false), max) { if (it == network) readable else throw SecurityException() }
        assertSame(readable, coarseOnly)
        assertNull(newestFix(lastKnownProviders(fine = false), max) { null })
        assertNull(newestFix(lastKnownProviders(fine = true), max) { throw SecurityException() })
    }

    @Test
    fun `a phone without a provider is skipped, not fatal`() {
        val readable = fix(1_000)
        assertSame(readable, newestFix(listOf(gps, network), max) { if (it == gps) throw IllegalArgumentException("no such provider") else readable })
    }

    @Test
    fun `the youngest position wins`() {
        val young = fix(10_000)
        val old = fix(90_000)
        assertSame(young, newestFix(listOf(gps, network, passive), max) { when (it) { gps -> old; network -> young; else -> fix(60_000) } })
    }

    @Test
    fun `a position older than the limit is never used, however little else there is`() {
        assertNull(newestFix(listOf(gps), max) { fix(max + 1) })
        assertNull("a half-hour-old position is not sent", newestFix(listOf(gps), max) { fix(30 * 60 * 1000L) })
        val atLimit = fix(max)
        assertSame("exactly at the limit is still fine", atLimit, newestFix(listOf(gps), max) { atLimit })
    }

    @Test
    fun `a clock that went backwards (negative age) is not trusted`() {
        assertNull(newestFix(listOf(gps), max) { fix(-5_000) })
    }
}
