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


def materialise_extension(binary_dir: Path) -> list[Path]:
    """Put the two extension folders **into the payload**, beside the launcher.

    They used to be written by the application, at start-up, into whichever
    folder that launch could write. On an all-users install that is not the
    folder the user chose: `Program Files` is writable to the elevated launch
    the installer starts and to nothing afterwards, so the extension landed
    there once and in `%APPDATA%` every time after — two folders, and the
    browser pointed at one (context.md §3.71).

    The installer runs with the privileges to write the folder the user picked.
    So it writes them, and the application stops needing to. The user's words:
    *"when user select for all user the installed install everything in the
    path user selected and updater later need to check this and update exactly
    where the user installed the old version."*

    Each browser's own manifest is written as `manifest.json`, exactly as
    `package_extension` does for the zips and `integration._write_manifest`
    does at run time — one folder, one manifest, no flavoured copies left in it
    for a browser that is not the one loading it (§3.43).
    """
    source = ROOT / "extension"
    if not source.is_dir() or not binary_dir.is_dir():
        return []

    section("Extension folders in the payload")
    produced = []
    for folder, manifest_name in (("extension", "manifest.chrome.json"),
                                  ("extension-firefox", "manifest.firefox.json")):
        manifest = source / manifest_name
        if not manifest.exists():
            continue
        body = manifest.read_text(encoding="utf-8")
        declared = json.loads(body).get("version")
        if declared != VERSION:
            raise SystemExit(
                f"{manifest_name} says version {declared!r}, "
                f"but this is {VERSION!r} — update the manifest."
            )
        target = binary_dir / folder
        shutil.rmtree(target, ignore_errors=True)
        target.mkdir(parents=True, exist_ok=True)
        for path in sorted(source.rglob("*")):
            if path.is_dir() or path.name.startswith("manifest."):
                continue
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        (target / "manifest.json").write_text(body, encoding="utf-8")
        produced.append(target)
        log(f"wrote {folder}/ ({sum(1 for _ in target.rglob('*') if _.is_file())} files)")
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


# ----------------------------------------------------------------------
# What the built tree actually needs from the system
#
# A .deb that declares no dependencies installs anywhere and may then refuse to
# start, which is rule 4 in packaging form: it looks finished and does nothing.
# The list is *measured* rather than written down, because PyInstaller bundles
# most of Qt's world into ``_internal`` and which libraries it leaves behind
# differs between build machines — a hand-kept list would be wrong the first
# time the base image changed, and wrong silently.
#
# ELF is read here with the standard library, for the same reason every other
# format in this project is: the Linux build runs inside whatever container
# holds the glibc floor down, and ``objdump`` is not guaranteed to be in it.

_ELF_MAGIC = b"\x7fELF"
_DT_NEEDED = 1

# soname → the Debian package that provides it.  Deliberately a superset of
# what is measured today: if a different base image bundles less, the extra
# entries are what keeps the answer right.  ``a | b`` is Debian's "either of
# these" and covers the 64-bit time_t renames, which hit some of these names
# in Ubuntu 24.04 and none of them in Debian 12.
SONAME_PACKAGES: dict[str, str] = {
    # the C library, and what used to be split out of it
    "libc.so.6": "libc6", "libm.so.6": "libc6", "libdl.so.2": "libc6",
    "ld-linux-x86-64.so.2": "libc6", "ld-linux-aarch64.so.1": "libc6",
    "libpthread.so.0": "libc6", "libresolv.so.2": "libc6",
    "librt.so.1": "libc6", "libutil.so.1": "libc6",
    "libcrypt.so.1": "libcrypt1",
    # the compiler runtime
    "libstdc++.so.6": "libstdc++6", "libgcc_s.so.1": "libgcc-s1",
    "libatomic.so.1": "libatomic1",
    # graphics: what Qt's platform plugins open at start-up
    "libGL.so.1": "libgl1", "libGLX.so.0": "libgl1",
    "libGLdispatch.so.0": "libgl1", "libOpenGL.so.0": "libopengl0",
    "libEGL.so.1": "libegl1", "libgbm.so.1": "libgbm1",
    "libdrm.so.2": "libdrm2",
    "libwayland-client.so.0": "libwayland-client0",
    "libwayland-cursor.so.0": "libwayland-cursor0",
    "libwayland-egl.so.1": "libwayland-egl1",
    "libwayland-server.so.0": "libwayland-server0",
    # X11 and xcb
    "libX11.so.6": "libx11-6", "libX11-xcb.so.1": "libx11-xcb1",
    "libxcb.so.1": "libxcb1", "libXau.so.6": "libxau6",
    "libXdmcp.so.6": "libxdmcp6", "libXext.so.6": "libxext6",
    "libXi.so.6": "libxi6", "libXfixes.so.3": "libxfixes3",
    "libXrender.so.1": "libxrender1", "libXrandr.so.2": "libxrandr2",
    "libXcursor.so.1": "libxcursor1", "libXcomposite.so.1": "libxcomposite1",
    "libXdamage.so.1": "libxdamage1", "libXinerama.so.1": "libxinerama1",
    "libXtst.so.6": "libxtst6", "libXRes.so.1": "libxres1",
    "libxcb-cursor.so.0": "libxcb-cursor0", "libxcb-glx.so.0": "libxcb-glx0",
    "libxcb-icccm.so.4": "libxcb-icccm4", "libxcb-image.so.0": "libxcb-image0",
    "libxcb-keysyms.so.1": "libxcb-keysyms1",
    "libxcb-randr.so.0": "libxcb-randr0", "libxcb-render.so.0": "libxcb-render0",
    "libxcb-render-util.so.0": "libxcb-render-util0",
    "libxcb-shape.so.0": "libxcb-shape0", "libxcb-shm.so.0": "libxcb-shm0",
    "libxcb-sync.so.1": "libxcb-sync1", "libxcb-util.so.1": "libxcb-util1",
    "libxcb-xfixes.so.0": "libxcb-xfixes0", "libxcb-xkb.so.1": "libxcb-xkb1",
    "libxkbcommon.so.0": "libxkbcommon0",
    "libxkbcommon-x11.so.0": "libxkbcommon-x11-0",
    # the rest of the desktop, for whatever a base image declines to bundle
    "libglib-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgobject-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgio-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgmodule-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libgthread-2.0.so.0": "libglib2.0-0t64 | libglib2.0-0",
    "libdbus-1.so.3": "libdbus-1-3",
    "libfontconfig.so.1": "libfontconfig1",
    "libfreetype.so.6": "libfreetype6",
    "libexpat.so.1": "libexpat1",
    "libz.so.1": "zlib1g", "libzstd.so.1": "libzstd1",
    "libbz2.so.1.0": "libbz2-1.0", "liblzma.so.5": "liblzma5",
    "libpng16.so.16": "libpng16-16t64 | libpng16-16",
    "libharfbuzz.so.0": "libharfbuzz0b",
    "libssl.so.3": "libssl3t64 | libssl3",
    "libcrypto.so.3": "libssl3t64 | libssl3",
    "libsqlite3.so.0": "libsqlite3-0",
    "libffi.so.8": "libffi8",
    "libsystemd.so.0": "libsystemd0",
    "libselinux.so.1": "libselinux1", "libseccomp.so.2": "libseccomp2",
    "libblkid.so.1": "libblkid1", "libmount.so.1": "libmount1",
    "libpcre2-8.so.0": "libpcre2-8-0",
    "libtinfo.so.6": "libtinfo6", "libreadline.so.8": "libreadline8t64 | libreadline8",
}


# Qt's image-format plugins each link their own codec, and the soname differs
# between the distributions this package targets — Ubuntu 22.04 has
# libtiff.so.5 where Debian 12 has libtiff.so.6.  Demanding either would make
# the package refuse to install on half of them over a plugin nothing needs to
# start, so these are Recommends: apt fetches them, dpkg does not insist.
SONAME_OPTIONAL: dict[str, str] = {
    "libtiff.so.5": "libtiff5 | libtiff6",
    "libtiff.so.6": "libtiff6 | libtiff5",
    "libjpeg.so.8": "libjpeg8 | libjpeg-turbo8",
    "libjpeg.so.62": "libjpeg62-turbo | libjpeg62",
    "libwebp.so.7": "libwebp7", "libwebp.so.6": "libwebp6",
    "libwebpdemux.so.2": "libwebpdemux2", "libwebpmux.so.3": "libwebpmux3",
    "libjasper.so.4": "libjasper4", "libmng.so.2": "libmng2",
    # Qt's GTK platform theme plugin. Without it Qt draws with its own style,
    # so none of this may be allowed to block an install. It is bundled when
    # the build image happens to carry GTK and declared when it does not,
    # which is the difference between building on a desktop and in a container.
    "libgtk-3.so.0": "libgtk-3-0t64 | libgtk-3-0",
    "libgdk-3.so.0": "libgtk-3-0t64 | libgtk-3-0",
    "libatk-1.0.so.0": "libatk1.0-0t64 | libatk1.0-0",
    "libatk-bridge-2.0.so.0": "libatk-bridge2.0-0t64 | libatk-bridge2.0-0",
    "libatspi.so.0": "libatspi2.0-0t64 | libatspi2.0-0",
    "libcairo.so.2": "libcairo2",
    "libcairo-gobject.so.2": "libcairo-gobject2",
    "libgdk_pixbuf-2.0.so.0": "libgdk-pixbuf-2.0-0",
    "libpango-1.0.so.0": "libpango-1.0-0",
    "libpangocairo-1.0.so.0": "libpangocairo-1.0-0",
    "libpangoft2-1.0.so.0": "libpangoft2-1.0-0",
    "libepoxy.so.0": "libepoxy0",
    "libharfbuzz-gobject.so.0": "libharfbuzz-gobject0",
}


def _elf_sections(data: bytes) -> dict[str, bytes] | None:
    """Section name → bytes, for a 64-bit little-endian ELF; None otherwise."""
    if len(data) < 64 or data[:4] != _ELF_MAGIC:
        return None
    if data[4] != 2 or data[5] != 1:          # ELFCLASS64, ELFDATA2LSB
        return None

    shoff = int.from_bytes(data[40:48], "little")
    shentsize = int.from_bytes(data[58:60], "little")
    shnum = int.from_bytes(data[60:62], "little")
    shstrndx = int.from_bytes(data[62:64], "little")
    if not shoff or not shnum or shentsize < 40 or shstrndx >= shnum:
        return None
    if shoff + shnum * shentsize > len(data):
        return None

    def header(index: int) -> tuple[int, int, int]:
        base = shoff + index * shentsize
        return (int.from_bytes(data[base:base + 4], "little"),        # sh_name
                int.from_bytes(data[base + 24:base + 32], "little"),  # sh_offset
                int.from_bytes(data[base + 32:base + 40], "little"))  # sh_size

    _, names_offset, names_size = header(shstrndx)
    names = data[names_offset:names_offset + names_size]

    sections: dict[str, bytes] = {}
    for index in range(shnum):
        name_offset, offset, size = header(index)
        end = names.find(b"\0", name_offset)
        name = names[name_offset:end if end >= 0 else None].decode("ascii", "replace")
        sections[name] = data[offset:offset + size]
    return sections


def _elf_requirements(path: Path) -> tuple[set[str], tuple[int, ...]]:
    """The sonames this file needs, and the newest glibc it asks for."""
    try:
        data = path.read_bytes()
    except OSError:
        return set(), ()
    sections = _elf_sections(data)
    if not sections:
        return set(), ()
    strings = sections.get(".dynstr", b"")

    def text(offset: int) -> str:
        end = strings.find(b"\0", offset)
        return strings[offset:end if end >= 0 else None].decode("utf-8", "replace")

    # DT_NEEDED entries in .dynamic name the libraries the loader must find.
    needed: set[str] = set()
    dynamic = sections.get(".dynamic", b"")
    for base in range(0, len(dynamic) - 15, 16):
        tag = int.from_bytes(dynamic[base:base + 8], "little")
        if tag == 0:                                   # DT_NULL ends the table
            break
        if tag == _DT_NEEDED:
            needed.add(text(int.from_bytes(dynamic[base + 8:base + 16], "little")))

    # .gnu.version_r is where "this binary calls a glibc 2.38 symbol" is
    # written down, and it is the only honest source for the floor: the
    # loader refuses the file on anything older, whatever the package says.
    floor: tuple[int, ...] = ()
    verneed = sections.get(".gnu.version_r", b"")
    cursor = 0
    while cursor + 16 <= len(verneed):
        count = int.from_bytes(verneed[cursor + 2:cursor + 4], "little")
        aux = cursor + int.from_bytes(verneed[cursor + 8:cursor + 12], "little")
        following = int.from_bytes(verneed[cursor + 12:cursor + 16], "little")
        for _ in range(count):
            if aux + 16 > len(verneed):
                break
            name = text(int.from_bytes(verneed[aux + 8:aux + 12], "little"))
            if name.startswith("GLIBC_"):
                try:
                    version = tuple(int(part) for part in name[6:].split("."))
                except ValueError:
                    version = ()                       # GLIBC_PRIVATE and such
                if version > floor:
                    floor = version
            step = int.from_bytes(verneed[aux + 12:aux + 16], "little")
            if not step:
                break
            aux += step
        if not following:
            break
        cursor += following
    return needed, floor


def dependency_survey(
    binary_dir: Path,
) -> tuple[list[str], list[str], tuple[int, ...], list[str]]:
    """(required packages, recommended packages, glibc floor, unmapped sonames)."""
    bundled: set[str] = set()
    files: list[Path] = []
    for path in binary_dir.rglob("*"):
        if path.is_symlink():
            bundled.add(path.name)
        elif path.is_file():
            bundled.add(path.name)
            files.append(path)

    needed: set[str] = set()
    floor: tuple[int, ...] = ()
    for path in files:
        sonames, version = _elf_requirements(path)
        needed |= sonames
        if version > floor:
            floor = version

    packages: list[str] = []
    optional: list[str] = []
    unmapped: list[str] = []
    for soname in sorted(needed - bundled):
        if soname in SONAME_OPTIONAL:
            package = SONAME_OPTIONAL[soname]
            if package not in optional:
                optional.append(package)
            continue
        package = SONAME_PACKAGES.get(soname)
        if package is None:
            unmapped.append(soname)
        elif package not in packages:
            packages.append(package)
    return sorted(packages), sorted(optional), floor, unmapped


def floor_contributors(binary_dir: Path, floor: tuple[int, ...]) -> list[str]:
    """The files that ask for `floor` — the ones holding the package back.

    A version on its own says a problem exists and not where it is. Three
    guesses were spent on a 2.35 that turned out not to be PyInstaller and not
    to be Qt; naming the files answers it in one run.
    """
    named: list[str] = []
    for path in sorted(binary_dir.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        _, version = _elf_requirements(path)
        if version and version >= floor:
            named.append(str(path.relative_to(binary_dir)))
    return named


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

    # Measured from the tree that was just built, not written down here: see
    # dependency_survey().  Both outcomes are logged — a survey that found
    # nothing and one that was never asked look identical otherwise.
    required, recommended, floor, unmapped = dependency_survey(binary_dir)
    if floor:
        glibc = ".".join(str(part) for part in floor)
        required = [f"libc6 (>= {glibc})"] + [p for p in required if p != "libc6"]
        log(f"needs glibc {glibc} or newer — that is the oldest system this "
            f"package will install on")
    else:
        log("no glibc version requirement could be read from the binaries")
    log(f"Depends: {', '.join(required)}")
    if recommended:
        log(f"Recommends: {', '.join(recommended)}")
    for soname in unmapped:
        log(f"! {soname} is needed and no package is mapped to it — "
            f"NOT declared; add it to SONAME_PACKAGES")

    control = (
        f"Package: {APP_NAME}\n"
        f"Version: {VERSION}\n"
        f"Section: net\n"
        f"Priority: optional\n"
        f"Architecture: {architecture}\n"
        f"Installed-Size: {installed_size}\n"
        f"Maintainer: {MAINTAINER}\n"
        + (f"Depends: {', '.join(required)}\n" if required else "")
        + (f"Recommends: {', '.join(recommended)}\n" if recommended else "")
        + f"Description: {DESCRIPTION}\n"
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

    # appimagetool takes the icon from .DirIcon and only guesses without it.
    if icon.exists():
        shutil.copy(icon, appdir / ".DirIcon")

    # appimagetool ships as an AppImage itself and so wants FUSE, which a CI
    # runner and a build container generally do not have;
    # APPIMAGE_EXTRACT_AND_RUN makes it unpack itself and run from a temporary
    # directory instead.  IXD_APPIMAGETOOL names a copy that is not on PATH.
    tool = os.environ.get("IXD_APPIMAGETOOL") or shutil.which("appimagetool")
    if not tool:
        log("appimagetool was NOT found, so NO .AppImage was produced — the "
            "AppDir is ready. Install appimagetool, or point "
            "IXD_APPIMAGETOOL at a copy.")
        return None

    target = DIST / f"{APP_NAME}-{VERSION}-linux-x86_64.AppImage"
    target.unlink(missing_ok=True)
    log(f"appimagetool: {tool}")
    environment = dict(os.environ, APPIMAGE_EXTRACT_AND_RUN="1", ARCH="x86_64")
    if run([str(tool), str(appdir), str(target)], env=environment) != 0:
        log("appimagetool FAILED, so NO .AppImage was produced — the AppDir is "
            "still usable")
        return None
    log(f"wrote {target.name} ({target.stat().st_size / 1048576:.1f} MB)")
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
    this project reimplements. `makensis` runs on Linux, so this machine can
    compile it, and CI does too.

    **This is the custom-window installer**, and it replaced the MUI2 one on
    2026-08-20 at the user's word: *"from now on you can replace the old nsis
    installer with our custom one."* It publishes under the same
    `windows-x64-setup.exe` name, which matters more than it looks: an
    installed build asks its update for `("windows", "setup", ".exe")` and
    `Release.asset()` returns the first published file containing all three,
    so there must be exactly one. `build_windows_custom_installer` — the
    by-hand route that takes a payload argument — writes a name matching no
    upload glob, and nothing publishes it.

    The payload is `_installer_payload`, not the folder as it stands: what the
    installer packs has to carry the marker that says it was installed, or
    every copy from `setup.exe` offers a link to the release page instead of
    an update (§432).
    """
    from installer_custom import script as custom_script

    section("Windows installer")
    output = DIST / f"ixd-{VERSION}-windows-x64-setup.exe"
    payload = _installer_payload(binary_dir)
    script_path = DIST / "installer.nsi"
    script_path.write_text(custom_script(
        app_name=BUNDLE_NAME,
        app_slug="IXD",
        version=VERSION,
        publisher="IXD",
        output=str(output),
        payload=str(payload),
        launcher="ixd.exe",
        icon=str(ROOT / "packaging" / "icons" / "ixd.ico"),
        art=str(ROOT / "packaging" / "installer-art"),
        uninstall_key=(r"Software\Microsoft\Windows\CurrentVersion"
                       r"\Uninstall\IXD"),
    # **A BOM, and it is not decoration.** `makensis` reads a source file in
    # the system codepage unless something tells it otherwise, and on the
    # Windows runner that is CP1252 — which turned `·` into `Â·` and `—` into
    # `â€”` on the user's page one (§3.80). `Unicode true` sets the *output*
    # encoding and has nothing to say about this. The BOM travels with the
    # file, so `dist/installer.nsi` compiled by hand is right too.
    ), encoding="utf-8-sig")
    log(f"wrote {script_path.name}")
    if not have("makensis"):
        log("makensis is not installed here, so the installer is not compiled")
        return None
    # `-INPUTCHARSET UTF8` says the same thing at the call site. Either alone
    # is enough; both cost nothing and the BOM is the one that survives being
    # compiled by somebody else.
    if run(["makensis", "-V2", "-INPUTCHARSET", "UTF8",
            str(script_path)]) != 0:
        log("makensis refused the script; no installer was produced")
        return None
    log(f"wrote {output.name} ({output.stat().st_size / 1048576:.1f} MB)")
    return output


def build_windows_custom_installer(payload: Path) -> Path | None:
    """The custom-window installer, published **beside** the MUI2 one.

    The user asked for it to be built and shipped as an extra so they can test
    it before it replaces anything: *"before push it for later release instead
    of old installer let me test it then when i told you you can replace the
    old one with the custom one."*

    Its name deliberately avoids the word *setup*. `Release.asset()` returns
    the first published file whose name contains every piece it is given, and
    an updating build asks for `("windows", "setup", ".exe")` — a second
    installer matching that could be handed to a machine as its update
    depending only on the order GitHub lists assets in.
    """
    from installer_custom import script as custom_script

    section("Windows installer (custom window)")
    output = DIST / f"ixd-{VERSION}-windows-x64-custom-installer.exe"
    script_path = DIST / "installer-custom.nsi"
    script_path.write_text(custom_script(
        app_name=BUNDLE_NAME,
        app_slug="IXD",
        version=VERSION,
        publisher="IXD",
        output=str(output),
        payload=str(payload),
        launcher="ixd.exe",
        icon=str(ROOT / "packaging" / "icons" / "ixd.ico"),
        art=str(ROOT / "packaging" / "installer-art"),
        uninstall_key=(r"Software\Microsoft\Windows\CurrentVersion"
                       r"\Uninstall\IXD"),
    # **A BOM, and it is not decoration.** `makensis` reads a source file in
    # the system codepage unless something tells it otherwise, and on the
    # Windows runner that is CP1252 — which turned `·` into `Â·` and `—` into
    # `â€”` on the user's page one (§3.80). `Unicode true` sets the *output*
    # encoding and has nothing to say about this. The BOM travels with the
    # file, so `dist/installer.nsi` compiled by hand is right too.
    ), encoding="utf-8-sig")
    log(f"wrote {script_path.name}")
    if not have("makensis"):
        log("makensis is not installed here, so it is not compiled")
        return None
    # `-INPUTCHARSET UTF8` says the same thing at the call site. Either alone
    # is enough; both cost nothing and the BOM is the one that survives being
    # compiled by somebody else.
    if run(["makensis", "-V2", "-INPUTCHARSET", "UTF8",
            str(script_path)]) != 0:
        log("makensis refused the custom script; nothing was produced")
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
    "linux": f"ixd-{VERSION}-linux-x86_64-portable.tar.gz",
    "darwin": f"ixd-{VERSION}-macos-arm64-portable.zip",
    "win32": f"ixd-{VERSION}-windows-x64-portable.zip",
}

#: What the build records in its own marker — the same file, with the version
#: taken out. A marker that names one release's file cannot match the next
#: one's: 1.0.8 on Windows recorded `ixd-1.0.8-windows-x64-selfupdate.zip`,
#: searched the 1.0.9 release for that exact string, and reported that nothing
#: it could use had been published while listing the file it wanted.
SELF_UPDATE_PATTERNS = {
    "linux": "linux-x86_64-portable.tar.gz",
    "darwin": "macos-arm64-portable.zip",
    "win32": "windows-x64-portable.zip",
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

    Published *instead of* a second plain archive, since 1.0.45. Every release
    up to 1.0.44 shipped two portable copies per platform that differed by that
    one file, and nobody could tell from the names which one to take — so there
    is one now, it is called `portable`, and it is the one that can update
    itself.
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
    parser.add_argument("--custom-installer", nargs="?", const="", default=None,
                        metavar="PAYLOAD",
                        help="only build the custom-window Windows installer "
                             "(not built by a normal run, and not published). "
                             "Takes the folder to pack, which for a testable "
                             "installer is a real Windows build unpacked from "
                             "the release's ixd-<version>-windows-x64.zip; "
                             "defaults to dist/ixd, which on this machine is a "
                             "Linux build and only compiles the script")
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

    if arguments.custom_installer is not None:
        # Deliberately not part of any normal run and matching no upload glob:
        # it is built when it is asked for, tested by hand, and published only
        # when the user says so.
        #
        # The payload is an argument because the useful one is never the folder
        # this machine just built: PyInstaller does not cross-compile, so a
        # testable Windows installer is made by packing a real Windows build
        # (§3.64). That used to be a manual swap of dist/ixd and nothing
        # recorded it.
        payload = Path(arguments.custom_installer) if arguments.custom_installer \
            else DIST / "ixd"
        if not (payload / "ixd.exe").is_file():
            log(f"note: {payload} holds no ixd.exe — this compiles the script "
                f"but the installer it makes will not run on Windows")
        build_windows_custom_installer(payload)
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
    # Before anything packages `binary`: every archive, installer and .deb
    # below copies that folder, so the extension has to be in it first.
    materialise_extension(binary if not IS_MACOS
                          else binary / "Contents" / "MacOS")

    if arguments.package:
        if IS_LINUX:
            build_deb(binary)
            build_appimage(binary)
        elif IS_MACOS:
            build_dmg(binary)
            build_macos_pkg(binary)
        elif IS_WINDOWS:
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
