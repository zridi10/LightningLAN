package lightninglan.core

import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel
import com.google.zxing.qrcode.encoder.Encoder
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject
import okhttp3.Request
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap

/*
 * The phone page, served by this phone (lightninglan/server.py's /phone routes, with phone.py and
 * security.py): another phone on the same Wi-Fi, without the app, scans the QR code in the app
 * and uses LightningLAN in its web browser through this phone. It lists ThunderCLOUD, downloads
 * any file in it (this phone's own, or relayed from the peer that has it, with the SHA-256 checked
 * on the way, so a damaged file never finishes), sends files into the shared folder and plays the
 * games as this phone. The page is the desktop app's own (lightninglan/static), packed into the app.
 *
 *   GET  /phone?key=<key>                    pairs: keeps the key in a cookie, then shows the page
 *   GET  /phone                              the page (it tells a phone that isn't paired how to pair)
 *   GET  /static/<file>                      the page's scripts, styles and pictures
 *   GET  /phone/api/status                   this phone's name, and whether it shares
 *   GET  /phone/api/session                  an event stream, held open while the page shows
 *   GET  /phone/api/files                    ThunderCLOUD (ETag / If-None-Match)
 *   GET  /phone/api/file/<id>                a file, with HTTP Range
 *   POST /phone/api/upload?name=&mtime=      a file for the shared folder
 *   GET  /phone/api/play                     the games (play.js), and POST .../invite, .../<id>/<action>
 *
 * Everything under /phone/api/ needs the cookie. Requests that change something must carry
 * X-LightningLAN-Client and, if the browser sent an Origin, the page's own (blocking cross-site
 * request forgery).
 */

private const val TAG = "phone"
const val PHONE_COOKIE = "lightninglan_phone"
private const val COOKIE_MAX_AGE = 400L * 24 * 3600 // browsers keep a cookie for at most 400 days
private const val CLIENT_HEADER = "x-lightninglan-client"
private const val SESSION_TICK_MS = 500L
private const val SESSION_PING_MS = 15_000L
private const val RELAY_CHUNK = 256 * 1024
private const val CONTENT_SECURITY_POLICY = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; " +
    "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
private val SECURITY_HEADERS = listOf(
    "X-Content-Type-Options" to "nosniff",
    "Referrer-Policy" to "no-referrer",
    "X-Frame-Options" to "DENY",
    "Cross-Origin-Resource-Policy" to "same-origin",
    "Content-Security-Policy" to CONTENT_SECURITY_POLICY,
)
private val STATIC_PATH = Regex("^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*\\.(html|js|css|svg)$")
private val CONTENT_TYPES = mapOf(
    "html" to "text/html; charset=utf-8", "js" to "text/javascript; charset=utf-8",
    "css" to "text/css; charset=utf-8", "svg" to "image/svg+xml",
)

fun isPhoneRoute(path: String): Boolean = path == "/phone" || path.startsWith("/phone/") || path.startsWith("/static/")

/** The phone page's files, from lightninglan/static, by their path there ("phone.html", "themes/bliss-sun.svg"). */
fun interface PhonePageFiles {
    fun read(path: String): ByteArray?
}

/** The phone page's link at one of this phone's addresses, and its QR code. */
class PhoneLink(val address: String, val url: String, val qr: List<String>)

fun phoneLinks(addresses: List<String>, port: Int, key: String): List<PhoneLink> = addresses.map { address ->
    val url = "http://$address:$port/phone?key=$key"
    PhoneLink(address, url, qrRows(url))
}

/**
 * What a "join this Wi-Fi" QR code holds (the format phone cameras read: WIFI:T:WPA;S:name;P:password;;),
 * for the app's own hotspot. No password means an open network.
 */
fun wifiQrText(ssid: String, password: String?): String {
    fun escape(text: String) = text.replace(Regex("""([\\;,":])"""), """\\$1""")
    return if (password.isNullOrEmpty()) "WIFI:T:nopass;S:${escape(ssid)};;"
    else "WIFI:T:WPA;S:${escape(ssid)};P:${escape(password)};;"
}

/** A QR code as rows of '1' (dark) and '0' (light), without the quiet zone around it (as in phone.py). */
fun qrRows(text: String): List<String> {
    val matrix = Encoder.encode(text, ErrorCorrectionLevel.M).matrix
    return (0 until matrix.height).map { y ->
        buildString { for (x in 0 until matrix.width) append(if (matrix.get(x, y).toInt() == 1) '1' else '0') }
    }
}

class PhoneApi(private val node: Node) {
    private val etags = ConcurrentHashMap<String, String>()

    fun handle(req: HttpRequest): HttpResponse {
        val response = try {
            route(req)
        } catch (e: PlayError) {
            jsonError(e.status, "play", e.message ?: "That didn't work.")
        }
        for ((name, value) in SECURITY_HEADERS) response.header(name, value)
        if (req.path == "/phone" || req.path.startsWith("/static/")) { // the page's own files are never cached stale
            response.headers.removeAll { it.first.equals("Cache-Control", ignoreCase = true) }
            response.header("Cache-Control", "no-cache")
        }
        return response
    }

    private fun route(req: HttpRequest): HttpResponse {
        val page = node.phonePage ?: return jsonError(404, "not_found", "Not Found")
        val path = req.path
        val method = req.method
        val reading = method == "GET" || method == "HEAD"
        if (path.startsWith("/static/")) {
            if (!reading) return notAllowed()
            return staticFile(page, path.removePrefix("/static/"), req)
        }
        if (path == "/phone") return if (reading) pageOrPairing(page, req) else notAllowed()
        if (!reading && method != "POST") return notAllowed()
        if (!node.phoneKeyMatches(cookie(req, PHONE_COOKIE))) {
            return jsonError(401, "not_paired", "Scan the QR code in LightningLAN to connect this phone.")
        }
        if (method == "POST") {
            val origin = req.header("origin")
            if (origin != null && !sameOrigin(origin, req.header("host"))) {
                return jsonError(403, "bad_origin", "Cross-site requests are not allowed.")
            }
            if (req.header(CLIENT_HEADER) == null) return jsonError(403, "missing_client_header", "Missing X-LightningLAN-Client header.")
        }
        val parts = path.removePrefix("/phone/api/").takeIf { path.startsWith("/phone/api/") }?.split("/")
            ?: return jsonError(404, "not_found", "Not Found")
        return when {
            parts == listOf("status") && reading -> json(200, status())
            parts == listOf("session") && reading -> session(req)
            parts == listOf("files") && reading -> files(req)
            parts.size == 2 && parts[0] == "file" && reading -> download(parts[1], req)
            parts == listOf("upload") && method == "POST" -> upload(req)
            parts == listOf("play") && reading -> playView(req)
            parts == listOf("play", "invite") && method == "POST" -> invite(req)
            parts.size == 3 && parts[0] == "play" && method == "POST" -> playAction(parts[1], parts[2], req)
            parts.firstOrNull() in setOf("status", "session", "files", "file", "upload", "play") -> notAllowed()
            else -> jsonError(404, "not_found", "Not Found")
        }
    }

    private fun notAllowed() = jsonError(405, "method_not_allowed", "Method not allowed.")

    private fun json(status: Int, body: JsonElement): HttpResponse =
        HttpResponse(status, BytesBody(body.toString().toByteArray(Charsets.UTF_8))).header("Content-Type", "application/json")

    /** A body with an ETag, or 304 when the page already has it. */
    private fun tagged(req: HttpRequest, body: ByteArray, etag: String, contentType: String): HttpResponse {
        if (etagMatches(req.header("if-none-match"), etag)) {
            return HttpResponse(304).header("ETag", etag).header("Cache-Control", "no-cache")
        }
        return HttpResponse(200, BytesBody(body)).header("Content-Type", contentType)
            .header("ETag", etag).header("Cache-Control", "no-cache")
    }

    // ----- the page and its files ---------------------------------------------------------------------

    private fun staticFile(page: PhonePageFiles, file: String, req: HttpRequest): HttpResponse {
        if (!STATIC_PATH.matches(file)) return jsonError(404, "not_found", "Not Found")
        val bytes = page.read(file) ?: return jsonError(404, "not_found", "Not Found")
        val etag = etags.getOrPut(file) { "\"" + sha256Hex(bytes).take(32) + "\"" }
        return tagged(req, bytes, etag, CONTENT_TYPES.getValue(file.substringAfterLast('.')))
    }

    private fun pageOrPairing(page: PhonePageFiles, req: HttpRequest): HttpResponse {
        val key = req.param("key") ?: return staticFile(page, "phone.html", req)
        // Opened from the QR code: keep the key in a cookie, and take it out of the address bar.
        if (!node.phoneKeyMatches(key)) return redirect("/phone#expired")
        CoreLog.i(TAG, "a phone was paired from ${req.remoteAddress}")
        return redirect("/phone").header("Set-Cookie", "$PHONE_COOKIE=$key; Max-Age=$COOKIE_MAX_AGE; Path=/; HttpOnly; SameSite=Lax")
    }

    private fun redirect(location: String) = HttpResponse(303).header("Location", location)

    // ----- status, the session and ThunderCLOUD ------------------------------------------------------

    private fun status(): JsonObject = buildJsonObject {
        put("name", node.name)
        put("version", node.version)
        put("online", node.online)
        put("offline_reason", node.offlineReason)
        put("peers_online", node.peers.online().size)
    }

    /**
     * Server-sent events that say whether this phone shares, held open by the page while it shows
     * (that's how the app counts the phones connected). It ends when "Disconnect phones" replaces the key.
     */
    private fun session(req: HttpRequest): HttpResponse {
        val key = cookie(req, PHONE_COOKIE)
        val stream = object : HttpBody() {
            override val length: Long = -1

            override fun writeTo(out: OutputStream) {
                node.phoneOpened()
                try {
                    var seen: Pair<Int, Int>? = null
                    var quiet = 0L
                    while (!node.closed && node.phoneKeyMatches(key)) {
                        val now = node.stateVersion to node.phonesVersion
                        if (now != seen) {
                            seen = now
                            quiet = 0
                            out.write("data: {\"online\":${node.online}}\n\n".toByteArray(Charsets.UTF_8))
                            out.flush()
                        } else if (quiet >= SESSION_PING_MS) {
                            quiet = 0
                            out.write(": ping\n\n".toByteArray(Charsets.UTF_8))
                            out.flush()
                        }
                        Thread.sleep(SESSION_TICK_MS)
                        quiet += SESSION_TICK_MS
                    }
                } catch (e: InterruptedException) {
                    // the server is stopping
                } finally {
                    node.phoneClosed()
                }
            }
        }
        return HttpResponse(200, stream).header("Content-Type", "text/event-stream").header("Cache-Control", "no-store")
    }

    private fun files(req: HttpRequest): HttpResponse {
        val (body, etag) = node.commonPayload()
        return tagged(req, body, etag, "application/json")
    }

    // ----- downloads to the phone -----------------------------------------------------------------------

    private fun download(fileId: String, req: HttpRequest): HttpResponse {
        if (!node.online) return jsonError(409, "offline", "LightningLAN is stopped on ${node.name}. Start it there first.")
        if (!SHA256_RE.matches(fileId)) return jsonError(404, "not_found", "No such file.")
        if (node.indexer.lookup(fileId) != null) return serveDownload(node, fileId, req)
        return relay(fileId, req)
    }

    /** Streams a file that only other peers have, from the first one that sends it. */
    private fun relay(fileId: String, req: HttpRequest): HttpResponse {
        val sources = node.downloads.sources(fileId)
        if (sources.isEmpty()) return jsonError(404, "not_found", "No online peer has this file any more.")
        val path = "/api/download/$fileId"
        var problem = "No peer could send the file."
        for ((peer, remote) in sources) {
            val signed = node.auth.signRequest(req.method, path)
            val builder = Request.Builder().url(peer.baseUrl() + path).method(req.method, null).header("Accept-Encoding", "identity")
            for ((name, value) in signed) builder.header(name, value)
            req.header("range")?.let { builder.header("Range", it) }
            req.header("if-range")?.let { builder.header("If-Range", it) }
            val upstream = try {
                node.relayHttp.newCall(builder.build()).execute()
            } catch (e: IOException) {
                problem = "Could not reach ${peer.name}: ${describeNetworkError(e)}."
                continue
            }
            if (!node.auth.verifyResponse(signed[TOKEN_HEADER], upstream.header(PROOF_HEADER)) || upstream.code !in setOf(200, 206, 416)) {
                problem = "${peer.name} could not send the file (HTTP ${upstream.code})."
                upstream.close()
                continue
            }
            val upstreamLength = upstream.header("Content-Length")?.toLongOrNull() ?: -1
            if (upstream.code == 416 || req.method == "HEAD") {
                upstream.close()
                val body = if (upstream.code == 416) null else object : HttpBody() { // only its length is sent
                    override val length: Long = maxOf(upstreamLength, 0)
                    override fun writeTo(out: OutputStream) = Unit
                }
                return passOn(HttpResponse(upstream.code, body), upstream)
            }
            val check = if (upstream.code == 200) fileId else null // a whole file can be checked before it ends
            val body = RelayBody(upstream.body!!.byteStream(), upstreamLength, check, node.uploads, peer.name)
            val fileName = displayName(remote.name).substringAfterLast('/')
            return passOn(HttpResponse(upstream.code, body) { upstream.close() }, upstream)
                .header("Content-Type", "application/octet-stream")
                .header("Content-Disposition", "attachment; filename*=UTF-8''${quoteFilename(fileName)}")
                .header("Cache-Control", "no-store")
        }
        return jsonError(502, "unavailable", problem)
    }

    private fun passOn(response: HttpResponse, upstream: okhttp3.Response): HttpResponse {
        for (name in listOf("Content-Range", "Accept-Ranges", "ETag")) upstream.header(name)?.let { response.header(name, it) }
        return response
    }

    // ----- files sent from the phone ----------------------------------------------------------------------

    private fun upload(req: HttpRequest): HttpResponse {
        val inbox = node.shareInbox() ?: return jsonError(409, "no_folder", "${node.name} has no shared folder to put files in.")
        val parts = (req.param("name") ?: "").replace('\\', '/').split('/').filter { it.isNotEmpty() }
        if (parts.isEmpty() || parts.any { it == "." || it == ".." }) return jsonError(400, "bad_name", "Invalid file name.")
        if (parts.any(::isIgnoredName)) {
            return jsonError(400, "bad_name", "Hidden and temporary files (names starting with '.' or ending in .tmp/.part) are not shared.")
        }
        if (req.header("content-length") == null) return jsonError(400, "no_length", "The upload needs a Content-Length.")
        val length = req.contentLength
        inbox.inboxFreeSpace()?.let { free ->
            if (length > free) return jsonError(507, "no_space", "Not enough space: ${formatSize(length)} needed, ${formatSize(free)} free.")
        }
        val fileName = safeLocalFilename(parts.last(), fallback = "file") // phones send no folders: it goes at the top
        val mtimeMs = req.param("mtime")?.toDoubleOrNull()?.toLong()?.takeIf { it in 1 until 32_503_680_000_000 } // before the year 3000
        val saved = try {
            inbox.receive(fileName, length, mtimeMs, req.body)
        } catch (e: IOException) {
            CoreLog.w(TAG, "a file from a phone could not be saved: $e")
            return jsonError(500, "not_saved", "Could not save the file: ${e.message}.")
        }
        node.indexer.requestRescan()
        CoreLog.i(TAG, "a phone added ${saved.name} to the shared folder")
        return json(201, buildJsonObject {
            put("name", saved.name)
            put("size", saved.size)
            put("renamed", saved.renamed || saved.name != parts.last())
        })
    }

    // ----- While you wait: the page plays as this phone ---------------------------------------------------

    private fun requireOnline() {
        if (!node.online) throw PlayError("LightningLAN is stopped on ${node.name}. Start it there first.")
    }

    private fun body(req: HttpRequest): JsonObject {
        val bytes = req.readBody() ?: throw PlayError("That request is too big.", 413)
        if (bytes.isEmpty()) return JsonObject(emptyMap())
        return try {
            Json.parseToJsonElement(bytes.toString(Charsets.UTF_8)).jsonObject
        } catch (e: Exception) {
            throw PlayError("That request isn't valid JSON.", 400)
        }
    }

    private fun playView(req: HttpRequest): HttpResponse {
        val body = playJson(node.arcade.view()).toString().toByteArray(Charsets.UTF_8)
        return tagged(req, body, "\"" + sha256Hex(body).take(32) + "\"", "application/json")
    }

    private fun invite(req: HttpRequest): HttpResponse {
        requireOnline()
        val b = body(req)
        val peerId = b.text("peer_id") ?: throw PlayError("Who to play with?", 400)
        val kind = b.text("kind") ?: throw PlayError("Unknown game.", 400)
        val bestOf = b["best_of"]?.jsonPrimitive?.intOrNull ?: 3
        val first = b["first"]?.jsonPrimitive?.booleanOrNull
        return json(200, gameJson(node.arcade.invite(peerId, kind, bestOf, first)))
    }

    private fun playAction(gameId: String, action: String, req: HttpRequest): HttpResponse {
        val arcade = node.arcade
        if (action in setOf("accept", "rematch", "move")) requireOnline()
        val game: GameView? = when (action) {
            "accept" -> arcade.answer(gameId, true)
            "decline" -> arcade.answer(gameId, false)
            "withdraw" -> null.also { arcade.withdraw(gameId) }
            "resign" -> arcade.resign(gameId)
            "close" -> null.also { arcade.close(gameId) }
            "rematch" -> arcade.rematch(gameId)
            "move" -> move(gameId, body(req))
            else -> return jsonError(404, "not_found", "unknown action")
        }
        return json(200, buildJsonObject { put("game", game?.let(::gameJson) ?: JsonNull) })
    }

    /** One move; which field depends on the game (server.py's PlayMove). */
    private fun move(gameId: String, b: JsonObject): GameView {
        val arcade = node.arcade
        b.text("choice")?.let { return arcade.choose(gameId, it) }
        (b["path"] as? kotlinx.serialization.json.JsonArray)?.let { path ->
            return arcade.movePath(gameId, path.map { it.jsonPrimitive.intOrNull ?: throw PlayError("That's not a move.", 400) })
        }
        if (b["shuffle"]?.jsonPrimitive?.booleanOrNull == true) return arcade.shuffleFleet(gameId)
        if (b["ready"]?.jsonPrimitive?.booleanOrNull == true) return arcade.fleetReady(gameId)
        b.int("shot")?.let { return arcade.fire(gameId, it) }
        b.int("time_ms")?.let { return arcade.finish(gameId, it) }
        return arcade.move(gameId, b.int("cell") ?: -1)
    }

    private fun JsonObject.text(key: String): String? = (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull

    private fun JsonObject.int(key: String): Int? = (this[key] as? JsonPrimitive)?.intOrNull
}

/** Streams a relayed file to the phone; for a whole file, the last chunk waits until the SHA-256 matched. */
private class RelayBody(
    private val input: InputStream,
    override val length: Long,
    private val sha256: String?,
    private val limiter: UploadLimiter,
    private val source: String,
) : HttpBody() {
    override fun writeTo(out: OutputStream) {
        val generation = limiter.generation
        val digest = sha256?.let { MessageDigest.getInstance("SHA-256") }
        var held = ByteArray(RELAY_CHUNK)
        var next = ByteArray(RELAY_CHUNK)
        var heldLength = 0
        while (true) {
            if (!limiter.allows(generation)) throw ConnectionCut() // this phone stopped sharing
            val n = try {
                input.read(next)
            } catch (e: IOException) {
                CoreLog.i(TAG, "relaying a file from $source to a phone stopped: ${describeNetworkError(e)}")
                throw ConnectionCut() // ending early makes the phone's browser report the download as failed
            }
            if (n < 0) break
            if (n == 0) continue
            digest?.update(next, 0, n)
            if (heldLength > 0) out.write(held, 0, heldLength)
            val swap = held
            held = next
            next = swap
            heldLength = n
        }
        if (digest != null && digest.digest().toHex() != sha256) {
            CoreLog.w(TAG, "a file relayed from $source to a phone failed SHA-256 verification")
            throw ConnectionCut()
        }
        if (heldLength > 0) out.write(held, 0, heldLength)
    }
}

private fun cookie(req: HttpRequest, name: String): String? = req.header("cookie")?.split(';')?.firstNotNullOfOrNull { part ->
    val eq = part.indexOf('=')
    if (eq > 0 && part.substring(0, eq).trim() == name) part.substring(eq + 1).trim().removeSurrounding("\"") else null
}

/** True if the Origin a browser sent is the site it is talking to (http://<Host header>). */
private fun sameOrigin(origin: String, host: String?): Boolean = host != null && origin.equals("http://" + host.trim(), ignoreCase = true)

// ----- the games as play.js reads them (lightninglan/play.py's Arcade.view) ------------------------------------

internal fun playJson(view: PlayView): JsonObject = buildJsonObject {
    putJsonArray("games") { for (g in view.games) add(gameJson(g)) }
    putJsonArray("invites") {
        for (inv in view.invites) {
            addJsonObject {
                put("id", inv.id)
                put("kind", inv.kind)
                put("title", inv.title)
                put("best_of", inv.bestOf)
                putJsonObject("from") {
                    put("peer_id", inv.from.peerId)
                    put("name", inv.from.name)
                }
                put("you_go_first", inv.youGoFirst)
            }
        }
    }
    putJsonArray("opponents") {
        for (o in view.opponents) {
            addJsonObject {
                put("peer_id", o.peerId)
                put("name", o.name)
            }
        }
    }
    putJsonObject("titles") { for ((kind, title) in GAME_TITLES) put(kind, title) }
    putJsonArray("best_of") { for (n in BEST_OF) add(n) }
}

private fun ints(values: List<Int>) = buildJsonArray { for (v in values) add(v) }

private fun strings(values: List<String>) = buildJsonArray { for (v in values) add(v) }

private fun pair(p: Pair<Int, Int>) = buildJsonArray {
    add(p.first)
    add(p.second)
}

internal fun gameJson(g: GameView): JsonObject = buildJsonObject {
    put("id", g.id)
    put("kind", g.kind)
    put("title", g.title)
    put("best_of", g.bestOf)
    put("host", g.host)
    put("seat", g.seat)
    put("mark", g.mark)
    putJsonObject("opponent") {
        put("peer_id", g.opponent.peerId)
        put("name", g.opponent.name)
        put("online", g.opponent.online)
    }
    put("status", g.status)
    put("result", g.result)
    put("reason", g.reason)
    put("your_turn", g.yourTurn)
    put("created", g.created / 1000.0)
    when (g.kind) {
        RPS -> g.rps?.let { r ->
            putJsonObject("rps") {
                putJsonArray("rounds") {
                    for (round in r.rounds) {
                        addJsonObject {
                            put("mine", round.mine)
                            put("theirs", round.theirs)
                            put("result", round.result)
                        }
                    }
                }
                put("score", pair(r.score))
                put("target", r.target)
                put("locked", r.locked)
                put("choice", r.choice)
                put("they_locked", r.theyLocked)
            }
        }
        BATTLESHIP -> g.battle?.let { b ->
            putJsonObject("battle") {
                put("phase", b.phase)
                put("ready", b.ready)
                put("they_ready", b.theyReady)
                putJsonArray("ships") { for (ship in b.ships) add(ints(ship)) }
                put("ship_names", strings(SHIP_NAMES))
                for ((key, shots) in listOf("incoming" to b.incoming, "outgoing" to b.outgoing)) {
                    putJsonArray(key) {
                        for (s in shots) {
                            addJsonObject {
                                put("cell", s.cell)
                                put("result", s.result)
                            }
                        }
                    }
                }
                putJsonArray("their_ships") { for (ship in b.theirShips) add(ints(ship)) }
                put("sunk", pair(b.sunk)) // ships I sank, ships I lost
                put("fleet", FLEET.size)
            }
        }
        TYPING -> g.race?.let { r ->
            putJsonObject("race") {
                put("sentence", r.sentence)
                put("my_time", r.myTime)
                put("they_done", r.theyDone)
                put("their_time", r.theirTime)
            }
        }
        MATH -> g.sprint?.let { s ->
            putJsonObject("sprint") {
                putJsonArray("problems") {
                    for ((question, answer) in s.problems) {
                        addJsonObject {
                            put("question", question)
                            put("answer", answer)
                        }
                    }
                }
                put("my_times", ints(s.myTimes))
                put("their_count", s.theirCount)
                put("their_times", ints(s.theirTimes)) // only the sums you've done too
                put("score", pair(s.score))
            }
        }
        else -> {
            if (g.kind == CHECKERS) putJsonArray("allowed") { for (p in g.paths) add(ints(p)) } else put("allowed", ints(g.allowed))
            when (g.kind) {
                TICTACTOE, ULTIMATE, CONNECT4 -> {
                    put("board", strings(g.board))
                    put("line", ints(g.line))
                    put("last", g.last)
                    if (g.kind == ULTIMATE) {
                        put("small", strings(g.small))
                        put("forced", g.forced)
                    }
                }
                CHECKERS -> {
                    put("board", strings(g.board))
                    put("last", ints(g.lastPath))
                }
                DOTS -> {
                    put("lines", strings(g.lines))
                    put("boxes", strings(g.boxes))
                    put("last", g.last)
                }
                MEMORY -> {
                    putJsonArray("cards") { for (c in g.cards) add(c) }
                    put("matched", strings(g.matched))
                    put("pending", g.pending)
                    put("shown", ints(g.shown))
                    put("last", g.last)
                }
            }
            g.score?.let { put("score", pair(it)) }
        }
    }
}
