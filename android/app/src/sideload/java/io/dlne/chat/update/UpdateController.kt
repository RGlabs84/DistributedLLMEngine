package io.dlne.chat.update

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import io.dlne.chat.BuildConfig
import java.io.File
import java.io.IOException
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrl

/** What the updater is doing right now. */
internal enum class Phase { IDLE, CHECKING, NEEDS_PERMISSION, DOWNLOADING, VERIFYING, INSTALLING }

/** The outcome of the last check, shown in Settings after "Check for updates". */
internal enum class CheckNote { UP_TO_DATE, NOT_FOR_THIS_PHONE, CHECK_FAILED, BAD_MANIFEST }

/** Why an update was not installed. The current app is always left as it was. */
internal enum class Problem {
    DOWNLOAD_FAILED,
    DOES_NOT_MATCH,
    WRONG_SIGNER,
    WRONG_PACKAGE,
    UNREADABLE,
    PERMISSION_DENIED,
    INSTALL_CANCELLED,
    NO_SPACE,
    INCOMPATIBLE,
    BLOCKED,
    INSTALL_FAILED,
}

/** Everything the update screens draw. */
internal data class UpdateUi(
    val autoCheck: Boolean = true,
    /** A valid, newer update this phone can take, or null. */
    val update: UpdateInfo? = null,
    /** "Later" was tapped for [update]: the chat banner stays away until the next check. */
    val hidden: Boolean = false,
    val phase: Phase = Phase.IDLE,
    /** 0..1 while downloading, when the size is known. */
    val progress: Float? = null,
    val checkNote: CheckNote? = null,
    val problem: Problem? = null,
    /** The installer's own words for [Problem.INSTALL_FAILED], if it gave any. */
    val problemDetail: String? = null,
) {
    val busy: Boolean get() = phase != Phase.IDLE
}

/**
 * The in-app updater (sideload build only). One per process; the UI observes [ui].
 *
 * Flow: check the official manifest, and when a newer version exists offer it. On Update:
 * make sure Android lets this app install packages, download the APK to the cache directory,
 * check its size and SHA-256 (while downloading) and that it is this app, newer, and signed by
 * the same certificate, then stream it into a PackageInstaller session. Android then asks the
 * person to confirm; the confirmation screen is started from a visible activity. Nothing that
 * fails a check is installed, and the downloaded file never outlives the attempt.
 */
internal class UpdateController private constructor(context: Context) {
    private val app = context.applicationContext
    private val store = UpdateStore(app)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main.immediate)
    private val fetcher = ManifestFetcher()
    private val downloader = ApkDownloader()
    private val cacheDir = File(app.cacheDir, "updates")
    private var job: Job? = null
    private var cleanedUp = false

    private val _ui = MutableStateFlow(initialUi())
    val ui: StateFlow<UpdateUi> = _ui.asStateFlow()

    private val _confirmation = MutableStateFlow<Intent?>(null)

    /** The installer's confirmation screen, waiting to be shown by a visible activity. */
    val confirmation: StateFlow<Intent?> = _confirmation.asStateFlow()

    private fun initialUi(): UpdateUi {
        val remembered = store.rememberedUpdate(BuildConfig.VERSION_CODE, Build.VERSION.SDK_INT)
        return UpdateUi(
            autoCheck = store.autoCheck,
            update = remembered,
            hidden = remembered != null && UpdateSchedule.isHidden(remembered, store.dismissedVersionCode),
        )
    }

    // ------------------------------------------------------------------ checking

    /** The app came to the screen: tidy up leftovers once, then check if automatic checks are on and due. */
    fun onAppStart() {
        if (!cleanedUp) {
            cleanedUp = true
            if (!_ui.value.busy) cleanUp()
        }
        if (store.autoCheck && UpdateSchedule.isDue(store.lastCheckMillis, System.currentTimeMillis())) check(manual = false)
    }

    fun checkNow() = check(manual = true)

    private fun check(manual: Boolean) {
        if (_ui.value.busy) return
        _ui.update { it.copy(phase = Phase.CHECKING, checkNote = null, problem = null, problemDetail = null) }
        job = scope.launch {
            val fetched = withContext(Dispatchers.IO) { fetcher.fetch(UpdateSource.MANIFEST_URL.toHttpUrl()) }
            val text = (fetched as? FetchResult.Text)?.body
            if (text == null) {
                finishCheck(if (manual) CheckNote.CHECK_FAILED else null)
                return@launch
            }
            val info = (UpdateManifestParser.parse(text) as? ManifestResult.Valid)?.info
            if (info == null) {
                finishCheck(if (manual) CheckNote.BAD_MANIFEST else null)
                return@launch
            }
            val now = System.currentTimeMillis()
            when (val decision = decideUpdate(info, BuildConfig.VERSION_CODE, Build.VERSION.SDK_INT)) {
                is UpdateDecision.Available -> {
                    store.recordCheck(now, text)
                    _ui.update { it.copy(phase = Phase.IDLE, update = decision.info, hidden = false) }
                }
                is UpdateDecision.NeedsNewerAndroid -> {
                    store.recordCheck(now, null)
                    _ui.update { it.copy(phase = Phase.IDLE, update = null, hidden = false, checkNote = if (manual) CheckNote.NOT_FOR_THIS_PHONE else null) }
                }
                UpdateDecision.UpToDate -> {
                    store.recordCheck(now, null)
                    _ui.update { it.copy(phase = Phase.IDLE, update = null, hidden = false, checkNote = if (manual) CheckNote.UP_TO_DATE else null) }
                }
            }
        }
    }

    private fun finishCheck(note: CheckNote?) {
        _ui.update { it.copy(phase = Phase.IDLE, checkNote = note) }
    }

    fun setAutoCheck(enabled: Boolean) {
        store.autoCheck = enabled
        _ui.update { it.copy(autoCheck = enabled) }
    }

    /** "Later": hide the banner for this version until the next check. */
    fun later() {
        val info = _ui.value.update ?: return
        store.dismissedVersionCode = info.versionCode
        _ui.update { it.copy(hidden = true) }
    }

    fun dismissProblem() {
        _ui.update { it.copy(problem = null, problemDetail = null) }
    }

    // ------------------------------------------------------------------ updating

    /** The person tapped Update. [context] is the visible activity (it opens Android's settings page if needed). */
    fun startUpdate(context: Context) {
        val info = _ui.value.update ?: return
        if (_ui.value.busy) return
        _ui.update { it.copy(problem = null, problemDetail = null, checkNote = null) }
        if (!app.packageManager.canRequestPackageInstalls()) {
            // Android keeps "install unknown apps" off until the person allows it for this app.
            _ui.update { it.copy(phase = Phase.NEEDS_PERMISSION) }
            val settings = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${app.packageName}"))
            try {
                context.startActivity(settings)
            } catch (e: ActivityNotFoundException) {
                fail(Problem.PERMISSION_DENIED)
            }
            return
        }
        download(info)
    }

    /** The app is visible again: carry on if the person just allowed installs. */
    fun onResume() {
        if (_ui.value.phase != Phase.NEEDS_PERMISSION) return
        val info = _ui.value.update
        when {
            info == null -> _ui.update { it.copy(phase = Phase.IDLE) }
            app.packageManager.canRequestPackageInstalls() -> download(info)
            else -> fail(Problem.PERMISSION_DENIED)
        }
    }

    /** Stops a download in progress. */
    fun cancel() {
        if (_ui.value.phase != Phase.DOWNLOADING && _ui.value.phase != Phase.NEEDS_PERMISSION) return
        job?.cancel()
        cleanUpFiles()
        _ui.update { it.copy(phase = Phase.IDLE, progress = null) }
    }

    private fun download(info: UpdateInfo) {
        _ui.update { it.copy(phase = Phase.DOWNLOADING, progress = 0f) }
        job = scope.launch {
            val file = File(cacheDir, "dlne-${info.versionCode}.apk")
            var lastPercent = -1
            val result = withContext(Dispatchers.IO) {
                downloader.download(
                    url = info.url.toHttpUrl(),
                    expectedSize = info.size,
                    expectedSha256 = info.sha256,
                    destination = file,
                    isCancelled = { !isActive },
                ) { bytes ->
                    val percent = (bytes * 100 / info.size).toInt()
                    if (percent != lastPercent) {
                        lastPercent = percent
                        _ui.update { it.copy(progress = bytes.toFloat() / info.size) }
                    }
                }
            }
            when (result) {
                is DownloadResult.Failed -> {
                    cleanUpFiles()
                    fail(
                        when (result.problem) {
                            DownloadProblem.SIZE_MISMATCH, DownloadProblem.HASH_MISMATCH -> Problem.DOES_NOT_MATCH
                            else -> Problem.DOWNLOAD_FAILED
                        },
                    )
                }
                is DownloadResult.Done -> verifyAndInstall(info, result.file)
            }
        }
    }

    private suspend fun verifyAndInstall(info: UpdateInfo, file: File) {
        _ui.update { it.copy(phase = Phase.VERIFYING, progress = null) }
        val problem = withContext(Dispatchers.IO) {
            val downloaded = ApkInspector.archive(app, file)
            val installed = ApkInspector.installed(app)
            if (downloaded == null || installed == null) {
                Problem.UNREADABLE
            } else {
                when (ApkCheck.problem(downloaded, installed, info.versionCode)) {
                    null -> null
                    ApkProblem.WRONG_SIGNER -> Problem.WRONG_SIGNER
                    ApkProblem.WRONG_PACKAGE -> Problem.WRONG_PACKAGE
                    ApkProblem.NOT_NEWER, ApkProblem.WRONG_VERSION -> Problem.DOES_NOT_MATCH
                }
            }
        }
        if (problem != null) {
            cleanUpFiles()
            fail(problem)
            return
        }
        _ui.update { it.copy(phase = Phase.INSTALLING) }
        try {
            withContext(Dispatchers.IO) { UpdateInstaller.install(app, file, info.sha256) }
        } catch (e: CancellationException) {
            throw e
        } catch (e: IOException) {
            fail(Problem.INSTALL_FAILED, e.message)
        } catch (e: SecurityException) {
            fail(Problem.PERMISSION_DENIED)
        } catch (e: RuntimeException) {
            fail(Problem.INSTALL_FAILED, e.message)
        } finally {
            // The installer has its own copy of the bytes now (or the attempt failed): the file is not needed.
            cleanUpFiles()
        }
    }

    /** The installer reports progress here (from [UpdateInstallReceiver]). */
    fun onInstallStatus(status: Int, message: String?, confirm: Intent?) {
        when (InstallStatus.classify(status)) {
            InstallOutcome.NEEDS_CONFIRMATION -> {
                if (confirm != null) _confirmation.value = confirm
                _ui.update { it.copy(phase = Phase.INSTALLING) }
            }
            InstallOutcome.SUCCESS -> {
                // Normally the system replaces the app and ends this process right here.
                _confirmation.value = null
                store.foundManifest = null
                _ui.update { it.copy(phase = Phase.IDLE, update = null) }
            }
            InstallOutcome.CANCELLED -> fail(Problem.INSTALL_CANCELLED)
            InstallOutcome.STORAGE -> fail(Problem.NO_SPACE)
            InstallOutcome.INCOMPATIBLE -> fail(Problem.INCOMPATIBLE, message)
            InstallOutcome.BLOCKED -> fail(Problem.BLOCKED, message)
            InstallOutcome.FAILED -> fail(Problem.INSTALL_FAILED, message)
        }
    }

    /** Called by the visible activity before it shows [intent]; true if this caller should show it. */
    fun claimConfirmation(intent: Intent): Boolean {
        if (_confirmation.value !== intent) return false
        _confirmation.value = null
        return true
    }

    private fun fail(problem: Problem, detail: String? = null) {
        _confirmation.value = null
        _ui.update { it.copy(phase = Phase.IDLE, progress = null, problem = problem, problemDetail = detail?.take(200)) }
    }

    // ------------------------------------------------------------------ files

    private fun cleanUp() {
        cleanUpFiles()
        UpdateInstaller.abandonOldSessions(app)
    }

    private fun cleanUpFiles() {
        cacheDir.listFiles()?.forEach { it.delete() }
    }

    companion object {
        @Volatile
        private var instance: UpdateController? = null

        fun get(context: Context): UpdateController =
            instance ?: synchronized(this) {
                instance ?: UpdateController(context).also { instance = it }
            }
    }
}
