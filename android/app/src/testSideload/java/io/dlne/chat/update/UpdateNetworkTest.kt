package io.dlne.chat.update

import java.io.File
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** The downloader and the manifest fetcher against a local server: what is kept, and what is thrown away. */
class UpdateNetworkTest {
    @get:Rule
    val temp = TemporaryFolder()

    private lateinit var server: MockWebServer
    private lateinit var dir: File
    private val apk = ByteArray(250_000) { (it % 251).toByte() }
    private val apkHash = Sha256.ofBytes(apk)

    @Before
    fun start() {
        server = MockWebServer().also { it.start() }
        dir = temp.newFolder("updates")
    }

    @After
    fun stop() {
        server.shutdown()
    }

    private fun download(
        size: Long = apk.size.toLong(),
        sha256: String = apkHash,
        cancelled: () -> Boolean = { false },
        progress: (Long) -> Unit = {},
    ): DownloadResult = ApkDownloader().download(
        url = server.url("/downloads/DLNE-Chat-1.0.5.apk"),
        expectedSize = size,
        expectedSha256 = sha256,
        destination = File(dir, "update.apk"),
        isCancelled = cancelled,
        onProgress = progress,
    )

    private fun assertNothingLeft() = assertEquals("no file, not even a .part", emptyList<String>(), dir.list()!!.toList())

    private fun serve(body: ByteArray) = server.enqueue(MockResponse().setBody(Buffer().write(body)))

    @Test
    fun `the exact published file is kept, with progress`() {
        serve(apk)
        val seen = mutableListOf<Long>()
        val result = download(progress = { seen += it })
        val done = result as DownloadResult.Done
        assertEquals(apkHash, Sha256.ofFile(done.file))
        assertEquals(listOf("update.apk"), dir.list()!!.toList())
        assertEquals(apk.size.toLong(), seen.last())
        assertTrue("progress only goes up", seen.zipWithNext().all { (a, b) -> b >= a })
    }

    @Test
    fun `a wrong hash is thrown away`() {
        serve(apk)
        val result = download(sha256 = Sha256.ofBytes("something else".toByteArray()))
        assertEquals(DownloadProblem.HASH_MISMATCH, (result as DownloadResult.Failed).problem)
        assertNothingLeft()
    }

    @Test
    fun `one changed byte is caught`() {
        val tampered = apk.copyOf().also { it[100_000] = (it[100_000] + 1).toByte() }
        serve(tampered)
        assertEquals(DownloadProblem.HASH_MISMATCH, (download() as DownloadResult.Failed).problem)
        assertNothingLeft()
    }

    @Test
    fun `a file bigger than published is cut off and thrown away`() {
        serve(apk + ByteArray(1000))
        val result = download() as DownloadResult.Failed
        assertEquals(DownloadProblem.SIZE_MISMATCH, result.problem)
        assertNothingLeft()
    }

    @Test
    fun `a file smaller than published is thrown away`() {
        serve(apk.copyOf(apk.size - 1))
        assertEquals(DownloadProblem.SIZE_MISMATCH, (download() as DownloadResult.Failed).problem)
        assertNothingLeft()
    }

    @Test
    fun `a connection that drops half way leaves nothing`() {
        server.enqueue(
            MockResponse().setBody(Buffer().write(apk))
                .setSocketPolicy(okhttp3.mockwebserver.SocketPolicy.DISCONNECT_DURING_RESPONSE_BODY),
        )
        val result = download() as DownloadResult.Failed
        assertTrue(result.problem == DownloadProblem.NETWORK || result.problem == DownloadProblem.SIZE_MISMATCH)
        assertNothingLeft()
    }

    @Test
    fun `an HTTP error is not a download`() {
        server.enqueue(MockResponse().setResponseCode(404).setBody("nope"))
        assertEquals(DownloadProblem.HTTP, (download() as DownloadResult.Failed).problem)
        assertNothingLeft()
    }

    @Test
    fun `a redirect is never followed, not even to the same server`() {
        val other = MockWebServer().also { it.start() }
        try {
            other.enqueue(MockResponse().setBody(Buffer().write(apk)))
            server.enqueue(MockResponse().setResponseCode(302).setHeader("Location", other.url("/evil.apk").toString()))
            val result = download() as DownloadResult.Failed
            assertEquals(DownloadProblem.REDIRECTED, result.problem)
            assertEquals("the second server was never asked", 0, other.requestCount)
            assertNothingLeft()

            server.enqueue(MockResponse().setResponseCode(301).setHeader("Location", server.url("/downloads/other.apk").toString()))
            assertEquals(DownloadProblem.REDIRECTED, (download() as DownloadResult.Failed).problem)
            assertEquals("no second request to the same server either", 2, server.requestCount)
        } finally {
            other.shutdown()
        }
    }

    @Test
    fun `a size announced differently from the manifest is refused before reading`() {
        serve(apk)
        val result = download(size = apk.size + 5L) as DownloadResult.Failed
        assertEquals(DownloadProblem.SIZE_MISMATCH, result.problem)
        assertNothingLeft()
    }

    @Test
    fun `cancelling stops the download and leaves nothing`() {
        serve(apk)
        var reads = 0
        val result = download(cancelled = { ++reads > 2 }) as DownloadResult.Failed
        assertEquals(DownloadProblem.CANCELLED, result.problem)
        assertNothingLeft()
    }

    @Test
    fun `an earlier leftover file is replaced, not trusted`() {
        File(dir, "update.apk").writeBytes(apk)
        server.enqueue(MockResponse().setResponseCode(500))
        download()
        assertFalse(File(dir, "update.apk").exists())
    }

    // ------------------------------------------------------------------ the manifest fetch

    @Test
    fun `the manifest text is returned`() {
        server.enqueue(MockResponse().setBody("""{"versionCode":6}"""))
        val result = ManifestFetcher().fetch(server.url("/downloads/android.json"))
        assertEquals("""{"versionCode":6}""", (result as FetchResult.Text).body)
    }

    @Test
    fun `a manifest redirect is refused`() {
        server.enqueue(MockResponse().setResponseCode(302).setHeader("Location", server.url("/elsewhere").toString()))
        assertTrue(ManifestFetcher().fetch(server.url("/downloads/android.json")) is FetchResult.Failed)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun `a manifest error status or oversized body is refused`() {
        server.enqueue(MockResponse().setResponseCode(503))
        assertTrue(ManifestFetcher().fetch(server.url("/x")) is FetchResult.Failed)
        server.enqueue(MockResponse().setBody("x".repeat(UpdateSource.MAX_MANIFEST_BYTES + 10)))
        assertTrue(ManifestFetcher().fetch(server.url("/x")) is FetchResult.Failed)
    }
}
