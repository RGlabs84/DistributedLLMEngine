package io.dlne.chat.update

import io.dlne.chat.net.SearchEvents
import java.time.LocalDate
import java.time.format.DateTimeParseException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** Where updates come from. Fixed in the app: it never follows the self-hosted server address. */
internal object UpdateSource {
    const val HOST = "dlne.rossgilsonlabs.dev"
    const val MANIFEST_URL = "https://$HOST/downloads/android.json"

    /** The downloadable APK must be a single file name directly under this path. */
    const val DOWNLOADS_PATH = "/downloads/"

    /** Anything this big or bigger is refused; the app is about 2 MB. */
    const val MAX_APK_BYTES = 100L * 1024 * 1024

    /** Anything smaller is not an app. */
    const val MIN_APK_BYTES = 100_000L

    /** The manifest is a few hundred bytes; more than this is not a manifest. */
    const val MAX_MANIFEST_BYTES = 16 * 1024

    const val MAX_NOTES_CHARS = 300
}

/** A validated update description: every field has passed [UpdateManifestParser]. */
internal data class UpdateInfo(
    val versionCode: Int,
    val versionName: String,
    val minSdk: Int,
    /** https, on [UpdateSource.HOST], one `.apk` file under `/downloads/`. */
    val url: String,
    /** 64 lowercase hex digits. */
    val sha256: String,
    val size: Long,
    /** One plain line of at most [UpdateSource.MAX_NOTES_CHARS], possibly empty. */
    val notes: String,
    /** `yyyy-MM-dd`, or null when the manifest has none. */
    val published: String?,
)

internal sealed interface ManifestResult {
    data class Valid(val info: UpdateInfo) : ManifestResult

    /** Rejected; [reason] is for logs and tests, never shown as is. */
    data class Invalid(val reason: String) : ManifestResult
}

/** What a valid manifest means for the installed app on this phone. */
internal sealed interface UpdateDecision {
    /** Not newer than what is installed. */
    data object UpToDate : UpdateDecision

    /** Newer, but it needs a newer Android than this phone has. */
    data class NeedsNewerAndroid(val info: UpdateInfo) : UpdateDecision

    data class Available(val info: UpdateInfo) : UpdateDecision
}

/**
 * Strict reader of `https://dlne.rossgilsonlabs.dev/downloads/android.json`:
 *
 *     {"versionCode": 6, "versionName": "1.0.5", "minSdk": 26,
 *      "url": "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
 *      "sha256": "<64 hex>", "size": 2017073, "notes": "...", "published": "2026-10-27"}
 *
 * Numbers must be JSON integers (not strings, not 6.5), the address must be https on the
 * official host (no other host, port, user info, query or odd path), and the size must be
 * plausible. Anything else is rejected whole. Unknown extra fields are ignored.
 */
internal object UpdateManifestParser {
    private val json = Json { isLenient = false }
    private val INTEGER = Regex("[0-9]{1,10}")
    private val VERSION_NAME = Regex("[0-9A-Za-z][0-9A-Za-z._+-]{0,31}")
    private val SHA256 = Regex("[0-9a-fA-F]{64}")
    private val APK_PATH = Regex("/downloads/[A-Za-z0-9._-]{1,100}\\.apk")
    private val DATE = Regex("[0-9]{4}-[0-9]{2}-[0-9]{2}")
    private const val MAX_RAW_NOTES = 2_000
    private const val MAX_URL_CHARS = 300

    fun parse(text: String): ManifestResult {
        if (text.length > UpdateSource.MAX_MANIFEST_BYTES) return invalid("manifest too large")
        val root: JsonElement = try {
            json.parseToJsonElement(text)
        } catch (e: SerializationException) {
            return invalid("not JSON")
        } catch (e: IllegalArgumentException) {
            return invalid("not JSON")
        }
        val obj = root as? JsonObject ?: return invalid("not a JSON object")

        val versionCode = integer(obj, "versionCode")?.takeIf { it in 1..Int.MAX_VALUE }
            ?: return invalid("versionCode")
        val versionName = (obj["versionName"] as? JsonPrimitive)?.takeIf { it.isString }?.content
            ?.takeIf { VERSION_NAME.matches(it) }
            ?: return invalid("versionName")
        val minSdk = integer(obj, "minSdk")?.takeIf { it in 1..200 } ?: return invalid("minSdk")
        val rawUrl = (obj["url"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: return invalid("url")
        val url = validateUrl(rawUrl) ?: return invalid("url is not an official download")
        val sha256 = (obj["sha256"] as? JsonPrimitive)?.takeIf { it.isString }?.content
            ?.takeIf { SHA256.matches(it) }?.lowercase()
            ?: return invalid("sha256")
        val size = integer(obj, "size")?.takeIf { it >= UpdateSource.MIN_APK_BYTES && it < UpdateSource.MAX_APK_BYTES }
            ?: return invalid("size")

        val notes = when (val raw = obj["notes"]) {
            null -> ""
            is JsonPrimitive ->
                if (!raw.isString || raw.content.length > MAX_RAW_NOTES) return invalid("notes")
                else SearchEvents.cleanText(raw.content, UpdateSource.MAX_NOTES_CHARS)
            else -> return invalid("notes")
        }
        val published = when (val raw = obj["published"]) {
            null -> null
            is JsonPrimitive -> raw.takeIf { it.isString }?.content?.takeIf(::isDate) ?: return invalid("published")
            else -> return invalid("published")
        }
        return ManifestResult.Valid(UpdateInfo(versionCode.toInt(), versionName, minSdk.toInt(), url, sha256, size, notes, published))
    }

    /**
     * The canonical form of [raw] if it is a download we accept, else null: https, host exactly
     * [UpdateSource.HOST] on the default port, no user info, no query or fragment, no
     * percent-encoding or backslashes, and a path of exactly `/downloads/<name>.apk`.
     */
    fun validateUrl(raw: String): String? {
        if (raw.length > MAX_URL_CHARS) return null
        if (raw.any { it.isWhitespace() || it.isISOControl() || it in "\\%@?#" }) return null
        val url = raw.toHttpUrlOrNull() ?: return null
        if (!url.isHttps || url.host != UpdateSource.HOST || url.port != 443) return null
        if (url.username.isNotEmpty() || url.password.isNotEmpty()) return null
        if (url.query != null || url.fragment != null) return null
        val path = url.encodedPath
        if (!APK_PATH.matches(path) || path.contains("..")) return null
        return url.toString()
    }

    private fun isDate(text: String): Boolean {
        if (!DATE.matches(text)) return false
        return try {
            LocalDate.parse(text)
            true
        } catch (e: DateTimeParseException) {
            false
        }
    }

    /** A JSON integer written as digits only: not a string, not a fraction, not an exponent, not negative. */
    private fun integer(obj: JsonObject, name: String): Long? {
        val value = obj[name] as? JsonPrimitive ?: return null
        if (value.isString || !INTEGER.matches(value.content)) return null
        return value.content.toLongOrNull()
    }

    private fun invalid(reason: String) = ManifestResult.Invalid(reason)
}

/** Only a strictly higher versionCode is an update; a minSdk above the phone's is "not for this phone". */
internal fun decideUpdate(info: UpdateInfo, installedVersionCode: Int, deviceSdk: Int): UpdateDecision = when {
    info.versionCode <= installedVersionCode -> UpdateDecision.UpToDate
    info.minSdk > deviceSdk -> UpdateDecision.NeedsNewerAndroid(info)
    else -> UpdateDecision.Available(info)
}
