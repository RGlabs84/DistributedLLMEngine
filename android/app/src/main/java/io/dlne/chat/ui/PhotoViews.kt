package io.dlne.chat.ui

import android.content.ActivityNotFoundException
import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.PickVisualMediaRequest
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxWithConstraints
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.WindowInsets
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawing
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.windowInsetsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.IconButtonDefaults
import androidx.compose.material3.ListItem
import androidx.compose.material3.ListItemDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.res.painterResource
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import androidx.core.content.FileProvider
import io.dlne.chat.AppGraph
import io.dlne.chat.R
import io.dlne.chat.data.ImageStore
import io.dlne.chat.data.Photo
import io.dlne.chat.data.PhotoEncoding
import io.dlne.chat.data.PhotoFiles
import java.io.File
import java.io.IOException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * The "+" at the left of the message box and the sheet it opens: Photos (the system photo picker),
 * Camera (the phone's camera app) and Create image (the dialog), each only when [menu] offers it.
 *
 * It is always part of the screen, even when the "+" itself is hidden, so that the photo picker's
 * and the camera's answers still arrive if the server status changes while one of them is open.
 * [photoRoom] is how many more photos fit in the message. [onAddPhotos] gets what was chosen, and the
 * camera's temporary file when it came from the camera (the receiver deletes it once read).
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun AttachButton(
    menu: AttachMenu,
    photoRoom: Int,
    enabled: Boolean,
    createImageEnabled: Boolean,
    onAddPhotos: (uris: List<Uri>, cameraFile: File?) -> Unit,
    onCreateImage: () -> Unit,
) {
    val context = LocalContext.current
    var sheet by rememberSaveable { mutableStateOf(false) }
    // The camera app can be in front for a while and this screen can be rebuilt meanwhile: the path survives that.
    var cameraPath by rememberSaveable { mutableStateOf<String?>(null) }

    val pickOne = rememberLauncherForActivityResult(ActivityResultContracts.PickVisualMedia()) { uri ->
        if (uri != null) onAddPhotos(listOf(uri), null)
    }
    // The picker takes at most as many as still fit; it needs a limit above 1, and the single-photo picker covers a limit of 1.
    val pickMany = rememberLauncherForActivityResult(
        remember(photoRoom) { ActivityResultContracts.PickMultipleVisualMedia(maxOf(2, photoRoom)) },
    ) { uris ->
        if (uris.isNotEmpty()) onAddPhotos(uris, null)
    }
    val takePicture = rememberLauncherForActivityResult(ActivityResultContracts.TakePicture()) { taken ->
        val file = cameraPath?.let(::File)
        cameraPath = null
        if (file != null) {
            if (taken) onAddPhotos(listOf(cameraUri(context, file)), file) else file.delete()
        }
    }

    val choosePhotos = {
        sheet = false
        val request = PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageOnly)
        if (photoRoom >= 2) pickMany.launch(request) else pickOne.launch(request)
    }
    val takePhoto = {
        sheet = false
        try {
            val file = PhotoFiles.newCameraFile(context.cacheDir)
            cameraPath = file.path
            try {
                takePicture.launch(cameraUri(context, file))
            } catch (_: ActivityNotFoundException) { // no camera app on this phone
                cameraPath = null
                file.delete()
                Toast.makeText(context, R.string.camera_unavailable, Toast.LENGTH_SHORT).show()
            }
        } catch (_: IOException) { // no room in the cache for the shot
            Toast.makeText(context, R.string.camera_unavailable, Toast.LENGTH_SHORT).show()
        }
    }

    if (!menu.visible) return

    IconButton(onClick = { sheet = true }, enabled = enabled, modifier = Modifier.size(52.dp)) {
        // With only Create image on offer the "+" is that, and says so.
        val label = if (menu.photos || menu.camera) R.string.attach else R.string.create_image
        Icon(painterResource(R.drawable.ic_add), contentDescription = stringResource(label))
    }

    if (sheet) {
        ModalBottomSheet(
            onDismissRequest = { sheet = false },
            sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true),
        ) {
            Column(Modifier.fillMaxWidth().padding(bottom = 16.dp)) {
                if (menu.photos) AttachRow(R.drawable.ic_image, stringResource(R.string.attach_photos), photoRoom > 0, choosePhotos)
                if (menu.camera) AttachRow(R.drawable.ic_camera, stringResource(R.string.attach_camera), photoRoom > 0, takePhoto)
                if (menu.createImage) {
                    AttachRow(R.drawable.ic_sparkle, stringResource(R.string.create_image), createImageEnabled) {
                        sheet = false
                        onCreateImage()
                    }
                }
                if (menu.photos || menu.camera) {
                    if (photoRoom == 0) {
                        SheetNote(pluralStringResource(R.plurals.attach_limit, PhotoEncoding.MAX_PHOTOS, PhotoEncoding.MAX_PHOTOS))
                    }
                    SheetNote(stringResource(R.string.attach_privacy))
                }
            }
        }
    }
}

/** One choice in the "+" sheet; greyed out and not tappable when [enabled] is false. */
@Composable
private fun AttachRow(icon: Int, label: String, enabled: Boolean, onClick: () -> Unit) {
    ListItem(
        headlineContent = { Text(label) },
        leadingContent = { Icon(painterResource(icon), contentDescription = null) },
        colors = ListItemDefaults.colors(containerColor = Color.Transparent),
        modifier = Modifier
            .alpha(if (enabled) 1f else 0.38f)
            .clickable(enabled = enabled, role = Role.Button, onClick = onClick),
    )
}

@Composable
private fun SheetNote(text: String) {
    Text(
        text,
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier.padding(horizontal = 16.dp, vertical = 4.dp),
    )
}

/** The camera app writes into this file through the app's FileProvider (the same one Share uses). */
private fun cameraUri(context: Context, file: File): Uri =
    FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)

/**
 * The photos chosen for the next message, as thumbnails above the message box, each with a × that
 * takes it back out. Photos still being prepared show as a spinner in their place, with the same ×,
 * so one that never gets ready can be taken out too.
 */
@Composable
fun DraftPhotoStrip(photos: DraftPhotos, onRemove: (String) -> Unit) {
    Row(
        Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()).padding(bottom = 4.dp),
        horizontalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        photos.ready.forEachIndexed { index, photo -> DraftThumbnail(photo, index, photos.count, onRemove) }
        photos.preparing.forEach { id -> PreparingThumbnail(id, onRemove) }
    }
}

/** The × in the corner of a thumbnail in the box. */
@Composable
private fun RemoveButton(label: String, onClick: () -> Unit, modifier: Modifier) {
    IconButton(
        onClick = onClick,
        modifier = modifier.size(28.dp),
        colors = IconButtonDefaults.iconButtonColors(
            containerColor = MaterialTheme.colorScheme.inverseSurface,
            contentColor = MaterialTheme.colorScheme.inverseOnSurface,
        ),
    ) {
        Icon(painterResource(R.drawable.ic_close), contentDescription = label, modifier = Modifier.size(16.dp))
    }
}

@Composable
private fun DraftThumbnail(photo: DraftPhoto, index: Int, total: Int, onRemove: (String) -> Unit) {
    val cover = with(LocalDensity.current) { 72.dp.roundToPx() }
    val bitmap by produceState<ImageBitmap?>(null, photo.id, cover) {
        value = withContext(Dispatchers.Default) { decodeThumbnail(photo.jpeg, cover)?.asImageBitmap() }
    }
    val shown = bitmap
    // 80 dp box: the 72 dp picture sits in its lower left corner, the × in the upper right.
    Box(Modifier.size(80.dp)) {
        Box(
            Modifier
                .align(Alignment.BottomStart)
                .size(72.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(MaterialTheme.colorScheme.surfaceContainerHigh),
        ) {
            if (shown != null) {
                Image(
                    shown,
                    contentDescription = photoDescription(index, total),
                    contentScale = ContentScale.Crop,
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
        val label = if (total == 1) stringResource(R.string.photo_remove) else stringResource(R.string.photo_remove_n, index + 1)
        RemoveButton(label, onClick = { onRemove(photo.id) }, modifier = Modifier.align(Alignment.TopEnd))
    }
}

/** A photo still being prepared: a spinner in its place, and the × that stops waiting for it. */
@Composable
private fun PreparingThumbnail(id: String, onRemove: (String) -> Unit) {
    val description = stringResource(R.string.photo_preparing)
    Box(Modifier.size(80.dp)) {
        Box(
            Modifier
                .align(Alignment.BottomStart)
                .size(72.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(MaterialTheme.colorScheme.surfaceContainerHigh)
                .semantics { contentDescription = description },
            contentAlignment = Alignment.Center,
        ) {
            CircularProgressIndicator(Modifier.size(24.dp), strokeWidth = 2.dp)
        }
        RemoveButton(stringResource(R.string.photo_remove), onClick = { onRemove(id) }, modifier = Modifier.align(Alignment.TopEnd))
    }
}

/**
 * The photos of a message of the person's, as thumbnails (two to a row) above its text. Tapping one
 * opens it full screen. A photo whose file is gone shows as a quiet placeholder.
 */
@Composable
fun MessagePhotos(photos: List<Photo>) {
    val context = LocalContext.current
    val store = remember { AppGraph.get(context).images }
    var open by rememberSaveable { mutableStateOf<String?>(null) }
    // A lone photo is shown larger; two to a row are squares that always fit the bubble, even on a narrow phone.
    BoxWithConstraints {
        val side = if (photos.size == 1) minOf(220.dp, maxWidth) else minOf(112.dp, (maxWidth - 4.dp) / 2)
        Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
            photos.chunked(2).forEachIndexed { rowIndex, row ->
                Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                    row.forEachIndexed { column, photo ->
                        BubblePhoto(photo, store, side, rowIndex * 2 + column, photos.size) { open = photo.file }
                    }
                }
            }
        }
    }
    open?.let { name ->
        PhotoViewer(photos.firstOrNull { it.file == name }?.let { store.file(it.file) }, onDismiss = { open = null })
    }
}

@Composable
private fun BubblePhoto(photo: Photo, store: ImageStore, side: Dp, index: Int, total: Int, onClick: () -> Unit) {
    val file = remember(photo.file) { store.file(photo.file) }
    val cover = with(LocalDensity.current) { side.roundToPx() }
    val bitmap by produceState<ImageBitmap?>(null, file, cover) {
        value = file?.let { withContext(Dispatchers.IO) { decodeThumbnail(it, cover)?.asImageBitmap() } }
    }
    val shown = bitmap
    val open = stringResource(R.string.photo_open)
    Box(
        Modifier
            .size(side)
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.onSecondaryContainer.copy(alpha = 0.12f))
            .then(if (file != null) Modifier.clickable(onClickLabel = open, role = Role.Button, onClick = onClick) else Modifier),
        contentAlignment = Alignment.Center,
    ) {
        if (shown != null) {
            Image(
                shown,
                contentDescription = photoDescription(index, total),
                contentScale = ContentScale.Crop,
                modifier = Modifier.fillMaxSize(),
            )
        } else if (file == null) {
            Icon(
                painterResource(R.drawable.ic_image),
                contentDescription = stringResource(R.string.photo_missing),
                tint = MaterialTheme.colorScheme.onSecondaryContainer.copy(alpha = 0.6f),
            )
        }
    }
}

/**
 * A photo full screen on black, over the chat. Tap it, the × or Back to close. [file] is null when
 * the photo is no longer stored on the phone.
 */
@Composable
fun PhotoViewer(file: File?, onDismiss: () -> Unit) {
    val bitmap by produceState<ImageBitmap?>(null, file) {
        value = file?.let { withContext(Dispatchers.IO) { BitmapFactory.decodeFile(it.path)?.asImageBitmap() } }
    }
    val shown = bitmap
    val close = stringResource(R.string.photo_close)
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Box(
            Modifier
                .fillMaxSize()
                .background(Color.Black)
                .clickable(interactionSource = null, indication = null, onClickLabel = close, onClick = onDismiss),
            contentAlignment = Alignment.Center,
        ) {
            if (shown != null) {
                Image(
                    shown,
                    contentDescription = stringResource(R.string.photo_one),
                    contentScale = ContentScale.Fit,
                    modifier = Modifier.fillMaxSize(),
                )
            } else if (file == null) {
                Text(stringResource(R.string.photo_missing), color = Color.White)
            } else {
                CircularProgressIndicator(color = Color.White)
            }
            IconButton(
                onClick = onDismiss,
                modifier = Modifier.align(Alignment.TopEnd).windowInsetsPadding(WindowInsets.safeDrawing).padding(8.dp),
            ) {
                Icon(painterResource(R.drawable.ic_close), contentDescription = close, tint = Color.White)
            }
        }
    }
}

@Composable
private fun photoDescription(index: Int, total: Int): String =
    if (total == 1) stringResource(R.string.photo_one) else stringResource(R.string.photo_n_of, index + 1, total)

/**
 * A small copy of a photo for a thumbnail that fills a [cover] x [cover] pixel square: the JPEG
 * decoded with pixels skipped, so a whole picture is never held for a thumbnail, and never fewer
 * pixels than the square needs, so it stays sharp.
 */
private fun decodeThumbnail(jpeg: ByteArray, cover: Int): Bitmap? {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, bounds)
    val options = thumbnailOptions(bounds, cover) ?: return null
    return BitmapFactory.decodeByteArray(jpeg, 0, jpeg.size, options)
}

private fun decodeThumbnail(file: File, cover: Int): Bitmap? {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeFile(file.path, bounds)
    val options = thumbnailOptions(bounds, cover) ?: return null
    return BitmapFactory.decodeFile(file.path, options)
}

private fun thumbnailOptions(bounds: BitmapFactory.Options, cover: Int): BitmapFactory.Options? {
    if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
    // The shorter side has to reach the square's side (the picture is cropped to fill it).
    val scale = cover.toDouble() / minOf(bounds.outWidth, bounds.outHeight)
    return BitmapFactory.Options().apply { inSampleSize = PhotoEncoding.sampleSizeFor(scale) }
}
