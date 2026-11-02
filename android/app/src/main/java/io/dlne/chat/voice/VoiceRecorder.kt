package io.dlne.chat.voice

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext

/** The microphone could not be started (busy, no input, or access taken away). */
class MicUnavailableException : Exception("The microphone could not be started")

/**
 * Records 16 kHz mono PCM16 from the microphone with [AudioRecord] until [stop] is called, the
 * coroutine is cancelled, or [maxBytes] have been read. The audio stays in memory.
 *
 * [AudioRecord.read] blocks, so the whole recording runs on [Dispatchers.IO] whoever calls it: on the
 * main thread it froze the app (1.0.7) until the clip ended, and the stop tap was never seen.
 */
class VoiceRecorder(private val maxBytes: Int) {
    private val stopRequested = AtomicBoolean(false)

    /** Ends the recording after the chunk being read; [record] then returns what it has. */
    fun stop() = stopRequested.set(true)

    /** Needs RECORD_AUDIO already granted. [onProgress] gets the bytes recorded so far, after each chunk. */
    @SuppressLint("MissingPermission")
    suspend fun record(onProgress: (Int) -> Unit): ByteArray = withContext(Dispatchers.IO) { recordBlocking(onProgress) }

    @SuppressLint("MissingPermission")
    private suspend fun recordBlocking(onProgress: (Int) -> Unit): ByteArray {
        val minBuffer = AudioRecord.getMinBufferSize(Wav.SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
        if (minBuffer <= 0) throw MicUnavailableException()
        val recorder = try {
            AudioRecord(
                MediaRecorder.AudioSource.MIC,
                Wav.SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                maxOf(minBuffer, Wav.BYTES_PER_SECOND / 2),
            )
        } catch (_: SecurityException) {
            throw MicUnavailableException()
        } catch (_: IllegalArgumentException) {
            throw MicUnavailableException()
        }
        try {
            if (recorder.state != AudioRecord.STATE_INITIALIZED) throw MicUnavailableException()
            try {
                recorder.startRecording()
            } catch (_: IllegalStateException) {
                throw MicUnavailableException()
            }
            val pcm = ByteArray(maxBytes)
            var filled = 0
            val chunk = ByteArray(3200) // 100 ms
            while (filled < maxBytes && !stopRequested.get()) {
                currentCoroutineContext().ensureActive()
                val read = recorder.read(chunk, 0, minOf(chunk.size, maxBytes - filled))
                if (read < 0) throw MicUnavailableException()
                System.arraycopy(chunk, 0, pcm, filled, read)
                filled += read
                onProgress(filled)
            }
            return pcm.copyOf(filled)
        } finally {
            try {
                recorder.stop()
            } catch (_: IllegalStateException) {
            }
            recorder.release()
        }
    }
}
