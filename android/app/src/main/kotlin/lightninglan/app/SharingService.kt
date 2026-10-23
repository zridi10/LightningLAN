package lightninglan.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager

private const val CHANNEL = "sharing"
private const val NOTIFICATION_ID = 1
private const val ACTION_STOP = "lightninglan.app.STOP"
private const val REFRESH_MS = 5_000L
private const val WAKE_LOCK_MS = 10 * 60 * 1000L

/**
 * Keeps the phone sharing while the app is in the background: a foreground service with a
 * notification (and its Stop button), holding the Wi-Fi awake so other peers can reach the phone.
 */
class SharingService : Service() {
    private val handler = Handler(Looper.getMainLooper())
    private var wakeLock: PowerManager.WakeLock? = null
    private var wifiLock: WifiManager.WifiLock? = null
    private var multicastLock: WifiManager.MulticastLock? = null
    private var shown: String? = null
    private val refresh = object : Runnable {
        override fun run() {
            updateNotification()
            wakeLock?.acquire(WAKE_LOCK_MS) // renewed while sharing; lapses by itself if this stops running
            handler.postDelayed(this, REFRESH_MS)
        }
    }

    private val sharing: Sharing get() = (application as LightningApp).sharing

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            sharing.stop()
            return START_NOT_STICKY
        }
        val notification = buildNotification(statusText())
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE)
        } else {
            startForeground(NOTIFICATION_ID, notification)
        }
        acquireLocks()
        handler.removeCallbacks(refresh)
        handler.postDelayed(refresh, REFRESH_MS)
        sharing.serviceStarted()
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        handler.removeCallbacks(refresh)
        releaseLocks()
        super.onDestroy()
    }

    @Suppress("DEPRECATION") // WIFI_MODE_FULL_HIGH_PERF: until Android 14 its replacement only works while the app is in front
    private fun acquireLocks() {
        if (wakeLock == null) {
            wakeLock = getSystemService(PowerManager::class.java)
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "LightningLAN:sharing")
                .apply { setReferenceCounted(false); acquire(WAKE_LOCK_MS) }
        }
        val wifi = applicationContext.getSystemService(WifiManager::class.java) ?: return
        if (wifiLock == null) {
            val mode = if (Build.VERSION.SDK_INT >= 34) WifiManager.WIFI_MODE_FULL_LOW_LATENCY else WifiManager.WIFI_MODE_FULL_HIGH_PERF
            wifiLock = wifi.createWifiLock(mode, "LightningLAN:sharing").apply { setReferenceCounted(false); acquire() }
        }
        if (multicastLock == null) {
            multicastLock = wifi.createMulticastLock("LightningLAN:discovery").apply { setReferenceCounted(false); acquire() }
        }
    }

    private fun releaseLocks() {
        wakeLock?.takeIf { it.isHeld }?.release()
        wifiLock?.takeIf { it.isHeld }?.release()
        multicastLock?.takeIf { it.isHeld }?.release()
        wakeLock = null
        wifiLock = null
        multicastLock = null
    }

    private fun statusText(): String {
        val status = sharing.status() ?: return "Starting…"
        val peers = when (status.peersOnline) {
            0 -> "no other devices yet"
            1 -> "1 other device"
            else -> "${status.peersOnline} other devices"
        }
        return "Sharing ${status.sharedFiles} files as ${status.name} · $peers"
    }

    private fun updateNotification() {
        val text = statusText()
        if (text == shown) return
        getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, buildNotification(text))
    }

    private fun buildNotification(text: String): Notification {
        shown = text
        val manager = getSystemService(NotificationManager::class.java)
        if (manager.getNotificationChannel(CHANNEL) == null) {
            manager.createNotificationChannel(
                NotificationChannel(CHANNEL, getString(R.string.channel_sharing), NotificationManager.IMPORTANCE_LOW).apply {
                    description = "Shown while LightningLAN shares files on your Wi-Fi"
                    setShowBadge(false)
                },
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        val stop = PendingIntent.getService(
            this, 1, Intent(this, SharingService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("LightningLAN is sharing")
            .setContentText(text)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }
}
