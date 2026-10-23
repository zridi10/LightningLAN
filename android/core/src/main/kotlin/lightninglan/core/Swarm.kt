package lightninglan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import okhttp3.Call
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.Closeable
import java.io.IOException
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutionException
import java.util.concurrent.ExecutorCompletionService
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong
import kotlin.concurrent.thread

/*
 * Swarm downloads (lightninglan/swarm.py): fetching a file's pieces from every peer that has them,
 * in parallel.
 *
 * Peers that share a file serve byte ranges of it (GET /api/download/<id> with a Range header).
 * Peers that are downloading the same file, or downloaded it and still list the download, serve
 * the pieces they have verified (GET /api/piece/<id>/<index>) and say which ones they have
 * (GET /api/swarm/<id>). Every piece is checked against its SHA-256 from the piece list before it is
 * kept or passed on. While some pieces exist only at the sharer, a downloader asks it only for
 * pieces no fellow downloader has or is fetching; everything else comes from fellow downloaders.
 * Pieces are requested rarest first, each downloader starting at its own point in the file.
 */

private const val TAG = "swarm"
private const val WORKERS = 8 // pieces in flight per download
private const val PER_SOURCE = 2 // pieces in flight per peer
private const val REFRESH_INTERVAL = 500L // how often fellow downloaders' have-maps are re-read
private const val MAX_FAILURES = 3 // failed requests in a row before a peer is given a long rest
private const val FAILURE_REST = 30_000L
private const val MAX_BAD_PIECES = 2 // pieces failing verification before a peer is dropped for good
private const val BAD_PIECE_REST = 2_000L
private const val STUCK_TIMEOUT = 15_000L // how long to wait for someone to offer the missing pieces

/** Paused or canceled. */
class SwarmStopped : Exception()

/** The download can't continue right now (message is user-facing); verified pieces are kept. */
class SwarmFailed(message: String) : Exception(message)

/** Pieces kept failing verification against the piece list: fall back to a whole-file download. */
class SwarmUnusable(message: String) : Exception(message)

private class BadPiece(message: String) : Exception(message)

/** This peer can't serve this file at all (no longer has it, changed it, failed auth). */
private class Gone(message: String) : Exception(message)

private class HttpStatusError(message: String) : IOException(message)

private val json = Json { ignoreUnknownKeys = true }

/** A piece that can be read right now; closing it lets the file be moved again. */
class Located(val file: ReadableFile, val start: Long, val length: Int, private val state: SwarmState) : Closeable {
    override fun close() {
        try {
            file.close()
        } finally {
            state.releaseReader()
        }
    }
}

/** The pieces of one file this peer holds and can pass on: while downloading, and after. */
class SwarmState(val info: PieceInfo, val folder: DownloadFolder, have: ByteArray? = null) {
    val have: ByteArray = have ?: ByteArray(info.count)
    val pending = ByteArray(info.count) // pieces being fetched right now

    @Volatile
    var complete = false
        private set

    @Volatile
    var saved: SavedFile? = null
        private set
    private var readers = 0
    private var paused = false
    private val lock = Object()

    fun has(index: Int): Boolean = have[index].toInt() != 0

    val haveCount: Int get() = synchronized(lock) { have.count { it.toInt() != 0 } }

    val haveBytes: Long
        get() = synchronized(lock) {
            var total = 0L
            for (i in have.indices) if (have[i].toInt() != 0) total += info.length(i)
            total
        }

    fun mark(index: Int) = synchronized(lock) {
        have[index] = 1
        pending[index] = 0
    }

    fun setPending(index: Int, value: Boolean) = synchronized(lock) { pending[index] = if (value) 1 else 0 }

    /** Length of the run of verified pieces at the start of the file. */
    fun prefixBytes(): Long = synchronized(lock) {
        var end = 0L
        for (i in have.indices) {
            if (have[i].toInt() == 0) break
            end = info.start(i) + info.length(i)
        }
        end
    }

    fun status(): JsonObject = synchronized(lock) {
        buildJsonObject {
            put("pieces", info.count)
            put("piece_size", info.pieceSize)
            put("have", encodeBits(have))
            put("pending", encodeBits(pending))
            put("complete", complete)
        }
    }

    /** Piece [index], opened for reading with a reader registered; null if we don't have it. */
    fun locate(index: Int): Located? {
        val savedFile: SavedFile?
        synchronized(lock) {
            if (paused || index !in 0 until info.count || have[index].toInt() == 0) return null
            readers++
            savedFile = if (complete) saved else null
        }
        val file = try {
            if (savedFile != null) folder.openSaved(savedFile.location) else folder.openPartialForReading(info.fileId)
        } catch (e: Exception) {
            null
        }
        // A finished download is only passed on while the file is exactly as we saved it.
        if (file == null || (savedFile != null && (file.size != savedFile.size || file.mtimeMs / 1000 != savedFile.mtimeMs / 1000))) {
            file?.close()
            releaseReader()
            return null
        }
        return Located(file, info.start(index), info.length(index), this)
    }

    fun releaseReader() = synchronized(lock) {
        readers--
        lock.notifyAll()
    }

    /** No piece reads while the file is moved or deleted. */
    fun <T> exclusive(timeoutMs: Long = 10_000, block: () -> T): T {
        synchronized(lock) {
            paused = true
            val deadline = monotonicMs() + timeoutMs
            while (readers > 0) {
                val left = deadline - monotonicMs()
                if (left <= 0) break
                lock.wait(left)
            }
        }
        try {
            return block()
        } finally {
            synchronized(lock) { paused = false }
        }
    }

    /** The download finished: from now on pieces are read from the saved file. */
    fun setComplete(saved: SavedFile) = synchronized(lock) {
        this.saved = saved
        complete = true
        have.fill(1)
        pending.fill(0)
    }
}

/** Which pieces an existing partial file already holds, by hashing them (resume after a restart). */
fun scanPartial(partial: PartialFile, info: PieceInfo, stop: StopSignal): ByteArray {
    val have = ByteArray(info.count)
    val size = try {
        partial.size()
    } catch (e: IOException) {
        return have
    }
    val buf = ByteBuffer.allocate(1024 * 1024)
    for (i in 0 until info.count) {
        if (stop.isSet) throw SwarmStopped()
        val start = info.start(i)
        val length = info.length(i)
        if (start + length > size) break
        val digest = MessageDigest.getInstance("SHA-256")
        var position = start
        var remaining = length
        while (remaining > 0) {
            buf.clear()
            buf.limit(minOf(buf.capacity(), remaining))
            val n = try {
                partial.read(buf, position)
            } catch (e: IOException) {
                -1
            }
            if (n <= 0) break
            digest.update(buf.array(), 0, n)
            position += n
            remaining -= n
        }
        if (remaining == 0 && digest.digest().contentEquals(info.digest(i))) have[i] = 1
    }
    return have
}

fun writeFully(partial: PartialFile, buf: ByteBuffer, position: Long) {
    var pos = position
    while (buf.hasRemaining()) {
        val n = partial.write(buf, pos)
        if (n <= 0) throw IOException("could not write to the partial file")
        pos += n
    }
}

/** A peer that sent us part of a download, for the progress shown. */
class Contributor(val peerId: String, @Volatile var name: String) {
    val bytes = AtomicLong()
}

/** Where a session reports progress (the download manager's [Download]). */
interface SwarmProgress {
    var received: Long
    var speed: Double
    val contributors: ConcurrentHashMap<String, Contributor>
}

private class Source(@Volatile var peer: Peer, val seed: Boolean) {
    var have: ByteArray? = null // null: has every piece
    var pending: ByteArray? = null // pieces it is fetching right now (fellow downloaders)
    var inflight = 0
    var failures = 0
    var badPieces = 0
    var retryAt = 0L
    var dropped = false // sent bad data or can't serve the file: not used again
    var present = true // still online and offering the file
    var bytes = 0L
    var lastError = ""

    fun has(index: Int): Boolean = have?.let { it[index].toInt() != 0 } ?: true

    fun usable(now: Long): Boolean = present && !dropped && retryAt <= now && inflight < PER_SOURCE
}

private class Job(val source: Source, val index: Int) {
    @Volatile
    var received = 0
}

class SwarmSession(
    private val state: SwarmState,
    private val partial: PartialFile,
    ownPeerId: String,
    private val peers: PeerTable,
    private val auth: Authenticator,
    client: OkHttpClient,
    private val seeds: () -> List<Peer>,
    private val progress: SwarmProgress,
    private val stop: StopSignal,
) {
    private val info = state.info
    private val n = info.count
    private val startedWith = state.haveBytes
    private val client = client
    private val haveClient = client.newBuilder().readTimeout(3, TimeUnit.SECONDS).callTimeout(3, TimeUnit.SECONDS).build()
    private val seedValue: Long =
        ByteBuffer.wrap(MessageDigest.getInstance("SHA-256").digest((ownPeerId + info.fileId).toByteArray(Charsets.UTF_8))).long
    private val cursor = java.lang.Long.remainderUnsigned(seedValue, n.toLong()).toInt()
    private val rng = java.util.Random(seedValue)
    private val sources = LinkedHashMap<String, Source>()
    private val inflight = HashMap<Int, Job>()
    private var order: IntArray = IntArray(n) { it }
    private var elsewhere = ByteArray(n) // a fellow downloader has or is fetching it: don't ask a sharer
    private var hasFellows = false // other downloaders of this file are online
    private val lock = Object()
    private val calls: MutableSet<Call> = ConcurrentHashMap.newKeySet()

    @Volatile
    private var done = false // tells workers and the refresher to wind down
    private var speedWindowStart = monotonicMs()
    private var speedWindowCount = 0L
    private val transferred = AtomicLong() // every byte received, kept or not (for the speed shown)

    @Volatile
    private var fatal: IOException? = null

    fun run() {
        try {
            refresh()
            if (synchronized(lock) { sources.values.none { it.present } }) throw SwarmFailed("No online peer has this file.")
            val refresher = thread(name = "swarm-refresh", isDaemon = true) { refreshLoop() }
            val pool = Executors.newFixedThreadPool(WORKERS) { r -> Thread(r, "swarm-worker").apply { isDaemon = true } }
            try {
                loop(pool)
            } finally {
                done = true // in-flight requests stop at their next chunk
                for (call in calls) call.cancel()
                pool.shutdown()
                pool.awaitTermination(20, TimeUnit.SECONDS)
                refresher.join(5_000)
                synchronized(lock) { for (index in inflight.keys) state.setPending(index, false) }
            }
        } finally {
            done = true
        }
    }

    private fun loop(pool: java.util.concurrent.ExecutorService) {
        val completion = ExecutorCompletionService<Unit>(pool)
        val running = HashMap<Future<Unit>, Job>()
        var stuckSince: Long? = null
        while (true) {
            if (stop.isSet) throw SwarmStopped()
            fatal?.let { throw LocalStorageError(it) }
            if (state.haveCount == n) return
            val jobs = synchronized(lock) { assign(running.size) }
            for (job in jobs) running[completion.submit { fetch(job) }] = job
            if (running.isNotEmpty()) {
                stuckSince = null
                var finished = completion.poll(250, TimeUnit.MILLISECONDS)
                while (finished != null) {
                    val job = running.remove(finished)!!
                    val error = try {
                        finished.get()
                        null
                    } catch (e: ExecutionException) {
                        e.cause ?: e
                    }
                    settle(job, error)
                    finished = completion.poll()
                }
            } else {
                val now = monotonicMs()
                val since = stuckSince ?: now
                stuckSince = since
                checkStuck(now - since)
                Thread.sleep(100)
            }
            report()
        }
    }

    // ----- choosing what to request from whom ------------------------------------------------

    private fun assign(busy: Int): List<Job> {
        val jobs = ArrayList<Job>()
        val now = monotonicMs()
        // While some pieces exist only at the sharers, their upload goes to those pieces alone.
        val spareSharers = (0 until n).any { !state.has(it) && elsewhere[it].toInt() == 0 && it !in inflight }
        while (busy + jobs.size < WORKERS) {
            val usable = sources.values.filter { it.usable(now) }
            if (usable.isEmpty()) break
            // Least busy first; spare the sharer when a fellow downloader has the piece too.
            val sorted = usable.map { it to rng.nextDouble() }
                .sortedWith(compareBy({ it.first.inflight }, { it.first.seed }, { it.first.failures }, { it.second }))
                .map { it.first }
            var job: Job? = null
            for (source in sorted) {
                val index = rarestFor(source, spareSharers)
                if (index != null) {
                    job = Job(source, index)
                    break
                }
            }
            if (job == null) break
            job.source.inflight++
            inflight[job.index] = job
            state.setPending(job.index, true)
            jobs.add(job)
        }
        return jobs
    }

    private fun rarestFor(source: Source, spareSharers: Boolean): Int? {
        if (source.seed && spareSharers && hasFellows) {
            // Pieces only the sharers have: pick one at random, so fellow downloaders asking at
            // the same moment rarely pick the same one.
            val choices = (0 until n).filter { !state.has(it) && elsewhere[it].toInt() == 0 && it !in inflight }
            return if (choices.isEmpty()) null else choices[rng.nextInt(choices.size)]
        }
        for (i in order) { // rarest first, then from our starting point onwards
            if (!state.has(i) && i !in inflight && source.has(i)) return i
        }
        return null
    }

    private fun reorder() {
        val avail = IntArray(n)
        val newElsewhere = ByteArray(n)
        for (s in sources.values) {
            if (!s.present || s.dropped) continue
            val have = s.have
            if (have == null) {
                for (i in 0 until n) avail[i]++
            } else {
                for (i in 0 until n) avail[i] += have[i]
            }
            if (!s.seed && s.failures < MAX_FAILURES) {
                if (have == null) {
                    newElsewhere.fill(1)
                } else {
                    val pending = s.pending
                    for (i in 0 until n) {
                        if (have[i].toInt() != 0 || (pending != null && pending[i].toInt() != 0)) newElsewhere[i] = 1
                    }
                }
            }
        }
        elsewhere = newElsewhere
        hasFellows = sources.values.any { !it.seed && it.present && !it.dropped }
        order = (0 until n).sortedWith(compareBy({ avail[it] }, { Math.floorMod(it - cursor, n) })).toIntArray()
    }

    // ----- fetching one piece ----------------------------------------------------------------

    private fun fetch(job: Job) {
        val source = job.source
        val index = job.index
        val start = info.start(index)
        val length = info.length(index)
        val fileId = info.fileId
        val path: String
        val headers = LinkedHashMap<String, String>()
        if (source.seed) {
            path = "/api/download/$fileId"
            headers.putAll(auth.signRequest("GET", path))
            headers["Range"] = "bytes=$start-${start + length - 1}"
            headers["If-Range"] = "\"$fileId\""
        } else {
            path = "/api/piece/$fileId/$index"
            headers.putAll(auth.signRequest("GET", path))
        }
        val builder = Request.Builder().url(source.peer.baseUrl() + path).header("Accept-Encoding", "identity")
        for ((k, v) in headers) builder.header(k, v)
        val digest = MessageDigest.getInstance("SHA-256")
        val call = client.newCall(builder.build())
        calls.add(call)
        try {
            if (done) throw SwarmStopped()
            call.execute().use { resp ->
                if (!auth.verifyResponse(headers[TOKEN_HEADER], resp.header(PROOF_HEADER))) throw Gone("failed the passphrase check")
                if (resp.code == 404 || resp.code == 409 || resp.code == 410) {
                    throw Gone(if (resp.code != 409) "no longer offers this file" else "the file changed")
                }
                if (source.seed) {
                    val expected = "bytes $start-${start + length - 1}/${info.size}"
                    if (resp.code != 206 || resp.header("content-range") != expected) {
                        throw Gone("did not send the requested part (HTTP ${resp.code})")
                    }
                } else if (resp.code != 200) {
                    throw HttpStatusError("HTTP ${resp.code}")
                }
                val input = resp.body!!.byteStream()
                val buf = ByteArray(64 * 1024)
                while (true) {
                    val got = input.read(buf)
                    if (got < 0) break
                    if (done || stop.isSet) throw SwarmStopped()
                    if (job.received + got > length) throw BadPiece("more data than the piece size")
                    local { writeFully(partial, ByteBuffer.wrap(buf, 0, got), start + job.received) }
                    digest.update(buf, 0, got)
                    job.received += got
                    transferred.addAndGet(got.toLong())
                }
            }
        } catch (e: IOException) {
            if (done || stop.isSet) throw SwarmStopped()
            throw e
        } finally {
            calls.remove(call)
        }
        if (job.received != length) throw IOException("short piece (${job.received} of $length bytes)")
        if (!digest.digest().contentEquals(info.digest(index))) throw BadPiece("piece failed SHA-256 verification")
    }

    private fun settle(job: Job, error: Throwable?) {
        val source = job.source
        synchronized(lock) {
            source.inflight--
            inflight.remove(job.index)
            if (error == null) {
                state.mark(job.index)
                source.failures = 0
                source.bytes += job.received
                val contributor = progress.contributors.getOrPut(source.peer.peerId) { Contributor(source.peer.peerId, source.peer.name) }
                contributor.name = source.peer.name
                contributor.bytes.addAndGet(job.received.toLong())
                return
            }
            state.setPending(job.index, false)
            when (error) {
                is SwarmStopped -> return
                is LocalStorageError -> {
                    fatal = error.error
                    return
                }
                is BadPiece -> {
                    source.badPieces++
                    source.lastError = error.message ?: ""
                    source.retryAt = monotonicMs() + BAD_PIECE_REST // no new requests while we see
                    CoreLog.w(TAG, "piece ${job.index} of ${info.fileId.take(12)} from ${source.peer.name}: ${error.message}")
                    if (source.badPieces >= MAX_BAD_PIECES) {
                        source.dropped = true
                        reorder()
                    }
                    return
                }
                is Gone -> {
                    source.dropped = true
                    source.lastError = error.message ?: ""
                    CoreLog.i(TAG, "swarm: ${source.peer.name} ${error.message}")
                    reorder()
                    return
                }
            }
            if (error !is IOException) CoreLog.e(TAG, "swarm request to ${source.peer.name} failed", error)
            source.failures++
            source.lastError = describeNetworkError(error)
            val rest = if (source.failures >= MAX_FAILURES) FAILURE_REST else minOf(8_000L, 1_000L shl (source.failures - 1))
            source.retryAt = monotonicMs() + rest
        }
    }

    // ----- giving up -------------------------------------------------------------------------

    private class Standing(
        val live: List<Source>,
        val available: List<Source>,
        val reachable: List<Source>,
        val bad: List<Source>,
        val failed: List<Source>,
    )

    /** Nothing in flight and nothing to request: decide whether to wait or stop. */
    private fun checkStuck(stuckFor: Long) {
        val s = synchronized(lock) {
            val live = sources.values.filter { it.present && !it.dropped }
            val missing = (0 until n).filter { !state.has(it) }
            val available = live.filter { src -> missing.any { src.has(it) } }
            Standing(
                live, available, available.filter { it.failures < MAX_FAILURES },
                sources.values.filter { it.badPieces >= MAX_BAD_PIECES },
                sources.values.filter { it.failures >= MAX_FAILURES || !it.present },
            )
        }
        if (s.reachable.isNotEmpty()) return // resting briefly after an error; it will be asked again
        if (s.bad.isNotEmpty() && s.available.isEmpty()) throw SwarmUnusable("pieces from ${names(s.bad)} failed SHA-256 verification")
        if (s.failed.isNotEmpty() && s.live.isEmpty()) throw SwarmFailed(offlineMessage(s.failed))
        if (s.available.isNotEmpty()) throw SwarmFailed(offlineMessage(s.available)) // every peer with the missing pieces is unreachable
        if (s.live.isEmpty()) throw SwarmFailed("No online peer has this file.")
        if (stuckFor >= STUCK_TIMEOUT) {
            throw SwarmFailed(
                "The peers still online don't have the rest of this file (${percent()} done). " +
                    "The partial file was kept; press Resume to continue.",
            )
        }
    }

    private fun offlineMessage(sources: List<Source>): String {
        val error = sources.firstOrNull { it.lastError.isNotEmpty() }?.lastError ?: "no answer"
        if (state.haveBytes > startedWith) {
            return "${names(sources)} went offline during the transfer (${percent()} done, $error). " +
                "The partial file was kept; press Resume to continue."
        }
        return "Could not reach ${names(sources)}: $error."
    }

    private fun percent(): String = if (info.size > 0) "${state.haveBytes * 100 / info.size}%" else "0%"

    // ----- knowing who has what --------------------------------------------------------------

    private fun refreshLoop() {
        while (!done) {
            Thread.sleep(REFRESH_INTERVAL)
            if (done) break
            try {
                refresh()
            } catch (e: Exception) {
                CoreLog.e(TAG, "swarm refresh failed", e)
            }
        }
    }

    private fun refresh() {
        val fileId = info.fileId
        val seedPeers = seeds().associateBy { it.peerId }
        val others = peers.online().filter { it.swarm && fileId in it.swarmFiles && it.peerId !in seedPeers }
        val haves = HashMap<String, Pair<ByteArray?, ByteArray?>>()
        for (peer in others) fetchHave(peer)?.let { haves[peer.peerId] = it }
        synchronized(lock) {
            for (source in sources.values) source.present = source.peer.peerId in seedPeers || source.peer.peerId in haves
            for ((peerId, peer) in seedPeers) {
                val source = sources[peerId]
                if (source == null || !source.seed) sources[peerId] = Source(peer, seed = true) else source.peer = peer
            }
            for (peer in others) {
                val found = haves[peer.peerId] ?: continue
                val source = sources.getOrPut(peer.peerId) { Source(peer, seed = false) }
                source.peer = peer
                source.have = found.first
                source.pending = found.second
            }
            reorder()
        }
    }

    /** A fellow downloader's (have, pending) maps; have is null if it has everything. Null if it can't tell us right now. */
    private fun fetchHave(peer: Peer): Pair<ByteArray?, ByteArray?>? {
        val path = "/api/swarm/${info.fileId}"
        val headers = auth.signRequest("GET", path)
        return try {
            val builder = Request.Builder().url(peer.baseUrl() + path).header("Accept-Encoding", "identity")
            for ((k, v) in headers) builder.header(k, v)
            haveClient.newCall(builder.build()).execute().use { resp ->
                if (resp.code != 200 || !auth.verifyResponse(headers[TOKEN_HEADER], resp.header(PROOF_HEADER))) return null
                val data = json.parseToJsonElement(readLimited(resp, 1024 * 1024).toString(Charsets.UTF_8)) as? JsonObject
                    ?: return null
                if ((data["pieces"] as? JsonPrimitive)?.intOrNull != n) return null
                if ((data["complete"] as? JsonPrimitive)?.booleanOrNull == true) return null to null
                val pending = (data["pending"] as? JsonPrimitive)?.takeIf { it.isString }?.content
                decodeBits((data["have"] as JsonPrimitive).content, n) to pending?.let { decodeBits(it, n) }
            }
        } catch (e: Exception) {
            null
        }
    }

    // ----- progress --------------------------------------------------------------------------

    private fun report() {
        val inFlight = synchronized(lock) { inflight.values.sumOf { it.received.toLong() } }
        progress.received = state.haveBytes + inFlight
        val elapsed = monotonicMs() - speedWindowStart
        if (elapsed >= 500) {
            val total = transferred.get()
            val rate = (total - speedWindowCount) * 1000.0 / elapsed
            progress.speed = if (progress.speed == 0.0) rate else 0.6 * rate + 0.4 * progress.speed
            speedWindowStart = monotonicMs()
            speedWindowCount = total
        }
    }
}

private fun names(sources: List<Source>): String {
    val names = sources.map { it.peer.name }.toSortedSet().toList()
    return when (names.size) {
        0 -> "the other peers"
        1 -> names[0]
        else -> names.dropLast(1).joinToString(", ") + " and " + names.last()
    }
}
