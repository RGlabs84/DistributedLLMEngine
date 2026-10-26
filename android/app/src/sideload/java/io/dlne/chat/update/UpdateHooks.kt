package io.dlne.chat.update

import android.content.ActivityNotFoundException
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleResumeEffect
import androidx.lifecycle.compose.LifecycleStartEffect
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import io.dlne.chat.R
import io.dlne.chat.ui.BodyText
import io.dlne.chat.ui.SectionDivider
import io.dlne.chat.ui.SectionTitle
import kotlinx.coroutines.flow.first

/*
 * The sideload build's side of the three hooks the shared screens call (the Play build has
 * empty versions in src/play): UpdateHost in DlneApp, UpdateBanner in the chat's top bar,
 * UpdateSettingsSection in Settings.
 */

/** Checks for updates when the app comes to the screen, and shows Android's install prompt when it asks for one. */
@Composable
fun UpdateHost() {
    val context = LocalContext.current
    val controller = remember { UpdateController.get(context) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle

    LifecycleStartEffect(controller) {
        controller.onAppStart()
        onStopOrDispose { }
    }
    // Back from Android's "install unknown apps" page: carry on with the update.
    LifecycleResumeEffect(controller) {
        controller.onResume()
        onPauseOrDispose { }
    }

    // The installer's confirmation screen can only be started by a visible activity, so it
    // waits here until the app is on screen.
    val confirmation by controller.confirmation.collectAsStateWithLifecycle()
    LaunchedEffect(confirmation) {
        val intent = confirmation ?: return@LaunchedEffect
        lifecycle.currentStateFlow.first { it.isAtLeast(Lifecycle.State.RESUMED) }
        if (controller.claimConfirmation(intent)) {
            try {
                context.startActivity(intent)
            } catch (e: ActivityNotFoundException) {
                controller.onInstallStatus(android.content.pm.PackageInstaller.STATUS_FAILURE, null, null)
            }
        }
    }
}

/** "DLNE Chat 1.0.5 is available. ... [Update] [Later]", at the top of the chat. Shows nothing when there is no news. */
@Composable
fun UpdateBanner() {
    val controller = UpdateController.get(LocalContext.current)
    val ui by controller.ui.collectAsStateWithLifecycle()
    val visible = ui.problem != null || (ui.update != null && (!ui.hidden || ui.busy))
    if (!visible) return
    Surface(
        color = MaterialTheme.colorScheme.secondaryContainer,
        contentColor = MaterialTheme.colorScheme.onSecondaryContainer,
        shape = RoundedCornerShape(16.dp),
        modifier = Modifier.fillMaxWidth().padding(horizontal = 12.dp, vertical = 4.dp),
    ) {
        UpdateCard(ui, controller, allowLater = true, modifier = Modifier.padding(12.dp))
    }
}

/** The Updates section of Settings: the switch, "Check for updates", and the update itself when there is one. */
@Composable
fun UpdateSettingsSection() {
    val controller = UpdateController.get(LocalContext.current)
    val ui by controller.ui.collectAsStateWithLifecycle()

    SectionDivider()
    SectionTitle(stringResource(R.string.update_settings_title))
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier.fillMaxWidth().padding(horizontal = 24.dp),
    ) {
        Column(Modifier.weight(1f).padding(end = 12.dp)) {
            Text(stringResource(R.string.update_auto), style = MaterialTheme.typography.bodyLarge)
            BodyText(stringResource(R.string.update_auto_sub))
        }
        Switch(checked = ui.autoCheck, onCheckedChange = controller::setAutoCheck)
    }
    Column(Modifier.padding(horizontal = 24.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedButton(onClick = controller::checkNow, enabled = !ui.busy) {
            Text(stringResource(R.string.update_check_now))
        }
        val status = when {
            ui.phase == Phase.CHECKING -> stringResource(R.string.update_checking)
            else -> when (ui.checkNote) {
                CheckNote.UP_TO_DATE -> stringResource(R.string.update_up_to_date)
                CheckNote.NOT_FOR_THIS_PHONE -> stringResource(R.string.update_not_for_this_phone)
                CheckNote.CHECK_FAILED -> stringResource(R.string.update_check_failed)
                CheckNote.BAD_MANIFEST -> stringResource(R.string.update_bad_manifest)
                null -> null
            }
        }
        status?.let { BodyText(it) }
        if (ui.update != null || ui.problem != null) {
            Surface(
                color = MaterialTheme.colorScheme.secondaryContainer,
                contentColor = MaterialTheme.colorScheme.onSecondaryContainer,
                shape = RoundedCornerShape(16.dp),
                modifier = Modifier.fillMaxWidth(),
            ) {
                // Settings keeps the update on offer after "Later": that only hides the chat banner.
                UpdateCard(ui, controller, allowLater = false, modifier = Modifier.padding(12.dp))
            }
        }
    }
}

@Composable
private fun UpdateCard(ui: UpdateUi, controller: UpdateController, allowLater: Boolean, modifier: Modifier = Modifier) {
    val context = LocalContext.current
    val info = ui.update
    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        ui.problem?.let { problem ->
            Text(problemText(problem, ui.problemDetail), style = MaterialTheme.typography.bodyMedium)
        }
        if (info != null) {
            when (ui.phase) {
                Phase.DOWNLOADING -> {
                    val percent = ((ui.progress ?: 0f) * 100).toInt()
                    Text(stringResource(R.string.update_downloading, info.versionName, percent), style = MaterialTheme.typography.bodyMedium)
                    LinearProgressIndicator(progress = { ui.progress ?: 0f }, modifier = Modifier.fillMaxWidth())
                }
                Phase.VERIFYING -> {
                    Text(stringResource(R.string.update_verifying), style = MaterialTheme.typography.bodyMedium)
                    LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                }
                Phase.INSTALLING -> Text(stringResource(R.string.update_installing), style = MaterialTheme.typography.bodyMedium)
                Phase.NEEDS_PERMISSION -> Text(stringResource(R.string.update_needs_permission), style = MaterialTheme.typography.bodyMedium)
                else -> {
                    Text(stringResource(R.string.update_available, info.versionName), style = MaterialTheme.typography.titleSmall)
                    if (info.notes.isNotEmpty()) Text(info.notes, style = MaterialTheme.typography.bodyMedium)
                    Text(
                        stringResource(R.string.update_one_tap),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSecondaryContainer.copy(alpha = 0.8f),
                    )
                }
            }
        }
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End), modifier = Modifier.fillMaxWidth()) {
            if (ui.problem != null) {
                TextButton(onClick = controller::dismissProblem) { Text(stringResource(R.string.update_dismiss)) }
            }
            if (ui.phase == Phase.DOWNLOADING) {
                TextButton(onClick = controller::cancel) { Text(stringResource(R.string.update_cancel)) }
            }
            if (info != null && ui.phase == Phase.IDLE) {
                if (allowLater && ui.problem == null) {
                    TextButton(onClick = controller::later) { Text(stringResource(R.string.update_later)) }
                }
                Button(onClick = { controller.startUpdate(context) }) {
                    Text(stringResource(if (ui.problem == null) R.string.update_button else R.string.update_try_again))
                }
            }
        }
    }
}

@Composable
private fun problemText(problem: Problem, detail: String?): String = when (problem) {
    Problem.DOWNLOAD_FAILED -> stringResource(R.string.update_err_download)
    Problem.DOES_NOT_MATCH -> stringResource(R.string.update_err_mismatch)
    Problem.WRONG_SIGNER -> stringResource(R.string.update_err_signer)
    Problem.WRONG_PACKAGE -> stringResource(R.string.update_err_package)
    Problem.UNREADABLE -> stringResource(R.string.update_err_unreadable)
    Problem.PERMISSION_DENIED -> stringResource(R.string.update_err_permission)
    Problem.INSTALL_CANCELLED -> stringResource(R.string.update_err_cancelled)
    Problem.NO_SPACE -> stringResource(R.string.update_err_space)
    Problem.INCOMPATIBLE -> stringResource(R.string.update_err_incompatible)
    Problem.BLOCKED -> stringResource(R.string.update_err_blocked)
    Problem.INSTALL_FAILED ->
        if (detail.isNullOrBlank()) stringResource(R.string.update_err_install)
        else stringResource(R.string.update_err_install_detail, detail)
}
