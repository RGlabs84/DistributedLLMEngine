package io.dlne.chat.ui

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.widget.Toast
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.window.DialogProperties
import io.dlne.chat.R
import io.dlne.chat.markdown.Markdown

/** Opens an http(s) link in the browser. Any other scheme is ignored. */
fun openInBrowser(context: Context, url: String) {
    val safe = Markdown.safeUrl(url) ?: return
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(safe))
        .addCategory(Intent.CATEGORY_BROWSABLE)
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    try {
        context.startActivity(intent)
    } catch (_: ActivityNotFoundException) {
        Toast.makeText(context, R.string.no_browser, Toast.LENGTH_SHORT).show()
    }
}

/** Copies [text]. Android 13+ shows its own confirmation, so a toast only on older versions. */
fun copyToClipboard(context: Context, text: String) {
    val clipboard = context.getSystemService(ClipboardManager::class.java) ?: return
    clipboard.setPrimaryClip(ClipData.newPlainText(context.getString(R.string.app_name), text))
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
        Toast.makeText(context, R.string.copied, Toast.LENGTH_SHORT).show()
    }
}

/**
 * The first-run notice. It cannot be dismissed by tapping outside or pressing Back: the
 * app is usable only after the person has read it and tapped "I understand".
 */
@Composable
fun PrivacyNoticeDialog(onAcknowledge: () -> Unit) {
    AlertDialog(
        onDismissRequest = {},
        properties = DialogProperties(dismissOnBackPress = false, dismissOnClickOutside = false),
        title = { Text(stringResource(R.string.privacy_title)) },
        text = { Text(stringResource(R.string.privacy_body)) },
        confirmButton = {
            TextButton(onClick = onAcknowledge) { Text(stringResource(R.string.privacy_ack)) }
        },
    )
}
