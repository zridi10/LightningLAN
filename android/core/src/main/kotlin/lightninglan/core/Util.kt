package lightninglan.core

import java.io.IOException
import java.net.ConnectException
import java.net.NoRouteToHostException
import java.net.SocketTimeoutException
import java.security.MessageDigest
import java.util.Locale

const val SERVICE_TYPE = "_lightninglan._tcp"

internal val SHA256_RE = Regex("^[0-9a-f]{64}$")

private val HEX = "0123456789abcdef".toCharArray()

fun ByteArray.toHex(): String {
    val out = CharArray(size * 2)
    for (i in indices) {
        val b = this[i].toInt() and 0xff
        out[2 * i] = HEX[b ushr 4]
        out[2 * i + 1] = HEX[b and 0x0f]
    }
    return String(out)
}

fun sha256Hex(data: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(data).toHex()

private val secureRandom = java.security.SecureRandom()

/** [bytes] random bytes as URL-safe base64 without padding (Python's secrets.token_urlsafe). */
fun randomToken(bytes: Int): String =
    java.util.Base64.getUrlEncoder().withoutPadding().encodeToString(ByteArray(bytes).also(secureRandom::nextBytes))

/** Milliseconds from a clock that never jumps (for intervals and deadlines). */
fun monotonicMs(): Long = System.nanoTime() / 1_000_000

fun formatSize(bytes: Long): String {
    var n = bytes.toDouble()
    for (unit in listOf("B", "KB", "MB", "GB")) {
        if (kotlin.math.abs(n) < 1024) {
            return if (unit == "B") String.format(Locale.US, "%.0f %s", n, unit) else String.format(Locale.US, "%.1f %s", n, unit)
        }
        n /= 1024
    }
    return String.format(Locale.US, "%.1f TB", n)
}

/** A short description of a network error, as shown to the user. */
fun describeNetworkError(error: Throwable): String = when (error) {
    is ConnectException, is NoRouteToHostException -> "connection refused or host unreachable"
    is SocketTimeoutException -> "timed out"
    is IOException -> error.message ?: error.javaClass.simpleName
    else -> error.message ?: error.javaClass.simpleName
}

/** Where the core's log lines go. The app points this at Logcat. */
object CoreLog {
    enum class Level { DEBUG, INFO, WARN, ERROR }

    @Volatile
    var sink: (Level, String, String, Throwable?) -> Unit = { level, tag, message, error ->
        if (level >= Level.INFO) {
            System.err.println("${level.name} $tag: $message")
            error?.printStackTrace()
        }
    }

    fun d(tag: String, message: String) = sink(Level.DEBUG, tag, message, null)
    fun i(tag: String, message: String) = sink(Level.INFO, tag, message, null)
    fun w(tag: String, message: String, error: Throwable? = null) = sink(Level.WARN, tag, message, error)
    fun e(tag: String, message: String, error: Throwable? = null) = sink(Level.ERROR, tag, message, error)
}

/** A flag one thread raises to stop another (like Python's threading.Event, with a reason). */
class StopSignal {
    @Volatile
    var reason: String? = null
    private val latch = java.util.concurrent.CountDownLatch(1)

    val isSet: Boolean get() = latch.count == 0L

    fun set(reason: String? = null) {
        if (reason != null) this.reason = reason
        latch.countDown()
    }

    /** Waits up to [ms] milliseconds; true if the signal was raised. */
    fun await(ms: Long): Boolean = latch.await(ms, java.util.concurrent.TimeUnit.MILLISECONDS)
}
