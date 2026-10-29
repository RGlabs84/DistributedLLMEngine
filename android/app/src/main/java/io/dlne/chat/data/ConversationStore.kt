package io.dlne.chat.data

import android.util.AtomicFile
import android.util.Log
import java.io.File
import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json

/**
 * Chat history kept in one JSON file in the app's private storage (no database, no
 * account, no sync). The in-memory list updates immediately so the screen never waits for
 * the disk; writes happen in the background, one at a time, always saving the newest state.
 *
 * There must be one instance per process (see [io.dlne.chat.AppGraph]) so that two
 * windows of the app never overwrite each other's changes.
 */
class ConversationStore(private val file: File, scope: CoroutineScope) {

    private val _conversations = MutableStateFlow<List<Conversation>>(emptyList())

    /** All conversations, most recently updated first. */
    val conversations: StateFlow<List<Conversation>> = _conversations.asStateFlow()

    // CONFLATED: a burst of changes collapses into a single write of the latest state.
    private val writeRequests = Channel<Unit>(Channel.CONFLATED)

    // Completes once the saved history has been read, with whether that was all of it.
    private val loaded = CompletableDeferred<Boolean>()

    init {
        scope.launch(Dispatchers.IO) {
            val fromDisk = read()
            // Anything created in the moment before the file was read is kept as well.
            _conversations.update { inMemory ->
                val ids = inMemory.mapTo(HashSet()) { it.id }
                sorted(inMemory + fromDisk.conversations.filter { it.id !in ids })
            }
            loaded.complete(fromDisk.complete)
            // Writes start only now, so an early change can never replace the file with a
            // list that is missing the conversations still being read.
            for (request in writeRequests) write(_conversations.value)
        }
    }

    /**
     * Waits until the saved history has been read. True when all of it was (no file yet counts: there
     * is nothing to read), false when the file could not be read or was unreadable, in which case the
     * list may be missing chats that still exist: anything that clears away what "no chat refers to"
     * must not run then.
     */
    suspend fun awaitLoaded(): Boolean = loaded.await()

    fun get(id: String): Conversation? = _conversations.value.firstOrNull { it.id == id }

    /** Adds [conversation] or replaces the stored one with the same id. */
    fun upsert(conversation: Conversation) {
        _conversations.update { list -> sorted(list.filter { it.id != conversation.id } + conversation) }
        writeRequests.trySend(Unit)
    }

    fun delete(id: String) {
        _conversations.update { list -> list.filter { it.id != id } }
        writeRequests.trySend(Unit)
    }

    fun deleteAll() {
        _conversations.value = emptyList()
        writeRequests.trySend(Unit)
    }

    private fun sorted(list: List<Conversation>) = list.sortedByDescending { it.updatedAt }

    /** What reading the saved history gave, and whether that is all of it. */
    private class Loaded(val conversations: List<Conversation>, val complete: Boolean)

    private fun read(): Loaded {
        if (!file.exists()) return Loaded(emptyList(), complete = true)
        return try {
            val text = AtomicFile(file).readFully().toString(Charsets.UTF_8)
            Loaded(json.decodeFromString(ConversationFile.serializer(), text).conversations, complete = true)
        } catch (e: IOException) {
            Log.w(TAG, "Could not read chat history", e)
            Loaded(emptyList(), complete = false)
        } catch (e: SerializationException) {
            Loaded(preserveUnreadable(e), complete = false)
        } catch (e: IllegalArgumentException) {
            Loaded(preserveUnreadable(e), complete = false)
        }
    }

    /** Keeps an unreadable file aside instead of overwriting it, then starts empty. */
    private fun preserveUnreadable(error: Exception): List<Conversation> {
        Log.w(TAG, "Chat history is unreadable; keeping it as ${file.name}.unreadable", error)
        file.renameTo(File(file.parentFile, "${file.name}.unreadable"))
        return emptyList()
    }

    private fun write(conversations: List<Conversation>) {
        val atomic = AtomicFile(file)
        val stream = try {
            atomic.startWrite()
        } catch (e: IOException) {
            Log.w(TAG, "Could not save chat history", e)
            return
        }
        try {
            val text = json.encodeToString(ConversationFile.serializer(), ConversationFile(conversations = conversations))
            stream.write(text.toByteArray(Charsets.UTF_8))
            atomic.finishWrite(stream)
        } catch (e: Exception) {
            atomic.failWrite(stream)
            Log.w(TAG, "Could not save chat history", e)
        }
    }

    private companion object {
        const val TAG = "ConversationStore"
        val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }
    }
}
