package io.dlne.chat.ui

import android.graphics.BitmapFactory
import android.widget.Toast
import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextField
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import io.dlne.chat.AppGraph
import io.dlne.chat.R
import io.dlne.chat.data.ChatMessage
import io.dlne.chat.data.ReplyPicture
import io.dlne.chat.net.ClusterStatus
import io.dlne.chat.net.ImageStatus
import io.dlne.chat.net.MAX_IMAGE_PROMPT_CHARS
import io.dlne.chat.net.defaultImageSize
import io.dlne.chat.net.imageEtaSeconds
import io.dlne.chat.net.imageSizes
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** "Create image": the description and the size. [initialPrompt] is whatever is typed in the message box. */
@Composable
fun CreateImageDialog(
    initialPrompt: String,
    status: ClusterStatus?,
    onCreate: (prompt: String, size: String) -> Unit,
    onDismiss: () -> Unit,
) {
    var prompt by rememberSaveable { mutableStateOf(initialPrompt.take(MAX_IMAGE_PROMPT_CHARS)) }
    val sizes = status?.imageSizes() ?: io.dlne.chat.net.DEFAULT_IMAGE_SIZES
    var size by rememberSaveable { mutableStateOf(status?.defaultImageSize() ?: io.dlne.chat.net.DEFAULT_IMAGE_SIZE) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.create_image)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                TextField(
                    value = prompt,
                    onValueChange = { prompt = it.take(MAX_IMAGE_PROMPT_CHARS) },
                    label = { Text(stringResource(R.string.create_image_prompt_label)) },
                    supportingText = {
                        Text(stringResource(R.string.create_image_count, prompt.length, MAX_IMAGE_PROMPT_CHARS))
                    },
                    minLines = 2,
                    maxLines = 5,
                    keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences),
                    modifier = Modifier.fillMaxWidth(),
                )
                Text(stringResource(R.string.create_image_size), style = MaterialTheme.typography.labelLarge)
                Column(Modifier.selectableGroup()) {
                    for (option in sizes) {
                        Row(
                            verticalAlignment = Alignment.CenterVertically,
                            modifier = Modifier
                                .fillMaxWidth()
                                .selectable(selected = option == size, role = Role.RadioButton, onClick = { size = option }),
                        ) {
                            RadioButton(selected = option == size, onClick = null, modifier = Modifier.padding(12.dp))
                            Text(sizeLabel(option, status), style = MaterialTheme.typography.bodyMedium)
                        }
                    }
                }
                Text(
                    stringResource(R.string.create_image_note),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        },
        confirmButton = {
            TextButton(onClick = { onCreate(prompt, size) }, enabled = prompt.isNotBlank()) {
                Text(stringResource(R.string.create_image_start))
            }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(stringResource(R.string.cancel)) } },
    )
}

/** "768 × 768 (square), about 80 s": the shape and the server's typical time when it said one. */
@Composable
private fun sizeLabel(size: String, status: ClusterStatus?): String {
    val parts = size.split('x')
    val w = parts.getOrNull(0)?.toIntOrNull()
    val h = parts.getOrNull(1)?.toIntOrNull()
    if (w == null || h == null) return size
    val shape = stringResource(
        when {
            w == h -> R.string.image_shape_square
            w < h -> R.string.image_shape_portrait
            else -> R.string.image_shape_landscape
        },
    )
    val base = stringResource(R.string.image_size_label, "$w × $h", shape)
    val eta = status?.imageEtaSeconds(size) ?: return base
    return stringResource(R.string.image_size_eta, base, eta)
}

/** The card for an image being made: the stage, elapsed time against the estimate, and Cancel. */
@Composable
fun ImageJobCard(job: ImageJobUi, onCancel: () -> Unit) {
    // The server's clock is read every 2 s; count the seconds in between so the card moves.
    var extra by remember(job.elapsedSeconds, job.status) { mutableIntStateOf(0) }
    LaunchedEffect(job.elapsedSeconds, job.status) {
        while (true) {
            delay(1_000)
            extra++
        }
    }
    val elapsed = job.elapsedSeconds + extra
    val eta = job.etaSeconds
    val stage = when {
        job.cancelling -> stringResource(R.string.image_status_cancelling)
        job.status == ImageStatus.QUEUED && job.position != null && job.position > 1 ->
            stringResource(R.string.image_status_position, job.position)
        job.status == ImageStatus.QUEUED -> stringResource(R.string.image_status_queued)
        job.status == ImageStatus.CHECKING -> stringResource(R.string.image_status_checking)
        job.status == ImageStatus.REVIEWING -> stringResource(R.string.image_status_reviewing)
        eta != null -> stringResource(R.string.image_status_generating, eta)
        else -> stringResource(R.string.image_status_generating_noeta)
    }
    val timing = if (eta != null && elapsed > eta + 15) {
        stringResource(R.string.image_taking_longer, elapsed)
    } else {
        stringResource(R.string.image_elapsed, elapsed)
    }
    val description = stringResource(R.string.image_job_description, job.prompt)
    Surface(
        shape = RoundedCornerShape(16.dp),
        color = MaterialTheme.colorScheme.surfaceContainerHigh,
        modifier = Modifier.fillMaxWidth().widthIn(max = 560.dp),
    ) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
            modifier = Modifier.padding(start = 16.dp, end = 8.dp, top = 12.dp, bottom = 12.dp),
        ) {
            CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
            Column(
                Modifier
                    .weight(1f)
                    // The stage is announced when it changes; the seconds ticking are not.
                    .semantics { contentDescription = "$description. $stage"; liveRegion = LiveRegionMode.Polite },
                verticalArrangement = Arrangement.spacedBy(2.dp),
            ) {
                Text(stage, style = MaterialTheme.typography.bodyMedium)
                Text(timing, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            // Until the server has answered there is no job to cancel yet (a second or two).
            TextButton(onClick = onCancel, enabled = !job.cancelling && job.jobId != null) { Text(stringResource(R.string.cancel)) }
        }
    }
}

/**
 * A finished image from the chat: the picture with Save and Share (and Report), or, when the
 * request failed or the file is gone, the reason in the quiet notice style.
 */
@Composable
fun ImageResult(message: ChatMessage, onReport: (() -> Unit)?) {
    val attachment = message.image ?: return
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        if (attachment.file != null) StoredPicture(attachment.file, attachment.prompt, onReport)
        message.notice?.let { NoticeRow(it, onRetry = null) }
    }
}

/**
 * A picture kept on the phone: the picture with Save and Share (and Report when [onReport] is given),
 * or a line saying it is no longer stored when its file is gone. It emits its parts straight into the
 * caller's column, so the caller's spacing applies.
 */
@Composable
private fun StoredPicture(fileName: String, prompt: String, onReport: (() -> Unit)?) {
    val context = LocalContext.current
    val file = remember(fileName) { AppGraph.get(context).images.file(fileName) }
    val bitmap by produceState<ImageBitmap?>(null, file) {
        value = file?.let { f -> withContext(Dispatchers.IO) { BitmapFactory.decodeFile(f.path)?.asImageBitmap() } }
    }
    val shown = bitmap
    if (shown != null) {
        Image(
            bitmap = shown,
            contentDescription = stringResource(R.string.image_description, prompt),
            contentScale = ContentScale.Fit,
            modifier = Modifier.widthIn(max = 420.dp).fillMaxWidth().clip(RoundedCornerShape(16.dp)),
        )
    } else if (file == null) {
        Text(
            stringResource(R.string.image_missing),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
    if (file != null) ImageActions(file, onReport)
}

/**
 * The picture a reply made, inside that reply: the job's card while [job] runs, then the picture with
 * Save, Share and (when [onReport] is given) Report. With no picture it says why, in the quiet notice
 * style: the server's own words for a refusal, or what went wrong. A picture the app has neither a job
 * nor a result for (the app was closed while it was being made) says it was not finished.
 */
@Composable
fun ReplyPictureView(picture: ReplyPicture, job: ImageJobUi?, onCancel: (String) -> Unit, onReport: (() -> Unit)?) {
    when {
        picture.file != null -> StoredPicture(picture.file, picture.prompt, onReport)
        job != null && picture.pending -> ImageJobCard(job, onCancel = { job.jobId?.let(onCancel) })
        picture.note != null -> NoticeRow(picture.note, onRetry = null)
        else -> NoticeRow(stringResource(R.string.picture_unfinished), onRetry = null)
    }
}

@Composable
private fun ImageActions(file: java.io.File, onReport: (() -> Unit)?) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val savedText = stringResource(R.string.image_saved)
    val errorText = stringResource(R.string.image_save_error)
    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        if (canSaveToPictures) {
            TextButton(onClick = {
                scope.launch {
                    val saved = withContext(Dispatchers.IO) { saveToPictures(context, file) }
                    Toast.makeText(context, if (saved) savedText else errorText, Toast.LENGTH_SHORT).show()
                }
            }) { Text(stringResource(R.string.image_save)) }
        }
        TextButton(onClick = { shareImage(context, file) }) { Text(stringResource(R.string.image_share)) }
        if (onReport != null) TextButton(onClick = onReport) { Text(stringResource(R.string.report_answer)) }
    }
}
