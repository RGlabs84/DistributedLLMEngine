package io.dlne.chat.ui

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.size
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import io.dlne.chat.AppGraph
import io.dlne.chat.R
import io.dlne.chat.net.ApiException
import io.dlne.chat.net.ClusterStatus
import io.dlne.chat.net.VoiceMessages
import io.dlne.chat.net.voiceAvailable
import io.dlne.chat.net.voiceMaxSeconds
import io.dlne.chat.voice.MicUnavailableException
import io.dlne.chat.voice.VoiceRecorder
import io.dlne.chat.voice.Wav
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

/** The mic is offered only while the server's status says `capabilities.voice` (contract: the clients show the mic for nothing else). */
fun voiceButtonVisible(status: ClusterStatus?): Boolean = status?.voiceAvailable == true

/** "0:07": a recording's clock. */
fun clockText(seconds: Int): String = "%d:%02d".format(seconds / 60, seconds % 60)

/** Seconds recorded after [bytes] of PCM. */
fun secondsRecorded(bytes: Int): Int = bytes / Wav.BYTES_PER_SECOND

/** The text after inserting [insert] over the selection [start]..[end], and where the cursor then sits. */
data class Inserted(val text: String, val cursor: Int)

/**
 * Puts [insert] at the cursor (or over the selection) of [text]. A space is added on a side that would
 * otherwise glue the new words to a neighbouring word.
 */
fun insertAtCursor(text: String, start: Int, end: Int, insert: String): Inserted {
    val a = minOf(start, end).coerceIn(0, text.length)
    val b = maxOf(start, end).coerceIn(0, text.length)
    val before = text.substring(0, a)
    val after = text.substring(b)
    val lead = if (before.isNotEmpty() && !before.last().isWhitespace()) " " else ""
    val trail = if (after.isNotEmpty() && !after.first().isWhitespace()) " " else ""
    val added = lead + insert + trail
    return Inserted(before + added + after, before.length + lead.length + insert.length)
}

private enum class VoicePhase { IDLE, RECORDING, SENDING }

/**
 * The mic next to the message box. Tap to record, tap again to stop (it stops by itself at the
 * server's `voice.max_seconds`); the clip is turned into text by the server and handed to [onText] to be
 * put in the message box. Nothing is ever sent as a message from here. The first tap shows a one-time note,
 * then Android's own microphone question.
 */
@Composable
fun VoiceButton(
    status: ClusterStatus?,
    serverUrl: String,
    enabled: Boolean,
    /** True when the Voice mode setting is Conversation. */
    conversation: Boolean = false,
    /** An answer is being read aloud: the button then stops it. */
    speaking: Boolean = false,
    /** The text of the clip and the language the server detected for it (null if unknown). */
    onText: (String, String?) -> Unit,
    onStopSpeaking: () -> Unit = {},
    onMessage: (String?) -> Unit,
) {
    if (!voiceButtonVisible(status)) return
    val context = LocalContext.current
    val view = LocalView.current
    val scope = rememberCoroutineScope()
    val graph = remember { AppGraph.get(context) }
    val maxSeconds = status.voiceMaxSeconds()

    var phase by remember { mutableStateOf(VoicePhase.IDLE) }
    var seconds by remember { mutableIntStateOf(0) }
    var note by remember { mutableStateOf(false) }
    var recorder by remember { mutableStateOf<VoiceRecorder?>(null) }
    var job by remember { mutableStateOf<Job?>(null) }

    fun announce(text: String) = view.announceForAccessibility(text)

    fun fail(text: String) {
        onMessage(text)
        announce(text)
    }

    fun begin() {
        onMessage(null)
        onStopSpeaking() // starting to record ends any speech
        val rec = VoiceRecorder(Wav.maxPcmBytes(maxSeconds))
        recorder = rec
        seconds = 0
        phase = VoicePhase.RECORDING
        announce(context.getString(R.string.voice_recording_announce, maxSeconds))
        job = scope.launch {
            try {
                val pcm = try {
                    rec.record { seconds = secondsRecorded(it) }
                } catch (_: MicUnavailableException) {
                    phase = VoicePhase.IDLE
                    fail(VoiceMessages.MIC_FAILED)
                    return@launch
                }
                phase = VoicePhase.SENDING
                announce(context.getString(R.string.voice_sending))
                if (pcm.size < Wav.BYTES_PER_SECOND / 4) {
                    fail(VoiceMessages.NOTHING_HEARD)
                    return@launch
                }
                val clip = graph.api.transcribeClip(serverUrl, Wav.wrap(pcm))
                val text = clip.text
                if (text.isBlank()) {
                    fail(VoiceMessages.NOTHING_HEARD)
                } else {
                    onText(text, clip.language)
                    announce(context.getString(if (conversation) R.string.voice_sent else R.string.voice_inserted))
                }
            } catch (e: ApiException) {
                fail(VoiceMessages.forError(e))
            } catch (e: CancellationException) {
                throw e
            } finally {
                phase = VoicePhase.IDLE
                recorder = null
            }
        }
    }

    val askMic = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (granted) begin() else fail(VoiceMessages.NO_PERMISSION)
    }
    fun startWithPermission() {
        val granted = ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        if (granted) begin() else askMic.launch(Manifest.permission.RECORD_AUDIO)
    }

    // Leaving the screen ends the recording and drops it: nothing is sent.
    DisposableEffect(Unit) { onDispose { job?.cancel() } }

    if (note) {
        AlertDialog(
            onDismissRequest = { note = false },
            title = { Text(stringResource(R.string.voice_note_title)) },
            text = { Text(stringResource(R.string.voice_note_body)) },
            confirmButton = {
                TextButton(onClick = {
                    note = false
                    graph.settings.markVoiceNoteSeen()
                    startWithPermission()
                }) { Text(stringResource(R.string.voice_note_continue)) }
            },
            dismissButton = { TextButton(onClick = { note = false }) { Text(stringResource(R.string.voice_note_cancel)) } },
        )
    }

    val size = Modifier.size(52.dp)
    when (phase) {
        VoicePhase.IDLE -> if (speaking) {
            FilledIconButton(
                onClick = onStopSpeaking,
                colors = IconButtonDefaults.filledTonalIconButtonColors(),
                modifier = size,
            ) {
                Icon(painterResource(R.drawable.ic_speaking_stop), contentDescription = stringResource(R.string.voice_stop_speaking))
            }
        } else FilledIconButton(
            onClick = { if (graph.settings.voiceNoteSeen) startWithPermission() else note = true },
            enabled = enabled,
            colors = IconButtonDefaults.filledTonalIconButtonColors(),
            modifier = size,
        ) {
            Icon(painterResource(R.drawable.ic_mic), contentDescription = stringResource(R.string.voice_start))
        }

        VoicePhase.RECORDING -> {
            val description = stringResource(R.string.voice_stop) + ". " +
                stringResource(R.string.voice_recording, clockText(seconds), clockText(maxSeconds))
            FilledIconButton(
                onClick = { recorder?.stop() },
                colors = IconButtonDefaults.filledIconButtonColors(
                    containerColor = MaterialTheme.colorScheme.error,
                    contentColor = MaterialTheme.colorScheme.onError,
                ),
                modifier = size.semantics { contentDescription = description },
            ) {
                Text(clockText(seconds), style = MaterialTheme.typography.labelMedium)
            }
        }

        VoicePhase.SENDING -> FilledIconButton(
            onClick = {},
            enabled = false,
            modifier = size.semantics { contentDescription = context.getString(R.string.voice_working) },
        ) {
            CircularProgressIndicator(modifier = Modifier.size(22.dp), strokeWidth = 2.dp)
        }
    }
}
