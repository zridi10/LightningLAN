package lightninglan.app

import android.content.Context
import lightninglan.core.PhonePageFiles
import java.io.IOException
import java.net.Inet4Address
import java.net.NetworkInterface

/*
 * The phone page: another phone on the same Wi-Fi (an iPhone, or an Android phone without the
 * app) scans the QR code in this app and uses LightningLAN in its browser, through this phone.
 * The page is served by the core (PhoneApi.kt); its files are the desktop app's, packed into the
 * app's assets (see app/build.gradle.kts).
 */

/** The page's files from the app's assets. */
class AssetPhonePage(context: Context) : PhonePageFiles {
    private val assets = context.applicationContext.assets

    override fun read(path: String): ByteArray? = try {
        assets.open("static/$path").use { it.readBytes() }
    } catch (e: IOException) {
        null
    }
}

// Interfaces another phone can't reach: mobile data, VPNs and the like.
private val UNREACHABLE = listOf("rmnet", "r_rmnet", "ccmni", "pdp", "seth", "v4-", "clat", "dummy", "tun", "ppp", "ipsec", "ifb")

/** This phone's addresses on Wi-Fi (or its own hotspot, or Ethernet): what another phone can open. Wi-Fi first. */
fun phoneAddresses(): List<String> = try {
    NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
        .filter { iface -> iface.isUp && !iface.isLoopback && !iface.isVirtual && UNREACHABLE.none { iface.name.startsWith(it) } }
        .sortedBy { if (it.name.startsWith("wlan")) 0 else 1 }
        .flatMap { it.inetAddresses.toList() }
        .filterIsInstance<Inet4Address>()
        .filter { !it.isLinkLocalAddress && !it.isLoopbackAddress }
        .mapNotNull { it.hostAddress }
        .distinct()
} catch (e: Exception) {
    emptyList()
}
