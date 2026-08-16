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
_SOURCE_EXCLUDED_FILES = frozenset({
    "packaging/extension-key.pem",
    "session-log.md",
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
        "asset": asset,
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
        elif IS_WINDOWS:
            build_windows_zip(binary)
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
