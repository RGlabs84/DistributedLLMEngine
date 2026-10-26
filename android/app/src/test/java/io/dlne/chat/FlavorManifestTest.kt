package io.dlne.chat

import java.io.File
import javax.xml.parsers.DocumentBuilderFactory
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element

/**
 * The Google Play build must have no way to update itself: no REQUEST_INSTALL_PACKAGES (a
 * restricted permission on Play), no installer receiver, no update code. The sideload build
 * (the APK on the website) must have exactly the permission and receiver the updater needs.
 *
 * It reads the merged release manifests that Gradle produces (build.gradle.kts hands their
 * paths in as the system properties dlne.manifest.play / dlne.manifest.sideload and merges them
 * before the tests run), so a permission pulled in by any library would show up too.
 */
class FlavorManifestTest {
    private val installPermission = "android.permission.REQUEST_INSTALL_PACKAGES"

    private fun manifest(flavor: String): Element {
        val path = System.getProperty("dlne.manifest.$flavor")
        assertNotNull("Gradle did not provide the merged $flavor manifest (dlne.manifest.$flavor)", path?.takeIf { it.isNotBlank() })
        val file = File(path!!)
        assertTrue("merged $flavor manifest not found at $file", file.isFile)
        return DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(file).documentElement
    }

    private fun Element.names(tag: String): List<String> {
        val nodes = getElementsByTagName(tag)
        return (0 until nodes.length).map { (nodes.item(it) as Element).getAttribute("android:name") }
    }

    @Test
    fun `the play build has no install permission and no updater receiver`() {
        val play = manifest("play")
        val permissions = play.names("uses-permission")
        assertFalse("play must not request $installPermission: $permissions", installPermission in permissions)
        assertTrue("play asks for internet", "android.permission.INTERNET" in permissions)
        assertEquals("nothing else in play's manifest mentions installing", emptyList<String>(), play.names("receiver").filter { "update" in it.lowercase() })
        assertFalse(play.names("receiver").any { it.contains("UpdateInstallReceiver") })
    }

    @Test
    fun `the play build asks for internet and nothing of its own beyond androidx's internal one`() {
        val permissions = manifest("play").names("uses-permission").toSet()
        val internal = setOf("io.dlne.chat.DYNAMIC_RECEIVER_NOT_EXPORTED_PERMISSION")
        assertEquals(setOf("android.permission.INTERNET"), permissions - internal)
    }

    @Test
    fun `the sideload build has the install permission and the unexported receiver`() {
        val sideload = manifest("sideload")
        val permissions = sideload.names("uses-permission")
        assertTrue("sideload needs $installPermission: $permissions", installPermission in permissions)
        assertTrue("android.permission.INTERNET" in permissions)
        val receivers = sideload.getElementsByTagName("receiver")
        val ours = (0 until receivers.length).map { receivers.item(it) as Element }
            .single { it.getAttribute("android:name") == "io.dlne.chat.update.UpdateInstallReceiver" }
        assertEquals("false", ours.getAttribute("android:exported"))
    }

    @Test
    fun `no source outside the sideload flavor mentions the installer or the manifest address`() {
        val appDir = File(System.getProperty("user.dir")!!)
        val forbidden = listOf("REQUEST_INSTALL_PACKAGES", "PackageInstaller", "android.json", "ACTION_MANAGE_UNKNOWN_APP_SOURCES")
        for (set in listOf("main", "play")) {
            File(appDir, "src/$set").walkTopDown().filter { it.isFile && it.extension in setOf("kt", "xml") }.forEach { file ->
                val text = file.readText()
                for (word in forbidden) {
                    // The comment in src/play/UpdateHooks.kt names the permission to explain its absence.
                    val code = text.lines().filterNot { it.trim().startsWith("*") || it.trim().startsWith("//") || it.trim().startsWith("/*") }
                    assertFalse("$file must not use $word", code.any { word in it })
                }
            }
        }
    }
}
