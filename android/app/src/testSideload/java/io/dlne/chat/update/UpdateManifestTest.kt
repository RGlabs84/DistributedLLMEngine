package io.dlne.chat.update

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class UpdateManifestTest {
    private val hash = "a".repeat(64)

    private fun manifest(
        versionCode: String = "6",
        versionName: String = "\"1.0.5\"",
        minSdk: String = "26",
        url: String = "\"https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk\"",
        sha256: String = "\"$hash\"",
        size: String = "2017073",
        notes: String = "\"What changed.\"",
        published: String = "\"2026-10-27\"",
    ) = """{"versionCode": $versionCode, "versionName": $versionName, "minSdk": $minSdk,
        "url": $url, "sha256": $sha256, "size": $size, "notes": $notes, "published": $published}"""

    private fun valid(text: String): UpdateInfo {
        val result = UpdateManifestParser.parse(text)
        assertTrue("expected valid but was $result", result is ManifestResult.Valid)
        return (result as ManifestResult.Valid).info
    }

    private fun invalid(text: String, what: String = text) {
        val result = UpdateManifestParser.parse(text)
        assertTrue("expected invalid: $what, but was $result", result is ManifestResult.Invalid)
    }

    // ------------------------------------------------------------------ the good one

    @Test
    fun `the documented manifest is accepted field by field`() {
        val info = valid(manifest())
        assertEquals(6, info.versionCode)
        assertEquals("1.0.5", info.versionName)
        assertEquals(26, info.minSdk)
        assertEquals("https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk", info.url)
        assertEquals(hash, info.sha256)
        assertEquals(2017073L, info.size)
        assertEquals("What changed.", info.notes)
        assertEquals("2026-10-27", info.published)
    }

    @Test
    fun `notes and published are optional and unknown fields are ignored`() {
        val text = """{"versionCode":6,"versionName":"1.0.5","minSdk":26,
            "url":"https://dlne.rossgilsonlabs.dev/downloads/a.apk","sha256":"$hash","size":2017073,"extra":{"x":1}}"""
        val info = valid(text)
        assertEquals("", info.notes)
        assertNull(info.published)
    }

    @Test
    fun `an upper-case hash is accepted and normalised to lower case`() {
        assertEquals(hash, valid(manifest(sha256 = "\"${hash.uppercase()}\"")).sha256)
    }

    @Test
    fun `notes are flattened to one plain line without hostile characters`() {
        val info = valid(manifest(notes = "\"Fixes\\n\\tthings \\u202eevil\\u202c and more\""))
        assertEquals("Fixes things evil and more", info.notes)
        val long = valid(manifest(notes = "\"" + "x".repeat(1500) + "\""))
        assertEquals(UpdateSource.MAX_NOTES_CHARS, long.notes.length)
    }

    // ------------------------------------------------------------------ not a manifest

    @Test
    fun `things that are not a manifest are rejected`() {
        invalid("")
        invalid("not json")
        invalid("[]")
        invalid("\"x\"")
        invalid("{}")
        invalid("""{"versionCode":6""")
        invalid(manifest() + " trailing")
        invalid(" ".repeat(UpdateSource.MAX_MANIFEST_BYTES + 1) + manifest())
    }

    @Test
    fun `numbers must be plain JSON integers in range`() {
        invalid(manifest(versionCode = "\"6\""), "versionCode as a string")
        invalid(manifest(versionCode = "6.5"))
        invalid(manifest(versionCode = "6e0"))
        invalid(manifest(versionCode = "-6"))
        invalid(manifest(versionCode = "0"))
        invalid(manifest(versionCode = "2147483648"), "above Int")
        invalid(manifest(versionCode = "true"))
        invalid(manifest(versionCode = "null"))
        invalid(manifest(minSdk = "\"26\""))
        invalid(manifest(minSdk = "0"))
        invalid(manifest(minSdk = "9999"))
        invalid(manifest(size = "\"2017073\""))
        invalid(manifest(size = "2017073.0"))
    }

    @Test
    fun `a missing field is rejected`() {
        val full = manifest()
        for (field in listOf("versionCode", "versionName", "minSdk", "url", "sha256", "size")) {
            val without = full.replace("\"$field\"", "\"x_$field\"")
            invalid(without, "without $field")
        }
    }

    @Test
    fun `version name and published have a strict shape`() {
        invalid(manifest(versionName = "\"\""))
        invalid(manifest(versionName = "\"1.0.5 beta\""))
        invalid(manifest(versionName = "\"<b>1</b>\""))
        invalid(manifest(versionName = "\"" + "1".repeat(40) + "\""))
        invalid(manifest(versionName = "105"))
        invalid(manifest(published = "\"27 Oct 2026\""))
        invalid(manifest(published = "\"2026-13-40\""))
        invalid(manifest(published = "20261027"))
        invalid(manifest(notes = "42"))
        invalid(manifest(notes = "\"" + "x".repeat(2500) + "\""))
    }

    // ------------------------------------------------------------------ the hash and the size

    @Test
    fun `a bad hash is rejected`() {
        invalid(manifest(sha256 = "\"${"a".repeat(63)}\""), "63 digits")
        invalid(manifest(sha256 = "\"${"a".repeat(65)}\""), "65 digits")
        invalid(manifest(sha256 = "\"${"g".repeat(64)}\""), "not hex")
        invalid(manifest(sha256 = "\" ${"a".repeat(63)}\""), "leading space")
        invalid(manifest(sha256 = "\"sha256:$hash\""))
        invalid(manifest(sha256 = "12345"))
        invalid(manifest(sha256 = "\"\""))
    }

    @Test
    fun `an implausible size is rejected`() {
        invalid(manifest(size = "0"))
        invalid(manifest(size = "1"))
        invalid(manifest(size = "99999"), "smaller than any app")
        invalid(manifest(size = "${UpdateSource.MAX_APK_BYTES}"), "100 MB is already too much")
        invalid(manifest(size = "${UpdateSource.MAX_APK_BYTES + 1}"))
        invalid(manifest(size = "9999999999"))
        invalid(manifest(size = "99999999999"), "eleven digits")
        valid(manifest(size = "${UpdateSource.MAX_APK_BYTES - 1}"))
        valid(manifest(size = "${UpdateSource.MIN_APK_BYTES}"))
    }

    // ------------------------------------------------------------------ the address

    private val official = "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk"

    @Test
    fun `the official address is canonical`() {
        assertEquals(official, UpdateManifestParser.validateUrl(official))
        assertEquals(official, UpdateManifestParser.validateUrl("HTTPS://DLNE.RossGilsonLabs.dev/downloads/DLNE-Chat-1.0.5.apk"))
        assertEquals(official, UpdateManifestParser.validateUrl("https://dlne.rossgilsonlabs.dev:443/downloads/DLNE-Chat-1.0.5.apk"))
    }

    @Test
    fun `plain http and other schemes are rejected`() {
        for (url in listOf(
            "http://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "ftp://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "file:///sdcard/DLNE-Chat-1.0.5.apk",
            "//dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "/downloads/DLNE-Chat-1.0.5.apk",
            "dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
        )) assertNull(url, UpdateManifestParser.validateUrl(url))
    }

    @Test
    fun `any other host is rejected`() {
        for (url in listOf(
            "https://evil.example/downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.duckdns.org/downloads/DLNE-Chat-1.0.5.apk",
            "https://73-215-216-195.sslip.io/downloads/DLNE-Chat-1.0.5.apk",
            "https://rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "https://www.dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "https://a.dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev.evil.example/downloads/DLNE-Chat-1.0.5.apk",
            "https://evil.example/dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev./downloads/DLNE-Chat-1.0.5.apk",
            "https://localhost/downloads/DLNE-Chat-1.0.5.apk",
            "https://127.0.0.1/downloads/DLNE-Chat-1.0.5.apk",
        )) assertNull(url, UpdateManifestParser.validateUrl(url))
    }

    @Test
    fun `redirect-style and disguised addresses are rejected`() {
        for (url in listOf(
            "https://dlne.rossgilsonlabs.dev@evil.example/downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev:pw@evil.example/downloads/DLNE-Chat-1.0.5.apk",
            "https://user@dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk?next=https://evil.example/x.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk?",
            "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk#frag",
            "https://dlne.rossgilsonlabs.dev/redirect?url=https://evil.example/x.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/../x.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/%2e%2e/x.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/%2E%2E%2Fx.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/..%2Fx.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/a%2Fb.apk",
            "https://dlne.rossgilsonlabs.dev/downloads\\..\\x.apk",
            "https://dlne.rossgilsonlabs.dev/downloads//evil.example/x.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/sub/x.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/DLNE..Chat.apk",
            "https://dlne.rossgilsonlabs.dev:8443/downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev:444/downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev/download/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev/Downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/",
            "https://dlne.rossgilsonlabs.dev/downloads/android.json",
            "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk ",
            " https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/DLNE Chat.apk",
            "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk\n",
            "https://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk/extra",
            "https://dlne.rossgilsonlabs.dev/downloads/" + "a".repeat(400) + ".apk",
        )) assertNull(url, UpdateManifestParser.validateUrl(url))
    }

    @Test
    fun `a manifest whose url is rejected is rejected whole`() {
        invalid(manifest(url = "\"https://evil.example/downloads/DLNE-Chat-1.0.5.apk\""))
        invalid(manifest(url = "\"http://dlne.rossgilsonlabs.dev/downloads/DLNE-Chat-1.0.5.apk\""))
        invalid(manifest(url = "\"https://dlne.rossgilsonlabs.dev/downloads/x.apk?go=https://evil.example\""))
        invalid(manifest(url = "42"))
        assertNotNull(valid(manifest()))
    }

    // ------------------------------------------------------------------ is it an update?

    private fun info(versionCode: Int = 6, minSdk: Int = 26) =
        valid(manifest(versionCode = "$versionCode", minSdk = "$minSdk"))

    @Test
    fun `only a strictly higher versionCode is an update`() {
        assertTrue(decideUpdate(info(6), installedVersionCode = 5, deviceSdk = 34) is UpdateDecision.Available)
        assertEquals(UpdateDecision.UpToDate, decideUpdate(info(5), installedVersionCode = 5, deviceSdk = 34))
        assertEquals(UpdateDecision.UpToDate, decideUpdate(info(4), installedVersionCode = 5, deviceSdk = 34))
        assertTrue(decideUpdate(info(1000), installedVersionCode = 999, deviceSdk = 34) is UpdateDecision.Available)
    }

    @Test
    fun `a minSdk above the phone's is not for this phone`() {
        assertTrue(decideUpdate(info(6, minSdk = 35), installedVersionCode = 5, deviceSdk = 34) is UpdateDecision.NeedsNewerAndroid)
        assertTrue(decideUpdate(info(6, minSdk = 34), installedVersionCode = 5, deviceSdk = 34) is UpdateDecision.Available)
        assertTrue(decideUpdate(info(6, minSdk = 26), installedVersionCode = 5, deviceSdk = 26) is UpdateDecision.Available)
        // Not newer wins over minSdk: there is nothing to offer either way.
        assertEquals(UpdateDecision.UpToDate, decideUpdate(info(5, minSdk = 35), installedVersionCode = 5, deviceSdk = 34))
    }
}
