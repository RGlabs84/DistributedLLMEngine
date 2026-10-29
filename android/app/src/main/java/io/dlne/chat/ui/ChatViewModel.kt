package io.dlne.chat.ui

import android.app.Application
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.viewModelScope
import io.dlne.chat.AppGraph
import io.dlne.chat.R
import io.dlne.chat.data.ChatHistory
import io.dlne.chat.data.PhoneLocation
import io.dlne.chat.data.ChatMessage
import io.dlne.chat.data.ImageAttachment
import io.dlne.chat.data.LocalQuestion
import io.dlne.chat.data.imageFiles
import io.dlne.chat.data.imagePlaceholder
import io.dlne.chat.data.Conversation
import io.dlne.chat.data.Photo
import io.dlne.chat.data.PhotoEncoding
import io.dlne.chat.data.PhotoReader
import io.dlne.chat.data.Prepared
import io.dlne.chat.data.ReplyPicture
import io.dlne.chat.data.ReportReason
import io.dlne.chat.data.Reports
import io.dlne.chat.data.Role
import io.dlne.chat.data.Source
import io.dlne.chat.data.newId
import io.dlne.chat.data.prepare
import io.dlne.chat.data.takeChars
import io.dlne.chat.data.titleFrom
import io.dlne.chat.data.withPhotosRefused
import io.dlne.chat.data.withReplyPicture
import io.dlne.chat.net.ApiException
import io.dlne.chat.net.ClusterStatus
import io.dlne.chat.net.DEFAULT_IMAGE_SIZE
import io.dlne.chat.net.GeneratedImage
import io.dlne.chat.net.MAX_IMAGE_PROMPT_CHARS
import io.dlne.chat.net.ImageProgress
import io.dlne.chat.net.ImageStatus
import io.dlne.chat.net.WireMessage
import io.dlne.chat.net.defaultImageSize
import io.dlne.chat.net.imageEtaSeconds
import io.dlne.chat.net.StreamEvent
import java.io.File
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
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
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeoutOrNull

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
    /** True while the phone is finding its position for a local question (a few seconds at most). */
    val locating: Boolean = false,
    /** The picture this reply is making (`image_job`), or why the server made none (`image_refused`). */
    val picture: ReplyPicture? = null,
    /** The id the answer is saved under, so what the reader unfolded stays unfolded once saved. */
    val messageId: String = newId(),
) {
    /**
     * The server took back what it had streamed (`retract`): the text is emptied and the
     * sources held for it are dropped, while the bubble itself (this object) stays and the
     * lookup, notes, notice and picture are left as they were (a picture job made before the
     * retract is still running). The lookup events and a fresh answer that follow fill it in again.
     */
    fun retracted(): PendingReply = copy(text = "", sources = emptyList())

    /**
     * The server started a picture job for this reply. A reply makes one picture: a second job is
     * ignored, while a refusal that came first gives way to the job.
     */
    fun withPictureJob(prompt: String): PendingReply =
        if (picture?.created == true) this else copy(picture = ReplyPicture(prompt))

    /** The server did not start a picture: [detail] is shown under the reply. Never replaces a picture being made. */
    fun withPictureRefused(detail: String): PendingReply =
        if (picture?.created == true) this else copy(picture = ReplyPicture.refusal(detail))

    /** The picture ended (made or failed) while the reply was still streaming: the saved reply keeps it. */
    fun withPicture(ended: ReplyPicture): PendingReply = copy(picture = ended)
}

/**
 * An image being made: the request is sent ([jobId] null until the server answers), then it waits,
 * is checked, drawn and checked again. Shown as a card in the conversation it belongs to.
 * [replyId] is the reply whose picture it is when the chat made it (the card sits in that reply and the
 * picture lands there); null for a Create image dialog job, whose picture is a message of its own.
 */
data class ImageJobUi(
    val conversationId: String,
    val prompt: String,
    val size: String,
    val jobId: String? = null,
    val replyId: String? = null,
    val status: ImageStatus = ImageStatus.QUEUED,
    val position: Int? = null,
    val elapsedSeconds: Int = 0,
    /** About how long the whole job takes, from the server; null when it did not say. */
    val etaSeconds: Int? = null,
    val cancelling: Boolean = false,
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
    val shareLocation: Boolean = false,
    /** Every picture being made: a Create image request still on its way first, then the running jobs. */
    val imageJobs: List<ImageJobUi> = emptyList(),
    /** Set while a send waits for the person's answer to "Use your location for this?". */
    val locationAsk: LocationAsk? = null,
) {
    /** "Create image" is offered only when the server says it can. */
    val imageAvailable: Boolean get() = status?.imageGeneration == true

    /** The Create image dialog's picture in progress, if any: that dialog makes one at a time. */
    val imageJob: ImageJobUi? get() = imageJobs.firstOrNull { it.replyId == null }

    /** The pictures being made in the conversation on screen (those of its replies, and a dialog one). */
    val imageJobsForCurrent: List<ImageJobUi> get() = imageJobs.filter { it.conversationId == current?.id }

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
    private val deviceLocation = graph.location
    private val images = graph.images

    /** The message being typed. Compose state (not a Flow) so typing never lags. */
    var draft by mutableStateOf("")
        private set

    /** The photos chosen for the next message. Compose state like [draft], and emptied with it. */
    var photoDraft by mutableStateOf(DraftPhotos())
        private set

    var screen by mutableStateOf(Screen.Chat)
        private set

    private data class Transient(
        val pending: PendingReply? = null,
        val error: ChatError? = null,
        val rateLimitedUntil: Long = 0L,
        val status: ClusterStatus? = null,
        /** The server address [status] came from, so a status is never kept across a change of server. */
        val statusServer: String? = null,
        /** The pictures being made, by job id (several can run at once). */
        val images: ImageJobs = ImageJobs(),
        /** A Create image request on its way: it has no job id until the server answers. */
        val request: ImageJobUi? = null,
        val locationAsk: LocationAsk? = null,
    )

    private val transient = MutableStateFlow(Transient())
    // The open conversation survives rotation and the process being reclaimed in the background.
    private val currentId: StateFlow<String?> = savedState.getStateFlow<String?>(KEY_CURRENT, null)

    private var answerJob: Job? = null
    private var stopRequested = false
    private var rateLimitJob: Job? = null

    private data class Prefs(val serverUrl: String, val privacyAcknowledged: Boolean, val shareLocation: Boolean)

    private val prefs = combine(settings.serverUrl, settings.privacyAcknowledged, settings.shareLocation, ::Prefs)

    val uiState: StateFlow<ChatUiState> = combine(
        store.conversations,
        currentId,
        transient,
        prefs,
    ) { conversations, id, t, p ->
        ChatUiState(
            conversations = conversations,
            current = id?.let { wanted -> conversations.firstOrNull { it.id == wanted } },
            pending = t.pending,
            error = t.error,
            rateLimitedUntil = t.rateLimitedUntil,
            status = t.status,
            imageJobs = listOfNotNull(t.request) + t.images.all,
            locationAsk = t.locationAsk,
            serverUrl = p.serverUrl,
            privacyAcknowledged = p.privacyAcknowledged,
            shareLocation = p.shareLocation,
        )
    }.stateIn(
        viewModelScope,
        SharingStarted.Eagerly,
        ChatUiState(
            conversations = store.conversations.value,
            serverUrl = settings.serverUrl.value,
            privacyAcknowledged = settings.privacyAcknowledged.value,
            shareLocation = settings.shareLocation.value,
        ),
    )

    // ------------------------------------------------------------------ typing and sending

    fun updateDraft(text: String) {
        draft = text
        // The card was shown for the text that is held. Once that no longer needs asking (the box was emptied,
        // or the question is not about where the person is any more) it goes away instead of waiting for nothing.
        if (transient.value.locationAsk != null && !mustAskLocation(text.trim())) dismissLocationAsk()
    }

    fun send() = sendDraft(askLocation = true)

    /**
     * Sends what is typed. A question about where the person is, with location sharing off, is not
     * sent yet: a card above the message box asks first (see [answerLocationAsk]) and the text and the
     * chosen photos stay in the box. [askLocation] is false once that has been answered; [declineLocation]
     * remembers in this chat that the person did not share, so it is not asked again here; [note] is shown
     * under the answer. [waited] is true once photos still being prepared have had their moment.
     */
    private fun sendDraft(askLocation: Boolean, declineLocation: Boolean = false, note: String? = null, waited: Boolean = false) {
        val typed = draft.trim()
        val photos = photoDraft
        // Photos alone are a message too. A photo still being prepared does not stop the words going (see below).
        if (!photos.canSend(typed.isNotEmpty()) || savingPhotos) return
        if (transient.value.pending != null || isRateLimited()) return
        if (!settings.privacyAcknowledged.value) return
        if (askLocation && holdForLocation(typed)) return // nothing is stored or sent until the card is answered
        transient.update { it.copy(locationAsk = null) }

        // The person usually taps Send right after choosing photos, and the message should carry them: they get
        // a moment to be ready. One that is stuck does not hold the message up beyond that moment, and stays in
        // the box (with its x) instead of being lost.
        if (!waited && photos.preparing.isNotEmpty()) {
            savingPhotos = true
            viewModelScope.launch {
                withTimeoutOrNull(PREPARING_GRACE_MS) { while (photoDraft.preparing.isNotEmpty()) delay(PREPARING_POLL_MS) }
                savingPhotos = false
                sendDraft(askLocation = false, declineLocation, note, waited = true)
            }
            return
        }

        if (photos.ready.isEmpty()) {
            post(typed, emptyList(), declineLocation, note)
            return
        }
        // The photos go to private storage first, off the main thread. The box empties at once, so a
        // second tap cannot send them twice, and fills again if the phone cannot store them. Photos still
        // being prepared stay in it for the next message.
        savingPhotos = true
        draft = ""
        photoDraft = DraftPhotos(preparing = photos.preparing)
        viewModelScope.launch {
            val names = withContext(Dispatchers.IO) { photos.ready.map { images.savePhoto(it.jpeg) } }
            savingPhotos = false
            if (names.any { it == null }) {
                images.delete(names)
                if (draft.isBlank()) draft = typed
                val now = photoDraft
                if (now.ready.isEmpty() && now.count + photos.ready.size <= PhotoEncoding.MAX_PHOTOS) {
                    photoDraft = now.copy(ready = photos.ready)
                }
                showError(ChatError(text(R.string.photo_save_failed), ErrorKind.REJECTED))
                return@launch
            }
            post(typed, photos.ready.zip(names) { photo, name -> Photo(name!!, photo.width, photo.height) }, declineLocation, note)
        }
    }

    /**
     * Adds the person's message, with its stored [photos], to its chat and asks for the answer.
     * [declineLocation] and [note] are the card's outcome (see [sendDraft]).
     */
    private fun post(typed: String, photos: List<Photo>, declineLocation: Boolean, note: String?) {
        val now = System.currentTimeMillis()
        val base = currentId.value?.let(store::get)
            ?: Conversation(newId(), chatTitle(typed, photos.size), now, now, emptyList())
        val conversation = base.copy(
            updatedAt = now,
            messages = base.messages + ChatMessage(newId(), Role.USER, typed, createdAt = now, photos = photos),
            locationDeclined = base.locationDeclined || declineLocation,
        )
        store.upsert(conversation)
        savedState[KEY_CURRENT] = conversation.id
        draft = ""
        ask(conversation, listOfNotNull(note))
    }

    /**
     * Whether sending [text] has to wait for the person's answer to "Use your location for this?": the
     * question is about where they are, sharing is off and this chat was not answered "Not now" (see
     * [LocalQuestion.shouldAsk]). Sharing that is on but whose permission Android has taken back (an "only
     * this time" answer ends with the session) is as good as off: the card asks again instead of the answer
     * going without. Nothing is changed by asking.
     */
    private fun mustAskLocation(text: String): Boolean {
        val chat = currentId.value?.let(store::get)
        val asked = chat?.messages.orEmpty() + ChatMessage(newId(), Role.USER, text, createdAt = System.currentTimeMillis())
        val sharing = settings.shareLocation.value && deviceLocation.permitted()
        return LocalQuestion.shouldAsk(asked, sharing, chat?.locationDeclined == true)
    }

    /** Holds the send when [mustAskLocation]: the card is shown and the text stays in the box. */
    private fun holdForLocation(text: String): Boolean {
        if (!mustAskLocation(text)) return false
        // Android may have been told "don't ask again" earlier; unless the permission has been given since, say so on the card.
        val blocked = locationBlocked && !deviceLocation.permitted()
        transient.update { it.copy(locationAsk = it.locationAsk ?: LocationAsk(blocked)) }
        return true
    }

    /** True once Android has refused the location permission for good (this session): the card then says "Open settings". */
    private var locationBlocked = false

    /**
     * The person answered the card. Granted: sharing turns on and the message goes out with a fresh
     * fix. Not now, or Android refused: the message goes without, this chat is not asked again, and a
     * refusal is noted under the answer. [LocationAnswer.BLOCKED] also remembers that Android will not
     * ask again, so the next card offers the app's settings page instead.
     */
    fun answerLocationAsk(answer: LocationAnswer) {
        if (transient.value.locationAsk == null) return
        transient.update { it.copy(locationAsk = null) }
        when (answer) {
            LocationAnswer.GRANTED -> {
                locationBlocked = false
                settings.setShareLocation(true)
                sendDraft(askLocation = false)
            }
            LocationAnswer.NOT_NOW -> sendDraft(askLocation = false, declineLocation = true)
            LocationAnswer.REFUSED, LocationAnswer.BLOCKED -> {
                locationBlocked = answer == LocationAnswer.BLOCKED
                sendDraft(askLocation = false, declineLocation = true, note = text(R.string.location_note_refused))
            }
        }
    }

    /** A chat that starts with photos alone has no words to name it by. */
    private fun chatTitle(typed: String, photos: Int): String =
        if (typed.isEmpty() && photos > 0) {
            getApplication<Application>().resources.getQuantityString(R.plurals.photo_chat_title, photos)
        } else {
            titleFrom(typed)
        }

    // ------------------------------------------------------------------ photos in the message box

    /** Bumped whenever the box is emptied, so a photo still being prepared for the old box is dropped. */
    private var draftEpoch = 0

    /** True while chosen photos are being written to storage for a send (a moment): nothing else is sent meanwhile. */
    private var savingPhotos = false

    /** The coroutine preparing each photo that is not ready yet, by its id in the box, so its x can stop it. */
    private val preparingJobs = HashMap<String, Job>()

    /** One photo is decoded at a time: each holds a whole bitmap, and four at once could run a small phone out of memory. */
    private val prepareLock = Mutex()

    /**
     * Prepares the photos at [uris] (from the photo picker, or the camera's file) for the next message:
     * each is turned upright, scaled and re-encoded off the main thread (see [PhotoReader]) and then
     * shows as a thumbnail. Only as many as still fit are taken. A photo that is not ready in
     * [PHOTO_PREPARE_MS] (a cloud photo with no connection) is given up on, and its x takes it out at any
     * time. [cameraFile], the camera's temporary file, is deleted once it has been read, or given up on.
     */
    fun addPhotos(uris: List<Uri>, cameraFile: File? = null) {
        val chosen = uris.take(photoDraft.room)
        if (chosen.isEmpty()) {
            cameraFile?.delete()
            return
        }
        val slots = chosen.map { newId() }
        photoDraft = photoDraft.copy(preparing = photoDraft.preparing + slots)
        val resolver = getApplication<Application>().contentResolver
        val epoch = draftEpoch
        for ((uri, slot) in chosen.zip(slots)) {
            preparingJobs[slot] = viewModelScope.launch {
                try {
                    val outcome = prepareLock.withLock {
                        prepare(viewModelScope, Dispatchers.IO, PHOTO_PREPARE_MS) { PhotoReader.read(resolver, uri) }
                    }
                    val now = photoDraft
                    // The box was emptied (another chat) or the photo taken out while it was being read: drop what came.
                    if (epoch != draftEpoch || slot !in now.preparing) return@launch
                    photoDraft = now.copy(
                        ready = if (outcome is Prepared.Ready) {
                            now.ready + DraftPhoto(slot, outcome.value.jpeg, outcome.value.width, outcome.value.height)
                        } else {
                            now.ready
                        },
                        preparing = now.preparing - slot,
                    )
                    when (outcome) {
                        is Prepared.Ready -> Unit
                        Prepared.Unreadable -> showError(ChatError(text(R.string.photo_unreadable), ErrorKind.REJECTED))
                        Prepared.TooSlow -> showError(ChatError(text(R.string.photo_too_slow), ErrorKind.REJECTED))
                    }
                } finally {
                    preparingJobs.remove(slot)
                    cameraFile?.delete()
                }
            }
        }
    }

    /**
     * Takes a chosen photo back out of the box (the x on its thumbnail). One that is still being prepared
     * goes too, and nobody waits for it any more.
     */
    fun removePhoto(id: String) {
        photoDraft = photoDraft.copy(ready = photoDraft.ready.filterNot { it.id == id }, preparing = photoDraft.preparing - id)
        preparingJobs.remove(id)?.cancel()
    }

    private fun clearPhotoDraft() {
        draftEpoch++
        photoDraft = DraftPhotos()
        // A copy: a cancelled photo takes itself out of the map as it ends.
        preparingJobs.values.toList().forEach { it.cancel() }
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
        if (last.image != null) return // an image request is not a question for the chat model
        if (last.role == Role.ASSISTANT) {
            if (last.picture != null) { // a picture is its answer: asking again would make another
                dismissError()
                return
            }
            if (last.notice == null && last.content.isNotBlank()) return // already answered
            messages = messages.dropLast(1)
        }
        val newest = messages.lastOrNull() ?: return
        if (newest.role != Role.USER) return
        // Trying again is the person's own choice to send the photos again: a refusal no longer holds them back.
        val updated = conversation.copy(
            messages = messages.withPhotosRefused(newest.id, false),
            updatedAt = System.currentTimeMillis(),
        )
        store.upsert(updated)
        ask(updated)
    }

    fun dismissError() {
        transient.update { it.copy(error = null) }
    }

    private fun ask(conversation: Conversation, notes: List<String> = emptyList()) {
        val serverUrl = settings.serverUrl.value
        stopRequested = false
        transient.update { it.copy(pending = PendingReply(conversation.id, searchNotes = notes), error = null) }

        answerJob = viewModelScope.launch {
            var sent: List<WireMessage> = emptyList() // what the request carries: tells whether a refusal was about its photos
            try {
                // The newest photos are read back from their files, which is disk work: off the main thread.
                val history = withContext(Dispatchers.IO) {
                    ChatHistory.toWire(conversation.messages, photoUrl = images::photoDataUrl)
                }
                if (history.isEmpty()) return@launch // nothing to send; the finally block takes the waiting state away
                sent = history
                // The conversation's id is also its chat_id: a random UUID made when the chat started.
                val place = placeFor(conversation)
                api.streamChat(serverUrl, conversation.id, history, place).collect { event ->
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
                        // The model answered from memory while unsure; the server takes that text
                        // back and looks the question up. Start the reply over, keeping the bubble.
                        StreamEvent.Retract -> updatePending { it.retracted() }
                        // The model asked for a picture: the card shows in this reply and the picture lands in it.
                        is StreamEvent.ImageJob -> startReplyPicture(conversation.id, event)
                        is StreamEvent.ImageRefused ->
                            updatePending { it.withPictureRefused(event.detail.ifBlank { text(R.string.image_refused) }) }
                        StreamEvent.Done -> Unit
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiException.Http) {
                // Photos the server turned down go as marks in every later request, not as pictures again.
                refusedPhotoTurn(conversation.messages, sent, e)?.let { markPhotosRefused(conversation.id, it) }
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

    /**
     * The server will not take the photos of [messageId]: later requests send them as `[photo]` marks, so
     * the refusal does not come back with every message after it and text still goes through. Retry on that
     * message clears it (see [retry]).
     */
    private fun markPhotosRefused(conversationId: String, messageId: String) {
        val chat = store.get(conversationId) ?: return
        store.upsert(chat.copy(messages = chat.messages.withPhotosRefused(messageId, true)))
    }

    /**
     * The place to send with this request: null unless sharing is on, the question is about where
     * the person is, and the phone can say. Other questions never wake the GPS. When a local
     * question goes without a place the answer says so (a note under it), so nobody takes a
     * generic result for a local one.
     */
    private suspend fun placeFor(conversation: Conversation): PhoneLocation? {
        if (!settings.shareLocation.value || !LocalQuestion.asks(conversation.messages)) return null
        if (!deviceLocation.permitted()) {
            settings.setShareLocation(false) // the permission was withdrawn in Android's settings
            updatePending { it.withSearchNote(text(R.string.location_note_withdrawn)) }
            return null
        }
        updatePending { it.copy(locating = true) }
        val place = deviceLocation.locate()
        updatePending {
            val done = it.copy(locating = false)
            if (place == null) done.withSearchNote(text(R.string.location_note_none)) else done
        }
        return place
    }

    private inline fun updatePending(change: (PendingReply) -> PendingReply) {
        transient.update { t -> t.copy(pending = t.pending?.let(change)) }
    }

    /** Adds a lookup remark once (a repeated remark is not shown twice), at most a handful. */
    private fun PendingReply.withSearchNote(note: String): PendingReply =
        if (note in searchNotes || searchNotes.size >= MAX_SEARCH_NOTES) this else copy(searchNotes = searchNotes + note)

    /**
     * Saves whatever arrived (text, notice, sources, lookup notes, the picture being made or the
     * reason none was) as the assistant's message. A reply with only a picture is kept too: the
     * picture lands in it.
     */
    private fun finishReply(conversationId: String) {
        val reply = transient.value.pending ?: return
        transient.update { it.copy(pending = null) }
        val notice = reply.notice ?: if (stopRequested) text(R.string.notice_stopped) else null
        stopRequested = false
        if (reply.text.isBlank() && notice == null && reply.picture == null) return

        val conversation = store.get(conversationId) ?: return
        val now = System.currentTimeMillis()
        val answer = ChatMessage(
            id = reply.messageId,
            role = Role.ASSISTANT,
            content = reply.text,
            notice = notice,
            createdAt = now,
            sources = reply.sources,
            searchNotes = reply.searchNotes,
            picture = reply.picture,
        )
        store.upsert(conversation.copy(updatedAt = now, messages = conversation.messages + answer))
        refreshStatus()
    }

    private fun onHttpError(e: ApiException.Http) {
        when {
            // The hour's photo allowance is used up: say so in the server's words, and keep Send working for text.
            isPhotoQuota(e) -> showError(ChatError(e.detail, ErrorKind.REJECTED))
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

    // ------------------------------------------------------------------ creating images
    //
    // Two things make pictures: the Create image dialog (its request and picture are messages of their own)
    // and the chat itself (the model calls its picture tool, the server sends `image_job`, and the picture
    // lands inside the reply that asked for it). Both end up as a job in `transient.images`, by job id, so
    // several can run at once; each is polled on its own while the app is on screen.

    private var imageRunner: Job? = null

    /** One poller per running job, by job id. */
    private val pollers = HashMap<String, Job>()

    /** Jobs whose end is being written to the chat (the picture is saved first), so an end is applied once. */
    private val finishing = HashSet<String>()

    /** True while the app is on screen: images are polled only then (the server keeps the result for minutes). */
    private var visible = false

    /**
     * Asks the server for an image of [prompt]. The conversation gets the placeholder
     * `[Image: prompt]` (never sent to the chat model), a card shows the job, and the picture
     * is added when it is ready. A refusal or error before the job exists is shown in the error
     * banner with the server's own words, and the request is taken back out of the chat.
     */
    fun createImage(prompt: String, size: String) {
        val clean = prompt.trim().takeChars(MAX_IMAGE_PROMPT_CHARS)
        if (clean.isEmpty() || dialogImageBusy() || !settings.privacyAcknowledged.value) return

        val now = System.currentTimeMillis()
        val before = currentId.value?.let(store::get)
        val base = before ?: Conversation(newId(), titleFrom(clean), now, now, emptyList())
        val request = ChatMessage(newId(), Role.USER, imagePlaceholder(clean), createdAt = now, image = ImageAttachment(clean))
        store.upsert(base.copy(updatedAt = now, messages = base.messages + request))
        savedState[KEY_CURRENT] = base.id
        if (draft.trim() == clean) draft = ""

        val serverUrl = settings.serverUrl.value
        val job = ImageJobUi(base.id, clean, size, etaSeconds = transient.value.status?.imageEtaSeconds(size))
        transient.update { it.copy(request = job, error = null) }
        imageRunner = viewModelScope.launch {
            try {
                // The conversation's id is the chat_id, as for chat: one image counts as one trial chat.
                val ticket = api.createImage(serverUrl, clean, size, base.id)
                val running = job.copy(jobId = ticket.id, position = ticket.position, etaSeconds = ticket.etaSeconds ?: job.etaSeconds)
                // Not if the chat was deleted meanwhile (the request was dropped): then nobody waits for it.
                transient.update { t -> if (t.request == job) t.copy(request = null, images = t.images.with(running)) else t }
                startImagePolling(ticket.id)
            } catch (e: CancellationException) {
                throw e
            } catch (e: ApiException.Http) {
                refuseImage(base, before, request.id, clean, imageHttpMessage(e))
            } catch (e: ApiException.Network) {
                refuseImage(base, before, request.id, clean, text(R.string.error_network))
            }
        }
    }

    /** Takes the request back out of the chat (and puts the prompt back in the box) after an error. */
    private fun refuseImage(base: Conversation, before: Conversation?, requestId: String, prompt: String, message: String) {
        transient.update { it.copy(request = null, error = ChatError(message, ErrorKind.REJECTED)) }
        if (before != null) {
            store.get(base.id)?.let { c -> store.upsert(c.copy(messages = c.messages.filterNot { it.id == requestId })) }
        } else {
            store.delete(base.id)
            savedState.set<String?>(KEY_CURRENT, null)
        }
        if (draft.isBlank()) draft = prompt
    }

    private fun imageHttpMessage(e: ApiException.Http): String {
        val wait = e.retryAfterSeconds?.toInt()?.takeIf { e.code == 429 && it > 0 } ?: return e.detail
        val hint = getApplication<Application>().resources.getQuantityString(R.plurals.image_try_again, wait, wait)
        return "${e.detail} $hint"
    }

    /**
     * A Create image request is waiting for its job id or a picture made with the dialog is running:
     * the dialog makes one picture at a time (pictures the chat makes do not count).
     */
    private fun dialogImageBusy(): Boolean =
        transient.value.request != null || transient.value.images.all.any { it.replyId == null }

    /** Stops the job [id]: asks the server to cancel it; what the server answers decides what the card becomes. */
    fun cancelImage(id: String) {
        val job = transient.value.images[id] ?: return
        if (job.cancelling || id in finishing) return
        updateImage(id) { it.copy(cancelling = true) }
        viewModelScope.launch {
            val status = try {
                api.cancelImage(settings.serverUrl.value, id)
            } catch (e: ApiException.Http) {
                ImageStatus.CANCELLED // 404: the server no longer knows the job, so there is nothing to wait for
            } catch (e: ApiException.Network) {
                updateImage(id) { it.copy(cancelling = false) }
                showError(ChatError(text(R.string.error_network), ErrorKind.REJECTED))
                return@launch
            }
            if (status == ImageStatus.COMPLETED) { // it finished first: let the next poll fetch the picture
                updateImage(id) { it.copy(cancelling = false) }
                startImagePolling(id)
            } else {
                pollers.remove(id)?.cancel()
                finishImage(job, null, notice = text(R.string.image_cancelled))
            }
        }
    }

    /**
     * The model asked for a picture ([event], sent before the reply's text): the job is tracked like a
     * Create image job, but its card sits in the reply streaming now and the picture lands in that
     * reply. A reply makes one picture, so a second job in the same reply is ignored.
     */
    private fun startReplyPicture(conversationId: String, event: StreamEvent.ImageJob) {
        transient.update { t ->
            val reply = t.pending ?: return@update t
            if (reply.picture?.created == true || event.id in t.images) return@update t
            val size = event.size ?: DEFAULT_IMAGE_SIZE
            val job = ImageJobUi(
                conversationId, event.prompt, size, jobId = event.id, replyId = reply.messageId,
                position = event.position, etaSeconds = event.etaSeconds ?: t.status?.imageEtaSeconds(size),
            )
            t.copy(images = t.images.with(job), pending = reply.withPictureJob(event.prompt))
        }
        startImagePolling(event.id)
    }

    /** The app came to the front: carry on polling every running job. */
    fun onForeground() {
        visible = true
        transient.value.images.ids.forEach(::startImagePolling)
    }

    /** The app left the screen: stop polling (the server keeps the result for a few minutes). */
    fun onBackground() {
        visible = false
        pollers.values.forEach { it.cancel() }
        pollers.clear()
    }

    private fun startImagePolling(id: String) {
        if (!visible || id !in transient.value.images || id in finishing || pollers[id]?.isActive == true) return
        val serverUrl = settings.serverUrl.value
        pollers[id] = viewModelScope.launch {
            var failures = 0
            while (isActive) {
                try {
                    val progress = api.imageProgress(serverUrl, id)
                    failures = 0
                    if (!applyImageProgress(id, progress)) return@launch
                } catch (e: ApiException.Http) {
                    transient.value.images[id]?.let { finishImage(it, null, notice = e.detail) }
                    return@launch
                } catch (e: ApiException.Network) {
                    if (++failures >= MAX_IMAGE_POLL_FAILURES) {
                        transient.value.images[id]?.let { finishImage(it, null, notice = text(R.string.image_lost_contact)) }
                        return@launch
                    }
                }
                delay(IMAGE_POLL_MS)
            }
        }
    }

    /** Shows the latest poll on the card. Returns false once the job has ended and polling should stop. */
    private suspend fun applyImageProgress(id: String, progress: ImageProgress): Boolean {
        val job = transient.value.images[id] ?: return false // cancelled or deleted meanwhile
        if (!progress.status.finished) {
            updateImage(id) {
                it.copy(
                    status = progress.status,
                    position = progress.position,
                    elapsedSeconds = progress.elapsedSeconds,
                    etaSeconds = progress.etaSeconds ?: it.etaSeconds,
                )
            }
            return true
        }
        when (progress.status) {
            ImageStatus.COMPLETED -> finishImage(job, progress.image)
            ImageStatus.CANCELLED -> finishImage(job, null, notice = progress.errorDetail ?: text(R.string.image_cancelled))
            ImageStatus.EXPIRED -> finishImage(job, null, notice = progress.errorDetail ?: text(R.string.image_expired))
            ImageStatus.REFUSED -> finishImage(job, null, notice = progress.errorDetail ?: text(R.string.image_refused))
            else -> finishImage(job, null, notice = progress.errorDetail ?: text(R.string.image_failed))
        }
        return false
    }

    /**
     * Ends [job]: with [image] the picture is saved in private storage and shown in the chat;
     * otherwise [notice] (the server's own words when it had some) is shown in its place. The job
     * stays in the running set until the chat has the result, so a card never gives way to a gap.
     */
    private fun finishImage(job: ImageJobUi, image: GeneratedImage?, notice: String? = null) {
        val id = job.jobId ?: return
        if (id !in transient.value.images || !finishing.add(id)) return // already ended, or being ended
        viewModelScope.launch {
            try {
                val file = image?.let { withContext(Dispatchers.IO) { images.save(it.png) } }
                saveImageEnd(job, image, file, notice)
            } finally {
                finishing.remove(id)
                pollers.remove(id)
            }
        }
    }

    /** Writes how [job] ended into its chat (a message of its own, or inside its reply), then drops the job. */
    private fun saveImageEnd(job: ImageJobUi, image: GeneratedImage?, file: String?, notice: String?) {
        val id = job.jobId ?: return
        val why = notice ?: if (image != null) text(R.string.image_save_failed) else text(R.string.image_failed)
        val now = System.currentTimeMillis()
        val conversation = store.get(job.conversationId)
        when {
            conversation == null -> images.delete(listOf(file)) // deleted while the picture was being made
            job.replyId != null -> saveInReply(job, job.replyId, conversation, image, file, why, now)
            else -> {
                val message = if (image != null && file != null) {
                    ChatMessage(
                        newId(), Role.ASSISTANT, imagePlaceholder(job.prompt), createdAt = now,
                        image = ImageAttachment(job.prompt, file, image.width, image.height),
                    )
                } else {
                    ChatMessage(newId(), Role.ASSISTANT, "", why, now, image = ImageAttachment(job.prompt))
                }
                store.upsert(conversation.copy(updatedAt = now, messages = conversation.messages + message))
            }
        }
        transient.update { it.copy(images = it.images.without(id)) }
    }

    /**
     * The end of a picture the chat made goes into the reply that asked for it: the reply still
     * streaming (a refusal or failure can come before it is saved) keeps it for when it is saved,
     * a saved reply is updated. With no reply left, the picture file is removed.
     */
    private fun saveInReply(
        job: ImageJobUi,
        replyId: String,
        conversation: Conversation,
        image: GeneratedImage?,
        file: String?,
        why: String,
        now: Long,
    ) {
        fun ended(before: ReplyPicture?): ReplyPicture {
            val started = before ?: ReplyPicture(job.prompt)
            return if (image != null && file != null) started.made(file, image.width, image.height) else started.failed(why)
        }
        if (transient.value.pending?.messageId == replyId) {
            transient.update { t ->
                val reply = t.pending?.takeIf { it.messageId == replyId } ?: return@update t
                t.copy(pending = reply.withPicture(ended(reply.picture)))
            }
        } else {
            val updated = conversation.withReplyPicture(replyId, now, ::ended)
            if (updated != null) store.upsert(updated) else images.delete(listOf(file)) // no reply left to show it in
        }
    }

    /**
     * Forgets the running jobs of [conversationId] (every job when null) without a trace in the chat
     * (it was deleted); the server is told to stop each one.
     */
    private fun dropImageJobs(conversationId: String?) {
        fun mine(jobConversationId: String) = conversationId == null || jobConversationId == conversationId
        if (transient.value.request?.let { mine(it.conversationId) } == true) {
            imageRunner?.cancel()
            transient.update { it.copy(request = null) }
        }
        val serverUrl = settings.serverUrl.value
        for (job in transient.value.images.all.filter { mine(it.conversationId) }) {
            val id = job.jobId ?: continue
            pollers.remove(id)?.cancel()
            transient.update { it.copy(images = it.images.without(id)) }
            viewModelScope.launch {
                try {
                    api.cancelImage(serverUrl, id)
                } catch (_: ApiException) {
                    // best effort: the result expires on its own
                }
            }
        }
    }

    private fun updateImage(id: String, change: (ImageJobUi) -> ImageJobUi) {
        transient.update { t -> t.copy(images = t.images.changed(id, change)) }
    }

    // ------------------------------------------------------------------ conversations

    fun newChat() {
        savedState.set<String?>(KEY_CURRENT, null)
        draft = ""
        clearPhotoDraft()
        screen = Screen.Chat
        closeReport()
        dismissError()
        dismissLocationAsk()
    }

    /** The card belongs to the message in the box and the chat it would go to: leaving either takes it away. */
    private fun dismissLocationAsk() {
        transient.update { it.copy(locationAsk = null) }
    }

    /** A share from another app: open a fresh chat with [text] ready to send. */
    fun startChatWithSharedText(text: String) {
        newChat()
        draft = text.take(MAX_SHARED_CHARS)
    }

    fun openConversation(id: String) {
        if (currentId.value != id) {
            draft = ""
            clearPhotoDraft()
            dismissLocationAsk()
        }
        savedState[KEY_CURRENT] = id
        screen = Screen.Chat
        closeReport()
        dismissError()
    }

    fun deleteConversation(id: String) {
        if (transient.value.pending?.conversationId == id) stop()
        dropImageJobs(id)
        store.get(id)?.let { images.delete(it.imageFiles()) }
        store.delete(id)
        if (currentId.value == id) newChat()
    }

    fun deleteAllConversations() {
        if (transient.value.pending != null) stop()
        dropImageJobs(null)
        images.delete(store.conversations.value.flatMap { it.imageFiles() })
        store.deleteAll()
        newChat()
    }

    // ------------------------------------------------------------------ settings and status

    fun showSettings() {
        // If the permission was withdrawn in Android's settings, show the switch off.
        if (settings.shareLocation.value && !deviceLocation.permitted()) settings.setShareLocation(false)
        screen = Screen.Settings
    }

    /** Turning it on is only called once the permission is granted; turning it off stops sending at once. */
    fun setShareLocation(on: Boolean) = settings.setShareLocation(on)

    fun locationPermitted(): Boolean = deviceLocation.permitted()

    fun closeSettings() {
        screen = Screen.Chat
    }

    fun acknowledgePrivacy() = settings.acknowledgePrivacy()

    /** Returns false (nothing saved) if [input] is not a usable https address. */
    fun setServerUrl(input: String): Boolean {
        val saved = settings.setServerUrl(input)
        if (saved) {
            forgetStatus()
            refreshStatus()
        }
        return saved
    }

    fun resetServerUrl() {
        settings.resetServerUrl()
        forgetStatus()
        refreshStatus()
    }

    /** Another server is in use: what the last one said about itself is no longer shown. */
    private fun forgetStatus() {
        transient.update { it.copy(status = null, statusServer = null) }
    }

    private val statusRefresher = StatusRefresher(viewModelScope, STATUS_INTERVAL_MS) { loadStatus() }

    /**
     * Refreshes the header's model and machine count while the app is visible: at once (so coming
     * back to the foreground shows the live model straight away, whatever the server switched to
     * meanwhile) and then every minute. See [StatusRefresher].
     */
    fun startStatusUpdates() = statusRefresher.start()

    fun stopStatusUpdates() = statusRefresher.stop()

    private fun refreshStatus() {
        viewModelScope.launch { loadStatus() }
    }

    /**
     * Fetches the status and shows it. A refresh that fails keeps the last good status of the same server
     * (see [ShownStatus]), so a network blip does not blank the header and the "+" menu until the next
     * minute; chat still works or reports its own error.
     */
    private suspend fun loadStatus() {
        val serverUrl = settings.serverUrl.value
        val fresh = try {
            api.fetchStatus(serverUrl)
        } catch (e: ApiException) {
            null
        }
        if (settings.serverUrl.value != serverUrl) return // the address was changed meanwhile: this answer is about another server
        transient.update { t ->
            val shown = ShownStatus(t.status, t.statusServer).after(fresh, serverUrl)
            t.copy(status = shown.status, statusServer = shown.server)
        }
    }

    private fun text(id: Int, vararg args: Any): String = getApplication<Application>().getString(id, *args)

    private companion object {
        const val KEY_CURRENT = "current_conversation"
        const val DEFAULT_RETRY_SECONDS = 5L
        const val STATUS_INTERVAL_MS = 60_000L
        const val MAX_SHARED_CHARS = 20_000
        const val MAX_SEARCH_NOTES = 6
        const val IMAGE_POLL_MS = 2_000L
        const val MAX_IMAGE_POLL_FAILURES = 5

        /** A chosen photo that is not ready after this long is given up on (a cloud photo with no connection). */
        const val PHOTO_PREPARE_MS = 30_000L

        /** How long Send waits for photos still being prepared before the message goes without them. */
        const val PREPARING_GRACE_MS = 2_000L
        const val PREPARING_POLL_MS = 50L
    }
}
