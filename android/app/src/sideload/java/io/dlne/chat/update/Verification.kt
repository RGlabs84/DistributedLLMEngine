package io.dlne.chat.update

import java.io.File
import java.io.InputStream
import java.security.MessageDigest

/** SHA-256 helpers: the download is checked against the hash the website published. */
internal object Sha256 {
    fun newDigest(): MessageDigest = MessageDigest.getInstance("SHA-256")

    fun hex(bytes: ByteArray): String = buildString(bytes.size * 2) {
        for (b in bytes) {
            append("0123456789abcdef"[(b.toInt() shr 4) and 0xF])
            append("0123456789abcdef"[b.toInt() and 0xF])
        }
    }

    fun ofBytes(bytes: ByteArray): String = hex(newDigest().digest(bytes))

    fun ofStream(input: InputStream): String {
        val digest = newDigest()
        val buffer = ByteArray(BUFFER)
        while (true) {
            val n = input.read(buffer)
            if (n < 0) break
            digest.update(buffer, 0, n)
        }
        return hex(digest.digest())
    }

    fun ofFile(file: File): String = file.inputStream().use(::ofStream)

    /** True when [actual] equals [expected] (64 hex digits, any case). */
    fun matches(actual: String, expected: String): Boolean =
        expected.length == 64 && actual.equals(expected, ignoreCase = true)

    private const val BUFFER = 64 * 1024
}

/**
 * Who signed an APK, as SHA-256 fingerprints of the signing certificates. [current] are the
 * certificates that sign it now; [history] adds earlier ones when the app rotated its key
 * (Android's signing lineage), and equals [current] otherwise.
 */
internal data class SignerSet(val current: Set<String>, val history: Set<String> = current) {
    companion object {
        /** Builds a set from raw DER certificates, as Android hands them out. */
        fun fromCertificates(current: List<ByteArray>, history: List<ByteArray> = current): SignerSet =
            SignerSet(current.map(Sha256::ofBytes).toSet(), history.map(Sha256::ofBytes).toSet())
    }
}

/**
 * Early version of the check Android makes anyway: an update must be signed by the same
 * publisher as the installed app. Failing here gives a clear message instead of a vague
 * "App not installed" from the system installer.
 */
internal object SignerCheck {
    fun sameSigner(installed: SignerSet, downloaded: SignerSet): Boolean {
        if (installed.current.isEmpty() || downloaded.current.isEmpty()) return false
        if (installed.current == downloaded.current) return true
        // The update rotated its key: it proves it by carrying the installed certificate in its
        // lineage. Lineages exist for a single signer only; several signers must match exactly.
        if (installed.current.size != 1 || downloaded.current.size != 1) return false
        return installed.current.single() in downloaded.history
    }
}

/** What the downloaded file says about itself, read with PackageManager. */
internal data class ApkFacts(val packageName: String, val versionCode: Long, val signers: SignerSet)

internal enum class ApkProblem { WRONG_PACKAGE, NOT_NEWER, WRONG_VERSION, WRONG_SIGNER }

/** The checks a downloaded APK must pass before anything is handed to the installer. Null means all passed. */
internal object ApkCheck {
    fun problem(
        downloaded: ApkFacts,
        installed: ApkFacts,
        expectedVersionCode: Int,
    ): ApkProblem? = when {
        downloaded.packageName != installed.packageName -> ApkProblem.WRONG_PACKAGE
        downloaded.versionCode != expectedVersionCode.toLong() -> ApkProblem.WRONG_VERSION
        downloaded.versionCode <= installed.versionCode -> ApkProblem.NOT_NEWER
        !SignerCheck.sameSigner(installed.signers, downloaded.signers) -> ApkProblem.WRONG_SIGNER
        else -> null
    }
}
