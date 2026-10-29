package io.dlne.chat.ui

import android.Manifest
import android.app.Activity
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.heading
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.core.app.ActivityCompat
import io.dlne.chat.R

/**
 * The card above the message box is showing: a message about where the person is waits for their
 * answer to "Use your location for this?". [blocked]: Android will not show its permission dialog
 * again (it was refused for good), so the main button opens the app's settings page instead.
 */
data class LocationAsk(val blocked: Boolean = false)

/** What the person did with the card. */
enum class LocationAnswer {
    /** Allowed, precisely or only approximately (or it already was): sharing turns on. */
    GRANTED,

    /** "Not now". */
    NOT_NOW,

    /** Android's dialog was refused; it may offer to ask again. */
    REFUSED,

    /** Android will not ask again: the person was sent to the app's settings page, or their refusal ended its dialogs. */
    BLOCKED,
}

private val LOCATION_PERMISSIONS = arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)

/**
 * What Android's answer to the permission request means, or null when nothing was decided. Either
 * permission is enough (Android 12+ lets the person allow only approximate). A refusal after which
 * Android would still explain and ask again ([shouldExplain]) is [LocationAnswer.REFUSED]; one after
 * which it never will is [LocationAnswer.BLOCKED]. An empty answer means the request was interrupted
 * (Android 8 to 10 cancel it when the person leaves the app, say for a call): the card stays, and the
 * message is not sent without a location on the strength of a dialog nobody answered.
 */
internal fun locationAnswerOf(answers: Map<String, Boolean>, shouldExplain: (String) -> Boolean): LocationAnswer? = when {
    answers.values.any { it } -> LocationAnswer.GRANTED
    answers.isEmpty() -> null
    LOCATION_PERMISSIONS.none(shouldExplain) -> LocationAnswer.BLOCKED
    else -> LocationAnswer.REFUSED
}

/**
 * "Use your location for this?": shown above the message box when a question needs the phone's
 * location (the weather, places near you) and sharing is off. "Use my location" asks Android for
 * the permission (it shows its own precise or approximate dialog), or opens the app's settings page
 * when Android will not ask any more; "Not now" sends the message without. The answer goes to
 * [onAnswer]; [permitted] says whether the permission is already given (then there is nothing to ask).
 */
@Composable
fun LocationAskCard(ask: LocationAsk, permitted: () -> Boolean, onAnswer: (LocationAnswer) -> Unit) {
    val context = LocalContext.current
    val request = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { answers ->
        val activity = context.findActivity()
        // An interrupted request gives no answer at all: the card stays for the person to try again.
        locationAnswerOf(answers) { permission ->
            // Without an activity there is no telling: keep offering rather than claim it is blocked.
            activity == null || ActivityCompat.shouldShowRequestPermissionRationale(activity, permission)
        }?.let(onAnswer)
    }
    Surface(
        shape = RoundedCornerShape(16.dp),
        color = MaterialTheme.colorScheme.surfaceContainerHigh,
        modifier = Modifier
            .fillMaxWidth()
            // Announced when it appears: the send is held until the person answers.
            .semantics { liveRegion = LiveRegionMode.Polite },
    ) {
        Column(
            Modifier.padding(start = 16.dp, end = 8.dp, top = 12.dp, bottom = 4.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            Text(
                stringResource(R.string.location_ask_title),
                style = MaterialTheme.typography.titleSmall,
                modifier = Modifier.semantics { heading() },
            )
            Text(
                stringResource(R.string.location_ask_body),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
                TextButton(onClick = { onAnswer(LocationAnswer.NOT_NOW) }) {
                    Text(stringResource(R.string.location_ask_not_now))
                }
                Button(
                    onClick = {
                        when {
                            permitted() -> onAnswer(LocationAnswer.GRANTED)
                            ask.blocked -> {
                                openAppSettings(context)
                                onAnswer(LocationAnswer.BLOCKED)
                            }
                            else -> request.launch(LOCATION_PERMISSIONS)
                        }
                    },
                    modifier = Modifier.padding(start = 8.dp, end = 8.dp, bottom = 4.dp),
                ) {
                    Text(stringResource(if (ask.blocked) R.string.location_ask_settings else R.string.location_ask_use))
                }
            }
        }
    }
}

/** The app's own page in Android's settings, where the location permission can be allowed. */
private fun openAppSettings(context: Context) {
    val page = Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", context.packageName, null))
    try {
        context.startActivity(page)
    } catch (_: ActivityNotFoundException) {
        try {
            context.startActivity(Intent(Settings.ACTION_SETTINGS))
        } catch (_: ActivityNotFoundException) {
            // A phone with no settings app: nothing more to offer.
        }
    }
}

private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}
