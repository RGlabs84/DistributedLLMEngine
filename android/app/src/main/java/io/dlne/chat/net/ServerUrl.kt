package io.dlne.chat.net

import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/** The DLNE server address: the public default, and validation for self-hosted ones. */
object ServerUrl {
    /** The public DLNE service, on the project's own name (1.0.4 on). */
    const val DEFAULT = "https://dlne.rossgilsonlabs.dev"

    /**
     * The address the public service had before it got a name (up to 1.0.0). It still works, so it
     * is still accepted as input ([normalize]), and an address saved by an earlier version of the
     * app is quietly replaced by [DEFAULT] at start-up ([startupAddress]).
     */
    const val LEGACY_DEFAULT = "https://73-215-216-195.sslip.io"

    /** The DuckDNS name 1.0.1-1.0.3 used. Same server, still answering; replaced like [LEGACY_DEFAULT]. */
    const val DUCKDNS_DEFAULT = "https://dlne.duckdns.org"

    private val OLD_DEFAULTS = setOf(LEGACY_DEFAULT, DUCKDNS_DEFAULT)

    /**
     * The address to use at start-up for [stored], the value saved in settings (null when
     * nothing is saved). Nothing saved, or a saved value that is no longer usable, means
     * [DEFAULT]; an old public address becomes [DEFAULT]; any other address (a self-hosted
     * server) is kept as it is.
     */
    fun startupAddress(stored: String?): String {
        val url = stored?.let(::normalize) ?: return DEFAULT
        return if (url in OLD_DEFAULTS) DEFAULT else url
    }

    /** True when [stored] is an old public address, which start-up replaces with [DEFAULT]. */
    fun isLegacyDefault(stored: String?): Boolean = stored?.let(::normalize) in OLD_DEFAULTS

    /**
     * Cleans up what someone typed into the server field, or returns null if it cannot be
     * used. A missing scheme means https; plain http is refused, because messages must not
     * travel unencrypted (the manifest also forbids cleartext traffic). A path is kept so a
     * server behind a reverse-proxy prefix works; a query, fragment or trailing slash is not.
     */
    fun normalize(input: String): String? {
        var text = input.trim()
        if (text.isEmpty() || text.any { it.isWhitespace() }) return null
        if (!text.contains("://")) text = "https://$text"
        val url = text.toHttpUrlOrNull() ?: return null
        if (!url.isHttps || url.host.isEmpty()) return null
        if (url.username.isNotEmpty() || url.password.isNotEmpty()) return null
        val clean = url.newBuilder().query(null).fragment(null).build().toString()
        return clean.trimEnd('/')
    }
}
