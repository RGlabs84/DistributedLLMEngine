package io.dlne.chat.ui

import androidx.compose.foundation.Image
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalDrawerSheet
import androidx.compose.material3.NavigationDrawerItem
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import io.dlne.chat.R
import io.dlne.chat.data.Conversation

/** The side drawer: new chat, every stored conversation (newest first) and Settings. */
@Composable
fun ConversationDrawer(
    conversations: List<Conversation>,
    currentId: String?,
    onNewChat: () -> Unit,
    onOpen: (String) -> Unit,
    onDelete: (String) -> Unit,
    onSettings: () -> Unit,
) {
    var confirmDelete by rememberSaveable { mutableStateOf<String?>(null) }

    ModalDrawerSheet(Modifier.widthIn(max = 320.dp)) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            modifier = Modifier.padding(horizontal = 24.dp, vertical = 20.dp),
        ) {
            // The official DLNE logo, unchanged (see mipmap-anydpi/ic_launcher.xml).
            Image(
                painterResource(R.drawable.dlne_logo),
                contentDescription = null,
                modifier = Modifier.size(36.dp).clip(RoundedCornerShape(8.dp)),
            )
            Text(stringResource(R.string.app_name), style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.SemiBold)
        }

        NavigationDrawerItem(
            label = { Text(stringResource(R.string.new_chat)) },
            icon = { Icon(painterResource(R.drawable.ic_add), contentDescription = null) },
            selected = false,
            onClick = onNewChat,
            modifier = Modifier.padding(horizontal = 12.dp),
        )
        HorizontalDivider(Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
        Text(
            stringResource(R.string.chats),
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.padding(horizontal = 28.dp, vertical = 8.dp),
        )

        if (conversations.isEmpty()) {
            Text(
                stringResource(R.string.no_chats),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(horizontal = 28.dp, vertical = 8.dp),
            )
        }
        LazyColumn(Modifier.weight(1f)) {
            items(conversations, key = { it.id }) { conversation ->
                NavigationDrawerItem(
                    label = { Text(conversation.title, maxLines = 1, overflow = TextOverflow.Ellipsis) },
                    selected = conversation.id == currentId,
                    onClick = { onOpen(conversation.id) },
                    badge = {
                        IconButton(onClick = { confirmDelete = conversation.id }, modifier = Modifier.size(36.dp)) {
                            Icon(
                                painterResource(R.drawable.ic_delete),
                                contentDescription = stringResource(R.string.delete_chat),
                                modifier = Modifier.size(18.dp),
                            )
                        }
                    },
                    modifier = Modifier.padding(horizontal = 12.dp),
                )
            }
        }

        HorizontalDivider(Modifier.padding(horizontal = 24.dp, vertical = 8.dp))
        NavigationDrawerItem(
            label = { Text(stringResource(R.string.settings)) },
            icon = { Icon(painterResource(R.drawable.ic_settings), contentDescription = null) },
            selected = false,
            onClick = onSettings,
            modifier = Modifier.padding(horizontal = 12.dp),
        )
        // Quiet footer: two small links, side by side so the chat list keeps its room.
        Row(Modifier.padding(start = 12.dp, end = 12.dp, bottom = 8.dp)) {
            DrawerLink(stringResource(R.string.settings_website), WEBSITE_URL)
            DrawerLink(stringResource(R.string.settings_discord), DISCORD_URL)
        }
    }

    confirmDelete?.let { id ->
        AlertDialog(
            onDismissRequest = { confirmDelete = null },
            title = { Text(stringResource(R.string.delete_chat_title)) },
            text = { Text(stringResource(R.string.delete_chat_body)) },
            confirmButton = {
                TextButton(onClick = {
                    onDelete(id)
                    confirmDelete = null
                }) { Text(stringResource(R.string.delete)) }
            },
            dismissButton = {
                TextButton(onClick = { confirmDelete = null }) { Text(stringResource(R.string.cancel)) }
            },
        )
    }
}

/** A small text link in the drawer's footer that opens [url] in the browser. */
@Composable
private fun DrawerLink(label: String, url: String) {
    val context = LocalContext.current
    TextButton(onClick = { openInBrowser(context, url) }) {
        Text(label, style = MaterialTheme.typography.labelLarge)
        Icon(
            painterResource(R.drawable.ic_open_in_new),
            contentDescription = null,
            modifier = Modifier.padding(start = 6.dp).size(14.dp),
        )
    }
}
