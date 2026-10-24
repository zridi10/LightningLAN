package lightninglan.core

/*
 * The NFC tap (Join.kt): the phone with the hotspot acts as an NFC tag holding the join link, the
 * way the app's HostApduService answers a reader (Android's host card emulation). It is an NFC
 * Forum Type 4 Tag: the reader selects the NDEF application, reads the capability container
 * (file E103), then the NDEF file (E104: a 2-byte length and the NDEF message). The other phone
 * reads it like any tag, and Android opens the link in LightningLAN. Read only.
 */

/** The NDEF application's ID, for the HostApduService's AID filter. */
const val NDEF_AID = "D2760000850101"
private val NDEF_AID_BYTES = NDEF_AID.chunked(2).map { it.toInt(16).toByte() }.toByteArray()
private const val CC_FILE = 0xE103
private const val NDEF_FILE = 0xE104
private const val MAX_READ = 0xF0 // the most bytes a reader may ask for at once (MLe)

private val OK = byteArrayOf(0x90.toByte(), 0x00)
private val NOT_FOUND = byteArrayOf(0x6A, 0x82.toByte())
private val WRONG_OFFSET = byteArrayOf(0x6B, 0x00)
private val NOT_ALLOWED = byteArrayOf(0x69, 0x82.toByte()) // writing: the tag is read only
private val NOT_SUPPORTED = byteArrayOf(0x6D, 0x00)

/** An NDEF message of one URI record (as Android's NdefRecord.createUri writes it, for https:// links). */
fun ndefUriMessage(uri: String): ByteArray {
    val (code, rest) = when {
        uri.startsWith("https://") -> 0x04 to uri.removePrefix("https://")
        uri.startsWith("http://") -> 0x03 to uri.removePrefix("http://")
        else -> 0x00 to uri
    }
    val payload = byteArrayOf(code.toByte()) + rest.toByteArray(Charsets.UTF_8)
    val short = payload.size <= 255
    // MB and ME (the only record), SR (a one-byte length) if it fits, TNF 1 (well known), type "U".
    val header = (0x80 or 0x40 or (if (short) 0x10 else 0) or 0x01).toByte()
    val length = if (short) byteArrayOf(payload.size.toByte())
    else byteArrayOf((payload.size ushr 24).toByte(), (payload.size ushr 16).toByte(), (payload.size ushr 8).toByte(), payload.size.toByte())
    return byteArrayOf(header, 1) + length + 'U'.code.toByte() + payload
}

/** Answers a reader's commands (APDUs); [message] is the NDEF message to hand out, or null for none right now. */
class NdefTag(private val message: () -> ByteArray?) {
    private var ndefFile: ByteArray? = null // fixed when the reader selects the application, so one read sees one message
    private var selected: ByteArray? = null

    fun process(apdu: ByteArray): ByteArray {
        if (apdu.size < 4) return NOT_SUPPORTED
        val ins = apdu[1].toInt() and 0xFF
        val p1 = apdu[2].toInt() and 0xFF
        val p2 = apdu[3].toInt() and 0xFF
        return when (ins) {
            0xA4 -> select(apdu, p1)
            0xB0 -> read(apdu, (p1 shl 8) or p2)
            0xD6 -> NOT_ALLOWED
            else -> NOT_SUPPORTED
        }
    }

    /** The reader went away (or picked another application). */
    fun reset() {
        ndefFile = null
        selected = null
    }

    private fun select(apdu: ByteArray, p1: Int): ByteArray {
        val lc = if (apdu.size > 4) apdu[4].toInt() and 0xFF else 0
        if (apdu.size < 5 + lc) return NOT_FOUND
        val data = apdu.copyOfRange(5, 5 + lc)
        if (p1 == 0x04) { // by name: the NDEF application
            if (!data.contentEquals(NDEF_AID_BYTES)) return NOT_FOUND
            val m = message() ?: return NOT_FOUND
            ndefFile = byteArrayOf((m.size ushr 8).toByte(), m.size.toByte()) + m
            selected = null
            return OK
        }
        val file = ndefFile ?: return NOT_FOUND
        if (p1 != 0x00 || data.size != 2) return NOT_FOUND
        selected = when (((data[0].toInt() and 0xFF) shl 8) or (data[1].toInt() and 0xFF)) {
            CC_FILE -> capabilityContainer(file.size)
            NDEF_FILE -> file
            else -> return NOT_FOUND
        }
        return OK
    }

    private fun read(apdu: ByteArray, offset: Int): ByteArray {
        val file = selected ?: return NOT_FOUND
        if (offset > file.size) return WRONG_OFFSET
        val le = if (apdu.size > 4) apdu[apdu.size - 1].toInt() and 0xFF else 0
        val count = minOf(if (le == 0) 256 else le, file.size - offset)
        return file.copyOfRange(offset, offset + count) + OK
    }

    /** Mapping version 2.0; one NDEF file, readable by anyone, never writable. */
    private fun capabilityContainer(ndefSize: Int): ByteArray = byteArrayOf(
        0x00, 0x0F, // its length
        0x20, // mapping version 2.0
        0x00, MAX_READ.toByte(), // MLe
        0x00, MAX_READ.toByte(), // MLc
        0x04, 0x06, // the NDEF file control TLV:
        (NDEF_FILE ushr 8).toByte(), NDEF_FILE.toByte(),
        (ndefSize ushr 8).toByte(), ndefSize.toByte(), // its size
        0x00, // read: anyone
        0xFF.toByte(), // write: nobody
    )
}
