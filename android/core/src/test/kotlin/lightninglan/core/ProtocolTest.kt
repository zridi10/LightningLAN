package lightninglan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import java.io.File
import java.nio.ByteBuffer
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertFalse
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Expected values were computed with the desktop app's Python code (lightninglan/auth.py, pieces.py, pathsafe.py). */
class ProtocolTest {
    private val peerId = "a".repeat(32)

    @Test
    fun `passphrase keys match Python's pbkdf2_hmac`() {
        assertEquals("5fa87f9ba908c26a5eaa90e330176865628f75e94e91d261326635d1db325341", deriveKey("correct horse").toHex())
        assertEquals("b8fa392541c47deaa24dd071219b11d8982c6cb72c1d0bcb07a866b8692a11fc", deriveKey("pässwörd ⚡").toHex())
    }

    @Test
    fun `a token signed by the desktop app is checked the same way`() {
        val auth = Authenticator(deriveKey("correct horse"), "b".repeat(32))
        val sig = "c2d4634ff1c8bec2ea2eeeca6faca6a68de307825ad386bb427505c776c00905"
        val token = "v1.$peerId.1700000000.${"b".repeat(24)}.$sig"
        assertEquals("b6a2200682b3578ba8f75bf911f7ef26277fb00d1cf09b1f44f2ca198c3be2d9", auth.proofFor(token))
        // The signature is right but the timestamp is from 2023.
        assertEquals("clock_skew", assertFailsWith<AuthError> { auth.verifyRequest("GET", "/api/files", token) }.reason)
        assertEquals("bad_signature", assertFailsWith<AuthError> { auth.verifyRequest("GET", "/api/other", token) }.reason)
    }

    @Test
    fun `signed requests verify, once`() {
        val key = deriveKey("lightning")
        val client = Authenticator(key, peerId)
        val server = Authenticator(key, "c".repeat(32))
        val token = client.signRequest("get", "/api/files").getValue(TOKEN_HEADER)
        assertEquals(peerId, server.verifyRequest("GET", "/api/files", token))
        assertEquals("replay", assertFailsWith<AuthError> { server.verifyRequest("GET", "/api/files", token) }.reason)
        assertTrue(client.verifyResponse(token, server.proofFor(token)))
        assertFalse(client.verifyResponse(token, Authenticator(deriveKey("thunder"), "d".repeat(32)).proofFor(token)))
        assertEquals("missing", assertFailsWith<AuthError> { server.verifyRequest("GET", "/api/files", null) }.reason)
        assertEquals("malformed", assertFailsWith<AuthError> { server.verifyRequest("GET", "/api/files", "v1.nope") }.reason)
        val other = Authenticator(deriveKey("thunder"), peerId).signRequest("GET", "/api/files").getValue(TOKEN_HEADER)
        assertEquals("bad_signature", assertFailsWith<AuthError> { server.verifyRequest("GET", "/api/files", other) }.reason)
        val off = Authenticator(null, peerId)
        assertTrue(off.signRequest("GET", "/x").isEmpty())
        assertTrue(off.verifyResponse(null, null))
    }

    @Test
    fun `piece hashes match Python's hash_file`() {
        val data = ByteArray(256 * (3 * 4096 + 1)) { (it % 256).toByte() }
        val file = File.createTempFile("pieces", ".bin").apply { deleteOnExit(); writeBytes(data) }
        val hashes = PathReadable(file).use { hashFile(it) }
        assertEquals("679361bf172a2b2f3d48919ee7b2b6fea9cf6e351005736ac7274ac8fde4e0f2", hashes.sha256)
        assertEquals(
            "fbbab289f7f94b25736c58be46a994c441fd02552cc6022352e3d86d2fab7c83".repeat(3) +
                "40aff2e9d2d8922e47afd4648e6967497158785fbd1da870e7110266bf944880",
            hashes.pieces.toHex(),
        )
        val small = File.createTempFile("small", ".bin").apply { deleteOnExit(); writeBytes(ByteArray(1000)) }
        assertEquals(0, PathReadable(small).use { hashFile(it) }.pieces.size)
    }

    @Test
    fun `piece sizes, piece lists and have-maps`() {
        val sizes = listOf(0L, 1L, 1L shl 20, 1L shl 31, 1L shl 33, 1L shl 35, 1L shl 40).map { pieceSizeFor(it) }
        assertEquals(listOf(1048576, 1048576, 1048576, 1048576, 4194304, 16777216, 16777216), sizes)
        assertEquals("sMA=", encodeBits(byteArrayOf(1, 0, 1, 1, 0, 0, 0, 0, 1, 1)))
        assertContentEquals(byteArrayOf(1, 0, 1, 1, 0, 0, 0, 0, 1, 1), decodeBits("sMA=", 10))
        assertFailsWith<IllegalArgumentException> { decodeBits("sMA=", 30) }

        val size = 3L * 1024 * 1024 + 5
        val info = PieceInfo("f".repeat(64), size, ByteArray(4 * 32) { it.toByte() })
        assertEquals(info, PieceInfo.fromJson(info.toJson(), info.fileId, size))
        assertEquals(3L * 1024 * 1024, info.start(3))
        assertEquals(5, info.length(3))
        assertFailsWith<IllegalArgumentException> { PieceInfo.fromJson(info.toJson(), "e".repeat(64), size) }
        assertFailsWith<IllegalArgumentException> { PieceInfo.fromJson(info.toJson(), info.fileId, size + 1) }
    }

    @Test
    fun `remote file names are made safe the way the desktop app makes them`() {
        val cases = mapOf(
            "a/b/c.txt" to "c.txt",
            "CON.txt" to "_CON.txt",
            ".." to "download",
            ".hidden" to "_hidden",
            " x. " to "x",
            "a:b?.txt" to "a_b_.txt",
            "x".repeat(300) + ".txt" to "x".repeat(196) + ".txt",
            "evil\u202etxt.exe" to "eviltxt.exe",
        )
        for ((name, expected) in cases) assertEquals(expected, safeLocalFilename(name), name)
        assertEquals(listOf("a.txt", "a (1).txt", "a (2).txt"), numberedNames("a.txt").take(3).toList())
        assertEquals("backup" to ".tar.gz", splitExtension("backup.tar.gz"))
        assertEquals("evil�name", displayName("evil\u0007name"))
        assertTrue(isIgnoredName(".DS_Store") && isIgnoredName("movie.mkv.part") && isIgnoredName("Thumbs.db"))
        assertFalse(isIgnoredName("holiday.jpg"))
    }

    @Test
    fun `ranges are read like the desktop app reads them`() {
        assertEquals(RangeResult.Bytes(0, 99), parseRange("bytes=0-", 100))
        assertEquals(RangeResult.Bytes(10, 19), parseRange("bytes=10-19", 100))
        assertEquals(RangeResult.Bytes(90, 99), parseRange("bytes=-10", 100))
        assertEquals(RangeResult.Bytes(0, 99), parseRange("bytes=-1000", 100))
        assertEquals(RangeResult.Bytes(50, 99), parseRange("bytes=50-99999999999999999999", 100))
        assertEquals(RangeResult.Unsatisfiable, parseRange("bytes=100-", 100))
        assertEquals(RangeResult.Unsatisfiable, parseRange("bytes=-0", 100))
        assertNull(parseRange("bytes=5-1", 100))
        assertNull(parseRange("bytes=0-1,5-6", 100))
        assertNull(parseRange("items=0-1", 100))
    }

    @Test
    fun `indexes from other peers are validated`() {
        val payload = Json.parseToJsonElement(
            """{"peer_id":"$peerId","name":"Desk\u0007top","files":[
               {"sha256":"${"1".repeat(64)}","name":"a.txt","size":5,"mtime":1.5},
               {"sha256":"nope","name":"b.txt","size":5},
               {"sha256":"${"2".repeat(64)}","name":"c.txt","size":-1},
               {"sha256":"${"3".repeat(64)}","name":"d.txt","size":1.5},
               {"sha256":"${"4".repeat(64)}","name":"e.txt","size":7,"mtime":"x"}],
               "indexing":{"pending":2},"swarm":["${"1".repeat(64)}","bad"]}""",
        )
        val parsed = parseIndex(payload, peerId)
        assertEquals("Desk�top", parsed.name)
        assertEquals(listOf("a.txt", "e.txt"), parsed.files.map { it.name })
        assertEquals(0.0, parsed.files[1].mtime)
        assertEquals(2, parsed.pending)
        assertEquals(true to setOf("1".repeat(64)), parseSwarm(payload.jsonObject))
        assertFailsWith<PollError> { parseIndex(payload, "b".repeat(32)) }
    }

    @Test
    fun `service records need a peer id, a port and an address`() {
        val txt = mapOf("id" to peerId, "name" to "Phone", "auth" to "1", "v" to "1.1.0")
        assertEquals(ServiceRecord("svc", peerId, "Phone", listOf("10.0.0.2"), 8765, true), parseServiceRecord("svc", txt, listOf("10.0.0.2"), 8765))
        assertNull(parseServiceRecord("svc", txt + ("id" to "zz"), listOf("10.0.0.2"), 8765))
        assertNull(parseServiceRecord("svc", txt, emptyList(), 8765))
        assertEquals("aaaaaaaa", parseServiceRecord("svc", txt - "name", listOf("10.0.0.2"), 8765)!!.name)
        val advert = Advert(peerId, "ü".repeat(150), 8765, false, "1.1.0")
        assertEquals(200, advert.txt().getValue("name").toByteArray().size)
        assertEquals("0", advert.txt()["auth"])
    }

    @Test
    fun `positional reads of a partial file`() {
        val file = File.createTempFile("partial", ".part").apply { deleteOnExit() }
        PathPartial(file).use { p ->
            writeFully(p, ByteBuffer.wrap("world".toByteArray()), 6)
            writeFully(p, ByteBuffer.wrap("hello ".toByteArray()), 0)
            assertEquals(11, p.size())
        }
        assertEquals("hello world", file.readText())
    }
}
