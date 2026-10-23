# LightningLAN for Android

A native Android app that is a full LightningLAN peer: it finds the other peers on the Wi-Fi by
itself, shares a folder (and single files) of its own, downloads from everyone, takes part in
swarm downloads and passes pieces on, and honours the passphrase. It speaks the same protocol as
the desktop app, so phones and computers see each other's files in ThunderCLOUD.

Android 8.0 (API 26) or newer. iPhones can use the desktop app's phone page in the browser instead.

## Layout

```
core/   the LightningLAN protocol in plain Kotlin (no Android classes): passphrase tokens, piece
        hashes, the file index, the peer-to-peer HTTP server, peer polling, ThunderCLOUD,
        downloads and swarms. Ported from lightninglan/*.py; runs and is tested on a desktop JVM.
app/    the Android app: screens (Jetpack Compose), the background sharing service, discovery
        (Android's NsdManager, mDNS like the desktop app), and folders through the Storage
        Access Framework
```

## Building

You need a JDK (17 or 21) and the Android SDK with platform 35 and build tools 34 (Android
Studio installs both; so does the command-line `sdkmanager`). Tell Gradle where the SDK is, in
`android/local.properties` (not committed):

```
sdk.dir=C\:/Users/you/AppData/Local/Android/Sdk
```

Then, from this folder:

```bash
gradlew assembleDebug        # Windows: gradlew.bat assembleDebug
```

The app is `app/build/outputs/apk/debug/app-debug.apk`. Install it with
`adb install -r app/build/outputs/apk/debug/app-debug.apk`, or copy it to the phone and open it
there (Android asks you to allow installing apps from that source once).

Debug builds are signed with the SDK's debug key. A release build (`gradlew assembleRelease`)
needs a signing key of your own, set up in `app/build.gradle.kts`; switching from a debug build
to a release build means uninstalling the app first.

The app's version is read from `lightninglan/__init__.py`, so it always matches the desktop app.

## Tests

```bash
gradlew :core:test           # protocol tests, plus interop tests against the desktop app
gradlew :app:lintDebug       # Android lint, including the core code against Android's API levels
```

The interop tests (`InteropTest`) start real desktop peers with the repository's `.venv` Python
(or the one in `LIGHTNINGLAN_PYTHON`) and run the phone's code against them on 127.0.0.1:
listing and downloading both ways, swarms, passphrases, Stop/Start and renaming. They are
skipped when no such Python environment exists.

## How it works on the phone

- **Folders.** On first start you pick one folder (for example Download/LightningLAN); the app
  makes `Shared` and `Downloads` folders inside it, like the desktop app's defaults. Partial
  downloads live in `Downloads/.lightninglan-partial` and are moved into place once verified.
  Settings can point either folder anywhere else.
- **Share files** shares picked files from where they are (no copy); **Unshare** stops that.
  Files sent to LightningLAN from other apps' Share menus are copied into the Shared folder.
- **In the background**, a foreground service with a notification keeps sharing, holding a wake
  lock and a Wi-Fi lock so computers can reach the phone with the screen off. **Stop** (in the
  app or the notification) leaves the network and ends the service.
- The peer-to-peer server listens on port 8765 (or the next free one), like the desktop app.
- **Themes** are the desktop app's: automatic, light, dark, and the animated Synthwave, Bliss
  and Deep Sea scenes (drawn in `ui/Scenes.kt` after `static/themes`). The theme button in the
  title bar cycles through them; Settings lists them all. With animations turned off in
  Android's settings, the scenes stand still.
