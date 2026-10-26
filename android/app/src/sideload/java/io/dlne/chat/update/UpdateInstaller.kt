package io.dlne.chat.update

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.content.IntentCompat
import androidx.core.content.pm.PackageInfoCompat
import java.io.File
import java.io.IOException

/** How the system installer's status codes are told to the person. */
internal enum class InstallOutcome { SUCCESS, NEEDS_CONFIRMATION, CANCELLED, STORAGE, INCOMPATIBLE, BLOCKED, FAILED }

internal object InstallStatus {
    fun classify(status: Int): InstallOutcome = when (status) {
        PackageInstaller.STATUS_SUCCESS -> InstallOutcome.SUCCESS
        PackageInstaller.STATUS_PENDING_USER_ACTION -> InstallOutcome.NEEDS_CONFIRMATION
        PackageInstaller.STATUS_FAILURE_ABORTED -> InstallOutcome.CANCELLED
        PackageInstaller.STATUS_FAILURE_STORAGE -> InstallOutcome.STORAGE
        PackageInstaller.STATUS_FAILURE_CONFLICT,
        PackageInstaller.STATUS_FAILURE_INCOMPATIBLE,
        PackageInstaller.STATUS_FAILURE_INVALID,
        -> InstallOutcome.INCOMPATIBLE
        PackageInstaller.STATUS_FAILURE_BLOCKED -> InstallOutcome.BLOCKED
        else -> InstallOutcome.FAILED
    }
}

/**
 * Hands a verified APK to Android's PackageInstaller (session API). Nothing silent or
 * privileged: the system always shows its own "Do you want to update this app?" prompt, and
 * the person taps Install.
 */
internal object UpdateInstaller {
    private const val ACTION = "io.dlne.chat.update.INSTALL_STATUS"
    private const val SESSION_FILE = "dlne-update.apk"

    /** Drops install sessions this app left open (for example when the process died mid-update). */
    fun abandonOldSessions(context: Context) {
        val installer = context.packageManager.packageInstaller
        try {
            installer.mySessions.forEach { installer.abandonSession(it.sessionId) }
        } catch (e: RuntimeException) {
            // Best effort: a stale session only wastes a little storage.
        }
    }

    /**
     * Streams [file] into a new install session and commits it. The status (confirmation
     * needed, done, failed) arrives at [UpdateInstallReceiver]. The bytes are hashed once more
     * on the way in and the session is dropped if they are not the verified ones.
     * @throws IOException if the file cannot be read or changed since it was verified.
     */
    fun install(context: Context, file: File, expectedSha256: String) {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL).apply {
            setAppPackageName(context.packageName)
            setSize(file.length())
        }
        val sessionId = installer.createSession(params)
        try {
            installer.openSession(sessionId).use { session ->
                val digest = Sha256.newDigest()
                file.inputStream().use { input ->
                    session.openWrite(SESSION_FILE, 0, file.length()).use { out ->
                        val buffer = ByteArray(64 * 1024)
                        while (true) {
                            val n = input.read(buffer)
                            if (n < 0) break
                            digest.update(buffer, 0, n)
                            out.write(buffer, 0, n)
                        }
                        session.fsync(out)
                    }
                }
                if (!Sha256.matches(Sha256.hex(digest.digest()), expectedSha256)) {
                    throw IOException("The file changed after it was checked")
                }
                session.commit(statusIntentSender(context, sessionId))
            }
        } catch (e: Exception) {
            runCatching { installer.abandonSession(sessionId) }
            throw e
        }
    }

    private fun statusIntentSender(context: Context, sessionId: Int): android.content.IntentSender {
        // Explicit, internal-only target. The installer adds its result extras to this intent,
        // so the PendingIntent must be mutable on Android 12+.
        val intent = Intent(ACTION).setClass(context, UpdateInstallReceiver::class.java)
        val mutable = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
        return PendingIntent.getBroadcast(context, sessionId, intent, PendingIntent.FLAG_UPDATE_CURRENT or mutable)
            .intentSender
    }
}

/** Receives the installer's status and passes it to the updater. Declared in src/sideload/AndroidManifest.xml, not exported. */
class UpdateInstallReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        val message = intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)
        val confirm = IntentCompat.getParcelableExtra(intent, Intent.EXTRA_INTENT, Intent::class.java)
        UpdateController.get(context).onInstallStatus(status, message, confirm)
    }
}

/** Reads package facts (name, version, signing certificates) from the installed app or a downloaded APK. */
internal object ApkInspector {
    @Suppress("DEPRECATION")
    private val flags: Int =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) PackageManager.GET_SIGNING_CERTIFICATES
        else PackageManager.GET_SIGNATURES

    @Suppress("DEPRECATION")
    fun installed(context: Context): ApkFacts? =
        try {
            factsOf(context.packageManager.getPackageInfo(context.packageName, flags))
        } catch (e: PackageManager.NameNotFoundException) {
            null
        }

    @Suppress("DEPRECATION")
    fun archive(context: Context, file: File): ApkFacts? =
        context.packageManager.getPackageArchiveInfo(file.absolutePath, flags)?.let(::factsOf)

    @Suppress("DEPRECATION")
    private fun factsOf(info: PackageInfo): ApkFacts? {
        val signers = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            val signing = info.signingInfo ?: return null
            val current = signing.apkContentsSigners.map { it.toByteArray() }
            val history = if (signing.hasMultipleSigners()) current
            else signing.signingCertificateHistory?.map { it.toByteArray() } ?: current
            SignerSet.fromCertificates(current, history)
        } else {
            SignerSet.fromCertificates((info.signatures ?: return null).map { it.toByteArray() })
        }
        return ApkFacts(info.packageName, PackageInfoCompat.getLongVersionCode(info), signers)
    }
}
