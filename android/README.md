# LightningLAN for Android

A native Android app that is a full LightningLAN peer: it finds the other peers on the Wi-Fi by
itself, shares a folder (and single files) of its own, downloads from everyone, takes part in
swarm downloads and passes pieces on, and honours the passphrase. It speaks the same protocol as
the desktop app, so phones and computers see each other's files in ThunderCLOUD.

Android 8.0 (API 26) or newer. iPhones (and phones without the app) use the phone page in their
browser instead, served by a computer running LightningLAN or by this app.
To install it, download `LightningLAN-android.apk` from the
[latest release](https://github.com/zridi10/LightningLAN/releases/latest) (see
[INSTALL.md](../INSTALL.md#android)).

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
is signed with the keystore that the `LIGHTNINGLAN_KEYSTORE` (its path) and
`LIGHTNINGLAN_KEYSTORE_PASSWORD` environment variables name, key alias `lightninglan`; without
them the APK is left unsigned. The release workflow signs the published APK with the project's
key (kept in repository secrets, see "Publishing a release" in the main README). An app signed
with another key, such as a debug build, can't update it or be updated by it: switching means
uninstalling the app first.

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

- **Folders.** Nothing to choose on first start, like the desktop app: the app makes
  `Download/LightningLAN/Shared` and `Download/LightningLAN/Downloads` on the phone's storage and
  starts sharing (`LocalFolders.kt`, plain file paths). Android 11 and later allow that without
  asking; Android 8 to 10 ask for the storage permission first, and without it the folders go in
  the app's own storage. Partial downloads live in `Downloads/.lightninglan-partial` and are moved
  into place once verified; finished files show up in the Files app and galleries. Android only
  lets the app see the files it put in its folders itself (downloads, and files sent to it from
  other apps' Share menus), not ones a file manager copies in: share those with **Share files**,
  or pick a different folder in Settings (through Android's folder chooser, which gives full access).
- **Share files** shares picked files from where they are (no copy); **Unshare** stops that.
  Files sent to LightningLAN from other apps' Share menus are copied into the Shared folder.
- **In the background**, a foreground service with a notification keeps sharing, holding a wake
  lock and a Wi-Fi lock so computers can reach the phone with the screen off. **Stop** (in the
  app or the notification) leaves the network and ends the service.
- The peer-to-peer server listens on port 8765 (or the next free one), like the desktop app.
- **The phone page**: the same server serves the desktop app's phone page (`core/.../PhoneApi.kt`,
  the same routes, cookie and checks as `lightninglan/server.py` and `security.py`), so a phone
  without the app scans the QR code behind the phone button and uses LightningLAN in its browser
  through this phone. The page's files are copied from `lightninglan/static` into the app's
  assets at build time (`app/build.gradle.kts`), so the two never drift apart; the interop tests
  check that the games reach the page in the desktop app's format. Files only other peers have
  are relayed with their SHA-256 checked, files sent from the page go into the shared folder,
  and the key is kept in the app's settings. The QR code is drawn from ZXing's encoder.
- **The hotspot** (`Hotspot.kt`): for phones with no Wi-Fi in common, the phone dialog starts
  Android's local-only hotspot (`WifiManager.startLocalOnlyHotspot`): no internet sharing, no
  carrier check, no SIM needed. Android picks its name and a random password each time; the dialog
  shows them as a `WIFI:` QR code (`wifiQrText` in core), then the phone page's code on the
  hotspot's address (the address the phone didn't have before it started). It needs "Nearby
  devices" on Android 13 and later (declared `neverForLocation`), or the location permission and
  Location switched on before that. Android only starts one for the app on screen; the sharing
  service keeps the process, and so the hotspot, running until **Stop hotspot**, **Stop** or the app
  goes away. Most phones can't be on a Wi-Fi and host at once, so they leave their Wi-Fi meanwhile.
  Computers can join it too, but nothing helps them find the phone there if mDNS doesn't get
  through; the phone page always works.
- **Phones that both have the app** (`core/.../Join.kt`, `Join.kt`, `NfcTag.kt`,
  `ui/ConnectDialogs.kt`), from the **⋮** menu: the phone with the hotspot shows one join link,
  `https://lightninglan.party/join/#v=1&s=<Wi-Fi>&p=<password>&h=<address>&o=<port>&i=<peer id>&n=<name>`,
  as a QR code or as an NFC tag. The other phone opens it in the app: an App Link (verified
  against `docs/.well-known/assetlinks.json`, so only the release signing key's builds open it
  straight from the camera; other builds go through the site's join page, `docs/join/`), an
  `NDEF_DISCOVERED` filter for the tag, or `lightninglan://join#…` from the join page. It asks
  for the hotspot with a `WifiNetworkSpecifier` request (Android 10 and later; Android shows its
  own "Connect?" prompt), binds the process to that network, since it has no internet, and calls
  `POST /api/introduce` on the other phone every 30 seconds. Each side then polls the other as a
  peer met directly (`PeerTable.meet`), forgotten after about two minutes out of reach. The tag
  is an NFC Forum Type 4 tag (`core/.../NdefTag.kt`) behind a `HostApduService` that is switched
  on only while the tap screen shows; on Android 15 that screen also stops the phone polling for
  tags itself, so the other phone's reader finds it. Before Android 10, the person joins the
  hotspot in the Wi-Fi settings and the app connects once the phone is on it.
- **While you wait** (the second tab) has the desktop app's ten head-to-head games, played against
  computers and other phones alike (`core/.../Games.kt` and `Play.kt`, the same rules and records
  as `lightninglan/games.py` and `play.py`), and the solo Falling blocks game (`ui/BlocksGame.kt`).
  Invitations arriving while the app is closed show as notifications.
- **The tutorial** (`ui/Tutorial.kt`) is the desktop app's tour, drawn in Compose: it dims the
  screen and spotlights the buttons tagged with `Modifier.tutorialTarget(...)`, one step at a time,
  and asks for a tap on the two tabs. It plays by itself once, after the first setup (a new
  install; phones that had the app before it don't get it by themselves), and from Settings.
- **Themes** are the desktop app's: the animated Synthwave (the default), Bliss and Deep Sea
  scenes (drawn in `ui/Scenes.kt` after `static/themes`), and plain light and dark, picked in
  Settings. With animations turned off in Android's settings, the scenes stand still.
- **Names**: a made-up name that another device also has is swapped for a free one by itself
  (the same rule as the desktop app); a name picked in Settings never changes.
