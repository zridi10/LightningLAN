package lightninglan.app

import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.nfc.NfcAdapter
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.SystemBarStyle
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.core.content.IntentCompat
import lightninglan.app.ui.App
import lightninglan.app.ui.AppTheme
import lightninglan.app.ui.LightningTheme
import lightninglan.app.ui.styleFor
import lightninglan.core.JoinLink

class MainActivity : ComponentActivity() {
    private val sharing: Sharing get() = (application as LightningApp).sharing
    private val tabRequest = mutableStateOf<String?>(null) // "play" when opened from a game invitation

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContent {
            var theme by remember { mutableStateOf(AppTheme.from(sharing.settings.theme)) } // Synthwave until another is picked
            val style = styleFor(theme)
            // Light status bar icons on dark themes and on Bliss's blue title bar.
            val lightIcons = style.dark || style.theme == AppTheme.BLISS
            LaunchedEffect(lightIcons) {
                val bars = if (lightIcons) SystemBarStyle.dark(Color.TRANSPARENT) else SystemBarStyle.light(Color.TRANSPARENT, Color.TRANSPARENT)
                enableEdgeToEdge(statusBarStyle = bars, navigationBarStyle = bars)
            }
            LightningTheme(style) {
                App(sharing, theme, { theme = it; sharing.settings.theme = it.key }, tabRequest)
            }
        }
        if (savedInstanceState == null) {
            receiveShared(intent)
            receiveJoinLink(intent)
            intent?.getStringExtra(EXTRA_TAB)?.let { tabRequest.value = it }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        receiveShared(intent)
        receiveJoinLink(intent)
        intent.getStringExtra(EXTRA_TAB)?.let { tabRequest.value = it }
    }

    override fun onStart() {
        super.onStart()
        sharing.appVisible = true
        sharing.appOpened()
    }

    override fun onStop() {
        sharing.appVisible = false
        super.onStop()
    }

    /** Another phone's join link, from its QR code or an NFC tap: join its hotspot. */
    private fun receiveJoinLink(intent: Intent?) {
        if (intent?.action != Intent.ACTION_VIEW && intent?.action != NfcAdapter.ACTION_NDEF_DISCOVERED) return
        val uri = intent.dataString ?: return
        setIntent(Intent(this, MainActivity::class.java)) // not again after a rotation
        val link = JoinLink.parse(uri)
        if (link == null) sharing.toast("This code is for another version of LightningLAN.") else sharing.join(link)
    }

    /** "Share" from another app: the files are copied into the shared folder. */
    private fun receiveShared(intent: Intent?) {
        val uris = when (intent?.action) {
            Intent.ACTION_SEND -> listOfNotNull(IntentCompat.getParcelableExtra(intent, Intent.EXTRA_STREAM, Uri::class.java))
            Intent.ACTION_SEND_MULTIPLE ->
                IntentCompat.getParcelableArrayListExtra(intent, Intent.EXTRA_STREAM, Uri::class.java).orEmpty().filterNotNull()
            else -> return
        }
        setIntent(Intent(this, MainActivity::class.java)) // not again after a rotation
        if (uris.isEmpty()) {
            sharing.toast("There was no file to share.")
        } else {
            sharing.importFiles(uris)
        }
    }
}

/** Opens the app on a tab ("play": While you wait). */
const val EXTRA_TAB = "lightninglan.app.TAB"
