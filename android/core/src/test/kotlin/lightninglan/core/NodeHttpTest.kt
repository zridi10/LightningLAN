package lightninglan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.BeforeEach
import org.junit.jupiter.api.io.TempDir
import java.io.File
import java.net.Socket
import java.security.MessageDigest
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** The phone's peer-to-peer HTTP API, called the way other peers call it. */
class NodeHttpTest {
    @TempDir
    lateinit var tmp: File
    private lateinit var share: File
    private lateinit var node: Node
    private val client = OkHttpClient.Builder().retryOnConnectionFailure(false).build()
    private val small = "hello from the phone".toByteArray()
    private val big = ByteArray(3 * 1024 * 1024 + 1234) { (it * 31 + it / 7).toByte() }

    private fun startNode(passphrase: String? = null) {
        share = File(tmp, "share").apply { mkdirs() }
        File(share, "small.txt").writeBytes(small)
        File(share, "sub").mkdirs()
        File(share, "sub/big.bin").writeBytes(big)
        File(share, ".hidden").writeText("never shared")
        File(share, "movie.mkv.part").writeText("never shared")
        val config = NodeConfig(
            "1".repeat(32), "Test Phone", "1.1.0", File(tmp, "state"), port = 0,
            passphraseKey = passphrase?.let(::deriveKey), refreshMs = 500, indexIntervalMs = 60_000,
        )
        node = Node(config, PathShareTree(share), PathDownloadFolder(File(tmp, "downloads").apply { mkdirs() }), null)
        node.start()
        // The indexer's first pass; the next one is a minute away.
        val deadline = monotonicMs() + 10_000
        while (node.indexer.snapshot.let { it.entries.size < 2 || it.pending > 0 } && monotonicMs() < deadline) Thread.sleep(20)
    }

    @BeforeEach
    fun setUp() = startNode()

    @AfterEach
    fun tearDown() = node.close()

    private fun url(path: String) = "http://127.0.0.1:${node.port}$path"

    private fun get(path: String, vararg headers: Pair<String, String>, method: String = "GET"): Response {
        val builder = Request.Builder().url(url(path)).header("Accept-Encoding", "identity")
        for ((k, v) in headers) builder.header(k, v)
        if (method == "HEAD") builder.head()
        if (method == "POST") builder.post(ByteArray(0).toRequestBody())
        return client.newCall(builder.build()).execute()
    }

    private fun sha(data: ByteArray) = MessageDigest.getInstance("SHA-256").digest(data).toHex()

    private fun json(resp: Response): JsonObject = Json.parseToJsonElement(resp.body!!.string()).jsonObject

    @Test
    fun `a made-up name gives way to another peer with the same name`() {
        node.setName("Test Phone", generated = true)
        var saved: String? = null
        node.onRenamed = { saved = it }
        val otherId = "2".repeat(32)
        node.peers.serviceFound(ServiceRecord("svc-other", otherId, "TEST PHONE", listOf("127.0.0.9"), 1, false))
        val other = node.peers.get(otherId)!!.apply {
            state = ONLINE
            nameAuto = true
        }
        node.checkName() // both made up: the one with the larger ID gives way, and that's the other one
        assertEquals("Test Phone", node.name)
        other.nameAuto = false // a picked name, or a peer from before 1.3
        node.checkName()
        assertTrue(node.name.lowercase() != "test phone" && node.nameGenerated)
        assertEquals(node.name, saved)
        assertEquals(true, json(get("/api/files"))["name_auto"]!!.jsonPrimitive.boolean)
        node.setName("Test Phone") // picked by the person: never changes by itself
        node.checkName()
        assertEquals("Test Phone", node.name)
        assertEquals(false, json(get("/api/files"))["name_auto"]!!.jsonPrimitive.boolean)
    }

    @Test
    fun `the index lists shareable files with an ETag`() {
        val (body, etag) = get("/api/files").use { assertEquals(200, it.code); json(it) to it.header("ETag")!! }
        assertEquals("1".repeat(32), body["peer_id"]!!.jsonPrimitive.content)
        assertEquals("Test Phone", body["name"]!!.jsonPrimitive.content)
        val files = body["files"]!!.jsonArray.map { it.jsonObject }
        assertEquals(listOf("small.txt", "sub/big.bin"), files.map { it["name"]!!.jsonPrimitive.content })
        assertEquals(sha(big), files[1]["sha256"]!!.jsonPrimitive.content)
        assertEquals(big.size.toLong(), files[1]["size"]!!.jsonPrimitive.content.toLong())
        assertTrue(body["swarm"]!!.jsonArray.isEmpty())
        get("/api/files", "If-None-Match" to etag).use { assertEquals(304, it.code) }
    }

    @Test
    fun `files are served whole, in ranges and as HEAD`() {
        val id = sha(big)
        get("/api/download/$id").use {
            assertEquals(200, it.code)
            assertEquals("\"$id\"", it.header("ETag"))
            assertEquals("attachment; filename*=UTF-8''big.bin", it.header("Content-Disposition"))
            assertContentEquals(big, it.body!!.bytes())
        }
        get("/api/download/$id", "Range" to "bytes=100-199", "If-Range" to "\"$id\"").use {
            assertEquals(206, it.code)
            assertEquals("bytes 100-199/${big.size}", it.header("Content-Range"))
            assertContentEquals(big.copyOfRange(100, 200), it.body!!.bytes())
        }
        get("/api/download/$id", "Range" to "bytes=-10").use { assertContentEquals(big.copyOfRange(big.size - 10, big.size), it.body!!.bytes()) }
        get("/api/download/$id", "Range" to "bytes=0-9", "If-Range" to "\"other\"").use { assertEquals(200, it.code) }
        get("/api/download/$id", "Range" to "bytes=${big.size}-").use {
            assertEquals(416, it.code)
            assertEquals("bytes */${big.size}", it.header("Content-Range"))
        }
        get("/api/download/$id", method = "HEAD").use {
            assertEquals(200, it.code)
            assertEquals(big.size.toString(), it.header("Content-Length"))
        }
        get("/api/download/${"0".repeat(64)}").use { assertEquals(404, it.code) }
        get("/api/download/nope").use { assertEquals(404, it.code) }
        get("/api/download/$id", method = "POST").use { assertEquals(405, it.code) }
        get("/api/status").use { assertEquals(404, it.code) }
    }

    @Test
    fun `a file that changed after indexing is refused`() {
        val id = sha(small)
        File(share, "small.txt").writeBytes("changed!".toByteArray())
        get("/api/download/$id").use { assertEquals(409, it.code) }
    }

    @Test
    fun `big files have piece lists`() {
        val body = get("/api/pieces/${sha(big)}").use { assertEquals(200, it.code); json(it) }
        val info = PieceInfo.fromJson(body, sha(big), big.size.toLong())
        assertEquals(4, info.count)
        assertContentEquals(MessageDigest.getInstance("SHA-256").digest(big.copyOfRange(0, 1024 * 1024)), info.digest(0))
        get("/api/pieces/${sha(small)}").use { assertEquals(404, it.code) }
        get("/api/swarm/${sha(big)}").use { assertEquals(404, it.code) }
        get("/api/piece/${sha(big)}/0").use { assertEquals(404, it.code) }
    }

    @Test
    fun `a stopped phone answers 503`() {
        node.goOffline()
        get("/api/files").use { assertEquals(503, it.code) }
        node.goOnline()
        get("/api/files").use { assertEquals(200, it.code) }
    }

    @Test
    fun `with a passphrase requests need a token and answers carry a proof`() {
        node.close()
        startNode(passphrase = "lightning")
        get("/api/files").use {
            assertEquals(401, it.code)
            assertEquals("missing", json(it)["reason"]!!.jsonPrimitive.content)
        }
        val mine = Authenticator(deriveKey("lightning"), "2".repeat(32))
        val token = mine.signRequest("GET", "/api/files").getValue(TOKEN_HEADER)
        get("/api/files", TOKEN_HEADER to token).use {
            assertEquals(200, it.code)
            assertTrue(mine.verifyResponse(token, it.header(PROOF_HEADER)))
        }
        val wrong = Authenticator(deriveKey("thunder"), "2".repeat(32)).signRequest("GET", "/api/files").getValue(TOKEN_HEADER)
        get("/api/files", TOKEN_HEADER to wrong).use {
            assertEquals(401, it.code)
            assertEquals("bad_signature", json(it)["reason"]!!.jsonPrimitive.content)
            assertNull(it.header(PROOF_HEADER))
        }
    }

    @Test
    fun `connections are kept alive between requests`() {
        Socket("127.0.0.1", node.port).use { s ->
            val out = s.getOutputStream()
            val input = s.getInputStream().bufferedReader(Charsets.ISO_8859_1)
            repeat(2) {
                out.write("HEAD /api/files HTTP/1.1\r\nHost: x\r\n\r\n".toByteArray())
                out.flush()
                assertEquals("HTTP/1.1 200 OK", input.readLine())
                while (input.readLine().isNotEmpty()) Unit
            }
            out.write("GET /api/files HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".toByteArray())
            out.flush()
            val all = input.readText()
            assertTrue(all.startsWith("HTTP/1.1 200 OK") && "Connection: close" in all && "\"peer_id\"" in all)
        }
    }

    @Test
    fun `the home page names the phone`() {
        get("/").use {
            assertEquals(200, it.code)
            assertNotNull(it.body!!.string().let { body -> body.takeIf { "Test Phone" in it } })
        }
    }
}
