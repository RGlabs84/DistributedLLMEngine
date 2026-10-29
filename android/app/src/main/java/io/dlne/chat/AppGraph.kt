package io.dlne.chat

import android.content.Context
import io.dlne.chat.data.ConversationStore
import io.dlne.chat.data.DeviceLocation
import io.dlne.chat.data.ImageStore
import io.dlne.chat.data.PhotoFiles
import io.dlne.chat.data.SettingsStore
import io.dlne.chat.net.DlneApi
import java.io.File
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

/**
 * The app's long-lived objects, created once per process. A share from another app can
 * open a second window of DLNE; both windows share these, so they see the same history.
 */
class AppGraph private constructor(private val context: Context) {
    /** Outlives any one screen, so a history write finishes even if the window closes. */
    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    val api = DlneApi()
    val settings = SettingsStore(context)
    val location = DeviceLocation(context)
    val images = ImageStore(File(context.filesDir, "images"))
    val conversations = ConversationStore(File(context.filesDir, "conversations.json"), appScope)

    private val storeSwept = AtomicBoolean(false)

    /**
     * Housekeeping when a window is created, off the main thread. [freshLaunch] is true when the app is
     * opened afresh and false when Android restores it (a camera answer comes back with the restored
     * state, so a camera shot may still be on its way then); the camera's cache folder is only cleared
     * on a fresh launch. Once per process, the stored photos and pictures that no chat refers to are
     * removed, after the history has been read.
     */
    fun tidyUp(freshLaunch: Boolean) {
        appScope.launch(Dispatchers.IO) {
            if (freshLaunch) PhotoFiles.sweepCameraDir(context.cacheDir)
            if (storeSwept.compareAndSet(false, true)) {
                val complete = conversations.awaitLoaded()
                images.sweep(conversations.conversations.value, complete)
            }
        }
    }

    companion object {
        @Volatile
        private var instance: AppGraph? = null

        fun get(context: Context): AppGraph =
            instance ?: synchronized(this) {
                instance ?: AppGraph(context.applicationContext).also { instance = it }
            }
    }
}
