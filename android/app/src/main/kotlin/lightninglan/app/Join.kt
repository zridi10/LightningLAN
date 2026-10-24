package lightninglan.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.WifiNetworkSpecifier
import android.os.Build
import android.os.Handler
import android.os.Looper
import androidx.annotation.RequiresApi
import lightninglan.core.CoreLog
import lightninglan.core.JoinLink
import lightninglan.core.monotonicMs
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/*
 * Joining another phone's hotspot from its join link (one QR code, or an NFC tap; see core
 * Join.kt). On Android 10 and later the app asks Android to join it, and Android asks the person
 * ("Connect?"). Older versions can't do that for an app, so the person joins it in the Wi-Fi
 * settings. Either way the app then sends everything it does over that Wi-Fi (it has no internet,
 * and LightningLAN needs none; other apps keep using mobile data), meets the other phone, and keeps
 * telling it this phone is there until it leaves.
 */

private const val TAG = "join"
private const val ASK_TIMEOUT_MS = 120_000 // how long Android's "Connect?" may stay up
private const val TICK_MS = 2_000L
private const val REINTRODUCE_MS = 30_000L
private const val QUIET_MS = 8_000L // what went wrong shows only after this long: it usually sorts itself out first

sealed interface JoinState {
    data object Off : JoinState

    /** Tells one join from the next, so a dialog closed for one doesn't come back for it. */
    sealed interface Active : JoinState {
        val link: JoinLink
        val session: Int
    }

    /** Waiting for the hotspot ([onHotspot] false), then for the other phone, which may not answer yet ([problem]). */
    data class Joining(override val link: JoinLink, override val session: Int, val onHotspot: Boolean = false, val problem: String? = null) : Active

    data class Joined(override val link: JoinLink, override val session: Int) : Active

    data class Failed(override val link: JoinLink, override val session: Int, val message: String) : Active
}

/** Android 10 and later join a hotspot for the app; before that, the person joins it in Settings. */
val joinsByItself: Boolean get() = Build.VERSION.SDK_INT >= 29

/**
 * This phone on another phone's hotspot. [introduce] says hello to the other phone (null: it
 * answered; otherwise why not), [isOnline] tells whether it is online, [part] forgets it.
 */
class HotspotJoin(
    context: Context,
    private val introduce: (JoinLink) -> String?,
    private val isOnline: (String) -> Boolean,
    private val part: (String) -> Unit,
) {
    private val connectivity = context.applicationContext.getSystemService(ConnectivityManager::class.java)
    private val main = Handler(Looper.getMainLooper())
    private val ticker = Executors.newSingleThreadScheduledExecutor { Thread(it, "lightninglan-join").apply { isDaemon = true } }
    private var callback: ConnectivityManager.NetworkCallback? = null
    private var network: Network? = null
    private var sessions = 0

    @Volatile
    private var onHotspotSince = 0L

    @Volatile
    private var lastHello = 0L

    @Volatile
    var state: JoinState = JoinState.Off
        private set

    /** Joins [link]'s hotspot, leaving any other first. */
    @Synchronized
    fun join(link: JoinLink) {
        leave()
        val session = ++sessions
        state = JoinState.Joining(link, session)
        CoreLog.i(TAG, "joining ${link.name}'s hotspot")
        val builder = NetworkRequest.Builder()
            .addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
            .removeCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        if (joinsByItself) builder.setNetworkSpecifier(specifier(link))
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) = available(this, network)
            override fun onLost(network: Network) = lost(this, network)
            override fun onUnavailable() = unavailable(this)
        }
        callback = cb
        try {
            // Before Android 10 this is whichever Wi-Fi the phone is on: the other phone answers once it's the hotspot.
            if (joinsByItself) connectivity.requestNetwork(builder.build(), cb, main, ASK_TIMEOUT_MS)
            else connectivity.requestNetwork(builder.build(), cb, main)
        } catch (e: Exception) { // SecurityException, or too many requests
            CoreLog.w(TAG, "could not ask for the hotspot: $e")
            callback = null
            state = JoinState.Failed(link, session, "Android didn't let LightningLAN join the hotspot: ${e.message}")
            return
        }
        ticker.schedule({ tick(session) }, TICK_MS, TimeUnit.MILLISECONDS)
    }

    /** Leaves the hotspot (Android goes back to the phone's own Wi-Fi) and forgets the other phone. */
    @Synchronized
    fun leave() {
        val active = state as? JoinState.Active
        release()
        if (active != null) {
            part(active.link.peerId)
            CoreLog.i(TAG, "left ${active.link.name}'s hotspot")
        }
        state = JoinState.Off
    }

    /** Joins the same hotspot again, after it failed. */
    fun retry() {
        (state as? JoinState.Failed)?.let { join(it.link) }
    }

    private fun release() {
        callback?.let { runCatching { connectivity.unregisterNetworkCallback(it) } }
        callback = null
        if (network != null) connectivity.bindProcessToNetwork(null)
        network = null
    }

    @Synchronized
    private fun available(cb: ConnectivityManager.NetworkCallback, network: Network) {
        val joining = state as? JoinState.Joining
        if (callback !== cb || joining == null) return
        this.network = network
        connectivity.bindProcessToNetwork(network) // the hotspot has no internet, so Android wouldn't use it by itself
        onHotspotSince = monotonicMs()
        lastHello = 0
        state = joining.copy(onHotspot = true)
        CoreLog.i(TAG, "on a Wi-Fi without internet: looking for ${joining.link.name}") // the next tick says hello
    }

    @Synchronized
    private fun lost(cb: ConnectivityManager.NetworkCallback, network: Network) {
        val active = state as? JoinState.Active
        if (callback !== cb || active == null || network != this.network) return
        connectivity.bindProcessToNetwork(null)
        this.network = null
        if (joinsByItself) {
            release()
            part(active.link.peerId)
            state = JoinState.Failed(active.link, active.session, "Lost ${active.link.name}'s hotspot. Is it still on?")
        } else { // the phone left that Wi-Fi: it may join it again
            state = JoinState.Joining(active.link, active.session)
        }
    }

    @Synchronized
    private fun unavailable(cb: ConnectivityManager.NetworkCallback) {
        val active = state as? JoinState.Active
        if (callback !== cb || active == null) return
        callback = null
        state = JoinState.Failed(active.link, active.session, "This phone didn't join ${active.link.ssid}.")
    }

    /** Says hello until the other phone answers, then now and then (it forgets a phone it doesn't hear from). */
    private fun tick(session: Int) {
        val (link, onHotspot) = synchronized(this) {
            val s = state
            if (s !is JoinState.Active || s.session != session || s is JoinState.Failed) return
            s.link to (network != null)
        }
        var problem: String? = null
        if (onHotspot) {
            val online = isOnline(link.peerId)
            val now = monotonicMs()
            if (!online || now - lastHello >= REINTRODUCE_MS) {
                problem = introduce(link)
                if (problem == null) lastHello = now
            }
            synchronized(this) {
                val s = state as? JoinState.Active
                if (s != null && s.session == session && s !is JoinState.Failed && network != null) {
                    state = if (isOnline(link.peerId)) JoinState.Joined(link, session)
                    else JoinState.Joining(link, session, onHotspot = true, problem = problem?.takeIf { now - onHotspotSince >= QUIET_MS })
                }
            }
        }
        ticker.schedule({ tick(session) }, TICK_MS, TimeUnit.MILLISECONDS)
    }

    @RequiresApi(29)
    private fun specifier(link: JoinLink): WifiNetworkSpecifier {
        val builder = WifiNetworkSpecifier.Builder().setSsid(link.ssid)
        val password = link.password
        if (password != null) {
            if (link.wpa3Only) builder.setWpa3Passphrase(password) else builder.setWpa2Passphrase(password)
        }
        return builder.build()
    }
}
