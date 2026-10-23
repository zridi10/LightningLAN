# Installing LightningLAN

LightningLAN lets computers on the same network share files with each other. There's nothing to
sign up for and nothing to set up. Run it on two or more computers on the same Wi-Fi, and each
sees the others' shared files.

## 1. Download

Open <https://github.com/zridi10/LightningLAN/releases/latest> and, under **Assets**, download the file for your computer:

| Your computer | Download |
| --- | --- |
| Windows 10 or 11 | `LightningLAN.exe` |
| Linux, 64-bit PC (Ubuntu 22.04 or newer, Debian 12, Fedora 36 or similar) | `LightningLAN-linux-x86_64` |

There is no Mac version yet.

LightningLAN doesn't need installing. It runs from wherever you put it.

LightningLAN isn't signed with a paid developer certificate, so Windows will warn you the first
time you open it. The steps below show you how to get past the warning. You only do this
once.

## 2. Open it

### Windows

1. Move `LightningLAN.exe` wherever you like, for example your Desktop or Documents folder.
2. Double-click it.
3. If you see **"Windows protected your PC"**, click **More info**, then **Run anyway**.
   - No **Run anyway** button? Right-click `LightningLAN.exe`, choose **Properties**, tick
     **Unblock** at the bottom of the **General** tab, click **OK** and open it again.
   - If your antivirus removes or blocks the file, restore it from the antivirus's quarantine
     and mark it as allowed. Apps packaged the way LightningLAN is are sometimes flagged by mistake.
4. When **Windows Defender Firewall** asks about LightningLAN, make sure **Private networks** is
   ticked and click **Allow access**. If you skip this, other computers can't see yours.
5. LightningLAN opens in a window of its own. A lightning-bolt icon also appears in the
   notification area at the bottom right of the screen. If you don't see it, click the small
   **^** arrow next to the clock. Closing the window leaves LightningLAN in the notification
   area; click the icon to open the window again. (On a PC without Microsoft's WebView2, which
   Windows 11 includes, it opens in your web browser instead.)

### Linux

1. Make the file executable. In most file managers, right-click it, choose **Properties** →
   **Permissions** and turn on **Allow executing file as program**. Or run this in a terminal:
   `chmod +x LightningLAN-linux-x86_64`
2. Double-click it. Some file managers ask whether to run it, so choose **Run**. Or start it
   from a terminal with `./LightningLAN-linux-x86_64`.
3. Your web browser opens the LightningLAN page.
   - **Tray icon**: desktops with a classic system tray (KDE Plasma, Xfce, Cinnamon, MATE)
     show a lightning-bolt icon, and clicking it opens LightningLAN. On GNOME the icon needs the
     *AppIndicator* extension (Ubuntu has it already).
   - **No tray icon?** LightningLAN is still running. Run the file again, or go to
     <http://localhost:8765> in your browser.
4. **Firewall**: most desktop Linux systems don't need any changes. If you turned on `ufw`
   yourself, allow LightningLAN with:

   ```
   sudo ufw allow 8765/tcp
   sudo ufw allow 5353/udp
   ```

## 3. Using LightningLAN

- **First start**: LightningLAN sets everything up for you. It creates a **LightningLAN** folder in
  your home folder with two folders inside, **Shared** and **Downloads**, and gives you a random
  funny name. A welcome note on the page shows you where your shared folder is.
- **Your name**: click the pencil next to your name at the top of the page, type a new one and
  press Enter. The dice button suggests another funny name. If someone else on the network got
  the same made-up name, one of you gets a new one by itself.
- **Themes**: pick one in **Settings**. Synthwave is the default; there are also Bliss and Deep
  Sea (animated too), and plain light and dark.
- **Share files**: drag files or folders onto the LightningLAN page, or copy them into your
  Shared folder. Everyone else running LightningLAN on your network sees them within a few
  seconds.
- **Get files**: click **Download** next to any file. It's saved in your Downloads folder.
  When several people download the same file at once, LightningLAN passes pieces between
  them, so it stays fast for everyone. A file you downloaded keeps helping others until you
  remove it from the Downloads list or stop LightningLAN. While files download, a Nyan Cat
  spins next to them; point at it and click to pause, click again to resume.
- **On your phone** (Android or iPhone, nothing to install): click the phone button at the top
  right of the page, then point the phone's camera at the QR code and tap the link. The phone
  has to be on the same Wi-Fi. In the phone's browser you can download anything in
  ThunderCLOUD and send photos and other files into your Shared folder, where everyone sees them.
  It works through this computer, so LightningLAN must be running here. **Disconnect phones**
  in the same window makes every phone scan a new code. If the phone can't connect, see
  section 4. Phones need the firewall to allow LightningLAN too, and guest Wi-Fi often blocks them.
- **Settings**: click the gear icon at the top right of the page to change your name, your
  folders (use **Browse…** to pick one), the theme or the passphrase. Changes apply immediately.
- **Passphrase (optional)**: if you set one, only computers using exactly the same passphrase
  see each other. Leave it empty to share with everyone on your network running LightningLAN.
- **Opening the page again**: open LightningLAN again. If it's already running, it just opens the
  page and doesn't start a second copy. You can also click the tray icon.
- **Stopping for a while**: click **Stop LightningLAN** on the page. Others stop seeing your
  files and your downloads pause. Click **Start LightningLAN** to carry on.
- **Closing the page** does the same as Stop, a few seconds after you close it. Open the page
  again (click the tray icon, or open LightningLAN again) and it starts sharing again.
- **Quitting**: on Windows, right-click the tray icon and choose **Quit LightningLAN**. Or, while
  LightningLAN is stopped, click **Quit completely** on the page.
- **Start LightningLAN when you log in** (optional). It opens its page when it starts, and
  shares while that page is open:
  - **Windows**: press **Windows key + R**, type `shell:startup` and press Enter. A folder
    opens. Drag `LightningLAN.exe` into it while holding the **right** mouse button, let go, and
    choose **Create shortcuts here**.

## 4. If the computers don't see each other

Go through this list on **both** computers:

1. **Is LightningLAN running on both?** Look for the lightning-bolt icon.
2. **Are both on the same network?** Check that both show the same Wi-Fi network name. A
   computer connected to the same router by cable is fine.
3. **Guest networks and public Wi-Fi usually don't work.** Guest Wi-Fi, and Wi-Fi in hotels,
   cafés, offices and universities, often stops devices from seeing each other. This is
   sometimes called *client isolation* or *AP isolation*. Use your main home network instead.
   Some routers also have an *AP isolation* setting that must be off.
4. **Windows: is the network set to Private?** Go to **Settings** → **Network & internet** →
   **Wi-Fi** (or **Ethernet**) → your network, and set **Network profile type** to
   **Private**. Windows hides your computer on Public networks.
5. **Was the firewall prompt allowed?**
   - **Windows**: open the Start menu and search for **Allow an app through Windows
     Firewall**. Click **Change settings**, find **LightningLAN** and tick **Private**, then click
     **OK**.
   - **Linux**: if you turned on a firewall such as `ufw`, allow the ports shown in the Linux
     section above.
6. **Is a VPN on?** Turn it off and try again. VPNs often send local network traffic
   elsewhere.
7. **Same passphrase?** The passphrase must be exactly the same on every computer, or empty
   on all of them. The **Peers** panel lists computers LightningLAN found but couldn't connect to,
   and says why.
8. **Still stuck?** Quit and reopen LightningLAN on both computers, or restart your router. If you
   ask for help, include the log file. Open it from the tray icon (**Open Log Folder**) or from
   **Settings** → **Open the log folder**.

## Where LightningLAN keeps things

- **Your files**: the Shared and Downloads folders, which you can change in Settings.
- **Settings and log files**:
  - Windows: `%APPDATA%\LightningLAN`
  - Linux: `~/.config/lightninglan`

LightningLAN never writes anything next to the app itself, so it works from any folder, including
read-only ones.

## Uninstalling

Quit LightningLAN and delete the app. If you also want to remove its settings, delete the
settings folder listed above. Your **LightningLAN** folder (Shared and Downloads) contains your
files, so check it before deleting it.
