# PyInstaller spec for the LanShare desktop app. Run it through build.py, which first writes
# the icons and the Windows version resource into build/.
#
#   Windows: dist/LanShare.exe        one file, no console window
#   macOS:   dist/LanShare.app        menu bar app (no Dock icon); build.py zips it
#   Linux:   dist/LanShare            one file

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
BUILD = ROOT / "build"
sys.path.insert(0, str(ROOT))
from lanshare import __version__  # noqa: E402

# Change this to a reverse-DNS name you own if you sign the macOS app.
BUNDLE_ID = "io.lanshare.LanShare"

# zeroconf's compiled modules import each other in ways PyInstaller cannot see. pystray picks
# its backend at run time, and on a Linux build machine without a display the standard hook
# cannot import pystray to find them, so they are listed here.
hiddenimports = collect_submodules("zeroconf") + collect_submodules("lanshare")
if sys.platform == "win32":
    hiddenimports += ["pystray._win32"]
elif sys.platform == "darwin":
    hiddenimports += ["pystray._darwin"]
else:
    hiddenimports += ["pystray._xorg", "pystray._appindicator", "pystray._gtk"]
excludes = ["pytest", "_pytest", "psutil", "PyInstaller"]
if sys.platform == "darwin":
    excludes.append("tkinter")  # macOS uses its own folder chooser through osascript

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "lanshare" / "static"), "lanshare/static")],
    hiddenimports=hiddenimports,
    excludes=excludes,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    icon = str(BUILD / "icons" / "LanShare.icns")
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="LanShare",
        console=False,
        upx=False,
        icon=icon,
    )
    coll = COLLECT(exe, a.binaries, a.datas, name="LanShare", upx=False)
    app = BUNDLE(
        coll,
        name="LanShare.app",
        icon=icon,
        bundle_identifier=BUNDLE_ID,
        version=__version__,
        info_plist={
            "CFBundleName": "LanShare",
            "CFBundleDisplayName": "LanShare",
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSUIElement": True,  # lives in the menu bar, not the Dock
            "LSMinimumSystemVersion": "11.0",
            "NSHighResolutionCapable": True,
            # macOS 15+ asks before an app may use the local network; these explain why and
            # declare the Bonjour service LanShare browses for.
            "NSLocalNetworkUsageDescription": (
                "LanShare finds other computers running LanShare on your network so you can share files with them."
            ),
            "NSBonjourServices": ["_lanshare._tcp"],
        },
    )
else:
    windows = sys.platform == "win32"
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="LanShare",
        console=False,  # no console window on Windows (no effect on Linux)
        upx=False,
        icon=str(BUILD / "icons" / "LanShare.ico") if windows else None,
        version=str(BUILD / "version_info.txt") if windows else None,
    )
