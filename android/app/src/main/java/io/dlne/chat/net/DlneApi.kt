package io.dlne.chat.net

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

/** One turn of the conversation as the server receives it. */
@Serializable
data class WireMessage(val role: String, val content: String)

@Serializable
private data class ChatRequest(
    val messages: List<WireMessage>,
    @SerialName("max_tokens") val maxTokens: Int,
    val stream: Boolean,
)

/** The parts of `GET /v1/cluster/status` the app shows. Unknown fields are ignored. */
@Serializable
data class ClusterStatus(
    @SerialName("app_pool") val appPool: AppPool? = null,
    @SerialName("server_version") val serverVersion: String? = null,
)

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
     * explanation; [retryAfterSeconds] comes from the Retry-After header when present.
     */
    class Http(val code: Int, val detail: String, val retryAfterSeconds: Long?) :
        ApiException("HTTP $code: $detail")

    /** No usable answer at all: offline, DNS, TLS, a timeout or a dropped connection. */
    class Network(cause: IOException) : ApiException(cause.message ?: "Network error", cause)
}

/**
 * The DLNE service client. Plain OkHttp with no interceptors, cookies, cache or tracking
 * headers: each call sends exactly the conversation and nothing else.
 */
class DlneApi(private val http: OkHttpClient = defaultHttpClient()) {

    /**
     * Streams one answer from `POST {baseUrl}/v1/app/chat/completions`.
     *
     * The returned flow emits [StreamEvent]s token by token and completes after
     * `data: [DONE]` (or when the server closes the stream). It fails with [ApiException].
     * Cancelling the collector (the Stop button) cancels the HTTP call, which unblocks the
     * read immediately and tells the server to stop generating.
     */
    fun streamChat(
        baseUrl: String,
        messages: List<WireMessage>,
        maxTokens: Int = DEFAULT_MAX_TOKENS,
    ): Flow<StreamEvent> = callbackFlow {
        val body = json.encodeToString(ChatRequest.serializer(), ChatRequest(messages, maxTokens, stream = true))
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

        private const val CHAT_PATH = "v1/app/chat/completions"
        private const val STATUS_PATH = "v1/cluster/status"
        private const val REPORT_PATH = "v1/app/report"
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

        /** Builds the exception for a non-2xx answer from its JSON `detail` and Retry-After. */
        internal fun httpError(response: Response): ApiException.Http {
            val text = try {
                response.peekBody(64L * 1024).string()
            } catch (_: IOException) {
                ""
            }
            val detail = extractDetail(text) ?: "The server answered ${response.code}."
            val retryAfter = response.header("Retry-After")?.trim()?.toLongOrNull()?.coerceIn(1, 3600)
            return ApiException.Http(response.code, detail, retryAfter)
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
