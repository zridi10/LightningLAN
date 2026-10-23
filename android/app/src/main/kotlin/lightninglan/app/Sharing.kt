package lightninglan.app

import android.app.Application
import android.content.ClipData
import android.content.ContentResolver
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.provider.DocumentsContract
import android.provider.OpenableColumns
import android.webkit.MimeTypeMap
import android.widget.Toast
import androidx.core.content.ContextCompat
import lightninglan.core.CommonFile
import lightninglan.core.CoreLog
import lightninglan.core.DownloadError
import lightninglan.core.DownloadFolder
import lightninglan.core.DownloadView
import lightninglan.core.Node
import lightninglan.core.NodeConfig
import lightninglan.core.NodeStatus
import lightninglan.core.PlayError
import lightninglan.core.PlayView
import lightninglan.core.GameView
import lightninglan.core.STOPPED
import lightninglan.core.deriveKey
import lightninglan.core.numberedNames
import lightninglan.core.randomName
import lightninglan.core.safeLocalFilename
import java.io.File
import java.io.IOException
import java.net.Inet4Address
import java.net.NetworkInterface
import java.util.concurrent.Executors

private const val TAG = "app"
private const val PORT = 8765

data class PeerView(val peerId: String, val name: String, val address: String, val files: Int, val reason: String?)

/** Everything the screens show, read in one go. */
data class UiSnapshot(
    val setupDone: Boolean,
    val status: NodeStatus?,
    val files: List<CommonFile>,
    val downloads: List<DownloadView>,
    val peers: List<PeerView>,
    val ignored: List<PeerView>,
    /** Ids of this phone's files that were shared one by one (they can be un-shared). */
    val linked: Set<String>,
    val busy: String?,
    val onWifi: Boolean,
    val problem: String?,
    val shareFolder: String?,
    val downloadsFolder: String?,
    val addresses: List<String>,
    /** While you wait: games, invitations and who can play. Null until sharing starts. */
    val play: PlayView?,
)

/**
 * The phone's peer and what the screens ask of it. The peer itself lives as long as the app's
 * process; [SharingService] keeps the process running in the background while it shares.
 */
class Sharing(private val app: Application) {
    val settings = AppSettings(app)
    private val control = Executors.newSingleThreadExecutor { Thread(it, "lightninglan-control") }
    private val actions = Executors.newCachedThreadPool { Thread(it, "lightninglan-action") }
    private val main = Handler(Looper.getMainLooper())

    @Volatile
    private var node: Node? = null

    @Volatile
    private var shareTree: PhoneShareTree? = null

    @Volatile
    private var busy: String? = null

    @Volatile
    private var problem: String? = null

    /** The app is on screen (then game invitations show in it, not as notifications). */
    @Volatile
    var appVisible = false

    /** Stop was pressed: opening the app again doesn't start sharing by itself. */
    @Volatile
    private var stoppedByPerson = false

    // ----- sharing on and off ------------------------------------------------------------------

    /** The app came to the front: share, unless Stop was pressed. */
    fun appOpened() {
        if (settings.setupDone && !stoppedByPerson && node?.online != true) start()
    }

    fun start() {
        stoppedByPerson = false
        problem = null
        ContextCompat.startForegroundService(app, Intent(app, SharingService::class.java))
    }

    fun stop() {
        stoppedByPerson = true
        control.execute { node?.goOffline(STOPPED) }
        app.stopService(Intent(app, SharingService::class.java))
    }

    /** Called by [SharingService] once it runs in the foreground: join the network. */
    internal fun serviceStarted() = control.execute {
        try {
            val n = node ?: createNode().also {
                it.start() // listens, indexes and joins the network
                node = it
            }
            n.goOnline()
        } catch (e: IOException) {
            CoreLog.e(TAG, "could not start sharing", e)
            problem = "Could not start sharing: ${e.message}"
            app.stopService(Intent(app, SharingService::class.java))
        }
    }

    private fun createNode(): Node {
        val tree = PhoneShareTree(app, settings.shareFolder, settings.links)
        shareTree = tree
        val config = NodeConfig(
            peerId = settings.peerId,
            name = settings.name,
            version = BuildConfig.VERSION_NAME,
            stateDir = app.filesDir,
            port = PORT,
            passphraseKey = settings.passphraseKey,
            nameGenerated = settings.nameGenerated,
        )
        return Node(config, tree, settings.downloadsFolder?.let(::downloadFolderAt), NsdDiscovery(app)).also {
            it.onRenamed = { name -> // another device had the same made-up name
                settings.name = name
                settings.nameGenerated = true
            }
        }
    }

    private fun downloadFolderAt(folder: Uri): DownloadFolder =
        folder.localFolder()?.let { LocalDownloadFolder(app, it) } ?: SafDownloadFolder(app, folder)

    // ----- what the screens show ---------------------------------------------------------------

    fun status(): NodeStatus? = node?.status()

    fun snapshot(): UiSnapshot {
        val n = node
        val entries = n?.indexer?.snapshot?.entries ?: emptyList()
        return UiSnapshot(
            setupDone = settings.setupDone,
            status = n?.status(),
            files = n?.commonFiles() ?: emptyList(),
            downloads = n?.downloads?.list() ?: emptyList(),
            peers = n?.peers?.online()?.map { PeerView(it.peerId, it.name, "${it.address ?: it.addresses.firstOrNull()}:${it.port}", it.files.size, null) }
                ?: emptyList(),
            ignored = n?.peers?.ignored()?.map { PeerView(it.peerId, it.name, "${it.addresses.firstOrNull()}:${it.port}", 0, it.ignoreReason) }
                ?: emptyList(),
            linked = entries.filter { it.key.startsWith("link:") }.map { it.sha256 }.toSet(),
            busy = busy,
            onWifi = onLocalNetwork(),
            problem = problem,
            shareFolder = settings.shareFolder?.let(::folderLabel),
            downloadsFolder = settings.downloadsFolder?.let(::folderLabel),
            addresses = localAddresses(),
            play = n?.arcade?.view(),
        )
    }

    private fun onLocalNetwork(): Boolean {
        val cm = app.getSystemService(ConnectivityManager::class.java) ?: return false
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) || caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
    }

    private fun localAddresses(): List<String> = try {
        NetworkInterface.getNetworkInterfaces()?.toList().orEmpty()
            .filter { it.isUp && !it.isLoopback }
            .flatMap { it.inetAddresses.toList() }
            .filterIsInstance<Inet4Address>()
            .filter { !it.isLinkLocalAddress }
            .mapNotNull { it.hostAddress }
    } catch (e: Exception) {
        emptyList()
    }

    // ----- downloads ---------------------------------------------------------------------------

    fun download(fileId: String, peerId: String? = null) = act {
        node?.downloads?.start(fileId, peerId)
    }

    fun pause(id: String) = act { node?.downloads?.pause(id) }
    fun resume(id: String) = act { node?.downloads?.resume(id) }
    fun cancel(id: String) = act { node?.downloads?.cancel(id) }
    fun remove(id: String) = act { node?.downloads?.remove(id) }
    fun clearFinished() = act { node?.downloads?.clearFinished() }

    private fun act(block: () -> Unit) = actions.execute {
        try {
            block()
        } catch (e: DownloadError) {
            toast(e.message ?: "That didn't work.")
        } catch (e: Exception) {
            CoreLog.e(TAG, "download action failed", e)
            toast("That didn't work: ${e.message}")
        }
    }

    // ----- While you wait: head-to-head games ------------------------------------------------------

    /** Runs a game action in the background; [done] gets the game to show, on the main thread. */
    private fun playAction(done: ((GameView) -> Unit)? = null, block: (lightninglan.core.Arcade) -> GameView?) = actions.execute {
        val arcade = node?.arcade
        if (arcade == null) {
            toast("Start sharing first.")
            return@execute
        }
        try {
            val game = block(arcade)
            if (game != null && done != null) main.post { done(game) }
        } catch (e: PlayError) {
            toast(e.message ?: "That didn't work.")
        } catch (e: Exception) {
            CoreLog.e(TAG, "game action failed", e)
            toast("That didn't work: ${e.message}")
        }
    }

    fun invite(peerId: String, kind: String, bestOf: Int, done: (GameView) -> Unit) =
        playAction(done) { it.invite(peerId, kind, bestOf) }

    fun answer(gameId: String, accept: Boolean, done: (GameView) -> Unit) = playAction(done) { it.answer(gameId, accept) }
    fun move(gameId: String, cell: Int) = playAction { it.move(gameId, cell) }
    fun movePath(gameId: String, path: List<Int>) = playAction { it.movePath(gameId, path) }
    fun shuffleFleet(gameId: String) = playAction { it.shuffleFleet(gameId) }
    fun fleetReady(gameId: String) = playAction { it.fleetReady(gameId) }
    fun fire(gameId: String, cell: Int) = playAction { it.fire(gameId, cell) }
    fun finish(gameId: String, timeMs: Int) = playAction { it.finish(gameId, timeMs) }
    fun choose(gameId: String, choice: String) = playAction { it.choose(gameId, choice) }
    fun resign(gameId: String) = playAction { it.resign(gameId) }
    fun withdraw(gameId: String) = playAction { it.withdraw(gameId); null }
    fun closeGame(gameId: String) = playAction { it.close(gameId); null }
    fun rematch(gameId: String, done: (GameView) -> Unit) = playAction(done) { it.rematch(gameId) }

    /** Invitations waiting for an answer (for the notification while the app is in the background). */
    fun openInvites() = node?.arcade?.view()?.invites ?: emptyList()

    // ----- this phone's files -------------------------------------------------------------------

    /**
     * First run: makes the Shared and Downloads folders (Download/LightningLAN on the phone, like
     * the desktop app's ~/LightningLAN) and starts sharing. Nothing to choose; Settings can change them.
     */
    fun setUp() = control.execute {
        if (settings.setupDone) return@execute
        try {
            val (share, downloads) = makeDefaultFolders(app)
            settings.shareFolder = Uri.fromFile(share)
            settings.downloadsFolder = Uri.fromFile(downloads)
            CoreLog.i(TAG, "made the folders in ${share.parent}")
            node?.let {
                applyShareTree(it)
                it.setDownloadFolder(LocalDownloadFolder(app, downloads))
            }
            main.post { start() }
        } catch (e: Exception) {
            CoreLog.e(TAG, "making the folders failed", e)
            problem = "Could not make the LightningLAN folders: ${e.message}"
        }
    }

    fun changeShareFolder(tree: Uri) = control.execute {
        try {
            keepAccess(tree, write = true)
            settings.shareFolder = treeFolder(tree)
            node?.let(::applyShareTree)
        } catch (e: Exception) {
            toast("Could not use that folder: ${e.message}")
        }
    }

    fun changeDownloadsFolder(tree: Uri) = control.execute {
        try {
            keepAccess(tree, write = true)
            val folder = treeFolder(tree)
            settings.downloadsFolder = folder
            node?.setDownloadFolder(downloadFolderAt(folder))
        } catch (e: Exception) {
            toast("Could not use that folder: ${e.message}")
        }
    }

    /** Share picked files from where they are, without copying them. */
    fun shareInPlace(uris: List<Uri>) = control.execute {
        val kept = uris.filter {
            try {
                keepAccess(it, write = false)
                true
            } catch (e: SecurityException) {
                false
            }
        }
        if (kept.size < uris.size) {
            importFiles(uris - kept.toSet()) // no lasting access to those: copy them instead
        }
        if (kept.isNotEmpty()) {
            settings.links = (settings.links + kept).distinct()
            node?.let(::applyShareTree)
            toast(if (kept.size == 1) "Sharing 1 file" else "Sharing ${kept.size} files")
        }
    }

    /** Stop sharing a file that was shared on its own (it stays where it is on the phone). */
    fun unshare(fileId: String) = control.execute {
        val key = node?.indexer?.lookup(fileId)?.key ?: return@execute
        if (!key.startsWith("link:")) return@execute
        val uri = Uri.parse(key.removePrefix("link:"))
        settings.links = settings.links.filter { it != uri }
        runCatching { app.contentResolver.releasePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
        node?.let(::applyShareTree)
    }

    /** Files shared to LightningLAN from other apps: copied into the shared folder. */
    fun importFiles(uris: List<Uri>) = actions.execute {
        val folder = settings.shareFolder
        if (folder == null) {
            toast("Open LightningLAN first, then share the files again.")
            return@execute
        }
        var done = 0
        for ((i, uri) in uris.withIndex()) {
            busy = "Adding ${i + 1} of ${uris.size} to your shared folder…"
            try {
                copyIntoFolder(uri, folder)
                done++
            } catch (e: Exception) {
                CoreLog.w(TAG, "could not add $uri: $e")
            }
        }
        busy = null
        node?.indexer?.requestRescan()
        toast(
            when {
                done == uris.size && done == 1 -> "Added 1 file to your shared folder"
                done == uris.size -> "Added $done files to your shared folder"
                else -> "Added $done of ${uris.size} files; the others couldn't be read"
            },
        )
    }

    private fun copyIntoFolder(source: Uri, folder: Uri) {
        val resolver = app.contentResolver
        val name = safeLocalFilename(queryName(resolver, source) ?: "shared file", fallback = "shared file")
        folder.localFolder()?.let { dir ->
            val target = numberedNames(name).take(10_000).map { File(dir, it) }.firstOrNull { !it.exists() }
                ?: throw IOException("no free name for $name")
            try {
                resolver.openInputStream(source).use { input ->
                    target.outputStream().use { output -> (input ?: throw IOException("could not read $name")).copyTo(output, 1024 * 1024) }
                }
            } catch (e: Exception) {
                target.delete()
                throw e
            }
            return
        }
        val mime = resolver.getType(source) ?: "application/octet-stream"
        val target = DocumentsContract.createDocument(resolver, folder, mime, name) ?: throw IOException("could not create $name")
        try {
            resolver.openInputStream(source).use { input ->
                resolver.openOutputStream(target, "w").use { output ->
                    if (input == null || output == null) throw IOException("could not copy $name")
                    input.copyTo(output, 1024 * 1024)
                }
            }
        } catch (e: Exception) {
            runCatching { DocumentsContract.deleteDocument(resolver, target) }
            throw e
        }
    }

    private fun queryName(resolver: ContentResolver, uri: Uri): String? = try {
        resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0) else null
        }
    } catch (e: Exception) {
        null
    }

    private fun applyShareTree(n: Node) {
        val tree = PhoneShareTree(app, settings.shareFolder, settings.links)
        shareTree = tree
        n.setShareTree(tree)
    }

    private fun keepAccess(uri: Uri, write: Boolean) {
        val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or (if (write) Intent.FLAG_GRANT_WRITE_URI_PERMISSION else 0)
        app.contentResolver.takePersistableUriPermission(uri, flags)
    }

    // ----- settings ----------------------------------------------------------------------------

    /** A name the person picked (even with the dice): it never changes by itself. */
    fun rename(name: String): Boolean {
        val cleaned = cleanName(name) ?: return false
        settings.name = cleaned
        settings.nameGenerated = false
        control.execute { node?.setName(cleaned) }
        return true
    }

    fun suggestName(): String = randomName(listOf(settings.name) + (node?.peers?.known()?.map { it.name } ?: emptyList()))

    /** Deriving the key takes a moment, so this runs in the background. Empty turns the passphrase off. */
    fun setPassphrase(passphrase: String, done: () -> Unit) = control.execute {
        val value = passphrase.takeIf { it.isNotEmpty() }
        val key = value?.let(::deriveKey)
        settings.passphrase = value
        settings.passphraseKey = key
        node?.setPassphraseKey(key)
        main.post(done)
        toast(if (value == null) "Passphrase turned off" else "Passphrase saved")
    }

    // ----- opening files -----------------------------------------------------------------------

    /** An intent that opens one of this phone's shared files, or a finished download. */
    fun openIntent(fileId: String?, savedLocation: String?, name: String): Intent? {
        val uri = savedLocation?.let { if (it.startsWith("/")) shareableUri(app, File(it)) else Uri.parse(it) }
            ?: fileId?.let { id -> node?.indexer?.lookup(id)?.key?.let { key -> shareTree?.uriFor(key) } }
            ?: return null
        val ext = name.substringAfterLast('.', "").lowercase()
        val mime = MimeTypeMap.getSingleton().getMimeTypeFromExtension(ext) ?: "*/*"
        return Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime)
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
            .apply { clipData = ClipData.newRawUri(name, uri) }
    }

    fun toast(message: String) = main.post { Toast.makeText(app, message, Toast.LENGTH_SHORT).show() }

    private fun cleanName(name: String): String? = name.filter { !it.isISOControl() }.trim().take(64).trim().ifEmpty { null }
}
