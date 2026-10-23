package lightninglan.app

import android.content.Context
import android.net.Uri
import lightninglan.core.randomName
import lightninglan.core.toHex
import java.util.UUID

/** What the app remembers (the desktop app keeps the same things in config.json). */
class AppSettings(context: Context) {
    private val prefs = context.getSharedPreferences("settings", Context.MODE_PRIVATE)

    val peerId: String = prefs.getString("peer_id", null)?.takeIf { PEER_ID_RE.matches(it) }
        ?: UUID.randomUUID().toString().replace("-", "").also { prefs.edit().putString("peer_id", it).apply() }

    /** A funny name until one is picked, remembered from the start. */
    var name: String
        get() = prefs.getString("name", null) ?: randomName().also { name = it }
        set(value) = prefs.edit().putString("name", value).apply()

    var passphrase: String?
        get() = prefs.getString("passphrase", null)
        set(value) = prefs.edit().putString("passphrase", value).apply()

    /** The passphrase's key, kept so it isn't derived again (slowly) on every start. */
    var passphraseKey: ByteArray?
        get() = prefs.getString("passphrase_key", null)?.let(::hexToBytes)
        set(value) = prefs.edit().putString("passphrase_key", value?.toHex()).apply()

    /** The shared folder and the downloads folder: document URIs inside folders the person granted us. */
    var shareFolder: Uri?
        get() = prefs.getString("share_folder", null)?.let(Uri::parse)
        set(value) = prefs.edit().putString("share_folder", value?.toString()).apply()

    var downloadsFolder: Uri?
        get() = prefs.getString("downloads_folder", null)?.let(Uri::parse)
        set(value) = prefs.edit().putString("downloads_folder", value?.toString()).apply()

    /** Files shared from where they are (picked with "Share files"), not copied into the shared folder. */
    var links: List<Uri>
        get() = prefs.getString("links", null)?.split('\n')?.filter { it.isNotEmpty() }?.map(Uri::parse) ?: emptyList()
        set(value) = prefs.edit().putString("links", value.joinToString("\n")).apply()

    /** The colour theme's key ("auto", "light", "dark", "synthwave", "bliss" or "deepsea"). */
    var theme: String
        get() = prefs.getString("theme", null) ?: "auto"
        set(value) = prefs.edit().putString("theme", value).apply()

    /** The best score in the solo falling-blocks game. */
    var blocksBest: Int
        get() = prefs.getInt("blocks_best", 0)
        set(value) = prefs.edit().putInt("blocks_best", value).apply()

    val setupDone: Boolean get() = shareFolder != null && downloadsFolder != null

    private companion object {
        val PEER_ID_RE = Regex("^[0-9a-f]{32}$")

        fun hexToBytes(hex: String): ByteArray? {
            if (hex.length % 2 != 0) return null
            return ByteArray(hex.length / 2) { i -> hex.substring(2 * i, 2 * i + 2).toIntOrNull(16)?.toByte() ?: return null }
        }
    }
}
