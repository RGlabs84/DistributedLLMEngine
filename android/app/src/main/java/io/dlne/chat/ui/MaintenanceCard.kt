package io.dlne.chat.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import io.dlne.chat.R
import io.dlne.chat.net.MaintenanceInfo

private val CardCyan = Color(0xFF38BDF8)
private val CardViolet = Color(0xFFC084FC)

/** What the card says when the server sent no message of its own (the same line as the web chat). */
internal fun maintenanceMessage(info: MaintenanceInfo, fallback: String): String =
    info.message?.trim()?.takeIf { it.isNotEmpty() } ?: fallback

/**
 * The maintenance screen (server 2.8.5): one full-screen card in DLNE's colours, same look and words as the web chat.
 * It covers the chat while the server says maintenance is on, swallows taps and goes away by itself when the status
 * stops saying so; the draft underneath is untouched.
 */
@Composable
fun MaintenanceCard(info: MaintenanceInfo, modifier: Modifier = Modifier) {
    val message = maintenanceMessage(info, stringResource(R.string.maintenance_default))
    val until = info.until?.trim()?.takeIf { it.isNotEmpty() }
    Box(
        modifier
            .fillMaxSize()
            .background(Color(0xFF05080F))
            .background(
                Brush.linearGradient(
                    0f to Color(0x2400F0FF),
                    0.5f to Color.Transparent,
                    1f to Color(0x24C084FC),
                ),
            )
            // Taps do not fall through to the chat underneath.
            .clickable(interactionSource = remember { MutableInteractionSource() }, indication = null) {},
        contentAlignment = Alignment.Center,
    ) {
        Column(
            Modifier
                .padding(24.dp)
                .widthIn(max = 460.dp)
                .clip(RoundedCornerShape(20.dp))
                .background(Color(0xEB090D18))
                .border(1.dp, Color(0x5200F0FF), RoundedCornerShape(20.dp))
                .padding(horizontal = 24.dp, vertical = 28.dp)
                .semantics { liveRegion = LiveRegionMode.Polite },
            horizontalAlignment = Alignment.CenterHorizontally,
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            Image(
                painterResource(R.drawable.dlne_logo),
                contentDescription = null,
                modifier = Modifier.size(84.dp).clip(RoundedCornerShape(18.dp)),
            )
            Text(
                stringResource(R.string.maintenance_label).uppercase(),
                style = TextStyle(fontFamily = FontFamily.Monospace, fontWeight = FontWeight.SemiBold, fontSize = 12.sp, letterSpacing = 2.sp),
                color = CardCyan,
            )
            Text(
                stringResource(R.string.maintenance_title),
                style = TextStyle(
                    brush = Brush.linearGradient(listOf(Color.White, CardCyan, CardViolet)),
                    fontSize = 25.sp,
                    lineHeight = 31.sp,
                    fontWeight = FontWeight.Bold,
                ),
                textAlign = TextAlign.Center,
            )
            Text(message, color = Color(0xFFE2E8F0), textAlign = TextAlign.Center, lineHeight = 22.sp)
            if (until != null) {
                Text(
                    stringResource(R.string.maintenance_expected, until),
                    style = TextStyle(fontFamily = FontFamily.Monospace, fontSize = 14.sp),
                    color = CardViolet,
                    textAlign = TextAlign.Center,
                )
            }
            Text(
                stringResource(R.string.maintenance_checks),
                color = Color(0xFF94A3B8),
                fontSize = 13.sp,
                textAlign = TextAlign.Center,
            )
        }
    }
}
