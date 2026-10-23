package lightninglan.app

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.media.MediaScannerConnection
import android.net.Uri
import android.os.Build
import android.os.Environment
import androidx.core.content.ContextCompat
import androidx.core.content.FileProvider
import lightninglan.core.DownloadFolder
import lightninglan.core.PathDownloadFolder
import lightninglan.core.SavedFile
import java.io.File
import java.io.IOException

/*
 * The folders LightningLAN makes by itself on first run, like the desktop app's ~/LightningLAN:
 * Download/LightningLAN/Shared and Download/LightningLAN/Downloads on the phone's storage, used
 * through plain file paths. Android 11 and later let an app make folders and files there without
 * asking; older versions need the storage permission, and without it the folders go in the app's
 * own storage instead. Folders picked in Settings go through the Storage Access Framework
 * (SafStorage.kt) instead; the two kinds are told apart by their URI's scheme (file: or content:).
 */

/** The permission older Android versions need for Download/LightningLAN, if it isn't granted yet. */
fun storagePermissionNeeded(context: Context): String? =
    Manifest.permission.WRITE_EXTERNAL_STORAGE.takeIf {
        Build.VERSION.SDK_INT < 30 && ContextCompat.checkSelfPermission(context, it) != PackageManager.PERMISSION_GRANTED
    }

/** Makes the Shared and Downloads folders (or finds them from an earlier run) and returns them. */
fun makeDefaultFolders(context: Context): Pair<File, File> {
    val public = File(Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS), "LightningLAN")
    val places = listOfNotNull(
        public.takeIf { storagePermissionNeeded(context) == null },
        context.getExternalFilesDir(null)?.let { File(it, "LightningLAN") }, // only this app can see it, but it always works
        File(context.filesDir, "LightningLAN"),
    )
    for (base in places) {
        val share = File(base, "Shared")
        val downloads = File(base, "Downloads")
        if (usable(share) && usable(downloads)) return share to downloads
    }
    throw IOException("could not make the LightningLAN folders")
}

private fun usable(dir: File): Boolean = (dir.isDirectory || dir.mkdirs()) && dir.canWrite()

/** The folder a file: URI points to, or null for a folder picked through the Storage Access Framework. */
fun Uri.localFolder(): File? = if (scheme == "file") path?.let(::File) else null

/** A content: URI other apps can open a file of ours with (for "Open"). */
fun shareableUri(context: Context, file: File): Uri? = try {
    FileProvider.getUriForFile(context, "${context.packageName}.files", file)
} catch (e: IllegalArgumentException) { // not in a folder the provider serves
    null
}

/** Where a file: folder is, as the phone's Files app shows it ("Download/LightningLAN/Shared"). */
fun localFolderLabel(dir: File): String {
    val root = Environment.getExternalStorageDirectory().absolutePath.trimEnd('/') + "/"
    return dir.absolutePath.removePrefix(root)
}

/** The downloads folder on a plain path; finished files are announced so galleries and players find them. */
class LocalDownloadFolder private constructor(private val context: Context, private val inner: PathDownloadFolder) :
    DownloadFolder by inner {
    constructor(context: Context, dir: File) : this(context, PathDownloadFolder(dir, localFolderLabel(dir)))

    override fun finish(fileId: String, fileName: String): SavedFile =
        inner.finish(fileId, fileName).also { MediaScannerConnection.scanFile(context, arrayOf(it.location), null, null) }
}
