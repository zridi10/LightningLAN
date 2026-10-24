package lightninglan.core

import java.net.URLDecoder
import java.net.URLEncoder

/*
 * Two phones with the app and no Wi-Fi in common: one makes a hotspot and hands the other a join
 * link, as one QR code or with an NFC tap (NdefTag.kt). The link says how to join the hotspot and
 * where this peer is on it, so the other phone needs no discovery: it joins the Wi-Fi, polls this
 * peer, and introduces itself (POST /api/introduce) so this one polls it back.
 *
 *   https://lightninglan.party/join/#v=1&s=<Wi-Fi>&p=<password>&h=<address>&o=<port>&i=<peer id>&n=<name>
 *
 * (w=sae: the hotspot takes WPA3 only; a=1: this peer uses a passphrase.) The details are in the
 * fragment, which browsers never send to a server: on a phone without the app the link opens the
 * website's join page, which says to get the app.
 */

const val JOIN_LINK_PREFIX = "https://lightninglan.party/join/"

/** The links the app opens: the website's, with or without the slash, and the scheme the join page's button uses. */
private val JOIN_LINK_BASES = setOf(JOIN_LINK_PREFIX, "https://lightninglan.party/join", "lightninglan://join", "lightninglan://join/")
private val IPV4_RE = Regex("^(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)(\\.(25[0-5]|2[0-4]\\d|1\\d\\d|[1-9]?\\d)){3}$")
private val PEER_ID = Regex("^[0-9a-f]{32}$")

internal fun isIpv4(text: String): Boolean = IPV4_RE.matches(text)

internal fun isPeerId(text: String): Boolean = PEER_ID.matches(text)
private const val NAME_IN_LINK = 40 // characters: keeps the QR code small

data class JoinLink(
    val ssid: String,
    /** The hotspot's password; null for an open one. */
    val password: String?,
    /** The hotspot takes WPA3 only (most take WPA2 too). */
    val wpa3Only: Boolean,
    val address: String,
    val port: Int,
    val peerId: String,
    val name: String,
    /** That peer uses a passphrase: this one needs the same. */
    val passphrase: Boolean,
) {
    fun toUri(): String = JOIN_LINK_PREFIX + "#" + listOfNotNull(
        "v=1",
        "s=" + encode(ssid),
        password?.let { "p=" + encode(it) },
        "w=sae".takeIf { wpa3Only },
        "h=$address",
        "o=$port",
        "i=$peerId",
        "n=" + encode(name.take(NAME_IN_LINK)),
        "a=1".takeIf { passphrase },
    ).joinToString("&")

    companion object {
        /** The link from a QR code or an NFC tag; null if it isn't one, or something in it is wrong. */
        fun parse(uri: String): JoinLink? {
            val hash = uri.indexOf('#')
            if (hash < 0 || uri.substring(0, hash) !in JOIN_LINK_BASES) return null
            val fields = HashMap<String, String>()
            for (pair in uri.substring(hash + 1).split('&')) {
                val eq = pair.indexOf('=')
                if (eq <= 0) continue
                fields.putIfAbsent(pair.substring(0, eq), decode(pair.substring(eq + 1)) ?: return null)
            }
            if (fields["v"] != "1") return null
            val ssid = fields["s"]?.takeIf { it.isNotEmpty() && it.toByteArray(Charsets.UTF_8).size <= 32 } ?: return null
            val password = fields["p"]
            if (password != null && password.length !in 8..63) return null
            val address = fields["h"]?.takeIf(::isIpv4) ?: return null
            val port = fields["o"]?.toIntOrNull()?.takeIf { it in 1..65535 } ?: return null
            val peerId = fields["i"]?.takeIf(::isPeerId) ?: return null
            val name = displayName(fields["n"] ?: "").trim().take(64).ifEmpty { peerId.take(8) }
            return JoinLink(ssid, password, fields["w"] == "sae", address, port, peerId, name, fields["a"] == "1")
        }

        private fun encode(text: String): String = URLEncoder.encode(text, "UTF-8")

        private fun decode(text: String): String? = try {
            URLDecoder.decode(text, "UTF-8")
        } catch (e: IllegalArgumentException) {
            null
        }
    }
}
