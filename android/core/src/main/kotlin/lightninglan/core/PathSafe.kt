package lightninglan.core

/*
 * File names from other peers are untrusted. The same rules as lightninglan/pathsafe.py, so a
 * name is shown and saved the same way on every peer.
 */

private val INVALID_CHARS: Set<Char> = "<>:\"|?*".toSet() + (0 until 32).map { it.toChar() } + '\u007f'

private val WINDOWS_RESERVED: Set<String> =
    setOf("CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$") +
        "123456789¹²³".map { "COM$it" } + "123456789¹²³".map { "LPT$it" }

// Unicode controls that reorder or hide text: "invoice\u202etxt.exe" displays as "invoiceexe.txt".
private val BIDI_CONTROLS: Set<Char> =
    "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069".toSet()

private val SEPARATORS = Regex("[\\\\/]")
const val MAX_PATH_CHARS = 1024

private val JUNK_NAMES = setOf("thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store")
private val TEMP_SUFFIXES = listOf(".part", ".partial", ".crdownload", ".tmp")

/** Hidden, temporary and OS junk files are never shared. */
fun isIgnoredName(name: String): Boolean {
    val lower = name.lowercase()
    return name.startsWith(".") || name.startsWith("~$") || lower in JUNK_NAMES || TEMP_SUFFIXES.any { lower.endsWith(it) }
}

/** A file name that is safe to show: no text-reordering controls, control characters replaced. */
fun displayName(name: String): String {
    val out = StringBuilder(name.length)
    for (c in name) {
        if (c in BIDI_CONTROLS) continue
        out.append(if (c.code < 32 || c.code == 0x7f) '�' else c)
    }
    return if (out.length > MAX_PATH_CHARS) out.substring(0, MAX_PATH_CHARS) else out.toString()
}

/**
 * Turn a (possibly hostile) remote file name into a single safe local file name. Never fails:
 * folders are dropped, bad characters become '_', and reserved or empty names get a prefix or a fallback.
 */
fun safeLocalFilename(name: String, fallback: String = "download"): String {
    var base = if (name.isNotEmpty()) name.split(SEPARATORS).last() else ""
    base = base.filter { it !in BIDI_CONTROLS }
    base = base.map { if (it in INVALID_CHARS) '_' else it }.joinToString("")
    base = base.trim().trimEnd('.', ' ')
    if (base.startsWith(".")) base = "_" + base.substring(1)
    if (base.isEmpty() || base.all { it == '_' || it == '.' }) base = fallback
    if (isReservedOnWindows(base)) base = "_$base"
    return truncateKeepExtension(base, 200)
}

private fun isReservedOnWindows(name: String): Boolean = name.split(".")[0].trimEnd(' ').uppercase() in WINDOWS_RESERVED

private fun truncateKeepExtension(name: String, maxBytes: Int): String {
    if (name.toByteArray(Charsets.UTF_8).size <= maxBytes) return name
    var (stem, ext) = splitExtension(name)
    if (ext.toByteArray(Charsets.UTF_8).size > 32) {
        stem = name
        ext = ""
    }
    val budget = maxBytes - ext.toByteArray(Charsets.UTF_8).size
    stem = truncateUtf8(stem, budget).trimEnd('.', ' ')
    return stem.ifEmpty { "download" } + ext
}

/** The longest prefix of [text] that fits in [maxBytes] bytes of UTF-8 (never splits a character). */
fun truncateUtf8(text: String, maxBytes: Int): String {
    var bytes = 0
    var i = 0
    while (i < text.length) {
        val cp = text.codePointAt(i)
        val len = when {
            cp < 0x80 -> 1
            cp < 0x800 -> 2
            cp < 0x10000 -> 3
            else -> 4
        }
        if (bytes + len > maxBytes) break
        bytes += len
        i += Character.charCount(cp)
    }
    return text.substring(0, i)
}

/** Split "report.pdf" into ("report", ".pdf"); keeps ".tar.gz" style double extensions. */
fun splitExtension(filename: String): Pair<String, String> {
    if (filename.startsWith(".") && filename.count { it == '.' } == 1) return filename to ""
    val dot = filename.lastIndexOf('.')
    if (dot <= 0) return filename to ""
    var stem = filename.substring(0, dot)
    var ext = filename.substring(dot)
    if (stem.lowercase().endsWith(".tar")) {
        ext = stem.substring(stem.length - 4) + ext
        stem = stem.substring(0, stem.length - 4)
    }
    return stem to ext
}

/** "a.txt", "a (1).txt", "a (2).txt", ... for collision handling. */
fun numberedNames(filename: String): Sequence<String> = sequence {
    yield(filename)
    val (stem, ext) = splitExtension(filename)
    var i = 1
    while (true) yield("$stem (${i++})$ext")
}
