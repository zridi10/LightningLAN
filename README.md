# LanShare

Peer-to-peer file sharing for everyone on the same local network. There is no server and
no account.

Each person runs LanShare and picks a folder to share. Peers find each other automatically
(mDNS/zeroconf), and everyone sees one combined **Common Space**: every file shared by every
online peer, with its size and who has it. Drag files into the web page to add them to your
shared folder. Click **Download** to fetch a file directly from the peer that has it.

- Automatic discovery on the LAN (`_lanshare._tcp.local.`), with no IP addresses to type.
- Identical files on several peers (same SHA-256) are shown once, with every owner listed.
- Downloads stream to disk, resume after an interruption (HTTP Range) and are verified with
  SHA-256 before they are saved. If the sending peer disappears, another peer with the same
  file takes over.
- Optional shared passphrase: peers without it can't see or download anything.
- Light and dark mode, sortable and filterable file list, progress bars and clear errors.

## Requirements

- Python 3.11 or newer ([python.org](https://www.python.org/downloads/); on Windows tick
  "Add python.exe to PATH").
- The computers must be on the same network segment (same Wi-Fi or wired LAN). mDNS does not
  cross routers, and guest Wi-Fi with "client isolation" blocks it.

## Quick start

The launch scripts create a virtual environment in `.venv`, install `requirements.txt` (only
on the first run and when it changes) and start LanShare. Your browser then opens
<http://localhost:8765>.

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
python -m lanshare --share <folder> --downloads <folder> --port 8765 --name "Zach's PC" --passphrase <optional>
```

On start, LanShare prints the local UI URL, the LAN address other peers use, and the shared
and downloads folders, then opens the UI in your default browser.

## Command-line options

| Option | Meaning |
| --- | --- |
| `--share FOLDER` | Folder to share. Default `~/LanShare/Shared`. |
| `--downloads FOLDER` | Where downloads are saved. Default `~/LanShare/Downloads`. It is kept separate from the shared folder unless you point both at the same place. |
| `--port PORT` | HTTP port for the UI and for other peers. Default `8765`. |
| `--name NAME` | Name other peers see. Default: the computer's hostname. |
| `--passphrase TEXT` | Shared secret. Only peers using the same passphrase are visible (see [Security](#security)). |
| `--config FILE` | Use a different config file (for example, to run two peers on one computer). |
| `--interface IP` | Advertise only on this local IPv4 address (repeatable). By default the LAN interfaces are detected automatically. |
| `--refresh-interval SECONDS` | How often peers are polled for changes. Default `3`. |
| `--max-upload-rate MB_PER_S` | Limit how fast each peer can download from you. Default: unlimited. |
| `--no-browser` | Don't open the browser on start. |
| `-v`, `--verbose` | Log every request and debug messages. |

`--share`, `--downloads`, `--port` and `--name` are **remembered** in the config file, so next
time `run.bat` or `./run.sh` without arguments uses the same settings. A passphrase given on the
command line is used for that run only and is never written to disk. To make it permanent, add
`"passphrase": "..."` to the config file yourself.

The config file holds the peer's unique ID, which is generated on first run. It is stored in:

- Windows: `%APPDATA%\LanShare\config.json`
- macOS: `~/Library/Application Support/LanShare/config.json`
- Linux: `$XDG_CONFIG_HOME/lanshare/config.json` (usually `~/.config/lanshare/config.json`)

A `hash_cache.json` next to it remembers file hashes, so a file is only re-hashed when its size
or modification time changes.

## Using it

- **Header**: your peer name, a lock badge when a passphrase is on, and how many peers are
  online.
- **Drop zone**: drag files or whole folders onto the page, or click to choose files. They are
  copied into your shared folder. If a name is taken, the copy is saved as `name (1).ext`,
  `name (2).ext`, and so on. Hidden and temporary files (names starting with `.`, `*.tmp`,
  `*.part`, `Thumbs.db`, ...) are skipped. You can also just copy files into the shared folder
  yourself; LanShare rescans it every few seconds.
- **Common Space**: click a column heading to sort, or type in the filter box. Files you
  already have say "In your share".
- **Downloads**: progress, speed and source, with Pause, Resume and Cancel. While a download
  runs, the data goes to `<downloads>/.lanshare-partial/<sha256>.part`. The file is renamed into
  place only after its SHA-256 matches, and a corrupted transfer is discarded. If the peer goes
  offline mid-transfer, the error says so, the partial file is kept, and **Resume** continues
  from where it stopped once the peer (or another peer with the same file) is back.
- **Peers**: everyone online with their file counts. Peers that were found but rejected (for
  example, a different passphrase) are listed separately with the reason.
- **Stop LanShare** (in the *Your shared folder* card) shuts this peer down. Ctrl+C in the
  terminal does the same.

Files only appear in the Common Space after they have been hashed. A very large shared folder
takes a while the first time; after that the hash cache makes restarts quick.

## Firewall notes

LanShare needs two kinds of traffic between the computers:

| Port | Direction | Used for |
| --- | --- | --- |
| TCP 8765 (or your `--port`) | inbound | other peers fetching your file list and downloading files |
| UDP 5353, multicast 224.0.0.251 | in and out | discovery (mDNS) |

### Windows (Defender Firewall)

On first start Windows shows *"Windows Defender Firewall has blocked some features of
Python"*. Tick **Private networks** and click **Allow access**. The prompt names `python.exe`
because LanShare runs inside Python. If you use the venv, the program allowed is the Python
installation the venv was created from.

If you clicked Cancel, or other computers still can't see you:

1. Make sure your network is set to **Private**: Settings → Network & internet → Wi-Fi or
   Ethernet → your network → Network profile type → Private. Windows blocks discovery on
   Public networks.
2. Allow the ports explicitly (PowerShell as Administrator):

   ```powershell
   New-NetFirewallRule -DisplayName "LanShare (TCP 8765)" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
   New-NetFirewallRule -DisplayName "LanShare discovery (UDP 5353)" -Direction Inbound -Protocol UDP -LocalPort 5353 -Action Allow -Profile Private
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
  LanShare from (Terminal, iTerm, VS Code, ...).

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
- **"cannot listen on port 8765"**: LanShare is already running, or another program uses the
  port. Start with `--port 8766`, which is then remembered.
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

The integration tests start real LanShare processes on random ports, which discover each other
through real mDNS on this computer. They include a 200 MB transfer, so leave about 1 GB of free
disk space for the temp folder. Temp files are deleted as each test passes. On a machine with
no network connection, use loopback for discovery: `LANSHARE_TEST_INTERFACE=127.0.0.1`.
Run `pytest -m "not integration"` for the fast unit tests only.

## Project layout

```
lanshare/
  cli.py         command line, startup banner, browser launch
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
