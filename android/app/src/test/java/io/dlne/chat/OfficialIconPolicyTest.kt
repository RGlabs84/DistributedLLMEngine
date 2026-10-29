package io.dlne.chat

import java.io.ByteArrayOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.zip.Inflater
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import org.w3c.dom.Element

/**
 * There is exactly one DLNE icon: vscode-extension/media/dlne_logo.png (the blue-and-pink
 * brain with "DLNE" under it). Every launcher icon, in-app mark and Play graphic must come
 * from it through android/tools/generate_dlne_icons.py, and no other drawn, vector or
 * bitmap DLNE mark may exist in the app.
 *
 * These tests fail if someone adds another launcher icon, a drawable that is not a plain
 * toolbar glyph, a mark drawn in code, or edits a generated file by hand.
 */
class OfficialIconPolicyTest {
    // Gradle runs unit tests with the module directory (android/app) as the working directory.
    private val appDir = File(System.getProperty("user.dir")!!)
    private val androidDir = appDir.parentFile!!
    private val repoRoot = androidDir.parentFile!!
    private val mainDir = File(appDir, "src/main")
    private val resDir = File(mainDir, "res")
    private val officialLogo = File(repoRoot, "vscode-extension/media/dlne_logo.png")

    /** The only drawables that are not icons of DLNE: plain functional glyphs (send, back...). */
    private val actionGlyphs = setOf(
        "ic_add", "ic_back", "ic_copy", "ic_delete", "ic_flag", "ic_image", "ic_info",
        "ic_menu", "ic_open_in_new", "ic_send", "ic_settings", "ic_stop",
        // The "+" menu and the photo thumbnails: camera, create image, remove (x).
        "ic_camera", "ic_sparkle", "ic_close",
    )

    /** Files generated from the official logo, relative to src/main/res. */
    private val generatedResources = setOf(
        "drawable-nodpi/dlne_logo.png",
        "drawable-nodpi/ic_launcher_monochrome.png",
        "mipmap-anydpi/ic_launcher.xml",
    )

    private val imageExtensions = setOf("png", "webp", "jpg", "jpeg", "gif", "svg", "ico", "bmp", "avif", "heic")

    private fun relative(base: File, file: File) = file.relativeTo(base).invariantSeparatorsPath

    private fun sha256(file: File): String =
        MessageDigest.getInstance("SHA-256").digest(file.readBytes()).joinToString("") { "%02x".format(it) }

    private fun xml(file: File): Element =
        DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(file).documentElement

    private fun child(parent: Element, tag: String): Element? {
        val list = parent.getElementsByTagName(tag)
        return if (list.length > 0) list.item(0) as Element else null
    }

    @Test
    fun `the official logo is where the build expects it`() {
        assertTrue("official logo missing: $officialLogo", officialLogo.isFile)
        val logo = Png.read(officialLogo)
        assertEquals(512, logo.width)
        assertEquals(512, logo.height)
    }

    @Test
    fun `launcher icons and drawables are only the official logo's files and plain glyphs`() {
        val found = mutableSetOf<String>()
        resDir.listFiles().orEmpty()
            .filter { it.isDirectory && (it.name.startsWith("drawable") || it.name.startsWith("mipmap")) }
            .forEach { dir -> dir.walkTopDown().filter { it.isFile }.forEach { found += relative(resDir, it) } }

        val expected = generatedResources + actionGlyphs.map { "drawable/$it.xml" }
        val unexpected = found - expected
        val missing = expected - found
        assertTrue(
            "Only the official DLNE logo (and files generated from it by android/tools/generate_dlne_icons.py) " +
                "may be a launcher icon or drawable. Remove these permanently: $unexpected",
            unexpected.isEmpty(),
        )
        assertTrue("Expected icon files are missing: $missing", missing.isEmpty())
    }

    @Test
    fun `no other image file exists anywhere in the app sources`() {
        val allowed = generatedResources.filter { it.endsWith(".png") }.map { "res/$it" }.toSet()
        val images = mainDir.walkTopDown()
            .filter { it.isFile && it.extension.lowercase() in imageExtensions }
            .map { relative(mainDir, it) }
            .toSet()
        assertEquals("Image files other than the official logo's: ${images - allowed}", allowed, images)
    }

    @Test
    fun `code only references the logo, plain glyphs and the one launcher icon`() {
        val allowedDrawables = actionGlyphs + setOf("dlne_logo")
        val offenders = mutableListOf<String>()
        File(mainDir, "java").walkTopDown().filter { it.isFile && it.extension == "kt" }.forEach { file ->
            val text = file.readText()
            Regex("""R\.drawable\.(\w+)""").findAll(text).forEach { m ->
                val name = m.groupValues[1]
                if (name !in allowedDrawables) offenders += "${file.name}: R.drawable.$name"
            }
            Regex("""R\.mipmap\.(\w+)""").findAll(text).forEach { m ->
                if (m.groupValues[1] != "ic_launcher") offenders += "${file.name}: R.mipmap.${m.groupValues[1]}"
            }
            listOf("ImageVector", "rememberVectorPainter", "ic_brand_mark", "ic_launcher_foreground").forEach { word ->
                if (word in text) offenders += "${file.name}: $word (no drawn marks; use R.drawable.dlne_logo)"
            }
        }
        assertTrue("Drawn or unofficial marks in code: $offenders", offenders.isEmpty())
    }

    @Test
    fun `the in-app logo is the official file byte for byte`() {
        assertEquals(sha256(officialLogo), sha256(File(resDir, "drawable-nodpi/dlne_logo.png")))
    }

    @Test
    fun `the manifest uses the one launcher icon`() {
        val application = child(xml(File(mainDir, "AndroidManifest.xml")), "application")!!
        assertEquals("@mipmap/ic_launcher", application.getAttribute("android:icon"))
        assertEquals("@mipmap/ic_launcher", application.getAttribute("android:roundIcon"))
    }

    @Test
    fun `the adaptive icon is the logo at 66 percent on the logo's backdrop colour with a brain-only themed layer`() {
        val icon = xml(File(resDir, "mipmap-anydpi/ic_launcher.xml"))
        assertEquals("adaptive-icon", icon.tagName)
        assertEquals("@color/ic_launcher_background", child(icon, "background")!!.getAttribute("android:drawable"))
        val inset = child(child(icon, "foreground")!!, "inset")
        assertTrue("the foreground must be an <inset> of the official logo", inset != null)
        assertEquals("@drawable/dlne_logo", inset!!.getAttribute("android:drawable"))
        // 17% each side leaves 66% of the 108 dp layer: Android's adaptive-icon safe zone.
        assertEquals("17%", inset.getAttribute("android:inset"))
        assertEquals("@drawable/ic_launcher_monochrome", child(icon, "monochrome")!!.getAttribute("android:drawable"))
    }

    @Test
    fun `the launcher background colour is the logo's own backdrop`() {
        val colours = xml(File(resDir, "values/colors.xml")).getElementsByTagName("color")
        val value = (0 until colours.length).map { colours.item(it) as Element }
            .first { it.getAttribute("name") == "ic_launcher_background" }.textContent.trim()
        assertTrue("not #RRGGBB: $value", Regex("#[0-9A-Fa-f]{6}").matches(value))
        val expected = (0..2).map { value.substring(1 + it * 2, 3 + it * 2).toInt(16) }

        val logo = Png.read(officialLogo)
        val sum = DoubleArray(3)
        var n = 0
        for (y in 0 until 512) for (x in 0 until 512) {
            if (x >= 24 && x < 488 && y >= 24 && y < 488) continue // outer 24 px ring only
            sum[0] += logo.r(x, y)
            sum[1] += logo.g(x, y)
            sum[2] += logo.b(x, y)
            n++
        }
        for (c in 0..2) {
            assertTrue(
                "launcher background $value is not the logo's backdrop (channel $c: ${sum[c] / n} vs ${expected[c]})",
                kotlin.math.abs(sum[c] / n - expected[c]) <= 3.0,
            )
        }
    }

    @Test
    fun `the themed-icon layer is a brain-only alpha silhouette inside the safe zone`() {
        val mono = Png.read(File(resDir, "drawable-nodpi/ic_launcher_monochrome.png"))
        assertEquals(432, mono.width) // 108 dp at xxxhdpi
        assertEquals(432, mono.height)
        assertTrue("monochrome layer needs an alpha channel", mono.hasAlpha)
        val safeRadius = 66 / 2.0 * 432 / 108 // 66 dp circle, in px
        var lit = 0
        var farthest = 0.0
        for (y in 0 until 432) for (x in 0 until 432) {
            if (mono.a(x, y) > 5) {
                lit++
                farthest = maxOf(farthest, kotlin.math.hypot(x + 0.5 - 216, y + 0.5 - 216))
            }
        }
        val coverage = lit / (432.0 * 432.0)
        assertTrue("silhouette coverage $coverage is not a brain-sized shape", coverage in 0.08..0.35)
        assertTrue("silhouette reaches $farthest px, beyond the $safeRadius px safe zone", farthest <= safeRadius)
    }

    @Test
    fun `generated files match the recorded hashes of the official logo`() {
        val lines = File(androidDir, "tools/official-icon-hashes.txt").readLines()
            .filter { it.isNotBlank() && !it.startsWith("#") }
        val recorded = lines.associate { line ->
            val (hash, path) = line.split(Regex("\\s+"), limit = 2)
            path.trim() to hash
        }
        val mustBeListed = setOf(
            "vscode-extension/media/dlne_logo.png",
            "android/app/src/main/res/drawable-nodpi/dlne_logo.png",
            "android/app/src/main/res/drawable-nodpi/ic_launcher_monochrome.png",
            "android/play/icon-512.png",
            "android/play/feature-graphic.png",
        )
        assertEquals(mustBeListed, recorded.keys)
        for ((path, hash) in recorded) {
            val file = File(repoRoot, path)
            assertTrue("$path is missing", file.isFile)
            if (sha256(file) != hash) {
                fail(
                    "$path no longer matches the hash recorded by generate_dlne_icons.py; " +
                        "do not edit icon files by hand, change the official logo and regenerate",
                )
            }
        }
    }

    @Test
    fun `the Play icon is the official logo as a 512 px 32-bit PNG`() {
        val icon = Png.read(File(androidDir, "play/icon-512.png"))
        val logo = Png.read(officialLogo)
        assertEquals(512, icon.width)
        assertEquals(512, icon.height)
        assertEquals("32-bit means 8-bit RGBA (PNG colour type 6)", 6, icon.colorType)
        for (y in 0 until 512) for (x in 0 until 512) {
            val same = icon.r(x, y) == logo.r(x, y) && icon.g(x, y) == logo.g(x, y) && icon.b(x, y) == logo.b(x, y)
            if (!same) fail("pixel ($x,$y) differs from the official logo")
            if (icon.a(x, y) != 255) fail("pixel ($x,$y) is not opaque")
        }
    }

    @Test
    fun `the feature graphic is 1024 by 500 without alpha`() {
        val image = Png.read(File(androidDir, "play/feature-graphic.png"))
        assertEquals(1024, image.width)
        assertEquals(500, image.height)
        assertEquals("no alpha means PNG colour type 2 (RGB)", 2, image.colorType)
    }
}

/**
 * A small PNG reader (8-bit RGB or RGBA, not interlaced; anything else is refused) so the
 * tests need neither AWT nor an image library: the unit-test classpath is android.jar.
 */
private class Png private constructor(
    val width: Int,
    val height: Int,
    val colorType: Int,
    private val channels: Int,
    private val pixels: ByteArray,
) {
    val hasAlpha: Boolean get() = channels == 4

    private fun at(x: Int, y: Int, c: Int) = pixels[(y * width + x) * channels + c].toInt() and 0xFF
    fun r(x: Int, y: Int) = at(x, y, 0)
    fun g(x: Int, y: Int) = at(x, y, 1)
    fun b(x: Int, y: Int) = at(x, y, 2)
    fun a(x: Int, y: Int) = if (channels == 4) at(x, y, 3) else 255

    companion object {
        private fun int(b: ByteArray, at: Int) =
            (b[at].toInt() and 0xFF shl 24) or (b[at + 1].toInt() and 0xFF shl 16) or
                (b[at + 2].toInt() and 0xFF shl 8) or (b[at + 3].toInt() and 0xFF)

        fun read(file: File): Png {
            val bytes = file.readBytes()
            require(bytes.size > 33 && bytes[0] == 0x89.toByte() && String(bytes, 1, 3, Charsets.US_ASCII) == "PNG") {
                "${file.name} is not a PNG"
            }
            var width = 0
            var height = 0
            var depth = 0
            var colorType = 0
            var interlace = 0
            val idat = ByteArrayOutputStream()
            var pos = 8
            while (pos + 8 <= bytes.size) {
                val length = int(bytes, pos)
                val type = String(bytes, pos + 4, 4, Charsets.US_ASCII)
                val body = pos + 8
                if (type == "IHDR") {
                    width = int(bytes, body)
                    height = int(bytes, body + 4)
                    depth = bytes[body + 8].toInt()
                    colorType = bytes[body + 9].toInt()
                    interlace = bytes[body + 12].toInt()
                } else if (type == "IDAT") {
                    idat.write(bytes, body, length)
                } else if (type == "IEND") {
                    break
                }
                pos = body + length + 4
            }
            require(depth == 8 && interlace == 0 && (colorType == 2 || colorType == 6)) {
                "${file.name}: only 8-bit RGB/RGBA, non-interlaced PNGs are supported " +
                    "(depth $depth, colour type $colorType, interlace $interlace)"
            }
            val channels = if (colorType == 6) 4 else 3
            val stride = width * channels
            val raw = ByteArray(height * (stride + 1))
            val inflater = Inflater()
            inflater.setInput(idat.toByteArray())
            var filled = 0
            while (filled < raw.size) {
                val n = inflater.inflate(raw, filled, raw.size - filled)
                if (n == 0 && (inflater.finished() || inflater.needsInput())) break
                filled += n
            }
            inflater.end()
            require(filled == raw.size) { "${file.name}: truncated image data" }

            val out = ByteArray(height * stride)
            for (y in 0 until height) {
                val filter = raw[y * (stride + 1)].toInt()
                val row = y * (stride + 1) + 1
                for (i in 0 until stride) {
                    val x = raw[row + i].toInt() and 0xFF
                    val a = if (i >= channels) out[y * stride + i - channels].toInt() and 0xFF else 0
                    val b = if (y > 0) out[(y - 1) * stride + i].toInt() and 0xFF else 0
                    val c = if (y > 0 && i >= channels) out[(y - 1) * stride + i - channels].toInt() and 0xFF else 0
                    val value = when (filter) {
                        0 -> x
                        1 -> x + a
                        2 -> x + b
                        3 -> x + (a + b) / 2
                        4 -> {
                            val p = a + b - c
                            val pa = kotlin.math.abs(p - a)
                            val pb = kotlin.math.abs(p - b)
                            val pc = kotlin.math.abs(p - c)
                            x + if (pa <= pb && pa <= pc) a else if (pb <= pc) b else c
                        }
                        else -> throw IllegalArgumentException("${file.name}: bad PNG filter $filter")
                    }
                    out[y * stride + i] = value.toByte()
                }
            }
            return Png(width, height, colorType, channels, out)
        }
    }
}
