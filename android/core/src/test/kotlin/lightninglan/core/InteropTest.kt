package lightninglan.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.junit.jupiter.api.AfterEach
import org.junit.jupiter.api.Assumptions.assumeTrue
import org.junit.jupiter.api.io.TempDir
import java.io.File
import java.io.IOException
import java.net.ServerSocket
import java.security.MessageDigest
import java.util.Random
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread
import kotlin.test.Test
import kotlin.test.assertContentEquals
import kotlin.test.assertEquals
import kotlin.test.assertTrue

/*
 * The phone's protocol code against real desktop peers: this repository's Python app, started
 * as separate processes. Discovery stays on 127.0.0.1, so nothing on the LAN takes part.
 * Skipped when the repository's .venv (or LIGHTNINGLAN_PYTHON) isn't there.
 */

private val repo = File(System.getProperty("lightninglan.repo") ?: "../..").absoluteFile

private val python: File? = run {
    System.getProperty("lightninglan.python")?.let { return@run File(it) }
    listOf(".venv/Scripts/python.exe", ".venv/bin/python").map { File(repo, it) }.firstOrNull { it.isFile }
}

private val http = OkHttpClient.Builder().readTimeout(30, TimeUnit.SECONDS).build()

private fun freePort(): Int = ServerSocket(0).use { it.localPort }

private fun waitFor(what: String, timeoutMs: Long = 60_000, check: () -> Boolean) {
    val deadline = monotonicMs() + timeoutMs
    var last: Exception? = null
    while (monotonicMs() < deadline) {
        try {
            if (check()) return
        } catch (e: Exception) {
            last = e
        }
        Thread.sleep(200)
    }
    throw AssertionError("timed out waiting for $what" + (last?.let { " (last error: $it)" } ?: ""))
}

private fun sha256(data: ByteArray) = MessageDigest.getInstance("SHA-256").digest(data).toHex()

private fun randomBytes(size: Int, seed: Long) = ByteArray(size).also { Random(seed).nextBytes(it) }

/** A desktop peer: `python -m lightninglan` in its own folders. */
private class DesktopPeer(base: File, val name: String, passphrase: String?) {
    val dir = File(base, name)
    val share = File(dir, "share").apply { mkdirs() }
    val downloads = File(dir, "downloads").apply { mkdirs() }
    val port = freePort()
    val log = File(dir, "peer.log")
    private val process: Process
    lateinit var peerId: String

    init {
        val cmd = mutableListOf(
            python!!.path, "-m", "lightninglan", "--share", share.path, "--downloads", downloads.path,
            "--port", port.toString(), "--name", name, "--config", File(dir, "config.json").path,
            "--refresh-interval", "0.5", "--no-browser", "--no-tray", "--interface", "127.0.0.1",
        )
        if (passphrase != null) cmd += listOf("--passphrase", passphrase)
        process = ProcessBuilder(cmd).directory(repo).redirectErrorStream(true).redirectOutput(log)
            .apply { environment()["PYTHONUNBUFFERED"] = "1" }.start()
        waitFor("$name to start", 30_000) {
            if (!process.isAlive) throw AssertionError("$name exited:\n${log.readText()}")
            peerId = get("/api/status").jsonObject["peer_id"]!!.jsonPrimitive.content
            true
        }
    }

    fun get(path: String): JsonElement = request(Request.Builder().url("http://127.0.0.1:$port$path"))

    fun post(path: String, body: String = "{}"): JsonElement =
        request(Request.Builder().url("http://127.0.0.1:$port$path").post(body.toRequestBody("application/json".toMediaType())))

    private fun request(builder: Request.Builder): JsonElement =
        http.newCall(builder.header("X-LightningLAN-Client", "test").build()).execute().use {
            if (!it.isSuccessful) throw IOException("HTTP ${it.code}: ${it.body?.string()}")
            Json.parseToJsonElement(it.body!!.string())
        }

    fun common(): List<JsonObject> = get("/api/common").jsonObject["files"]!!.jsonArray.map { it.jsonObject }

    fun lists(fileId: String, from: String): Boolean = common().any { f ->
        f["id"]!!.jsonPrimitive.content == fileId && f["sources"]!!.jsonArray.any { it.jsonObject["peer_id"]!!.jsonPrimitive.content == from }
    }

    fun peerIds(kind: String = "peers"): Set<String> =
        get("/api/peers").jsonObject[kind]!!.jsonArray.map { it.jsonObject["peer_id"]!!.jsonPrimitive.content }.toSet()

    /** Downloads a file and waits for it to finish; returns the download as the desktop app reports it. */
    fun download(fileId: String): JsonObject {
        val id = post("/api/downloads", """{"file_id":"$fileId"}""").jsonObject["id"]!!.jsonPrimitive.content
        var result: JsonObject? = null
        waitFor("$name to download $fileId", 120_000) {
            val d = get("/api/downloads").jsonObject["downloads"]!!.jsonArray.map { it.jsonObject }.first { it["id"]!!.jsonPrimitive.content == id }
            val status = d["status"]!!.jsonPrimitive.content
            if (status in setOf("failed", "canceled", "paused")) throw AssertionError("download $status: ${d["error"]}\n${log.readText()}")
            result = d
            status == "completed"
        }
        return result!!
    }

    fun stop() {
        try {
            post("/api/shutdown")
            if (process.waitFor(15, TimeUnit.SECONDS)) return
        } catch (e: Exception) {
            // fall through
        }
        process.destroyForcibly()
        process.waitFor(15, TimeUnit.SECONDS)
    }
}

/** Discovery for a Kotlin peer on the desktop: python-zeroconf announces it and reports the others. */
private class HelperDiscovery : Discovery {
    private var process: Process? = null
    private var listener: DiscoveryListener? = null
    private val script = File(repo, "android/core/src/test/resources/discovery_helper.py")

    @Synchronized
    override fun start(advert: Advert, listener: DiscoveryListener) {
        this.listener = listener
        val p = ProcessBuilder(
            python!!.path, script.path, advert.peerId, advert.name, advert.port.toString(),
            if (advert.auth) "1" else "0", advert.version, "127.0.0.1",
        ).directory(repo).redirectError(ProcessBuilder.Redirect.DISCARD).start()
        process = p
        val registered = java.util.concurrent.CountDownLatch(1)
        thread(isDaemon = true) {
            p.inputStream.bufferedReader().forEachLine { line ->
                val msg = Json.parseToJsonElement(line).jsonObject
                when {
                    "registered" in msg -> registered.countDown()
                    "lost" in msg -> listener.serviceLost(msg["lost"]!!.jsonPrimitive.content)
                    "found" in msg -> {
                        val txt = msg["txt"]!!.jsonObject.mapValues { it.value.jsonPrimitive.content }
                        val addresses = msg["addresses"]!!.jsonArray.map { it.jsonPrimitive.content }
                        parseServiceRecord(msg["found"]!!.jsonPrimitive.content, txt, addresses, msg["port"]!!.jsonPrimitive.content.toInt())
                            ?.let(listener::serviceFound)
                    }
                }
            }
        }
        registered.await(20, TimeUnit.SECONDS)
    }

    @Synchronized
    override fun update(advert: Advert) {
        val l = listener ?: return
        stop()
        start(advert, l)
    }

    @Synchronized
    override fun stop() {
        val p = process ?: return
        process = null
        p.outputStream.close() // the helper says goodbye and exits
        if (!p.waitFor(15, TimeUnit.SECONDS)) p.destroyForcibly()
    }
}

/** The phone's code, running on this computer. */
private class PhonePeer(base: File, name: String, passphrase: String?) {
    val share = File(base, "$name/share").apply { mkdirs() }
    val downloads = File(base, "$name/downloads").apply { mkdirs() }
    private val stateDir = File(base, "$name/state")
    val node: Node by lazy {
        val config = NodeConfig(
            randomBytes(16, System.nanoTime()).toHex(), name, "1.1.0", stateDir, port = freePort(),
            passphraseKey = passphrase?.let(::deriveKey), refreshMs = 500, indexIntervalMs = 500,
        )
        Node(config, PathShareTree(share), PathDownloadFolder(downloads), HelperDiscovery())
    }

    fun start() = node.start()

    fun lists(fileId: String, from: String) = node.commonFiles().any { f -> f.id == fileId && f.sources.any { it.peerId == from } }

    fun download(fileId: String): DownloadView {
        val id = node.downloads.start(fileId).id
        var view: DownloadView? = null
        waitFor("the phone to download $fileId", 120_000) {
            val d = node.downloads.list().first { it.id == id }
            if (d.status in setOf(FAILED, CANCELED, PAUSED)) throw AssertionError("download ${d.status}: ${d.error}")
            view = d
            d.status == COMPLETED
        }
        return view!!
    }
}

class InteropTest {
    @TempDir
    lateinit var tmp: File
    private val desktops = ArrayList<DesktopPeer>()
    private val phones = ArrayList<PhonePeer>()

    private fun desktop(name: String, passphrase: String? = null) = DesktopPeer(tmp, name, passphrase).also { desktops += it }

    private fun phone(name: String, passphrase: String? = null) = PhonePeer(tmp, name, passphrase).also { phones += it }

    @AfterEach
    fun tearDown() {
        for (p in phones) runCatching { p.node.close() }
        for (d in desktops) d.stop()
        if (desktops.isNotEmpty()) println(desktops.joinToString("\n") { "===== ${it.name} =====\n" + it.log.readText() })
    }

    private fun requirePython() = assumeTrue(python != null, "no Python environment with LightningLAN at $repo/.venv")

    @Test
    fun `a desktop peer lists and downloads the phone's files`() {
        requirePython()
        val phone = phone("Phone")
        val small = "hello from the phone".toByteArray()
        val big = randomBytes(3 * 1024 * 1024 + 1234, 1)
        File(phone.share, "small.txt").writeBytes(small)
        File(phone.share, "Camera").mkdirs()
        File(phone.share, "Camera/big.bin").writeBytes(big)
        phone.start()
        val desk = desktop("Desk")

        waitFor("Desk to list the phone's files") { desk.lists(sha256(big), phone.node.peerId) && desk.lists(sha256(small), phone.node.peerId) }
        val item = desk.common().first { it["id"]!!.jsonPrimitive.content == sha256(big) }
        assertEquals("Camera/big.bin", item["name"]!!.jsonPrimitive.content)

        val d = desk.download(sha256(big))
        assertTrue(d["swarm"] !is JsonNull, "the big file should come as a swarm of pieces: $d")
        assertContentEquals(big, File(desk.downloads, "big.bin").readBytes())
        desk.download(sha256(small))
        assertContentEquals(small, File(desk.downloads, "small.txt").readBytes())
        assertTrue(phone.node.uploads.sent.get() >= big.size + small.size)
    }

    @Test
    fun `the phone lists and downloads a desktop peer's files`() {
        requirePython()
        val desk = desktop("Desk")
        val small = "hello from the desk".toByteArray()
        val big = randomBytes(5 * 1024 * 1024 + 77, 2)
        File(desk.share, "notes.txt").writeBytes(small)
        File(desk.share, "big.iso").writeBytes(big)
        val phone = phone("Phone")
        phone.start()

        waitFor("the phone to list Desk's files") { phone.lists(sha256(big), desk.peerId) && phone.lists(sha256(small), desk.peerId) }
        val d = phone.download(sha256(big))
        assertEquals(6, d.swarmPieces)
        assertEquals(listOf("Desk"), d.contributors.map { it.first })
        assertContentEquals(big, File(phone.downloads, "big.iso").readBytes())
        phone.download(sha256(small))
        assertContentEquals(small, File(phone.downloads, "notes.txt").readBytes())
        assertTrue(waitForPeerFile(desk, phone, sha256(big)), "the phone passes on the pieces of what it downloaded")
    }

    private fun waitForPeerFile(desk: DesktopPeer, phone: PhonePeer, fileId: String): Boolean {
        waitFor("the phone to offer pieces of $fileId") { fileId in phone.node.downloads.swarmFiles() }
        return phone.node.pieceInfo(fileId) != null
    }

    @Test
    fun `a desktop peer takes pieces from the phone while it downloads`() {
        requirePython()
        val seed = desktop("Seed")
        val big = randomBytes(8 * 1024 * 1024, 3)
        File(seed.share, "video.mp4").writeBytes(big)
        val phone = phone("Phone")
        phone.start()
        waitFor("the phone to list the video") { phone.lists(sha256(big), seed.peerId) }
        phone.download(sha256(big))

        val other = desktop("Other")
        waitFor("Other to see the phone pass pieces on") {
            other.peerIds().containsAll(listOf(seed.peerId, phone.node.peerId)) && other.lists(sha256(big), seed.peerId)
        }
        val d = other.download(sha256(big))
        val fromPhone = d["swarm"]!!.jsonObject["peers"]!!.jsonArray.map { it.jsonObject }
            .firstOrNull { it["peer_id"]!!.jsonPrimitive.content == phone.node.peerId }
        assertTrue(fromPhone != null && fromPhone["bytes"]!!.jsonPrimitive.long > 0, "Other should get pieces from the phone: $d")
        assertContentEquals(big, File(other.downloads, "video.mp4").readBytes())
    }

    @Test
    fun `passphrases work both ways`() {
        requirePython()
        val phone = phone("Phone", "lightning")
        val mine = "the phone's secret".toByteArray()
        File(phone.share, "secret.txt").writeBytes(mine)
        phone.start()
        val friend = desktop("Friend", "lightning")
        val stranger = desktop("Stranger", "thunder")
        val theirs = "the desk's secret".toByteArray()
        File(friend.share, "desk.txt").writeBytes(theirs)

        waitFor("Friend and the phone to see each other's files") {
            friend.lists(sha256(mine), phone.node.peerId) && phone.lists(sha256(theirs), friend.peerId)
        }
        waitFor("the stranger and the phone to ignore each other") {
            phone.node.peerId in stranger.peerIds("ignored") && phone.node.peers.ignored().any { it.peerId == stranger.peerId }
        }
        assertEquals("passphrase does not match", phone.node.peers.ignored().first { it.peerId == stranger.peerId }.ignoreReason)
        phone.download(sha256(theirs))
        assertContentEquals(theirs, File(phone.downloads, "desk.txt").readBytes())
        friend.download(sha256(mine))
        assertContentEquals(mine, File(friend.downloads, "secret.txt").readBytes())
    }

    @Test
    fun `stopping the phone takes it off the network and starting brings it back`() {
        requirePython()
        val phone = phone("Phone")
        val data = "now you see me".toByteArray()
        File(phone.share, "a.txt").writeBytes(data)
        phone.start()
        val desk = desktop("Desk")
        waitFor("Desk to see the phone") { phone.node.peerId in desk.peerIds() }

        phone.node.goOffline()
        waitFor("Desk to see the phone leave", 30_000) { phone.node.peerId !in desk.peerIds() && !desk.lists(sha256(data), phone.node.peerId) }
        phone.node.goOnline()
        waitFor("Desk to see the phone return") { desk.lists(sha256(data), phone.node.peerId) }
    }

    @Test
    fun `renaming the phone shows up on desktop peers`() {
        requirePython()
        val phone = phone("Phone")
        File(phone.share, "a.txt").writeText("a")
        phone.start()
        val desk = desktop("Desk")
        waitFor("Desk to see the phone") { phone.node.peerId in desk.peerIds() }
        phone.node.setName("Captain Bandwidth's Phone")
        waitFor("Desk to see the new name") {
            (desk.get("/api/peers").jsonObject["peers"] as JsonArray).any { it.jsonObject["name"]!!.jsonPrimitive.content == "Captain Bandwidth's Phone" }
        }
    }
}
