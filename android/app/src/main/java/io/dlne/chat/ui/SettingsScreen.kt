package io.dlne.chat.ui

import android.widget.Toast
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.consumeWindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardActions
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalFocusManager
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import io.dlne.chat.BuildConfig
import io.dlne.chat.R

private const val COFFEE_URL = "https://buymeacoffee.com/wubarrk"
private const val DONATE_COMPUTE_URL = "https://open-vsx.org/extension/dlne/dlne-code-assistant"
private const val SOURCE_URL = "https://github.com/RGlabs84/DistributedLLMEngine"

class SettingsActions(
    val onBack: () -> Unit,
    /** Returns false if the address was not accepted. */
    val onSaveServerUrl: (String) -> Boolean,
    val onResetServerUrl: () -> Unit,
    val onDeleteAllChats: () -> Unit,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(state: ChatUiState, actions: SettingsActions) {
    val context = LocalContext.current
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.settings)) },
                navigationIcon = {
                    IconButton(onClick = actions.onBack) {
                        Icon(painterResource(R.drawable.ic_back), contentDescription = stringResource(R.string.back))
                    }
                },
            )
        },
    ) { padding ->
        Column(
            Modifier
                .fillMaxSize()
                .padding(padding)
                // The bar insets are already in `padding`; only the keyboard's extra height remains.
                .consumeWindowInsets(padding)
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(vertical = 8.dp),
        ) {
            ServerSection(state.serverUrl, actions)

            SectionDivider()
            SectionTitle(stringResource(R.string.settings_privacy_title))
            Column(Modifier.padding(horizontal = 24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                BodyText(stringResource(R.string.settings_privacy_body_1))
                BodyText(stringResource(R.string.settings_privacy_body_2))
                BodyText(stringResource(R.string.settings_privacy_body_3))
                BodyText(stringResource(R.string.settings_privacy_body_4))
            }
            DeleteAllChats(actions.onDeleteAllChats)

            SectionDivider()
            SectionTitle(stringResource(R.string.settings_support_title))
            LinkRow(stringResource(R.string.settings_coffee), stringResource(R.string.settings_coffee_sub)) {
                openInBrowser(context, COFFEE_URL)
            }
            LinkRow(stringResource(R.string.settings_donate_compute), stringResource(R.string.settings_donate_compute_sub)) {
                openInBrowser(context, DONATE_COMPUTE_URL)
            }

            SectionDivider()
            SectionTitle(stringResource(R.string.settings_about_title))
            LinkRow(stringResource(R.string.settings_source), stringResource(R.string.settings_source_sub)) {
                openInBrowser(context, SOURCE_URL)
            }
            Column(Modifier.padding(horizontal = 24.dp, vertical = 8.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                BodyText(stringResource(R.string.settings_version, BuildConfig.VERSION_NAME))
                state.status?.serverVersion?.let { BodyText(stringResource(R.string.settings_server_version, it)) }
            }
            Spacer(Modifier.size(16.dp))
        }
    }
}

@Composable
private fun ServerSection(serverUrl: String, actions: SettingsActions) {
    val context = LocalContext.current
    val focus = LocalFocusManager.current
    // Keyed on the saved address so "Use default" refreshes the field.
    var input by rememberSaveable(serverUrl) { mutableStateOf(serverUrl) }
    var invalid by rememberSaveable { mutableStateOf(false) }
    val savedMessage = stringResource(R.string.settings_server_saved)

    fun save() {
        if (actions.onSaveServerUrl(input)) {
            invalid = false
            focus.clearFocus()
            Toast.makeText(context, savedMessage, Toast.LENGTH_SHORT).show()
        } else {
            invalid = true
        }
    }

    SectionTitle(stringResource(R.string.settings_server_title))
    Column(Modifier.padding(horizontal = 24.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
        OutlinedTextField(
            value = input,
            onValueChange = {
                input = it
                invalid = false
            },
            label = { Text(stringResource(R.string.settings_server_label)) },
            singleLine = true,
            isError = invalid,
            supportingText = {
                Text(stringResource(if (invalid) R.string.settings_server_invalid else R.string.settings_server_help))
            },
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri, imeAction = ImeAction.Done, autoCorrectEnabled = false),
            keyboardActions = KeyboardActions(onDone = { save() }),
            modifier = Modifier.fillMaxWidth(),
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            OutlinedButton(onClick = { save() }, enabled = input.trim() != serverUrl) {
                Text(stringResource(R.string.settings_save))
            }
            TextButton(onClick = {
                invalid = false
                actions.onResetServerUrl()
            }) { Text(stringResource(R.string.settings_use_default)) }
        }
    }
}

@Composable
private fun DeleteAllChats(onDeleteAll: () -> Unit) {
    var confirm by rememberSaveable { mutableStateOf(false) }
    TextButton(onClick = { confirm = true }, modifier = Modifier.padding(horizontal = 12.dp, vertical = 4.dp)) {
        Text(stringResource(R.string.settings_delete_all), color = MaterialTheme.colorScheme.error)
    }
    if (confirm) {
        AlertDialog(
            onDismissRequest = { confirm = false },
            title = { Text(stringResource(R.string.settings_delete_all_title)) },
            text = { Text(stringResource(R.string.settings_delete_all_body)) },
            confirmButton = {
                TextButton(onClick = {
                    confirm = false
                    onDeleteAll()
                }) { Text(stringResource(R.string.delete)) }
            },
            dismissButton = { TextButton(onClick = { confirm = false }) { Text(stringResource(R.string.cancel)) } },
        )
    }
}

/** A tappable row that opens a web page in the browser. */
@Composable
private fun LinkRow(title: String, subtitle: String, onClick: () -> Unit) {
    Row(
        verticalAlignment = Alignment.CenterVertically,
        modifier = Modifier
            .fillMaxWidth()
            .clickable(role = Role.Button, onClick = onClick)
            .padding(horizontal = 24.dp, vertical = 12.dp),
    ) {
        Column(Modifier.weight(1f)) {
            Text(title, style = MaterialTheme.typography.bodyLarge)
            Text(subtitle, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Icon(
            painterResource(R.drawable.ic_open_in_new),
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(start = 12.dp).size(18.dp),
        )
    }
}

@Composable
private fun SectionTitle(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.titleSmall,
        color = MaterialTheme.colorScheme.primary,
        modifier = Modifier.padding(start = 24.dp, end = 24.dp, top = 16.dp, bottom = 8.dp),
    )
}

@Composable
private fun BodyText(text: String) {
    Text(text, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
}

@Composable
private fun SectionDivider() {
    HorizontalDivider(Modifier.padding(top = 16.dp))
}
