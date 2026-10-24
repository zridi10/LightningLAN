package lightninglan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.longOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/*
 * The peer table (lightninglan/peers.py): peers found by discovery, verified and kept fresh by
 * polling their index. Polling GET /api/files doubles as the health check. A peer is listed only
 * after a successful poll; three failures in a row take it off the list. While its mDNS record
 * still exists it is re-probed at a slower pace and returns as soon as it answers again.
 */

private const val TAG = "peers"
private const val MAX_FAILURES = 3
private const val MAX_INDEX_BYTES = 64L * 1024 * 1024
private const val MAX_FILES_PER_PEER = 200_000
private const val MAX_SWARM_FILES = 10_000
private const val OFFLINE_RETRY_INTERVAL = 10_000L
private const val IGNORED_RETRY_INTERVAL = 30_000L
private const val MAX_DIRECT = 16
private const val DIRECT_FORGET_FAILURES = 12 // failed polls in a row: about two minutes

/** The service name of a peer met directly ([PeerTable.meet]), not through discovery. */
private const val DIRECT_SERVICE = "direct:"

const val PENDING = "pending"
const val ONLINE = "online"
const val OFFLINE = "offline"
const val IGNORED = "ignored"

/** A peer as discovery announced it. [serviceName] identifies the announcement. */
data class ServiceRecord(
    val serviceName: String,
    val peerId: String,
    val name: String,
    val addresses: List<String>,
    val port: Int,
    val auth: Boolean,
)

/** A file in another peer's index. [name] is its relative path exactly as the peer reported it (untrusted). */
data class RemoteFile(val sha256: String, val name: String, val size: Long, val mtime: Double)

class Peer(val peerId: String, name: String, port: Int, addresses: List<String>) {
    @Volatile var name: String = name
    @Volatile var port: Int = port
    @Volatile var addresses: List<String> = addresses
    val serviceNames: MutableSet<String> = ConcurrentHashMap.newKeySet()
    @Volatile var state: String = PENDING
    @Volatile var failures = 0
    @Volatile var lastSeen: Long? = null
    @Volatile var lastError: String? = null
    @Volatile var ignoreReason: String? = null
    @Volatile var address: String? = null // the address that last answered
    @Volatile var files: List<RemoteFile> = emptyList()
    @Volatile var indexing = 0
    @Volatile var etag: String? = null
    @Volatile var nextPoll = 0L
    @Volatile var swarm = false // serves piece lists and swarm pieces (LightningLAN 1.1+)
    @Volatile var swarmFiles: Set<String> = emptySet() // files it can pass pieces of
    @Volatile var play = false // plays head-to-head games (LightningLAN 1.2+)
    @Volatile var nameAuto = false // its name was made up by LightningLAN (1.3+), so it gives way when names clash
    @Volatile var games: Set<String>? = null // which ones (null: the first three, from before it said)

    val online: Boolean get() = state == ONLINE

    /** Met directly (on a hotspot, see Join.kt), not (only) through discovery. */
    val direct: Boolean get() = serviceNames.any { it.startsWith(DIRECT_SERVICE) }

    fun candidateAddresses(): List<String> {
        val current = address
        val ordered = if (current != null && current in addresses) listOf(current) else emptyList()
        return ordered + addresses.filter { it !in ordered }
    }

    fun baseUrl(): String = "http://${address ?: addresses.first()}:$port"
}

class PollError(message: String) : Exception(message)

private class IgnoredPeer(message: String) : Exception(message)

private val json = Json { ignoreUnknownKeys = true }

/** Whether a peer's index says it takes part in swarms, and the files it has pieces of. */
fun parseSwarm(payload: JsonObject): Pair<Boolean, Set<String>> {
    val raw = payload["swarm"] as? JsonArray ?: return false to emptySet()
    val ids = raw.asSequence().take(MAX_SWARM_FILES)
        .mapNotNull { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content }
        .filter { SHA256_RE.matches(it) }.toSet()
    return true to ids
}

class ParsedIndex(val name: String, val files: List<RemoteFile>, val pending: Int)

/** Validate another peer's /api/files response. Malformed entries are dropped. */
fun parseIndex(payload: JsonElement, expectedPeerId: String): ParsedIndex {
    if (payload !is JsonObject) throw PollError("index is not a JSON object")
    if ((payload["peer_id"] as? JsonPrimitive)?.content != expectedPeerId) throw PollError("a different peer answered at this address")
    val rawFiles = payload["files"] as? JsonArray ?: throw PollError("index has no file list")
    val files = ArrayList<RemoteFile>()
    for (item in rawFiles.take(MAX_FILES_PER_PEER)) {
        if (item !is JsonObject) continue
        val sha = (item["sha256"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: continue
        if (!SHA256_RE.matches(sha)) continue
        val name = (item["name"] as? JsonPrimitive)?.takeIf { it.isString }?.content ?: continue
        if (name.isEmpty() || name.length > 1024) continue
        val sizeValue = item["size"] as? JsonPrimitive ?: continue
        if (sizeValue.isString || '.' in sizeValue.content || 'e' in sizeValue.content.lowercase()) continue
        val size = sizeValue.longOrNull ?: continue
        if (size < 0) continue
        val mtimeValue = item["mtime"] as? JsonPrimitive
        val mtime = if (mtimeValue == null || mtimeValue.isString) 0.0 else mtimeValue.doubleOrNull ?: 0.0
        files.add(RemoteFile(sha, name, size, mtime))
    }
    val rawName = (payload["name"] as? JsonPrimitive)?.takeIf { it.isString }?.content
    val name = rawName?.let { displayName(it).trim().take(64) } ?: ""
    val indexing = payload["indexing"] as? JsonObject
    val pending = (indexing?.get("pending") as? JsonPrimitive)?.takeIf { !it.isString }?.intOrNull ?: 0
    return ParsedIndex(name, files, pending)
}

class PeerTable(
    val ownPeerId: String,
    private val auth: Authenticator,
    private val refreshMs: Long,
    private val client: OkHttpClient,
) : DiscoveryListener {
    private val peers = ConcurrentHashMap<String, Peer>()
    private val versionCounter = AtomicInteger()

    /** Called after each round of polls (on the poll loop's thread). */
    @Volatile
    var afterPoll: (() -> Unit)? = null

    /** Bumps whenever the set of online peers or their files change. */
    val version: Int get() = versionCounter.get()

    private val wakeLock = Object()
    private var wake = false

    @Volatile
    private var running = false
    private var loop: Thread? = null
    private var pollers: ExecutorService? = null

    // ----- lifecycle -------------------------------------------------------------------------

    @Synchronized
    fun start() {
        if (running) return
        running = true
        pollers = Executors.newFixedThreadPool(16) { r -> Thread(r, "lightninglan-poll").apply { isDaemon = true } }
        loop = thread(name = "lightninglan-peers", isDaemon = true) { run() }
    }

    @Synchronized
    fun stop() {
        if (!running) return
        running = false
        wake()
        loop?.join(10_000)
        loop = null
        pollers?.shutdownNow()
        pollers = null
    }

    /** Forget every peer (this peer left the network; discovery finds them again on return). */
    fun clear() {
        peers.clear()
        versionCounter.incrementAndGet()
    }

    // ----- queries ---------------------------------------------------------------------------

    fun get(peerId: String): Peer? = peers[peerId]

    fun online(): List<Peer> = peers.values.filter { it.online }.sortedBy { it.name.lowercase() }

    fun ignored(): List<Peer> = peers.values.filter { it.state == IGNORED }

    /** Every peer found, online or not. */
    fun known(): List<Peer> = peers.values.toList()

    // ----- discovery callbacks ---------------------------------------------------------------

    @Synchronized
    override fun serviceFound(record: ServiceRecord) {
        if (record.peerId == ownPeerId) return
        var peer = peers[record.peerId]
        if (peer == null) {
            peer = Peer(record.peerId, record.name, record.port, record.addresses)
            peers[record.peerId] = peer
            CoreLog.i(TAG, "discovered peer ${record.name} at ${record.addresses.first()}:${record.port}")
        } else {
            if (peer.port != record.port || peer.addresses != record.addresses) peer.address = null
            peer.port = record.port
            peer.addresses = record.addresses
        }
        peer.serviceNames.add(record.serviceName)
        peer.nextPoll = 0
        wake()
    }

    @Synchronized
    override fun serviceLost(serviceName: String) {
        for (peer in peers.values.toList()) {
            if (peer.serviceNames.remove(serviceName) && peer.serviceNames.isEmpty()) {
                peers.remove(peer.peerId)
                CoreLog.i(TAG, "peer ${peer.name} left the network")
                if (peer.online) versionCounter.incrementAndGet()
            }
        }
    }

    // ----- peers met directly (Join.kt) --------------------------------------------------------

    /**
     * A peer met without discovery: the one whose hotspot this phone joined, or one that introduced
     * itself (POST /api/introduce). It is polled like any other, and forgotten after about two
     * minutes out of reach unless discovery knows it too; meeting it again starts that over. False
     * if too many peers met this way are known already.
     */
    @Synchronized
    fun meet(peerId: String, name: String, address: String, port: Int): Boolean {
        if (peerId == ownPeerId) return false
        var peer = peers[peerId]
        if (peer == null) {
            if (peers.values.count { it.direct } >= MAX_DIRECT) return false
            peer = Peer(peerId, name, port, listOf(address))
            peers[peerId] = peer
            CoreLog.i(TAG, "met peer $name at $address:$port")
        } else if (peer.port != port || address !in peer.addresses) {
            peer.address = null
            peer.port = port
            peer.addresses = listOf(address) + peer.addresses.filter { it != address }
        }
        peer.serviceNames.add(DIRECT_SERVICE + peerId)
        peer.failures = 0
        peer.nextPoll = 0
        wake()
        return true
    }

    /** Forgets a peer met directly (this phone left its hotspot); if discovery knows it too, it stays. */
    fun part(peerId: String) = serviceLost(DIRECT_SERVICE + peerId)

    @Synchronized
    private fun forget(peer: Peer) {
        if (peers.remove(peer.peerId, peer)) CoreLog.i(TAG, "forgot ${peer.name}, out of reach for a while")
    }

    fun wake() {
        synchronized(wakeLock) {
            wake = true
            wakeLock.notifyAll()
        }
    }

    /** Our passphrase changed: check every peer again right away, including ignored ones. */
    fun credentialsChanged() {
        for (peer in peers.values) {
            if (peer.state == IGNORED) {
                peer.state = PENDING
                peer.ignoreReason = null
                peer.failures = 0
            }
            peer.etag = null
            peer.nextPoll = 0
        }
        versionCounter.incrementAndGet()
        wake()
    }

    // ----- polling ---------------------------------------------------------------------------

    private fun run() {
        while (running) {
            val now = monotonicMs()
            val due = peers.values.filter { it.nextPoll <= now }
            val executor = pollers
            if (due.isNotEmpty() && executor != null) {
                val futures = due.mapNotNull { peer -> runCatching { executor.submit { poll(peer) } }.getOrNull() }
                for (f in futures) runCatching { f.get() }
                try {
                    afterPoll?.invoke()
                } catch (e: Exception) {
                    CoreLog.e(TAG, "after-poll check failed", e)
                }
            }
            synchronized(wakeLock) {
                if (!wake && running) {
                    val nextDue = peers.values.minOfOrNull { it.nextPoll } ?: (monotonicMs() + refreshMs)
                    val delay = (nextDue - monotonicMs()).coerceIn(50, refreshMs)
                    wakeLock.wait(delay)
                }
                wake = false
            }
        }
    }

    private fun poll(peer: Peer) {
        var error = "no address answered"
        for (address in peer.candidateAddresses()) {
            try {
                pollAddress(peer, address)
                return
            } catch (e: IgnoredPeer) {
                ignore(peer, e.message ?: "ignored")
                return
            } catch (e: PollError) {
                error = e.message ?: "invalid answer"
            } catch (e: IOException) {
                error = describeNetworkError(e)
            } catch (e: Exception) { // never let one bad peer stall the poll loop
                CoreLog.e(TAG, "unexpected error polling ${peer.name}", e)
                error = e.message ?: e.javaClass.simpleName
            }
        }
        failed(peer, error)
    }

    private fun pollAddress(peer: Peer, address: String) {
        val path = "/api/files"
        val signed = auth.signRequest("GET", path)
        val builder = Request.Builder().url("http://$address:${peer.port}$path").header("Accept-Encoding", "identity")
        for ((k, v) in signed) builder.header(k, v)
        val etag = peer.etag
        if (etag != null && peer.online) builder.header("If-None-Match", etag)
        val body: ByteArray
        val newEtag: String?
        client.newCall(builder.build()).execute().use { resp ->
            if (resp.code == 401 || resp.code == 403) {
                throw IgnoredPeer(authReason(readLimited(resp, 64 * 1024), auth.enabled))
            }
            if (!auth.verifyResponse(signed[TOKEN_HEADER], resp.header(PROOF_HEADER))) {
                throw IgnoredPeer(AUTH_REASONS.getValue("no_proof"))
            }
            if (resp.code == 304) {
                succeeded(peer, address)
                return
            }
            if (resp.code != 200) throw PollError("HTTP ${resp.code}")
            body = readLimited(resp, MAX_INDEX_BYTES)
            newEtag = resp.header("etag")
        }
        val payload = try {
            json.parseToJsonElement(body.toString(Charsets.UTF_8))
        } catch (e: Exception) {
            throw PollError("invalid JSON")
        }
        val parsed = parseIndex(payload, peer.peerId)
        val (swarm, swarmFiles) = parseSwarm(payload as JsonObject)
        peer.swarm = swarm
        peer.swarmFiles = swarmFiles
        peer.play = (payload["play"] as? JsonPrimitive)?.let { !it.isString && it.content == "1" } == true
        peer.games = (payload["games"] as? JsonArray)?.take(50)?.mapNotNull { (it as? JsonPrimitive)?.takeIf { p -> p.isString }?.content }?.toSet()
        peer.nameAuto = (payload["name_auto"] as? JsonPrimitive)?.let { !it.isString && it.content == "true" } == true
        if (parsed.name.isNotEmpty() && parsed.name != peer.name) {
            peer.name = parsed.name
            versionCounter.incrementAndGet() // ThunderCLOUD shows its files under the new name
        }
        if (parsed.files != peer.files || !peer.online) versionCounter.incrementAndGet()
        peer.files = parsed.files
        peer.indexing = parsed.pending
        peer.etag = newEtag
        succeeded(peer, address)
    }

    private fun succeeded(peer: Peer, address: String) {
        if (!peer.online) {
            CoreLog.i(TAG, "peer ${peer.name} is online ($address:${peer.port})")
            versionCounter.incrementAndGet()
        }
        peer.state = ONLINE
        peer.address = address
        peer.failures = 0
        peer.lastError = null
        peer.ignoreReason = null
        peer.lastSeen = System.currentTimeMillis()
        peer.nextPoll = monotonicMs() + refreshMs
    }

    private fun failed(peer: Peer, error: String) {
        peer.failures++
        peer.lastError = error
        if (peer.state == ONLINE && peer.failures >= MAX_FAILURES) {
            CoreLog.i(TAG, "peer ${peer.name} is offline after ${peer.failures} failed checks: $error")
            peer.state = OFFLINE
            peer.etag = null
            versionCounter.incrementAndGet()
        } else if (peer.state == PENDING && peer.failures >= MAX_FAILURES) {
            peer.state = OFFLINE
        }
        val retry = if (peer.state == OFFLINE) minOf(OFFLINE_RETRY_INTERVAL, 4 * refreshMs) else refreshMs
        peer.nextPoll = monotonicMs() + retry
        // Met directly and gone (it left the hotspot, say): no discovery will say goodbye for it.
        if (peer.state != ONLINE && peer.failures >= DIRECT_FORGET_FAILURES && peer.serviceNames.all { it.startsWith(DIRECT_SERVICE) }) {
            forget(peer)
        }
    }

    private fun ignore(peer: Peer, reason: String) {
        if (peer.state != IGNORED) {
            CoreLog.w(TAG, "ignoring peer ${peer.name}: $reason")
            if (peer.online) versionCounter.incrementAndGet()
        }
        peer.state = IGNORED
        peer.ignoreReason = reason
        peer.files = emptyList()
        peer.etag = null
        peer.nextPoll = monotonicMs() + IGNORED_RETRY_INTERVAL
    }
}

/** Reads a response body, refusing more than [limit] bytes. */
fun readLimited(resp: Response, limit: Long): ByteArray {
    val body = resp.body ?: return ByteArray(0)
    val out = java.io.ByteArrayOutputStream()
    val buf = ByteArray(64 * 1024)
    body.byteStream().use { input ->
        while (true) {
            val n = input.read(buf)
            if (n < 0) break
            if (out.size() + n > limit) throw PollError("response too large")
            out.write(buf, 0, n)
        }
    }
    return out.toByteArray()
}

internal fun authReason(body: ByteArray, weHavePassphrase: Boolean): String {
    var reason = try {
        ((json.parseToJsonElement(body.toString(Charsets.UTF_8)) as? JsonObject)?.get("reason") as? JsonPrimitive)?.content
    } catch (e: Exception) {
        null
    }
    if (reason == "missing" && weHavePassphrase) reason = "malformed"
    return AUTH_REASONS[reason ?: ""] ?: "peer rejected our credentials"
}
