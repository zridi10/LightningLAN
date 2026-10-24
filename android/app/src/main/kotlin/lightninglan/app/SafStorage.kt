package lightninglan.app

import android.content.ContentResolver
import android.content.Context
import android.net.Uri
import android.os.ParcelFileDescriptor
import android.os.StatFs
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import android.provider.OpenableColumns
import android.system.ErrnoException
import android.system.Os
import lightninglan.core.DownloadFolder
import lightninglan.core.PARTIAL_DIR_NAME
import lightninglan.core.PartialFile
import lightninglan.core.PathReadable
import lightninglan.core.ReceivedFile
import lightninglan.core.ReadableFile
import lightninglan.core.SavedFile
import lightninglan.core.ShareInbox
import lightninglan.core.ShareTree
import lightninglan.core.TreeEntry
import lightninglan.core.UPLOAD_TEMP_PREFIX
import lightninglan.core.copyExactly
import lightninglan.core.numberedNames
import lightninglan.core.randomToken
import lightninglan.core.receiveInto
import java.io.File
import java.io.FileNotFoundException
import java.io.IOException
import java.io.InputStream
import java.nio.ByteBuffer
import java.nio.file.Files

/*
 * The shared and downloads folders through the Storage Access Framework: the person picks a
 * folder in Settings once and the app keeps access to it. A folder is a document URI inside that
 * grant. The folders the app makes by itself are plain paths instead (LocalFolders.kt).
 */

private val CHILD_COLUMNS = arrayOf(
    Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE,
    Document.COLUMN_SIZE, Document.COLUMN_LAST_MODIFIED,
)

private inline fun <T> errno(block: () -> T): T = try {
    block()
} catch (e: ErrnoException) {
    throw IOException(e.message, e)
}

class SafReadable(private val pfd: ParcelFileDescriptor) : ReadableFile {
    override val size: Long
    override val mtimeMs: Long

    init {
        val st = try {
            Os.fstat(pfd.fileDescriptor)
        } catch (e: ErrnoException) {
            pfd.close()
            throw IOException(e.message, e)
        }
        size = st.st_size
        mtimeMs = st.st_mtime * 1000
    }

    override fun read(buf: ByteBuffer, position: Long): Int = errno { Os.pread(pfd.fileDescriptor, buf, position) }

    override fun close() = pfd.close()
}

class SafPartial(private val pfd: ParcelFileDescriptor) : PartialFile {
    override fun size(): Long = errno { Os.fstat(pfd.fileDescriptor).st_size }
    override fun read(buf: ByteBuffer, position: Long): Int = errno { Os.pread(pfd.fileDescriptor, buf, position) }
    override fun write(buf: ByteBuffer, position: Long): Int = errno { Os.pwrite(pfd.fileDescriptor, buf, position) }
    override fun truncate(size: Long) = errno { Os.ftruncate(pfd.fileDescriptor, size) }
    override fun close() = pfd.close()
}

/** A child of a folder: its document id, name and type. */
private class Child(val id: String, val name: String, val mime: String?, val size: Long, val mtimeMs: Long) {
    val isDirectory: Boolean get() = mime == Document.MIME_TYPE_DIR
}

private fun ContentResolver.children(folder: Uri, documentId: String): List<Child>? {
    val uri = DocumentsContract.buildChildDocumentsUriUsingTree(folder, documentId)
    return query(uri, CHILD_COLUMNS, null, null, null)?.use { c ->
        val out = ArrayList<Child>(c.count)
        while (c.moveToNext()) {
            out.add(
                Child(
                    c.getString(0) ?: continue, c.getString(1) ?: continue, c.getString(2),
                    if (c.isNull(3)) 0 else c.getLong(3), if (c.isNull(4)) 0 else c.getLong(4),
                ),
            )
        }
        out
    }
}

/** A name to show for a folder, like "Download/LightningLAN/Shared". */
fun folderLabel(folder: Uri): String {
    folder.localFolder()?.let { return localFolderLabel(it) }
    val id = try {
        DocumentsContract.getDocumentId(folder)
    } catch (e: IllegalArgumentException) {
        return folder.toString()
    }
    val colon = id.indexOf(':')
    if (colon < 0) return id
    val volume = id.substring(0, colon)
    val path = id.substring(colon + 1)
    return when {
        volume == "primary" -> path.ifEmpty { "Phone storage" }
        path.isEmpty() -> "SD card"
        else -> "SD card/$path"
    }
}

/** A folder's document URI inside the tree the person picked (the tree's own folder). */
fun treeFolder(tree: Uri): Uri = DocumentsContract.buildDocumentUriUsingTree(tree, DocumentsContract.getTreeDocumentId(tree))

/**
 * Shares a folder (a picked one, or one of the app's own on a plain path) and, at its top level,
 * the files picked one by one ([links]). Files sent from the phone page go into the folder.
 */
class PhoneShareTree(private val context: Context, private val folder: Uri?, private val links: List<Uri>) : ShareTree, ShareInbox {
    private val resolver = context.contentResolver
    private val dir: File? = folder?.localFolder()
    override val id: String = "saf:" + (folder?.toString() ?: "")

    override fun receive(fileName: String, length: Long, mtimeMs: Long?, input: InputStream): ReceivedFile {
        dir?.let { return receiveInto(it, fileName, length, mtimeMs, input) }
        val parent = folder ?: throw IOException("there is no shared folder")
        // Under a hidden name until it is all there (the index skips it), then renamed: the provider
        // picks "name (1).ext" if the name is taken. A picked folder can't keep the file's date.
        val temp = DocumentsContract.createDocument(resolver, parent, "application/octet-stream", UPLOAD_TEMP_PREFIX + randomToken(12) + ".tmp")
            ?: throw IOException("could not make a file in the shared folder")
        try {
            (resolver.openOutputStream(temp, "w") ?: throw IOException("could not write to the shared folder")).use { copyExactly(input, it, length) }
            val saved = DocumentsContract.renameDocument(resolver, temp, fileName) ?: temp
            val name = displayName(saved) ?: fileName
            return ReceivedFile(name, length, name != fileName)
        } catch (e: Exception) {
            runCatching { DocumentsContract.deleteDocument(resolver, temp) }
            throw e as? IOException ?: IOException(e.message, e)
        }
    }

    override fun inboxFreeSpace(): Long? = dir?.usableSpace?.takeIf { it > 0 }

    private fun displayName(document: Uri): String? = try {
        resolver.query(document, arrayOf(Document.COLUMN_DISPLAY_NAME), null, null, null)?.use { c -> if (c.moveToFirst()) c.getString(0) else null }
    } catch (e: Exception) {
        null
    }

    override fun list(directoryKey: String?): List<TreeEntry>? {
        if (directoryKey != null) {
            return if (directoryKey.startsWith("file:")) listDir(File(directoryKey.removePrefix("file:")))
            else listFolder(directoryKey.removePrefix("doc:"))
        }
        val top = when {
            dir != null -> listDir(dir)
            folder != null -> listFolder(DocumentsContract.getDocumentId(folder))
            else -> null
        } ?: emptyList()
        val taken = top.map { it.name.lowercase() }.toMutableSet()
        return top + links.mapNotNull { linkEntry(it, taken) }
    }

    private fun listFolder(documentId: String): List<TreeEntry>? {
        val f = folder ?: return null
        return resolver.children(f, documentId)?.map { TreeEntry(it.name, "doc:" + it.id, it.isDirectory, it.size, it.mtimeMs) }
    }

    private fun listDir(d: File): List<TreeEntry>? = d.listFiles()?.filter { !Files.isSymbolicLink(it.toPath()) }?.map {
        TreeEntry(it.name, "file:" + it.absolutePath, it.isDirectory, if (it.isDirectory) 0 else it.length(), it.lastModified())
    }

    private fun linkEntry(uri: Uri, taken: MutableSet<String>): TreeEntry? {
        val name = try {
            resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
                if (c.moveToFirst()) c.getString(0) else null
            }
        } catch (e: Exception) {
            null
        } ?: return null
        // Size and time from the file itself: not every app's documents report them.
        val (size, mtime) = try {
            open("link:$uri")?.use { it.size to it.mtimeMs }
        } catch (e: Exception) {
            null
        } ?: return null
        val unique = numberedNames(name).first { it.lowercase() !in taken }
        taken.add(unique.lowercase())
        return TreeEntry(unique, "link:$uri", false, size, mtime)
    }

    override fun open(key: String): ReadableFile? {
        if (key.startsWith("file:")) {
            val file = File(key.removePrefix("file:"))
            return if (file.isFile) runCatching { PathReadable(file) }.getOrNull() else null
        }
        val uri = when {
            key.startsWith("doc:") -> DocumentsContract.buildDocumentUriUsingTree(folder ?: return null, key.removePrefix("doc:"))
            key.startsWith("link:") -> Uri.parse(key.removePrefix("link:"))
            else -> return null
        }
        val pfd = try {
            resolver.openFileDescriptor(uri, "r")
        } catch (e: FileNotFoundException) {
            null
        } catch (e: SecurityException) {
            null
        } ?: return null
        return SafReadable(pfd)
    }

    /** The document URI of a shared file, to open it in another app. */
    fun uriFor(key: String): Uri? = when {
        key.startsWith("file:") -> shareableUri(context, File(key.removePrefix("file:")))
        key.startsWith("doc:") -> folder?.let { DocumentsContract.buildDocumentUriUsingTree(it, key.removePrefix("doc:")) }
        key.startsWith("link:") -> Uri.parse(key.removePrefix("link:"))
        else -> null
    }
}

/** The downloads folder, with the partial downloads in its hidden .lightninglan-partial folder. */
class SafDownloadFolder(private val context: Context, private val folder: Uri) : DownloadFolder {
    private val resolver = context.contentResolver
    override val id: String = folder.toString()
    override val label: String = folderLabel(folder)
    private val rootId = DocumentsContract.getDocumentId(folder)
    private val lock = Any()
    private var partialDirId: String? = null

    private fun docUri(documentId: String): Uri = DocumentsContract.buildDocumentUriUsingTree(folder, documentId)

    private fun partialDir(create: Boolean): String? = synchronized(lock) {
        partialDirId?.let { return it }
        val found = resolver.children(folder, rootId)?.firstOrNull { it.name == PARTIAL_DIR_NAME && it.isDirectory }?.id
            ?: if (create) {
                DocumentsContract.createDocument(resolver, folder, Document.MIME_TYPE_DIR, PARTIAL_DIR_NAME)
                    ?.let(DocumentsContract::getDocumentId) ?: throw IOException("could not create the partial downloads folder")
            } else {
                null
            }
        partialDirId = found
        found
    }

    private fun partialDoc(fileId: String, create: Boolean): Uri? = synchronized(lock) {
        val name = "$fileId.part"
        repeat(2) { // a second time if the partial folder went away since it was looked up
            val dir = try {
                partialDir(create)
            } catch (e: IOException) {
                throw e
            } catch (e: Exception) {
                partialDirId = null
                throw IOException(e.message, e)
            } ?: return null
            val existing = resolver.children(folder, dir)
            if (existing == null) {
                partialDirId = null
                return@repeat
            }
            existing.firstOrNull { it.name == name }?.let { return docUri(it.id) }
            if (!create) return null
            return DocumentsContract.createDocument(resolver, docUri(dir), "application/octet-stream", name)
                ?: throw IOException("could not create the partial download")
        }
        if (create) throw IOException("could not open the partial downloads folder") else null
    }

    override fun partialSize(fileId: String): Long? = partialDoc(fileId, false)?.let { uri ->
        resolver.openFileDescriptor(uri, "r")?.use { it.statSize }
    }

    override fun openPartial(fileId: String): PartialFile {
        val uri = partialDoc(fileId, true)!!
        val pfd = resolver.openFileDescriptor(uri, "rw") ?: throw IOException("could not open the partial download")
        return SafPartial(pfd)
    }

    override fun openPartialForReading(fileId: String): ReadableFile? = try {
        partialDoc(fileId, false)?.let { uri -> resolver.openFileDescriptor(uri, "r")?.let(::SafReadable) }
    } catch (e: Exception) {
        null
    }

    override fun deletePartial(fileId: String) {
        try {
            partialDoc(fileId, false)?.let { DocumentsContract.deleteDocument(resolver, it) }
        } catch (e: Exception) {
            lightninglan.core.CoreLog.w("storage", "could not delete a partial download: $e")
        }
    }

    override fun finish(fileId: String, fileName: String): SavedFile {
        val part = partialDoc(fileId, false) ?: throw IOException("the partial download is missing")
        val taken = (resolver.children(folder, rootId) ?: throw IOException("the downloads folder is gone"))
            .map { it.name.lowercase() }.toSet()
        val name = numberedNames(fileName).take(10_000).firstOrNull { it.lowercase() !in taken }
            ?: throw IOException("could not find a free name for $fileName")
        val saved = try {
            // Rename and move: no copying, even for big files.
            val renamed = DocumentsContract.renameDocument(resolver, part, name) ?: part
            DocumentsContract.moveDocument(resolver, renamed, docUri(partialDir(false)!!), folder)
                ?: throw IOException("move failed")
        } catch (e: Exception) {
            copyThenDelete(partialDoc(fileId, false) ?: findRenamed(name) ?: throw IOException("the partial download is missing"), name)
        }
        val (size, mtime) = resolver.openFileDescriptor(saved, "r")?.use { pfd ->
            val st = Os.fstat(pfd.fileDescriptor)
            st.st_size to st.st_mtime * 1000
        } ?: throw IOException("the saved file is missing")
        val shownName = displayNameOf(saved) ?: name
        return SavedFile(saved.toString(), "$label/$shownName", size, mtime)
    }

    private fun findRenamed(name: String): Uri? {
        val dir = partialDir(false) ?: return null
        return resolver.children(folder, dir)?.firstOrNull { it.name == name }?.let { docUri(it.id) }
    }

    private fun copyThenDelete(source: Uri, name: String): Uri {
        val target = DocumentsContract.createDocument(resolver, folder, "application/octet-stream", name)
            ?: throw IOException("could not create $name")
        resolver.openInputStream(source).use { input ->
            resolver.openOutputStream(target, "w").use { output ->
                if (input == null || output == null) throw IOException("could not copy the download")
                input.copyTo(output, 1024 * 1024)
            }
        }
        DocumentsContract.deleteDocument(resolver, source)
        return target
    }

    private fun displayNameOf(uri: Uri): String? = try {
        resolver.query(uri, arrayOf(Document.COLUMN_DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0) else null
        }
    } catch (e: Exception) {
        null
    }

    override fun openSaved(location: String): ReadableFile? = try {
        resolver.openFileDescriptor(Uri.parse(location), "r")?.let(::SafReadable)
    } catch (e: Exception) {
        null
    }

    override fun freeSpace(): Long? {
        if (!rootId.startsWith("primary:")) return null // only known for the phone's own storage
        val dir = context.getExternalFilesDir(null) ?: return null
        return try {
            StatFs(dir.path).availableBytes
        } catch (e: IllegalArgumentException) {
            null
        }
    }
}
