package lightninglan.core

import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import okhttp3.ConnectionPool
import okhttp3.OkHttpClient
import java.io.File
import java.io.IOException
import java.math.BigDecimal
import java.net.Proxy
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/*
 * A running LightningLAN peer (lightninglan/node.py): ties the index, discovery, peer polling,
 * transfers and the peer-to-peer HTTP server together.
 */

private const val TAG = "node"
private const val RESUME_WAIT = 15_000L // after going online, how long paused downloads wait for a peer with the file

/** Why a peer is offline: the Stop button. */
const val STOPPED = "stopped"

class NodeConfig(
    val peerId: String,
    val name: String,
    /** This app's version, reported to other peers. */
    val version: String,
    /** Where the hash cache is kept. */
    val stateDir: File,
    val port: Int = 8765,
    /** The passphrase's key from [deriveKey], or null for no passphrase. */
    val passphraseKey: ByteArray? = null,
    val refreshMs: Long = 3_000,
    val indexIntervalMs: Long = 10_000,
)

data class NodeStatus(
    val online: Boolean,
    val offlineReason: String?,
    val name: String,
    val port: Int,
    val sharedFiles: Int,
    val indexingPending: Int,
    val peersOnline: Int,
    val uploadedBytes: Long,
    val passphrase: Boolean,
)

class Node(
    private val config: NodeConfig,
    shareTree: ShareTree?,
    downloadFolder: DownloadFolder?,
    private val discovery: Discovery?,
) {
    val peerId: String = config.peerId
    val version: String = config.version

    @Volatile
    var name: String = config.name
        private set
    val auth = Authenticator(config.passphraseKey, peerId)
    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(2, TimeUnit.SECONDS)
        .readTimeout(5, TimeUnit.SECONDS)
        .connectionPool(ConnectionPool(32, 5, TimeUnit.MINUTES))
        .proxy(Proxy.NO_PROXY) // never route LAN traffic through a configured HTTP proxy
        .build()
    val indexer = Indexer(shareTree, File(config.stateDir, "hash_cache.json"), config.indexIntervalMs)
    val peers = PeerTable(peerId, auth, config.refreshMs, http)
    val downloads = DownloadManager(
        downloadFolder, auth, peers,
        http.newBuilder().connectTimeout(5, TimeUnit.SECONDS).readTimeout(15, TimeUnit.SECONDS).build(),
    )
    val uploads = UploadLimiter()
    val arcade = Arcade(peerId, { name }, auth, peers, config.refreshMs, http)
    private val server = HttpServer(LanApi(this)::handle)

    // Online: sharing and downloading. Offline: hidden from peers, transfers stopped.
    @Volatile
    var online = false
        private set

    @Volatile
    var offlineReason: String? = STOPPED
        private set

    /** Bumps on every online/offline change. */
    @Volatile
    var stateVersion = 0
        private set

    /** The port the peer-to-peer server listens on. */
    @Volatile
    var port = 0
        private set
    private val switching = Any()
    private var closing = false
    private var pausedByStop: List<String> = emptyList()

    @Volatile
    private var filesCache: Triple<Any, ByteArray, String>? = null

    @Volatile
    private var commonCache: Pair<Any, List<CommonFile>>? = null

    /** Starts listening and indexing, then joins the network. */
    @Throws(IOException::class)
    fun start() {
        port = listen(config.port)
        indexer.start()
        goOnline()
    }

    /** The preferred port, or a nearby one if it is taken, or any free port. */
    private fun listen(preferred: Int): Int {
        for (candidate in listOf(preferred) + (preferred + 1..preferred + 20) + 0) {
            try {
                return server.start(candidate)
            } catch (e: IOException) {
                if (candidate == 0) throw e
            }
        }
        throw IOException("no free port")
    }

    fun close() {
        synchronized(switching) {
            if (closing) return
            closing = true
        }
        if (online) {
            try {
                discovery?.stop() // say goodbye before anything else goes away
            } catch (e: Exception) {
                CoreLog.e(TAG, "stopping discovery failed", e)
            }
        }
        online = false
        uploads.cutOff()
        downloads.shutdown()
        arcade.stop()
        peers.stop()
        indexer.stop()
        server.stop()
    }

    // ----- going offline and back (Stop and Start) ---------------------------------------------

    /** Stop sharing: leave the network, refuse other peers, cut off uploads, pause downloads. */
    fun goOffline(reason: String = STOPPED) {
        synchronized(switching) {
            if (closing) return
            if (!online) {
                if (reason == STOPPED && offlineReason != STOPPED) {
                    offlineReason = STOPPED
                    stateVersion++
                }
                return
            }
            online = false
            offlineReason = reason
            stateVersion++
            uploads.cutOff()
            try {
                discovery?.stop()
            } catch (e: Exception) {
                CoreLog.e(TAG, "stopping discovery failed", e)
            }
            pausedByStop = downloads.pauseAll()
            arcade.stop()
            peers.stop()
            peers.clear()
            CoreLog.i(TAG, "stopped sharing")
        }
    }

    /** Share again: rejoin the network and resume the downloads that going offline paused. */
    fun goOnline() {
        synchronized(switching) {
            if (online || closing) return
            uploads.reopen()
            peers.start()
            arcade.start()
            try {
                discovery?.start(advert(), peers)
            } catch (e: Exception) {
                CoreLog.e(TAG, "could not join the network", e)
            }
            online = true
            offlineReason = null
            stateVersion++
            val paused = pausedByStop
            pausedByStop = emptyList()
            if (paused.isNotEmpty()) thread(name = "lightninglan-resume", isDaemon = true) { resumeWhenFound(paused) }
            CoreLog.i(TAG, "sharing on port $port")
        }
    }

    private fun resumeWhenFound(downloadIds: List<String>) {
        // Peers take a moment to reappear: resume each download once a peer has its file again
        // (or after RESUME_WAIT, so it can say that nobody has it).
        val deadline = monotonicMs() + RESUME_WAIT
        val waiting = downloadIds.toMutableList()
        while (waiting.isNotEmpty() && online) {
            val giveUp = monotonicMs() >= deadline
            for (id in waiting.toList()) {
                val d = downloads.find(id)
                if (d == null || d.status != PAUSED) {
                    waiting.remove(id) // removed, or resumed by hand
                } else if (giveUp || downloads.sources(d.fileId).isNotEmpty()) {
                    waiting.remove(id)
                    downloads.resumeAll(listOf(id))
                }
            }
            Thread.sleep(500)
        }
    }

    fun advert(): Advert = Advert(peerId, name, port, auth.enabled, version)

    // ----- what other peers and the UI see ---------------------------------------------------

    /** The JSON body and ETag for GET /api/files, cached per index and swarm version. */
    fun filesPayload(): Pair<ByteArray, String> {
        val snap = indexer.snapshot
        val key = Triple(snap.version, downloads.swarmVersion, name)
        filesCache?.let { if (it.first == key) return it.second to it.third }
        val body = buildJsonObject {
            put("peer_id", peerId)
            put("name", name)
            put("version", version)
            put("files", buildJsonArray {
                for (e in snap.entries) {
                    add(buildJsonObject {
                        put("id", e.sha256)
                        put("name", e.relpath)
                        put("size", e.size)
                        put("sha256", e.sha256)
                        put("mtime", JsonPrimitive(BigDecimal.valueOf(e.mtimeMs, 3)))
                    })
                }
            })
            put("indexing", buildJsonObject { put("pending", snap.pending) })
            // Its presence says we serve piece lists; the ids are files we can pass pieces of.
            put("swarm", buildJsonArray { for (id in downloads.swarmFiles()) add(id) })
            put("play", 1) // serves GET /api/games: plays head-to-head games (LightningLAN 1.2+)
            put("games", buildJsonArray { for (kind in GAME_TITLES.keys) add(kind) }) // which ones
        }.toString().toByteArray(Charsets.UTF_8)
        val etag = "\"" + sha256Hex(body).take(32) + "\""
        filesCache = Triple(key, body, etag)
        return body to etag
    }

    /** Piece hashes for a file we share, or one we downloaded whole and still list. */
    fun pieceInfo(fileId: String): PieceInfo? = indexer.piecesInfo(fileId) ?: downloads.pieceInfoFor(fileId)

    /** ThunderCLOUD: this peer's files and every online peer's, merged. */
    fun commonFiles(): List<CommonFile> {
        val key = Triple(indexer.snapshot.version, peers.version, name)
        commonCache?.let { if (it.first == key) return it.second }
        val files = mergeCommonSpace(peerId, name, indexer.snapshot.entries, peers.online())
        commonCache = key to files
        return files
    }

    fun status(): NodeStatus {
        val snap = indexer.snapshot
        return NodeStatus(
            online, offlineReason, name, port, snap.entries.size, snap.pending, peers.online().size, uploads.sent.get(), auth.enabled,
        )
    }

    // ----- settings ----------------------------------------------------------------------------

    fun setName(name: String) {
        this.name = name
        filesCache = null
        commonCache = null
        reannounce()
    }

    /** A passphrase's key from [deriveKey], or null to turn the passphrase off. */
    fun setPassphraseKey(key: ByteArray?) {
        auth.setKey(key)
        peers.credentialsChanged()
        commonCache = null
        reannounce()
    }

    fun setShareTree(tree: ShareTree?) = indexer.setTree(tree)

    /** Downloads already started finish where they are. */
    fun setDownloadFolder(folder: DownloadFolder?) {
        downloads.folder = folder
    }

    private fun reannounce() {
        if (!online) return
        try {
            discovery?.update(advert())
        } catch (e: Exception) {
            CoreLog.e(TAG, "could not update the announcement", e)
        }
    }
}
