package lightninglan.core

import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.io.TempDir
import java.io.File
import java.net.ServerSocket
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertNotNull
import kotlin.test.assertNull
import kotlin.test.assertTrue

/** Two phones with the app and no Wi-Fi in common: the join link, the NFC tag that hands it over, and the meeting. */
class JoinTest {
    @TempDir
    lateinit var tmp: File
    private val nodes = mutableListOf<Node>()
    private val client = OkHttpClient.Builder().retryOnConnectionFailure(false).build()
    private val hostId = "a".repeat(32)
    private val guestId = "b".repeat(32)

    @AfterEach
    fun tearDown() = nodes.forEach { it.close() }

    private fun node(peerId: String, name: String, passphrase: String? = null, refreshMs: Long = 200): Node {
        val config = NodeConfig(
            peerId, name, "1.4.0", File(tmp, "state-$peerId").apply { mkdirs() }, port = 0,
            passphraseKey = passphrase?.let(::deriveKey), refreshMs = refreshMs, indexIntervalMs = 60_000,
        )
        return Node(config, null, null, null).also {
            it.start()
            nodes.add(it)
        }
    }

    private fun linkTo(host: Node, passphrase: Boolean = false) =
        JoinLink("AndroidShare_1234", "secret password", false, "127.0.0.1", host.port, host.peerId, host.name, passphrase)

    private fun waitFor(what: String, timeoutMs: Long = 15_000, condition: () -> Boolean) {
        val deadline = monotonicMs() + timeoutMs
        while (!condition()) {
            if (monotonicMs() > deadline) throw AssertionError("timed out waiting for $what")
            Thread.sleep(50)
        }
    }

    // ----- the link -----------------------------------------------------------------------------

    @Test
    fun `a join link comes back the same, odd characters and all`() {
        val link = JoinLink("Café & #1=ok+", "p@ss word&=#%", true, "192.168.49.1", 8765, hostId, "Zoë's phone 📱", true)
        val uri = link.toUri()
        assertTrue(uri.startsWith("https://lightninglan.party/join/#v=1&"), uri)
        assertFalse(' ' in uri, uri)
        assertEquals(link, JoinLink.parse(uri))
        val plain = link.copy(password = null, wpa3Only = false, passphrase = false)
        assertEquals(plain, JoinLink.parse(plain.toUri()))
        assertFalse("p=" in plain.toUri() || "w=" in plain.toUri() || "a=" in plain.toUri())
    }

    @Test
    fun `the join page's other spellings open too, and nothing else does`() {
        val fragment = JoinLink("Hotspot", "password1", false, "10.0.0.2", 8765, hostId, "Host", false).toUri().substringAfter('#')
        for (base in listOf("https://lightninglan.party/join", "lightninglan://join", "lightninglan://join/")) {
            assertNotNull(JoinLink.parse("$base#$fragment"), base)
        }
        for (base in listOf("https://evil.example/join/", "http://lightninglan.party/join/", "https://lightninglan.party/phone")) {
            assertNull(JoinLink.parse("$base#$fragment"), base)
        }
        assertNull(JoinLink.parse("https://lightninglan.party/join/"))
    }

    @Test
    fun `a link with something wrong in it is refused`() {
        val good = JoinLink("Hotspot", "password1", false, "10.0.0.2", 8765, hostId, "Host", false).toUri()
        assertNotNull(JoinLink.parse(good))
        for ((from, to) in listOf(
            "v=1" to "v=2", "h=10.0.0.2" to "h=10.0.0.256", "h=10.0.0.2" to "h=example.com", "o=8765" to "o=0",
            "o=8765" to "o=70000", "i=$hostId" to "i=${hostId.uppercase()}", "i=$hostId" to "i=abc", "p=password1" to "p=short",
            "s=Hotspot" to "s=", "s=Hotspot" to "s=${"x".repeat(33)}", "s=Hotspot" to "s=%zz",
        )) {
            assertNull(JoinLink.parse(good.replace(from, to)), "$from -> $to")
        }
        assertEquals("aaaaaaaa", JoinLink.parse(good.replace("n=Host", "n="))?.name) // no name: the start of its ID
    }

    // ----- the NFC tag --------------------------------------------------------------------------

    @Test
    fun `the NDEF message is one URI record, as Android writes it`() {
        val short = ndefUriMessage("https://lightninglan.party/x")
        val rest = "lightninglan.party/x".toByteArray()
        assertContentEquals(byteArrayOf(0xD1.toByte(), 1, (rest.size + 1).toByte(), 'U'.code.toByte(), 4) + rest, short)
        val long = ndefUriMessage("https://lightninglan.party/" + "x".repeat(300))
        assertEquals(0xC1.toByte(), long[0]) // no SR flag: a four-byte length
        assertEquals(1 + "lightninglan.party/".length + 300, (long[2].toInt() shl 24) or (long[3].toInt() shl 16) or ((long[4].toInt() and 0xFF) shl 8) or (long[5].toInt() and 0xFF))
        assertEquals('U'.code.toByte(), long[6])
    }

    private fun hex(text: String) = text.replace(" ", "").chunked(2).map { it.toInt(16).toByte() }.toByteArray()

    private val ok = hex("9000")

    @Test
    fun `a reader reads the link off the tag the way the Type 4 Tag spec says`() {
        val message = ndefUriMessage(JoinLink("Hotspot", "password1", false, "10.0.0.2", 8765, hostId, "Host", false).toUri())
        val tag = NdefTag { message }
        assertContentEquals(hex("6A82"), tag.process(hex("00A4000C02E103")), "a file before the application")
        assertContentEquals(ok, tag.process(hex("00A4040007D276000085010100")))
        assertContentEquals(ok, tag.process(hex("00A4000C02E103")))
        val cc = tag.process(hex("00B000000F"))
        assertEquals(17, cc.size)
        assertContentEquals(ok, cc.copyOfRange(15, 17))
        assertContentEquals(hex("000F20"), cc.copyOfRange(0, 3))
        val mle = ((cc[3].toInt() and 0xFF) shl 8) or (cc[4].toInt() and 0xFF)
        assertContentEquals(hex("0406E104"), cc.copyOfRange(7, 11))
        assertEquals(message.size + 2, ((cc[11].toInt() and 0xFF) shl 8) or (cc[12].toInt() and 0xFF))
        assertContentEquals(hex("00FF"), cc.copyOfRange(13, 15)) // read by anyone, written by nobody
        assertContentEquals(ok, tag.process(hex("00A4000C02E104")))
        val nlen = tag.process(hex("00B0000002"))
        assertEquals(message.size, ((nlen[0].toInt() and 0xFF) shl 8) or (nlen[1].toInt() and 0xFF))
        val read = java.io.ByteArrayOutputStream()
        var offset = 2
        while (offset < message.size + 2) {
            val n = minOf(mle, message.size + 2 - offset)
            val r = tag.process(byteArrayOf(0, 0xB0.toByte(), (offset shr 8).toByte(), offset.toByte(), n.toByte()))
            assertContentEquals(ok, r.copyOfRange(r.size - 2, r.size))
            read.write(r, 0, r.size - 2)
            offset += r.size - 2
        }
        assertContentEquals(message, read.toByteArray())
        assertContentEquals(hex("6B00"), tag.process(hex("00B0FFFF01")))
        assertContentEquals(hex("6982"), tag.process(hex("00D6000002 0000")), "it can't be written")
        assertContentEquals(hex("6D00"), tag.process(hex("00CA000000")))
        assertContentEquals(hex("6A82"), tag.process(hex("00A4040007A000000003101000")), "another application")
    }

    @Test
    fun `with nothing to hand out, the tag isn't there`() {
        var message: ByteArray? = null
        val tag = NdefTag { message }
        assertContentEquals(hex("6A82"), tag.process(hex("00A4040007D276000085010100")))
        message = ndefUriMessage("https://lightninglan.party/join/")
        assertContentEquals(ok, tag.process(hex("00A4040007D276000085010100")))
        tag.reset()
        assertContentEquals(hex("6A82"), tag.process(hex("00A4000C02E104")))
    }

    // ----- meeting on the hotspot ---------------------------------------------------------------

    @Test
    fun `the phone that joined introduces itself, and the two see each other`() {
        val host = node(hostId, "Host Phone")
        val guest = node(guestId, "Guest Phone")
        assertNull(guest.introduce(linkTo(host)))
        waitFor("both to be online") {
            host.peers.online().any { it.peerId == guestId } && guest.peers.online().any { it.peerId == hostId }
        }
        val seen = host.peers.get(guestId)!!
        assertEquals("Guest Phone", seen.name)
        assertEquals(listOf("127.0.0.1"), seen.addresses)
        assertEquals(guest.port, seen.port)
        assertTrue(seen.direct)
        assertTrue(guest.peers.get(hostId)!!.direct)
        guest.peers.part(hostId) // it left the hotspot
        assertNull(guest.peers.get(hostId))
    }

    @Test
    fun `a passphrase has to match, as for any peer`() {
        val host = node(hostId, "Host Phone", passphrase = "lan party")
        val guest = node(guestId, "Guest Phone")
        assertEquals(AUTH_REASONS["missing"], guest.introduce(linkTo(host, passphrase = true)))
        assertNull(host.peers.get(guestId))
        val other = node("c".repeat(32), "Other Phone", passphrase = "something else")
        assertEquals(AUTH_REASONS["bad_signature"], other.introduce(linkTo(host, passphrase = true)))
        val friend = node("d".repeat(32), "Friend Phone", passphrase = "lan party")
        assertNull(friend.introduce(linkTo(host, passphrase = true)))
        waitFor("the friend to be online") { host.peers.online().any { it.name == "Friend Phone" } }
    }

    @Test
    fun `an introduction has to make sense`() {
        val host = node(hostId, "Host Phone")
        fun post(body: String) = client.newCall(
            Request.Builder().url("http://127.0.0.1:${host.port}/api/introduce").post(body.toRequestBody("application/json".toMediaType())).build(),
        ).execute().use { it.code }
        assertEquals(405, client.newCall(Request.Builder().url("http://127.0.0.1:${host.port}/api/introduce").build()).execute().use { it.code })
        assertEquals(400, post("not json"))
        assertEquals(400, post("""{"peer_id": "nope", "port": 1}"""))
        assertEquals(400, post("""{"peer_id": "$guestId", "port": 0}"""))
        assertEquals(400, post("""{"peer_id": "$guestId", "port": "8765"}"""))
        assertEquals(400, post("""{"peer_id": "$hostId", "port": 8765}"""), "its own ID")
        assertEquals(200, post("""{"peer_id": "$guestId", "port": 9, "name": "‮Guest"}"""))
        assertEquals("Guest", host.peers.get(guestId)?.name) // no bidi tricks in names
        host.goOffline()
        assertEquals(503, post("""{"peer_id": "$guestId", "port": 9}"""))
    }

    @Test
    fun `only so many peers can be met this way`() {
        val host = node(hostId, "Host Phone")
        val met = (0 until 20).count { host.peers.meet("%032x".format(it + 1), "Peer $it", "127.0.0.1", 9) }
        assertEquals(16, met)
        assertTrue(host.peers.meet("%032x".format(1), "Peer 0", "127.0.0.1", 9), "one it knows already can always come back")
    }

    @Test
    fun `a peer met directly is forgotten once it has been out of reach for a while`() {
        val host = node(hostId, "Host Phone", refreshMs = 50)
        val closed = ServerSocket(0).use { it.localPort } // nothing listens there now
        assertTrue(host.peers.meet(guestId, "Gone Phone", "127.0.0.1", closed))
        assertNotNull(host.peers.get(guestId))
        // Twelve failed polls; a refused connection to this computer can take Windows a second or two each.
        waitFor("it to be forgotten", timeoutMs = 90_000) { host.peers.get(guestId) == null }
    }
}
