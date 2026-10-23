package lightninglan.core

import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/*
 * Optional passphrase authentication between peers (the same scheme as lightninglan/auth.py).
 *
 * When a passphrase is set, every peer-to-peer request carries an HMAC token bound to the
 * method, path, a timestamp and a single-use nonce. The server answers with a proof (an HMAC of
 * that token), so the client also learns whether the server knows the passphrase. The passphrase
 * itself never crosses the network.
 */

const val TOKEN_HEADER = "X-LightningLAN-Auth"
const val PROOF_HEADER = "X-LightningLAN-Proof"
private const val MAX_CLOCK_SKEW = 300L // seconds
private const val PRUNE_INTERVAL = 30L
private val KDF_SALT = "lightninglan/v1/peer-auth".toByteArray(Charsets.UTF_8)
private const val KDF_ITERATIONS = 200_000
private val TOKEN_RE = Regex("^v1\\.([0-9a-f]{32})\\.(\\d{1,12})\\.([0-9a-f]{24})\\.([0-9a-f]{64})$")

/** Reasons reported back to a rejected client and shown in its UI. */
val AUTH_REASONS = mapOf(
    "missing" to "this peer requires a passphrase",
    "malformed" to "invalid authentication token",
    "bad_signature" to "passphrase does not match",
    "clock_skew" to "the two computers' clocks differ by more than 5 minutes",
    "replay" to "authentication token was reused",
    "no_proof" to "peer does not use the same passphrase",
)

class AuthError(val reason: String) : Exception(AUTH_REASONS[reason] ?: reason)

/** PBKDF2-HMAC-SHA256 of the passphrase (UTF-8), as Python's hashlib.pbkdf2_hmac computes it. Slow on purpose. */
fun deriveKey(passphrase: String): ByteArray {
    require(passphrase.isNotEmpty()) { "empty passphrase" }
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(passphrase.toByteArray(Charsets.UTF_8), "HmacSHA256"))
    mac.update(KDF_SALT)
    mac.update(byteArrayOf(0, 0, 0, 1)) // one 32-byte block is the whole key
    var u = mac.doFinal()
    val key = u.copyOf()
    repeat(KDF_ITERATIONS - 1) {
        u = mac.doFinal(u)
        for (i in key.indices) key[i] = (key[i].toInt() xor u[i].toInt()).toByte()
    }
    return key
}

class Authenticator(key: ByteArray?, val peerId: String) {
    @Volatile
    var enabled: Boolean = key != null && key.isNotEmpty()
        private set

    @Volatile
    private var key: ByteArray = key ?: ByteArray(0)
    private val seen = HashMap<String, Long>() // nonce -> when it can be forgotten (epoch seconds)
    private var nextPrune = 0L
    private val random = SecureRandom()

    /** Switch to another passphrase's key (from [deriveKey]), or turn auth off with null. */
    fun setKey(key: ByteArray?) {
        if (key != null && key.isNotEmpty()) {
            this.key = key
            enabled = true
        } else {
            enabled = false
            this.key = ByteArray(0)
        }
    }

    private fun mac(vararg fields: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(key.takeIf { it.isNotEmpty() } ?: ByteArray(1), "HmacSHA256"))
        return mac.doFinal(fields.joinToString("\n").toByteArray(Charsets.UTF_8)).toHex()
    }

    // ----- client side ----------------------------------------------------------------------

    /** Headers to send with a request to another peer (empty when auth is off). */
    fun signRequest(method: String, path: String): Map<String, String> {
        if (!enabled) return emptyMap()
        val ts = (System.currentTimeMillis() / 1000).toString()
        val nonceBytes = ByteArray(12)
        random.nextBytes(nonceBytes)
        val nonce = nonceBytes.toHex()
        val sig = mac("req", method.uppercase(), path, ts, nonce, peerId)
        return mapOf(TOKEN_HEADER to "v1.$peerId.$ts.$nonce.$sig")
    }

    /** True if the responding peer proved it knows the passphrase (always true when off). */
    fun verifyResponse(token: String?, proof: String?): Boolean {
        if (!enabled) return true
        if (token.isNullOrEmpty() || proof.isNullOrEmpty()) return false
        return constantTimeEquals(proofFor(token), proof)
    }

    // ----- server side ----------------------------------------------------------------------

    /** Check a request token. Returns the caller's peer ID or throws [AuthError]. */
    fun verifyRequest(method: String, path: String, token: String?): String {
        if (token.isNullOrEmpty()) throw AuthError("missing")
        val m = TOKEN_RE.matchEntire(token) ?: throw AuthError("malformed")
        val (callerId, ts, nonce, sig) = m.destructured
        val expected = mac("req", method.uppercase(), path, ts, nonce, callerId)
        if (!constantTimeEquals(expected, sig)) throw AuthError("bad_signature")
        val now = System.currentTimeMillis() / 1000
        if (kotlin.math.abs(now - ts.toLong()) > MAX_CLOCK_SKEW) throw AuthError("clock_skew")
        synchronized(seen) {
            if (nonce in seen) throw AuthError("replay")
            if (now >= nextPrune) { // swarms send many requests: prune by time, not on every call
                seen.entries.removeAll { it.value <= now }
                nextPrune = now + PRUNE_INTERVAL
            }
            seen[nonce] = now + 2 * MAX_CLOCK_SKEW
        }
        return callerId
    }

    fun proofFor(token: String): String = mac("resp", token)

    private fun constantTimeEquals(a: String, b: String): Boolean =
        MessageDigest.isEqual(a.toByteArray(Charsets.UTF_8), b.toByteArray(Charsets.UTF_8))
}
