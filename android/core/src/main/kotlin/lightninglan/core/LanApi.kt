package lightninglan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import java.io.OutputStream
import java.nio.ByteBuffer
import java.util.concurrent.atomic.AtomicLong

/*
 * The peer-to-peer HTTP API, answered the way lightninglan/server.py and security.py answer it:
 *
 *   GET /api/files                 this peer's index (ETag / If-None-Match)
 *   GET /api/download/<id>         a shared file, with HTTP Range
 *   GET /api/pieces/<id>           its piece hashes (files of more than one piece)
 *   GET /api/swarm/<id>            which pieces of a file we are downloading we have
 *   GET /api/piece/<id>/<n>        one verified piece of such a file
 *   GET /api/games                 this peer's half of its head-to-head games (Play.kt)
 *   POST /api/introduce            a peer that joined this phone's hotspot says where it is (Join.kt;
 *                                  the app's own: the desktop app doesn't make hotspots)
 *
 * Only GET and HEAD, but for that one. While this peer is stopped they answer 503 and transfers in
 * progress are cut off. With a passphrase they need a valid peer token, and responses carry a proof header.
 *
 * The phone page (/phone, /static/) is PhoneApi.kt's; opening this phone's address in a browser
 * leads there.
 */

private const val STREAM_CHUNK = 256 * 1024
private val LAN_PREFIXES = listOf("/api/download/", "/api/pieces/", "/api/swarm/", "/api/piece/")

fun isLanRoute(path: String): Boolean = path == "/api/files" || path == "/api/games" || LAN_PREFIXES.any { path.startsWith(it) }

/** Counts the bytes sent to other peers, and ends every transfer at once when this peer stops sharing. */
class UploadLimiter {
    @Volatile
    var open = true
        private set

    @Volatile
    var generation = 0 // bumps on every cut-off; transfers started before it end
        private set
    val sent = AtomicLong()

    /** End the transfers in progress (at their next chunk) and refuse new ones until [reopen]. */
    @Synchronized
    fun cutOff() {
        open = false
        generation++
    }

    fun reopen() {
        open = true
    }

    fun allows(generation: Int): Boolean = open && this.generation == generation
}

sealed class RangeResult {
    data class Bytes(val start: Long, val end: Long) : RangeResult()
    object Unsatisfiable : RangeResult()
}

private val RANGE_RE = Regex("^\\s*bytes\\s*=\\s*(\\d*)\\s*-\\s*(\\d*)\\s*$", RegexOption.IGNORE_CASE)

/**
 * A single-range "bytes=" header as an inclusive range. Null when the header should be ignored
 * (malformed or multi-range: the whole file is sent instead).
 */
fun parseRange(header: String, size: Long): RangeResult? {
    val m = RANGE_RE.matchEntire(header) ?: return null
    val (first, last) = m.destructured
    if (first.isEmpty() && last.isEmpty()) return null
    if (first.isEmpty()) { // suffix range: the last N bytes
        val n = last.toBigInteger()
        if (n.signum() == 0 || size == 0L) return RangeResult.Unsatisfiable
        val start = if (n >= size.toBigInteger()) 0L else size - n.toLong()
        return RangeResult.Bytes(start, size - 1)
    }
    val startBig = first.toBigInteger()
    if (startBig >= size.toBigInteger()) return RangeResult.Unsatisfiable
    val start = startBig.toLong()
    val endBig = if (last.isNotEmpty()) last.toBigInteger() else (size - 1).toBigInteger()
    if (endBig < startBig) return null
    return RangeResult.Bytes(start, minOf(endBig, (size - 1).toBigInteger()).toLong())
}

/** Percent-encodes a file name for Content-Disposition (as Python's urllib.parse.quote does). */
fun quoteFilename(name: String): String {
    val sb = StringBuilder()
    for (b in name.toByteArray(Charsets.UTF_8)) {
        val c = b.toInt() and 0xff
        if ((c in 'A'.code..'Z'.code) || (c in 'a'.code..'z'.code) || (c in '0'.code..'9'.code) || c.toChar() in "_.-~/") {
            sb.append(c.toChar())
        } else {
            sb.append('%').append("0123456789ABCDEF"[c shr 4]).append("0123456789ABCDEF"[c and 15])
        }
    }
    return sb.toString()
}

/** Streams part of a file, ending the connection if this peer stops sharing meanwhile. */
private class FileBody(
    private val file: ReadableFile,
    private val start: Long,
    override val length: Long,
    private val limiter: UploadLimiter,
) : HttpBody() {
    override fun writeTo(out: OutputStream) {
        val generation = limiter.generation
        val buf = ByteBuffer.allocate(STREAM_CHUNK)
        var position = start
        var remaining = length
        while (remaining > 0) {
            if (!limiter.allows(generation)) throw ConnectionCut()
            buf.clear()
            buf.limit(minOf(STREAM_CHUNK.toLong(), remaining).toInt())
            val n = file.read(buf, position)
            if (n <= 0) throw ConnectionCut() // the file shrank: the client sees a short body
            out.write(buf.array(), 0, n)
            position += n
            remaining -= n
            limiter.sent.addAndGet(n.toLong())
        }
    }
}

class LanApi(private val node: Node, private val phone: PhoneApi) {
    fun handle(req: HttpRequest): HttpResponse {
        val path = req.path
        if (isLanRoute(path)) {
            if (req.method != "GET" && req.method != "HEAD") {
                return jsonError(405, "method_not_allowed", "Only GET and HEAD are allowed here.")
            }
            if (!node.online) return jsonError(503, "offline", "This peer has stopped sharing.")
            var proof: String? = null
            if (node.auth.enabled) {
                val token = req.header(TOKEN_HEADER)
                try {
                    node.auth.verifyRequest(req.method, path, token)
                } catch (e: AuthError) {
                    return jsonError(401, "auth_failed", e.message ?: "authentication failed", reason = e.reason)
                }
                proof = node.auth.proofFor(token!!)
            }
            val response = route(req)
            if (proof != null) response.header(PROOF_HEADER, proof)
            return response
        }
        if (path == "/api/introduce") return introduce(req)
        if (isPhoneRoute(path)) return phone.handle(req)
        if (path == "/" && (req.method == "GET" || req.method == "HEAD")) {
            // Someone typed this phone's address into a browser: the phone page tells them how to connect.
            return if (node.phonePage != null) HttpResponse(307).header("Location", "/phone") else homePage()
        }
        return jsonError(404, "not_found", "Not Found")
    }

    private fun route(req: HttpRequest): HttpResponse {
        val path = req.path
        if (path == "/api/files") return files(req)
        if (path == "/api/games") return games(req)
        val parts = path.removePrefix("/api/").split("/")
        return when {
            parts.size == 2 && parts[0] == "download" -> serveDownload(node, parts[1], req)
            parts.size == 2 && parts[0] == "pieces" -> pieceList(parts[1])
            parts.size == 2 && parts[0] == "swarm" -> swarmStatus(parts[1])
            parts.size == 3 && parts[0] == "piece" -> servePiece(parts[1], parts[2])
            else -> jsonError(404, "not_found", "Not Found")
        }
    }

    /**
     * A peer that joined this phone's hotspot (Join.kt) says who it is and which port it listens
     * on: this phone polls it back, at the address the request came from (and no other).
     */
    private fun introduce(req: HttpRequest): HttpResponse {
        if (req.method != "POST") return jsonError(405, "method_not_allowed", "Only POST is allowed here.")
        if (!node.online) return jsonError(503, "offline", "This peer has stopped sharing.")
        var proof: String? = null
        var caller: String? = null
        if (node.auth.enabled) {
            val token = req.header(TOKEN_HEADER)
            try {
                caller = node.auth.verifyRequest(req.method, req.path, token)
            } catch (e: AuthError) {
                return jsonError(401, "auth_failed", e.message ?: "authentication failed", reason = e.reason)
            }
            proof = node.auth.proofFor(token!!)
        }
        val body = req.readBody(4096)?.let { runCatching { Json.parseToJsonElement(it.toString(Charsets.UTF_8)) }.getOrNull() } as? JsonObject
        val peerId = (body?.get("peer_id") as? JsonPrimitive)?.takeIf { it.isString }?.content
        val port = (body?.get("port") as? JsonPrimitive)?.takeIf { !it.isString }?.intOrNull
        if (body == null || peerId == null || !isPeerId(peerId) || port == null || port !in 1..65535) {
            return jsonError(400, "bad_request", "Expected the peer's ID and port.")
        }
        if (caller != null && caller != peerId) return jsonError(403, "forbidden", "That is another peer's ID.")
        if (peerId == node.peerId) return jsonError(400, "bad_request", "That is this peer's own ID.")
        val address = req.remoteAddress.removePrefix("::ffff:")
        if (!isIpv4(address)) return jsonError(400, "bad_request", "Only IPv4 addresses for now.")
        val rawName = (body["name"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        val name = rawName?.let { displayName(it).trim().take(64) }?.ifEmpty { null } ?: peerId.take(8)
        if (!node.peers.meet(peerId, name, address, port)) return jsonError(429, "too_many", "Too many peers met this way already.")
        val response = json(200, buildJsonObject {
            put("peer_id", node.peerId)
            put("name", node.name)
        }.toString())
        if (proof != null) response.header(PROOF_HEADER, proof)
        return response
    }

    private fun json(status: Int, body: String): HttpResponse =
        HttpResponse(status, BytesBody(body.toByteArray(Charsets.UTF_8))).header("Content-Type", "application/json")

    private fun files(req: HttpRequest): HttpResponse {
        val (body, etag) = node.filesPayload()
        if (etagMatches(req.header("if-none-match"), etag)) {
            return HttpResponse(304).header("ETag", etag).header("Cache-Control", "no-cache")
        }
        return HttpResponse(200, BytesBody(body)).header("Content-Type", "application/json")
            .header("ETag", etag).header("Cache-Control", "no-cache")
    }

    /** This peer's half of its head-to-head games, read by its opponents (Play.kt). */
    private fun games(req: HttpRequest): HttpResponse {
        val (body, etag) = node.arcade.published()
        if (etagMatches(req.header("if-none-match"), etag)) {
            return HttpResponse(304).header("ETag", etag).header("Cache-Control", "no-cache")
        }
        return HttpResponse(200, BytesBody(body)).header("Content-Type", "application/json")
            .header("ETag", etag).header("Cache-Control", "no-cache")
    }

    private fun pieceList(fileId: String): HttpResponse {
        val info = node.pieceInfo(fileId) ?: return jsonError(404, "not_found", "No piece list for that file.")
        return json(200, info.toJson().toString()).header("Cache-Control", "no-store")
    }

    private fun swarmStatus(fileId: String): HttpResponse {
        val status = node.downloads.swarmStatus(fileId) ?: return jsonError(404, "not_found", "Not downloading that file.")
        return json(200, status.toString()).header("Cache-Control", "no-store")
    }

    /** One verified piece of a file this peer is downloading (or downloaded), for the swarm. */
    private fun servePiece(fileId: String, indexText: String): HttpResponse {
        val index = indexText.toIntOrNull()
        if (!SHA256_RE.matches(fileId) || index == null || index < 0) return jsonError(404, "not_found", "No such piece.")
        val located = node.downloads.locatePiece(fileId, index) ?: return jsonError(404, "not_found", "This peer doesn't have that piece.")
        return HttpResponse(200, FileBody(located.file, located.start, located.length.toLong(), node.uploads)) { located.close() }
            .header("Content-Type", "application/octet-stream")
            .header("Cache-Control", "no-store")
    }

    private fun homePage(): HttpResponse {
        val name = htmlEscape(node.name)
        val html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
            |<meta name="viewport" content="width=device-width, initial-scale=1"><title>LightningLAN</title>
            |<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5;color:#1b1f27}</style>
            |</head><body><h1>&#9889; LightningLAN</h1>
            |<p>This is <b>$name</b>, a phone running LightningLAN.</p>
            |<p>The files it shares show up in ThunderCLOUD in LightningLAN on your computer.</p></body></html>""".trimMargin()
        return HttpResponse(200, BytesBody(html.toByteArray(Charsets.UTF_8)))
            .header("Content-Type", "text/html; charset=utf-8").header("Cache-Control", "no-cache")
    }
}

/** A file this peer shares, with HTTP Range (for other peers, and for phone pages). */
internal fun serveDownload(node: Node, fileId: String, req: HttpRequest): HttpResponse {
    if (!SHA256_RE.matches(fileId)) return jsonError(404, "not_found", "No such file.")
    val entry = node.indexer.lookup(fileId) ?: return jsonError(404, "not_found", "This peer does not share that file (any more).")
    val file = node.indexer.open(entry)
    if (file == null) {
        node.indexer.requestRescan()
        return jsonError(404, "not_found", "This peer does not share that file (any more).")
    }
    if (file.size != entry.size || file.mtimeMs / 1000 != entry.mtimeMs / 1000) {
        file.close()
        node.indexer.requestRescan()
        return jsonError(409, "changed", "The file changed since it was indexed; try again shortly.")
    }
    val size = entry.size
    val etag = "\"${entry.sha256}\""
    var start = 0L
    var end = size - 1
    var status = 200
    val range = req.header("range")
    val ifRange = req.header("if-range")
    if (range != null && (ifRange == null || ifRange.trim() == etag)) {
        when (val parsed = parseRange(range, size)) {
            RangeResult.Unsatisfiable -> {
                file.close()
                return jsonError(416, "range_not_satisfiable", "Requested range is past the end of the file.")
                    .header("Content-Range", "bytes */$size")
            }
            is RangeResult.Bytes -> {
                start = parsed.start
                end = parsed.end
                status = 206
            }
            null -> {}
        }
    }
    val length = maxOf(0L, end - start + 1)
    val response = HttpResponse(status, FileBody(file, start, length, node.uploads)) { file.close() }
        .header("Content-Type", "application/octet-stream")
        .header("Accept-Ranges", "bytes")
        .header("ETag", etag)
        .header("Content-Disposition", "attachment; filename*=UTF-8''${quoteFilename(entry.relpath.substringAfterLast('/'))}")
        .header("Cache-Control", "no-store")
    if (status == 206) response.header("Content-Range", "bytes $start-$end/$size")
    return response
}

fun etagMatches(ifNoneMatch: String?, etag: String): Boolean {
    if (ifNoneMatch.isNullOrEmpty()) return false
    val candidates = ifNoneMatch.split(",").map { it.trim().removePrefix("W/") }
    return etag in candidates || "*" in candidates
}

fun htmlEscape(text: String): String =
    text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\"", "&quot;").replace("'", "&#39;")
