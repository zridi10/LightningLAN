package lightninglan.app

import android.app.Activity
import android.content.ComponentName
import android.content.Context
import android.content.pm.PackageManager
import android.nfc.NfcAdapter
import android.nfc.cardemulation.CardEmulation
import android.nfc.cardemulation.HostApduService
import android.os.Build
import android.os.Bundle
import lightninglan.core.CoreLog
import lightninglan.core.NdefTag
import lightninglan.core.ndefUriMessage

/*
 * The NFC tap (core NdefTag.kt): while the tap screen shows, this phone is an NFC tag holding its
 * join link, and the other phone reads it like any tag. The service is switched off the rest of
 * the time, so LightningLAN never answers a reader (a payment terminal, say) otherwise.
 */

private const val TAG = "nfc"

/** What a reader gets: Android hands this service the commands of a reader that asks for the NDEF application. */
class NfcTagService : HostApduService() {
    private val tag = NdefTag { NfcTag.message }

    override fun processCommandApdu(commandApdu: ByteArray, extras: Bundle?): ByteArray = tag.process(commandApdu)

    override fun onDeactivated(reason: Int) = tag.reset()
}

object NfcTag {
    /** The NDEF message readers get; none while nothing is handed out. */
    @Volatile
    var message: ByteArray? = null
        private set

    /** The phone can act as a tag (host card emulation): most phones with NFC can. */
    fun supported(context: Context): Boolean =
        context.packageManager.hasSystemFeature(PackageManager.FEATURE_NFC_HOST_CARD_EMULATION) && NfcAdapter.getDefaultAdapter(context) != null

    fun turnedOn(context: Context): Boolean = NfcAdapter.getDefaultAdapter(context)?.isEnabled == true

    /** The tap screen opened: Android takes a moment to route readers to the service. */
    fun open(context: Context) = setService(context, on = true)

    /** Hands [link] out to readers, until [unserve] or [close]. */
    fun serve(link: String) {
        message = ndefUriMessage(link)
    }

    fun unserve() {
        message = null
    }

    /** The tap screen closed (or the app started: a tap screen that never closed, after a crash). */
    fun close(context: Context) {
        message = null
        setService(context, on = false)
    }

    /**
     * While [activity] shows the tap screen: this service goes first, and on Android 15 the phone
     * stops looking for tags itself (two phones that both look find each other less reliably).
     */
    fun preferWhile(activity: Activity) {
        val adapter = NfcAdapter.getDefaultAdapter(activity) ?: return
        runCatching { CardEmulation.getInstance(adapter).setPreferredService(activity, component(activity)) }
            .onFailure { CoreLog.w(TAG, "could not put the tag first: $it") }
        if (Build.VERSION.SDK_INT >= 35) {
            runCatching { adapter.setDiscoveryTechnology(activity, NfcAdapter.FLAG_READER_DISABLE, NfcAdapter.FLAG_LISTEN_KEEP) }
                .onFailure { CoreLog.w(TAG, "could not stop looking for tags: $it") }
        }
    }

    fun stopPreferring(activity: Activity) {
        val adapter = NfcAdapter.getDefaultAdapter(activity) ?: return
        runCatching { CardEmulation.getInstance(adapter).unsetPreferredService(activity) }
        if (Build.VERSION.SDK_INT >= 35) runCatching { adapter.resetDiscoveryTechnology(activity) }
    }

    private fun component(context: Context) = ComponentName(context, NfcTagService::class.java)

    private fun setService(context: Context, on: Boolean) {
        val state = if (on) PackageManager.COMPONENT_ENABLED_STATE_ENABLED else PackageManager.COMPONENT_ENABLED_STATE_DISABLED
        try {
            val pm = context.packageManager
            if (pm.getComponentEnabledSetting(component(context)) != state) {
                pm.setComponentEnabledSetting(component(context), state, PackageManager.DONT_KILL_APP)
            }
        } catch (e: Exception) {
            CoreLog.w(TAG, "could not switch the tag ${if (on) "on" else "off"}: $e")
        }
    }
}
