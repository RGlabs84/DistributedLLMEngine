package io.dlne.chat.update

import android.content.SharedPreferences
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class UpdateStoreTest {
    private val hour = 60L * 60 * 1000
    private val hash = "b".repeat(64)

    private fun manifestText(versionCode: Int, minSdk: Int = 26) =
        """{"versionCode":$versionCode,"versionName":"1.0.$versionCode","minSdk":$minSdk,
        "url":"https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.$versionCode.apk",
        "sha256":"$hash","size":2000000,"notes":"n"}"""

    private fun info(versionCode: Int) =
        (UpdateManifestParser.parse(manifestText(versionCode)) as ManifestResult.Valid).info

    // ------------------------------------------------------------------ the 12 hour interval

    @Test
    fun `a check is due when none ever ran`() {
        assertTrue(UpdateSchedule.isDue(lastCheckMillis = 0L, nowMillis = 1_000L))
    }

    @Test
    fun `a check is not due before 12 hours and due from 12 hours on`() {
        val last = 1_000_000_000L
        assertFalse(UpdateSchedule.isDue(last, last))
        assertFalse(UpdateSchedule.isDue(last, last + 11 * hour + 59 * 60_000L))
        assertTrue(UpdateSchedule.isDue(last, last + 12 * hour))
        assertTrue(UpdateSchedule.isDue(last, last + 30 * hour))
    }

    @Test
    fun `the interval is twelve hours`() {
        assertEquals(12 * hour, UpdateSchedule.INTERVAL_MS)
    }

    @Test
    fun `a clock set back counts as due rather than waiting for days`() {
        assertTrue(UpdateSchedule.isDue(lastCheckMillis = 5_000_000L, nowMillis = 4_000_000L))
    }

    @Test
    fun `the interval is measured from the last recorded check`() {
        val store = UpdateStore(MemoryPrefs())
        assertEquals(0L, store.lastCheckMillis)
        store.recordCheck(nowMillis = 50 * hour, manifestText = null)
        assertEquals(50 * hour, store.lastCheckMillis)
        assertFalse(UpdateSchedule.isDue(store.lastCheckMillis, 61 * hour))
        assertTrue(UpdateSchedule.isDue(store.lastCheckMillis, 62 * hour))
    }

    // ------------------------------------------------------------------ "Later"

    @Test
    fun `Later hides that version and anything older, not a newer one`() {
        assertFalse(UpdateSchedule.isHidden(info(6), dismissedVersionCode = 0))
        assertTrue(UpdateSchedule.isHidden(info(6), dismissedVersionCode = 6))
        assertTrue(UpdateSchedule.isHidden(info(6), dismissedVersionCode = 7))
        assertFalse(UpdateSchedule.isHidden(info(7), dismissedVersionCode = 6))
    }

    @Test
    fun `Later is remembered across restarts and forgotten by the next check`() {
        val prefs = MemoryPrefs()
        val store = UpdateStore(prefs)
        store.recordCheck(1 * hour, manifestText(6))
        store.dismissedVersionCode = 6

        // A restart before the next check still hides it.
        val restarted = UpdateStore(prefs)
        assertEquals(6, restarted.dismissedVersionCode)
        assertTrue(UpdateSchedule.isHidden(restarted.rememberedUpdate(5, 34)!!, restarted.dismissedVersionCode))

        // The next check (automatic or manual) shows it again, even for the same version.
        restarted.recordCheck(14 * hour, manifestText(6))
        assertEquals(0, restarted.dismissedVersionCode)
        assertFalse(UpdateSchedule.isHidden(restarted.rememberedUpdate(5, 34)!!, restarted.dismissedVersionCode))
    }

    // ------------------------------------------------------------------ the remembered update

    @Test
    fun `the update found is remembered and re-validated on load`() {
        val store = UpdateStore(MemoryPrefs())
        assertNull(store.rememberedUpdate(5, 34))
        store.recordCheck(hour, manifestText(6))
        assertEquals(6, store.rememberedUpdate(installedVersionCode = 5, deviceSdk = 34)!!.versionCode)
    }

    @Test
    fun `a remembered update that is no longer newer, or not for this phone, is forgotten`() {
        val store = UpdateStore(MemoryPrefs())
        store.recordCheck(hour, manifestText(6))
        // The app was updated meanwhile.
        assertNull(store.rememberedUpdate(installedVersionCode = 6, deviceSdk = 34))
        assertNull("it is dropped from storage", store.foundManifest)

        store.recordCheck(hour, manifestText(7, minSdk = 35))
        assertNull(store.rememberedUpdate(installedVersionCode = 5, deviceSdk = 34))
    }

    @Test
    fun `tampered or invalid stored text is never trusted`() {
        val prefs = MemoryPrefs()
        val store = UpdateStore(prefs)
        store.foundManifest = manifestText(6).replace("dlne.rossgilsonlabs.dev", "evil.example")
        assertNull(store.rememberedUpdate(5, 34))
        store.foundManifest = "garbage"
        assertNull(store.rememberedUpdate(5, 34))
        assertNull(store.foundManifest)
    }

    @Test
    fun `a check that finds nothing clears what was remembered`() {
        val store = UpdateStore(MemoryPrefs())
        store.recordCheck(hour, manifestText(6))
        store.recordCheck(13 * hour, null)
        assertNull(store.foundManifest)
        assertNull(store.rememberedUpdate(5, 34))
    }

    // ------------------------------------------------------------------ the switch

    @Test
    fun `automatic checking is on by default and the choice is kept`() {
        val prefs = MemoryPrefs()
        assertTrue(UpdateStore(prefs).autoCheck)
        UpdateStore(prefs).autoCheck = false
        assertFalse(UpdateStore(prefs).autoCheck)
        UpdateStore(prefs).autoCheck = true
        assertTrue(UpdateStore(prefs).autoCheck)
    }
}

/** An in-memory SharedPreferences, so the store can be tested without a device. */
internal class MemoryPrefs : SharedPreferences {
    private val values = mutableMapOf<String, Any?>()

    override fun getAll(): MutableMap<String, *> = values
    override fun getString(key: String?, defValue: String?): String? = values[key] as String? ?: defValue
    override fun getStringSet(key: String?, defValues: MutableSet<String>?): MutableSet<String>? = defValues
    override fun getInt(key: String?, defValue: Int): Int = values[key] as Int? ?: defValue
    override fun getLong(key: String?, defValue: Long): Long = values[key] as Long? ?: defValue
    override fun getFloat(key: String?, defValue: Float): Float = values[key] as Float? ?: defValue
    override fun getBoolean(key: String?, defValue: Boolean): Boolean = values[key] as Boolean? ?: defValue
    override fun contains(key: String?): Boolean = values.containsKey(key)
    override fun edit(): SharedPreferences.Editor = Editor()
    override fun registerOnSharedPreferenceChangeListener(listener: SharedPreferences.OnSharedPreferenceChangeListener?) {}
    override fun unregisterOnSharedPreferenceChangeListener(listener: SharedPreferences.OnSharedPreferenceChangeListener?) {}

    private inner class Editor : SharedPreferences.Editor {
        private val puts = mutableMapOf<String, Any>()
        private val removes = mutableSetOf<String>()

        override fun putString(key: String?, value: String?): SharedPreferences.Editor =
            also { if (value == null) removes += key!! else puts[key!!] = value }
        override fun putStringSet(key: String?, values: MutableSet<String>?): SharedPreferences.Editor = also { }
        override fun putInt(key: String?, value: Int): SharedPreferences.Editor = also { puts[key!!] = value }
        override fun putLong(key: String?, value: Long): SharedPreferences.Editor = also { puts[key!!] = value }
        override fun putFloat(key: String?, value: Float): SharedPreferences.Editor = also { puts[key!!] = value }
        override fun putBoolean(key: String?, value: Boolean): SharedPreferences.Editor = also { puts[key!!] = value }
        override fun remove(key: String?): SharedPreferences.Editor = also { removes += key!! }
        override fun clear(): SharedPreferences.Editor = also { values.clear() }
        override fun commit(): Boolean {
            apply()
            return true
        }

        override fun apply() {
            // Removals first, then writes, so remove(k) followed by put(k) in one edit keeps the new value.
            removes.filter { it !in puts }.forEach { values.remove(it) }
            values.putAll(puts)
        }
    }
}
