package io.dlne.chat.ui

import android.net.Uri
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.Image
import androidx.compose.foundation.combinedClickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.calculateEndPadding
import androidx.compose.foundation.layout.calculateStartPadding
import androidx.compose.foundation.layout.WindowInsetsSides
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.ime
import androidx.compose.foundation.layout.navigationBars
import androidx.compose.foundation.layout.only
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.union
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilledIconButton
import androidx.compose.material3.FilledTonalIconButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextField
import androidx.compose.material3.TextFieldDefaults
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalLayoutDirection
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.dlne.chat.R
import io.dlne.chat.data.ChatMessage
import io.dlne.chat.data.ReportReason
import io.dlne.chat.data.Role
import io.dlne.chat.net.ClusterStatus
import io.dlne.chat.update.UpdateBanner
import java.io.File
import kotlinx.coroutines.delay

/** What the chat screen can ask the view model to do. */
class ChatActions(
    val onOpenDrawer: () -> Unit,
    val onNewChat: () -> Unit,
    val onDraftChange: (String) -> Unit,
    val onSend: () -> Unit,
    val onStop: () -> Unit,
    val onRetry: () -> Unit,
    val onDismissError: () -> Unit,
    val onReport: (messageId: String) -> Unit,
    val onSendReport: (ReportReason, String) -> Unit,
    val onCloseReport: () -> Unit,
    val onReportThanksShown: () -> Unit,
    val onCreateImage: (prompt: String, size: String) -> Unit,
    /** Cancels the picture being made under this job id (several can run at once). */
    val onCancelImage: (jobId: String) -> Unit,
    /** The person answered the "Use your location for this?" card. */
    val onAnswerLocationAsk: (LocationAnswer) -> Unit,
    /** True when Android's location permission is already given. */
    val locationPermitted: () -> Boolean,
    /** Photos chosen with the "+" menu (and the camera's temporary file, when it came from the camera). */
    val onAddPhotos: (uris: List<Uri>, cameraFile: File?) -> Unit,
    val onRemovePhoto: (id: String) -> Unit,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ChatScreen(
    state: ChatUiState,
    draft: String,
    photos: DraftPhotos,
    report: ReportUiState?,
    reportThanksPending: Boolean,
    actions: ChatActions,
) {
    val snackbarHostState = remember { SnackbarHostState() }
    val thanks = stringResource(R.string.report_thanks)
    LaunchedEffect(reportThanksPending) {
        if (reportThanksPending) {
            actions.onReportThanksShown()
            snackbarHostState.showSnackbar(thanks)
        }
    }
    report?.let { ReportSheet(it, onSend = actions.onSendReport, onDismiss = actions.onCloseReport) }

    Scaffold(
        snackbarHost = { SnackbarHost(snackbarHostState) },
        topBar = {
            Column {
            TopAppBar(
                title = {
                    Column {
                        Text(stringResource(R.string.app_name), fontWeight = FontWeight.SemiBold)
                        statusLine(state.status)?.let { line ->
                            Text(
                                line,
                                style = MaterialTheme.typography.labelMedium,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                            )
                        }
                    }
                },
                navigationIcon = {
                    IconButton(onClick = actions.onOpenDrawer) {
                        Icon(painterResource(R.drawable.ic_menu), contentDescription = stringResource(R.string.open_chats))
                    }
                },
                actions = {
                    IconButton(onClick = actions.onNewChat) {
                        Icon(painterResource(R.drawable.ic_add), contentDescription = stringResource(R.string.new_chat))
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(containerColor = MaterialTheme.colorScheme.surface),
            )
            // Sideload build: "DLNE Chat x is available" card when there is an update. Play build: nothing.
            UpdateBanner()
            }
        },
        bottomBar = {
            InputBar(
                draft = draft,
                photos = photos,
                state = state,
                actions = actions,
            )
        },
    ) { padding ->
        val messages = state.current?.messages.orEmpty()
        val pending = state.pendingForCurrent
        val imageJobs = state.imageJobsForCurrent
        if (messages.isEmpty() && pending == null && imageJobs.isEmpty()) {
            EmptyState(Modifier.fillMaxSize().padding(padding))
        } else {
            MessageList(
                messages = messages,
                pending = pending,
                imageJobs = imageJobs,
                onCancelImage = actions.onCancelImage,
                canRetry = !state.isStreaming && state.error == null,
                onRetry = actions.onRetry,
                onReport = actions.onReport,
                contentPadding = padding,
            )
        }
    }
}

/** "Qwen3 4B Instruct · 3 machines serving", from the service status, when available. */
@Composable
private fun statusLine(status: ClusterStatus?): String? {
    val ready = status?.appPool?.membersReady ?: 0
    return statusLineOf(
        status,
        noMachines = stringResource(R.string.status_no_machines),
        machines = if (ready > 0) pluralStringResource(R.plurals.status_machines, ready, ready) else "",
    )
}

@Composable
private fun EmptyState(modifier: Modifier) {
    Box(modifier.padding(horizontal = 32.dp), contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(12.dp)) {
            // The official DLNE logo, unchanged (see mipmap-anydpi/ic_launcher.xml).
            Image(
                painterResource(R.drawable.dlne_logo),
                contentDescription = null,
                modifier = Modifier.size(112.dp).clip(RoundedCornerShape(22.dp)),
            )
            Text(stringResource(R.string.empty_title), style = MaterialTheme.typography.titleLarge, textAlign = TextAlign.Center)
            Text(
                stringResource(R.string.empty_body),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
        }
    }
}

/**
 * The conversation, newest at the bottom. The list is laid out in reverse (item 0 is the
 * bottom), so a streaming answer grows upward while its newest line stays in view.
 */
@Composable
private fun MessageList(
    messages: List<ChatMessage>,
    pending: PendingReply?,
    imageJobs: List<ImageJobUi>,
    onCancelImage: (String) -> Unit,
    canRetry: Boolean,
    onRetry: () -> Unit,
    onReport: (String) -> Unit,
    contentPadding: PaddingValues,
) {
    val listState = rememberLazyListState()
    // Jump to the newest message whenever one is added (sent, or an answer saved), or a picture starts or ends.
    LaunchedEffect(messages.size, pending != null, imageJobs.size) { listState.scrollToItem(0) }

    val last = messages.lastOrNull()
    val unanswered = canRetry && pending == null && last?.role == Role.USER && last.image == null
    val direction = LocalLayoutDirection.current

    // Answers whose sources the reader unfolded, by message id. Held here rather than in each
    // answer so the streaming reply (known by the id it will be saved under) stays unfolded
    // when it is saved; rememberSaveable keeps it through scrolling and rotation.
    var openSources by rememberSaveable { mutableStateOf(listOf<String>()) }
    val toggleSources = { id: String -> openSources = if (id in openSources) openSources - id else openSources + id }

    LazyColumn(
        state = listState,
        reverseLayout = true,
        contentPadding = PaddingValues(
            // Side padding includes any landscape navigation bar or display cutout.
            start = contentPadding.calculateStartPadding(direction) + 16.dp,
            end = contentPadding.calculateEndPadding(direction) + 16.dp,
            top = contentPadding.calculateTopPadding() + 12.dp,
            bottom = contentPadding.calculateBottomPadding() + 12.dp,
        ),
        verticalArrangement = Arrangement.spacedBy(16.dp, Alignment.Bottom),
        modifier = Modifier.fillMaxSize(),
    ) {
        if (unanswered) {
            item(key = "unanswered") { NoticeRow(stringResource(R.string.not_answered), onRetry) }
        }
        // A Create image request or picture in progress is a card of its own; the pictures the chat makes
        // sit inside the reply that asked for them (below).
        for (job in imageJobs) {
            if (job.replyId != null) continue
            item(key = "image-job-${job.jobId ?: "request"}") { ImageJobCard(job, onCancel = { job.jobId?.let(onCancelImage) }) }
        }
        if (pending != null) {
            item(key = "pending") {
                PendingMessage(
                    pending,
                    sourcesOpen = pending.messageId in openSources,
                    onToggleSources = { toggleSources(pending.messageId) },
                    job = imageJobs.forReply(pending.messageId),
                    onCancelImage = onCancelImage,
                )
            }
        }
        items(count = messages.size, key = { index -> messages[messages.size - 1 - index].id }) { index ->
            val message = messages[messages.size - 1 - index]
            val isLast = index == 0
            when (message.role) {
                Role.USER -> UserMessage(message)
                Role.ASSISTANT -> if (message.image != null) {
                    ImageResult(message, onReport = if (message.image.file != null) ({ onReport(message.id) }) else null)
                } else AssistantMessage(
                    message,
                    // A reply that made (or was refused) a picture is answered: asking again would make another.
                    onRetry = if (isLast && canRetry && pending == null && message.notice != null && message.picture == null) onRetry else null,
                    onReport = { onReport(message.id) },
                    sourcesOpen = message.id in openSources,
                    onToggleSources = { toggleSources(message.id) },
                    job = imageJobs.forReply(message.id),
                    onCancelImage = onCancelImage,
                )
            }
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun UserMessage(message: ChatMessage) {
    val context = LocalContext.current
    val copyLabel = stringResource(R.string.copy_message)
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
        Surface(
            shape = RoundedCornerShape(topStart = 18.dp, topEnd = 18.dp, bottomStart = 18.dp, bottomEnd = 4.dp),
            color = MaterialTheme.colorScheme.secondaryContainer,
            modifier = Modifier
                .widthIn(max = 560.dp)
                .padding(start = 48.dp)
                .combinedClickable(
                    interactionSource = null,
                    indication = null,
                    onLongClickLabel = copyLabel,
                    // A message of photos alone has no words to copy.
                    onLongClick = { if (message.content.isNotBlank()) copyToClipboard(context, message.content) },
                    onClick = {},
                ),
        ) {
            Column(Modifier.padding(horizontal = 14.dp, vertical = 10.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                if (message.photos.isNotEmpty()) MessagePhotos(message.photos)
                if (message.content.isNotBlank()) {
                    Text(
                        message.content,
                        style = MaterialTheme.typography.bodyLarge,
                        color = MaterialTheme.colorScheme.onSecondaryContainer,
                    )
                }
            }
        }
    }
}

/**
 * A saved answer: the text (with `[n]` citations), its sources (folded unless [sourcesOpen]),
 * the service's lookup notes, any notice, the picture it made (its card while [job] runs, then the
 * picture with Save, Share and Report), then Copy and Report. Long-press anywhere also copies.
 */
@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun AssistantMessage(
    message: ChatMessage,
    onRetry: (() -> Unit)?,
    onReport: () -> Unit,
    sourcesOpen: Boolean,
    onToggleSources: () -> Unit,
    job: ImageJobUi?,
    onCancelImage: (String) -> Unit,
) {
    val context = LocalContext.current
    val copyLabel = stringResource(R.string.copy_message)
    Column(
        Modifier
            .fillMaxWidth()
            .combinedClickable(
                interactionSource = null,
                indication = null,
                onLongClickLabel = copyLabel,
                onLongClick = { copyToClipboard(context, message.content) },
                onClick = {},
            ),
        verticalArrangement = Arrangement.spacedBy(6.dp),
    ) {
        if (message.content.isNotBlank()) MarkdownText(message.content, sources = message.sources)
        if (message.sources.isNotEmpty()) SourceList(message.sources, sourcesOpen, onToggleSources)
        if (message.searchNotes.isNotEmpty()) SearchNotes(message.searchNotes)
        message.notice?.let { NoticeRow(it, onRetry) }
        // A finished picture carries its own Report next to Save and Share; the flag below would repeat it.
        val reportInPicture = message.picture?.file != null
        message.picture?.let { ReplyPictureView(it, job, onCancelImage, onReport = onReport.takeIf { reportInPicture }) }
        if (message.content.isNotBlank()) {
            AnswerActions(onCopy = { copyToClipboard(context, message.content) }, onReport = onReport.takeUnless { reportInPicture })
        }
    }
}

/**
 * The answer while it streams: lookup status until text arrives, then text, sources and notes,
 * and the card of the picture it is making ([job]) or why none was made.
 */
@Composable
private fun PendingMessage(
    pending: PendingReply,
    sourcesOpen: Boolean,
    onToggleSources: () -> Unit,
    job: ImageJobUi?,
    onCancelImage: (String) -> Unit,
) {
    Column(Modifier.fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
        if (pending.text.isEmpty() && pending.notice == null) {
            if (pending.locating) {
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
                    Text(
                        stringResource(R.string.locating),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            } else if (pending.lookup != null) {
                LookupStatusRow(pending.lookup)
            } else if (pending.picture == null) { // with a picture on its way its card says what is happening
                Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
                    Text(
                        stringResource(R.string.waiting_for_machine),
                        style = MaterialTheme.typography.bodyMedium,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        }
        if (pending.text.isNotEmpty()) MarkdownText(pending.text, sources = pending.sources)
        if (pending.sources.isNotEmpty()) SourceList(pending.sources, sourcesOpen, onToggleSources)
        if (pending.searchNotes.isNotEmpty()) SearchNotes(pending.searchNotes)
        pending.notice?.let { NoticeRow(it, onRetry = null) }
        pending.picture?.let { ReplyPictureView(it, job, onCancelImage, onReport = null) }
    }
}

/** A line from the service (not the model), in a quieter style, with an optional Retry. */
@Composable
internal fun NoticeRow(text: String, onRetry: (() -> Unit)?) {
    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.fillMaxWidth()) {
        Text(
            text,
            style = MaterialTheme.typography.bodyMedium,
            fontStyle = FontStyle.Italic,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(1f, fill = false),
        )
        if (onRetry != null) {
            Spacer(Modifier.size(8.dp))
            TextButton(onClick = onRetry) { Text(stringResource(R.string.retry)) }
        }
    }
}

/**
 * The message box, send/stop button and the standing privacy hint. Padded for the
 * navigation bar and the keyboard, whichever is taller, so it always sits just above both.
 */
@Composable
private fun InputBar(draft: String, photos: DraftPhotos, state: ChatUiState, actions: ChatActions) {
    Surface(color = MaterialTheme.colorScheme.surface) {
        Column(
            Modifier
                .fillMaxWidth()
                .windowInsetsPadding(
                    WindowInsets.navigationBars.union(WindowInsets.ime)
                        .only(WindowInsetsSides.Horizontal + WindowInsetsSides.Bottom)
                )
                .padding(start = 12.dp, end = 12.dp, top = 4.dp, bottom = 8.dp),
        ) {
            state.error?.let { error ->
                ErrorBanner(
                    error = error,
                    onRetry = if (error.retryable && !state.isStreaming) actions.onRetry else null,
                    onDismiss = actions.onDismissError,
                )
                Spacer(Modifier.size(8.dp))
            }
            // A message about where the person is waits here, with the text still in the box, until they answer.
            state.locationAsk?.let { ask ->
                LocationAskCard(ask, permitted = actions.locationPermitted, onAnswer = actions.onAnswerLocationAsk)
                Spacer(Modifier.size(8.dp))
            }
            var imageDialog by rememberSaveable { mutableStateOf(false) }
            if (imageDialog) {
                CreateImageDialog(
                    initialPrompt = draft,
                    status = state.status,
                    onCreate = { prompt, size ->
                        imageDialog = false
                        actions.onCreateImage(prompt, size)
                    },
                    onDismiss = { imageDialog = false },
                )
            }
            if (!photos.isEmpty) DraftPhotoStrip(photos, onRemove = actions.onRemovePhoto)
            Row(verticalAlignment = Alignment.Bottom) {
                // The "+": Photos and Camera when the server can look at photos, Create image when it can make
                // them; nothing at all on an older server.
                AttachButton(
                    menu = AttachMenu.of(state.status),
                    photoRoom = photos.room,
                    enabled = state.privacyAcknowledged,
                    createImageEnabled = state.imageJob == null,
                    onAddPhotos = actions.onAddPhotos,
                    onCreateImage = { imageDialog = true },
                )
                TextField(
                    value = draft,
                    onValueChange = actions.onDraftChange,
                    placeholder = {
                        Text(
                            if (photos.isEmpty) stringResource(R.string.input_placeholder)
                            else pluralStringResource(R.plurals.input_placeholder_photos, photos.count),
                        )
                    },
                    maxLines = 6,
                    shape = RoundedCornerShape(26.dp),
                    keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences),
                    colors = TextFieldDefaults.colors(
                        focusedContainerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
                        unfocusedContainerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
                        disabledContainerColor = MaterialTheme.colorScheme.surfaceContainerHigh,
                        focusedIndicatorColor = Color.Transparent,
                        unfocusedIndicatorColor = Color.Transparent,
                        disabledIndicatorColor = Color.Transparent,
                    ),
                    modifier = Modifier.weight(1f).heightIn(min = 52.dp),
                )
                Spacer(Modifier.size(8.dp))
                SendButton(draft, photos, state, actions)
            }
            Text(
                stringResource(R.string.input_hint),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 4.dp),
            )
        }
    }
}

/** Send; Stop while an answer streams; a countdown while the service asks us to wait. */
@Composable
private fun SendButton(draft: String, photos: DraftPhotos, state: ChatUiState, actions: ChatActions) {
    var now by remember { mutableLongStateOf(System.currentTimeMillis()) }
    LaunchedEffect(state.rateLimitedUntil) {
        while (System.currentTimeMillis() < state.rateLimitedUntil) {
            now = System.currentTimeMillis()
            delay(250)
        }
        now = System.currentTimeMillis()
    }
    val secondsLeft = ((state.rateLimitedUntil - now + 999) / 1000).toInt()
    val buttonModifier = Modifier.size(52.dp)

    when {
        state.isStreaming -> FilledTonalIconButton(onClick = actions.onStop, modifier = buttonModifier) {
            Icon(painterResource(R.drawable.ic_stop), contentDescription = stringResource(R.string.stop))
        }

        secondsLeft > 0 -> {
            val description = stringResource(R.string.send_available_in, secondsLeft)
            FilledIconButton(
                onClick = {},
                enabled = false,
                modifier = buttonModifier.semantics { contentDescription = description },
            ) {
                Text(stringResource(R.string.wait_seconds, secondsLeft), style = MaterialTheme.typography.labelLarge)
            }
        }

        else -> FilledIconButton(
            onClick = actions.onSend,
            // Photos alone can be sent; photos still being prepared are waited for.
            enabled = photos.canSend(draft.isNotBlank()) && state.privacyAcknowledged,
            modifier = buttonModifier,
        ) {
            Icon(painterResource(R.drawable.ic_send), contentDescription = stringResource(R.string.send))
        }
    }
}

@Composable
private fun ErrorBanner(error: ChatError, onRetry: (() -> Unit)?, onDismiss: () -> Unit) {
    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.errorContainer,
        contentColor = MaterialTheme.colorScheme.onErrorContainer,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(start = 14.dp, end = 4.dp)) {
            Text(
                error.message,
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.weight(1f).padding(vertical = 10.dp),
            )
            if (onRetry != null) {
                TextButton(onClick = onRetry) { Text(stringResource(R.string.retry)) }
            }
            if (error.kind != ErrorKind.RATE_LIMITED) {
                TextButton(onClick = onDismiss) { Text(stringResource(R.string.dismiss)) }
            }
        }
    }
}

/**
 * The status line's text: the model name and the machine count, "No machines online right now"
 * when none is ready, nothing without a status. The server counts the engine's own phone lanes
 * as a serving machine, so `members_ready >= 1` reads as "1 machine serving".
 */
internal fun statusLineOf(status: ClusterStatus?, noMachines: String, machines: String): String? {
    val pool = status?.appPool ?: return null
    if ((pool.membersReady ?: 0) <= 0) return noMachines
    return listOfNotNull(pool.activeModel?.takeIf { it.isNotBlank() }, machines).joinToString(" · ")
}
