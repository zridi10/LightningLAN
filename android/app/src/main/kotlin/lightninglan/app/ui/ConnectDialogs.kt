package lightninglan.app.ui

import android.app.Activity
import android.content.Context
import android.content.ContextWrapper
import android.content.Intent
import android.provider.Settings
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import kotlinx.coroutines.delay
import lightninglan.app.HotspotState
import lightninglan.app.JoinState
import lightninglan.app.NfcTag
import lightninglan.app.Sharing
import lightninglan.app.UiSnapshot
import lightninglan.app.joinsByItself
import lightninglan.app.locationOffForHotspot
import lightninglan.core.JoinLink
import lightninglan.core.qrRows

/*
 * Two phones that both have LightningLAN and no Wi-Fi in common (Join.kt), from the ⋮ menu: this
 * phone makes a hotspot and hands the other one its join link, as one QR code (ConnectQrDialog) or
 * with an NFC tap (ConnectTapDialog). The other phone opens the link in LightningLAN, which joins
 * the hotspot (JoinDialog shows how that goes there). The phone dialog (PhonesDialog.kt) stays for
 * phones without the app.
 */

/** "Connect a LightningLAN phone (QR code)": one scan joins the hotspot and connects. */
@Composable
internal fun ConnectQrDialog(sharing: Sharing, snap: UiSnapshot, onDismiss: () -> Unit) {
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    ConnectDialog("Connect a phone with LightningLAN", onDismiss) {
        Text(
            "For two phones that both have LightningLAN, and no Wi-Fi in common. This phone makes its own Wi-Fi, and the " +
                "other one joins it with one scan. No internet or cell service needed.",
            color = muted,
        )
        HotspotHost(sharing, snap) { link ->
            val qr = remember(link) { qrRows(link.toUri()) }
            CenteredQr(qr, "QR code for another phone with LightningLAN")
            Text(
                "On the other phone, open the camera, point it at this code and tap the link. LightningLAN opens there, and " +
                    "Android asks to connect to ${link.ssid}: tap Connect.",
            )
            Text(
                "A web page opens instead of LightningLAN? Tap its \"Open in LightningLAN\" button.",
                color = muted, style = MaterialTheme.typography.bodySmall,
            )
        }
        SameWifiNote()
    }
}

/** "Connect a LightningLAN phone (NFC tap)": the two phones touch, and the other one joins the hotspot and connects. */
@Composable
internal fun ConnectTapDialog(sharing: Sharing, snap: UiSnapshot, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val activity = remember(context) { context.findActivity() }
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    var nfcOn by remember { mutableStateOf(NfcTag.turnedOn(context)) }
    LaunchedEffect(Unit) {
        while (true) { // switched on in Settings meanwhile?
            nfcOn = NfcTag.turnedOn(context)
            delay(1_000)
        }
    }
    DisposableEffect(Unit) {
        NfcTag.open(context) // Android takes a moment to route readers to the tag: start now
        onDispose { NfcTag.close(context) }
    }
    val view = LocalView.current
    DisposableEffect(view) {
        view.keepScreenOn = true // the tag only works with the screen on
        onDispose { view.keepScreenOn = false }
    }
    ConnectDialog("Tap to connect", onDismiss) {
        Text(
            "For two phones that both have LightningLAN and NFC, and no Wi-Fi in common. This phone makes its own Wi-Fi, " +
                "and the other one joins it when the two touch. No internet or cell service needed.",
            color = muted,
        )
        if (!nfcOn) {
            Text("NFC is off on this phone.", color = MaterialTheme.colorScheme.error)
            TextButton(onClick = { openScreen(context, sharing, Intent(Settings.ACTION_NFC_SETTINGS)) }) { Text("NFC settings") }
        }
        HotspotHost(sharing, snap) { link ->
            DisposableEffect(link) {
                NfcTag.serve(link.toUri())
                activity?.let(NfcTag::preferWhile)
                onDispose {
                    NfcTag.unserve()
                    activity?.let(NfcTag::stopPreferring)
                }
            }
            if (nfcOn) {
                Text("Ready. Hold the other phone against this one, back to back.", fontWeight = FontWeight.Medium)
                Text(
                    "It has to be unlocked, with NFC on. LightningLAN opens on it, and Android asks to connect to " +
                        "${link.ssid}: tap Connect there. Keep this screen open until it's connected.",
                )
                Text(
                    "Nothing happens? Slide the phones around slowly: the NFC spot is somewhere else on every phone. A few " +
                        "phones can't read each other at all; the QR code in the same menu always works.",
                    color = muted, style = MaterialTheme.typography.bodySmall,
                )
            }
        }
        SameWifiNote()
    }
}

@Composable
private fun ConnectDialog(title: String, onDismiss: () -> Unit, content: @Composable () -> Unit) {
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = LocalAppStyle.current.solidSurface,
        confirmButton = { TextButton(onClick = onDismiss) { Text("Done") } },
        title = { Text(title) },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) { content() }
        },
    )
}

@Composable
private fun SameWifiNote() = Text(
    "Already on the same Wi-Fi? Nothing to do: LightningLAN phones find each other by themselves.",
    color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall,
)

/** What both menus share: starting the hotspot, then [ready] with the join link, then which phones joined. */
@Composable
private fun HotspotHost(sharing: Sharing, snap: UiSnapshot, ready: @Composable (JoinLink) -> Unit) {
    val starter = rememberHotspotStarter(sharing)
    val hotspot = snap.hotspot
    val link = remember(hotspot, snap.status) { sharing.joinLink() }
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    val error = MaterialTheme.colorScheme.error
    when {
        snap.status?.online != true -> Text("Start sharing first.", color = error)
        hotspot is HotspotState.Starting -> Progress("Starting the hotspot…")
        hotspot is HotspotState.On && link == null -> Progress("Getting the hotspot ready…")
        hotspot is HotspotState.On && link != null -> {
            ready(link)
            val met = snap.peers.filter { it.direct }
            if (met.isEmpty()) {
                Text("No phone connected yet.", color = muted)
            } else {
                for (p in met) Text("✓ ${p.name} is connected.", color = LocalAppStyle.current.ok, fontWeight = FontWeight.Medium)
            }
            Text(
                "Only show it to people you trust: whoever joins sees what's shared. Most phones leave their own Wi-Fi " +
                    "while the hotspot is on, and it uses extra battery, so stop it when you're done.",
                color = muted, style = MaterialTheme.typography.bodySmall,
            )
            OutlinedButton(onClick = sharing::stopHotspot) { Text("Stop hotspot") }
        }
        else -> {
            (hotspot as? HotspotState.Failed)?.let { Text(it.message, color = error) }
            HotspotBlockMessage(starter.blocked, sharing)
            ThemedButton(onClick = starter.start) { Text("Make a hotspot") }
        }
    }
}

@Composable
private fun Progress(text: String) {
    Text(text)
    LinearProgressIndicator(Modifier.fillMaxWidth())
}

/** On the phone that opened a join link: joining the other phone's hotspot, and how it went. */
@Composable
internal fun JoinDialog(sharing: Sharing, join: JoinState.Active, onDismiss: () -> Unit) {
    val context = LocalContext.current
    val link = join.link
    val muted = MaterialTheme.colorScheme.onSurfaceVariant
    val error = MaterialTheme.colorScheme.error
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = LocalAppStyle.current.solidSurface,
        confirmButton = {
            if (join is JoinState.Failed) TextButton(onClick = sharing::joinAgain) { Text("Try again") }
            else TextButton(onClick = onDismiss) { Text(if (join is JoinState.Joined) "Done" else "Hide") }
        },
        dismissButton = {
            TextButton(onClick = {
                sharing.leaveHotspot()
                onDismiss()
            }) { Text(if (join is JoinState.Joined) "Leave" else if (join is JoinState.Failed) "Close" else "Cancel") }
        },
        title = { Text(if (join is JoinState.Joined) "Connected to ${link.name}" else "Connecting to ${link.name}") },
        text = {
            Column(Modifier.verticalScroll(rememberScrollState()), verticalArrangement = Arrangement.spacedBy(10.dp)) {
                when (join) {
                    is JoinState.Joining -> when {
                        join.onHotspot -> {
                            Progress("On ${link.name}'s hotspot. Saying hello…")
                            join.problem?.let { Text("Not yet: $it", color = error) }
                        }
                        joinsByItself -> Progress("Android asks to connect to ${link.ssid}: tap Connect.")
                        else -> {
                            Text("This Android version can't join a Wi-Fi for an app. Join this one in the Wi-Fi settings:")
                            SelectionContainer {
                                Column {
                                    Text("Wi-Fi: ${link.ssid}", fontFamily = FontFamily.Monospace)
                                    link.password?.let { Text("Password: $it", fontFamily = FontFamily.Monospace) }
                                }
                            }
                            TextButton(onClick = { openScreen(context, sharing, Intent(Settings.ACTION_WIFI_SETTINGS)) }) { Text("Wi-Fi settings") }
                            Progress("Then come back here. If Android says it has no internet, stay connected: LightningLAN needs none.")
                        }
                    }
                    is JoinState.Joined -> {
                        Text(
                            "✓ ${link.name}'s files are in ThunderCLOUD, and the two of you can play in While you wait.",
                            color = LocalAppStyle.current.ok, fontWeight = FontWeight.Medium,
                        )
                        Text(
                            "Leave when you're done, and this phone goes back to its own Wi-Fi. Other apps keep using mobile data meanwhile.",
                            color = muted,
                        )
                    }
                    is JoinState.Failed -> {
                        Text(join.message, color = error)
                        Text("Tap Try again, and Connect when Android asks.")
                        if (locationOffForHotspot(context)) {
                            Text("On this Android version, switching Location on can help it find the hotspot.", color = muted)
                        }
                    }
                }
                if (link.passphrase && sharing.settings.passphrase == null) {
                    Text("${link.name} uses a passphrase: set the same one in Settings.", color = muted)
                }
            }
        },
    )
}

private tailrec fun Context.findActivity(): Activity? = when (this) {
    is Activity -> this
    is ContextWrapper -> baseContext.findActivity()
    else -> null
}
