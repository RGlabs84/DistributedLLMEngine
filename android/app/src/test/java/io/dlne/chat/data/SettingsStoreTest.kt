package io.dlne.chat.data

import android.content.SharedPreferences
import io.dlne.chat.net.ServerUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The public server moved from https://73-215-216-195.sslip.io to https://dlne.duckdns.org, then
 * (1.0.4) to https://dlne.rossgilsonlabs.dev. An address saved by an earlier version must be
 * upgraded silently when the app starts.
 */
class SettingsStoreTest {
    private val legacy = "https://73-215-216-195.sslip.io"

    @Test
    fun `a saved old public address starts as the new default and is dropped from storage`() {
        val prefs = FakePrefs("server_url" to legacy)
        val store = SettingsStore(prefs)

        assertEquals("https://dlne.rossgilsonlabs.dev", store.serverUrl.value)
        assertEquals(ServerUrl.DEFAULT, store.serverUrl.value)
        assertFalse("the stale address must not stay saved", prefs.contains("server_url"))
        // A second start finds nothing saved and also uses the default.
        assertEquals(ServerUrl.DEFAULT, SettingsStore(prefs).serverUrl.value)
    }

    @Test
    fun `the old address is upgraded however it was saved`() {
        for (saved in listOf("https://73-215-216-195.sslip.io/", "HTTPS://73-215-216-195.SSLIP.IO", " 73-215-216-195.sslip.io ")) {
            val prefs = FakePrefs("server_url" to saved)
            assertEquals(saved, ServerUrl.DEFAULT, SettingsStore(prefs).serverUrl.value)
            assertFalse(saved, prefs.contains("server_url"))
        }
    }

    @Test
    fun `a self-hosted address is left alone`() {
        val prefs = FakePrefs("server_url" to "https://dlne.example.org")
        val store = SettingsStore(prefs)

        assertEquals("https://dlne.example.org", store.serverUrl.value)
        assertEquals("https://dlne.example.org", prefs.values["server_url"])
    }

    @Test
    fun `nothing saved starts on the default without writing anything`() {
        val prefs = FakePrefs()
        val store = SettingsStore(prefs)

        assertEquals(ServerUrl.DEFAULT, store.serverUrl.value)
        assertEquals(0, prefs.writes)
    }

    @Test
    fun `the new default saved explicitly stays the new default`() {
        val prefs = FakePrefs("server_url" to ServerUrl.DEFAULT)
        assertEquals(ServerUrl.DEFAULT, SettingsStore(prefs).serverUrl.value)
    }

    @Test
    fun `an unusable saved address falls back to the default`() {
        val prefs = FakePrefs("server_url" to "http://insecure.example.org")
        assertEquals(ServerUrl.DEFAULT, SettingsStore(prefs).serverUrl.value)
    }

    @Test
    fun `the old address is still accepted in settings and upgraded on the next start`() {
        val prefs = FakePrefs()
        val store = SettingsStore(prefs)

        assertTrue(store.setServerUrl(legacy))
        assertEquals(legacy, store.serverUrl.value) // it still works, so this session may use it
        assertEquals(ServerUrl.DEFAULT, SettingsStore(prefs).serverUrl.value) // next start: upgraded
    }

    @Test
    fun `upgrading the address leaves the other setting alone`() {
        val prefs = FakePrefs("server_url" to legacy, "privacy_acknowledged_v1" to true)
        val store = SettingsStore(prefs)

        assertTrue(store.privacyAcknowledged.value)
        assertEquals(true, prefs.values["privacy_acknowledged_v1"])
    }

    @Test
    fun `use default goes to the new default`() {
        val store = SettingsStore(FakePrefs("server_url" to "https://dlne.example.org"))
        store.resetServerUrl()
        assertEquals("https://dlne.rossgilsonlabs.dev", store.serverUrl.value)
    }

    @Test
    fun `a saved DuckDNS address (1_0_1 to 1_0_3) moves to the new name and is dropped from storage`() {
        val prefs = FakePrefs("server_url" to "https://dlne.duckdns.org")
        val store = SettingsStore(prefs)
        assertEquals(ServerUrl.DEFAULT, store.serverUrl.value)
        assertFalse(prefs.contains("server_url"))
    }
}

/** An in-memory SharedPreferences, so the store can be tested without a device. */
private class FakePrefs(vararg initial: Pair<String, Any>) : SharedPreferences {
    val values: MutableMap<String, Any> = mutableMapOf(*initial)
    var writes = 0

    override fun getAll(): MutableMap<String, *> = values
    override fun getString(key: String?, defValue: String?): String? = values[key] as String? ?: defValue
    override fun getStringSet(key: String?, defValues: MutableSet<String>?): MutableSet<String>? = defValues
    override fun getInt(key: String?, defValue: Int): Int = defValue
    override fun getLong(key: String?, defValue: Long): Long = defValue
    override fun getFloat(key: String?, defValue: Float): Float = defValue
    override fun getBoolean(key: String?, defValue: Boolean): Boolean = values[key] as Boolean? ?: defValue
    override fun contains(key: String?): Boolean = values.containsKey(key)
    override fun edit(): SharedPreferences.Editor = FakeEditor()
    override fun registerOnSharedPreferenceChangeListener(listener: SharedPreferences.OnSharedPreferenceChangeListener?) {}
    override fun unregisterOnSharedPreferenceChangeListener(listener: SharedPreferences.OnSharedPreferenceChangeListener?) {}

    private inner class FakeEditor : SharedPreferences.Editor {
        private val puts = mutableMapOf<String, Any>()
        private val removes = mutableSetOf<String>()
        private var cleared = false

        override fun putString(key: String?, value: String?): SharedPreferences.Editor = also {
            if (value == null) removes += key!! else puts[key!!] = value
        }
        override fun putStringSet(key: String?, values: MutableSet<String>?): SharedPreferences.Editor = also {}
        override fun putInt(key: String?, value: Int): SharedPreferences.Editor = also { puts[key!!] = value }
        override fun putLong(key: String?, value: Long): SharedPreferences.Editor = also { puts[key!!] = value }
        override fun putFloat(key: String?, value: Float): SharedPreferences.Editor = also { puts[key!!] = value }
        override fun putBoolean(key: String?, value: Boolean): SharedPreferences.Editor = also { puts[key!!] = value }
        override fun remove(key: String?): SharedPreferences.Editor = also { removes += key!! }
        override fun clear(): SharedPreferences.Editor = also { cleared = true }
        override fun commit(): Boolean {
            apply()
            return true
        }

        override fun apply() {
            writes++
            if (cleared) values.clear()
            removes.forEach { values.remove(it) }
            values.putAll(puts)
        }
    }
}
