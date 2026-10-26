package io.dlne.chat.ui.theme

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.Immutable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.graphics.Color

/** DLNE brand cyan. Used sparingly: the send button, links and the brand mark. */
val DlneCyan = Color(0xFF00F0FF)

/*
 * Dark theme: near-black with a teal cast, cyan as the accent. Light theme: cyan is far too
 * bright to read on white, so a deep teal from the same hue carries the accent there.
 */
private val DarkColors = darkColorScheme(
    primary = DlneCyan,
    onPrimary = Color(0xFF00363B),
    primaryContainer = Color(0xFF00494F),
    onPrimaryContainer = Color(0xFFA6F5FF),
    secondary = Color(0xFF9FCDD2),
    onSecondary = Color(0xFF00363B),
    secondaryContainer = Color(0xFF203B3F),
    onSecondaryContainer = Color(0xFFBBE9EE),
    background = Color(0xFF0A1214),
    onBackground = Color(0xFFDCE5E6),
    surface = Color(0xFF0A1214),
    onSurface = Color(0xFFDCE5E6),
    surfaceVariant = Color(0xFF223033),
    onSurfaceVariant = Color(0xFFA9B9BB),
    surfaceContainerLowest = Color(0xFF060C0D),
    surfaceContainerLow = Color(0xFF101A1C),
    surfaceContainer = Color(0xFF141F21),
    surfaceContainerHigh = Color(0xFF1A2729),
    surfaceContainerHighest = Color(0xFF223033),
    outline = Color(0xFF5E7174),
    outlineVariant = Color(0xFF2E3D40),
    error = Color(0xFFFFB4AB),
    onError = Color(0xFF690005),
    errorContainer = Color(0xFF5C1A16),
    onErrorContainer = Color(0xFFFFDAD6),
)

private val LightColors = lightColorScheme(
    primary = Color(0xFF00676E),
    onPrimary = Color.White,
    primaryContainer = Color(0xFFA6F0F8),
    onPrimaryContainer = Color(0xFF002023),
    secondary = Color(0xFF4A6366),
    onSecondary = Color.White,
    secondaryContainer = Color(0xFFCCE8EB),
    onSecondaryContainer = Color(0xFF051F22),
    background = Color(0xFFF6FAFA),
    onBackground = Color(0xFF161D1E),
    surface = Color(0xFFF6FAFA),
    onSurface = Color(0xFF161D1E),
    surfaceVariant = Color(0xFFDAE4E5),
    onSurfaceVariant = Color(0xFF3F4949),
    surfaceContainerLowest = Color.White,
    surfaceContainerLow = Color(0xFFF0F5F5),
    surfaceContainer = Color(0xFFEAEFEF),
    surfaceContainerHigh = Color(0xFFE4E9EA),
    surfaceContainerHighest = Color(0xFFDEE4E4),
    outline = Color(0xFF6F797A),
    outlineVariant = Color(0xFFBEC8C9),
    error = Color(0xFFBA1A1A),
    onError = Color.White,
    errorContainer = Color(0xFFFFDAD6),
    onErrorContainer = Color(0xFF410002),
)

/** Colours for rendered Markdown that the Material scheme has no slot for. */
@Immutable
data class CodeColors(val inlineBackground: Color, val blockBackground: Color, val blockBorder: Color)

private val DarkCode = CodeColors(Color(0xFF1C2A2D), Color(0xFF060C0D), Color(0xFF223033))
private val LightCode = CodeColors(Color(0xFFE2EAEB), Color(0xFFEEF3F3), Color(0xFFD2DCDD))

val LocalCodeColors = staticCompositionLocalOf { DarkCode }

/** Follows the system light/dark setting. Brand colours are fixed (no wallpaper colours). */
@Composable
fun DlneTheme(darkTheme: Boolean = isSystemInDarkTheme(), content: @Composable () -> Unit) {
    CompositionLocalProvider(
        LocalCodeColors provides if (darkTheme) DarkCode else LightCode,
    ) {
        MaterialTheme(
            colorScheme = if (darkTheme) DarkColors else LightColors,
            content = content,
        )
    }
}
