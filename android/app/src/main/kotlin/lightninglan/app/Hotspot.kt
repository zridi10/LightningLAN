package lightninglan.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.location.LocationManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import androidx.core.content.ContextCompat
import lightninglan.core.CoreLog

/*
 * The hotspot: when there is no Wi-Fi to share, or the Wi-Fi keeps devices apart (guest, hotel
 * and event networks often do), this phone makes its own with Android's local-only hotspot. It
 * gives nobody internet, needs no cell service or data plan, and lasts until it is stopped (or
 * the app goes away). Android picks its name and a random password each time; the phone dialog
 * shows them as a "join this Wi-Fi" QR code, then the phone page's QR code on the hotspot's address.
 * Most phones leave their own Wi-Fi while they host one.
 */

private const val TAG = "hotspot"
private const val ADDRESS_WAIT_MS = 10_000L

sealed interface HotspotState {
    data object Off : HotspotState

    data object Starting : HotspotState

    /** [address]: this phone's address on the hotspot, once it has one. */
    data class On(val ssid: String, val password: String?, val address: String?) : HotspotState

    data class Failed(val message: String) : HotspotState
}

/** The permissions Android wants before an app starts a hotspot: "Nearby devices", or location before Android 13. */
fun hotspotPermissions(): Array<String> =
    if (Build.VERSION.SDK_INT >= 33) arrayOf(Manifest.permission.NEARBY_WIFI_DEVICES)
    else arrayOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)

fun hotspotPermissionsMissing(context: Context): Boolean =
    hotspotPermissions().any { ContextCompat.checkSelfPermission(context, it) != PackageManager.PERMISSION_GRANTED }

/** Before Android 13, Location must also be switched on to start a hotspot (the app never asks where the phone is). */
fun locationOffForHotspot(context: Context): Boolean {
    if (Build.VERSION.SDK_INT >= 33) return false
    if (Build.VERSION.SDK_INT >= 28) return context.getSystemService(LocationManager::class.java)?.isLocationEnabled == false
    @Suppress("DEPRECATION")
    return Settings.Secure.getInt(context.contentResolver, Settings.Secure.LOCATION_MODE, Settings.Secure.LOCATION_MODE_OFF) ==
        Settings.Secure.LOCATION_MODE_OFF
}

class Hotspot(context: Context, private val changed: () -> Unit) {
    private val wifi = context.applicationContext.getSystemService(WifiManager::class.java)
    private val main = Handler(Looper.getMainLooper())
    private var reservation: WifiManager.LocalOnlyHotspotReservation? = null

    @Volatile
    var state: HotspotState = HotspotState.Off
        private set

    private fun set(new: HotspotState) {
        state = new
        changed()
    }

    /**
     * Starts the hotspot. On the main thread, while the app is on screen: Android only starts one
     * for the app in front (the sharing service keeps it running after that). Needs [hotspotPermissions].
     */
    @SuppressLint("MissingPermission") // the dialog asks for hotspotPermissions() before it gets here
    fun start() {
        if (state is HotspotState.Starting || state is HotspotState.On) return
        if (wifi == null) return set(HotspotState.Failed("This phone has no Wi-Fi to make a hotspot with."))
        val before = phoneAddresses().toSet()
        set(HotspotState.Starting)
        try {
            wifi.startLocalOnlyHotspot(object : WifiManager.LocalOnlyHotspotCallback() {
                override fun onStarted(r: WifiManager.LocalOnlyHotspotReservation) {
                    if (state !is HotspotState.Starting) { // stopped while it was starting
                        r.close()
                        return
                    }
                    reservation = r
                    val (ssid, password) = credentials(r)
                    CoreLog.i(TAG, "hotspot $ssid is on")
                    set(HotspotState.On(ssid, password, null))
                    findAddress(before, System.currentTimeMillis() + ADDRESS_WAIT_MS)
                }

                override fun onStopped() {
                    reservation = null
                    CoreLog.i(TAG, "Android stopped the hotspot")
                    if (state !is HotspotState.Off) set(HotspotState.Failed("The hotspot stopped. Start it again to carry on."))
                }

                override fun onFailed(reason: Int) {
                    reservation = null
                    CoreLog.w(TAG, "the hotspot didn't start ($reason)")
                    set(HotspotState.Failed(failure(reason)))
                }
            }, main)
        } catch (e: SecurityException) {
            set(HotspotState.Failed("Android didn't let LightningLAN start a hotspot: ${e.message}"))
        } catch (e: IllegalStateException) { // an earlier request is still open
            set(HotspotState.Failed("The hotspot is still busy. Try again in a moment."))
        }
    }

    fun stop() {
        reservation?.close()
        reservation = null
        if (state !is HotspotState.Off) {
            CoreLog.i(TAG, "hotspot off")
            set(HotspotState.Off)
        }
    }

    /** The hotspot's address shows up a moment after it starts: the one this phone didn't have before. */
    private fun findAddress(before: Set<String>, deadline: Long) {
        val on = state as? HotspotState.On ?: return
        val fresh = phoneAddresses().firstOrNull { it !in before }
        when {
            fresh != null -> set(on.copy(address = fresh))
            System.currentTimeMillis() < deadline -> main.postDelayed({ findAddress(before, deadline) }, 300)
            else -> { // it kept an address this phone already had: the best guess is the first one
                CoreLog.w(TAG, "couldn't tell which address is the hotspot's")
                val any = phoneAddresses().firstOrNull()
                if (any != null) {
                    set(on.copy(address = any))
                } else {
                    stop()
                    set(HotspotState.Failed("The hotspot started, but this phone got no address on it. Try again."))
                }
            }
        }
    }

    @Suppress("DEPRECATION") // the older ways, for the Android versions that only have those
    private fun credentials(r: WifiManager.LocalOnlyHotspotReservation): Pair<String, String?> =
        if (Build.VERSION.SDK_INT >= 30) {
            val c = r.softApConfiguration
            (c.ssid ?: "") to c.passphrase
        } else {
            val c = r.wifiConfiguration
            (c?.SSID?.removeSurrounding("\"") ?: "") to c?.preSharedKey?.removeSurrounding("\"")
        }

    private fun failure(reason: Int): String = when (reason) {
        WifiManager.LocalOnlyHotspotCallback.ERROR_NO_CHANNEL -> "There's no free Wi-Fi channel for a hotspot right now. Try again in a moment."
        WifiManager.LocalOnlyHotspotCallback.ERROR_INCOMPATIBLE_MODE ->
            "Android couldn't start it. Turn off the phone's own hotspot (in its settings) and keep LightningLAN open while it starts."
        WifiManager.LocalOnlyHotspotCallback.ERROR_TETHERING_DISALLOWED -> "Hotspots are turned off on this phone (by a work profile or its owner)."
        else -> "Android couldn't start the hotspot. Try again, or use the phone's own hotspot from its settings."
    }
}
