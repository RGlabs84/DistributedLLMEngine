package io.dlne.chat.data

import android.location.LocationManager
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class PhoneLocationTest {
    @Test
    fun `a position keeps five decimals, about 1 m, and no more`() {
        assertEquals(PhoneLocation(40.12346, -74.56495), PhoneLocation.of(40.123456, -74.564949))
        assertEquals(PhoneLocation(-33.8688, 151.2093), PhoneLocation.of(-33.8688, 151.2093))
        assertEquals(PhoneLocation(51.50735, -0.12776), PhoneLocation.of(51.5073509, -0.1277583))
    }

    @Test
    fun `the poles, the date line and a tiny negative value stay valid`() {
        assertEquals(PhoneLocation(90.0, 180.0), PhoneLocation.of(90.0, 180.0))
        assertEquals(PhoneLocation(-90.0, -180.0), PhoneLocation.of(-90.0, -180.0))
        assertEquals("0.0", PhoneLocation.of(-0.000004, -0.000004)!!.lat.toString())
    }

    @Test
    fun `anything that is not a place on Earth is dropped`() {
        assertNull(PhoneLocation.of(91.0, 0.0))
        assertNull(PhoneLocation.of(0.0, 181.0))
        assertNull(PhoneLocation.of(Double.NaN, 5.0))
        assertNull(PhoneLocation.of(5.0, Double.POSITIVE_INFINITY))
    }

    @Test
    fun `with the precise permission the GPS is asked first and the network gets a short second chance`() {
        val plan = attempts(fine = true)
        assertEquals(listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER), plan.map { it.provider })
        assertEquals(FRESH_WAIT_MS, plan.sumOf { it.timeoutMs })
    }

    @Test
    fun `a message is never held back for more than six seconds`() {
        assertTrue(FRESH_WAIT_MS <= 6_000L)
    }

    @Test
    fun `with only the approximate permission the GPS is never asked`() {
        val plan = attempts(fine = false)
        assertEquals(listOf(LocationManager.NETWORK_PROVIDER), plan.map { it.provider })
        assertEquals(FRESH_WAIT_MS, plan.single().timeoutMs)
    }
}
