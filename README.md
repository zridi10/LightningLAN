# LightningLAN

Peer-to-peer file sharing for everyone on the same local network. There is no server and
no account.

Each person runs LightningLAN and picks a folder to share. Peers find each other automatically
(mDNS/zeroconf), and everyone sees one combined **ThunderCLOUD**: every file shared by every
online peer, with its size and who has it. Drag files into the web page to add them to your
shared folder. Click **Download** to fetch a file directly from the peer that has it.

- Automatic discovery on the LAN (`_lightninglan._tcp.local.`), with no IP addresses to type.
- Identical files on several peers (same SHA-256) are shown once, with every owner listed.
- Swarm downloads: when several people download the same file, they pass pieces to each other
  instead of all queuing at the person sharing it, so it stays fast for everyone (see
  [Swarm downloads](#swarm-downloads)).
- Downloads stream to disk, resume after an interruption and are verified piece by piece and
  then as a whole (SHA-256) before they are saved. If a sending peer disappears, the others
  carry on.
- Phones too: the Android app is a full peer that shares the phone's own files, and any phone
  (Android or iPhone) can scan a QR code to download from ThunderCLOUD and send photos in its
  browser, with nothing to install (see [Phones](#phones)).
- Optional shared passphrase: peers without it can't see or download anything.
- Light and dark mode, sortable and filterable file list, progress bars and clear errors.
- Zero setup: default folders and name, a free port, a tray / menu bar icon, and a Settings
  panel in the web page for everything else.

**Just want to use it?** Download the app for Windows or Linux from the
[latest release](https://github.com/zridi10/LightningLAN/releases/latest) and follow [INSTALL.md](INSTALL.md). No Python needed. The rest of this README
is about running from source, building the app and publishing releases.

## Requirements (running from source)

- Python 3.11 or newer ([python.org](https://www.python.org/downloads/); on Windows tick
  "Add python.exe to PATH").
- The computers must be on the same network segment (same Wi-Fi or wired LAN). mDNS does not
  cross routers, and guest Wi-Fi with "client isolation" blocks it.

## Quick start

The launch scripts create a virtual environment in `.venv`, install `requirements.txt` (only
on the first run and when it changes) and start LightningLAN. Your browser then opens
<http://localhost:8765>, and a lightning-bolt icon appears in the system tray (menu bar on
macOS). With no arguments, the first run uses `~/LightningLAN/Shared` and `~/LightningLAN/Downloads`
and a random funny name (say, "Captain Bandwidth"). You can change these later: the name with
the pencil next to it at the top of the page, everything in the Settings panel.

**Windows:** double-click `run.bat`, or from a terminal:

```bat
run.bat --share "D:\Stuff to share" --name "Den PC"
```

In PowerShell, type `.\run.bat` instead of `run.bat`.

**macOS / Linux:**

```bash
chmod +x run.sh   # once, if your copy lost the executable bit
./run.sh --share ~/Shared --name "Laptop"
```

Do the same on a second computer and the two find each other within a few seconds. Each sees
the other's files in ThunderCLOUD.

On first start your OS firewall will probably ask whether to allow Python on the network.
**Allow it on private networks**, or other computers won't be able to reach you. See
[Firewall notes](#firewall-notes) below.

### Manual install

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
python -m lightninglan --share <folder> --downloads <folder> --port 8765 --name "Zach's PC" --passphrase <optional>
```

On start, LightningLAN prints the local UI URL, the LAN address other peers use, and the shared
and downloads folders, then opens the UI in your default browser. The packaged Windows app opens
it in a window of its own instead (Edge WebView2, through pywebview; `lightninglan/window.py`),
and falls back to the browser where WebView2 is missing.

## Command-line options

| Option | Meaning |
| --- | --- |
| `--share FOLDER` | Folder to share. Default `~/LightningLAN/Shared`. |
| `--downloads FOLDER` | Where downloads are saved. Default `~/LightningLAN/Downloads`. It is kept separate from the shared folder unless you point both at the same place. |
| `--port PORT` | HTTP port for the UI and for other peers. Default `8765`. If that port is taken, the next free one is used, unless you gave `--port`. |
| `--name NAME` | Name other peers see. Default: the computer's hostname. |
| `--passphrase TEXT` | Shared secret. Only peers using the same passphrase are visible (see [Security](#security)). |
| `--config FILE` | Use a different config file (for example, to run two peers on one computer). |
| `--interface IP` | Advertise only on this local IPv4 address (repeatable). By default the LAN interfaces are detected automatically. |
| `--refresh-interval SECONDS` | How often peers are polled for changes. Default `3`. |
| `--max-upload-rate MB_PER_S` | Limit your total upload to other peers (all transfers together). Default: unlimited. |
| `--no-browser` | Don't open the browser on start. |
| `--no-tray` | No tray icon and no pop-up error messages, for servers and scripts. The tray is also skipped automatically when there is no desktop session. |
| `-v`, `--verbose` | Log every request and debug messages. |

`--share`, `--downloads`, `--port` and `--name` are **remembered** in the config file, so next
time `run.bat` or `./run.sh` without arguments uses the same settings. A passphrase given on the
command line is used for that run only and is never written to disk. To make it permanent, set
it in the web page's **Settings** panel, which saves the name, folders and passphrase to the
config file. The file is readable only by your user account.

The config file holds the peer's unique ID, which is generated on first run. It is stored in:

- Windows: `%APPDATA%\LightningLAN\config.json`
- macOS: `~/Library/Application Support/LightningLAN/config.json`
- Linux: `$XDG_CONFIG_HOME/lightninglan/config.json` (usually `~/.config/lightninglan/config.json`)

Next to it:

- `hash_cache.json` remembers file hashes, so a file is only re-hashed when its size or
  modification time changes.
- `logs/lightninglan.log` is the log, rotated at 1 MB with 3 old copies kept. Open it from the tray
  menu (**Open Log Folder**) or from Settings.
- `lightninglan.lock` and `instance.json` keep LightningLAN to one copy per config file. Starting it
  again while it runs just opens the running copy's web page. `--config` with a different file
  starts an independent peer.

Nothing is ever written next to the program itself, so the packaged app runs from read-only
locations too.

## Using it

- **Header**: your peer name, a lock badge when a passphrase is on, and how many peers are
  online. The pencil next to your name changes it right there; the dice in the editor
  suggests another funny name (one nobody online is using). A peer that never picked a name
  gets a random funny one on first start, which it keeps.
- **Themes**: the half-circle button at the top right cycles through automatic, light, dark
  and three animated themes: **Synthwave** (an 80s neon sunset with a rolling grid), **Bliss**
  (sunny hills and drifting clouds, with a nod to Windows XP) and **Deep Sea** (fish, bubbles
  and swaying seaweed). The choice is remembered by the browser. With "reduce motion" turned
  on in the system settings, the scenes stand still.
- **Phone button** (top right): a QR code that opens LightningLAN on a phone. See [Phones](#phones).
- **Drop zone**: drag files or whole folders onto the page, or click to choose files. They are
  copied into your shared folder. If a name is taken, the copy is saved as `name (1).ext`,
  `name (2).ext`, and so on. Hidden and temporary files (names starting with `.`, `*.tmp`,
  `*.part`, `Thumbs.db`, ...) are skipped. You can also just copy files into the shared folder
  yourself; LightningLAN rescans it every few seconds.
- **ThunderCLOUD** (every file on the network in one list): click a column heading to sort, or
  type in the filter box. Files you already have say "In your share".
- **Downloads**: progress, speed and sources, with Pause, Resume and Cancel. Each active
  download shows a spinning Nyan Cat, and so does the top bar while anything downloads. Point
  at the cat for a pause sign: a click pauses, another click resumes (the top-bar cat pauses or
  resumes all downloads). Up to three downloads run at once; more wait as *queued*. While a download runs, the data goes to
  `<downloads>/.lightninglan-partial/<sha256>.part`. The file is renamed into place only after
  its SHA-256 matches, and a corrupted transfer is discarded. If every peer with the file goes
  offline mid-transfer, the error says so, the verified pieces are kept, and **Resume**
  continues once a peer with the file is back.
- **Peers**: everyone online with their file counts. Peers that were found but rejected (for
  example, a different passphrase) are listed separately with the reason.
- **Settings** (the gear icon): your name, the shared and downloads folders (with a
  **Browse…** button that opens the system's folder chooser) and the passphrase. Changes apply
  immediately, without a restart. Peers see a new name within seconds. A new shared folder is
  indexed from scratch. Downloads already started finish in the old downloads folder. After a
  passphrase change, every peer is checked again.
- **Tray / menu bar icon**: Open LightningLAN (also what clicking the icon does on Windows),
  Open Shared Folder, Open Downloads Folder, Open Log Folder, Quit LightningLAN.
- **Stop LightningLAN** (in the *Your shared folder* card) takes this computer off the network
  without quitting. Other peers stop seeing it, transfers to them are cut off, and your
  downloads pause. **Start LightningLAN** brings it back and resumes those downloads once a
  peer with the file is found again. To quit, use **Quit completely** on the banner shown
  while stopped, the tray icon's **Quit LightningLAN**, or Ctrl+C in the terminal.
- **Closing the page** works like Stop: about 10 seconds after the last LightningLAN tab
  closes, this computer goes offline. A reload doesn't count. Opening the page again, from the
  tray icon or by starting LightningLAN again, brings it back online. If you pressed Stop
  yourself, it stays stopped until you press Start. Browsers may put a long-unused background
  tab to sleep, which counts as closing it. Started with `--no-browser` and never opened, it
  stays online. In the Windows app's own window, closing the window counts as closing the page:
  the window hides, LightningLAN stays in the tray, and opening it again shows the window.

Files only appear in ThunderCLOUD after they have been hashed. A very large shared folder
takes a while the first time; after that the hash cache makes restarts quick.

## Phones

There are two ways: the Android app, and the phone page in any phone's browser.

### The Android app

The app in [`android/`](android/README.md) is a full peer, like a computer running LightningLAN:
it finds the other peers by itself, shares a folder of its own (plus single files picked with
**Share files**), downloads from everyone with swarms and resume, passes pieces on, and uses
the same passphrase. Computers see the phone's files in ThunderCLOUD and the phone sees
theirs; no computer has to relay anything. It shares in the background (with a notification
and a **Stop** button) until you stop it. For now it is built from source: see
[android/README.md](android/README.md).

### The phone page (any phone, in the browser)

A phone can also join through a computer running LightningLAN, in the phone's own browser.
There's no app to install, so Android and iPhone both work.

1. On the computer, click the phone button at the top right. It shows a QR code (and the same
   link as text) for each network address the computer has.
2. Scan it with the phone's camera on the same Wi-Fi. The link holds a random key (144 bits),
   which the phone keeps in a cookie, so it stays connected and can bookmark the page.
3. The phone page lists ThunderCLOUD. **Get** downloads a file to the phone. Files this computer
   shares are sent directly. Files that only other peers have are relayed from one of them as
   they arrive (with the passphrase, if one is set), and their SHA-256 is checked on the way:
   if the file doesn't match, the phone's download fails rather than finishing with damaged data.
   **Send files** puts photos and other files into this computer's shared folder, so every peer
   sees them.

An open phone page counts like an open LightningLAN tab: the computer keeps sharing while it is
open, even if the computer's own page is closed. The phone button shows a badge while phones
are connected. **Disconnect phones** in the QR code window replaces the key, so phones need the
new code. The key lives in the config file. Typing the computer's address
(`http://<ip>:8765`) on a phone also leads to the phone page.

The phone page isn't a full peer: it only downloads and sends through the computer, and only
while that computer runs LightningLAN. On Android, the app does all of that by itself.

## Swarm downloads

Files larger than 1 MB are split into pieces of 1–16 MB (at most about 2,000 pieces per file),
and the sharer's index holds a SHA-256 for every piece as well as for the whole file. A download
fetches up to 8 pieces at a time from every peer that has them:

- **Sharers** (peers with the file in their shared folder) send byte ranges of it.
- **Fellow downloaders** pass on the pieces they have already verified, while they are still
  downloading. Each one publishes which pieces it has and which it is fetching.
- **Finished downloads** keep passing pieces on while LightningLAN runs (and isn't stopped) and
  the download is still in the Downloads list. Removing it from the list, clearing finished downloads, or
  changing the saved file stops that.

Usually the sharer's upload is the bottleneck. So while some pieces exist only at the sharer, a
downloader asks it only for those, and gets everything else from the fellow downloaders. On
this PC, 10 downloaders starting a 100 MB file at once from a sharer capped at 10 MB/s all
finished in 14–18 seconds. Without swarming each would take at least 100 seconds, and a single
download alone takes 10. Those peers all ran on one computer, so their uploads to each other
weren't limited; on a real network, each downloader's own upload speed matters too. On one
shared Wi-Fi network the gain is smaller, because every transfer uses the same radio channel.

Every piece is checked against its hash before it is kept or passed on. A peer that sends two
bad pieces is dropped for the rest of the download. If pieces keep failing against the piece
list itself, the download falls back to fetching the file whole from one sharer. Peers running
LightningLAN 1.0 still work: they download whole files from 1.1 peers, and serve whole files to
them.

## Firewall notes

LightningLAN needs two kinds of traffic between the computers:

| Port | Direction | Used for |
| --- | --- | --- |
| TCP 8765 (or your `--port`) | inbound | other peers fetching your file list and downloading files |
| UDP 5353, multicast 224.0.0.251 | in and out | discovery (mDNS) |

### Windows (Defender Firewall)

On first start Windows shows *"Windows Defender Firewall has blocked some features of
Python"* (or *"of LightningLAN"* for the packaged `LightningLAN.exe`). Tick **Private networks** and
click **Allow access**. When running from source, the prompt names `python.exe` because
LightningLAN runs inside Python. If you use the venv, the program allowed is the Python
installation the venv was created from. For the packaged app the rule belongs to that copy of
`LightningLAN.exe`, so moving the file brings the prompt back.

If you clicked Cancel, or other computers still can't see you:

1. Make sure your network is set to **Private**: Settings → Network & internet → Wi-Fi or
   Ethernet → your network → Network profile type → Private. Windows blocks discovery on
   Public networks.
2. Allow the ports explicitly (PowerShell as Administrator):

   ```powershell
   New-NetFirewallRule -DisplayName "LightningLAN (TCP 8765)" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
   New-NetFirewallRule -DisplayName "LightningLAN discovery (UDP 5353)" -Direction Inbound -Protocol UDP -LocalPort 5353 -Action Allow -Profile Private
   ```

   Or open *Windows Defender Firewall → Allow an app through firewall* and tick **Private** for
   Python.

### macOS

- The application firewall may ask *"Do you want the application "Python" to accept incoming
  network connections?"*. Click **Allow**. To change it later: System Settings → Network →
  Firewall → Options.
- macOS 15 (Sequoia) and later also ask for **Local Network** access, for example *""Terminal"
  would like to find devices on your local network"*. Click **Allow**. If peers never appear,
  check System Settings → Privacy & Security → Local Network and enable the app you started
  LightningLAN from (Terminal, iTerm, VS Code, ...).

### Linux

Most desktop distributions allow this traffic by default. If you run a firewall:

```bash
# ufw
sudo ufw allow 8765/tcp
sudo ufw allow 5353/udp
# firewalld
sudo firewall-cmd --permanent --add-port=8765/tcp --add-service=mdns && sudo firewall-cmd --reload
```

## Security

- **What the LAN can reach.** Only read-only endpoints: the file list (`GET /api/files`),
  downloads of files inside your shared folder (`GET /api/download/<sha256>`), and the swarm
  endpoints: a shared file's piece hashes (`/api/pieces/<sha256>`), which pieces you hold of a
  file you are downloading (`/api/swarm/<sha256>`), and those pieces themselves
  (`/api/piece/<sha256>/<n>`). Files are always looked up by hash in your own index or download
  list, never by a path from the network. While LightningLAN is stopped (or its page is
  closed), all of these answer "503 offline", and transfers in progress are cut off.
- **What swarming reveals.** Your file list includes the hashes of files you are downloading or
  have downloaded (while they are in the Downloads list), so other peers can tell which shared
  files you fetched. Pieces of those files are served only to peers asking for them by hash,
  that is, peers who can already see the file in someone's share.
- **Phones.** The phone page (`/phone`) and the pages' scripts, styles and pictures
  (`/static/`) are open to the LAN; neither holds anything private. The phone API
  (`/phone/api/...`: ThunderCLOUD, downloads, sending files into the share) needs the key from
  the QR code, kept by the phone as an HttpOnly, SameSite=Lax cookie. Requests that change
  something also need a custom header and a same-origin `Origin`. Anyone who gets the key, by
  scanning the code or capturing traffic, can use the phone page until **Disconnect phones** is
  pressed. The computer's own API stays closed to phones.
- **This computer only.** Everything else (the web UI, uploads, starting downloads, shutdown,
  the QR code) only answers requests from this computer. It
  checks the client address is loopback and the `Host` header is `localhost`, which defeats DNS
  rebinding. State-changing requests also need a custom header and a same-origin `Origin`, so
  other websites you visit can't post files into your share.
- **Without a passphrase, anyone on your network can list and download your shared files.**
  Share accordingly, or set a passphrase.
- **Passphrase.** Every peer-to-peer request carries an HMAC-SHA256 token bound to the method,
  path, a timestamp and a single-use nonce. The key is derived from the passphrase with PBKDF2.
  The server answers with a proof derived from the same key, so both sides verify each other,
  and the passphrase itself never crosses the network. Peers with a different passphrase or none
  are ignored, and the Peers card shows why. The two computers' clocks must agree within
  5 minutes.
- **Traffic is not encrypted.** It is plain HTTP on your LAN. The passphrase controls who can
  fetch files, but someone capturing network traffic could still see file contents in transit.
- **Path safety.** Every path from outside (upload names, file names in other peers' indexes)
  is validated and resolved. Absolute paths, `..`, Windows drive and UNC paths, alternate data
  streams, reserved device names (`CON`, `NUL`, ...) and symlinks or junctions that lead outside
  the shared folder are rejected. Names from other peers are reduced to a safe single file name
  before anything is saved.
- **Display safety.** File and peer names are rendered as plain text only, never as HTML, and
  bidirectional-override characters are stripped. The page is served with a strict
  Content-Security-Policy.

## Troubleshooting

- **No peers appear.** Check the firewall prompts above, that both computers are on the same
  network and the network is Private on Windows, that no VPN is capturing traffic, and that the
  passphrases match (a mismatch is listed under *Not connected* in the Peers card). Guest Wi-Fi
  often isolates clients from each other.
- **"cannot listen on port 8765"** only happens with an explicit `--port`. Another program
  uses that port, so pick another one. Without `--port`, LightningLAN moves to the next free port
  by itself.
- **Something else went wrong**: check `logs/lightninglan.log` in the config folder (tray menu →
  **Open Log Folder**).
- **A peer stays listed briefly after its computer disappears.** Peers that quit or stop
  normally vanish immediately. A computer that drops off the network is removed after 3 failed checks,
  typically 10–20 seconds.
- **A download stopped with "went offline during the transfer".** Press **Resume** once the
  peer is back. The partial file is kept, and only the rest is transferred.

## Running the tests

```bash
.venv\Scripts\python -m pytest            # Windows
.venv/bin/python -m pytest                # macOS / Linux
```

The integration tests start real LightningLAN processes on random ports, which discover each other
through real mDNS on this computer. They include a 200 MB transfer, so leave about 1 GB of free
disk space for the temp folder. Temp files are deleted as each test passes. On a machine with
no network connection, use loopback for discovery: `LIGHTNINGLAN_TEST_INTERFACE=127.0.0.1`.
Run `pytest -m "not integration"` for the fast unit tests only.

The Android app's tests run with Gradle from `android/` (`gradlew :core:test`); they include
interop tests that run the phone's protocol code against desktop peers started from `.venv`.
See [android/README.md](android/README.md).

## Building the desktop app

`build.py` packages LightningLAN with PyInstaller for the OS it runs on:

```bash
python build.py               # Windows: py build.py
python build.py --smoke-test  # also start the result and check the UI, indexing and shutdown
```

| OS | Result |
| --- | --- |
| Windows | `dist/LightningLAN.exe`, one file with no console window, an icon and version information |
| macOS | `dist/LightningLAN-macOS.zip`, containing `LightningLAN.app`, a menu bar app with no Dock icon |
| Linux | `dist/LightningLAN-linux-x86_64`, one file |

If the Python running `build.py` lacks the requirements, it creates or reuses `.venv` and
installs `requirements.txt` and `requirements-build.txt` there. The PyInstaller settings are in
`packaging/lightninglan.spec`. The web UI files are bundled inside the app and found through
`lightninglan/resources.py`. You can only build for the OS you are on. The release workflow builds
all three.

The macOS build targets Apple silicon only (arm64): several dependencies publish separate Intel
and Apple wheels rather than universal2 ones, so a universal2 build isn't practical. The Linux
binary runs on the glibc version it was built with or newer. CI builds it on Ubuntu 22.04
(glibc 2.35).

## Publishing a release

`.github/workflows/release.yml` builds on native Windows and Linux runners, runs the unit tests
and the smoke test on each, and attaches `LightningLAN.exe`, `LightningLAN-linux-x86_64` and
`SHA256SUMS.txt` to a GitHub Release. A macOS entry is in the workflow but commented out
until the Mac app has been tested on a Mac.

1. Set `__version__` in `lightninglan/__init__.py` (for example `1.0.0`) and commit.
2. Tag and push: `git tag v1.0.0 && git push origin v1.0.0`. The workflow fails early if the tag
   and `__version__` differ.

To try the builds without releasing, run the workflow from the Actions tab (**Run workflow**).
The files are then attached to the run as artifacts. The apps are not code-signed.
[INSTALL.md](INSTALL.md) walks users through the SmartScreen warning.

## Project layout

```
build.py         builds the desktop app with PyInstaller
packaging/       PyInstaller spec and entry script
.github/         release workflow (builds and publishes on tag push)
lightninglan/
  cli.py         entry point: single instance, port choice, startup banner, browser, tray
  window.py      the Windows app's own window (pywebview, Edge WebView2)
  tray.py        system tray / menu bar icon (pystray)
  desktop.py     file manager, browser, message boxes, folder chooser
  instance.py    one running copy per config folder
  logs.py        console and log file setup
  icon.py        the app icon, drawn with Pillow
  resources.py   locating bundled files (source checkout or PyInstaller app)
  config.py      config file, peer ID, remembered settings
  node.py        wires the parts of a running peer together
  server.py      HTTP routes (FastAPI)
  security.py    LAN vs. localhost access rules, CSRF/DNS-rebinding checks, security headers
  auth.py        passphrase HMAC tokens and proofs
  discovery.py   zeroconf advertising and browsing
  netutil.py     choosing which network interfaces to advertise on
  peers.py       peer table, polling and health checks
  indexer.py     scanning the shared folder, SHA-256 hash cache
  common.py      merging indexes into ThunderCLOUD
  names.py       the funny names given to peers that haven't picked one
  phone.py       phones: QR code links, relaying other peers' files with SHA-256 checks
  serving.py     streaming files and pieces to peers, Range requests, the upload limit
  transfers.py   download manager: queue, resume, verification, fall-back to whole files
  swarm.py       swarm downloads: choosing pieces and peers, passing pieces on
  pieces.py      piece sizes, piece hashes, have-maps
  uploads.py     receiving files from the UI
  pathsafe.py    path traversal protection and file-name sanitising
  static/        the web UI (index.html, app.js, style.css; themes/ holds the theme artwork)
                 and the phone page (phone.html, phone.js)
tests/           unit tests and a multi-process integration harness
android/         the Android app (Kotlin): see android/README.md
  core/          the protocol, ported from lightninglan/ and tested against it
  app/           screens, background sharing service, discovery, folder access
```

## Known limitations

- Traffic is unencrypted HTTP (see Security).
- The Android app is built from source for now (it isn't in the release downloads or on Google
  Play). iPhones only have the phone page, so they can't share a folder of their own or
  download while no computer runs LightningLAN.
- Discovery only works within one network segment, and there is no "add a peer by IP address"
  option. IPv4 only.
- The downloads list is not kept across restarts, but partial files are: downloading the same
  file again resumes it.
- The shared folder is rescanned every few seconds rather than watched for changes, and a new
  file can only be downloaded once it has been hashed.
- Symlinked files are shared only if they point inside the shared folder. Symlinked folders and
  junctions are never followed.
- Swarming helps only peers downloading the same file around the same time, or downloading it
  after someone finished and still lists it. A new download can't start from fellow downloaders
  alone: at least one peer must still share the whole file.
- Downloaded files keep their name but not their folder: everything lands directly in the
  downloads folder.
- Files with the same name but different content show up as separate rows.
- The packaged apps are not code-signed, so Windows SmartScreen and macOS Gatekeeper warn on
  first launch (see [INSTALL.md](INSTALL.md)). Antivirus programs sometimes flag one-file
  PyInstaller executables by mistake.
- There is no macOS download yet. `build.py` can build an Apple silicon app on a Mac, but it
  hasn't been tested there. On a Mac, run from source.
- On Linux the tray icon uses the classic X11 system tray. It shows on KDE, Xfce, Cinnamon and
  MATE, and on GNOME with the AppIndicator extension. Clicking it opens the web page; it has no
  menu there. Without a tray, run the binary again or open <http://localhost:8765>.
- LightningLAN does not add itself to login items. [INSTALL.md](INSTALL.md) shows how to do it by
  hand.
