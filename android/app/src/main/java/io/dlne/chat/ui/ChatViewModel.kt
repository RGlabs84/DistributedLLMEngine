package io.dlne.chat.ui

import android.app.Application
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.viewModelScope
import io.dlne.chat.AppGraph
import io.dlne.chat.R
import io.dlne.chat.data.ChatHistory
import io.dlne.chat.data.ChatMessage
import io.dlne.chat.data.Conversation
import io.dlne.chat.data.ReportReason
import io.dlne.chat.data.Reports
import io.dlne.chat.data.Role
import io.dlne.chat.data.Source
import io.dlne.chat.data.newId
import io.dlne.chat.data.titleFrom
import io.dlne.chat.net.ApiException
import io.dlne.chat.net.ClusterStatus
import io.dlne.chat.net.StreamEvent
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/** Which full screen is showing. The conversation list is a drawer over [Chat]. */
enum class Screen { Chat, Settings }

/** A lookup the server is running for the model (`tool` as sent: weather, wikipedia, ...). */
data class Lookup(val tool: String, val query: String)

/**
 * The answer currently streaming in, before it is saved to its conversation. [lookup] is
 * the latest lookup announced; it is shown until the answer text starts.
 */
data class PendingReply(
    val conversationId: String,
    val text: String = "",
    val notice: String? = null,
    val lookup: Lookup? = null,
    val sources: List<Source> = emptyList(),
    val searchNotes: List<String> = emptyList(),
)

/** The open "Report answer" sheet: which answer, and how sending is going. */
data class ReportUiState(val messageId: String, val sending: Boolean = false, val error: String? = null)

enum class ErrorKind {
    /** 429: the network already has an answer running or asked too often. Send is paused. */
    RATE_LIMITED,

    /** No connection, DNS, TLS, timeout. Retrying makes sense. */
    NETWORK,

    /** The service failed (5xx or an in-band error). Retrying makes sense. */
    SERVER,

    /** The request itself was refused (400, 404). Retrying the same thing will not help. */
    REJECTED,
}

data class ChatError(val message: String, val kind: ErrorKind) {
    val retryable: Boolean get() = kind == ErrorKind.NETWORK || kind == ErrorKind.SERVER
}

/** Everything the screens draw, in one immutable snapshot. */
data class ChatUiState(
    val conversations: List<Conversation> = emptyList(),
    /** The open conversation, or null for a new chat that has no messages yet. */
    val current: Conversation? = null,
    val pending: PendingReply? = null,
    val error: ChatError? = null,
    /** Wall-clock millis until which sending is paused after a 429; 0 when not paused. */
    val rateLimitedUntil: Long = 0L,
    val status: ClusterStatus? = null,
    val serverUrl: String = "",
    val privacyAcknowledged: Boolean = true,
) {
    val isStreaming: Boolean get() = pending != null

    /** The streaming answer, if it belongs to the conversation on screen. */
    val pendingForCurrent: PendingReply? get() = pending?.takeIf { it.conversationId == current?.id }
}

/**
 * Holds the chat state and talks to the service. Conversations live in the process-wide
 * [io.dlne.chat.data.ConversationStore]; this class adds what is specific to one window:
 * the open conversation, the draft, the answer in flight, errors and the status line.
 */
class ChatViewModel(application: Application, private val savedState: SavedStateHandle) :
    AndroidViewModel(application) {

    private val graph = AppGraph.get(application)
    private val api = graph.api
    private val store = graph.conversations
    private val settings = graph.settings

    /** The message being typed. Compose state (not a Flow) so typing never lags. */
    var draft by mutableStateOf("")
        private set

    var screen by mutableStateOf(Screen.Chat)
        private set

    private data class Transient(
        val pending: PendingReply? = null,
        val error: ChatError? = null,
        val rateLimitedUntil: Long = 0L,
        val status: ClusterStatus? = null,
    )

    private val transient = MutableStateFlow(Transient())
    // The open conversation survives rotation and the process being reclaimed in the background.
    private val currentId: StateFlow<String?> = savedState.getStateFlow<String?>(KEY_CURRENT, null)

    private var answerJob: Job? = null
    private var stopRequested = false
    private var rateLimitJob: Job? = null
    private var statusJob: Job? = null

    val uiState: StateFlow<ChatUiState> = combine(
        store.conversations,
        currentId,
        transient,
        settings.serverUrl,
        settings.privacyAcknowledged,
    ) { conversations, id, t, url, acknowledged ->
        ChatUiState(
            conversations = conversations,
            current = id?.let { wanted -> conversations.firstOrNull { it.id == wanted } },
            pending = t.pending,
            error = t.error,
            rateLimitedUntil = t.rateLimitedUntil,
            status = t.status,
            serverUrl = url,
            privacyAcknowledged = acknowledged,
        )
    }.stateIn(
        viewModelScope,
        SharingStarted.Eagerly,
        ChatUiState(
            conversations = store.conversations.value,
            serverUrl = settings.serverUrl.value,
            privacyAcknowledged = settings.privacyAcknowledged.value,
        ),
    )

    // ------------------------------------------------------------------ typing and sending

    fun updateDraft(text: String) {
        draft = text
    }

    fun send() {
        val text = draft.trim()
        if (text.isEmpty() || transient.value.pending != null || isRateLimited()) return
        if (!settings.privacyAcknowledged.value) return

        val now = System.currentTimeMillis()
        val base = currentId.value?.let(store::get)
            ?: Conversation(newId(), titleFrom(text), now, now, emptyList())
        val conversation = base.copy(
            updatedAt = now,
            messages = base.messages + ChatMessage(newId(), Role.USER, text, createdAt = now),
        )
        store.upsert(conversation)
        savedState[KEY_CURRENT] = conversation.id
        draft = ""
        ask(conversation)
    }

    /** Stops the answer in progress; what has arrived so far is kept. */
    fun stop() {
        stopRequested = true
        answerJob?.cancel()
    }

    /**
     * Asks again for the last unanswered message: after an error, after a service notice
     * ("no machine is free") or after Stop. An incomplete answer is replaced.
     */
    fun retry() {
        if (transient.value.pending != null || isRateLimited()) return
        val conversation = currentId.value?.let(store::get) ?: return
        var messages = conversation.messages
        val last = messages.lastOrNull() ?: return
        if (last.role == Role.ASSISTANT) {
            if (last.notice == null && last.content.isNotBlank()) return // already answered
            messages = messages.dropLast(1)
        }
        if (messages.lastOrNull()?.role != Role.USER) return
        val updated = conversation.copy(messages = messages, updatedAt = System.currentTimeMillis())
        store.upsert(updated)
        ask(updated)
    }

    fun dismissError() {
        transient.update { it.copy(error = null) }
    }

    private fun ask(conversation: Conversation) {
        val history = ChatHistory.toWire(conversation.messages)
        if (history.isEmpty()) return
        val serverUrl = settings.serverUrl.value
        stopRequested = false
        transient.update { it.copy(pending = PendingReply(conversation.id), error = null) }

        answerJob = viewModelScope.launch {
            try {
                api.streamChat(serverUrl, history).collect { event ->
                    when (event) {
                        is StreamEvent.Content -> updatePending { it.copy(text = it.text + event.text) }
                        is StreamEvent.Notice -> updatePending { it.copy(notice = event.text) }
                        is StreamEvent.Failure ->
                            transient.update { it.copy(error = ChatError(event.message, ErrorKind.SERVER)) }
                        is StreamEvent.Searching -> updatePending { it.copy(lookup = Lookup(event.tool, event.query)) }
                        // Each "sources" event carries the whole list so far: replace, don't append.
                        is StreamEvent.Sources -> updatePending { it.copy(sources = event.sources) }
                        is StreamEvent.SearchNote -> updatePending { it.withSearchNote(event.note) }
                        is StreamEvent.SearchUnavailable ->
                            updatePending { it.withSearchNote(event.reason.ifBlank { text(R.string.search_unavailable_default) }) }
                        StreamEvent.Done -> Unit
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiException.Http) {
                onHttpError(e)
            } catch (e: ApiException.Network) {
                showError(ChatError(text(R.string.error_network), ErrorKind.NETWORK))
            } catch (e: Exception) {
                showError(ChatError(text(R.string.error_unexpected), ErrorKind.SERVER))
            } finally {
                finishReply(conversation.id)
            }
        }
    }

    private inline fun updatePending(change: (PendingReply) -> PendingReply) {
        transient.update { t -> t.copy(pending = t.pending?.let(change)) }
    }

    /** Adds a lookup remark once (a repeated remark is not shown twice), at most a handful. */
    private fun PendingReply.withSearchNote(note: String): PendingReply =
        if (note in searchNotes || searchNotes.size >= MAX_SEARCH_NOTES) this else copy(searchNotes = searchNotes + note)

    /** Saves whatever arrived (text, notice, sources, lookup notes) as the assistant's message. */
    private fun finishReply(conversationId: String) {
        val reply = transient.value.pending ?: return
        transient.update { it.copy(pending = null) }
        val notice = reply.notice ?: if (stopRequested) text(R.string.notice_stopped) else null
        stopRequested = false
        if (reply.text.isBlank() && notice == null) return

        val conversation = store.get(conversationId) ?: return
        val now = System.currentTimeMillis()
        val answer = ChatMessage(
            id = newId(),
            role = Role.ASSISTANT,
            content = reply.text,
            notice = notice,
            createdAt = now,
            sources = reply.sources,
            searchNotes = reply.searchNotes,
        )
        store.upsert(conversation.copy(updatedAt = now, messages = conversation.messages + answer))
        refreshStatus()
    }

    private fun onHttpError(e: ApiException.Http) {
        when {
            e.code == 429 -> {
                val until = System.currentTimeMillis() + (e.retryAfterSeconds ?: DEFAULT_RETRY_SECONDS) * 1000
                transient.update {
                    it.copy(error = ChatError(e.detail, ErrorKind.RATE_LIMITED), rateLimitedUntil = until)
                }
                rateLimitJob?.cancel()
                rateLimitJob = viewModelScope.launch {
                    delay(until - System.currentTimeMillis())
                    // The wait is over: sending works again and the stale detail goes away.
                    transient.update { t ->
                        t.copy(
                            rateLimitedUntil = 0L,
                            error = t.error?.takeUnless { it.kind == ErrorKind.RATE_LIMITED },
                        )
                    }
                }
            }
            e.code == 404 -> showError(ChatError(text(R.string.error_not_found), ErrorKind.REJECTED))
            e.code in 400..499 -> showError(ChatError(e.detail, ErrorKind.REJECTED))
            else -> showError(ChatError(text(R.string.error_server, e.code), ErrorKind.SERVER))
        }
    }

    private fun showError(error: ChatError) {
        transient.update { it.copy(error = error) }
    }

    private fun isRateLimited(): Boolean = transient.value.rateLimitedUntil > System.currentTimeMillis()

    // ------------------------------------------------------------------ reporting an answer

    /** The open report sheet, or null. Compose state, read directly by the screen. */
    var report by mutableStateOf<ReportUiState?>(null)
        private set

    /** True once a report went through, until the screen has shown its "Thanks" message. */
    var reportThanksPending by mutableStateOf(false)
        private set

    private var reportJob: Job? = null

    fun openReport(messageId: String) {
        reportJob?.cancel()
        report = ReportUiState(messageId)
    }

    fun closeReport() {
        reportJob?.cancel()
        report = null
    }

    fun reportThanksShown() {
        reportThanksPending = false
    }

    /**
     * Sends the open report: the answer and the question it answered (see [Reports.build]),
     * to the server the chat uses. On success the sheet closes and "Thanks" is shown; on
     * failure the sheet stays open with the reason.
     */
    fun sendReport(reason: ReportReason, note: String) {
        val target = report ?: return
        if (target.sending) return
        val request = currentId.value?.let(store::get)?.let { Reports.build(it.messages, target.messageId, reason, note) }
        if (request == null) {
            report = null // the answer no longer exists (e.g. the chat was deleted meanwhile)
            return
        }
        val serverUrl = settings.serverUrl.value
        report = target.copy(sending = true, error = null)
        reportJob = viewModelScope.launch {
            val error: String? = try {
                api.sendReport(serverUrl, request)
                null
            } catch (e: ApiException.Http) {
                when (e.code) {
                    404 -> text(R.string.report_unavailable)
                    429 -> (e.retryAfterSeconds ?: DEFAULT_RETRY_SECONDS).toInt().let { seconds ->
                        getApplication<Application>().resources.getQuantityString(R.plurals.report_too_many, seconds, seconds)
                    }
                    else -> text(R.string.report_failed, e.code)
                }
            } catch (e: ApiException.Network) {
                text(R.string.report_network)
            }
            if (report?.messageId != target.messageId) return@launch // closed or replaced meanwhile
            if (error == null) {
                report = null
                reportThanksPending = true
            } else {
                report = target.copy(sending = false, error = error)
            }
        }
    }

    // ------------------------------------------------------------------ conversations

    fun newChat() {
        savedState.set<String?>(KEY_CURRENT, null)
        draft = ""
        screen = Screen.Chat
        closeReport()
        dismissError()
    }

    /** A share from another app: open a fresh chat with [text] ready to send. */
    fun startChatWithSharedText(text: String) {
        newChat()
        draft = text.take(MAX_SHARED_CHARS)
    }

    fun openConversation(id: String) {
        if (currentId.value != id) draft = ""
        savedState[KEY_CURRENT] = id
        screen = Screen.Chat
        closeReport()
        dismissError()
    }

    fun deleteConversation(id: String) {
        if (transient.value.pending?.conversationId == id) stop()
        store.delete(id)
        if (currentId.value == id) newChat()
    }

    fun deleteAllConversations() {
        if (transient.value.pending != null) stop()
        store.deleteAll()
        newChat()
    }

    // ------------------------------------------------------------------ settings and status

    fun showSettings() {
        screen = Screen.Settings
    }

    fun closeSettings() {
        screen = Screen.Chat
    }

    fun acknowledgePrivacy() = settings.acknowledgePrivacy()

    /** Returns false (nothing saved) if [input] is not a usable https address. */
    fun setServerUrl(input: String): Boolean {
        val saved = settings.setServerUrl(input)
        if (saved) refreshStatus()
        return saved
    }

    fun resetServerUrl() {
        settings.resetServerUrl()
        refreshStatus()
    }

    /** Refreshes the header's model and machine count every minute while the app is visible. */
    fun startStatusUpdates() {
        if (statusJob?.isActive == true) return
        statusJob = viewModelScope.launch {
            while (isActive) {
                loadStatus()
                delay(STATUS_INTERVAL_MS)
            }
        }
    }

    fun stopStatusUpdates() {
        statusJob?.cancel()
        statusJob = null
    }

    private fun refreshStatus() {
        viewModelScope.launch { loadStatus() }
    }

    private suspend fun loadStatus() {
        val status = try {
            api.fetchStatus(settings.serverUrl.value)
        } catch (e: ApiException) {
            null // The header simply shows no status; chat still works or reports its own error.
        }
        transient.update { it.copy(status = status) }
    }

    private fun text(id: Int, vararg args: Any): String = getApplication<Application>().getString(id, *args)

    private companion object {
        const val KEY_CURRENT = "current_conversation"
        const val DEFAULT_RETRY_SECONDS = 5L
        const val STATUS_INTERVAL_MS = 60_000L
        const val MAX_SHARED_CHARS = 20_000
        const val MAX_SEARCH_NOTES = 6
    }
}
