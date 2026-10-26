package io.dlne.chat.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.selectable
import androidx.compose.foundation.selection.selectableGroup
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.unit.dp
import io.dlne.chat.R
import io.dlne.chat.data.ReportReason
import io.dlne.chat.data.Reports
import io.dlne.chat.data.takeChars

private fun ReportReason.label(): Int = when (this) {
    ReportReason.HARMFUL -> R.string.report_reason_harmful
    ReportReason.WRONG -> R.string.report_reason_wrong
    ReportReason.OFFENSIVE -> R.string.report_reason_offensive
    ReportReason.OTHER -> R.string.report_reason_other
}

/**
 * "Report answer": pick a reason, optionally add a note, send. The sheet stays open while
 * sending and on an error (so the person can retry or cancel); on success the view model
 * closes it and the chat shows "Thanks, we'll review it".
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ReportSheet(state: ReportUiState, onSend: (ReportReason, String) -> Unit, onDismiss: () -> Unit) {
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    // Keyed on the answer, so reporting a different answer starts from a clean form.
    var reason by rememberSaveable(state.messageId) { mutableStateOf<ReportReason?>(null) }
    var note by rememberSaveable(state.messageId) { mutableStateOf("") }

    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheetState) {
        Column(
            Modifier
                .fillMaxWidth()
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(start = 24.dp, end = 24.dp, bottom = 24.dp),
        ) {
            Text(stringResource(R.string.report_answer), style = MaterialTheme.typography.titleLarge)
            Spacer(Modifier.size(8.dp))
            Text(
                stringResource(R.string.report_body),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.size(12.dp))

            Column(Modifier.selectableGroup()) {
                for (option in ReportReason.entries) {
                    Row(
                        verticalAlignment = Alignment.CenterVertically,
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(min = 48.dp)
                            .selectable(
                                selected = reason == option,
                                enabled = !state.sending,
                                role = Role.RadioButton,
                                onClick = { reason = option },
                            ),
                    ) {
                        RadioButton(selected = reason == option, onClick = null, enabled = !state.sending)
                        Spacer(Modifier.size(12.dp))
                        Text(stringResource(option.label()), style = MaterialTheme.typography.bodyLarge)
                    }
                }
            }

            Spacer(Modifier.size(8.dp))
            OutlinedTextField(
                value = note,
                onValueChange = { note = it.takeChars(Reports.MAX_NOTE_CHARS) },
                label = { Text(stringResource(R.string.report_note_label)) },
                supportingText = {
                    Text(stringResource(R.string.report_note_count, note.length, Reports.MAX_NOTE_CHARS))
                },
                enabled = !state.sending,
                minLines = 2,
                maxLines = 5,
                keyboardOptions = KeyboardOptions(capitalization = KeyboardCapitalization.Sentences),
                modifier = Modifier.fillMaxWidth(),
            )

            state.error?.let { error ->
                Text(
                    error,
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }

            Row(
                horizontalArrangement = Arrangement.End,
                verticalAlignment = Alignment.CenterVertically,
                modifier = Modifier.fillMaxWidth().padding(top = 16.dp),
            ) {
                TextButton(onClick = onDismiss) { Text(stringResource(R.string.cancel)) }
                Spacer(Modifier.size(8.dp))
                Button(
                    onClick = { reason?.let { onSend(it, note) } },
                    enabled = reason != null && !state.sending,
                ) {
                    if (state.sending) {
                        CircularProgressIndicator(
                            Modifier.size(18.dp),
                            strokeWidth = 2.dp,
                            color = LocalContentColor.current,
                        )
                    } else {
                        Text(stringResource(R.string.report_send))
                    }
                }
            }
        }
    }
}
