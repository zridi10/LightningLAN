package lightninglan.app

import android.content.Intent
import android.graphics.Color
import android.net.Uri
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

class MainActivity : ComponentActivity() {
    private val sharing: Sharing get() = (application as LightningApp).sharing

    override fun onCreate(savedInstanceState: Bundle?) {
        enableEdgeToEdge()
        super.onCreate(savedInstanceState)
        setContent {
            var theme by remember { mutableStateOf(AppTheme.from(sharing.settings.theme)) }
            val style = styleFor(theme)
            // Light status bar icons on dark themes and on Bliss's blue title bar.
            val lightIcons = style.dark || style.theme == AppTheme.BLISS
            LaunchedEffect(lightIcons) {
                val bars = if (lightIcons) SystemBarStyle.dark(Color.TRANSPARENT) else SystemBarStyle.light(Color.TRANSPARENT, Color.TRANSPARENT)
                enableEdgeToEdge(statusBarStyle = bars, navigationBarStyle = bars)
            }
            LightningTheme(style) {
                App(sharing, theme) {
                    theme = it
                    sharing.settings.theme = it.key
                }
            }
        }
        if (savedInstanceState == null) receiveShared(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        receiveShared(intent)
    }

    override fun onStart() {
        super.onStart()
        sharing.appOpened()
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
