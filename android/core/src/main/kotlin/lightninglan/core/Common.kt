package lightninglan.core

/*
 * ThunderCLOUD (lightninglan/common.py): one list of every file shared by this peer and all
 * online peers. Files are keyed by SHA-256, so identical content shared by several peers (even
 * under different names) is shown once with every source listed.
 */

private const val MAX_ALIASES = 5

data class FileSource(val peerId: String, val name: String, val fileName: String, val self: Boolean)

data class CommonFile(
    val id: String,
    val name: String,
    val size: Long,
    val mtime: Double,
    val local: Boolean,
    val sources: List<FileSource>,
    val aliases: List<String>,
)

private class Building(val id: String, val name: String, val size: Long, var mtime: Double) {
    var local = false
    val sources = ArrayList<FileSource>()
    val aliases = ArrayList<String>()
}

fun mergeCommonSpace(ownPeerId: String, ownName: String, ownEntries: List<FileEntry>, peers: List<Peer>): List<CommonFile> {
    val items = LinkedHashMap<String, Building>()

    fun add(sha: String, name: String, size: Long, mtime: Double, peerId: String, peerName: String, self: Boolean) {
        val shown = displayName(name)
        var item = items[sha]
        if (item == null) {
            item = Building(sha, shown, size, mtime)
            items[sha] = item
        } else if (shown != item.name && shown !in item.aliases && item.aliases.size < MAX_ALIASES) {
            item.aliases.add(shown)
        }
        if (item.sources.none { it.peerId == peerId }) item.sources.add(FileSource(peerId, peerName, shown, self))
        item.mtime = maxOf(item.mtime, mtime)
        if (self) item.local = true
    }

    for (entry in ownEntries) add(entry.sha256, entry.relpath, entry.size, entry.mtimeMs / 1000.0, ownPeerId, ownName, true)
    for (peer in peers.sortedWith(compareBy({ it.name.lowercase() }, { it.peerId }))) {
        if (peer.peerId == ownPeerId || !peer.online) continue
        for (f in peer.files) add(f.sha256, f.name, f.size, f.mtime, peer.peerId, peer.name, false)
    }
    return items.values
        .map { CommonFile(it.id, it.name, it.size, it.mtime, it.local, it.sources.toList(), it.aliases.toList()) }
        .sortedWith(compareBy({ it.name.lowercase() }, { it.id }))
}
