# LightningLAN

Peer-to-peer file sharing for everyone on the same local network. There is no server and
no account.

Each person runs LightningLAN and picks a folder to share. Peers find each other automatically
(mDNS/zeroconf), and everyone sees one combined **Common Space**: every file shared by every
online peer, with its size and who has it. Drag files into the web page to add them to your
shared folder. Click **Download** to fetch a file directly from the peer that has it.

- Automatic discovery on the LAN (`_lightninglan._tcp.local.`), with no IP addresses to type.
- Identical files on several peers (same SHA-256) are shown once, with every owner listed.
- Downloads stream to disk, resume after an interruption (HTTP Range) and are verified with
  SHA-256 before they are saved. If the sending peer disappears, another peer with the same
  file takes over.
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
and the computer's name. You can change these later in the web page's Settings panel.

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
the other's files in the Common Space.

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
and downloads folders, then opens the UI in your default browser.

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
| `--max-upload-rate MB_PER_S` | Limit how fast each peer can download from you. Default: unlimited. |
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
  online.
- **Drop zone**: drag files or whole folders onto the page, or click to choose files. They are
  copied into your shared folder. If a name is taken, the copy is saved as `name (1).ext`,
  `name (2).ext`, and so on. Hidden and temporary files (names starting with `.`, `*.tmp`,
  `*.part`, `Thumbs.db`, ...) are skipped. You can also just copy files into the shared folder
  yourself; LightningLAN rescans it every few seconds.
- **Common Space**: click a column heading to sort, or type in the filter box. Files you
  already have say "In your share".
- **Downloads**: progress, speed and source, with Pause, Resume and Cancel. While a download
  runs, the data goes to `<downloads>/.lightninglan-partial/<sha256>.part`. The file is renamed into
  place only after its SHA-256 matches, and a corrupted transfer is discarded. If the peer goes
  offline mid-transfer, the error says so, the partial file is kept, and **Resume** continues
  from where it stopped once the peer (or another peer with the same file) is back.
- **Peers**: everyone online with their file counts. Peers that were found but rejected (for
  example, a different passphrase) are listed separately with the reason.
- **Settings** (the gear icon): your name, the shared and downloads folders (with a
  **Browse…** button that opens the system's folder chooser) and the passphrase. Changes apply
  immediately, without a restart. Peers see a new name within seconds. A new shared folder is
  indexed from scratch. Downloads already started finish in the old downloads folder. After a
  passphrase change, every peer is checked again.
- **Tray / menu bar icon**: Open LightningLAN (also what clicking the icon does on Windows),
  Open Shared Folder, Open Downloads Folder, Open Log Folder, Quit LightningLAN.
- **Stop LightningLAN** (in the *Your shared folder* card) shuts this peer down. Ctrl+C in the
  terminal does the same.

Files only appear in the Common Space after they have been hashed. A very large shared folder
takes a while the first time; after that the hash cache makes restarts quick.

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

- **What the LAN can reach.** Only two read-only endpoints: the file list (`GET /api/files`)
  and downloads of files inside your shared folder (`GET /api/download/<sha256>`). A download is
  looked up by hash in your own index, never by a path from the network. Everything else (the
  web UI, uploads, starting downloads, shutdown) only answers requests from this computer. It
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
- **A peer stays listed briefly after its computer disappears.** Peers that quit normally
  vanish immediately. A computer that drops off the network is removed after 3 failed checks,
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
  common.py      merging indexes into the Common Space
  serving.py     streaming files to peers, Range requests
  transfers.py   download manager: resume, verification, fail-over
  uploads.py     receiving files from the UI
  pathsafe.py    path traversal protection and file-name sanitising
  static/        the web UI (index.html, app.js, style.css)
tests/           unit tests and a multi-process integration harness
```

## Known limitations

- Traffic is unencrypted HTTP (see Security).
- Discovery only works within one network segment, and there is no "add a peer by IP address"
  option. IPv4 only.
- The downloads list is not kept across restarts, but partial files are: downloading the same
  file again resumes it.
- The shared folder is rescanned every few seconds rather than watched for changes, and a new
  file can only be downloaded once it has been hashed.
- Symlinked files are shared only if they point inside the shared folder. Symlinked folders and
  junctions are never followed.
- `--max-upload-rate` applies to each transfer separately, not to the total.
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
