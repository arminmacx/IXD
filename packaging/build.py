"""One-command build for Windows, macOS and Linux.

    python packaging/build.py                # native binary for the host OS
    python packaging/build.py --package      # …plus a distributable package
    python packaging/build.py --extension    # only zip the browser extension

Produces
--------
Linux    ``dist/ixd/`` + ``.deb`` + AppDir/``.AppImage``
macOS    ``dist/Internet Xtreme Downloader.app`` + ``.dmg``
Windows  ``dist/ixd/`` + a zip

The Debian package is assembled here with nothing but the standard library
(``ar`` + two tarballs), so no dpkg toolchain is required.  AppImage and DMG
creation use ``appimagetool`` and ``hdiutil`` when they are present and are
skipped with a clear message when they are not.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "ixd"
BUNDLE_NAME = "Internet Xtreme Downloader"
VERSION = re.search(
    r'__version__ = "([^"]+)"',
    (ROOT / "ixd" / "__init__.py").read_text(encoding="utf-8"),
).group(1)
MAINTAINER = "IXD <noreply@example.com>"
DESCRIPTION = "Accelerated multi-threaded download manager with browser integration"

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def log(message: str) -> None:
    print(f"  {message}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


def run(command: list[str], **kwargs) -> int:
    log(" ".join(str(part) for part in command))
    return subprocess.call(command, **kwargs)


def have(executable: str) -> bool:
    return shutil.which(executable) is not None


# ----------------------------------------------------------------------
def build_icons() -> None:
    section("Icons")
    script = ROOT / "packaging" / "make_icons.py"
    if run([sys.executable, str(script)]) != 0:
        raise SystemExit("icon generation failed")
    _write_ico()


def _write_ico() -> None:
    """Assemble a multi-resolution .ico from the generated PNGs (Windows)."""
    import struct

    sizes = [16, 32, 48, 128, 256]
    entries = []
    for size in sizes:
        path = ROOT / "packaging" / "icons" / f"ixd-{size}.png"
        if path.exists():
            entries.append((size, path.read_bytes()))
    if not entries:
        return

    header = struct.pack("<HHH", 0, 1, len(entries))
    directory = b""
    offset = 6 + 16 * len(entries)
    payload = b""
    for size, data in entries:
        dimension = 0 if size >= 256 else size
        directory += struct.pack(
            "<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(data), offset
        )
        payload += data
        offset += len(data)

    target = ROOT / "packaging" / "icons" / "ixd.ico"
    target.write_bytes(header + directory + payload)
    log(f"wrote {target.relative_to(ROOT)}")


# ----------------------------------------------------------------------
def build_binary(clean: bool) -> Path:
    section("Compiling the application")
    if clean:
        for directory in (DIST, BUILD):
            if directory.exists():
                shutil.rmtree(directory, ignore_errors=True)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyInstaller is not installed.\n"
            "  pip install pyinstaller"
        )

    command = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "packaging" / "ixd.spec"),
        "--noconfirm",
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
    ]
    if run(command, cwd=str(ROOT)) != 0:
        raise SystemExit("PyInstaller failed")

    if IS_MACOS and (DIST / f"{BUNDLE_NAME}.app").exists():
        result = DIST / f"{BUNDLE_NAME}.app"
    else:
        result = DIST / APP_NAME
    log(f"built {result}")
    return result


# ----------------------------------------------------------------------
def package_extension() -> list[Path]:
    """Zip the extension twice, once per browser manifest flavour."""
    section("Browser extension")
    source = ROOT / "extension"
    if not source.exists():
        log("extension folder missing — skipped")
        return []

    DIST.mkdir(parents=True, exist_ok=True)
    produced = []
    for browser, manifest_name in (("chrome", "manifest.chrome.json"),
                                   ("firefox", "manifest.firefox.json")):
        manifest = source / manifest_name
        if not manifest.exists():
            continue
        # A manifest still carrying the previous release's number ships an
        # extension the browser will not treat as an update, inside a zip whose
        # name says otherwise. Caught here rather than found afterwards.
        body = manifest.read_text(encoding="utf-8")
        declared = json.loads(body).get("version")
        if declared != VERSION:
            raise SystemExit(
                f"{manifest_name} says version {declared!r}, "
                f"but this is {VERSION!r} — update the manifest."
            )
        target = DIST / f"ixd-extension-{browser}-{VERSION}.zip"
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", body)
            for path in sorted(source.rglob("*")):
                if path.is_dir() or path.name.startswith("manifest."):
                    continue
                archive.write(path, str(path.relative_to(source)))
        produced.append(target)
        log(f"wrote {target.name}")
    return produced


# ----------------------------------------------------------------------
def _desktop_entry() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={BUNDLE_NAME}\n"
        f"Comment={DESCRIPTION}\n"
        f"Exec=/opt/{APP_NAME}/{APP_NAME} %U\n"
        f"Icon={APP_NAME}\n"
        "Terminal=false\n"
        "Categories=Network;FileTransfer;\n"
        "StartupNotify=true\n"
        "MimeType=x-scheme-handler/ixd;\n"
    )


def _ar_entry(name: str, data: bytes) -> bytes:
    """One member of a Unix ``ar`` archive (the container a .deb uses)."""
    header = (
        f"{name:<16}{int(time.time()):<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
    ).encode("ascii")
    payload = data
    if len(payload) % 2:
        payload += b"\n"        # members are 2-byte aligned
    return header + payload


def build_deb(binary_dir: Path) -> Path | None:
    """Assemble a .deb with only the standard library."""
    section("Debian package")
    if not binary_dir.exists():
        log("no built binary — skipped")
        return None

    architecture = {"x86_64": "amd64", "aarch64": "arm64",
                    "armv7l": "armhf"}.get(os.uname().machine, "amd64")

    installed_size = sum(
        f.stat().st_size for f in binary_dir.rglob("*") if f.is_file()
    ) // 1024

    control = (
        f"Package: {APP_NAME}\n"
        f"Version: {VERSION}\n"
        f"Section: net\n"
        f"Priority: optional\n"
        f"Architecture: {architecture}\n"
        f"Installed-Size: {installed_size}\n"
        f"Maintainer: {MAINTAINER}\n"
        f"Description: {DESCRIPTION}\n"
        "  Multi-threaded, resumable download manager with dynamic chunking,\n"
        "  native media extraction, proxy rotation and browser integration.\n"
    )

    postinst = (
        "#!/bin/sh\n"
        "set -e\n"
        "if command -v update-desktop-database >/dev/null 2>&1; then\n"
        "  update-desktop-database -q || true\n"
        "fi\n"
        "exit 0\n"
    )

    # -- data.tar.gz --------------------------------------------------
    data_buffer = io.BytesIO()
    with tarfile.open(fileobj=data_buffer, mode="w:gz") as archive:
        def add_tree(source: Path, prefix: str) -> None:
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                info = tarfile.TarInfo(f"{prefix}/{path.relative_to(source)}")
                data = path.read_bytes()
                info.size = len(data)
                info.mode = 0o755 if (path.stat().st_mode & stat.S_IXUSR) else 0o644
                info.mtime = int(time.time())
                archive.addfile(info, io.BytesIO(data))

        add_tree(binary_dir, f"./opt/{APP_NAME}")

        desktop = _desktop_entry().encode("utf-8")
        info = tarfile.TarInfo(f"./usr/share/applications/{APP_NAME}.desktop")
        info.size = len(desktop)
        info.mode = 0o644
        info.mtime = int(time.time())
        archive.addfile(info, io.BytesIO(desktop))

        for size in (16, 32, 64, 128, 256):
            icon = ROOT / "packaging" / "icons" / f"ixd-{size}.png"
            if not icon.exists():
                continue
            data = icon.read_bytes()
            info = tarfile.TarInfo(
                f"./usr/share/icons/hicolor/{size}x{size}/apps/{APP_NAME}.png"
            )
            info.size = len(data)
            info.mode = 0o644
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(data))

        # /usr/bin symlink target
        launcher = f"#!/bin/sh\nexec /opt/{APP_NAME}/{APP_NAME} \"$@\"\n".encode()
        info = tarfile.TarInfo(f"./usr/bin/{APP_NAME}")
        info.size = len(launcher)
        info.mode = 0o755
        info.mtime = int(time.time())
        archive.addfile(info, io.BytesIO(launcher))

    # -- control.tar.gz -----------------------------------------------
    control_buffer = io.BytesIO()
    with tarfile.open(fileobj=control_buffer, mode="w:gz") as archive:
        for name, text, mode in (
            ("./control", control, 0o644),
            ("./postinst", postinst, 0o755),
        ):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = mode
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(data))

    target = DIST / f"{APP_NAME}_{VERSION}_{architecture}.deb"
    with open(target, "wb") as handle:
        handle.write(b"!<arch>\n")
        handle.write(_ar_entry("debian-binary", b"2.0\n"))
        handle.write(_ar_entry("control.tar.gz", control_buffer.getvalue()))
        handle.write(_ar_entry("data.tar.gz", data_buffer.getvalue()))

    log(f"wrote {target.name} ({target.stat().st_size / 1048576:.1f} MB)")
    return target


def build_appimage(binary_dir: Path) -> Path | None:
    section("AppImage")
    appdir = DIST / f"{APP_NAME}.AppDir"
    if appdir.exists():
        shutil.rmtree(appdir, ignore_errors=True)
    (appdir / "usr" / "bin").mkdir(parents=True, exist_ok=True)

    shutil.copytree(binary_dir, appdir / "usr" / "bin" / APP_NAME, dirs_exist_ok=True)

    (appdir / f"{APP_NAME}.desktop").write_text(
        _desktop_entry().replace(f"/opt/{APP_NAME}/{APP_NAME}", APP_NAME),
        encoding="utf-8",
    )
    icon = ROOT / "packaging" / "icons" / "ixd-256.png"
    if icon.exists():
        shutil.copy(icon, appdir / f"{APP_NAME}.png")

    apprun = appdir / "AppRun"
    apprun.write_text(
        "#!/bin/sh\n"
        'HERE="$(dirname "$(readlink -f "${0}")")"\n'
        f'exec "$HERE/usr/bin/{APP_NAME}/{APP_NAME}" "$@"\n',
        encoding="utf-8",
    )
    apprun.chmod(apprun.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    log(f"assembled {appdir.name}")

    if not have("appimagetool"):
        log("appimagetool not found — the AppDir is ready; install appimagetool "
            "to produce a single-file .AppImage")
        return None

    target = DIST / f"{BUNDLE_NAME.replace(' ', '_')}-{VERSION}-x86_64.AppImage"
    if run(["appimagetool", str(appdir), str(target)]) != 0:
        log("appimagetool failed — the AppDir is still usable")
        return None
    log(f"wrote {target.name}")
    return target


def build_dmg(app_bundle: Path) -> Path | None:
    section("DMG")
    if not have("hdiutil"):
        log("hdiutil unavailable — skipped")
        return None
    target = DIST / f"{BUNDLE_NAME.replace(' ', '-')}-{VERSION}.dmg"
    if target.exists():
        target.unlink()
    staging = DIST / "dmg-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(app_bundle, staging / app_bundle.name)
    os.symlink("/Applications", staging / "Applications")

    code = run([
        "hdiutil", "create", "-volname", BUNDLE_NAME,
        "-srcfolder", str(staging), "-ov", "-format", "UDZO", str(target),
    ])
    shutil.rmtree(staging, ignore_errors=True)
    if code != 0:
        log("hdiutil failed")
        return None
    log(f"wrote {target.name}")
    return target


def build_windows_zip(binary_dir: Path) -> Path:
    section("Windows archive")
    target = DIST / f"{APP_NAME}-{VERSION}-windows-x64.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(binary_dir.rglob("*")):
            if path.is_file():
                archive.write(path, str(Path(APP_NAME) / path.relative_to(binary_dir)))
    log(f"wrote {target.name}")
    return target


#: What never leaves this tree in a source bundle handed to someone else.
#:
#: `extension-key.pem` is the extension's **private signing key** — the public
#: half is already in `manifest.chrome.json`, which is all that is needed for
#: the fixed extension ID. `idm/` is somebody else's extension and not ours to
#: redistribute. `session-log.md` is history nobody building needs, and it is
#: now larger than everything else combined.
#: The launch copy is the same kind of thing: notes about how to talk about
#: the project rather than part of it, and untracked besides — a source bundle
#: built from a working tree would otherwise ship them when the repository
#: does not.
_SOURCE_EXCLUDED_FILES = frozenset({
    "packaging/extension-key.pem",
    "session-log.md",
    "packaging/reddit-post.md",
    "packaging/producthunt.md",
})
_SOURCE_EXCLUDED_DIRS = frozenset({
    ".git", ".venv", "dist", "build", "idm", "backups", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
_SOURCE_EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".sqlite3", ".deb", ".zip",
                             ".AppImage", ".log")

#: Screenshots from field reports live at the top of the tree. They are dropped
#: **by location, not by suffix**: excluding `.png` outright also took
#: `extension/icons/*.png`, which the manifest names — so the bundle built an
#: extension the browser would refuse to load, and the zip looked complete.
_SOURCE_EXCLUDED_ROOT_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif")


def build_windows_source() -> Path:
    """The tree itself, zipped, for a machine that can build what this cannot.

    PyInstaller bundles the interpreter and extension modules *of the machine it
    runs on*, so a Windows binary needs a Windows Python — there is no
    cross-compilation and none is coming. What can be shipped is the source, and
    `packaging/windows/build.bat` on the far end.

    This exists as a build step because the first one was assembled by hand: the
    next build's clean step deleted it, nothing could recreate it, and what it
    held was a day out of date the moment anything changed.
    """
    section("Windows source bundle")
    target = DIST / f"{APP_NAME}-{VERSION}-windows-source.zip"
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            parts = set(relative.parts)
            if parts & _SOURCE_EXCLUDED_DIRS:
                continue
            if relative.as_posix() in _SOURCE_EXCLUDED_FILES:
                continue
            if path.suffix in _SOURCE_EXCLUDED_SUFFIXES:
                continue
            if (len(relative.parts) == 1
                    and path.suffix.lower() in _SOURCE_EXCLUDED_ROOT_SUFFIXES):
                continue
            archive.write(path, str(Path(f"{APP_NAME}-{VERSION}") / relative))
            count += 1
    log(f"wrote {target.name} ({count} files, "
        f"{target.stat().st_size / 1024:.0f} KB)")
    return target


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Windows: an installer, for people who expect to install things
# ----------------------------------------------------------------------
#: Written from here rather than kept as a file, because every path and name
#: in it has to agree with what the build just produced — and a template that
#: drifts from the build is an installer that ships the wrong version number
#: or omits a file nobody notices until somebody runs it.
_NSIS_SCRIPT = r"""
Unicode true

; MultiUser gives the "everyone / just me" page, and with it the two things
; that have to change together: where the files go, and which hive the
; uninstall entry is written to. Doing that by hand is how an installer ends up
; writing an all-users install into HKCU, where Add/Remove Programs shows it to
; one account and nobody else can remove it.
;
; `Highest` means: elevate when the person running this can, and offer only the
; per-user option when they cannot. A standard user still gets a working
; install, which is the whole point of having the second option.
!define MULTIUSER_EXECUTIONLEVEL Highest
!define MULTIUSER_MUI
!define MULTIUSER_INSTALLMODE_COMMANDLINE
!define MULTIUSER_INSTALLMODE_INSTDIR "{app_slug}"
!define MULTIUSER_INSTALLMODE_INSTALL_REGISTRY_KEY "Software\{app_slug}"
!define MULTIUSER_INSTALLMODE_INSTALL_REGISTRY_VALUENAME "InstallDir"
!define MULTIUSER_INSTALLMODE_DEFAULT_REGISTRY_KEY "Software\{app_slug}"
!define MULTIUSER_INSTALLMODE_DEFAULT_REGISTRY_VALUENAME "InstallDir"
!define MULTIUSER_INSTALLMODE_FUNCTION OnInstallModeChanged
!include "MultiUser.nsh"
!include "MUI2.nsh"
!include "LogicLib.nsh"

Name "{app_name}"
OutFile "{output}"
BrandingText "{app_name} {version}"
ShowInstDetails show
ShowUninstDetails show

!define MUI_ABORTWARNING
!define MUI_ICON "{icon}"
!define MUI_UNICON "{icon}"
!define MUI_FINISHPAGE_RUN "$INSTDIR\{launcher}"
!define MUI_FINISHPAGE_RUN_TEXT "Start {app_name}"

!insertmacro MULTIUSER_PAGE_INSTALLMODE
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

; Chosen on the page above, and the two answers are not symmetrical.
;
; All users goes to Program Files, which needs administrator and which the
; person who *runs* the application afterwards cannot write to — so the
; extension folder falls back to their data directory, by design.
;
; Just me goes to %APPDATA%\{app_slug} — writable, no elevation, and the
; extension folder therefore sits inside the install where the browser can be
; pointed at it once and keep working across updates.
Function OnInstallModeChanged
  ${{If}} $MultiUser.InstallMode == "AllUsers"
    StrCpy $INSTDIR "$PROGRAMFILES64\{app_slug}"
  ${{Else}}
    StrCpy $INSTDIR "$APPDATA\{app_slug}"
  ${{EndIf}}
FunctionEnd

Function .onInit
  !insertmacro MULTIUSER_INIT
FunctionEnd

Function un.onInit
  !insertmacro MULTIUSER_UNINIT
FunctionEnd

Section "Install"
  ; Close a copy that is already running, before a single file is written.
  ;
  ; Reported on the first in-app update: the application starts this installer
  ; and quits — but "and quits" is a race, and NSIS got to the first locked
  ; file first: *"its not close and quit the app completely and hit with error
  ; that ixd is running and i should manually quit the app then hit retry"*.
  ;
  ; So the installer stops depending on the timing and does it itself. Politely
  ; first, because the application is writing out its database; then plainly,
  ; because by that point it has had five seconds and the person is watching a
  ; progress bar. `ixd.exe` is also the name the browser's messaging host runs
  ; under, so this closes that too — which is the other thing holding the
  ; folder open (context.md §3.14u57).
  DetailPrint "Closing {app_name} if it is running…"
  ExecWait 'taskkill /IM "{launcher}"'
  Sleep 2500
  ExecWait 'taskkill /IM "{launcher}" /F'
  Sleep 800

  SetOutPath "$INSTDIR"
  ; Everything the portable build contains, exactly as it was tested.
  ; `\*` and not `\*.*`: the second is the DOS spelling and is reported to skip
  ; files with no extension. Only one file in the build has none — the launcher,
  ; which on Windows is `ixd.exe` and does — but this is a script that cannot be
  ; run here, and the superset costs nothing.
  File /r "{payload}\*"

  CreateDirectory "$SMPROGRAMS\{app_name}"
  CreateShortCut "$SMPROGRAMS\{app_name}\{app_name}.lnk" "$INSTDIR\{launcher}"
  CreateShortCut "$SMPROGRAMS\{app_name}\Uninstall {app_name}.lnk" "$INSTDIR\uninstall.exe"
  CreateShortCut "$DESKTOP\{app_name}.lnk" "$INSTDIR\{launcher}"

  ; SHCTX is HKLM for an all-users install and HKCU for a per-user one.
  ; MultiUser sets it; writing the literal hive is the mistake it exists to
  ; prevent.
  WriteRegStr SHCTX "Software\{app_slug}" "InstallDir" "$INSTDIR"
  WriteRegStr SHCTX "Software\{app_slug}" "InstallMode" "$MultiUser.InstallMode"
  ; The entry Add/Remove Programs reads. Without it the application can be
  ; installed and not uninstalled, which is worse than not installing at all.
  WriteRegStr SHCTX "{uninstall_key}" "DisplayName" "{app_name}"
  WriteRegStr SHCTX "{uninstall_key}" "DisplayVersion" "{version}"
  WriteRegStr SHCTX "{uninstall_key}" "Publisher" "{publisher}"
  WriteRegStr SHCTX "{uninstall_key}" "DisplayIcon" "$INSTDIR\{launcher}"
  WriteRegStr SHCTX "{uninstall_key}" "UninstallString" "$INSTDIR\uninstall.exe"
  WriteRegStr SHCTX "{uninstall_key}" "InstallLocation" "$INSTDIR"
  WriteRegDWORD SHCTX "{uninstall_key}" "NoModify" 1
  WriteRegDWORD SHCTX "{uninstall_key}" "NoRepair" 1
  WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

Section "Uninstall"
  ; The application is asked to close first: a folder cannot be removed while
  ; anything inside it is running, which is the same lesson the updater
  ; learned the hard way.
  ExecWait 'taskkill /IM "{launcher}" /F'
  Sleep 500
  Delete "$DESKTOP\{app_name}.lnk"
  Delete "$SMPROGRAMS\{app_name}\{app_name}.lnk"
  Delete "$SMPROGRAMS\{app_name}\Uninstall {app_name}.lnk"
  RMDir "$SMPROGRAMS\{app_name}"
  ; Takes the generated extension folders with it. They are written by the
  ; application rather than by this installer, so nothing else would.
  RMDir /r "$INSTDIR"
  DeleteRegKey SHCTX "{uninstall_key}"
  DeleteRegKey SHCTX "Software\{app_slug}"
SectionEnd
"""


def windows_installer_script(binary_dir: Path, output: Path) -> str:
    """The installer script for what has just been built.

    `app_name` is the **display** name and `app_slug` the one that goes in
    paths and the registry. They were the same string — the slug — until the
    script was compiled and its preprocessed output read: Add/Remove Programs
    would have said `ixd`, and so would the Start menu folder and the desktop
    shortcut. Nothing about reading the template showed it.
    """
    icon = ROOT / "packaging" / "icons" / "ixd.ico"
    return _NSIS_SCRIPT.format(
        app_name=BUNDLE_NAME,
        app_slug="IXD",
        version=VERSION,
        publisher="IXD",
        output=str(output),
        payload=str(binary_dir),
        launcher="ixd.exe",
        icon=str(icon),
        uninstall_key=(r"Software\Microsoft\Windows\CurrentVersion"
                       r"\Uninstall\IXD"),
    )


def build_macos_pkg(app_bundle: Path) -> Path | None:
    """A double-clickable installer, which a .dmg is not.

    The `.dmg` is a disk image: it opens a window and the person drags the
    application into `Applications` themselves. That is idiomatic on macOS and
    it is still published — but it is not an installer, and "an installer for
    every OS" should mean one on every OS rather than one on two of them.

    `pkgbuild` ships with the developer tools and exists only on macOS, so this
    is written here and runs on the macOS runner, the same arrangement as NSIS.
    Unsigned, like the .dmg: the first launch still needs right-click → Open.
    """
    section("macOS installer")
    if not app_bundle.exists():
        log("no .app bundle — skipped")
        return None
    if not have("pkgbuild"):
        log("pkgbuild is not available here, so no .pkg is produced")
        return None

    target = DIST / f"{APP_NAME}-{VERSION}-macos-arm64.pkg"
    target.unlink(missing_ok=True)
    code = run([
        "pkgbuild",
        "--install-location", "/Applications",
        "--component", str(app_bundle),
        "--identifier", "com.ixd.downloader",
        "--version", VERSION,
        str(target),
    ])
    if code != 0 or not target.exists():
        log("pkgbuild failed; no .pkg was produced")
        return None
    log(f"wrote {target.name} ({target.stat().st_size / 1048576:.1f} MB)")
    return target


#: What an *installed* copy records about itself. It is not the portable
#: marker: there is no folder for it to swap, and the update it wants is the
#: next `setup.exe` — which keeps the uninstaller, the Add/Remove Programs
#: version and the shortcuts correct, and is the only route that can write into
#: an all-users install at all.
INSTALLED_MARKER = {
    "self_update": True,
    "kind": "installer",
    # No version in it, for the reason SELF_UPDATE_PATTERNS records below.
    "asset": "windows-x64-setup.exe",
}


def _installer_payload(binary_dir: Path) -> Path:
    """A copy of the build that knows it was installed.

    The installer used to pack `binary_dir` as it stood, which carries **no
    marker at all** — so every copy installed from `setup.exe` answered "this
    build was installed from a package" and offered a link to the release page
    instead of an update. Reported against 1.0.16 by the one person who could
    see it: this machine builds Linux, and the portable build it produces has
    a marker of its own.

    A copy rather than the folder itself, because the same `binary_dir` is
    packed into the plain `.zip` — which is deliberately *not* self-updating.
    """
    staging = DIST / "installer-payload"
    shutil.rmtree(staging, ignore_errors=True)
    payload = staging / binary_dir.name
    shutil.copytree(binary_dir, payload, symlinks=True)
    (payload / "update-channel.json").write_text(
        json.dumps({**INSTALLED_MARKER, "version": VERSION}, indent=2) + "\n",
        encoding="utf-8")
    log("staged the payload with an installed-build marker")
    return payload


def build_windows_installer(binary_dir: Path) -> Path | None:
    """A real installer, when the machine has something to build one with.

    NSIS is the tool Windows installers are built with; it is not something
    this project reimplements, and it is not something a Linux machine has. So
    the script is written either way — it can be read, and it is checked by a
    test — and it is compiled only where `makensis` exists, which is CI.
    """
    section("Windows installer")
    output = DIST / f"ixd-{VERSION}-windows-x64-setup.exe"
    payload = _installer_payload(binary_dir)
    script_path = DIST / "installer.nsi"
    script_path.write_text(windows_installer_script(payload, output),
                           encoding="utf-8")
    log(f"wrote {script_path.name}")
    if not have("makensis"):
        log("makensis is not installed here, so the installer is not compiled")
        return None
    if run(["makensis", "-V2", str(script_path)]) != 0:
        log("makensis refused the script; no installer was produced")
        return None
    log(f"wrote {output.name} ({output.stat().st_size / 1048576:.1f} MB)")
    return output


# ----------------------------------------------------------------------
# the build that can replace itself
# ----------------------------------------------------------------------
#: The name each platform's self-updating archive is published under. The
#: build writes it into its own marker so a running copy asks for exactly the
#: file it came from rather than guessing from the platform at run time.
SELF_UPDATE_ASSETS = {
    "linux": "ixd-linux-x86_64-selfupdate.tar.gz",
    "darwin": "ixd-macos-arm64-selfupdate.zip",
    "win32": f"ixd-{VERSION}-windows-x64-selfupdate.zip",
}

#: What the build records in its own marker — the same file, with the version
#: taken out. A marker that names one release's file cannot match the next
#: one's: 1.0.8 on Windows recorded `ixd-1.0.8-windows-x64-selfupdate.zip`,
#: searched the 1.0.9 release for that exact string, and reported that nothing
#: it could use had been published while listing the file it wanted.
SELF_UPDATE_PATTERNS = {
    "linux": "ixd-linux-x86_64-selfupdate.tar.gz",
    "darwin": "ixd-macos-arm64-selfupdate.zip",
    "win32": "windows-x64-selfupdate.zip",
}


def _platform_key() -> str:
    if IS_WINDOWS:
        return "win32"
    if IS_MACOS:
        return "darwin"
    return "linux"


def build_self_updating(binary_dir: Path) -> Path | None:
    """Package a second copy that is allowed to update itself.

    The binaries are identical; what differs is one file. A build says whether
    it may replace its own folder — it is never inferred at run time, because
    an unpacked `.deb` in `/opt` looks exactly like a portable folder and
    replacing that one leaves a machine whose next package upgrade fails.

    Published beside the ordinary archives, never instead of them: somebody
    who wants a build that touches nothing by itself keeps having one.
    """
    section("Self-updating build")
    asset = SELF_UPDATE_ASSETS[_platform_key()]
    staging = DIST / "selfupdate"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    copy = staging / binary_dir.name
    shutil.copytree(binary_dir, copy, symlinks=True)
    # The marker sits beside the launcher, because that is where the running
    # program looks for it: `Path(sys.executable).parent`. Inside an .app
    # bundle that is `Contents/MacOS`, not the bundle's own folder.
    beside = copy
    if copy.suffix == ".app" and (copy / "Contents" / "MacOS").is_dir():
        beside = copy / "Contents" / "MacOS"
    (beside / "update-channel.json").write_text(json.dumps({
        "self_update": True,
        "kind": "portable",
        "asset": SELF_UPDATE_PATTERNS[_platform_key()],
        "version": VERSION,
    }, indent=2) + "\n", encoding="utf-8")

    target = DIST / asset
    target.unlink(missing_ok=True)
    if target.suffix == ".zip":
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(copy.rglob("*")):
                if path.is_file() or path.is_symlink():
                    archive.write(path, str(path.relative_to(staging)))
    else:
        with tarfile.open(target, "w:gz") as archive:
            archive.add(copy, arcname=copy.name)
    shutil.rmtree(staging, ignore_errors=True)
    log(f"wrote {target.name} ({target.stat().st_size / 1048576:.1f} MB)")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Internet Xtreme Downloader")
    parser.add_argument("--package", action="store_true",
                        help="also produce a distributable package for this OS")
    parser.add_argument("--extension", action="store_true",
                        help="only build the browser extension archives")
    parser.add_argument("--icons", action="store_true", help="only regenerate icons")
    parser.add_argument("--no-clean", action="store_true",
                        help="keep previous build artefacts")
    parser.add_argument("--self-update", action="store_true",
                        help="also produce the archive that may replace itself")
    parser.add_argument("--installer", action="store_true",
                        help="write (and, where possible, compile) the installer")
    arguments = parser.parse_args()

    DIST.mkdir(parents=True, exist_ok=True)

    if arguments.icons:
        build_icons()
        return 0

    if arguments.extension:
        package_extension()
        return 0

    # `--self-update` on its own packages what is already built. CI calls it
    # after the platform's own build script has run, and a second PyInstaller
    # pass to produce an archive that differs by one file is ten minutes
    # nobody gets back.
    existing = DIST / "ixd"
    bundle = DIST / f"{BUNDLE_NAME}.app"
    if arguments.installer and not arguments.package:
        # `--installer` means "this platform's installer", so it does the right
        # thing wherever it is run rather than being a Windows-only flag with a
        # general-sounding name.
        if IS_MACOS and bundle.exists():
            build_macos_pkg(bundle)
        elif IS_LINUX and existing.exists():
            build_deb(existing)
        elif existing.exists():
            build_windows_installer(existing)
        if not arguments.self_update:
            section("Done")
            return 0

    if arguments.self_update and not arguments.package and existing.exists():
        build_self_updating(existing)
        section("Done")
        return 0

    build_icons()
    binary = build_binary(clean=not arguments.no_clean)
    package_extension()

    if arguments.package:
        if IS_LINUX:
            build_deb(binary)
            build_appimage(binary)
        elif IS_MACOS:
            build_dmg(binary)
            build_macos_pkg(binary)
        elif IS_WINDOWS:
            build_windows_zip(binary)
            build_windows_installer(binary)
        # Built everywhere, because the machine that can build for Windows is
        # never the machine asking for it.
        if not IS_WINDOWS:
            build_windows_source()

    if arguments.self_update or arguments.package:
        build_self_updating(binary)

    section("Done")
    for path in sorted(DIST.iterdir()):
        size = ""
        if path.is_file():
            size = f"  ({path.stat().st_size / 1048576:.1f} MB)"
        print(f"  {path.name}{size}")
    print(
        "\nNext: register the browser bridge with\n"
        "  python native-host/install_host.py            (then --verify)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
