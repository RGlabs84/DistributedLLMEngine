package io.dlne.chat.ui

import io.dlne.chat.data.ChatMessage
import io.dlne.chat.data.Photo
import io.dlne.chat.data.Role
import io.dlne.chat.net.ApiException
import io.dlne.chat.net.Capabilities
import io.dlne.chat.net.ClusterStatus
import io.dlne.chat.net.WireMessage
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Which entries the "+" menu offers, the limits on the photos in the box (contract 2.2), and when a
 * refusal from the server is about the photos a request carried.
 */
class AttachMenuTest {
    private fun status(vision: Boolean, images: Boolean) =
        ClusterStatus(capabilities = Capabilities(imageGeneration = images, appVision = vision))

    @Test
    fun `photos and camera follow app_vision, create image follows image_generation`() {
        assertEquals(AttachMenu(photos = true, camera = true, createImage = true), AttachMenu.of(status(vision = true, images = true)))
        assertEquals(AttachMenu(photos = true, camera = true, createImage = false), AttachMenu.of(status(vision = true, images = false)))
        assertEquals(AttachMenu(photos = false, camera = false, createImage = true), AttachMenu.of(status(vision = false, images = true)))
    }

    @Test
    fun `the plus is hidden when neither is on offer`() {
        assertFalse(AttachMenu.of(status(vision = false, images = false)).visible)
        assertTrue(AttachMenu.of(status(vision = true, images = false)).visible)
        assertTrue(AttachMenu.of(status(vision = false, images = true)).visible)
        assertTrue(AttachMenu.of(status(vision = true, images = true)).visible)
    }

    @Test
    fun `an older server or no status offers nothing`() {
        assertFalse("no status yet", AttachMenu.of(null).visible)
        assertFalse("a server with no capabilities", AttachMenu.of(ClusterStatus()).visible)
        assertFalse("capabilities without either", AttachMenu.of(ClusterStatus(capabilities = Capabilities())).visible)
        assertEquals(AttachMenu(photos = false, camera = false, createImage = false), AttachMenu.of(null))
    }

    // ------------------------------------------------------------------ the photo allowance

    @Test
    fun `the hour's photo allowance is the 429 with code image_quota, and nothing else`() {
        val words = "That's more photos than this network can send in an hour (30). You can send more in about 12 minutes."
        assertTrue(isPhotoQuota(ApiException.Http(429, words, 720, "image_quota")))
        assertTrue("with no Retry-After", isPhotoQuota(ApiException.Http(429, words, null, "image_quota")))
        // asking too fast pauses Send, as it always did
        assertFalse(isPhotoQuota(ApiException.Http(429, "One answer at a time.", 5, null)))
        assertFalse(isPhotoQuota(ApiException.Http(429, "Busy.", 5, "image_busy")))
        // a photo the server refuses outright is not the allowance
        assertFalse(isPhotoQuota(ApiException.Http(400, "An image is over 4 MiB.", null, "image_too_large")))
        assertFalse(isPhotoQuota(ApiException.Http(400, words, null, "image_quota")))
    }

    // ------------------------------------------------------------------ a request the server refused for its photos

    private val quota = ApiException.Http(429, "That's more photos than this network can send in an hour (30).", 720, "image_quota")

    private fun sent(vararg images: String) = listOf(WireMessage("user", "Look", images.toList()))

    private fun turn(id: String, text: String, vararg photos: String) =
        ChatMessage(id, Role.USER, text, createdAt = 0, photos = photos.map { Photo("$it.jpg", 10, 10) })

    @Test
    fun `every code the server gives for a photo it will not take is a photo refusal`() {
        assertTrue(isPhotoRefusal(quota))
        for (code in listOf("image_too_large", "image_input_unavailable", "image_whatever", "too_many_images", "unsupported_image_type", "remote_image_url")) {
            assertTrue(code, isPhotoRefusal(ApiException.Http(400, "No.", null, code)))
        }
    }

    @Test
    fun `nothing else is, not a rate limit, a server error, a missing endpoint or a refusal with no code`() {
        assertFalse(isPhotoRefusal(ApiException.Http(429, "One answer at a time.", 5, null)))
        assertFalse(isPhotoRefusal(ApiException.Http(429, "Busy.", 5, "image_busy")))
        assertFalse(isPhotoRefusal(ApiException.Http(400, "A field is missing.", null, null)))
        assertFalse(isPhotoRefusal(ApiException.Http(400, "Bad.", null, "bad_request")))
        assertFalse(isPhotoRefusal(ApiException.Http(404, "No chat here.", null, null)))
        assertFalse(isPhotoRefusal(ApiException.Http(503, "Restarting.", null, "image_input_unavailable")))
        assertFalse(isPhotoRefusal(ApiException.Http(500, "Oops.", null, null)))
    }

    @Test
    fun `a refusal marks the message that carried the pictures, the newest with photos`() {
        val chat = listOf(turn("a", "old", "x"), turn("b", "new", "y", "z"), turn("c", "and then words"))
        assertEquals("b", refusedPhotoTurn(chat, sent("data:image/jpeg;base64,y", "data:image/jpeg;base64,z"), quota))
        assertEquals("b", refusedPhotoTurn(chat, sent("data:image/jpeg;base64,y"), ApiException.Http(400, "Not a PNG or JPEG.", null, "unsupported_image_type")))
    }

    @Test
    fun `a request that carried no pictures cannot have been refused for them`() {
        val chat = listOf(turn("a", "look", "x"), turn("b", "and words"))
        // the photos were already marks (trimmed away, or refused before), so nothing in the request was a picture
        assertNull(refusedPhotoTurn(chat, listOf(WireMessage("user", "look\n\n[photo]\n\nand words")), quota))
        assertNull(refusedPhotoTurn(chat, emptyList(), quota))
    }

    @Test
    fun `a failure that says nothing about the photos marks nothing`() {
        val chat = listOf(turn("a", "look", "x"))
        val carried = sent("data:image/jpeg;base64,x")
        assertNull(refusedPhotoTurn(chat, carried, ApiException.Http(429, "One answer at a time.", 5, null)))
        assertNull(refusedPhotoTurn(chat, carried, ApiException.Http(500, "Oops.", null, null)))
        assertNull(refusedPhotoTurn(chat, carried, ApiException.Http(404, "No chat here.", null, null)))
    }

    @Test
    fun `with no message holding photos there is none to mark`() {
        val words = listOf(ChatMessage("a", Role.USER, "just words", createdAt = 0))
        assertNull(refusedPhotoTurn(words, sent("data:image/jpeg;base64,x"), quota))
    }

    // ------------------------------------------------------------------ the photos in the box

    private fun photos(ready: Int, loading: Int = 0) =
        DraftPhotos(List(ready) { DraftPhoto("p$it", byteArrayOf(1), 10, 10) }, List(loading) { "l$it" })

    @Test
    fun `four photos fit in a message, counting those still being prepared`() {
        assertEquals(4, DraftPhotos().room)
        assertEquals(3, photos(ready = 1).room)
        assertEquals(1, photos(ready = 2, loading = 1).room)
        assertEquals(0, photos(ready = 4).room)
        assertEquals(0, photos(ready = 3, loading = 1).room)
    }

    @Test
    fun `there is never negative room`() {
        assertEquals(0, photos(ready = 3, loading = 3).room)
    }

    @Test
    fun `an empty box has no photos, a loading photo counts as one`() {
        assertTrue(DraftPhotos().isEmpty)
        assertFalse(photos(ready = 0, loading = 1).isEmpty)
        assertEquals(3, photos(ready = 2, loading = 1).count)
    }

    @Test
    fun `send needs words or a ready photo, and a photo still being prepared does not hold it up`() {
        assertFalse("nothing to send", DraftPhotos().canSend(hasText = false))
        assertTrue("text alone", DraftPhotos().canSend(hasText = true))
        assertTrue("photos alone", photos(ready = 1).canSend(hasText = false))
        assertTrue("photos and text", photos(ready = 2).canSend(hasText = true))
        // a photo that never gets ready must not block the box
        assertTrue("words, and a photo still being prepared", photos(ready = 0, loading = 1).canSend(hasText = true))
        assertTrue("words, a ready photo and one being prepared", photos(ready = 1, loading = 1).canSend(hasText = true))
        assertTrue("the ready photo goes", photos(ready = 1, loading = 1).canSend(hasText = false))
        assertFalse("only a photo being prepared, and no words", photos(ready = 0, loading = 1).canSend(hasText = false))
    }

    @Test
    fun `each photo being prepared has its own place in the box, so one can be taken out`() {
        val box = photos(ready = 1, loading = 2)
        assertEquals(listOf("l0", "l1"), box.preparing)
        assertEquals(2, box.loading)
        val without = box.copy(preparing = box.preparing - "l0")
        assertEquals(listOf("l1"), without.preparing)
        assertEquals(2, without.count)
        assertEquals(2, without.room)
    }
}
