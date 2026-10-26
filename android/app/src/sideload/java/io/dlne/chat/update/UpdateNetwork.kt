package io.dlne.chat.update

import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit
import okhttp3.HttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request

/** The OkHttp client for update traffic: no redirects at all, so a file can never be served from elsewhere. */
internal fun updateHttpClient(base: OkHttpClient = OkHttpClient()): OkHttpClient = base.newBuilder()
    .followRedirects(false)
    .followSslRedirects(false)
    .connectTimeout(15, TimeUnit.SECONDS)
    .readTimeout(30, TimeUnit.SECONDS)
    .writeTimeout(30, TimeUnit.SECONDS)
    .callTimeout(15, TimeUnit.MINUTES)
    .build()

internal sealed interface FetchResult {
    data class Text(val body: String) : FetchResult
    data class Failed(val reason: String) : FetchResult
}

/** Downloads `android.json`. Blocking: call it off the main thread. */
internal class ManifestFetcher(client: OkHttpClient = updateHttpClient()) {
    // Built again here so no caller can hand in a client that follows redirects.
    private val http = updateHttpClient(client)

    fun fetch(url: HttpUrl): FetchResult = try {
        http.newCall(Request.Builder().url(url).build()).execute().use { response ->
            when {
                response.isRedirect -> FetchResult.Failed("redirected")
                !response.isSuccessful -> FetchResult.Failed("HTTP ${response.code}")
                else -> {
                    val source = response.body!!.source()
                    // Read at most one byte more than allowed, so an endless body costs nothing.
                    source.request(MAX_BYTES + 1)
                    if (source.buffer.size > MAX_BYTES) FetchResult.Failed("too large") else FetchResult.Text(source.buffer.readUtf8())
                }
            }
        }
    } catch (e: IOException) {
        FetchResult.Failed(e.message ?: "network error")
    }

    private companion object {
        const val MAX_BYTES = UpdateSource.MAX_MANIFEST_BYTES.toLong()
    }
}

internal enum class DownloadProblem { NETWORK, HTTP, REDIRECTED, SIZE_MISMATCH, HASH_MISMATCH, CANCELLED }

internal sealed interface DownloadResult {
    data class Done(val file: File) : DownloadResult
    data class Failed(val problem: DownloadProblem, val detail: String = "") : DownloadResult
}

/**
 * Downloads the update APK with OkHttp. The bytes are counted and hashed as they arrive; the
 * file is kept only if it is exactly the published size and SHA-256 (it is written as
 * `<name>.part` and renamed on success, so a half or wrong file never has the final name).
 * Blocking: call it off the main thread.
 */
internal class ApkDownloader(client: OkHttpClient = updateHttpClient()) {
    private val http = updateHttpClient(client)

    fun download(
        url: HttpUrl,
        expectedSize: Long,
        expectedSha256: String,
        destination: File,
        isCancelled: () -> Boolean = { false },
        onProgress: (bytes: Long) -> Unit = {},
    ): DownloadResult {
        val part = File(destination.parentFile, destination.name + ".part")
        part.parentFile?.mkdirs()
        part.delete()
        destination.delete()
        try {
            http.newCall(Request.Builder().url(url).build()).execute().use { response ->
                if (response.isRedirect) return DownloadResult.Failed(DownloadProblem.REDIRECTED)
                if (!response.isSuccessful) return DownloadResult.Failed(DownloadProblem.HTTP, response.code.toString())
                val body = response.body!!
                val announced = body.contentLength()
                if (announced >= 0 && announced != expectedSize) {
                    return DownloadResult.Failed(DownloadProblem.SIZE_MISMATCH, "announced $announced")
                }
                val digest = Sha256.newDigest()
                var total = 0L
                part.outputStream().use { out ->
                    val buffer = ByteArray(64 * 1024)
                    val input = body.byteStream()
                    while (true) {
                        if (isCancelled()) return DownloadResult.Failed(DownloadProblem.CANCELLED)
                        val n = input.read(buffer)
                        if (n < 0) break
                        total += n
                        if (total > expectedSize) return DownloadResult.Failed(DownloadProblem.SIZE_MISMATCH, "more than $expectedSize")
                        digest.update(buffer, 0, n)
                        out.write(buffer, 0, n)
                        onProgress(total)
                    }
                }
                if (total != expectedSize) return DownloadResult.Failed(DownloadProblem.SIZE_MISMATCH, "got $total")
                if (!Sha256.matches(Sha256.hex(digest.digest()), expectedSha256)) {
                    return DownloadResult.Failed(DownloadProblem.HASH_MISMATCH)
                }
            }
            if (!part.renameTo(destination)) return DownloadResult.Failed(DownloadProblem.NETWORK, "could not save the file")
            return DownloadResult.Done(destination)
        } catch (e: IOException) {
            return DownloadResult.Failed(DownloadProblem.NETWORK, e.message ?: "")
        } finally {
            part.delete()
        }
    }
}
