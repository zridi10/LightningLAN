package lightninglan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import java.io.File
import java.util.Base64
import kotlin.concurrent.thread

/*
 * Index of the shared files: relative path, size, modification time and SHA-256 of each
 * (lightninglan/indexer.py). Hashes are cached in memory and in a JSON file, keyed by the file's
 * storage key, and a file is only hashed again when its size or modification time changes.
 */

private const val TAG = "indexer"
private const val CACHE_FORMAT_VERSION = 2

data class FileEntry(val relpath: String, val size: Long, val mtimeMs: Long, val sha256: String, val key: String)

class IndexSnapshot(val entries: List<FileEntry>, val pending: Int, val version: Int)

private class Cached(val size: Long, val mtimeMs: Long, val sha256: String, val pieces: ByteArray?) {
    fun usableFor(size: Long, mtimeMs: Long) = this.size == size && this.mtimeMs == mtimeMs

    override fun equals(other: Any?) = other is Cached && other.size == size && other.mtimeMs == mtimeMs &&
        other.sha256 == sha256 && sameBytes(other.pieces, pieces)

    private fun sameBytes(a: ByteArray?, b: ByteArray?) = if (a == null || b == null) a == b else a.contentEquals(b)

    override fun hashCode() = sha256.hashCode()
}

private class Found(val relpath: String, val key: String, val size: Long, val mtimeMs: Long)

class Indexer(tree: ShareTree?, private val cacheFile: File?, private val intervalMs: Long) {
    @Volatile
    var snapshot = IndexSnapshot(emptyList(), 0, 0)
        private set

    @Volatile
    var tree: ShareTree? = tree
        private set

    /** Files hashed since start (diagnostics and tests). */
    @Volatile
    var hashCount = 0
        private set

    @Volatile
    private var byHash: Map<String, FileEntry> = emptyMap()

    @Volatile
    private var pieces: Map<String, ByteArray> = emptyMap()
    private var cache: Map<String, Cached> = loadCache()
    private val scanLock = Any()
    private val publishLock = Any()
    private var generation = 0 // bumped by setTree(); results of older scans are dropped
    private val wakeLock = Object()
    private var wake = false

    @Volatile
    private var stopped = false

    @Volatile
    private var abort = false // interrupts hashing (stop or setTree)
    private var worker: Thread? = null

    fun lookup(sha256: String): FileEntry? = byHash[sha256]

    /** Piece hashes of a shared file that is big enough to be split into pieces. */
    fun piecesInfo(sha256: String): PieceInfo? {
        val entry = byHash[sha256] ?: return null
        val hashes = pieces[sha256] ?: return null
        return PieceInfo(sha256, entry.size, hashes)
    }

    fun open(entry: FileEntry): ReadableFile? = try {
        tree?.open(entry.key)
    } catch (e: Exception) {
        CoreLog.d(TAG, "cannot open ${entry.relpath}: $e")
        null
    }

    fun start() {
        stopped = false
        worker = thread(name = "lightninglan-indexer", isDaemon = true) { run() }
    }

    fun stop() {
        stopped = true
        abort = true
        requestRescan()
        worker?.join(10_000)
        worker = null
    }

    fun requestRescan() {
        synchronized(wakeLock) {
            wake = true
            wakeLock.notifyAll()
        }
    }

    /**
     * Share a different folder: the index empties at once and the new one is scanned. The same
     * folder with other files added or taken away (same [ShareTree.id]) is simply scanned again.
     */
    fun setTree(tree: ShareTree?) {
        if (tree != null && tree.id == this.tree?.id) {
            this.tree = tree
            requestRescan()
            return
        }
        synchronized(publishLock) {
            generation++
            this.tree = tree
            abort = true
            publishLocked(emptyMap(), emptyMap(), 0)
        }
        requestRescan()
    }

    /** One full pass over the shared files. Returns true if the index changed. */
    fun scan(): Boolean = synchronized(scanLock) { scanOnce() }

    private fun run() {
        while (!stopped) {
            try {
                scan()
            } catch (e: Exception) { // keep indexing alive no matter what one pass hits
                CoreLog.e(TAG, "index scan failed", e)
            }
            synchronized(wakeLock) {
                if (!wake && !stopped) wakeLock.wait(intervalMs)
                wake = false
            }
        }
    }

    private fun scanOnce(): Boolean {
        val myGeneration: Int
        val root: ShareTree?
        synchronized(publishLock) {
            myGeneration = generation
            root = tree
            if (!stopped) abort = false
        }
        val entries = LinkedHashMap<String, FileEntry>()
        val newPieces = HashMap<String, ByteArray>()
        val newCache = HashMap<String, Cached>()
        val todo = ArrayList<Found>()
        if (root != null) {
            for (found in walk(root)) {
                val cacheKey = root.id + "\u0000" + found.key
                val cached = cache[cacheKey]
                if (cached != null && cached.usableFor(found.size, found.mtimeMs)) {
                    entries[found.relpath] = FileEntry(found.relpath, found.size, found.mtimeMs, cached.sha256, found.key)
                    newCache[cacheKey] = cached
                    if (cached.pieces != null && cached.pieces.isNotEmpty()) {
                        newPieces[cached.sha256] = cached.pieces
                    } else if (swarmable(found.size)) {
                        todo.add(found) // listed already; piece hashes still to do
                    }
                } else {
                    todo.add(found)
                }
            }
        }

        var changed = publish(entries, newPieces, todo.size, myGeneration)
        var lastPublish = monotonicMs()
        for ((i, found) in todo.withIndex()) {
            val hashes = try {
                val file = root!!.open(found.key) ?: continue
                file.use {
                    if (it.size != found.size || it.mtimeMs / 1000 != found.mtimeMs / 1000) {
                        null // modified since the listing; the next pass picks it up
                    } else {
                        hashFile(it) { abort }
                    }
                }
            } catch (e: HashCancelled) {
                return changed
            } catch (e: Exception) {
                CoreLog.d(TAG, "skipping ${found.relpath}: $e")
                null
            } ?: continue
            hashCount++
            entries[found.relpath] = FileEntry(found.relpath, found.size, found.mtimeMs, hashes.sha256, found.key)
            newCache[root!!.id + "\u0000" + found.key] =
                Cached(found.size, found.mtimeMs, hashes.sha256, hashes.pieces.takeIf { it.isNotEmpty() })
            if (hashes.pieces.isNotEmpty()) newPieces[hashes.sha256] = hashes.pieces
            if (monotonicMs() - lastPublish > 500) {
                changed = publish(entries, newPieces, todo.size - i - 1, myGeneration) || changed
                lastPublish = monotonicMs()
            }
        }
        changed = publish(entries, newPieces, 0, myGeneration) || changed

        if (myGeneration != generation) return changed // the shared folder changed during this scan
        if (newCache != cache) {
            cache = newCache
            saveCache()
        }
        return changed
    }

    private fun publish(entries: Map<String, FileEntry>, newPieces: Map<String, ByteArray>, pending: Int, myGeneration: Int): Boolean =
        synchronized(publishLock) {
            if (myGeneration != generation) false else publishLocked(entries, newPieces, pending)
        }

    private fun publishLocked(entries: Map<String, FileEntry>, newPieces: Map<String, ByteArray>, pending: Int): Boolean {
        val ordered = entries.values.sortedWith(compareBy({ it.relpath.lowercase() }, { it.relpath }))
        val current = snapshot
        pieces = HashMap(newPieces)
        if (ordered == current.entries && pending == current.pending) return false
        val index = HashMap<String, FileEntry>()
        for (entry in ordered) index.putIfAbsent(entry.sha256, entry)
        byHash = index
        snapshot = IndexSnapshot(ordered, pending, current.version + 1)
        return true
    }

    /** Every shareable file: hidden, temporary and junk files and folders are skipped. */
    private fun walk(root: ShareTree): List<Found> {
        val found = ArrayList<Found>()
        val stack = ArrayDeque<Pair<String?, String>>()
        stack.addLast(null to "")
        while (stack.isNotEmpty()) {
            val (dirKey, prefix) = stack.removeLast()
            val children = try {
                root.list(dirKey)
            } catch (e: Exception) {
                CoreLog.d(TAG, "cannot list $prefix: $e")
                null
            } ?: continue
            for (child in children) {
                if (isIgnoredName(child.name)) continue
                val rel = prefix + child.name
                if (child.isDirectory) stack.addLast(child.key to "$rel/") else found.add(Found(rel, child.key, child.size, child.mtimeMs))
            }
        }
        return found
    }

    // ----- hash cache persistence ------------------------------------------------------------

    private fun loadCache(): Map<String, Cached> {
        val file = cacheFile ?: return emptyMap()
        if (!file.exists()) return emptyMap()
        return try {
            val data = Json.parseToJsonElement(file.readText()) as JsonObject
            if ((data["version"] as? JsonPrimitive)?.int != CACHE_FORMAT_VERSION) return emptyMap()
            val result = HashMap<String, Cached>()
            for ((key, value) in data["files"] as JsonObject) {
                val v = value as? JsonArray ?: continue
                if (v.size !in 3..4) continue
                val pieces = if (v.size == 4) Base64.getDecoder().decode((v[3] as JsonPrimitive).content) else null
                result[key] = Cached(
                    (v[0] as JsonPrimitive).long, (v[1] as JsonPrimitive).long, (v[2] as JsonPrimitive).content, pieces,
                )
            }
            result
        } catch (e: Exception) {
            CoreLog.w(TAG, "ignoring unreadable hash cache $file: $e")
            emptyMap()
        }
    }

    private fun saveCache() {
        val file = cacheFile ?: return
        val payload = buildJsonObject {
            put("version", CACHE_FORMAT_VERSION)
            put("files", buildJsonObject {
                for ((key, c) in cache) {
                    put(key, buildJsonArray {
                        add(JsonPrimitive(c.size))
                        add(JsonPrimitive(c.mtimeMs))
                        add(JsonPrimitive(c.sha256))
                        if (c.pieces != null) add(JsonPrimitive(Base64.getEncoder().encodeToString(c.pieces)))
                    })
                }
            })
        }
        try {
            file.parentFile?.mkdirs()
            val tmp = File(file.path + ".tmp")
            tmp.writeText(payload.toString())
            if (!tmp.renameTo(file)) {
                file.delete()
                tmp.renameTo(file)
            }
        } catch (e: Exception) {
            CoreLog.w(TAG, "could not save hash cache: $e")
        }
    }
}
