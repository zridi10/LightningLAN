package lightninglan.core

import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import java.nio.ByteBuffer
import java.security.MessageDigest
import java.util.Base64

/*
 * How files are split into pieces for swarm downloads (the same rules as lightninglan/pieces.py).
 *
 * A file larger than one piece is split into equal pieces (the last one may be shorter). The piece
 * size depends only on the file size, so every peer computes the same pieces for the same file.
 */

const val MIN_PIECE_SIZE = 1024 * 1024
const val MAX_PIECE_SIZE = 16 * 1024 * 1024
const val TARGET_PIECES = 2048
const val DIGEST_SIZE = 32
const val HASH_CHUNK = 1024 * 1024 // divides every piece size

/** A power of two from 1 MiB to 16 MiB, chosen so a file has at most ~2048 pieces. */
fun pieceSizeFor(size: Long): Int {
    var piece = MIN_PIECE_SIZE.toLong()
    while (piece < MAX_PIECE_SIZE && size > piece * TARGET_PIECES) piece *= 2
    return piece.toInt()
}

fun pieceCount(size: Long): Int {
    val piece = pieceSizeFor(size)
    return ((size + piece - 1) / piece).toInt()
}

/** Files of more than one piece are split up; smaller ones are always sent whole. */
fun swarmable(size: Long): Boolean = size > MIN_PIECE_SIZE

class FileHashes(val sha256: String, val pieces: ByteArray)

class HashCancelled : Exception("hashing cancelled")

/**
 * SHA-256 of the whole file and, for files of more than one piece, of each piece (one pass).
 * Throws [HashCancelled] when [cancelled] turns true.
 */
fun hashFile(file: ReadableFile, cancelled: () -> Boolean = { false }): FileHashes {
    val whole = MessageDigest.getInstance("SHA-256")
    val digests = java.io.ByteArrayOutputStream()
    val size = file.size
    val split = swarmable(size)
    val pieceSize = pieceSizeFor(size)
    var piece = MessageDigest.getInstance("SHA-256")
    var inPiece = 0
    val buf = ByteBuffer.allocate(HASH_CHUNK)
    var position = 0L
    while (true) {
        if (cancelled()) throw HashCancelled()
        buf.clear()
        val n = file.read(buf, position)
        if (n <= 0) break
        position += n
        val array = buf.array()
        whole.update(array, 0, n)
        if (!split) continue
        var offset = 0
        while (offset < n) {
            val take = minOf(n - offset, pieceSize - inPiece)
            piece.update(array, offset, take)
            inPiece += take
            offset += take
            if (inPiece == pieceSize) {
                digests.write(piece.digest())
                piece = MessageDigest.getInstance("SHA-256")
                inPiece = 0
            }
        }
    }
    if (split && inPiece > 0) digests.write(piece.digest())
    return FileHashes(whole.digest().toHex(), digests.toByteArray())
}

/** A file's piece hashes: what a downloader checks every piece against. */
class PieceInfo(val fileId: String, val size: Long, val hashes: ByteArray) {
    val pieceSize: Int get() = pieceSizeFor(size)
    val count: Int get() = hashes.size / DIGEST_SIZE

    fun digest(index: Int): ByteArray = hashes.copyOfRange(index * DIGEST_SIZE, (index + 1) * DIGEST_SIZE)

    /** Offset of a piece in the file. */
    fun start(index: Int): Long = index.toLong() * pieceSize

    fun length(index: Int): Int = minOf(pieceSize.toLong(), size - start(index)).toInt()

    fun toJson(): JsonObject = buildJsonObject {
        put("file_id", fileId)
        put("size", size)
        put("piece_size", pieceSize)
        put("hashes", Base64.getEncoder().encodeToString(hashes))
    }

    override fun equals(other: Any?): Boolean =
        other is PieceInfo && other.fileId == fileId && other.size == size && other.hashes.contentEquals(hashes)

    override fun hashCode(): Int = fileId.hashCode() * 31 + hashes.contentHashCode()

    companion object {
        /** Validate another peer's piece list for the file we expect. Throws IllegalArgumentException. */
        fun fromJson(data: JsonElement, fileId: String, size: Long): PieceInfo {
            require(data is JsonObject) { "piece list is not a JSON object" }
            val id = (data["file_id"] as? JsonPrimitive)?.takeIf { it.isString }?.content
            val reportedSize = (data["size"] as? JsonPrimitive)?.takeIf { !it.isString }?.longOrNull
            require(id == fileId && reportedSize == size) { "piece list is for a different file" }
            val pieceSize = (data["piece_size"] as? JsonPrimitive)?.takeIf { !it.isString }?.intOrNull
            require(pieceSize == pieceSizeFor(size)) { "unexpected piece size" }
            val raw = (data["hashes"] as? JsonPrimitive)?.takeIf { it.isString }?.content
                ?: throw IllegalArgumentException("piece list has no hashes")
            val hashes = Base64.getDecoder().decode(raw)
            require(hashes.size == pieceCount(size) * DIGEST_SIZE) { "wrong number of piece hashes" }
            return PieceInfo(fileId, size, hashes)
        }
    }
}

/** A have-map (one 0/1 entry per piece) as base64 of the packed bits, first piece in the high bit. */
fun encodeBits(have: ByteArray): String {
    val packed = ByteArray((have.size + 7) / 8)
    for (i in have.indices) {
        if (have[i].toInt() != 0) packed[i shr 3] = (packed[i shr 3].toInt() or (0x80 ushr (i and 7))).toByte()
    }
    return Base64.getEncoder().encodeToString(packed)
}

/** The inverse of [encodeBits]. Throws IllegalArgumentException if it doesn't describe [count] pieces. */
fun decodeBits(text: String, count: Int): ByteArray {
    val packed = Base64.getDecoder().decode(text)
    require(packed.size == (count + 7) / 8) { "have-map has the wrong length" }
    return ByteArray(count) { i -> if (packed[i shr 3].toInt() and (0x80 ushr (i and 7)) != 0) 1 else 0 }
}
