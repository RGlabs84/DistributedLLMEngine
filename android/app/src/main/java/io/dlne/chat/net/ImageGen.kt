package io.dlne.chat.net

import java.util.Base64
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/** The sizes the server offers; the first is the default until the status says otherwise. */
val DEFAULT_IMAGE_SIZES = listOf("768x768", "1024x1024", "768x1024", "1024x768")
const val DEFAULT_IMAGE_SIZE = "768x768"

/** The longest prompt the server takes after cleaning it (`prompt_too_long` beyond that). */
const val MAX_IMAGE_PROMPT_CHARS = 1000

/** `capabilities` of `GET /v1/cluster/status`. A missing field means false. */
@Serializable
data class Capabilities(
    @SerialName("image_generation") val imageGeneration: Boolean = false,
    /** The phone chat reads photos (server 2.6.2+); an older server never says it. */
    @SerialName("app_vision") val appVision: Boolean = false,
)

/** `image_generation` of the status while the server has it on: sizes, queue and typical times. */
@Serializable
data class ImageGenerationInfo(
    val queue: Int? = null,
    val busy: Boolean? = null,
    @SerialName("eta_seconds") val etaSeconds: Map<String, Double> = emptyMap(),
    val sizes: List<String> = emptyList(),
    @SerialName("default_size") val defaultSize: String? = null,
)

/** The sizes to offer: the server's list when it sent one, else the four it documents. */
fun ClusterStatus.imageSizes(): List<String> =
    imageGenerationInfo?.sizes?.filter { IMAGE_SIZE.matches(it) }?.takeIf { it.isNotEmpty() } ?: DEFAULT_IMAGE_SIZES

fun ClusterStatus.defaultImageSize(): String {
    val wanted = imageGenerationInfo?.defaultSize
    return imageSizes().let { sizes -> if (wanted in sizes) wanted!! else sizes.first() }
}

/** Typical seconds for [size] from the status, or null. */
fun ClusterStatus.imageEtaSeconds(size: String): Int? =
    imageGenerationInfo?.etaSeconds?.get(size)?.takeIf { it > 0 }?.let { Math.round(it).toInt() }

/** What a size looks like on the wire: "768x768". */
internal val IMAGE_SIZE = Regex("\\d{3,4}x\\d{3,4}")

/** Where an image job stands (the API's `status`). */
enum class ImageStatus(val wire: String, val finished: Boolean) {
    QUEUED("queued", false),
    CHECKING("checking", false),
    GENERATING("generating", false),
    REVIEWING("reviewing", false),
    COMPLETED("completed", true),
    REFUSED("refused", true),
    FAILED("failed", true),
    CANCELLED("cancelled", true),
    EXPIRED("expired", true),

    /** A status a newer server invented: shown as waiting, never as an end. */
    UNKNOWN("", false);

    companion object {
        fun of(wire: String?): ImageStatus = entries.firstOrNull { it.wire.isNotEmpty() && it.wire == wire } ?: UNKNOWN
    }
}

/** The finished picture, checked to be a PNG. Not a data class: it holds a byte array. */
class GeneratedImage(val png: ByteArray, val width: Int, val height: Int)

/** The answer to creating a job (`202`). */
data class ImageTicket(val id: String, val position: Int?, val etaSeconds: Int?)

/** One poll of `GET /v1/app/images/{id}`. */
data class ImageProgress(
    val id: String,
    val status: ImageStatus,
    val position: Int?,
    val elapsedSeconds: Int,
    val etaSeconds: Int?,
    val image: GeneratedImage?,
    /** The server's own words for a refusal or failure; null otherwise. */
    val errorDetail: String?,
    val errorCode: String?,
)

// The wire shapes. Numbers are Double so that 80 and 80.0 both read.
@Serializable
internal data class ImageRequestBody(
    val prompt: String,
    val size: String? = null,
    @SerialName("chat_id") val chatId: String? = null,
)

@Serializable
internal data class WireTicket(
    val id: String = "",
    val position: Double? = null,
    @SerialName("eta_seconds") val etaSeconds: Double? = null,
)

@Serializable
internal data class WireImage(
    @SerialName("b64_png") val b64Png: String = "",
    val width: Int = 0,
    val height: Int = 0,
)

@Serializable
internal data class WireImageError(val detail: String? = null, val code: String? = null)

@Serializable
internal data class WireProgress(
    val id: String = "",
    val status: String? = null,
    val position: Double? = null,
    @SerialName("elapsed_seconds") val elapsedSeconds: Double? = null,
    @SerialName("eta_seconds") val etaSeconds: Double? = null,
    val image: WireImage? = null,
    val error: WireImageError? = null,
)

private val PNG_SIGNATURE = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)

/** The decoded PNG, or null when the text is not base64 or the bytes are not a PNG. */
internal fun decodePng(b64: String, width: Int, height: Int): GeneratedImage? {
    val bytes = try {
        Base64.getMimeDecoder().decode(b64)
    } catch (_: IllegalArgumentException) {
        return null
    }
    if (bytes.size < PNG_SIGNATURE.size || !PNG_SIGNATURE.indices.all { bytes[it] == PNG_SIGNATURE[it] }) return null
    return GeneratedImage(bytes, width, height)
}

private fun Double?.seconds(): Int? = this?.takeIf { it.isFinite() && it >= 0 }?.let { Math.round(it).toInt() }

internal fun WireTicket.toTicket() = ImageTicket(id, position.seconds(), etaSeconds.seconds())

internal fun WireProgress.toProgress() = ImageProgress(
    id = id,
    status = ImageStatus.of(status),
    position = position.seconds(),
    elapsedSeconds = elapsedSeconds.seconds() ?: 0,
    etaSeconds = etaSeconds.seconds(),
    image = image?.let { decodePng(it.b64Png, it.width, it.height) },
    errorDetail = error?.detail?.takeIf { it.isNotBlank() },
    errorCode = error?.code,
)
