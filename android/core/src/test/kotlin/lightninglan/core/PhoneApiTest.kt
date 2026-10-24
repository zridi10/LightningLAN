package lightninglan.core

import com.google.zxing.BinaryBitmap
import com.google.zxing.RGBLuminanceSource
import com.google.zxing.common.HybridBinarizer
import com.google.zxing.qrcode.QRCodeReader
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.io.TempDir
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

private val staticDir = File(System.getProperty("lightninglan.repo") ?: "../..", "lightninglan/static").absoluteFile

/** The desktop app's phone page files, as the app packs them. */
private val repoPage = PhonePageFiles { path -> File(staticDir, path).takeIf { it.isFile }?.readBytes() }

/** The phone page served by the phone (PhoneApi.kt), called the way a phone's browser calls it. */
class PhoneApiTest {
    @TempDir
    lateinit var tmp: File
    private lateinit var share: File
    private lateinit var node: Node
    private val extra = ArrayList<Node>()
    private val client = OkHttpClient.Builder().followRedirects(false).retryOnConnectionFailure(false)
        .readTimeout(20, TimeUnit.SECONDS).build()
    private val small = "hello from the phone".toByteArray()
    private val savedKeys = ArrayList<String>()
    private lateinit var cookie: String

    private fun makeNode(name: String, dir: File, peerId: String, page: PhonePageFiles?): Node {
        val config = NodeConfig(
            peerId, name, "1.3.0", File(dir, "state"), port = 0, refreshMs = 300, indexIntervalMs = 60_000, phonePage = page,
        )
        val shareDir = File(dir, "share").apply { mkdirs() }
        return Node(config, PathShareTree(shareDir), PathDownloadFolder(File(dir, "downloads").apply { mkdirs() }), null)
    }

    private fun waitFor(what: String, timeoutMs: Long = 15_000, check: () -> Boolean) {
        val deadline = monotonicMs() + timeoutMs
        while (!check()) {
            if (monotonicMs() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(50)
        }
    }

    private fun indexed(n: Node, files: Int) = waitFor("the index") { n.indexer.snapshot.let { it.entries.size >= files && it.pending == 0 } }

    @BeforeEach
    fun setUp() {
        share = File(tmp, "phone/share").apply { mkdirs() }
        File(share, "small.txt").writeBytes(small)
        node = makeNode("Test Phone", File(tmp, "phone"), "1".repeat(32), repoPage)
        node.onPhoneKey = { savedKeys += it }
        node.start()
        indexed(node, 1)
        cookie = "$PHONE_COOKIE=${node.ensurePhoneKey()}"
    }

    @AfterEach
    fun tearDown() {
        for (n in extra) n.close()
        node.close()
    }

    private fun url(path: String, n: Node = node) = "http://127.0.0.1:${n.port}$path"

    private fun call(builder: Request.Builder, vararg headers: Pair<String, String>): Response {
        builder.header("Accept-Encoding", "identity")
        for ((k, v) in headers) builder.header(k, v)
        return client.newCall(builder.build()).execute()
    }

    private fun get(path: String, vararg headers: Pair<String, String>, paired: Boolean = true): Response =
        call(Request.Builder().url(url(path)), *(if (paired) arrayOf("Cookie" to cookie) else emptyArray()), *headers)

    private fun post(path: String, body: ByteArray, vararg headers: Pair<String, String>, client: Boolean = true): Response {
        val all = listOf("Cookie" to cookie) + (if (client) listOf("X-LightningLAN-Client" to "phone") else emptyList()) + headers
        return call(Request.Builder().url(url(path)).post(body.toRequestBody("application/octet-stream".toMediaType())), *all.toTypedArray())
    }

    private fun postJson(path: String, json: String) = post(path, json.toByteArray(), "Content-Type" to "application/json")

    private fun json(resp: Response): JsonObject = Json.parseToJsonElement(resp.body!!.string()).jsonObject

    private fun sha(data: ByteArray) = MessageDigest.getInstance("SHA-256").digest(data).toHex()

    @Test
    fun `scanning the QR code pairs the phone, and only the right key does`() {
        get("/phone/api/status", paired = false).use {
            assertEquals(401, it.code)
            assertEquals("not_paired", json(it)["error"]!!.jsonPrimitive.content)
        }
        get("/phone?key=wrong", paired = false).use {
            assertEquals(303, it.code)
            assertEquals("/phone#expired", it.header("Location"))
            assertNull(it.header("Set-Cookie"))
        }
        val key = node.ensurePhoneKey()
        assertEquals(listOf(key), savedKeys) // made once and kept
        get("/phone?key=$key", paired = false).use {
            assertEquals(303, it.code)
            assertEquals("/phone", it.header("Location"))
            val set = it.header("Set-Cookie")!!
            assertTrue(set.startsWith("$PHONE_COOKIE=$key;") && "HttpOnly" in set && "SameSite=Lax" in set, set)
        }
        get("/phone/api/status").use {
            assertEquals(200, it.code)
            val s = json(it)
            assertEquals("Test Phone", s["name"]!!.jsonPrimitive.content)
            assertEquals(true, s["online"]!!.jsonPrimitive.boolean)
        }
    }

    @Test
    fun `the page and its files are served with the desktop app's security headers`() {
        get("/", paired = false).use {
            assertEquals(307, it.code)
            assertEquals("/phone", it.header("Location"))
        }
        get("/phone", paired = false).use {
            assertEquals(200, it.code)
            assertEquals("text/html; charset=utf-8", it.header("Content-Type"))
            assertTrue("/static/phone.js" in it.body!!.string())
            assertEquals("no-cache", it.header("Cache-Control"))
            assertTrue("frame-ancestors 'none'" in it.header("Content-Security-Policy")!!)
            assertEquals("nosniff", it.header("X-Content-Type-Options"))
        }
        val etag = get("/static/phone.js", paired = false).use {
            assertEquals(200, it.code)
            assertEquals("text/javascript; charset=utf-8", it.header("Content-Type"))
            assertContentEquals(File(staticDir, "phone.js").readBytes(), it.body!!.bytes())
            it.header("ETag")!!
        }
        get("/static/phone.js", "If-None-Match" to etag, paired = false).use { assertEquals(304, it.code) }
        get("/static/themes/synthwave-sun.svg", paired = false).use {
            assertEquals(200, it.code)
            assertEquals("image/svg+xml", it.header("Content-Type"))
        }
        for (path in listOf("/static/../server.py", "/static/index.html.bak", "/static/missing.js", "/phone/api/nothing")) {
            get(path).use { assertEquals(404, it.code, path) }
        }
    }

    @Test
    fun `ThunderCLOUD and this phone's files reach the page`() {
        val (body, etag) = get("/phone/api/files").use { assertEquals(200, it.code); json(it) to it.header("ETag")!! }
        assertEquals("Test Phone", body["self"]!!.jsonObject["name"]!!.jsonPrimitive.content)
        val file = body["files"]!!.jsonArray.single().jsonObject
        assertEquals("small.txt", file["name"]!!.jsonPrimitive.content)
        assertEquals(true, file["local"]!!.jsonPrimitive.boolean)
        assertEquals(true, file["sources"]!!.jsonArray.single().jsonObject["self"]!!.jsonPrimitive.boolean)
        get("/phone/api/files", "If-None-Match" to etag).use { assertEquals(304, it.code) }

        get("/phone/api/file/${sha(small)}").use {
            assertEquals(200, it.code)
            assertEquals("attachment; filename*=UTF-8''small.txt", it.header("Content-Disposition"))
            assertContentEquals(small, it.body!!.bytes())
        }
        get("/phone/api/file/${sha(small)}", "Range" to "bytes=6-9").use {
            assertEquals(206, it.code)
            assertEquals("from", it.body!!.string())
        }
        node.goOffline()
        get("/phone/api/file/${sha(small)}").use { assertEquals(409, it.code) }
    }

    @Test
    fun `files sent from the page land in the shared folder under a free name`() {
        val photo = ByteArray(300_000) { (it % 251).toByte() }
        post("/phone/api/upload?name=${java.net.URLEncoder.encode("holiday photo.jpg", "UTF-8")}&mtime=1700000000000", photo).use {
            assertEquals(201, it.code)
            val r = json(it)
            assertEquals("holiday photo.jpg", r["name"]!!.jsonPrimitive.content)
            assertEquals(false, r["renamed"]!!.jsonPrimitive.boolean)
        }
        assertContentEquals(photo, File(share, "holiday photo.jpg").readBytes())
        assertEquals(1_700_000_000_000, File(share, "holiday photo.jpg").lastModified())
        post("/phone/api/upload?name=small.txt", "another".toByteArray()).use {
            assertEquals(201, it.code)
            val r = json(it)
            assertEquals("small (1).txt", r["name"]!!.jsonPrimitive.content)
            assertEquals(true, r["renamed"]!!.jsonPrimitive.boolean)
        }
        assertContentEquals(small, File(share, "small.txt").readBytes()) // never replaced
        post("/phone/api/upload?name=.secret", "x".toByteArray()).use { assertEquals(400, it.code) }
        post("/phone/api/upload?name=a.txt", "x".toByteArray(), client = false).use { assertEquals(403, it.code) }
        post("/phone/api/upload?name=a.txt", "x".toByteArray(), "Origin" to "http://evil.example").use { assertEquals(403, it.code) }
        post("/phone/api/upload?name=a.txt", "x".toByteArray(), "Origin" to "http://127.0.0.1:${node.port}").use { assertEquals(201, it.code) }
        assertTrue(share.list()!!.none { it.startsWith(UPLOAD_TEMP_PREFIX) })
        // The same connection carries on after a refused upload.
        get("/phone/api/status").use { assertEquals(200, it.code) }
    }

    @Test
    fun `an open page counts as a connected phone until the phones are disconnected`() {
        assertEquals(0, node.status().phones)
        get("/phone/api/session").use { resp ->
            assertEquals(200, resp.code)
            assertEquals("text/event-stream", resp.header("Content-Type"))
            val source = resp.body!!.source()
            assertEquals("data: {\"online\":true}", source.readUtf8Line())
            waitFor("the phone to count") { node.status().phones == 1 }
            node.ensurePhoneKey(new = true) // "Disconnect phones"
            while (source.readUtf8Line() != null) Unit // the stream ends
        }
        waitFor("the phone to go") { node.status().phones == 0 }
        assertEquals(2, savedKeys.size)
        get("/phone/api/status").use { assertEquals(401, it.code) } // the old cookie stopped working
    }

    @Test
    fun `files only other peers have are relayed, checked and ranged`() {
        val other = makeNode("Laptop", File(tmp, "laptop"), "2".repeat(32), null).also { extra += it }
        val movie = ByteArray(2 * 1024 * 1024 + 77) { (it * 7 + it / 300).toByte() }
        File(tmp, "laptop/share/movie.mkv").writeBytes(movie)
        other.start()
        indexed(other, 1)
        node.peers.serviceFound(ServiceRecord("svc-laptop", other.peerId, "Laptop", listOf("127.0.0.1"), other.port, false))
        val id = sha(movie)
        waitFor("the phone to list the laptop's file") { node.commonFiles().any { it.id == id } }

        get("/phone/api/file/$id").use {
            assertEquals(200, it.code)
            assertEquals("attachment; filename*=UTF-8''movie.mkv", it.header("Content-Disposition"))
            assertEquals(movie.size.toLong(), it.header("Content-Length")!!.toLong())
            assertContentEquals(movie, it.body!!.bytes())
        }
        get("/phone/api/file/$id", "Range" to "bytes=1000-1999").use {
            assertEquals(206, it.code)
            assertEquals("bytes 1000-1999/${movie.size}", it.header("Content-Range"))
            assertContentEquals(movie.copyOfRange(1000, 2000), it.body!!.bytes())
        }
        call(Request.Builder().url(url("/phone/api/file/$id")).head(), "Cookie" to cookie).use {
            assertEquals(200, it.code)
            assertEquals(movie.size.toLong(), it.header("Content-Length")!!.toLong())
        }
        other.goOffline()
        waitFor("the laptop to drop out") { node.commonFiles().none { it.id == id } }
        get("/phone/api/file/$id").use { assertEquals(404, it.code) }
    }

    @Test
    fun `the page plays as this phone`() {
        val other = makeNode("Laptop", File(tmp, "laptop"), "2".repeat(32), null).also { extra += it }
        other.start()
        node.peers.serviceFound(ServiceRecord("svc-laptop", other.peerId, "Laptop", listOf("127.0.0.1"), other.port, false))
        other.peers.serviceFound(ServiceRecord("svc-phone", node.peerId, "Test Phone", listOf("127.0.0.1"), node.port, false))
        waitFor("the laptop to be someone to play") { node.arcade.view().opponents.isNotEmpty() }

        val view = get("/phone/api/play").use { assertEquals(200, it.code); json(it) }
        assertEquals(GAME_TITLES.keys, view["titles"]!!.jsonObject.keys)
        assertEquals(listOf(1, 3, 5, 7), view["best_of"]!!.jsonArray.map { it.jsonPrimitive.int })
        assertEquals(other.peerId, view["opponents"]!!.jsonArray.single().jsonObject["peer_id"]!!.jsonPrimitive.content)

        val game = postJson("/phone/api/play/invite", """{"peer_id":"${other.peerId}","kind":"tictactoe","first":true}""").use {
            assertEquals(200, it.code)
            json(it)
        }
        val id = game["id"]!!.jsonPrimitive.content
        assertEquals("waiting", game["status"]!!.jsonPrimitive.content)
        waitFor("the laptop to get the invitation") { other.arcade.view().invites.any { it.id == id } }
        other.arcade.answer(id, true)
        waitFor("the game to start") { node.arcade.view().games.single().status == GAME_PLAYING }

        val playing = get("/phone/api/play").use { json(it) }["games"]!!.jsonArray.single().jsonObject
        assertEquals(true, playing["your_turn"]!!.jsonPrimitive.boolean)
        assertEquals(9, playing["board"]!!.jsonArray.size)
        assertEquals((0..8).toList(), playing["allowed"]!!.jsonArray.map { it.jsonPrimitive.int })
        val moved = postJson("/phone/api/play/$id/move", """{"cell":4}""").use { assertEquals(200, it.code); json(it) }
        assertEquals("x", moved["game"]!!.jsonObject["board"]!!.jsonArray[4].jsonPrimitive.content)
        postJson("/phone/api/play/$id/move", """{"cell":4}""").use { assertEquals(409, it.code) } // not this phone's turn
        postJson("/phone/api/play/$id/resign", "{}").use { assertEquals(200, it.code) }
        postJson("/phone/api/play/$id/close", "{}").use {
            assertEquals(200, it.code)
            assertTrue(json(it)["game"].toString() == "null")
        }
        node.goOffline()
        postJson("/phone/api/play/invite", """{"peer_id":"${other.peerId}","kind":"tictactoe"}""").use { assertEquals(409, it.code) }
    }

    /** Draws a QR code with a quiet zone, four pixels a module, and reads it back. */
    private fun decode(rows: List<String>): String {
        val scale = 4
        val side = (rows.size + 8) * scale
        val pixels = IntArray(side * side) { 0xffffff }
        for ((y, row) in rows.withIndex()) for ((x, c) in row.withIndex()) if (c == '1') {
            for (dy in 0 until scale) for (dx in 0 until scale) pixels[((y + 4) * scale + dy) * side + (x + 4) * scale + dx] = 0
        }
        return QRCodeReader().decode(BinaryBitmap(HybridBinarizer(RGBLuminanceSource(side, side, pixels)))).text
    }

    @Test
    fun `the QR code holds the link`() {
        val links = phoneLinks(listOf("192.168.1.23"), 8765, "abc_DEF-123")
        val link = links.single()
        assertEquals("http://192.168.1.23:8765/phone?key=abc_DEF-123", link.url)
        val rows = link.qr
        assertTrue(rows.size >= 21 && rows.all { it.length == rows.size })
        assertEquals(link.url, decode(rows))
        assertNotNull(node.ensurePhoneKey())
        assertFalse(node.phoneKeyMatches(null))
    }

    @Test
    fun `the hotspot's QR code joins its Wi-Fi`() {
        assertEquals("WIFI:T:WPA;S:AndroidShare_4821;P:k7h2q9xz;;", wifiQrText("AndroidShare_4821", "k7h2q9xz"))
        assertEquals("""WIFI:T:WPA;S:Bob\;s \"Wi-Fi\"\, really\:\\;P:a\;b;;""", wifiQrText("""Bob;s "Wi-Fi", really:\""", "a;b"))
        assertEquals("WIFI:T:nopass;S:Open;;", wifiQrText("Open", null))
        val text = wifiQrText("DIRECT-xy-Phone", "secret pass")
        assertEquals(text, decode(qrRows(text)))
    }
}
