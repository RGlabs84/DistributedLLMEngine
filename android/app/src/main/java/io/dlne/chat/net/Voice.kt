package io.dlne.chat.net

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** `voice` of the status: queue, whether a clip is being turned into text, and the longest clip. */
@Serializable
data class VoiceInfo(
    val queue: Int? = null,
    val busy: Boolean? = null,
    @SerialName("max_seconds") val maxSeconds: Int? = null,
)

const val DEFAULT_VOICE_MAX_SECONDS = 60

/** The mic is shown only while the server says `capabilities.voice` is true (older servers never say it). */
val ClusterStatus.voiceAvailable: Boolean get() = capabilities?.voice == true

/** The longest clip to record: the server's `voice.max_seconds`, kept between 1 and the contract's 60. */
fun ClusterStatus?.voiceMaxSeconds(): Int =
    (this?.voiceInfo?.maxSeconds ?: DEFAULT_VOICE_MAX_SECONDS).coerceIn(1, DEFAULT_VOICE_MAX_SECONDS)

@Serializable
internal data class TranscriptionAnswer(val text: String = "", val language: String? = null)

/** A clip turned into text, and the language the server detected for it (null when it did not say). */
data class Transcription(val text: String, val language: String? = null)

/** Plain words for what went wrong with a clip (docs/voice-contract.md). */
object VoiceMessages {
    const val QUOTA = "Voice is used up for this hour. Try again later, or type your message."
    const val BUSY = "Voice is busy right now. Try again in a moment."
    const val OFFLINE = "Could not reach the DLNE server. Check your connection and try again."
    const val NOTHING_HEARD = "Nothing was heard. Try again a little closer to the microphone."
    const val UNREADABLE = "The server's answer could not be read. Try again."
    const val NO_PERMISSION =
        "Microphone access is off. To use voice, allow it in Android Settings > Apps > DLNE Chat > Permissions."
    const val MIC_FAILED = "The microphone could not be started. Another app may be using it."

    /** The sentence to show for [error]. */
    fun forError(error: ApiException): String = when (error) {
        is ApiException.Network -> OFFLINE
        is ApiException.Http -> when (error.errorCode) {
            "voice_quota" -> QUOTA
            "voice_busy" -> BUSY
            "voice_paused", "voice_unavailable" -> error.detail
            "voice_bad_audio" -> "That recording could not be used: ${error.detail}"
            else -> error.detail
        }
    }
}
