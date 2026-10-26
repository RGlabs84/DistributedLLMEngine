package io.dlne.chat.ui

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.LinkAnnotation
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLinkStyles
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.BaselineShift
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.text.withLink
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.em
import io.dlne.chat.R
import io.dlne.chat.data.Source
import io.dlne.chat.markdown.Markdown
import io.dlne.chat.markdown.MdBlock
import io.dlne.chat.markdown.MdInline
import io.dlne.chat.ui.theme.LocalCodeColors

/**
 * Renders an answer with the app's Markdown subset (see [Markdown]) using plain Compose
 * text styling: no WebView, no HTML. Re-parses as the text grows while streaming.
 *
 * [sources] are the answer's web sources: `[n]` for one of them is drawn as a small
 * superscript that opens that source when it has an http(s) address.
 */
@Composable
fun MarkdownText(
    markdown: String,
    modifier: Modifier = Modifier,
    sources: List<Source> = emptyList(),
    color: Color = Color.Unspecified,
) {
    val citationUrls = remember(sources) { sources.associate { it.n to it.url } }
    val blocks = remember(markdown, citationUrls) { Markdown.parse(markdown, citationUrls.keys) }
    val styles = rememberInlineStyles()
    val textStyle = MaterialTheme.typography.bodyLarge.copy(color = color.orIfUnspecified(MaterialTheme.colorScheme.onSurface))

    Column(modifier, verticalArrangement = Arrangement.spacedBy(8.dp)) {
        for (block in blocks) {
            when (block) {
                is MdBlock.Paragraph ->
                    Text(remember(block, styles, citationUrls) { annotate(block.inlines, styles, citationUrls) }, style = textStyle)

                is MdBlock.Heading ->
                    Text(
                        remember(block, styles, citationUrls) { annotate(block.inlines, styles, citationUrls) },
                        style = MaterialTheme.typography.titleMedium.copy(color = textStyle.color),
                        fontWeight = FontWeight.SemiBold,
                    )

                is MdBlock.BulletList -> Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    for (item in block.items) {
                        Row(Modifier.padding(start = (item.level * 16).dp)) {
                            Text(if (item.level == 0) "•" else "◦", style = textStyle, modifier = Modifier.width(18.dp))
                            Text(remember(item, styles, citationUrls) { annotate(item.inlines, styles, citationUrls) }, style = textStyle)
                        }
                    }
                }

                is MdBlock.CodeBlock -> CodeBlock(block.language, block.code)

                MdBlock.Rule -> HorizontalDivider(Modifier.padding(vertical = 4.dp))
            }
        }
    }
}

internal data class InlineStyles(
    val code: SpanStyle,
    val link: TextLinkStyles,
    /** A citation marker: small, raised, accent-coloured, not underlined. */
    val citation: SpanStyle,
    val onLink: (String) -> Unit,
)

@Composable
private fun rememberInlineStyles(): InlineStyles {
    val context = LocalContext.current
    val codeBackground = LocalCodeColors.current.inlineBackground
    val linkColor = MaterialTheme.colorScheme.primary
    return remember(context, codeBackground, linkColor) {
        InlineStyles(
            code = SpanStyle(fontFamily = FontFamily.Monospace, background = codeBackground, fontSize = 0.9.em),
            link = TextLinkStyles(style = SpanStyle(color = linkColor, textDecoration = TextDecoration.Underline)),
            citation = SpanStyle(
                color = linkColor,
                fontSize = 0.7.em,
                fontWeight = FontWeight.SemiBold,
                baselineShift = BaselineShift.Superscript,
            ),
            onLink = { url -> openInBrowser(context, url) },
        )
    }
}

/**
 * Builds the styled text for one block. [citationUrls] maps each source number to its
 * http(s) address (null when the source cannot be opened). Internal for unit tests.
 */
internal fun annotate(
    inlines: List<MdInline>,
    styles: InlineStyles,
    citationUrls: Map<Int, String?> = emptyMap(),
): AnnotatedString = buildAnnotatedString { appendInlines(inlines, styles, citationUrls) }

private fun AnnotatedString.Builder.appendInlines(
    inlines: List<MdInline>,
    styles: InlineStyles,
    citationUrls: Map<Int, String?>,
) {
    for (inline in inlines) {
        when (inline) {
            is MdInline.Text -> append(inline.text)
            is MdInline.Bold -> withStyle(SpanStyle(fontWeight = FontWeight.Bold)) {
                appendInlines(inline.children, styles, citationUrls)
            }
            is MdInline.Italic -> withStyle(SpanStyle(fontStyle = FontStyle.Italic)) {
                appendInlines(inline.children, styles, citationUrls)
            }
            is MdInline.Code -> withStyle(styles.code) { append(inline.text) }
            // The link only ever opens through openInBrowser, which re-checks the scheme
            // and survives a phone with no browser installed.
            is MdInline.Link -> withLink(
                LinkAnnotation.Url(inline.url, styles.link) { styles.onLink(inline.url) }
            ) { append(inline.text) }
            is MdInline.Citation -> {
                val url = citationUrls[inline.n]?.let(Markdown::safeUrl)
                if (url != null) {
                    withLink(LinkAnnotation.Url(url, TextLinkStyles(style = styles.citation)) { styles.onLink(url) }) {
                        append("[${inline.n}]")
                    }
                } else {
                    withStyle(styles.citation) { append("[${inline.n}]") }
                }
            }
        }
    }
}

/** A fenced code block: monospace, scrolls sideways instead of wrapping, with a copy button. */
@Composable
private fun CodeBlock(language: String, code: String) {
    val context = LocalContext.current
    val colors = LocalCodeColors.current
    Surface(
        shape = RoundedCornerShape(10.dp),
        color = colors.blockBackground,
        border = BorderStroke(1.dp, colors.blockBorder),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column {
            Row(Modifier.fillMaxWidth().padding(start = 12.dp), verticalAlignment = Alignment.CenterVertically) {
                Text(
                    language.ifBlank { stringResource(R.string.code) },
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                IconButton(onClick = { copyToClipboard(context, code) }) {
                    Icon(
                        painterResource(R.drawable.ic_copy),
                        contentDescription = stringResource(R.string.copy_code),
                        modifier = Modifier.size(18.dp),
                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            Text(
                code,
                fontFamily = FontFamily.Monospace,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface,
                softWrap = false,
                modifier = Modifier
                    .horizontalScroll(rememberScrollState())
                    .padding(start = 12.dp, end = 12.dp, bottom = 12.dp),
            )
        }
    }
}

private fun Color.orIfUnspecified(fallback: Color): Color = if (this == Color.Unspecified) fallback else this
