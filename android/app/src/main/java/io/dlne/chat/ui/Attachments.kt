package io.dlne.chat.ui

import io.dlne.chat.data.ChatHistory
import io.dlne.chat.data.ChatMessage
import io.dlne.chat.data.PhotoEncoding
import io.dlne.chat.net.ApiException
import io.dlne.chat.net.ClusterStatus
import io.dlne.chat.net.WireMessage

/**
 * True for the 429 that says this network has used its hour of photos (`code: image_quota`,
 * contract 2.3). Unlike a 429 for asking too fast, it does not pause Send: the chat just shows
 * the server's own words, and the photos that were refused are not sent again with later
 * messages (see [refusedPhotoTurn]), so messages without photos still go through.
 */
fun isPhotoQuota(error: ApiException.Http): Boolean = error.code == 429 && error.errorCode == "image_quota"

/**
 * The 400 codes the server gives for a photo it will not take (engine/media.py): too many, too
 * big, not a PNG or JPEG, a link instead of the picture. (The engine being unable to read pictures
 * is `image_input_unavailable`, which the `image_` prefix covers, like any other image code.)
 */
private val PHOTO_REFUSALS = setOf("too_many_images", "image_too_large", "unsupported_image_type", "remote_image_url")

/** True when the server turned a request down because of its photos: the hour's allowance, or a photo it will not take. */
fun isPhotoRefusal(error: ApiException.Http): Boolean =
    isPhotoQuota(error) || (error.code == 400 && error.errorCode?.let { it.startsWith("image_") || it in PHOTO_REFUSALS } == true)

/**
 * The message to mark [ChatMessage.photosRefused] after [error], or null. Only a request that really
 * carried pictures ([sent] holds an image part) can have been refused for them, and the pictures it
 * carried are those of the newest message with photos ([ChatHistory.photoTurnId]). Any other failure
 * (the network, a server error, a rate limit) says nothing about the photos, which then stay as they are.
 */
fun refusedPhotoTurn(messages: List<ChatMessage>, sent: List<WireMessage>, error: ApiException.Http): String? =
    if (isPhotoRefusal(error) && sent.any { it.images.isNotEmpty() }) ChatHistory.photoTurnId(messages) else null

/**
 * What the "+" next to the message box offers, from what the server says it can do (contract 2.2):
 * Photos and Camera need `capabilities.app_vision`, Create image needs `capabilities.image_generation`,
 * and with neither the "+" is not shown at all. An older server or no status yet offers nothing.
 */
data class AttachMenu(val photos: Boolean, val camera: Boolean, val createImage: Boolean) {
    val visible: Boolean get() = photos || camera || createImage

    companion object {
        fun of(status: ClusterStatus?): AttachMenu = AttachMenu(
            photos = status?.appVision == true,
            camera = status?.appVision == true,
            createImage = status?.imageGeneration == true,
        )
    }
}

/**
 * A photo chosen for the next message, already turned into the JPEG that will be sent. It is kept
 * in memory only until Send: nothing is written to the phone's storage for a photo that is never sent.
 * [id] is the one it was given when it was chosen (its place in the box). Not a data class (it holds a
 * byte array).
 */
class DraftPhoto(val id: String, val jpeg: ByteArray, val width: Int, val height: Int)

/**
 * The photos chosen for the next message: the [ready] ones, and those still being prepared
 * ([preparing]: the id of each, so one that never gets ready can be taken out).
 */
data class DraftPhotos(val ready: List<DraftPhoto> = emptyList(), val preparing: List<String> = emptyList()) {
    /** How many photos are still being prepared. */
    val loading: Int get() = preparing.size

    val count: Int get() = ready.size + preparing.size

    val isEmpty: Boolean get() = count == 0

    /** How many more photos fit in this message. */
    val room: Int get() = (PhotoEncoding.MAX_PHOTOS - count).coerceAtLeast(0)

    /**
     * Send needs something to send: words, or a photo that is ready. A photo still being prepared does
     * not hold it up (one that never gets ready must not block the box); the view model gives such a
     * photo a moment before the message goes without it.
     */
    fun canSend(hasText: Boolean): Boolean = hasText || ready.isNotEmpty()
}
