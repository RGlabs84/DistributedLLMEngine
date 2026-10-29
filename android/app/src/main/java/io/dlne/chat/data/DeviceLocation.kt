package io.dlne.chat.data

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.Location
import android.location.LocationManager
import android.os.CancellationSignal
import android.os.SystemClock
import androidx.core.content.ContextCompat
import androidx.core.location.LocationManagerCompat
import java.util.concurrent.Executor
import kotlin.coroutines.resume
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeoutOrNull
import kotlinx.serialization.Serializable

/**
 * The phone's position with at most five decimals (about 1 m). It goes only to the DLNE
 * server, which uses it for lookups and never keeps it.
 */
@Serializable
data class PhoneLocation(val lat: Double, val lon: Double) {
    companion object {
        /** Null for anything that is not a real place on Earth. */
        fun of(latitude: Double, longitude: Double): PhoneLocation? {
            if (!(latitude in -90.0..90.0) || !(longitude in -180.0..180.0)) return null
            return PhoneLocation(round5(latitude), round5(longitude))
        }

        // Rounding through a Long keeps a tiny negative value from becoming -0.0.
        private fun round5(value: Double): Double = Math.round(value * 100_000) / 100_000.0
    }
}

/** How long to wait on one provider for a fresh fix. */
data class Attempt(val provider: String, val timeoutMs: Long)

/**
 * The providers to ask for a fresh fix, in order. With the precise permission the GPS comes
 * first and the network gets a short second chance (indoors the GPS often has no fix); with
 * only the approximate permission the network provider is all the app may use. The waits add
 * up to [FRESH_WAIT_MS]: a local question is held back for at most that long.
 */
fun attempts(fine: Boolean): List<Attempt> =
    if (fine) listOf(Attempt(LocationManager.GPS_PROVIDER, 4_000L), Attempt(LocationManager.NETWORK_PROVIDER, 2_000L))
    else listOf(Attempt(LocationManager.NETWORK_PROVIDER, FRESH_WAIT_MS))

/** The most the app waits for a new fix. */
const val FRESH_WAIT_MS = 6_000L

/** A position the system already holds, and how long ago it was taken. */
data class CachedFix(val latitude: Double, val longitude: Double, val ageMs: Long)

/**
 * The providers whose last known position the granted permission may read. Android refuses
 * the GPS and the passive provider (which relays the GPS and other apps' fixes) to an app that
 * holds only the approximate permission, which is what a person gets by choosing
 * "Approximate" in the Android 12+ dialog.
 */
fun lastKnownProviders(fine: Boolean): List<String> =
    if (fine) listOf(LocationManager.GPS_PROVIDER, LocationManager.NETWORK_PROVIDER, LocationManager.PASSIVE_PROVIDER)
    else listOf(LocationManager.NETWORK_PROVIDER)

/**
 * The youngest position under [maxAgeMs] old among [providers]. [read] asks the system for one
 * provider; if it throws [SecurityException] or [IllegalArgumentException] (the permission
 * does not cover that provider, or this phone has no such provider) only that provider is
 * skipped, never the others.
 */
fun newestFix(providers: List<String>, maxAgeMs: Long, read: (String) -> CachedFix?): CachedFix? =
    providers.mapNotNull { provider ->
        try {
            read(provider)
        } catch (_: SecurityException) {
            null
        } catch (_: IllegalArgumentException) {
            null
        }
    }.filter { it.ageMs in 0..maxAgeMs }.minByOrNull { it.ageMs }

/**
 * A fix from the platform [LocationManager] (no Google Play Services, so the sideload build
 * works without them). Nothing is kept: every call asks the system again. A fix under
 * [MAX_AGE_MS] old is used as it is; otherwise a new one is requested ([attempts]); if none
 * arrives in time the answer is null. An older position is never used: a person who has
 * travelled since would get another place's weather.
 */
class DeviceLocation(private val context: Context) {

    private fun granted(permission: String) =
        ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED

    private fun fine() = granted(Manifest.permission.ACCESS_FINE_LOCATION)

    /** True when the person allowed precise or approximate location (Android 12+ lets them allow only approximate). */
    fun permitted(): Boolean = fine() || granted(Manifest.permission.ACCESS_COARSE_LOCATION)

    /** The place, or null: no permission, no provider (location off) or no answer in time. */
    @SuppressLint("MissingPermission") // checked in permitted()
    suspend fun locate(): PhoneLocation? {
        if (!permitted()) return null
        val manager = context.getSystemService(LocationManager::class.java) ?: return null
        val fine = fine()
        val fix = newest(manager, fine) ?: fresh(manager, fine)
        return fix?.let { PhoneLocation.of(it.latitude, it.longitude) }
    }

    /** The youngest position the system holds from the last [MAX_AGE_MS], if any. */
    @SuppressLint("MissingPermission")
    private fun newest(manager: LocationManager, fine: Boolean): CachedFix? {
        val nowNanos = SystemClock.elapsedRealtimeNanos()
        return newestFix(lastKnownProviders(fine), MAX_AGE_MS) { provider ->
            manager.getLastKnownLocation(provider)?.let {
                CachedFix(it.latitude, it.longitude, (nowNanos - it.elapsedRealtimeNanos) / 1_000_000L)
            }
        }
    }

    @SuppressLint("MissingPermission")
    private suspend fun fresh(manager: LocationManager, fine: Boolean): CachedFix? {
        for (attempt in attempts(fine)) {
            val location = try {
                if (!manager.isProviderEnabled(attempt.provider)) continue
                withTimeoutOrNull(attempt.timeoutMs) { current(manager, attempt.provider) }
            } catch (_: SecurityException) {
                null // the permission does not cover this provider (or was taken away): try the next one
            } catch (_: IllegalArgumentException) {
                null // this phone has no such provider
            }
            if (location != null) return CachedFix(location.latitude, location.longitude, 0L)
        }
        return null
    }

    @SuppressLint("MissingPermission")
    private suspend fun current(manager: LocationManager, provider: String): Location? =
        suspendCancellableCoroutine { continuation ->
            val cancel = CancellationSignal()
            continuation.invokeOnCancellation { cancel.cancel() }
            LocationManagerCompat.getCurrentLocation(
                manager, provider, cancel, Executor { it.run() },
            ) { location -> continuation.resume(location) }
        }

    private companion object {
        /** A position older than this is never sent. */
        const val MAX_AGE_MS = 2 * 60 * 1000L
    }
}
