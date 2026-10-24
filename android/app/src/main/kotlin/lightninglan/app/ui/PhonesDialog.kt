@file:OptIn(ExperimentalLayoutApi::class)

package lightninglan.app.ui

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.State
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import lightninglan.app.HotspotState
import lightninglan.app.Sharing
import lightninglan.app.UiSnapshot
import lightninglan.app.hotspotPermissions
import lightninglan.app.hotspotPermissionsMissing
import lightninglan.app.locationOffForHotspot
import lightninglan.core.PhoneLink
import lightninglan.core.qrRows
import lightninglan.core.wifiQrText

/** Why the hotspot can't start yet: something the person has to allow or switch on first. */
internal enum class HotspotBlock { PERMISSION, LOCATION }

/** Starts this phone's hotspot, asking first for what Android wants (here and in the join menus, ConnectDialogs.kt). */
internal class HotspotStarter(private val blockedState: State<HotspotBlock?>, val start: () -> Unit) {
    /** What stopped the last start, if anything. */
    val blocked: HotspotBlock? get() = blockedState.value
}

@Composable
internal fun rememberHotspotStarter(sharing: Sharing): HotspotStarter {
    val context = LocalContext.current
    val blocked = remember { mutableStateOf<HotspotBlock?>(null) }
    fun startUnlessLocationOff() {
        if (locationOffForHotspot(context)) {
            blocked.value = HotspotBlock.LOCATION
        } else {
            blocked.value = null
            sharing.startHotspot()
        }
    }
    val askPermission = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) {
        if (hotspotPermissionsMissing(context)) blocked.value = HotspotBlock.PERMISSION else startUnlessLocationOff()
    }
    return HotspotStarter(blocked) {
        if (hotspotPermissionsMissing(context)) askPermission.launch(hotspotPermissions()) else startUnlessLocationOff()
    }
}

/** What to allow or switch on before the hotspot can start, and the settings screen for it. */
@Composable
internal fun HotspotBlockMessage(blocked: HotspotBlock?, sharing: Sharing) {
    val context = LocalContext.current
    val error = MaterialTheme.colorScheme.error
    when (blocked) {
        HotspotBlock.PERMISSION -> {
            Text(
                if (Build.VERSION.SDK_INT >= 33) "Android needs you to allow \"Nearby devices\" for LightningLAN to start a hotspot."
                else "Android needs you to allow location for LightningLAN to start a hotspot. LightningLAN never asks where the phone is.",
                color = error,
            )
            TextButton(onClick = {
                openScreen(context, sharing, Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", context.packageName, null)))
            }) { Text("App settings") }
        }
        HotspotBlock.LOCATION -> {
            Text(
                "On this Android version, Location has to be switched on to start a hotspot. LightningLAN never asks " +
                    "where the phone is.",
                color = error,
            )
            TextButton(onClick = { openScreen(context, sharing, Intent(Settings.ACTION_LOCATION_SOURCE_SETTINGS)) }) { Text("Location settings") }
        }
        null -> Unit
    }
}

/** Opens one of Android's settings screens. */
internal fun openScreen(context: Context, sharing: Sharing, intent: Intent) = try {
    context.startActivity(intent)
} catch (e: ActivityNotFoundException) {
    sharing.toast("This phone has no settings screen for that.")
}

/**
 * "Use LightningLAN on another phone": the QR code that opens the phone page (the desktop app's
 * phone dialog). The other phone needs no app: it uses LightningLAN in its browser, through this
 * one. With no Wi-Fi in common, this phone makes its own (Hotspot.kt) and the dialog shows two
 * codes: one to join that Wi-Fi, then the phone page's.
 */
@Composable
internal fun PhonesDialog(sharing: Sharing, snap: UiSnapshot, onDismiss: () -> Unit) {
    var links by remember { mutableStateOf<List<PhoneLink>?>(null) }
    var chosen by remember { mutableStateOf<String?>(null) }
    var confirming by remember { mutableStateOf(false) }
    var step by remember { mutableIntStateOf(1) } // with the hotspot: 1 join its Wi-Fi, 2 open LightningLAN
    val starter = rememberHotspotStarter(sharing)
    val phones = snap.status?.phones ?: 0
    var phonesSeen by remember { mutableIntStateOf(phones) }
    val hotspot = snap.hotspot
    LaunchedEffect(snap.addresses, snap.status != null, hotspot) { sharing.phoneLinks { links = it } }
    LaunchedEffect(phones) {
        if (phones > phonesSeen) sharing.toast("Phone connected!")
        phonesSeen = phones
    }
    LaunchedEffect(hotspot is HotspotState.On) { step = 1 }

    val all = links ?: emptyList()
    // With the hotspot, only its own address will do (none until it has one); otherwise the one picked, or the first.
    val link = if (hotspot is HotspotState.On) all.firstOrNull { it.address == hotspot.address }
    else all.firstOrNull { it.address == chosen } ?: all.firstOrNull()
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    val error = MaterialTheme.colorScheme.error

    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = LocalAppStyle.current.solidSurface,
        confirmButton = { TextButton(onClick = onDismiss) { Text("Done") } },
        dismissButton = {
            if (link != null && hotspot !is HotspotState.Starting) {
                TextButton(onClick = { confirming = true }) { Text("Disconnect phones", color = error) }
            }
        },
        title = { Text("Use LightningLAN on another phone") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                Text("An iPhone, or an Android phone without the app: in its web browser, through this phone. Nothing to install.", color = muted)
                when {
                    links == null -> Unit
                    snap.status == null -> Text("Start sharing first.", color = error)
                    hotspot is HotspotState.On -> {
                        FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                            FilterChip(selected = step == 1, onClick = { step = 1 }, label = { Text("1. Join the Wi-Fi") })
                            FilterChip(selected = step == 2, onClick = { step = 2 }, label = { Text("2. Open LightningLAN") })
                        }
                        if (step == 1) {
                            val wifiQr = remember(hotspot.ssid, hotspot.password) { qrRows(wifiQrText(hotspot.ssid, hotspot.password)) }
                            CenteredQr(wifiQr, "QR code to join this phone's Wi-Fi")
                            Text("On the other phone, open the camera, point it at this code and join the Wi-Fi.")
                            SelectionContainer {
                                Column {
                                    Text("Wi-Fi: ${hotspot.ssid}", fontFamily = FontFamily.Monospace)
                                    hotspot.password?.let { Text("Password: $it", fontFamily = FontFamily.Monospace) }
                                }
                            }
                            Text("Camera doesn't offer to join? Pick that Wi-Fi in the phone's settings and type the password.",
                                color = muted, style = MaterialTheme.typography.bodySmall)
                            ThemedButton(onClick = { step = 2 }) { Text("Joined: next") }
                        } else if (link == null) {
                            Text("Getting the hotspot ready…", color = muted)
                            LinearProgressIndicator(Modifier.fillMaxWidth())
                        } else {
                            CenteredQr(link.qr, "QR code of the phone link")
                            Text(
                                "Now scan this one to open LightningLAN. If the phone says the Wi-Fi has no internet, choose to stay " +
                                    "connected: LightningLAN doesn't need any.",
                            )
                            LinkText(link)
                        }
                        Connected(phones)
                        Text(
                            "While the hotspot is on, only phones on it can reach this one, and most phones leave their own Wi-Fi " +
                                "meanwhile. It uses extra battery, so stop it when you're done.",
                            color = muted, style = MaterialTheme.typography.bodySmall,
                        )
                        OutlinedButton(onClick = { sharing.stopHotspot() }) { Text("Stop hotspot") }
                    }
                    hotspot is HotspotState.Starting -> {
                        Text("Starting the hotspot…")
                        LinearProgressIndicator(Modifier.fillMaxWidth())
                    }
                    else -> {
                        if (link == null) {
                            Text("This phone isn't on a Wi-Fi, so another phone can't reach it yet.", color = error)
                        } else {
                            CenteredQr(link.qr, "QR code of the phone link")
                            Text(
                                "1. Connect the other phone to the same Wi-Fi as this one.\n" +
                                    "2. Point its camera at the code and tap the link that appears.\n" +
                                    "3. It can now download anything in ThunderCLOUD, send photos and files here, and play.",
                            )
                            if (all.size > 1) {
                                Text("Network address", style = MaterialTheme.typography.labelLarge)
                                FlowRow(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                                    for (l in all) FilterChip(selected = l == link, onClick = { chosen = l.address }, label = { Text(l.address) })
                                }
                                Text("This phone has more than one. If the other phone can't connect, try another.", color = muted,
                                    style = MaterialTheme.typography.bodySmall)
                            }
                            LinkText(link)
                            Connected(phones)
                            HorizontalDivider()
                        }
                        Text("No Wi-Fi in common?", style = MaterialTheme.typography.labelLarge)
                        Text(
                            "No Wi-Fi here, or one that keeps phones apart (guest, hotel and event Wi-Fi often do)? This phone can " +
                                "make its own for the other phone to join. It needs no internet or cell service.",
                            color = muted,
                        )
                        (hotspot as? HotspotState.Failed)?.let { Text(it.message, color = error) }
                        HotspotBlockMessage(starter.blocked, sharing)
                        if (link == null) {
                            ThemedButton(onClick = starter.start) { Text("Make a hotspot") }
                        } else {
                            OutlinedButton(onClick = starter.start) { Text("Make a hotspot") }
                        }
                        Text(
                            "The other phone has LightningLAN too? ⋮ at the top connects the two with one scan, or a tap.",
                            color = muted, style = MaterialTheme.typography.bodySmall,
                        )
                    }
                }
                if (link != null) {
                    Text(
                        if (hotspot is HotspotState.On) "Anyone who scans the codes can do the same, so only show them to people you trust."
                        else "Anyone who scans the code can do the same, so only show it to people you trust. Not connecting? " +
                            "Guest Wi-Fi often keeps phones apart: make a hotspot instead.",
                        color = muted, style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        },
    )
    if (confirming) {
        AlertDialog(
            onDismissRequest = { confirming = false },
            containerColor = LocalAppStyle.current.solidSurface,
            title = { Text("Disconnect every phone?") },
            text = { Text("To connect again, a phone has to scan the new QR code.") },
            confirmButton = {
                TextButton(onClick = {
                    confirming = false
                    sharing.phoneLinks(newKey = true) { links = it }
                }) { Text("Disconnect") }
            },
            dismissButton = { TextButton(onClick = { confirming = false }) { Text("Cancel") } },
        )
    }
}

@Composable
internal fun CenteredQr(rows: List<String>, description: String) {
    Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.Center) {
        QrCode(rows, description, Modifier.widthIn(max = 260.dp).fillMaxWidth())
    }
}

@Composable
private fun LinkText(link: PhoneLink) {
    Text("No camera? Type this into its browser:", style = MaterialTheme.typography.bodySmall)
    SelectionContainer {
        Text(link.url, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
private fun Connected(phones: Int) {
    Text(
        if (phones == 0) "No phone connected yet." else if (phones == 1) "✓ 1 phone connected right now." else "✓ $phones phones connected right now.",
        color = if (phones > 0) LocalAppStyle.current.ok else MaterialTheme.colorScheme.onSurfaceVariant,
        fontWeight = if (phones > 0) FontWeight.Medium else null,
    )
}

/** A QR code from rows of '1' (dark) and '0' (light), black on white with a quiet zone of four modules. */
@Composable
private fun QrCode(rows: List<String>, description: String, modifier: Modifier = Modifier) {
    Canvas(
        modifier.aspectRatio(1f).clip(RoundedCornerShape(8.dp)).background(Color.White).padding(2.dp)
            .semantics { contentDescription = description },
    ) {
        val modules = rows.size + 8
        val cell = kotlin.math.floor(size.minDimension / modules) // whole pixels, so no seams between modules
        val origin = (size.minDimension - cell * modules) / 2 + cell * 4
        for ((y, row) in rows.withIndex()) {
            var x = 0
            while (x < row.length) {
                if (row[x] != '1') {
                    x++
                    continue
                }
                var run = 1
                while (x + run < row.length && row[x + run] == '1') run++
                drawRect(Color.Black, topLeft = Offset(origin + x * cell, origin + y * cell), size = Size(run * cell, cell))
                x += run
            }
        }
    }
}
