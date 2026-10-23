@file:OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)

package lightninglan.app.ui

import android.Manifest
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.RowScope
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Badge
import androidx.compose.material3.BadgedBox
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TextFieldColors
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.MutableState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawWithContent
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import lightninglan.app.BuildConfig
import lightninglan.app.Sharing
import lightninglan.app.UiSnapshot
import lightninglan.app.storagePermissionNeeded
import lightninglan.core.ACTIVE_STATES
import lightninglan.core.CANCELED
import lightninglan.core.COMPLETED
import lightninglan.core.CommonFile
import lightninglan.core.DOWNLOADING
import lightninglan.core.DownloadView
import lightninglan.core.FAILED
import lightninglan.core.PAUSED
import lightninglan.core.QUEUED
import lightninglan.core.VERIFYING
import lightninglan.core.formatSize
import java.text.DateFormat
import java.util.Date

private const val REFRESH_MS = 500L

@Composable
fun App(sharing: Sharing, theme: AppTheme, onTheme: (AppTheme) -> Unit, tabRequest: MutableState<String?>) {
    var snap by remember { mutableStateOf(sharing.snapshot()) }
    val lifecycle = LocalLifecycleOwner.current.lifecycle
    LaunchedEffect(lifecycle) {
        lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
            while (true) {
                snap = withContext(Dispatchers.Default) { sharing.snapshot() }
                delay(REFRESH_MS)
            }
        }
    }
    var screen by rememberSaveable { mutableStateOf("main") }
    val style = LocalAppStyle.current
    Box(Modifier.fillMaxSize().background(if (style.scene) Color.Transparent else MaterialTheme.colorScheme.background)) {
        ThemeScene(style)
        when {
            !snap.setupDone -> FirstRun(sharing, snap.problem)
            screen == "settings" -> {
                BackHandler { screen = "main" }
                SettingsScreen(sharing, snap, theme, onTheme, onBack = { screen = "main" })
            }
            else -> MainScreen(sharing, snap, tabRequest, onSettings = { screen = "settings" })
        }
    }
}

/** A screen's frame: see-through over a theme's scene. */
@Composable
private fun Screen(
    topBar: @Composable () -> Unit = {},
    bottomBar: @Composable () -> Unit = {},
    content: @Composable (PaddingValues) -> Unit,
) {
    val style = LocalAppStyle.current
    Scaffold(
        topBar = topBar,
        bottomBar = bottomBar,
        containerColor = if (style.scene) Color.Transparent else MaterialTheme.colorScheme.background,
        content = content,
    )
}

@Composable
private fun AppTopBar(
    title: @Composable () -> Unit,
    navigationIcon: @Composable () -> Unit = {},
    actions: @Composable RowScope.() -> Unit = {},
) {
    val style = LocalAppStyle.current
    val content = style.topBarContent ?: MaterialTheme.colorScheme.onSurface
    TopAppBar(
        title = title,
        navigationIcon = navigationIcon,
        actions = actions,
        modifier = style.topBar?.let { Modifier.background(it) } ?: Modifier,
        colors = TopAppBarDefaults.topAppBarColors(
            containerColor = if (style.topBar != null) Color.Transparent else MaterialTheme.colorScheme.background,
            titleContentColor = content,
            navigationIconContentColor = content,
            actionIconContentColor = content,
        ),
    )
}

/** A filled button in the theme's style (Bliss: the green Start button; Synthwave: a neon glow). */
@Composable
internal fun ThemedButton(
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
    enabled: Boolean = true,
    contentPadding: PaddingValues = ButtonDefaults.ContentPadding,
    content: @Composable RowScope.() -> Unit,
) {
    val style = LocalAppStyle.current
    val shape = ButtonDefaults.shape
    var m = modifier
    val glow = style.buttonGlow
    if (glow != null && enabled) m = m.shadow(10.dp, shape, ambientColor = glow, spotColor = glow)
    val brush = style.button
    if (brush != null && enabled) {
        Button(
            onClick = onClick,
            modifier = m.background(brush, shape),
            colors = ButtonDefaults.buttonColors(containerColor = Color.Transparent, contentColor = style.buttonContent ?: Color.White),
            contentPadding = contentPadding,
            content = content,
        )
    } else {
        Button(onClick = onClick, modifier = m, enabled = enabled, contentPadding = contentPadding, content = content)
    }
}

/** Text fields get a solid-ish background over a scene, so the scene doesn't show through the text. */
@Composable
private fun fieldColors(): TextFieldColors {
    val style = LocalAppStyle.current
    if (!style.scene) return OutlinedTextFieldDefaults.colors()
    val fill = MaterialTheme.colorScheme.surfaceContainer
    return OutlinedTextFieldDefaults.colors(focusedContainerColor = fill, unfocusedContainerColor = fill, disabledContainerColor = fill)
}

@Composable
internal fun headingStyle(): TextStyle {
    val style = LocalAppStyle.current
    return MaterialTheme.typography.titleMedium.copy(color = style.heading, shadow = style.headingGlow, fontWeight = FontWeight.SemiBold)
}

// ----- first run -------------------------------------------------------------------------------

/**
 * First run: like the desktop app, nothing to choose. The app asks for what it needs (notifications;
 * storage on Android 10 and older), makes its folders and starts sharing; Settings can change it all.
 */
@Composable
private fun FirstRun(sharing: Sharing, problem: String?) {
    val context = LocalContext.current
    var asked by rememberSaveable { mutableStateOf(false) }
    val ask = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { sharing.setUp() }
    LaunchedEffect(Unit) {
        if (asked) return@LaunchedEffect // turned the phone while the question was up
        asked = true
        val notifications = if (Build.VERSION.SDK_INT >= 33) Manifest.permission.POST_NOTIFICATIONS else null
        val wanted = listOfNotNull(
            notifications?.takeIf { ContextCompat.checkSelfPermission(context, it) != PackageManager.PERMISSION_GRANTED },
            storagePermissionNeeded(context),
        )
        if (wanted.isEmpty()) sharing.setUp() else ask.launch(wanted.toTypedArray())
    }
    Screen { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).padding(24.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Spacer(Modifier.height(24.dp))
            Logo(56)
            Text("Welcome to LightningLAN", style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold)
            Text(problem ?: "Getting ready…", color = if (problem != null) MaterialTheme.colorScheme.error else Color.Unspecified)
            if (problem != null) ThemedButton(onClick = sharing::setUp) { Text("Try again") }
        }
    }
}

// ----- the main screen -------------------------------------------------------------------------

@Composable
private fun MainScreen(
    sharing: Sharing,
    snap: UiSnapshot,
    tabRequest: MutableState<String?>,
    onSettings: () -> Unit,
) {
    val context = LocalContext.current
    val style = LocalAppStyle.current
    var tab by rememberSaveable { mutableStateOf("files") }
    LaunchedEffect(tabRequest.value) {
        tabRequest.value?.let {
            tab = it
            tabRequest.value = null
        }
    }
    var query by rememberSaveable { mutableStateOf("") }
    var sort by rememberSaveable { mutableStateOf("name") }
    var sharer by rememberSaveable { mutableStateOf<String?>(null) } // a peer ID: only the files it shares
    var showPeers by remember { mutableStateOf(false) }
    val shareFiles = rememberLauncherForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) sharing.shareInPlace(uris)
    }
    val online = snap.status?.online == true
    val latest = snap.downloads.groupBy { it.fileId }.mapValues { (_, list) -> list.maxBy { it.createdAt } }
    val words = query.lowercase().split(' ').filter { it.isNotEmpty() }
    // Everyone sharing something right now: this phone first, then by name.
    val sharers = snap.files.flatMap { it.sources }
        .associate { it.peerId to (if (it.self) "This phone" else it.name) }
        .entries.sortedWith(compareBy({ it.value != "This phone" }, { it.value.lowercase() }))
    val onlyFrom = sharer?.takeIf { id -> sharers.any { it.key == id } } // gone: everyone again
    fun firstSharer(f: CommonFile) = f.sources.minOfOrNull { (if (it.self) "" else it.name).lowercase() } ?: ""
    val shown = snap.files
        .filter { f -> onlyFrom == null || f.sources.any { it.peerId == onlyFrom } }
        .filter { f -> words.all { w -> w in f.name.lowercase() || f.sources.any { w in it.name.lowercase() } } }
        .let { files ->
            when (sort) {
                "new" -> files.sortedByDescending { it.mtime }
                "size" -> files.sortedByDescending { it.size }
                "sharer" -> files.sortedBy(::firstSharer)
                else -> files
            }
        }

    Screen(
        topBar = {
            AppTopBar(
                title = {
                    Row(verticalAlignment = Alignment.CenterVertically) {
                        Logo(28)
                        Spacer(Modifier.width(10.dp))
                        Text(
                            "LightningLAN",
                            fontWeight = FontWeight.SemiBold,
                            style = style.brand?.let { MaterialTheme.typography.titleLarge.copy(brush = it) } ?: MaterialTheme.typography.titleLarge,
                        )
                    }
                },
                actions = {
                    PeersPill(snap.peers.size, online) { showPeers = true }
                    IconButton(onClick = onSettings) { Icon(Icons.Filled.Settings, contentDescription = "Settings") }
                },
            )
        },
        bottomBar = {
            NavigationBar(containerColor = if (style.scene) MaterialTheme.colorScheme.surfaceContainer else MaterialTheme.colorScheme.surface) {
                NavigationBarItem(
                    selected = tab == "files",
                    onClick = { tab = "files" },
                    icon = { Icon(AppIcons.Folder, contentDescription = null) },
                    label = { Text("Files") },
                )
                NavigationBarItem(
                    selected = tab == "play",
                    onClick = { tab = "play" },
                    icon = {
                        BadgedBox(badge = { waitingForYou(snap.play).takeIf { it > 0 }?.let { Badge { Text("$it") } } }) {
                            Icon(AppIcons.Gamepad, contentDescription = null)
                        }
                    },
                    label = { Text("While you wait") },
                )
            }
        },
    ) { padding ->
        if (tab == "play") {
            PlayPanel(sharing, snap.play, online, padding)
            return@Screen
        }
        LazyColumn(
            contentPadding = PaddingValues(16.dp, padding.calculateTopPadding() + 4.dp, 16.dp, padding.calculateBottomPadding() + 24.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item(key = "status") { StatusCard(sharing, snap) }
            item(key = "share") {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    ThemedButton(onClick = { shareFiles.launch(arrayOf("*/*")) }) {
                        Text("Share files")
                    }
                    Spacer(Modifier.width(12.dp))
                    Text(
                        snap.busy ?: "Everything in ${snap.shareFolder ?: "your Shared folder"} is shared too.",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            if (snap.downloads.isNotEmpty()) {
                item(key = "downloads") {
                    SectionHeader("Downloads", null) {
                        if (snap.downloads.any { it.status == COMPLETED || it.status == CANCELED }) {
                            TextButton(onClick = sharing::clearFinished) { Text("Clear finished") }
                        }
                    }
                }
                item(key = "download-list") { DownloadList(sharing, snap.downloads) }
            }
            item(key = "cloud") {
                SectionHeader("ThunderCLOUD", if (snap.files.size == 1) "1 file" else "${snap.files.size} files") {}
            }
            item(key = "tools") {
                Column(verticalArrangement = Arrangement.spacedBy(6.dp)) {
                    OutlinedTextField(
                        value = query,
                        onValueChange = { query = it },
                        placeholder = { Text("Search") },
                        leadingIcon = { Icon(Icons.Filled.Search, contentDescription = null) },
                        singleLine = true,
                        colors = fieldColors(),
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Row(Modifier.horizontalScroll(rememberScrollState()), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        for ((key, label) in listOf("name" to "Name", "new" to "Newest", "size" to "Largest", "sharer" to "Sharer")) {
                            FilterChip(selected = sort == key, onClick = { sort = key }, label = { Text(label) })
                        }
                    }
                    if (sharers.size > 1) {
                        Row(
                            Modifier.horizontalScroll(rememberScrollState()),
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text("Shared by", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.onSurfaceVariant)
                            FilterChip(selected = onlyFrom == null, onClick = { sharer = null }, label = { Text("Anyone") })
                            for ((id, name) in sharers) {
                                FilterChip(selected = onlyFrom == id, onClick = { sharer = id }, label = { Text(name) })
                            }
                        }
                    }
                }
            }
            if (shown.isEmpty()) {
                item(key = "empty") {
                    Text(
                        when {
                            snap.files.isNotEmpty() -> if (query.isNotBlank()) "No file matches “$query”." else "No files from them."
                            !online -> "Start sharing to see the files on your other devices."
                            snap.peers.isEmpty() -> "No files yet. Open LightningLAN on a computer on this Wi-Fi, or share files from this phone."
                            else -> "No files yet."
                        },
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.padding(vertical = 24.dp),
                    )
                }
            }
            items(shown, key = { "f" + it.id }) { file ->
                FileRow(sharing, context, file, latest[file.id], file.id in snap.linked, online)
            }
        }
    }
    if (showPeers) PeersDialog(snap) { showPeers = false }
}

@Composable
private fun StatusCard(sharing: Sharing, snap: UiSnapshot) {
    val status = snap.status
    val online = status?.online == true
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
        Column(Modifier.padding(16.dp).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Column(Modifier.weight(1f)) {
                    Text(status?.name ?: sharing.settings.name, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                    val line = when {
                        status == null -> if (snap.problem != null) "Not sharing." else "Starting…"
                        !online -> "Stopped. Your other devices can't see this phone."
                        else -> buildString {
                            append(if (status.sharedFiles == 1) "Sharing 1 file" else "Sharing ${status.sharedFiles} files")
                            if (status.indexingPending > 0) append(" · checking ${status.indexingPending} more")
                            if (status.passphrase) append(" · passphrase on")
                        }
                    }
                    Text(line, style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                if (online) {
                    OutlinedButton(onClick = sharing::stop) { Text("Stop") }
                } else if (status != null || snap.problem != null) {
                    ThemedButton(onClick = sharing::start) {
                        Icon(Icons.Filled.PlayArrow, contentDescription = null)
                        Text("Start")
                    }
                }
            }
            if (!snap.onWifi) {
                Text(
                    "Not on Wi-Fi. Connect to the same Wi-Fi as your computers.",
                    color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall,
                )
            }
            snap.problem?.let { Text(it, color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.bodySmall) }
        }
    }
}

@Composable
private fun SectionHeader(title: String, subtitle: String?, action: @Composable () -> Unit) {
    Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
        Text(title, style = headingStyle())
        if (subtitle != null) {
            Spacer(Modifier.width(8.dp))
            Text(subtitle, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        Spacer(Modifier.weight(1f))
        action()
    }
}

@Composable
private fun PeersPill(count: Int, online: Boolean, onClick: () -> Unit) {
    val style = LocalAppStyle.current
    Surface(onClick = onClick, shape = RoundedCornerShape(50), color = MaterialTheme.colorScheme.surfaceContainerHigh) {
        Row(Modifier.padding(horizontal = 12.dp, vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
            Box(Modifier.size(8.dp).clip(CircleShape).background(if (online) style.ok else MaterialTheme.colorScheme.outline))
            Spacer(Modifier.width(6.dp))
            Text(if (count == 1) "1 device" else "$count devices", style = MaterialTheme.typography.labelLarge)
        }
    }
}

@Composable
private fun PeersDialog(snap: UiSnapshot, onDismiss: () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = LocalAppStyle.current.solidSurface,
        confirmButton = { TextButton(onClick = onDismiss) { Text("Done") } },
        title = { Text("Devices on this Wi-Fi") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                if (snap.peers.isEmpty() && snap.ignored.isEmpty()) {
                    Text(
                        "None found yet. Make sure LightningLAN is running on your computer, that both are on the same Wi-Fi " +
                            "(not a guest network), and that the computer's firewall allows LightningLAN on private networks.",
                    )
                }
                for (p in snap.peers) {
                    Column {
                        Text(p.name, fontWeight = FontWeight.Medium)
                        Text(
                            "${if (p.files == 1) "1 file" else "${p.files} files"} · ${p.address}",
                            style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                if (snap.ignored.isNotEmpty()) {
                    HorizontalDivider()
                    Text("Not connected", style = MaterialTheme.typography.labelLarge)
                    for (p in snap.ignored) {
                        Column {
                            Text(p.name, fontWeight = FontWeight.Medium)
                            Text(p.reason ?: "", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)
                        }
                    }
                }
            }
        },
    )
}

// ----- files -----------------------------------------------------------------------------------

@Composable
private fun FileRow(sharing: Sharing, context: Context, file: CommonFile, download: DownloadView?, linked: Boolean, online: Boolean) {
    val baseName = file.name.substringAfterLast('/')
    val folder = file.name.substringBeforeLast('/', "")
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
        Row(Modifier.padding(12.dp).fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            ExtensionBadge(baseName)
            Spacer(Modifier.width(12.dp))
            Column(Modifier.weight(1f)) {
                Text(baseName, fontWeight = FontWeight.Medium, maxLines = 2, overflow = TextOverflow.Ellipsis)
                if (folder.isNotEmpty()) {
                    Text(folder, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 1, overflow = TextOverflow.Ellipsis)
                }
                Text(fileMeta(file), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant, maxLines = 2, overflow = TextOverflow.Ellipsis)
            }
            Spacer(Modifier.width(8.dp))
            when {
                file.local && linked -> TextButton(onClick = { sharing.unshare(file.id) }) { Text("Unshare") }
                file.local -> TextButton(onClick = { open(context, sharing.openIntent(file.id, null, baseName)) }) { Text("Open") }
                download != null && download.status in ACTIVE_STATES ->
                    Text("${(download.progress * 100).toInt()}%", style = MaterialTheme.typography.labelLarge, color = MaterialTheme.colorScheme.primary)
                download?.status == COMPLETED && download.saved != null ->
                    TextButton(onClick = { open(context, sharing.openIntent(null, download.saved!!.location, baseName)) }) { Text("Open") }
                else -> ThemedButton(onClick = { sharing.download(file.id) }, enabled = online, contentPadding = PaddingValues(horizontal = 14.dp)) {
                    Icon(AppIcons.Download, contentDescription = null, modifier = Modifier.size(18.dp))
                    Spacer(Modifier.width(4.dp))
                    Text("Get")
                }
            }
        }
    }
}

private fun fileMeta(file: CommonFile): String {
    val where = buildList {
        if (file.local) add("this phone")
        addAll(file.sources.filter { !it.self }.map { it.name })
    }.joinToString(", ")
    val date = if (file.mtime > 0) DateFormat.getDateInstance(DateFormat.MEDIUM).format(Date((file.mtime * 1000).toLong())) else null
    return listOfNotNull(formatSize(file.size), date, "on $where").joinToString(" · ")
}

@Composable
private fun ExtensionBadge(name: String) {
    val ext = name.substringAfterLast('.', "").take(4).uppercase().ifEmpty { "FILE" }
    val hue = ((ext.hashCode() % 360) + 360) % 360
    Box(
        Modifier.size(40.dp).clip(RoundedCornerShape(8.dp)).background(Color.hsl(hue.toFloat(), 0.55f, 0.5f)),
        contentAlignment = Alignment.Center,
    ) {
        Text(ext, color = Color.White, style = MaterialTheme.typography.labelSmall, fontWeight = FontWeight.Bold, maxLines = 1)
    }
}

// ----- downloads -------------------------------------------------------------------------------

/** The downloads scroll in a box of their own, so a long list doesn't push the files down the screen. */
@Composable
private fun DownloadList(sharing: Sharing, downloads: List<DownloadView>) {
    val scroll = rememberScrollState()
    val thumb = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.5f)
    Column(
        Modifier
            .fillMaxWidth()
            .heightIn(max = 320.dp)
            .drawWithContent {
                drawContent()
                if (scroll.maxValue > 0) { // a slim scrollbar on the right
                    val visible = size.height / (size.height + scroll.maxValue)
                    val length = size.height * visible
                    val top = (size.height - length) * scroll.value / scroll.maxValue
                    drawRoundRect(thumb, Offset(size.width - 4.dp.toPx(), top), Size(3.dp.toPx(), length), CornerRadius(2.dp.toPx()))
                }
            }
            .verticalScroll(scroll)
            .padding(end = 8.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        for (d in downloads) DownloadRow(sharing, d)
    }
}

@Composable
private fun DownloadRow(sharing: Sharing, d: DownloadView) {
    val context = LocalContext.current
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
        Column(Modifier.padding(start = 12.dp, end = 4.dp, top = 6.dp, bottom = 10.dp).fillMaxWidth()) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(d.name, fontWeight = FontWeight.Medium, modifier = Modifier.weight(1f))
                when (d.status) {
                    QUEUED, DOWNLOADING, VERIFYING -> {
                        IconButton(onClick = { sharing.pause(d.id) }) { Icon(AppIcons.Pause, contentDescription = "Pause") }
                        IconButton(onClick = { sharing.cancel(d.id) }) { Icon(Icons.Filled.Close, contentDescription = "Cancel") }
                    }
                    PAUSED, FAILED -> {
                        TextButton(onClick = { sharing.resume(d.id) }) { Text(if (d.status == PAUSED) "Resume" else "Retry") }
                        IconButton(onClick = { sharing.cancel(d.id) }) { Icon(Icons.Filled.Close, contentDescription = "Cancel") }
                    }
                    COMPLETED -> {
                        TextButton(onClick = { open(context, sharing.openIntent(null, d.saved?.location, d.name)) }) { Text("Open") }
                        IconButton(onClick = { sharing.remove(d.id) }) { Icon(Icons.Filled.Close, contentDescription = "Remove from the list") }
                    }
                    else -> IconButton(onClick = { sharing.remove(d.id) }) { Icon(Icons.Filled.Close, contentDescription = "Remove from the list") }
                }
            }
            if (d.status in ACTIVE_STATES || d.status == PAUSED) {
                LinearProgressIndicator(progress = { d.progress.toFloat() }, modifier = Modifier.fillMaxWidth().padding(end = 8.dp, bottom = 4.dp))
            }
            Text(
                downloadLine(d),
                style = MaterialTheme.typography.bodySmall,
                color = if (d.status == FAILED) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(end = 8.dp),
            )
        }
    }
}

private fun downloadLine(d: DownloadView): String {
    val percent = "${(d.progress * 100).toInt()}%"
    return when (d.status) {
        QUEUED -> d.note ?: "Waiting…"
        VERIFYING -> d.note ?: "Checking the file…"
        DOWNLOADING -> listOfNotNull(
            percent,
            "${formatSize(d.received)} of ${formatSize(d.size)}",
            if (d.speed > 0) "${formatSize(d.speed.toLong())}/s" else null,
            d.contributors.takeIf { it.isNotEmpty() }?.let { c -> "from " + c.joinToString(", ") { it.first } }
                ?: d.sourceName?.let { "from $it" },
        ).joinToString(" · ") + (d.note?.let { "\n$it" } ?: "")
        PAUSED -> "Paused at $percent"
        FAILED -> d.error ?: "The download failed."
        COMPLETED -> "Saved to ${d.saved?.label ?: "the downloads folder"}"
        else -> "Canceled"
    }
}

// ----- settings --------------------------------------------------------------------------------

@Composable
private fun SettingsScreen(sharing: Sharing, snap: UiSnapshot, theme: AppTheme, onTheme: (AppTheme) -> Unit, onBack: () -> Unit) {
    val context = LocalContext.current
    var name by rememberSaveable { mutableStateOf(sharing.settings.name) }
    var savedName by remember { mutableStateOf(sharing.settings.name) }
    var passphrase by rememberSaveable { mutableStateOf(sharing.settings.passphrase ?: "") }
    var savedPassphrase by remember { mutableStateOf(sharing.settings.passphrase ?: "") }
    var showPassphrase by remember { mutableStateOf(false) }
    var saving by remember { mutableStateOf(false) }
    val pickShare = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri -> uri?.let(sharing::changeShareFolder) }
    val pickDownloads = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri -> uri?.let(sharing::changeDownloadsFolder) }

    Screen(
        topBar = {
            AppTopBar(
                title = { Text("Settings") },
                navigationIcon = { IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back") } },
            )
        },
    ) { padding ->
        Column(
            Modifier.fillMaxSize().padding(padding).verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(16.dp),
        ) {
            Setting("Name", "What your other devices call this phone.") {
                NameField(name, onChange = { name = it }, onDice = { name = sharing.suggestName() })
                ThemedButton(
                    onClick = {
                        if (sharing.rename(name)) {
                            savedName = sharing.settings.name
                            name = savedName
                            Toast.makeText(context, "Name saved", Toast.LENGTH_SHORT).show()
                        }
                    },
                    enabled = name.isNotBlank() && name.trim() != savedName,
                ) { Text("Save name") }
            }
            Setting("Theme", null) {
                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    for (option in AppTheme.entries) {
                        FilterChip(
                            selected = option == theme,
                            onClick = { onTheme(option) },
                            label = { Text(option.label) },
                        )
                    }
                }
            }
            Setting("Passphrase", "Only devices with the same passphrase see each other. Leave it empty to share with everyone on this Wi-Fi.") {
                OutlinedTextField(
                    value = passphrase,
                    onValueChange = { passphrase = it },
                    singleLine = true,
                    label = { Text("Passphrase") },
                    visualTransformation = if (showPassphrase) VisualTransformation.None else PasswordVisualTransformation(),
                    keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Password),
                    trailingIcon = {
                        TextButton(onClick = { showPassphrase = !showPassphrase }) { Text(if (showPassphrase) "Hide" else "Show") }
                    },
                    colors = fieldColors(),
                    modifier = Modifier.fillMaxWidth(),
                )
                ThemedButton(
                    onClick = {
                        saving = true
                        sharing.setPassphrase(passphrase) {
                            saving = false
                            savedPassphrase = passphrase
                        }
                    },
                    enabled = !saving && passphrase != savedPassphrase,
                ) { Text(if (saving) "Saving…" else if (passphrase.isEmpty()) "Turn off passphrase" else "Save passphrase") }
            }
            Setting("Shared folder", "Everything in it is shared. Files you share with “Share files” stay where they are instead.") {
                FolderRow(snap.shareFolder) { pickShare.launch(null) }
            }
            Setting("Downloads folder", "Where the files you get are saved.") {
                FolderRow(snap.downloadsFolder) { pickDownloads.launch(null) }
            }
            Setting("Network", null) {
                val port = snap.status?.port?.takeIf { it > 0 }
                Text(
                    if (snap.addresses.isEmpty()) "Not connected to a network." else snap.addresses.joinToString("\n") { if (port != null) "$it:$port" else it },
                )
                Text(
                    "Your other devices find this phone by themselves. Sharing keeps the Wi-Fi awake while it's on, which uses " +
                        "some battery: press Stop when you're done.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Text(
                "LightningLAN ${BuildConfig.VERSION_NAME}",
                style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** One setting, on a card so it stays readable over a theme's scene. */
@Composable
private fun Setting(title: String, help: String?, content: @Composable () -> Unit) {
    Card(colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
        Column(Modifier.padding(16.dp).fillMaxWidth(), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(title, style = headingStyle())
            if (help != null) Text(help, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            content()
        }
    }
}

@Composable
private fun FolderRow(label: String?, onChange: () -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(AppIcons.Folder, contentDescription = null, tint = MaterialTheme.colorScheme.onSurfaceVariant)
        Spacer(Modifier.width(8.dp))
        Text(label ?: "Not chosen", modifier = Modifier.weight(1f))
        OutlinedButton(onClick = onChange) { Text("Change") }
    }
}

// ----- shared pieces ---------------------------------------------------------------------------

@Composable
private fun NameField(value: String, onChange: (String) -> Unit, onDice: () -> Unit, label: String = "Name") {
    OutlinedTextField(
        value = value,
        onValueChange = { onChange(it.take(64)) },
        label = { Text(label) },
        singleLine = true,
        trailingIcon = { IconButton(onClick = onDice) { Icon(AppIcons.Dice, contentDescription = "Pick a funny name") } },
        colors = fieldColors(),
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun Logo(sizeDp: Int) {
    Box(
        Modifier.size(sizeDp.dp).clip(RoundedCornerShape((sizeDp / 4).dp)).background(Color(0xFF4F46E5)),
        contentAlignment = Alignment.Center,
    ) {
        Icon(AppIcons.Bolt, contentDescription = null, tint = LocalAppStyle.current.bolt, modifier = Modifier.size((sizeDp * 3 / 4).dp))
    }
}

private fun open(context: Context, intent: Intent?) {
    if (intent == null) {
        Toast.makeText(context, "That file isn't there any more.", Toast.LENGTH_SHORT).show()
        return
    }
    try {
        context.startActivity(intent)
    } catch (e: ActivityNotFoundException) {
        Toast.makeText(context, "No app on this phone can open that file.", Toast.LENGTH_SHORT).show()
    } catch (e: SecurityException) {
        Toast.makeText(context, "That file can't be opened from here.", Toast.LENGTH_SHORT).show()
    }
}
