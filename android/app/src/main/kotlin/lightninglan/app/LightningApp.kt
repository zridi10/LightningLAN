package lightninglan.app

import android.app.Application
import android.util.Log
import lightninglan.core.CoreLog

class LightningApp : Application() {
    lateinit var sharing: Sharing
        private set

    override fun onCreate() {
        super.onCreate()
        CoreLog.sink = { level, tag, message, error ->
            val priority = when (level) {
                CoreLog.Level.DEBUG -> Log.DEBUG
                CoreLog.Level.INFO -> Log.INFO
                CoreLog.Level.WARN -> Log.WARN
                CoreLog.Level.ERROR -> Log.ERROR
            }
            if (level != CoreLog.Level.DEBUG || BuildConfig.DEBUG) {
                Log.println(priority, "LightningLAN", "$tag: $message" + (error?.let { "\n" + Log.getStackTraceString(it) } ?: ""))
            }
        }
        sharing = Sharing(this)
    }
}
