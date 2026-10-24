package lightninglan.core

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.SynchronousQueue
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/*
 * A small HTTP/1.1 server for the peer-to-peer API and the phone page: keep-alive, fixed-length
 * streamed bodies (or, for event streams, a body that lasts until the connection closes), and
 * request bodies with a Content-Length. Other peers only ever send bodiless GET and HEAD
 * requests; the phone page posts small JSON bodies and the files it sends.
 */

private const val TAG = "http"
private const val IDLE_TIMEOUT_MS = 30_000
private const val MAX_HEADER_BYTES = 16 * 1024
private const val MAX_HEADERS = 100
private const val MAX_SKIPPED_BODY = 64 * 1024 // a body nobody read: skipped to keep the connection, or the connection closes

class HttpRequest(
    val method: String,
    val path: String,
    val query: String?,
    /** Header names in lower case; repeated headers joined with ", ". */
    val headers: Map<String, String>,
    val remoteAddress: String,
    /** The request body: exactly Content-Length bytes (none without one). */
    val body: InputStream = java.io.ByteArrayInputStream(ByteArray(0)),
    val contentLength: Long = 0,
) {
    fun header(name: String): String? = headers[name.lowercase()]

    /** A query parameter, percent-decoded. */
    fun param(name: String): String? = query?.split('&')?.firstNotNullOfOrNull { pair ->
        val eq = pair.indexOf('=')
        val key = if (eq >= 0) pair.substring(0, eq) else pair
        if (decodeQuery(key) == name) decodeQuery(if (eq >= 0) pair.substring(eq + 1) else "") else null
    }

    /** A small body (JSON), or null if it is longer than [limit]. */
    fun readBody(limit: Int = 64 * 1024): ByteArray? = if (contentLength > limit) null else body.readBytes()
}

private fun decodeQuery(text: String): String = try {
    java.net.URLDecoder.decode(text, "UTF-8")
} catch (e: IllegalArgumentException) {
    text
}

abstract class HttpBody {
    /** The length in bytes, or -1 for a body that lasts until the connection closes (an event stream). */
    abstract val length: Long

    /** Writes exactly [length] bytes, or throws (then the connection is closed mid-response). */
    abstract fun writeTo(out: OutputStream)
}

/** Reads at most [remaining] bytes of a connection: one request's body. */
private class BodyStream(private val input: InputStream, var remaining: Long) : InputStream() {
    override fun read(): Int {
        if (remaining <= 0) return -1
        val b = input.read()
        if (b < 0) throw IOException("the request body ended early")
        remaining--
        return b
    }

    override fun read(b: ByteArray, off: Int, len: Int): Int {
        if (remaining <= 0) return -1
        val n = input.read(b, off, minOf(len.toLong(), remaining).toInt())
        if (n < 0) throw IOException("the request body ended early")
        remaining -= n
        return n
    }

    override fun available(): Int = minOf(input.available().toLong(), remaining).toInt()

    /** Skips what the handler didn't read. False if that is too much (then the connection closes). */
    fun drain(): Boolean {
        if (remaining > MAX_SKIPPED_BODY) return false
        val buf = ByteArray(8192)
        while (remaining > 0) if (read(buf) < 0) return false
        return true
    }
}

class BytesBody(private val bytes: ByteArray) : HttpBody() {
    override val length: Long get() = bytes.size.toLong()
    override fun writeTo(out: OutputStream) = out.write(bytes)
}

class HttpResponse(
    val status: Int,
    val body: HttpBody? = null,
    /** Runs after the response was sent or abandoned (closes files). */
    private val onClose: (() -> Unit)? = null,
) {
    val headers = ArrayList<Pair<String, String>>()

    fun header(name: String, value: String): HttpResponse {
        headers.add(name to value)
        return this
    }

    fun header(name: String): String? = headers.firstOrNull { it.first.equals(name, ignoreCase = true) }?.second

    internal fun close() {
        try {
            onClose?.invoke()
        } catch (e: Exception) {
            CoreLog.w(TAG, "closing a response failed", e)
        }
    }
}

/** Thrown while writing a body to end the connection right there (the client sees a short body). */
class ConnectionCut : IOException("transfer cut off")

class HttpServer(private val handler: (HttpRequest) -> HttpResponse, private val maxConnections: Int = 64) {
    @Volatile
    private var server: ServerSocket? = null
    private var pool: ThreadPoolExecutor? = null
    private val connections: MutableSet<Socket> = ConcurrentHashMap.newKeySet()

    val port: Int get() = server?.localPort ?: 0

    /** Listens on every interface. Returns the port (useful when [port] is 0). */
    @Throws(IOException::class)
    fun start(port: Int): Int {
        val socket = ServerSocket()
        try {
            socket.reuseAddress = true
            socket.bind(InetSocketAddress(port), 128)
        } catch (e: IOException) {
            socket.close()
            throw e
        }
        server = socket
        val count = AtomicInteger()
        val executor = ThreadPoolExecutor(0, maxConnections, 30, TimeUnit.SECONDS, SynchronousQueue()) { r ->
            Thread(r, "lightninglan-http-${count.incrementAndGet()}").apply { isDaemon = true }
        }
        pool = executor
        thread(name = "lightninglan-http-accept", isDaemon = true) { acceptLoop(socket, executor) }
        return socket.localPort
    }

    fun stop() {
        server?.close()
        server = null
        for (c in connections) runCatching { c.close() }
        pool?.shutdownNow()
        pool = null
    }

    private fun acceptLoop(socket: ServerSocket, executor: ThreadPoolExecutor) {
        while (!socket.isClosed) {
            val client = try {
                socket.accept()
            } catch (e: IOException) {
                break
            }
            try {
                executor.execute { serve(client) }
            } catch (e: RejectedExecutionException) {
                runCatching { client.close() } // too many connections at once
            }
        }
    }

    private fun serve(client: Socket) {
        connections.add(client)
        try {
            client.soTimeout = IDLE_TIMEOUT_MS
            client.tcpNoDelay = true
            val input = RequestReader(BufferedInputStream(client.getInputStream(), 8192))
            val output = BufferedOutputStream(client.getOutputStream(), 64 * 1024)
            val remote = client.inetAddress?.hostAddress ?: ""
            while (true) {
                val parsed = try {
                    input.next(remote)
                } catch (e: BadRequest) {
                    writeHead(output, HttpResponse(400), keepAlive = false, length = 0)
                    output.flush()
                    break
                } ?: break
                val (request, keepAliveRequested) = parsed
                val response = try {
                    handler(request)
                } catch (e: Exception) {
                    CoreLog.e(TAG, "error handling ${request.method} ${request.path}", e)
                    jsonError(500, "internal_error", "Internal error.")
                }
                // An unread body (a refused upload) is skipped if it is small; a big one ends the connection.
                val bodyDone = (request.body as? BodyStream)?.let { body -> body.remaining == 0L || runCatching { body.drain() }.getOrDefault(false) } ?: true
                val keepAlive = keepAliveRequested && bodyDone && response.body?.length != -1L
                val ok = try {
                    send(output, request, response, keepAlive)
                } finally {
                    response.close()
                }
                if (!ok || !keepAlive) break
            }
        } catch (e: IOException) {
            // the other side went away
        } catch (e: Exception) { // never let one connection bring the app down
            CoreLog.e(TAG, "a connection failed", e)
        } finally {
            connections.remove(client)
            runCatching { client.close() }
        }
    }

    private class BadRequest : Exception()

    /** Reads the requests that arrive on one connection. */
    private class RequestReader(private val input: InputStream) {
        private var headerBytes = 0

        /** The next request and whether to keep the connection open; null when the client closed it (or stayed idle). */
        fun next(remote: String): Pair<HttpRequest, Boolean>? {
            headerBytes = 0
            val requestLine = try {
                readLine(firstLine = true) ?: return null
            } catch (e: SocketTimeoutException) {
                return null
            }
            val parts = requestLine.split(" ")
            if (parts.size != 3 || !parts[2].startsWith("HTTP/1.")) throw BadRequest()
            val (method, target, version) = parts
            val headers = LinkedHashMap<String, String>()
            var count = 0
            while (true) {
                val line = readLine(firstLine = false) ?: throw BadRequest()
                if (line.isEmpty()) break
                if (++count > MAX_HEADERS) throw BadRequest()
                val colon = line.indexOf(':')
                if (colon <= 0) throw BadRequest()
                val name = line.substring(0, colon).trim().lowercase()
                val value = line.substring(colon + 1).trim()
                headers[name] = headers[name]?.let { "$it, $value" } ?: value
            }
            if (headers.containsKey("transfer-encoding")) throw BadRequest()
            val bodyLength = headers["content-length"]?.let { it.toLongOrNull() ?: throw BadRequest() } ?: 0L
            if (bodyLength < 0) throw BadRequest()
            val connection = headers["connection"]?.lowercase() ?: ""
            val keepAlive = if (version == "HTTP/1.0") "keep-alive" in connection else "close" !in connection
            val q = target.indexOf('?')
            val path = if (q >= 0) target.substring(0, q) else target
            val query = if (q >= 0) target.substring(q + 1) else null
            val body = BodyStream(input, bodyLength) // the handler reads it, or it is skipped after the response
            return HttpRequest(method.uppercase(), path, query, headers, remote, body, bodyLength) to keepAlive
        }

        private fun readLine(firstLine: Boolean): String? {
            val sb = StringBuilder()
            while (true) {
                val b = input.read()
                if (b < 0) {
                    if (firstLine && sb.isEmpty()) return null
                    throw BadRequest()
                }
                if (++headerBytes > MAX_HEADER_BYTES) throw BadRequest()
                if (b == '\n'.code) {
                    if (sb.isNotEmpty() && sb[sb.length - 1] == '\r') sb.setLength(sb.length - 1)
                    if (firstLine && sb.isEmpty()) continue // tolerate a stray CRLF between requests
                    return sb.toString()
                }
                sb.append(b.toChar()) // ISO-8859-1
            }
        }
    }

    /** Writes the response. False if the connection can't be used any more. */
    private fun send(out: OutputStream, request: HttpRequest, response: HttpResponse, keepAlive: Boolean): Boolean {
        val body = response.body
        val noBody = response.status == 304 || response.status == 204 || response.status < 200
        val length = if (noBody) -1 else body?.length ?: 0 // -1: no Content-Length (the body ends with the connection)
        writeHead(out, response, keepAlive, length)
        if (request.method == "HEAD" || noBody || body == null) {
            out.flush()
            return true
        }
        return try {
            body.writeTo(out)
            out.flush()
            true
        } catch (e: ConnectionCut) {
            false
        }
    }

    private fun writeHead(out: OutputStream, response: HttpResponse, keepAlive: Boolean, length: Long) {
        val sb = StringBuilder()
        sb.append("HTTP/1.1 ").append(response.status).append(' ').append(reasonPhrase(response.status)).append("\r\n")
        for ((name, value) in response.headers) {
            if (name.equals("content-length", true) || name.equals("connection", true)) continue
            sb.append(name).append(": ").append(value).append("\r\n")
        }
        if (length >= 0) sb.append("Content-Length: ").append(length).append("\r\n")
        sb.append("Server: LightningLAN\r\n")
        if (!keepAlive) sb.append("Connection: close\r\n")
        sb.append("\r\n")
        out.write(sb.toString().toByteArray(Charsets.ISO_8859_1))
    }
}

fun reasonPhrase(status: Int): String = when (status) {
    200 -> "OK"
    201 -> "Created"
    204 -> "No Content"
    206 -> "Partial Content"
    303 -> "See Other"
    304 -> "Not Modified"
    307 -> "Temporary Redirect"
    400 -> "Bad Request"
    401 -> "Unauthorized"
    403 -> "Forbidden"
    404 -> "Not Found"
    405 -> "Method Not Allowed"
    409 -> "Conflict"
    413 -> "Content Too Large"
    416 -> "Range Not Satisfiable"
    500 -> "Internal Server Error"
    502 -> "Bad Gateway"
    503 -> "Service Unavailable"
    507 -> "Insufficient Storage"
    else -> "Status"
}

fun jsonError(status: Int, error: String, detail: String, reason: String? = null): HttpResponse {
    val payload = kotlinx.serialization.json.buildJsonObject {
        put("error", kotlinx.serialization.json.JsonPrimitive(error))
        put("detail", kotlinx.serialization.json.JsonPrimitive(detail))
        if (reason != null) put("reason", kotlinx.serialization.json.JsonPrimitive(reason))
    }
    return HttpResponse(status, BytesBody(payload.toString().toByteArray(Charsets.UTF_8)))
        .header("Content-Type", "application/json")
}
