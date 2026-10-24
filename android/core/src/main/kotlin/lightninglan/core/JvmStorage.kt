package lightninglan.core

import java.io.File
import java.io.IOException
import java.io.InputStream
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.channels.FileChannel
import java.nio.file.FileAlreadyExistsException
import java.nio.file.Files

/* Storage on plain file paths: the Android app's own folders (Download/LightningLAN), and the JVM tests. */

const val PARTIAL_DIR_NAME = ".lightninglan-partial"

class PathReadable(file: File) : ReadableFile {
    private val raf = RandomAccessFile(file, "r")
    private val channel: FileChannel = raf.channel
    override val size: Long = channel.size()
    override val mtimeMs: Long = file.lastModified()

    override fun read(buf: ByteBuffer, position: Long): Int = channel.read(buf, position)

    override fun close() = raf.close()
}

class PathShareTree(private val root: File) : ShareTree, ShareInbox {
    override val id: String = root.absolutePath

    override fun list(directoryKey: String?): List<TreeEntry>? {
        val dir = directoryKey?.let(::File) ?: root
        val children = dir.listFiles() ?: return null
        return children.filter { !Files.isSymbolicLink(it.toPath()) }.map {
            TreeEntry(it.name, it.absolutePath, it.isDirectory, if (it.isDirectory) 0 else it.length(), it.lastModified())
        }
    }

    override fun open(key: String): ReadableFile? = File(key).takeIf { it.isFile }?.let(::PathReadable)

    override fun receive(fileName: String, length: Long, mtimeMs: Long?, input: InputStream): ReceivedFile =
        receiveInto(root, fileName, length, mtimeMs, input)

    override fun inboxFreeSpace(): Long? = root.usableSpace.takeIf { it > 0 }
}

/** Saves a file sent from the phone page in [dir]: under a hidden name until it is all there, then moved into place. */
fun receiveInto(dir: File, fileName: String, length: Long, mtimeMs: Long?, input: InputStream): ReceivedFile {
    if (!dir.isDirectory && !dir.mkdirs()) throw IOException("could not make the folder $dir")
    val temp = File(dir, UPLOAD_TEMP_PREFIX + randomToken(12) + ".tmp")
    try {
        temp.outputStream().use { copyExactly(input, it, length) }
        if (mtimeMs != null) temp.setLastModified(mtimeMs)
        for (name in numberedNames(fileName).take(10_000)) {
            try {
                Files.move(temp.toPath(), File(dir, name).toPath())
            } catch (e: FileAlreadyExistsException) {
                continue
            }
            return ReceivedFile(name, length, name != fileName)
        }
        throw IOException("could not find a free name for $fileName in $dir")
    } finally {
        temp.delete() // only still there if it failed
    }
}

class PathPartial(file: File) : PartialFile {
    private val raf = RandomAccessFile(file, "rw")
    private val channel: FileChannel = raf.channel

    override fun size(): Long = channel.size()
    override fun read(buf: ByteBuffer, position: Long): Int = channel.read(buf, position)
    override fun write(buf: ByteBuffer, position: Long): Int = channel.write(buf, position)
    override fun truncate(size: Long) {
        channel.truncate(size)
    }

    override fun close() = raf.close()
}

/** [label]: how the folder is shown (its path by default). */
class PathDownloadFolder(private val dir: File, override val label: String = dir.absolutePath) : DownloadFolder {
    override val id: String = dir.absolutePath
    private val partialDir = File(dir, PARTIAL_DIR_NAME)

    private fun partial(fileId: String) = File(partialDir, "$fileId.part")

    override fun partialSize(fileId: String): Long? = partial(fileId).takeIf { it.isFile }?.length()

    override fun openPartial(fileId: String): PartialFile {
        partialDir.mkdirs()
        return PathPartial(partial(fileId))
    }

    override fun openPartialForReading(fileId: String): ReadableFile? =
        partial(fileId).takeIf { it.isFile }?.let { runCatching { PathReadable(it) }.getOrNull() }

    override fun deletePartial(fileId: String) {
        partial(fileId).delete()
    }

    override fun finish(fileId: String, fileName: String): SavedFile {
        val source = partial(fileId)
        for (name in numberedNames(fileName).take(10_000)) {
            val target = File(dir, name)
            try {
                Files.move(source.toPath(), target.toPath())
            } catch (e: FileAlreadyExistsException) {
                continue
            }
            val shown = if (label == dir.absolutePath) target.absolutePath else "$label/${target.name}"
            return SavedFile(target.absolutePath, shown, target.length(), target.lastModified())
        }
        throw IOException("could not find a free name for $fileName in $dir")
    }

    override fun openSaved(location: String): ReadableFile? =
        File(location).takeIf { it.isFile }?.let { runCatching { PathReadable(it) }.getOrNull() }

    override fun freeSpace(): Long? = dir.usableSpace.takeIf { it > 0 }
}
