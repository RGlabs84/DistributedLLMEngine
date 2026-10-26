package io.dlne.chat

import android.content.Context
import io.dlne.chat.data.ConversationStore
import io.dlne.chat.data.SettingsStore
import io.dlne.chat.net.DlneApi
import java.io.File
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob

/**
 * The app's long-lived objects, created once per process. A share from another app can
 * open a second window of DLNE; both windows share these, so they see the same history.
 */
class AppGraph private constructor(context: Context) {
    /** Outlives any one screen, so a history write finishes even if the window closes. */
    private val appScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    val api = DlneApi()
    val settings = SettingsStore(context)
    val conversations = ConversationStore(File(context.filesDir, "conversations.json"), appScope)

    companion object {
        @Volatile
        private var instance: AppGraph? = null

        fun get(context: Context): AppGraph =
            instance ?: synchronized(this) {
                instance ?: AppGraph(context.applicationContext).also { instance = it }
            }
    }
}
