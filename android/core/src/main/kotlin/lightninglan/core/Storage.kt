package lightninglan.core

import java.io.Closeable
import java.io.IOException
import java.nio.ByteBuffer

/*
 * Where files live. The desktop app uses plain paths; on Android the shared and downloads folders
 * are reached through the Storage Access Framework. The core only sees these interfaces.
 */

/** A file opened for reading at any position. */
interface ReadableFile : Closeable {
    /** Size and modification time when it was opened (the time may only be accurate to the second). */
    val size: Long
    val mtimeMs: Long

    /** Reads into [buf] from [position]; returns the number of bytes read, or -1 at the end. */
    fun read(buf: ByteBuffer, position: Long): Int
}

/** One entry of a shared folder listing. [key] identifies it to [ShareTree.open] and [ShareTree.list]. */
class TreeEntry(val name: String, val key: String, val isDirectory: Boolean, val size: Long, val mtimeMs: Long)

/** The shared folder (and anything else this peer shares). */
interface ShareTree {
    /** Tells trees apart; part of the hash cache keys. */
    val id: String

    /** The entries of a directory (null: the top level), or null if it can't be listed. */
    fun list(directoryKey: String?): List<TreeEntry>?

    fun open(key: String): ReadableFile?
}

/** A download in progress, written piece by piece in any order. */
interface PartialFile : Closeable {
    fun size(): Long
    fun read(buf: ByteBuffer, position: Long): Int
    fun write(buf: ByteBuffer, position: Long): Int
    fun truncate(size: Long)
}

/** A finished download. [location] is what [DownloadFolder.openSaved] takes; [label] is shown to people. */
class SavedFile(val location: String, val label: String, val size: Long, val mtimeMs: Long)

/** The downloads folder, with its hidden folder of partial downloads. */
interface DownloadFolder {
    val id: String
    val label: String

    /** Size of the partial download of this file, or null if there is none. */
    fun partialSize(fileId: String): Long?

    /** Opens the partial download for reading and writing, creating an empty one if needed. */
    @Throws(IOException::class)
    fun openPartial(fileId: String): PartialFile

    fun openPartialForReading(fileId: String): ReadableFile?

    fun deletePartial(fileId: String)

    /** Moves the finished partial download into the folder as [fileName], or "name (1).ext" and so on. Never replaces a file. */
    @Throws(IOException::class)
    fun finish(fileId: String, fileName: String): SavedFile

    fun openSaved(location: String): ReadableFile?

    /** Bytes free where downloads are written, or null if unknown. */
    fun freeSpace(): Long?
}

/** Writing to this peer's own storage failed: no other peer can fix that. */
class LocalStorageError(val error: IOException) : IOException(error.message, error)

/** Runs a local storage operation, marking its I/O errors as local (network errors are handled differently). */
inline fun <T> local(block: () -> T): T = try {
    block()
} catch (e: LocalStorageError) {
    throw e
} catch (e: IOException) {
    throw LocalStorageError(e)
}

fun ReadableFile.readFully(buf: ByteBuffer, position: Long): Int {
    var total = 0
    while (buf.hasRemaining()) {
        val n = read(buf, position + total)
        if (n <= 0) break
        total += n
    }
    return total
}
