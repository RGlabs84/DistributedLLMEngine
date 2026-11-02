package io.dlne.chat.ui

import androidx.activity.compose.BackHandler
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.platform.LocalSoftwareKeyboardController
import androidx.lifecycle.compose.LifecycleStartEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import io.dlne.chat.update.UpdateHost
import kotlinx.coroutines.launch

/** The whole app: the chat (with its conversation drawer) or Settings, plus the first-run notice. */
@Composable
fun DlneApp(viewModel: ChatViewModel) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()

    // The status line is refreshed only while the app is on screen: each start (also coming back from
    // another app) fetches at once, so the header shows the live model, then every minute.
    LifecycleStartEffect(viewModel) {
        viewModel.startStatusUpdates()
        viewModel.onForeground() // image jobs are polled only while the app is shown
        onStopOrDispose {
            viewModel.stopStatusUpdates()
            viewModel.onBackground()
        }
    }

    when (viewModel.screen) {
        Screen.Settings -> {
            BackHandler(onBack = viewModel::closeSettings)
            SettingsScreen(
                state = state,
                actions = remember(viewModel) {
                    SettingsActions(
                        onBack = viewModel::closeSettings,
                        onSaveServerUrl = viewModel::setServerUrl,
                        onResetServerUrl = viewModel::resetServerUrl,
                        onDeleteAllChats = viewModel::deleteAllConversations,
                        onShareLocationChange = viewModel::setShareLocation,
                        locationPermitted = viewModel::locationPermitted,
                        onVoiceModeChange = viewModel::setVoiceMode,
                    )
                },
            )
        }

        Screen.Chat -> ChatWithDrawer(state, viewModel)
    }

    // Maintenance (server 2.8.5): one full-screen card over everything until the status says it is over.
    state.maintenance?.let { MaintenanceCard(it) }

    if (!state.privacyAcknowledged) {
        PrivacyNoticeDialog(onAcknowledge = viewModel::acknowledgePrivacy)
    }

    // Sideload build: checks for updates and shows the install prompt. Play build: nothing (src/play).
    UpdateHost()
}

@Composable
private fun ChatWithDrawer(state: ChatUiState, viewModel: ChatViewModel) {
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    fun closeDrawer() {
        scope.launch { drawerState.close() }
    }
    BackHandler(enabled = drawerState.isOpen) { closeDrawer() }
    // The keyboard goes away as the menu starts to open (its button or a swipe): it would only cover the chats
    // (owner, 1.1.1).
    val keyboard = LocalSoftwareKeyboardController.current
    val focus = LocalFocusManager.current
    LaunchedEffect(drawerState.targetValue) {
        if (drawerState.targetValue == DrawerValue.Open) {
            keyboard?.hide()
            focus.clearFocus()
        }
    }

    val actions = remember(viewModel) {
        ChatActions(
            onOpenDrawer = { scope.launch { drawerState.open() } },
            onNewChat = viewModel::newChat,
            onDraftChange = viewModel::updateDraft,
            onSend = viewModel::send,
            onStop = viewModel::stop,
            onRetry = viewModel::retry,
            onDismissError = viewModel::dismissError,
            onReport = viewModel::openReport,
            onSendReport = viewModel::sendReport,
            onCloseReport = viewModel::closeReport,
            onReportThanksShown = viewModel::reportThanksShown,
            onCreateImage = viewModel::createImage,
            onCancelImage = viewModel::cancelImage,
            onAnswerLocationAsk = viewModel::answerLocationAsk,
            locationPermitted = viewModel::locationPermitted,
            onAddPhotos = viewModel::addPhotos,
            onRemovePhoto = viewModel::removePhoto,
            onVoiceText = viewModel::sendByVoice,
            onStopSpeaking = viewModel::stopSpeaking,
            onSpeechNoteShown = viewModel::speechNoteShown,
        )
    }

    ModalNavigationDrawer(
        drawerState = drawerState,
        drawerContent = {
            ConversationDrawer(
                conversations = state.conversations,
                currentId = state.current?.id,
                onNewChat = {
                    viewModel.newChat()
                    closeDrawer()
                },
                onOpen = { id ->
                    viewModel.openConversation(id)
                    closeDrawer()
                },
                onDelete = viewModel::deleteConversation,
                onSettings = {
                    viewModel.showSettings()
                    closeDrawer()
                },
            )
        },
    ) {
        ChatScreen(
            state = state,
            draft = viewModel.draft,
            photos = viewModel.photoDraft,
            report = viewModel.report,
            reportThanksPending = viewModel.reportThanksPending,
            actions = actions,
        )
    }
}
