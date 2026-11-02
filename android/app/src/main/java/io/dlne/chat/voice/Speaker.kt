package io.dlne.chat.voice

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.os.Bundle
import java.util.Locale
import java.util.concurrent.ConcurrentHashMap

/**
 * The device's own voice for Conversation mode. Created lazily on first use (so Dictate users never start the
 * speech engine) and shut down by [shutdown]. Nothing here blocks the main thread: the engine starts itself and
 * calls back; sentences said before it is ready wait in a small list.
 */
class Speaker(
    private val context: Context,
    /** Whether anything is being said or waiting: drives the mic button's stop-speaking state. */
    private val onSpeaking: (Boolean) -> Unit,
    /** The engine could not start or has no voice: told once (see [UNAVAILABLE_NOTE]). */
    private val onUnavailable: () -> Unit,
) {
    private var tts: TextToSpeech? = null
    private var ready = false
    private var failed = false
    private var language: String? = null
    private val waiting = ArrayList<String>()
    private val live: MutableSet<String> = ConcurrentHashMap.newKeySet()
    private var counter = 0
    private val audio = context.applicationContext.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    private val attributes = AudioAttributes.Builder()
        .setUsage(AudioAttributes.USAGE_ASSISTANT)
        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
        .build()
    private var focus: AudioFocusRequest? = null

    // Other audio ducks or pauses while the answer is spoken, and comes back after.
    private fun takeFocus() {
        if (focus != null) return
        val request = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
            .setAudioAttributes(attributes)
            .setOnAudioFocusChangeListener { }
            .build()
        audio.requestAudioFocus(request)
        focus = request
    }

    private fun dropFocus() {
        focus?.let { audio.abandonAudioFocusRequest(it) }
        focus = null
    }

    private fun speaking(on: Boolean) {
        if (on) takeFocus() else dropFocus()
        onSpeaking(on)
    }

    /** Says [sentences] after whatever is already queued. */
    fun speak(sentences: List<String>, languageTag: String?) {
        if (sentences.isEmpty() || failed) return
        if (languageTag != null) language = languageTag
        if (tts == null) start()
        if (failed) return
        if (!ready) {
            waiting += sentences
            return
        }
        enqueue(sentences)
    }

    private fun start() {
        val engine = TextToSpeech(context.applicationContext) { status ->
            // Called on the main thread once the engine is bound; setLanguage is a quick call.
            if (status != TextToSpeech.SUCCESS) {
                fail()
                return@TextToSpeech
            }
            val t = tts ?: return@TextToSpeech
            val locale = language?.let { Locale.forLanguageTag(it) } ?: Locale.getDefault()
            val result = t.setLanguage(locale)
            if (result == TextToSpeech.LANG_MISSING_DATA || result == TextToSpeech.LANG_NOT_SUPPORTED) {
                val fallback = t.setLanguage(Locale.getDefault())
                if (fallback == TextToSpeech.LANG_MISSING_DATA || fallback == TextToSpeech.LANG_NOT_SUPPORTED) {
                    fail()
                    return@TextToSpeech
                }
            }
            ready = true
            val pending = waiting.toList()
            waiting.clear()
            enqueue(pending)
        }
        engine.setAudioAttributes(attributes)
        engine.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = Unit
            override fun onDone(utteranceId: String?) = finished(utteranceId)
            @Deprecated("Deprecated in Java")
            override fun onError(utteranceId: String?) = finished(utteranceId)
            override fun onStop(utteranceId: String?, interrupted: Boolean) = finished(utteranceId)
        })
        tts = engine
    }

    private fun enqueue(sentences: List<String>) {
        val t = tts ?: return
        for (sentence in sentences) {
            val id = "dlne-${counter++}"
            live += id
            speaking(true)
            // QUEUE_ADD: sentences of one answer follow each other.
            if (t.speak(sentence, TextToSpeech.QUEUE_ADD, Bundle(), id) != TextToSpeech.SUCCESS) {
                live -= id
            }
        }
        if (live.isEmpty()) speaking(false)
    }

    private fun finished(id: String?) {
        if (id != null) live -= id
        if (live.isEmpty()) speaking(false)
    }

    private fun fail() {
        if (failed) return
        failed = true
        waiting.clear()
        live.clear()
        speaking(false)
        tts?.shutdown()
        tts = null
        onUnavailable()
    }

    /** Stops what is being said and drops what waits. */
    fun stop() {
        waiting.clear()
        tts?.stop()
        live.clear()
        speaking(false)
    }

    fun shutdown() {
        waiting.clear()
        live.clear()
        tts?.stop()
        tts?.shutdown()
        tts = null
        ready = false
        speaking(false)
    }

    companion object {
        const val UNAVAILABLE_NOTE =
            "This phone has no voice to read answers aloud, so they stay text. To add one, open Android Settings > Accessibility > Text-to-speech."
    }
}
