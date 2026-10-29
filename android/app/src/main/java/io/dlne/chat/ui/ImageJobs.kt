package io.dlne.chat.ui

/**
 * The picture jobs running now, keyed by the server's job id. Several can run at once: one for each
 * reply that asked the chat for a picture, plus one made with the Create image dialog. A Create image
 * request that is still on its way has no job id yet, so it is not here (the view model holds it apart
 * until the server has answered).
 *
 * Immutable: every change returns a new set, so it can sit inside the view model's state.
 */
class ImageJobs private constructor(private val byId: Map<String, ImageJobUi>) {
    constructor() : this(emptyMap())

    val all: List<ImageJobUi> get() = byId.values.toList()

    val ids: List<String> get() = byId.keys.toList()

    operator fun get(id: String): ImageJobUi? = byId[id]

    operator fun contains(id: String): Boolean = id in byId

    /** Adds [job] under its own job id. A job that has none yet is not added. */
    fun with(job: ImageJobUi): ImageJobs {
        val id = job.jobId ?: return this
        return ImageJobs(byId + (id to job))
    }

    /** Applies [change] to the job [id] only; the other jobs and an unknown id are left alone. */
    fun changed(id: String, change: (ImageJobUi) -> ImageJobUi): ImageJobs {
        val job = byId[id] ?: return this
        return ImageJobs(byId + (id to change(job)))
    }

    fun without(id: String): ImageJobs = if (id in byId) ImageJobs(byId - id) else this

    fun inConversation(conversationId: String): List<ImageJobUi> = byId.values.filter { it.conversationId == conversationId }

    /** The job of the reply saved (or streaming) under [replyId], if it is still running. */
    fun forReply(replyId: String): ImageJobUi? = byId.values.forReply(replyId)

    override fun equals(other: Any?): Boolean = other is ImageJobs && other.byId == byId

    override fun hashCode(): Int = byId.hashCode()
}

/** The job whose picture belongs to the reply [replyId], among [this]. */
fun Iterable<ImageJobUi>.forReply(replyId: String): ImageJobUi? = firstOrNull { it.replyId == replyId }
