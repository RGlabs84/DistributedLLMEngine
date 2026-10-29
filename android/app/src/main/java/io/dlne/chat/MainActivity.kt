package io.dlne.chat

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.viewModels
import io.dlne.chat.ui.ChatViewModel
import io.dlne.chat.ui.DlneApp
import io.dlne.chat.ui.theme.DlneTheme

/** The app's single activity. Draws edge to edge; Compose handles every inset. */
class MainActivity : ComponentActivity() {
    private val viewModel: ChatViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        // Only on a fresh start: after rotation the shared text is already in the draft.
        if (savedInstanceState == null) handleShare(intent)
        // Clears leftovers of earlier runs (a camera shot nobody collected, photos no chat refers to).
        AppGraph.get(this).tidyUp(freshLaunch = savedInstanceState == null)
        setContent {
            DlneTheme {
                DlneApp(viewModel)
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleShare(intent)
    }

    /** Text shared from another app (ACTION_SEND, text/plain) starts a new chat with it typed in. */
    private fun handleShare(intent: Intent?) {
        if (intent?.action != Intent.ACTION_SEND || intent.type?.startsWith("text/") != true) return
        val text = intent.getCharSequenceExtra(Intent.EXTRA_TEXT)?.toString()
            ?: intent.getStringExtra(Intent.EXTRA_SUBJECT)
            ?: return
        if (text.isNotBlank()) viewModel.startChatWithSharedText(text.trim())
    }
}
