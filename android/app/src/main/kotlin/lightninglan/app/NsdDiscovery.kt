package lightninglan.app

import android.content.Context
import android.net.ConnectivityManager
import android.net.LinkProperties
import android.net.Network
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.os.Build
import android.os.Handler
import android.os.Looper
import lightninglan.core.Advert
import lightninglan.core.CoreLog
import lightninglan.core.Discovery
import lightninglan.core.DiscoveryListener
import lightninglan.core.SERVICE_TYPE
import lightninglan.core.parseServiceRecord
import java.net.Inet4Address
import java.net.InetAddress

private const val TAG = "discovery"
private const val NETWORK_SETTLE_MS = 2_000L

/**
 * Announces this phone and finds other peers with Android's DNS-SD (the desktop app uses
 * python-zeroconf; both speak mDNS, `_lightninglan._tcp`). Starts over when the network changes.
 */
class NsdDiscovery(context: Context) : Discovery {
    private val nsd = context.getSystemService(NsdManager::class.java)
    private val connectivity = context.getSystemService(ConnectivityManager::class.java)
    private val handler = Handler(Looper.getMainLooper())
    private val lock = Any()
    private var advert: Advert? = null
    private var listener: DiscoveryListener? = null
    private var registration: NsdManager.RegistrationListener? = null
    private var browsing: NsdManager.DiscoveryListener? = null
    private val toResolve = ArrayDeque<NsdServiceInfo>()
    private var resolving = false
    private var networkCallback: ConnectivityManager.NetworkCallback? = null
    private var lastAddresses: List<InetAddress>? = null
    private val restart = Runnable {
        synchronized(lock) {
            val a = advert ?: return@Runnable
            CoreLog.i(TAG, "network changed: announcing again")
            stopNsd()
            startNsd(a)
        }
    }

    override fun start(advert: Advert, listener: DiscoveryListener) {
        synchronized(lock) {
            this.advert = advert
            this.listener = listener
            startNsd(advert)
            watchNetwork()
        }
    }

    override fun update(advert: Advert) {
        synchronized(lock) {
            if (this.advert == null) return
            this.advert = advert
            unregister()
            register(advert)
        }
    }

    override fun stop() {
        synchronized(lock) {
            advert = null
            listener = null
            handler.removeCallbacks(restart)
            networkCallback?.let { runCatching { connectivity.unregisterNetworkCallback(it) } }
            networkCallback = null
            lastAddresses = null
            stopNsd()
        }
    }

    private fun startNsd(advert: Advert) {
        register(advert)
        browse()
    }

    private fun stopNsd() {
        unregister()
        browsing?.let { runCatching { nsd.stopServiceDiscovery(it) } }
        browsing = null
        toResolve.clear()
        resolving = false
    }

    // ----- announcing --------------------------------------------------------------------------

    private fun register(advert: Advert) {
        val info = NsdServiceInfo().apply {
            serviceName = advert.serviceName
            serviceType = SERVICE_TYPE
            port = advert.port
            for ((key, value) in advert.txt()) setAttribute(key, value)
        }
        val l = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(info: NsdServiceInfo) = CoreLog.i(TAG, "announcing ${info.serviceName}")
            override fun onRegistrationFailed(info: NsdServiceInfo, errorCode: Int) = CoreLog.w(TAG, "could not announce this phone (error $errorCode)")
            override fun onServiceUnregistered(info: NsdServiceInfo) = Unit
            override fun onUnregistrationFailed(info: NsdServiceInfo, errorCode: Int) = Unit
        }
        registration = l
        try {
            nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, l)
        } catch (e: Exception) {
            registration = null
            CoreLog.e(TAG, "could not announce this phone", e)
        }
    }

    private fun unregister() {
        registration?.let { runCatching { nsd.unregisterService(it) } } // sends the goodbye
        registration = null
    }

    // ----- finding the others ------------------------------------------------------------------

    private fun browse() {
        val l = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) = Unit
            override fun onDiscoveryStopped(serviceType: String) = Unit
            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) = CoreLog.w(TAG, "could not look for peers (error $errorCode)")
            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) = Unit

            override fun onServiceFound(info: NsdServiceInfo) {
                synchronized(lock) {
                    if (browsing !== this || info.serviceName == advert?.serviceName) return
                    if (!info.serviceName.startsWith("LightningLAN-")) return
                    toResolve.addLast(info)
                    if (!resolving) resolveNext()
                }
            }

            override fun onServiceLost(info: NsdServiceInfo) {
                val l = synchronized(lock) { listener.takeIf { browsing === this } } ?: return
                l.serviceLost(info.serviceName)
            }
        }
        browsing = l
        try {
            nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, l)
        } catch (e: Exception) {
            browsing = null
            CoreLog.e(TAG, "could not look for peers", e)
        }
    }

    /** One at a time: older Android versions refuse a second resolve while one is running. */
    @Suppress("DEPRECATION") // resolveService: the replacement needs Android 14
    private fun resolveNext() {
        val next = toResolve.removeFirstOrNull()
        if (next == null) {
            resolving = false
            return
        }
        resolving = true
        val session = browsing
        try {
            nsd.resolveService(next, object : NsdManager.ResolveListener {
                override fun onResolveFailed(info: NsdServiceInfo, errorCode: Int) {
                    synchronized(lock) {
                        if (browsing !== session) return
                        if (errorCode == NsdManager.FAILURE_ALREADY_ACTIVE) {
                            toResolve.addLast(info)
                            handler.postDelayed({ synchronized(lock) { if (browsing === session) resolveNext() } }, 500)
                        } else {
                            resolveNext()
                        }
                    }
                }

                override fun onServiceResolved(info: NsdServiceInfo) {
                    val l = synchronized(lock) {
                        if (browsing !== session) return
                        resolveNext()
                        listener
                    } ?: return
                    deliver(info, l)
                }
            })
        } catch (e: Exception) {
            CoreLog.w(TAG, "could not resolve ${next.serviceName}: $e")
            resolveNext()
        }
    }

    private fun deliver(info: NsdServiceInfo, listener: DiscoveryListener) {
        val txt = info.attributes.mapValues { (_, value) -> value?.toString(Charsets.UTF_8) ?: "" }
        val addresses = hostAddresses(info).filterIsInstance<Inet4Address>()
            .filter { !it.isLoopbackAddress && !it.isLinkLocalAddress }.mapNotNull { it.hostAddress }
        val record = parseServiceRecord(info.serviceName, txt, addresses, info.port) ?: return
        listener.serviceFound(record)
    }

    @Suppress("DEPRECATION") // NsdServiceInfo.host: hostAddresses needs Android 14
    private fun hostAddresses(info: NsdServiceInfo): List<InetAddress> =
        if (Build.VERSION.SDK_INT >= 34) info.hostAddresses else listOfNotNull(info.host)

    // ----- network changes ---------------------------------------------------------------------

    private fun watchNetwork() {
        if (networkCallback != null) return
        val callback = object : ConnectivityManager.NetworkCallback() {
            override fun onLinkPropertiesChanged(network: Network, linkProperties: LinkProperties) {
                val addresses = linkProperties.linkAddresses.map { it.address }.filterIsInstance<Inet4Address>()
                synchronized(lock) {
                    val before = lastAddresses
                    lastAddresses = addresses
                    if (before == null || before == addresses || advert == null) return
                }
                handler.removeCallbacks(restart)
                handler.postDelayed(restart, NETWORK_SETTLE_MS) // a Wi-Fi switch comes as several changes
            }
        }
        try {
            connectivity.registerDefaultNetworkCallback(callback, handler)
            networkCallback = callback
        } catch (e: Exception) {
            CoreLog.w(TAG, "cannot follow network changes: $e")
        }
    }
}
