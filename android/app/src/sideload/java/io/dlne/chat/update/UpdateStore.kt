package io.dlne.chat.update

import android.content.Context
import android.content.SharedPreferences

/** When the automatic check runs, and what "Later" hides. Pure functions, tested without a phone. */
internal object UpdateSchedule {
    const val INTERVAL_MS = 12L * 60 * 60 * 1000

    /**
     * True when an automatic check is due: never checked, 12 hours (or more) since the last
     * successful one, or the clock was set back behind the last check.
     */
    fun isDue(lastCheckMillis: Long, nowMillis: Long, intervalMs: Long = INTERVAL_MS): Boolean =
        lastCheckMillis <= 0L || nowMillis < lastCheckMillis || nowMillis - lastCheckMillis >= intervalMs

    /** "Later" was tapped for [dismissedVersionCode]: that version (or an older one) stays hidden. */
    fun isHidden(info: UpdateInfo, dismissedVersionCode: Int): Boolean =
        dismissedVersionCode >= info.versionCode
}

/**
 * The updater's small private memory: the switch, the time of the last successful check, the
 * version "Later" was tapped for, and the last update found (kept as the manifest text and
 * checked again on load, so it shows without network until the next check replaces it).
 */
internal class UpdateStore(private val prefs: SharedPreferences) {
    constructor(context: Context) : this(context.getSharedPreferences("dlne_updates", Context.MODE_PRIVATE))

    var autoCheck: Boolean
        get() = prefs.getBoolean(KEY_AUTO, true)
        set(value) = prefs.edit().putBoolean(KEY_AUTO, value).apply()

    var lastCheckMillis: Long
        get() = prefs.getLong(KEY_LAST, 0L)
        set(value) = prefs.edit().putLong(KEY_LAST, value).apply()

    /** The versionCode "Later" hid, or 0 for none. */
    var dismissedVersionCode: Int
        get() = prefs.getInt(KEY_DISMISSED, 0)
        set(value) = prefs.edit().putInt(KEY_DISMISSED, value).apply()

    /** The manifest text of the update found by the last check, or null when there was none. */
    var foundManifest: String?
        get() = prefs.getString(KEY_FOUND, null)
        set(value) {
            val edit = prefs.edit()
            if (value == null) edit.remove(KEY_FOUND) else edit.putString(KEY_FOUND, value)
            edit.apply()
        }

    /**
     * The update remembered from the last check, if it is valid and still newer than the
     * installed app and fits this phone. Anything else is forgotten.
     */
    fun rememberedUpdate(installedVersionCode: Int, deviceSdk: Int): UpdateInfo? {
        val text = foundManifest ?: return null
        val valid = (UpdateManifestParser.parse(text) as? ManifestResult.Valid)?.info
        val decision = valid?.let { decideUpdate(it, installedVersionCode, deviceSdk) }
        if (decision is UpdateDecision.Available) return decision.info
        foundManifest = null
        return null
    }

    /** A check finished: remember when, and what it found ([manifestText] null when nothing). "Later" is forgotten. */
    fun recordCheck(nowMillis: Long, manifestText: String?) {
        prefs.edit()
            .putLong(KEY_LAST, nowMillis)
            .putInt(KEY_DISMISSED, 0)
            .also { if (manifestText == null) it.remove(KEY_FOUND) else it.putString(KEY_FOUND, manifestText) }
            .apply()
    }

    private companion object {
        const val KEY_AUTO = "auto_check"
        const val KEY_LAST = "last_check_ms"
        const val KEY_DISMISSED = "dismissed_version_code"
        const val KEY_FOUND = "found_manifest"
    }
}
