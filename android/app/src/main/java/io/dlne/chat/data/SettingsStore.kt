package io.dlne.chat.data

import android.content.Context
import android.content.SharedPreferences
import io.dlne.chat.net.ServerUrl
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/** The settings the app keeps: the server address, whether the privacy notice was read, and whether to share the location. */
class SettingsStore(private val prefs: SharedPreferences) {
    constructor(context: Context) : this(context.getSharedPreferences("dlne_settings", Context.MODE_PRIVATE))

    private val _serverUrl = MutableStateFlow(loadServerUrl())
    val serverUrl: StateFlow<String> = _serverUrl.asStateFlow()

    private val _privacyAcknowledged = MutableStateFlow(prefs.getBoolean(PREF_PRIVACY_ACK, false))
    val privacyAcknowledged: StateFlow<Boolean> = _privacyAcknowledged.asStateFlow()

    // Off until the person turns it on (and allows the permission it needs).
    private val _shareLocation = MutableStateFlow(prefs.getBoolean(PREF_SHARE_LOCATION, false))
    val shareLocation: StateFlow<Boolean> = _shareLocation.asStateFlow()

    /**
     * The saved address, with the old public address upgraded to the current default without
     * any message: an earlier version saved it, and the person never chose it. The saved value
     * is dropped, so the app simply follows the default from now on.
     */
    private fun loadServerUrl(): String {
        val stored = prefs.getString(PREF_SERVER_URL, null)
        if (ServerUrl.isLegacyDefault(stored)) prefs.edit().remove(PREF_SERVER_URL).apply()
        return ServerUrl.startupAddress(stored)
    }

    /** Saves [input] if it is a usable https address; returns false (and saves nothing) if not. */
    fun setServerUrl(input: String): Boolean {
        val url = ServerUrl.normalize(input) ?: return false
        prefs.edit().putString(PREF_SERVER_URL, url).apply()
        _serverUrl.value = url
        return true
    }

    fun resetServerUrl() {
        prefs.edit().remove(PREF_SERVER_URL).apply()
        _serverUrl.value = ServerUrl.DEFAULT
    }

    fun acknowledgePrivacy() {
        prefs.edit().putBoolean(PREF_PRIVACY_ACK, true).apply()
        _privacyAcknowledged.value = true
    }

    fun setShareLocation(on: Boolean) {
        prefs.edit().putBoolean(PREF_SHARE_LOCATION, on).apply()
        _shareLocation.value = on
    }

    private companion object {
        const val PREF_SERVER_URL = "server_url"
        const val PREF_PRIVACY_ACK = "privacy_acknowledged_v1"
        const val PREF_SHARE_LOCATION = "share_location"
    }
}
