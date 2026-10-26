package io.dlne.chat.ui

import androidx.annotation.StringRes
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.offset
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.minimumInteractiveComponentSize
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.rotate
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.semantics.stateDescription
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import io.dlne.chat.R
import io.dlne.chat.data.Source
import io.dlne.chat.markdown.Markdown

/**
 * The status wording for a lookup tool: (with a query, without one). Unknown tools get the
 * neutral "Looking up …" so a newer server never shows something misleading.
 */
internal fun lookupStrings(tool: String): Pair<Int, Int> = when (tool) {
    "weather" -> R.string.lookup_weather to R.string.lookup_weather_no_query
    "wikipedia" -> R.string.lookup_wikipedia to R.string.lookup_wikipedia_no_query
    "news" -> R.string.lookup_news to R.string.lookup_news_no_query
    "web_search" -> R.string.lookup_web to R.string.lookup_web_no_query
    else -> R.string.lookup_other to R.string.lookup_other_no_query
}

/** "Checking the weather for “Philadelphia”" with a small spinner, while a lookup runs. */
@Composable
fun LookupStatusRow(lookup: Lookup) {
    val (withQuery, withoutQuery) = lookupStrings(lookup.tool)
    val text = if (lookup.query.isBlank()) stringResource(withoutQuery) else stringResource(withQuery, lookup.query)
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        CircularProgressIndicator(Modifier.size(16.dp), strokeWidth = 2.dp)
        Text(
            text,
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
        )
    }
}

/** The service's remarks about its lookups, as quiet lines with an info mark. */
@Composable
fun SearchNotes(notes: List<String>) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        for (note in notes) {
            Row(verticalAlignment = Alignment.Top, horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                Icon(
                    painterResource(R.drawable.ic_info),
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(top = 2.dp).size(14.dp),
                )
                Text(note, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
    }
}

/** How many distinct hosts the folded sources line names before "+K". */
private const val SUMMARY_HOSTS = 3

/**
 * The folded sources line, in parts: how many sources there are, the first few distinct
 * hosts in list order, and how many more distinct hosts there are. A source without a host
 * is counted but names nothing.
 */
internal data class SourcesSummary(val count: Int, val hosts: List<String>, val moreHosts: Int) {
    /** " · patriots.com, nytimes.com, bostonglobe.com +3" after the count, or "" when no source has a host. */
    val hostsSuffix: String
        get() = if (hosts.isEmpty()) "" else " · " + hosts.joinToString(", ") + if (moreHosts > 0) " +$moreHosts" else ""

    /** The whole line: [countText] is the "10 sources" plural (R.plurals.sources_count). */
    fun text(countText: String): String = countText + hostsSuffix
}

internal fun sourcesSummary(sources: List<Source>): SourcesSummary {
    val hosts = sources.mapNotNull { source -> source.host?.takeIf { it.isNotBlank() } }.distinct()
    return SourcesSummary(
        count = sources.size,
        hosts = hosts.take(SUMMARY_HOSTS),
        moreHosts = (hosts.size - SUMMARY_HOSTS).coerceAtLeast(0),
    )
}

/**
 * The sources under an answer, folded into one tappable line ("› 10 sources · patriots.com,
 * nytimes.com, bostonglobe.com +3") so a long list never pushes the answer out of view.
 * Unfolded, each source shows its number, title, then "host · age". A row opens its page
 * in the browser only when the address is http(s); otherwise it is plain text. Titles are
 * shown as text, never interpreted as Markdown.
 *
 * [expanded] is held by the caller (per message), so it survives the list being replaced
 * while an answer streams and the answer then being saved.
 */
@Composable
fun SourceList(sources: List<Source>, expanded: Boolean, onToggle: () -> Unit) {
    val context = LocalContext.current
    Column(Modifier.fillMaxWidth()) {
        SourcesHeader(sourcesSummary(sources), expanded, onToggle)
        if (expanded) {
            for (source in sources) {
                SourceRow(source, onOpen = { url -> openInBrowser(context, url) })
            }
        }
    }
}

/** The one-line, rounded toggle: chevron, count, then the first hosts (ellipsised). */
@Composable
private fun SourcesHeader(summary: SourcesSummary, expanded: Boolean, onToggle: () -> Unit) {
    val countText = pluralStringResource(R.plurals.sources_count, summary.count, summary.count)
    val stateText = stringResource(if (expanded) R.string.sources_expanded else R.string.sources_collapsed)
    val actionText = stringResource(if (expanded) R.string.sources_hide else R.string.sources_show)
    val chevronAngle by animateFloatAsState(if (expanded) 90f else 0f, label = "sources chevron")
    val colors = MaterialTheme.colorScheme
    val line = buildAnnotatedString {
        withStyle(SpanStyle(color = colors.onSurface, fontWeight = FontWeight.SemiBold)) { append(countText) }
        append(summary.hostsSuffix)
    }
    val shape = RoundedCornerShape(50)
    Row(
        Modifier
            // 36 dp to look at, 48 dp to touch.
            .minimumInteractiveComponentSize()
            .clip(shape)
            .border(1.dp, colors.outlineVariant, shape)
            .background(colors.surfaceContainerLow)
            .clickable(onClickLabel = actionText, role = Role.Button, onClick = onToggle)
            .semantics { stateDescription = stateText }
            .padding(start = 10.dp, end = 14.dp, top = 8.dp, bottom = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(
            "›",
            style = MaterialTheme.typography.titleMedium,
            color = colors.onSurfaceVariant,
            // Decoration: TalkBack hears the state ("Collapsed" / "Expanded") instead.
            modifier = Modifier.rotate(chevronAngle).clearAndSetSemantics {},
        )
        Text(
            line,
            style = MaterialTheme.typography.labelLarge,
            color = colors.onSurfaceVariant,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f, fill = false),
        )
    }
}

@Composable
private fun SourceRow(source: Source, onOpen: (String) -> Unit) {
    val url = source.url?.let(Markdown::safeUrl)
    val openLabel = stringResource(R.string.open_source_page)
    val clickable = if (url != null) {
        Modifier.clickable(onClickLabel = openLabel, role = Role.Button) { onOpen(url) }
    } else {
        Modifier
    }
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(8.dp))
            .then(clickable)
            .padding(vertical = 5.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Text(
            source.n.toString(),
            style = MaterialTheme.typography.labelMedium,
            color = MaterialTheme.colorScheme.primary,
            modifier = Modifier.width(24.dp).padding(top = 1.dp),
        )
        Column(Modifier.weight(1f)) {
            Text(
                source.title,
                style = MaterialTheme.typography.bodyMedium,
                color = if (url != null) MaterialTheme.colorScheme.onSurface else MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            val meta = listOfNotNull(source.host, source.age).joinToString(" · ")
            if (meta.isNotEmpty()) {
                Text(
                    meta,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis,
                )
            }
        }
    }
}

/** Copy and Report, under each finished answer. */
@Composable
fun AnswerActions(onCopy: () -> Unit, onReport: () -> Unit) {
    // Pulled left so the icons line up with the text above despite the 48 dp touch targets.
    Row(Modifier.offset(x = (-12).dp)) {
        SmallAction(R.drawable.ic_copy, R.string.copy_message, onCopy)
        SmallAction(R.drawable.ic_flag, R.string.report_answer, onReport)
    }
}

@Composable
private fun SmallAction(icon: Int, @StringRes label: Int, onClick: () -> Unit) {
    IconButton(onClick = onClick) {
        Icon(
            painterResource(icon),
            contentDescription = stringResource(label),
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.size(18.dp),
        )
    }
}
