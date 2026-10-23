package lightninglan.core

import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Semaphore
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/*
 * Downloading files from other peers (lightninglan/transfers.py).
 *
 * Each download runs in its own thread (at most MAX_ACTIVE_DOWNLOADS at a time) and writes into
 * the downloads folder's .lightninglan-partial/<sha256>.part. Files of more than one piece are
 * fetched as a swarm (Swarm.kt); peers without swarm support send the file whole, as one stream
 * that an HTTP Range request resumes. Either way the SHA-256 of the whole file is checked before
 * the partial file is moved into place; on a mismatch the data is discarded.
 */

private const val TAG = "downloads"
private const val CHUNK = 512 * 1024
private const val MAX_ACTIVE_DOWNLOADS = 3 // more wait their turn as "queued"
private const val MAX_PIECE_LIST_BYTES = 2L * 1024 * 1024

const val QUEUED = "queued"
const val DOWNLOADING = "downloading"
const val VERIFYING = "verifying"
const val COMPLETED = "completed"
const val PAUSED = "paused"
const val FAILED = "failed"
const val CANCELED = "canceled"
val ACTIVE_STATES = setOf(QUEUED, DOWNLOADING, VERIFYING)

/** A request to the download manager could not be carried out (message is user-facing). */
class DownloadError(message: String, val status: Int = 404) : Exception(message)

private class Stopped : Exception()

/** This peer could not deliver the file; another source may still work. */
private class SourceFailed(message: String) : Exception(message)

private class Corrupt(message: String = "") : Exception(message)

private val json = Json { ignoreUnknownKeys = true }

/** One download, as the UI shows it. */
data class DownloadView(
    val id: String,
    val fileId: String,
    val name: String,
    val size: Long,
    val received: Long,
    val status: String,
    val sourceName: String?,
    val error: String?,
    val note: String?,
    val saved: SavedFile?,
    val speed: Double,
    val createdAt: Long,
    val finishedAt: Long?,
    val swarmPieces: Int?,
    val swarmHave: Int?,
    /** Who sent how much, most first (swarm downloads). */
    val contributors: List<Pair<String, Long>>,
) {
    val progress: Double get() = if (size > 0) received.toDouble() / size else if (status == COMPLETED) 1.0 else 0.0
}

class Download(
    val id: String,
    val fileId: String,
    val name: String,
    val size: Long,
    val folder: DownloadFolder, // the downloads folder when this download started; it finishes there
    @Volatile var preferredPeer: String?,
) : SwarmProgress {
    @Volatile var status = QUEUED
    @Volatile override var received = 0L
    @Volatile var resumedFrom = 0L
    @Volatile var sourcePeerId: String? = null
    @Volatile var sourceName: String? = null
    @Volatile var error: String? = null
    @Volatile var note: String? = null
    @Volatile var saved: SavedFile? = null
    @Volatile override var speed = 0.0
    val createdAt = System.currentTimeMillis()
    @Volatile var finishedAt: Long? = null
    @Volatile var pieceInfo: PieceInfo? = null
    @Volatile var swarmState: SwarmState? = null
    override val contributors = ConcurrentHashMap<String, Contributor>()

    private fun peersByBytes(): List<Contributor> = contributors.values.sortedByDescending { it.bytes.get() }

    fun view(): DownloadView {
        val peers = peersByBytes()
        val state = swarmState
        return DownloadView(
            id, fileId, displayName(name), size, received, status,
            peers.firstOrNull()?.name ?: sourceName, // a swarm download: the peer that sent the most
            error, note, saved, speed, createdAt, finishedAt,
            state?.info?.count, state?.haveCount, peers.map { it.name to it.bytes.get() },
        )
    }

    fun sourceLabel(): String = peersByBytes().firstOrNull()?.name ?: sourceName ?: "?"
}

class DownloadManager(
    @Volatile var folder: DownloadFolder?, // where new downloads go
    private val auth: Authenticator,
    private val peers: PeerTable,
    private val client: OkHttpClient,
) {
    /** Bumps when the set of files we can pass pieces of changes. */
    @Volatile
    var swarmVersion = 0
        private set
    private val downloads = LinkedHashMap<String, Download>()
    private val stops = ConcurrentHashMap<String, StopSignal>()
    private val threads = ConcurrentHashMap<String, Thread>()
    private val swarm = HashMap<String, SwarmState>()
    private val slots = Semaphore(MAX_ACTIVE_DOWNLOADS)
    private val lock = Any()
    private val streamClient = client.newBuilder().readTimeout(15, TimeUnit.SECONDS).connectTimeout(5, TimeUnit.SECONDS).build()

    // ----- public API (called from the UI) ---------------------------------------------------

    fun list(): List<DownloadView> = synchronized(lock) { downloads.values.toList() }
        .sortedByDescending { it.createdAt }.map { it.view() }

    fun get(downloadId: String): Download = synchronized(lock) { downloads[downloadId] } ?: throw DownloadError("no such download")

    fun find(downloadId: String): Download? = synchronized(lock) { downloads[downloadId] }

    fun start(fileId: String, peerId: String? = null): Download {
        if (!SHA256_RE.matches(fileId)) throw DownloadError("invalid file id", 400)
        synchronized(lock) {
            val sameFile = downloads.values.filter { it.fileId == fileId }
            sameFile.firstOrNull { it.status in ACTIVE_STATES }?.let { return it }
            val sources = sources(fileId)
            if (sources.isEmpty()) throw DownloadError("No online peer has this file.")
            for (d in sameFile) {
                // Downloading a paused or failed file again resumes that entry and its partial data.
                if ((d.status == PAUSED || d.status == FAILED) && !isRunning(d.id)) {
                    d.error = null
                    d.note = null
                    d.status = QUEUED
                    d.preferredPeer = peerId ?: d.preferredPeer
                    launch(d)
                    return d
                }
            }
            val target = folder ?: throw DownloadError("Choose where downloads are saved first.", 409)
            val remote = sources.firstOrNull { it.first.peerId == peerId }?.second ?: sources[0].second
            val d = Download(UUID.randomUUID().toString().replace("-", "").take(12), fileId, remote.name, remote.size, target, peerId)
            downloads[d.id] = d
            launch(d)
            return d
        }
    }

    fun pause(downloadId: String): Download {
        val d = get(downloadId)
        if (d.status in ACTIVE_STATES) stopThread(d.id, PAUSED)
        return d
    }

    fun resume(downloadId: String): Download {
        val d = get(downloadId)
        if (isRunning(downloadId)) throw DownloadError("the download is still stopping; try again in a moment", 409)
        synchronized(lock) {
            if (downloads.values.any { it.id != d.id && it.fileId == d.fileId && it.status in ACTIVE_STATES }) {
                throw DownloadError("this file is already being downloaded", 409)
            }
            if (d.status == PAUSED || d.status == FAILED) {
                d.error = null
                d.note = null
                d.status = QUEUED
                launch(d)
            }
        }
        return d
    }

    fun cancel(downloadId: String): Download {
        val d = get(downloadId)
        if (d.status in ACTIVE_STATES) stopThread(d.id, CANCELED)
        if (d.status == PAUSED || d.status == FAILED) {
            d.status = CANCELED
            discardPartial(d)
        }
        return d
    }

    fun remove(downloadId: String) {
        val d = get(downloadId)
        if (d.status in ACTIVE_STATES) throw DownloadError("cancel or pause the download first", 409)
        if (d.status == PAUSED || d.status == FAILED) discardPartial(d)
        stopPassingOn(d)
        synchronized(lock) { downloads.remove(downloadId) }
    }

    fun clearFinished() {
        val finished = synchronized(lock) {
            val done = downloads.values.filter { it.status == COMPLETED || it.status == CANCELED }
            for (d in done) downloads.remove(d.id)
            done
        }
        for (d in finished) stopPassingOn(d)
    }

    /** Pause every queued or running download. Returns their ids. */
    fun pauseAll(): List<String> {
        val active = synchronized(lock) { downloads.values.filter { it.status in ACTIVE_STATES } }
        for (d in active) stops[d.id]?.set(PAUSED) // tell them all first, then wait for each
        for (d in active) stopThread(d.id, PAUSED)
        return active.map { it.id }
    }

    /** Resume the paused downloads (all of them, or those listed) that can be resumed now. */
    fun resumeAll(downloadIds: List<String>? = null) {
        val paused = synchronized(lock) { downloads.values.filter { it.status == PAUSED }.map { it.id } }
        for (id in paused) {
            if (downloadIds == null || id in downloadIds) {
                try {
                    resume(id)
                } catch (e: DownloadError) {
                    // still stopping, or the same file is downloading under another entry
                }
            }
        }
    }

    // ----- swarm: what we can pass on to other peers -------------------------------------------

    /** Files we can serve pieces of: downloading now, or downloaded and still listed. */
    fun swarmFiles(): List<String> = synchronized(lock) { swarm.keys.sorted() }

    fun swarmStatus(fileId: String): kotlinx.serialization.json.JsonObject? = synchronized(lock) { swarm[fileId] }?.status()

    /** Where to read a piece we hold, opened with a reader registered on its state (close it after). */
    fun locatePiece(fileId: String, index: Int): Located? = synchronized(lock) { swarm[fileId] }?.locate(index)

    /** The piece list of a file we finished downloading (proven correct by its SHA-256). */
    fun pieceInfoFor(fileId: String): PieceInfo? = synchronized(lock) { swarm[fileId] }?.takeIf { it.complete }?.info

    private fun register(state: SwarmState) {
        synchronized(lock) {
            val current = swarm[state.info.fileId]
            if (current != null && current !== state && current.complete && !state.complete) return // keep passing on the finished copy
            swarm[state.info.fileId] = state
            swarmVersion++
        }
    }

    private fun unregister(state: SwarmState) {
        synchronized(lock) {
            if (swarm[state.info.fileId] === state) {
                swarm.remove(state.info.fileId)
                swarmVersion++
            }
        }
    }

    private fun stopPassingOn(d: Download) {
        d.swarmState?.let { unregister(it) }
    }

    fun shutdown() {
        for (stop in stops.values) stop.set()
        for (t in threads.values.toList()) t.join(5_000)
    }

    fun sources(fileId: String): List<Pair<Peer, RemoteFile>> {
        val result = ArrayList<Pair<Peer, RemoteFile>>()
        for (peer in peers.online()) {
            val match = peer.files.firstOrNull { it.sha256 == fileId } ?: continue
            result.add(peer to match)
        }
        return result
    }

    // ----- worker ----------------------------------------------------------------------------

    private fun isRunning(downloadId: String): Boolean = threads[downloadId]?.isAlive == true

    private fun launch(d: Download) {
        val stop = StopSignal()
        stops[d.id] = stop
        val t = thread(name = "download-${d.id}", isDaemon = true, start = false) { run(d, stop) }
        threads[d.id] = t
        t.start()
    }

    private fun stopThread(downloadId: String, reason: String) {
        stops[downloadId]?.set(reason)
        val t = threads[downloadId]
        if (t != null && t !== Thread.currentThread()) t.join(10_000)
    }

    private fun discardPartial(d: Download) {
        val others = synchronized(lock) { downloads.values.toList() }
        if (others.any { it.fileId == d.fileId && it.status in ACTIVE_STATES }) return
        val state = d.swarmState
        d.swarmState = null
        if (state != null) {
            unregister(state)
            state.exclusive { d.folder.deletePartial(d.fileId) }
        } else {
            d.folder.deletePartial(d.fileId)
        }
    }

    private fun run(d: Download, stop: StopSignal) {
        var hasSlot = false
        try {
            hasSlot = waitForSlot(d, stop)
            val info = pieceInfo(d)
            if (info == null || !swarmDownload(d, info, stop)) wholeFileDownload(d, stop)
            finish(d)
        } catch (e: Stopped) {
            val reason = stop.reason ?: PAUSED
            d.status = reason
            d.speed = 0.0
            d.note = null
            if (reason == CANCELED) discardPartial(d)
        } catch (e: SourceFailed) {
            fail(d, e.message ?: "The download failed.")
        } catch (e: Corrupt) {
            fail(d, e.message ?: "The file failed SHA-256 verification.")
        } catch (e: LocalStorageError) {
            CoreLog.w(TAG, "download ${d.name} failed: ${e.message}")
            fail(d, "Could not write the file: ${e.message}")
        } catch (e: Exception) {
            CoreLog.e(TAG, "download ${d.name} failed", e)
            fail(d, "Unexpected error: ${e.message ?: e.javaClass.simpleName}")
        } finally {
            if (hasSlot) slots.release()
            stops.remove(d.id)
            threads.remove(d.id)
        }
    }

    private fun fail(d: Download, error: String) {
        d.status = FAILED
        d.error = error
        d.note = null
        d.speed = 0.0
    }

    private fun waitForSlot(d: Download, stop: StopSignal): Boolean {
        if (slots.tryAcquire()) return true
        d.status = QUEUED
        d.note = "Waiting for another download to finish…"
        while (!slots.tryAcquire(250, TimeUnit.MILLISECONDS)) {
            if (stop.isSet) throw Stopped()
        }
        d.note = null
        return true
    }

    // ----- swarm download --------------------------------------------------------------------

    /** The file's piece list from a peer sharing it, or null to download it whole. */
    private fun pieceInfo(d: Download): PieceInfo? {
        d.pieceInfo?.let { return it }
        if (!swarmable(d.size)) return null
        val sharers = sources(d.fileId).map { it.first }.filter { it.swarm }.sortedBy { it.peerId != d.preferredPeer }
        val path = "/api/pieces/${d.fileId}"
        for (peer in sharers) {
            val headers = auth.signRequest("GET", path)
            try {
                val builder = Request.Builder().url(peer.baseUrl() + path).header("Accept-Encoding", "identity")
                for ((k, v) in headers) builder.header(k, v)
                val body = client.newBuilder().callTimeout(10, TimeUnit.SECONDS).build().newCall(builder.build()).execute().use { resp ->
                    if (resp.code != 200 || !auth.verifyResponse(headers[TOKEN_HEADER], resp.header(PROOF_HEADER))) null
                    else readLimited(resp, MAX_PIECE_LIST_BYTES)
                } ?: continue
                val info = PieceInfo.fromJson(json.parseToJsonElement(body.toString(Charsets.UTF_8)), d.fileId, d.size)
                d.pieceInfo = info
                return info
            } catch (e: Exception) {
                CoreLog.i(TAG, "no piece list from ${peer.name}: ${e.message}")
            }
        }
        return null
    }

    /** Fetch all pieces and check the whole file. False: fall back to a whole-file download. */
    private fun swarmDownload(d: Download, info: PieceInfo, stop: StopSignal): Boolean {
        val partial = local { d.folder.openPartial(d.fileId) }
        try {
            local { if (partial.size() > d.size) partial.truncate(d.size) }
            var state = d.swarmState
            if (state == null || state.info != info) {
                val have = if (local { partial.size() } > 0) {
                    d.status = VERIFYING
                    d.note = "Checking the partial file…"
                    try {
                        scanPartial(partial, info, stop)
                    } catch (e: SwarmStopped) {
                        throw Stopped()
                    } finally {
                        d.note = null
                    }
                } else {
                    ByteArray(info.count)
                }
                state = SwarmState(info, d.folder, have)
                d.swarmState = state
            }
            register(state)
            val needed = d.size - state.haveBytes
            val free = d.folder.freeSpace()
            if (free != null && free < needed) {
                throw SourceFailed("Not enough space: ${formatSize(needed)} needed, ${formatSize(free)} free.")
            }
            d.status = DOWNLOADING
            d.received = state.haveBytes
            if (d.received > 0) d.resumedFrom = d.received // verified data the download continues from
            d.speed = 0.0
            val session = SwarmSession(state, partial, peers.ownPeerId, peers, auth, client, { sources(d.fileId).map { it.first } }, d, stop)
            try {
                session.run()
            } catch (e: SwarmStopped) {
                d.received = state.haveBytes
                throw Stopped()
            } catch (e: SwarmFailed) {
                d.received = state.haveBytes
                throw SourceFailed(e.message ?: "The download failed.")
            } catch (e: SwarmUnusable) {
                CoreLog.w(TAG, "swarm download of ${d.name}: ${e.message}; downloading the whole file instead")
                unregister(state)
                state.exclusive { local { partial.truncate(state.prefixBytes()) } } // keep the verified start for a Range resume
                d.swarmState = null
                d.pieceInfo = null
                d.note = "Some pieces failed their checks; downloading the file in one piece instead…"
                return false
            }
            d.received = d.size
            d.speed = 0.0
            if (!verify(d, partial)) {
                unregister(state)
                d.swarmState = null
                partial.close()
                state.exclusive { d.folder.deletePartial(d.fileId) }
                val names = d.contributors.values.joinToString(", ") { it.name }.ifEmpty { "the other peers" }
                throw Corrupt("The file received from $names failed SHA-256 verification and was discarded.")
            }
            return true
        } finally {
            runCatching { partial.close() }
        }
    }

    // ----- whole-file download (single source, resumed with Range) ------------------------------

    private fun wholeFileDownload(d: Download, stop: StopSignal) {
        val tried = HashSet<String>()
        val corruptFrom = ArrayList<String>()
        var lastError = "No online peer has this file."
        while (true) {
            val peer = pickSource(d, tried)
            if (peer == null) {
                if (corruptFrom.isNotEmpty()) {
                    throw Corrupt("The file received from ${corruptFrom.joinToString(", ")} failed SHA-256 verification and was discarded.")
                }
                throw SourceFailed(lastError)
            }
            d.sourcePeerId = peer.peerId
            d.sourceName = peer.name
            try {
                transferVerified(d, peer, stop)
                return
            } catch (e: SourceFailed) {
                tried.add(peer.peerId)
                lastError = e.message ?: lastError
                d.note = "$lastError Trying another peer…"
            } catch (e: Corrupt) {
                tried.add(peer.peerId)
                corruptFrom.add(peer.name)
            }
        }
    }

    private fun pickSource(d: Download, tried: Set<String>): Peer? {
        val candidates = sources(d.fileId).map { it.first }.filter { it.peerId !in tried }
        for (peerId in listOf(d.preferredPeer, d.sourcePeerId)) {
            candidates.firstOrNull { it.peerId == peerId }?.let { return it }
        }
        return candidates.firstOrNull()
    }

    /** Transfer from one peer; if a resumed file fails verification, retry once from zero. */
    private fun transferVerified(d: Download, peer: Peer, stop: StopSignal) {
        val resumed = transfer(d, peer, stop)
        if (verifyPartial(d)) return
        d.folder.deletePartial(d.fileId)
        if (resumed) {
            d.note = "The partial file was damaged; downloading again from the start…"
            transfer(d, peer, stop)
            if (verifyPartial(d)) return
            d.folder.deletePartial(d.fileId)
        }
        throw Corrupt()
    }

    /** Bring the partial file up to the full size from [peer]. True if it resumed existing data. */
    private fun transfer(d: Download, peer: Peer, stop: StopSignal): Boolean {
        val partial = local { d.folder.openPartial(d.fileId) }
        try {
            var offset = local { partial.size() }
            if (offset > d.size) {
                local { partial.truncate(0) }
                offset = 0
            }
            val free = d.folder.freeSpace()
            if (free != null && free < d.size - offset) {
                throw SourceFailed("Not enough space: ${formatSize(d.size - offset)} needed, ${formatSize(free)} free.")
            }
            d.status = DOWNLOADING
            d.received = offset
            if (offset > 0) d.resumedFrom = offset // where the most recent resume picked up
            if (offset >= d.size) return offset > 0

            val path = "/api/download/${d.fileId}"
            val headers = LinkedHashMap(auth.signRequest("GET", path))
            if (offset > 0) {
                headers["Range"] = "bytes=$offset-"
                headers["If-Range"] = "\"${d.fileId}\""
            }
            val startOffset = offset
            val builder = Request.Builder().url(peer.baseUrl() + path).header("Accept-Encoding", "identity")
            for ((k, v) in headers) builder.header(k, v)
            try {
                streamClient.newCall(builder.build()).execute().use { resp ->
                    if (!auth.verifyResponse(headers[TOKEN_HEADER], resp.header(PROOF_HEADER))) {
                        throw SourceFailed("${peer.name} failed the passphrase check.")
                    }
                    when {
                        resp.code == 404 -> throw SourceFailed("${peer.name} no longer shares this file.")
                        resp.code == 409 -> throw SourceFailed("The file changed on ${peer.name}.")
                        resp.code == 416 -> {
                            local { partial.truncate(0) }
                            throw SourceFailed("${peer.name} rejected the resume request.")
                        }
                        resp.code == 206 && offset > 0 -> {
                            if (resp.header("content-range") != "bytes $offset-${d.size - 1}/${d.size}") {
                                throw SourceFailed("${peer.name} sent an unexpected byte range.")
                            }
                        }
                        resp.code == 200 -> {
                            offset = 0 // the peer ignored our Range header: start over
                            d.received = 0
                        }
                        else -> throw SourceFailed("${peer.name} answered HTTP ${resp.code}.")
                    }
                    receive(d, resp.body!!.byteStream(), partial, offset, stop)
                }
            } catch (e: LocalStorageError) {
                throw e
            } catch (e: IOException) {
                val done = if (d.size > 0) "${d.received * 100 / d.size}%" else "0%"
                if (d.received > startOffset) {
                    throw SourceFailed(
                        "${peer.name} went offline during the transfer ($done done, ${describeNetworkError(e)}). " +
                            "The partial file was kept; press Resume to continue.",
                    )
                }
                throw SourceFailed("Could not reach ${peer.name}: ${describeNetworkError(e)}.")
            }
            if (d.received != d.size) {
                throw SourceFailed(
                    "${peer.name} ended the transfer early (${formatSize(d.received)} of ${formatSize(d.size)}). " +
                        "The partial file was kept; press Resume to continue.",
                )
            }
            return startOffset > 0 && offset > 0
        } finally {
            runCatching { partial.close() }
        }
    }

    private fun receive(d: Download, input: java.io.InputStream, partial: PartialFile, start: Long, stop: StopSignal) {
        var windowStart = monotonicMs()
        var windowBytes = 0L
        local { partial.truncate(start) }
        var position = start
        val buf = ByteArray(CHUNK)
        while (true) {
            val n = input.read(buf) // as it arrives: nothing is lost if the link drops
            if (n < 0) break
            if (stop.isSet) throw Stopped()
            if (d.received + n > d.size) throw SourceFailed("${d.sourceName} sent more data than the file size.")
            local { writeFully(partial, ByteBuffer.wrap(buf, 0, n), position) }
            position += n
            d.received += n
            windowBytes += n
            val elapsed = monotonicMs() - windowStart
            if (elapsed >= 500) {
                val rate = windowBytes * 1000.0 / elapsed
                d.speed = if (d.speed == 0.0) rate else 0.6 * rate + 0.4 * d.speed
                windowStart = monotonicMs()
                windowBytes = 0
            }
        }
    }

    private fun verifyPartial(d: Download): Boolean {
        val partial = local { d.folder.openPartial(d.fileId) }
        return partial.use { verify(d, it) }
    }

    private fun verify(d: Download, partial: PartialFile): Boolean {
        d.status = VERIFYING
        d.speed = 0.0
        val digest = MessageDigest.getInstance("SHA-256")
        val buf = ByteBuffer.allocate(4 * CHUNK)
        var position = 0L
        while (true) {
            buf.clear()
            val n = local { partial.read(buf, position) }
            if (n <= 0) break
            digest.update(buf.array(), 0, n)
            position += n
        }
        return digest.digest().toHex() == d.fileId
    }

    private fun finish(d: Download) {
        val fileName = safeLocalFilename(d.name)
        val state = d.swarmState
        val saved = if (state != null) {
            // Nobody may be reading a piece from the partial file while it moves.
            state.exclusive { local { d.folder.finish(d.fileId, fileName) } }.also {
                state.setComplete(it) // and from now on, pieces are read from the saved file
                register(state)
            }
        } else {
            local { d.folder.finish(d.fileId, fileName) }
        }
        d.saved = saved
        d.status = COMPLETED
        d.error = null
        d.note = null
        d.received = d.size
        d.finishedAt = System.currentTimeMillis()
        CoreLog.i(TAG, "downloaded ${d.name} from ${d.sourceLabel()} to ${saved.label}")
    }
}
