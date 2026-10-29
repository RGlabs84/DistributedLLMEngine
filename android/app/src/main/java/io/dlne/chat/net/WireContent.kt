package io.dlne.chat.net

import kotlinx.serialization.KSerializer
import kotlinx.serialization.SerializationException
import kotlinx.serialization.descriptors.SerialDescriptor
import kotlinx.serialization.descriptors.buildClassSerialDescriptor
import kotlinx.serialization.descriptors.element
import kotlinx.serialization.encoding.Decoder
import kotlinx.serialization.encoding.Encoder
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonEncoder
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * Writes a [WireMessage] the way the server reads it (contract 2.1). `content` is a plain string,
 * exactly as before photos existed, for every turn without photos. A user turn with photos is a
 * list of parts instead: `{"type": "text", "text": ...}` when there is text (a turn may be photos
 * alone), then one `{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}` per
 * photo.
 *
 * The request is only ever written, never read back, so reading is refused rather than guessed at.
 */
object WireMessageSerializer : KSerializer<WireMessage> {
    override val descriptor: SerialDescriptor = buildClassSerialDescriptor("io.dlne.chat.net.WireMessage") {
        element<String>("role")
        element<JsonElement>("content")
    }

    override fun serialize(encoder: Encoder, value: WireMessage) {
        val json = encoder as? JsonEncoder ?: throw SerializationException("A chat message is only ever written as JSON.")
        json.encodeJsonElement(
            buildJsonObject {
                put("role", value.role)
                put("content", contentOf(value))
            },
        )
    }

    override fun deserialize(decoder: Decoder): WireMessage =
        throw SerializationException("A chat message is only written, never read back.")

    private fun contentOf(message: WireMessage): JsonElement {
        if (message.images.isEmpty()) return JsonPrimitive(message.content)
        return JsonArray(
            buildList {
                if (message.content.isNotEmpty()) {
                    add(
                        buildJsonObject {
                            put("type", "text")
                            put("text", message.content)
                        },
                    )
                }
                for (url in message.images) {
                    add(
                        buildJsonObject {
                            put("type", "image_url")
                            put("image_url", buildJsonObject { put("url", url) })
                        },
                    )
                }
            },
        )
    }
}
