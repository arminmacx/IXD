# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Internet Xtreme Downloader.

Invoked by ``packaging/build.py``; can also be run directly:

    pyinstaller packaging/ixd.spec --noconfirm
"""

import sys
from pathlib import Path

# ``__file__`` is not defined while a spec executes; SPECPATH is.
ROOT = Path(SPECPATH).resolve().parent

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

APP_NAME = "ixd"
BUNDLE_NAME = "Internet Xtreme Downloader"

datas = [
    (str(ROOT / "packaging" / "icons"), "packaging/icons"),
    (str(ROOT / "extension"), "extension"),
    (str(ROOT / "native-host" / "install_host.py"), "native-host"),
    # The sandbox relay is copied out as a *file* at registration time, so the
    # source has to survive freezing — PyInstaller would otherwise keep only
    # the compiled module, which cannot be copied into a browser's snap area.
    (str(ROOT / "ixd" / "ipc" / "relay.py"), "ixd/ipc"),
]

hiddenimports = [
    "ixd.extractors.youtube",
    "ixd.extractors.generic",
    "ixd.extractors.hls",
    "ixd.extractors.dash",
    "ixd.ipc.native_host",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
]

# Qt modules the UI never touches — excluding them roughly halves the bundle.
excludes = [
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQml", "PySide6.Qt3DCore", "PySide6.Qt3DRender",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtCharts",
    "PySide6.QtDataVisualization", "PySide6.QtBluetooth", "PySide6.QtNfc",
    "PySide6.QtPositioning", "PySide6.QtSerialPort", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner", "PySide6.QtHelp",
    "tkinter", "unittest", "pydoc_data", "test",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

#: Each platform accepts one container and *refuses* the others — PyInstaller
#: does not convert. The PNG is therefore a fallback only where a PNG is legal:
#: on macOS an absent `.icns` means no icon at all, because handing it the PNG
#: raises "which exists but is not in the correct format" and the build fails
#: with no application produced. That is exactly how the first CI release died.
_ICONS = ROOT / "packaging" / "icons"
icon_file = None
if IS_WINDOWS:
    if (_ICONS / "ixd.ico").exists():
        icon_file = str(_ICONS / "ixd.ico")
elif IS_MACOS:
    if (_ICONS / "ixd.icns").exists():
        icon_file = str(_ICONS / "ixd.icns")
elif (_ICONS / "ixd-256.png").exists():
    icon_file = str(_ICONS / "ixd-256.png")

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A console is required on Windows for the native-messaging stdio channel
    # to work when the binary is launched by the browser.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)

if IS_MACOS:
    app = BUNDLE(
        coll,
        name=f"{BUNDLE_NAME}.app",
        icon=icon_file,
        bundle_identifier="com.ixd.downloader",
        info_plist={
            "CFBundleName": BUNDLE_NAME,
            "CFBundleDisplayName": BUNDLE_NAME,
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            # Keeps the app alive in the menu bar with no window open.
            "LSUIElement": False,
            "CFBundleURLTypes": [
                {
                    "CFBundleURLName": "IXD Download",
                    "CFBundleURLSchemes": ["ixd"],
                }
            ],
        },
    )
