package lightninglan.core

/** What this peer announces on the network (mDNS/DNS-SD `_lightninglan._tcp`). */
data class Advert(val peerId: String, val name: String, val port: Int, val auth: Boolean, val version: String) {
    /** The DNS-SD instance name: the same on every platform, so peers recognise their own announcement. */
    val serviceName: String get() = "LightningLAN-$peerId"

    /** The TXT record, as lightninglan/discovery.py writes it. */
    fun txt(): Map<String, String> = mapOf(
        "id" to peerId,
        "name" to truncateUtf8(name, 200),
        "v" to version,
        "auth" to if (auth) "1" else "0",
    )
}

interface DiscoveryListener {
    fun serviceFound(record: ServiceRecord)
    fun serviceLost(serviceName: String)
}

/** Announces this peer and reports other peers as they appear and disappear (Android: NsdManager). */
interface Discovery {
    fun start(advert: Advert, listener: DiscoveryListener)

    /** Re-announce with a new name or passphrase flag. */
    fun update(advert: Advert)

    /** Say goodbye and stop listening. [start] may be called again afterwards. */
    fun stop()
}

private val PEER_ID_RE = Regex("^[0-9a-f]{32}$")

/** A discovered service's TXT record as a [ServiceRecord], or null if it isn't a usable LightningLAN peer. */
fun parseServiceRecord(serviceName: String, txt: Map<String, String>, addresses: List<String>, port: Int): ServiceRecord? {
    val peerId = txt["id"] ?: ""
    if (!PEER_ID_RE.matches(peerId) || port <= 0 || addresses.isEmpty()) return null
    val name = (txt["name"]?.takeIf { it.isNotEmpty() } ?: peerId.take(8)).take(64)
    return ServiceRecord(serviceName, peerId, name, addresses, port, txt["auth"] == "1")
}
