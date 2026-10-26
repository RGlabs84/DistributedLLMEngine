package io.dlne.chat.ui

import androidx.activity.compose.BackHandler
import androidx.compose.material3.DrawerValue
import androidx.compose.material3.ModalNavigationDrawer
import androidx.compose.material3.rememberDrawerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.lifecycle.compose.LifecycleStartEffect
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import kotlinx.coroutines.launch

/** The whole app: the chat (with its conversation drawer) or Settings, plus the first-run notice. */
@Composable
fun DlneApp(viewModel: ChatViewModel) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()

    // The status line is refreshed only while the app is on screen.
    LifecycleStartEffect(viewModel) {
        viewModel.startStatusUpdates()
        onStopOrDispose { viewModel.stopStatusUpdates() }
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
                    )
                },
            )
        }

        Screen.Chat -> ChatWithDrawer(state, viewModel)
    }

    if (!state.privacyAcknowledged) {
        PrivacyNoticeDialog(onAcknowledge = viewModel::acknowledgePrivacy)
    }
}

@Composable
private fun ChatWithDrawer(state: ChatUiState, viewModel: ChatViewModel) {
    val drawerState = rememberDrawerState(DrawerValue.Closed)
    val scope = rememberCoroutineScope()
    fun closeDrawer() {
        scope.launch { drawerState.close() }
    }
    BackHandler(enabled = drawerState.isOpen) { closeDrawer() }

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
            report = viewModel.report,
            reportThanksPending = viewModel.reportThanksPending,
            actions = actions,
        )
    }
}
