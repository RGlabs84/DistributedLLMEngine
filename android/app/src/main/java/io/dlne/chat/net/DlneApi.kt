package io.dlne.chat.net

import io.dlne.chat.data.PhoneLocation
import io.dlne.chat.data.ReportRequest
import java.io.IOException
import java.io.InputStream
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.channels.ProducerScope
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response

/**
 * One turn of the conversation as the server receives it. [content] is the turn's text and goes on
 * the wire as a plain string. A user turn with photos carries them in [images], each the
 * `data:image/jpeg;base64,...` URL of one photo, and then goes as a list of parts instead (see
 * [WireMessageSerializer]).
 */
@Serializable(with = WireMessageSerializer::class)
data class WireMessage(val role: String, val content: String, val images: List<String> = emptyList())

@Serializable
private data class ChatRequest(
    @SerialName("chat_id") val chatId: String,
    val messages: List<WireMessage>,
    @SerialName("max_tokens") val maxTokens: Int,
    val stream: Boolean,
    // Left out of the request when null (defaults are not encoded), which is the usual case.
    val location: PhoneLocation? = null,
    // What this app can show beyond text (see CHAT_FEATURES). It has no default on purpose: a default is not encoded.
    val features: List<String>,
)

/** The parts of `GET /v1/cluster/status` the app shows. Unknown fields are ignored. */
@Serializable
data class ClusterStatus(
    @SerialName("app_pool") val appPool: AppPool? = null,
    @SerialName("server_version") val serverVersion: String? = null,
    /** What this server can do beyond chat; missing (an older server) means nothing extra. */
    val capabilities: Capabilities? = null,
    @SerialName("image_generation") val imageGenerationInfo: ImageGenerationInfo? = null,
) {
    /** True only when the server says so: the "Create image" button is shown for nothing else. */
    val imageGeneration: Boolean get() = capabilities?.imageGeneration == true

    /** True only when the server says the phone chat can look at photos: Photos and Camera are shown for nothing else. */
    val appVision: Boolean get() = capabilities?.appVision == true
}

/** The CPU pool that answers the phone app: how many machines and which model. */
@Serializable
data class AppPool(
    val members: Int? = null,
    @SerialName("members_ready") val membersReady: Int? = null,
    @SerialName("active_model") val activeModel: String? = null,
    @SerialName("median_answer_seconds") val medianAnswerSeconds: Double? = null,
)

/** Why a request did not produce an answer. */
sealed class ApiException(message: String, cause: Throwable? = null) : Exception(message, cause) {
    /**
     * The server refused the request: 400 for a bad request, 429 when this network is
     * already waiting for an answer or asked too often. [detail] is the server's own
     * explanation; [retryAfterSeconds] comes from the Retry-After header when present;
     * [errorCode] is the JSON `code` (for example `image_quota`) when there is one.
     */
    class Http(val code: Int, val detail: String, val retryAfterSeconds: Long?, val errorCode: String? = null) :
        ApiException("HTTP $code: $detail")

    /** No usable answer at all: offline, DNS, TLS, a timeout or a dropped connection. */
    class Network(cause: IOException) : ApiException(cause.message ?: "Network error", cause)
}

/**
 * The DLNE service client. Plain OkHttp with no interceptors, cookies, cache or tracking
 * headers: each call sends exactly the conversation, its random chat id and, only if the
 * person turned that on, their location.
 */
class DlneApi(private val http: OkHttpClient = defaultHttpClient()) {

    /**
     * Streams one answer from `POST {baseUrl}/v1/app/chat/completions`.
     *
     * The returned flow emits [StreamEvent]s token by token and completes after
     * `data: [DONE]` (or when the server closes the stream). It fails with [ApiException].
     * Cancelling the collector (the Stop button) cancels the HTTP call, which unblocks the
     * read immediately and tells the server to stop generating.
     *
     * [chatId] is the conversation's random id: the server keeps a private prompt cache per
     * chat with it. [location] is sent only when given (see [io.dlne.chat.data.DeviceLocation]).
     * Every request also says which extra events this app can show ([CHAT_FEATURES]).
     */
    fun streamChat(
        baseUrl: String,
        chatId: String,
        messages: List<WireMessage>,
        location: PhoneLocation? = null,
        maxTokens: Int = DEFAULT_MAX_TOKENS,
    ): Flow<StreamEvent> = callbackFlow {
        val body = json.encodeToString(
            ChatRequest.serializer(),
            ChatRequest(chatId, messages, maxTokens, stream = true, location = location, features = CHAT_FEATURES),
        )
        val request = Request.Builder()
            .url(endpoint(baseUrl, CHAT_PATH))
            .header("Accept", "text/event-stream")
            .post(body.toRequestBody(JSON))
            .build()
        val call = http.newCall(request)

        launch(Dispatchers.IO) {
            try {
                call.execute().use { response ->
                    if (!response.isSuccessful) throw httpError(response)
                    val stream = response.body?.byteStream() ?: throw IOException("Empty response")
                    pump(stream)
                }
                close()
            } catch (e: ApiException) {
                close(e)
            } catch (e: IOException) {
                // After Stop the call is cancelled on purpose; that is not an error.
                close(if (call.isCanceled()) null else ApiException.Network(e))
            }
        }
        awaitClose { call.cancel() }
    }

    /** Fetches the service status shown in the chat header. */
    suspend fun fetchStatus(baseUrl: String): ClusterStatus = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(endpoint(baseUrl, STATUS_PATH)).get().build()
        try {
            shortClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) throw httpError(response)
                val text = response.body?.string() ?: throw IOException("Empty response")
                json.decodeFromString(ClusterStatus.serializer(), text)
            }
        } catch (e: IOException) {
            throw ApiException.Network(e)
        } catch (e: SerializationException) {
            throw ApiException.Network(IOException("Unreadable status", e))
        } catch (e: IllegalArgumentException) {
            throw ApiException.Network(IOException("Unreadable status", e))
        }
    }

    /**
     * Sends one answer report to `POST {baseUrl}/v1/app/report`. Returns normally when the
     * server confirms with `{"ok": true}`. Fails with [ApiException.Http] (404 on servers
     * without reporting, 429 with Retry-After when reporting too often) or
     * [ApiException.Network].
     */
    suspend fun sendReport(baseUrl: String, report: ReportRequest): Unit = withContext(Dispatchers.IO) {
        val body = json.encodeToString(ReportRequest.serializer(), report)
        val request = Request.Builder().url(endpoint(baseUrl, REPORT_PATH)).post(body.toRequestBody(JSON)).build()
        try {
            shortClient.newCall(request).execute().use { response ->
                if (!response.isSuccessful) throw httpError(response)
                val text = response.peekBody(64L * 1024).string()
                val ok = try {
                    ((json.parseToJsonElement(text) as? JsonObject)?.get("ok") as? JsonPrimitive)?.contentOrNull == "true"
                } catch (_: SerializationException) {
                    false
                } catch (_: IllegalArgumentException) {
                    false
                }
                if (!ok) throw ApiException.Http(response.code, extractDetail(text) ?: "The report was not accepted.", null)
            }
        } catch (e: IOException) {
            throw ApiException.Network(e)
        }
    }

    /**
     * Asks for an image: `POST {baseUrl}/v1/app/images`. [chatId] is the conversation's random
     * id (the free trial counts one image as one chat). Fails with [ApiException.Http] (400
     * prompt_empty / prompt_too_long / bad_size / prompt_refused, 429 image_quota / image_busy
     * with Retry-After, 503 image_generation_unavailable) or [ApiException.Network].
     */
    suspend fun createImage(baseUrl: String, prompt: String, size: String?, chatId: String?): ImageTicket =
        withContext(Dispatchers.IO) {
            val body = json.encodeToString(ImageRequestBody.serializer(), ImageRequestBody(prompt, size, chatId))
            val request = Request.Builder().url(endpoint(baseUrl, IMAGES_PATH)).post(body.toRequestBody(JSON)).build()
            imageCall(request) { text ->
                json.decodeFromString(WireTicket.serializer(), text).toTicket().also {
                    if (it.id.isBlank()) throw SerializationException("no job id")
                }
            }
        }

    /** One poll of `GET {baseUrl}/v1/app/images/{id}`; the picture arrives with `completed`. */
    suspend fun imageProgress(baseUrl: String, id: String): ImageProgress = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(imageEndpoint(baseUrl, id)).get().build()
        imageCall(request, MAX_IMAGE_RESPONSE_BYTES) { text ->
            json.decodeFromString(WireProgress.serializer(), text).toProgress()
        }
    }

    /** `POST {baseUrl}/v1/app/images/{id}/cancel`; returns the job's status afterwards. */
    suspend fun cancelImage(baseUrl: String, id: String): ImageStatus = withContext(Dispatchers.IO) {
        val request = Request.Builder().url(imageEndpoint(baseUrl, id, "cancel"))
            .post(ByteArray(0).toRequestBody(null)).build()
        imageCall(request) { text -> json.decodeFromString(WireProgress.serializer(), text).let { ImageStatus.of(it.status) } }
    }

    // A picture comes back base64 in the answer, so these calls get a longer limit than a status check.
    private val imageClient: OkHttpClient by lazy {
        http.newBuilder().callTimeout(60, TimeUnit.SECONDS).build()
    }

    private fun <T> imageCall(request: Request, maxBytes: Long = 64L * 1024, parse: (String) -> T): T = try {
        imageClient.newCall(request).execute().use { response ->
            if (!response.isSuccessful) throw httpError(response)
            val text = response.peekBody(maxBytes).string()
            try {
                parse(text)
            } catch (e: SerializationException) {
                throw ApiException.Network(IOException("Unreadable answer", e))
            } catch (e: IllegalArgumentException) {
                throw ApiException.Network(IOException("Unreadable answer", e))
            }
        }
    } catch (e: IOException) {
        throw ApiException.Network(e)
    }

    /** Reads SSE bytes as they arrive and forwards each event, stopping at [StreamEvent.Done]. */
    private suspend fun ProducerScope<StreamEvent>.pump(stream: InputStream) {
        val lines = SseLineBuffer()
        val chunk = ByteArray(8 * 1024)
        while (true) {
            val read = stream.read(chunk)
            if (read < 0) break
            for (line in lines.feed(chunk, 0, read)) {
                val event = SseEventParser.parse(line) ?: continue
                send(event)
                if (event == StreamEvent.Done) return
            }
        }
        for (line in lines.finish()) {
            SseEventParser.parse(line)?.let { send(it) }
        }
    }

    // A status check or a report should be quick or not at all; neither may hold up the chat.
    private val shortClient: OkHttpClient by lazy {
        http.newBuilder().callTimeout(15, TimeUnit.SECONDS).build()
    }

    companion object {
        /** The server caps answers at 1024 tokens; asking for that much is the most useful. */
        const val DEFAULT_MAX_TOKENS = 1024

        /**
         * Sent with every chat request: `image_jobs` says this app shows the `image_job` and
         * `image_refused` events, so the server lets the model create pictures in a reply. A server
         * that does not know it ignores it (and a request without it gets no picture tool).
         */
        internal val CHAT_FEATURES = listOf("image_jobs")

        private const val CHAT_PATH = "v1/app/chat/completions"
        private const val STATUS_PATH = "v1/cluster/status"
        private const val REPORT_PATH = "v1/app/report"
        private const val IMAGES_PATH = "v1/app/images"

        /** A 1024x1024 PNG is about 2 MB, 3 MB as base64; anything past this is not a picture. */
        private const val MAX_IMAGE_RESPONSE_BYTES = 16L * 1024 * 1024
        private val JSON = "application/json; charset=utf-8".toMediaType()
        private val json = Json { ignoreUnknownKeys = true; coerceInputValues = true }

        /**
         * Timeouts sized for volunteer CPUs: the service may try up to three machines for
         * up to 30 s each before the first token, so the read timeout (the longest silence
         * allowed between two reads) is generous. There is no overall call timeout because
         * a long answer simply streams for a while; the person can always press Stop.
         */
        fun defaultHttpClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(15, TimeUnit.SECONDS)
            .readTimeout(150, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            .retryOnConnectionFailure(true)
            .build()

        private fun endpoint(baseUrl: String, path: String): HttpUrl {
            val base = baseUrl.trimEnd('/').toHttpUrlOrNull()
                ?: throw ApiException.Network(IOException("Invalid server address: $baseUrl"))
            return base.newBuilder().addPathSegments(path).build()
        }

        private fun imageEndpoint(baseUrl: String, id: String, vararg more: String): HttpUrl {
            val builder = endpoint(baseUrl, IMAGES_PATH).newBuilder().addPathSegment(id)
            more.forEach { builder.addPathSegment(it) }
            return builder.build()
        }

        /** Builds the exception for a non-2xx answer from its JSON `detail` and Retry-After. */
        internal fun httpError(response: Response): ApiException.Http {
            val text = try {
                response.peekBody(64L * 1024).string()
            } catch (_: IOException) {
                ""
            }
            val detail = extractDetail(text) ?: "The server answered ${response.code}."
            val retryAfter = response.header("Retry-After")?.trim()?.toLongOrNull()?.coerceIn(1, 3600)
            return ApiException.Http(response.code, detail, retryAfter, extractCode(text))
        }

        /** The JSON `code` of an error body (`{"detail": "...", "code": "image_quota"}`), if any. */
        internal fun extractCode(body: String): String? {
            val root = try {
                json.parseToJsonElement(body) as? JsonObject
            } catch (_: SerializationException) {
                null
            } catch (_: IllegalArgumentException) {
                null
            } ?: return null
            return (root["code"] as? JsonPrimitive)?.contentOrNull?.takeIf { it.isNotBlank() && it.length <= 64 }
        }

        /**
         * FastAPI errors are `{"detail": "text"}`; validation errors are
         * `{"detail": [{"msg": "..."}]}`. Anything else yields null.
         */
        internal fun extractDetail(body: String): String? {
            val root = try {
                json.parseToJsonElement(body) as? JsonObject
            } catch (_: SerializationException) {
                null
            } catch (_: IllegalArgumentException) {
                null
            } ?: return null
            return when (val detail = root["detail"]) {
                is JsonPrimitive -> detail.contentOrNull?.takeIf { it.isNotBlank() }
                is JsonArray -> detail.mapNotNull { item ->
                    ((item as? JsonObject)?.get("msg") as? JsonPrimitive)?.contentOrNull
                }.joinToString("; ").takeIf { it.isNotBlank() }
                else -> null
            }
        }
    }
}
