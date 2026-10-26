package io.dlne.chat.update

import android.content.pm.PackageInstaller
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class VerificationTest {
    @get:Rule
    val temp = TemporaryFolder()

    // ------------------------------------------------------------------ SHA-256

    @Test
    fun `sha256 matches the known test vectors`() {
        assertEquals("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", Sha256.ofBytes("abc".toByteArray()))
        assertEquals("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855", Sha256.ofBytes(ByteArray(0)))
    }

    @Test
    fun `sha256 of a file equals sha256 of its bytes, however large`() {
        val bytes = ByteArray(300_000) { (it * 31).toByte() }
        val file = temp.newFile("big.bin").also { it.writeBytes(bytes) }
        assertEquals(Sha256.ofBytes(bytes), Sha256.ofFile(file))
    }

    @Test
    fun `a hash matches only itself, in any case, and only when complete`() {
        val hash = Sha256.ofBytes("abc".toByteArray())
        assertTrue(Sha256.matches(hash, hash))
        assertTrue(Sha256.matches(hash, hash.uppercase()))
        assertFalse(Sha256.matches(hash, Sha256.ofBytes("abd".toByteArray())))
        assertFalse(Sha256.matches(hash, hash.dropLast(1)))
        assertFalse(Sha256.matches(hash, ""))
        // One flipped digit.
        assertFalse(Sha256.matches(hash, (if (hash[0] == '0') "1" else "0") + hash.drop(1)))
    }

    // ------------------------------------------------------------------ the signing certificate

    private val ours = SignerSet.fromCertificates(listOf("our certificate".toByteArray()))
    private val theirs = SignerSet.fromCertificates(listOf("someone else".toByteArray()))

    @Test
    fun `the same certificate is accepted`() {
        assertTrue(SignerCheck.sameSigner(ours, SignerSet.fromCertificates(listOf("our certificate".toByteArray()))))
    }

    @Test
    fun `a different certificate is refused`() {
        assertFalse(SignerCheck.sameSigner(ours, theirs))
        assertFalse(SignerCheck.sameSigner(theirs, ours))
    }

    @Test
    fun `no certificate at all is refused`() {
        val none = SignerSet(emptySet())
        assertFalse(SignerCheck.sameSigner(ours, none))
        assertFalse(SignerCheck.sameSigner(none, ours))
        assertFalse(SignerCheck.sameSigner(none, none))
    }

    @Test
    fun `an extra or missing signer is refused`() {
        val both = SignerSet.fromCertificates(listOf("our certificate".toByteArray(), "someone else".toByteArray()))
        assertFalse(SignerCheck.sameSigner(ours, both))
        assertFalse(SignerCheck.sameSigner(both, ours))
        assertTrue(SignerCheck.sameSigner(both, SignerSet.fromCertificates(listOf("someone else".toByteArray(), "our certificate".toByteArray()))))
    }

    @Test
    fun `a rotated key is accepted only when its lineage contains the installed certificate`() {
        val rotated = SignerSet.fromCertificates(
            current = listOf("new key".toByteArray()),
            history = listOf("our certificate".toByteArray(), "new key".toByteArray()),
        )
        assertTrue(SignerCheck.sameSigner(ours, rotated))

        val unrelated = SignerSet.fromCertificates(
            current = listOf("new key".toByteArray()),
            history = listOf("someone else".toByteArray(), "new key".toByteArray()),
        )
        assertFalse(SignerCheck.sameSigner(ours, unrelated))
    }

    @Test
    fun `fingerprints are the sha256 of the certificate bytes`() {
        assertEquals(setOf(Sha256.ofBytes("our certificate".toByteArray())), ours.current)
    }

    // ------------------------------------------------------------------ what the APK says it is

    private val installed = ApkFacts("io.dlne.chat", 5, ours)
    private fun downloaded(pkg: String = "io.dlne.chat", code: Long = 6, signers: SignerSet = ours) = ApkFacts(pkg, code, signers)

    @Test
    fun `the right app, the right version, the same signer passes`() {
        assertNull(ApkCheck.problem(downloaded(), installed, expectedVersionCode = 6))
    }

    @Test
    fun `each wrong thing is named`() {
        assertEquals(ApkProblem.WRONG_PACKAGE, ApkCheck.problem(downloaded(pkg = "com.evil"), installed, 6))
        assertEquals(ApkProblem.WRONG_SIGNER, ApkCheck.problem(downloaded(signers = theirs), installed, 6))
        assertEquals(ApkProblem.WRONG_VERSION, ApkCheck.problem(downloaded(code = 7), installed, 6))
        assertEquals(ApkProblem.NOT_NEWER, ApkCheck.problem(downloaded(code = 5), installed, 5))
        assertEquals(ApkProblem.NOT_NEWER, ApkCheck.problem(downloaded(code = 4), installed, 4))
    }

    // ------------------------------------------------------------------ the installer's answer

    @Test
    fun `installer status codes are told apart`() {
        assertEquals(InstallOutcome.SUCCESS, InstallStatus.classify(PackageInstaller.STATUS_SUCCESS))
        assertEquals(InstallOutcome.NEEDS_CONFIRMATION, InstallStatus.classify(PackageInstaller.STATUS_PENDING_USER_ACTION))
        assertEquals(InstallOutcome.CANCELLED, InstallStatus.classify(PackageInstaller.STATUS_FAILURE_ABORTED))
        assertEquals(InstallOutcome.STORAGE, InstallStatus.classify(PackageInstaller.STATUS_FAILURE_STORAGE))
        assertEquals(InstallOutcome.INCOMPATIBLE, InstallStatus.classify(PackageInstaller.STATUS_FAILURE_CONFLICT))
        assertEquals(InstallOutcome.INCOMPATIBLE, InstallStatus.classify(PackageInstaller.STATUS_FAILURE_INCOMPATIBLE))
        assertEquals(InstallOutcome.INCOMPATIBLE, InstallStatus.classify(PackageInstaller.STATUS_FAILURE_INVALID))
        assertEquals(InstallOutcome.BLOCKED, InstallStatus.classify(PackageInstaller.STATUS_FAILURE_BLOCKED))
        assertEquals(InstallOutcome.FAILED, InstallStatus.classify(PackageInstaller.STATUS_FAILURE))
        assertEquals(InstallOutcome.FAILED, InstallStatus.classify(12345))
    }
}
