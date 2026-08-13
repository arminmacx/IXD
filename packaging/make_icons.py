"""Generate the application and extension icons.

No imaging library is available (and none should be required to build), so the
icons are rasterised here and written with a small PNG encoder built on the
standard library's ``zlib``.  Shapes are supersampled 4x and box-filtered down,
which gives clean antialiased edges.

Run:  python packaging/make_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_ICONS = ROOT / "extension" / "icons"
APP_ICONS = ROOT / "packaging" / "icons"

SUPERSAMPLE = 4
ACCENT_TOP = (91, 140, 255)
ACCENT_BOTTOM = (126, 96, 255)
GLYPH = (255, 255, 255)


def write_png(path: Path, width: int, height: int, pixels: bytearray) -> None:
    """Write RGBA8 pixel data as a PNG."""
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)                                  # filter type 0 (None)
        raw += pixels[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def _inside_rounded_rect(x: float, y: float, size: float, radius: float) -> bool:
    if x < 0 or y < 0 or x >= size or y >= size:
        return False
    for corner_x, corner_y in ((radius, radius), (size - radius, radius),
                               (radius, size - radius), (size - radius, size - radius)):
        in_x = x < radius if corner_x == radius else x > size - radius
        in_y = y < radius if corner_y == radius else y > size - radius
        if in_x and in_y:
            return (x - corner_x) ** 2 + (y - corner_y) ** 2 <= radius * radius
    return True


def _inside_glyph(x: float, y: float, size: float) -> bool:
    """A downward arrow above a baseline — the universal download mark."""
    unit = size / 32.0
    cx = size / 2.0

    # Shaft
    if abs(x - cx) <= 2.6 * unit and 7.0 * unit <= y <= 18.0 * unit:
        return True

    # Arrow head: an isosceles triangle pointing down.
    head_top = 16.0 * unit
    head_bottom = 23.0 * unit
    half_width = 7.2 * unit
    if head_top <= y <= head_bottom:
        span = half_width * (1.0 - (y - head_top) / (head_bottom - head_top))
        if abs(x - cx) <= span:
            return True

    # Baseline tray
    if 25.0 * unit <= y <= 27.6 * unit and 7.0 * unit <= x <= 25.0 * unit:
        return True
    return False


def render(size: int) -> bytearray:
    """Rasterise one icon at ``size`` pixels square."""
    hi = size * SUPERSAMPLE
    radius = hi * 0.22
    samples = SUPERSAMPLE * SUPERSAMPLE

    # Accumulate coverage and colour at high resolution, then box-filter down.
    pixels = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            red = green = blue = alpha = 0
            for sub_y in range(SUPERSAMPLE):
                for sub_x in range(SUPERSAMPLE):
                    hx = x * SUPERSAMPLE + sub_x + 0.5
                    hy = y * SUPERSAMPLE + sub_y + 0.5
                    if not _inside_rounded_rect(hx, hy, hi, radius):
                        continue
                    ratio = hy / hi
                    base = tuple(
                        int(ACCENT_TOP[i] + (ACCENT_BOTTOM[i] - ACCENT_TOP[i]) * ratio)
                        for i in range(3)
                    )
                    colour = GLYPH if _inside_glyph(hx, hy, hi) else base
                    red += colour[0]
                    green += colour[1]
                    blue += colour[2]
                    alpha += 255

            offset = (y * size + x) * 4
            if alpha == 0:
                continue
            covered = alpha // 255
            pixels[offset] = red // covered
            pixels[offset + 1] = green // covered
            pixels[offset + 2] = blue // covered
            pixels[offset + 3] = alpha // samples
    return pixels


#: An ICNS is a container, not an image format: a header, then typed entries.
#: Since OS X 10.7 the entries below hold a PNG *verbatim*, which is why this
#: can be written here rather than needing an imaging library or `iconutil`.
#: The `@2x` types are the same pixel count as a larger plain type — `ic11` is
#: 16pt at 2x, so 32 pixels — and macOS wants both present.
_ICNS_ENTRIES = (
    (b"icp4", 16),    # 16pt
    (b"icp5", 32),    # 32pt
    (b"icp6", 64),    # 64pt
    (b"ic07", 128),   # 128pt
    (b"ic08", 256),   # 256pt
    (b"ic11", 32),    # 16pt @2x
    (b"ic12", 64),    # 32pt @2x
    (b"ic13", 256),   # 128pt @2x
)


def write_icns(path: Path, sizes: dict[int, bytes]) -> None:
    """Write an ICNS built from PNGs already rendered.

    Without this the macOS build fails outright: PyInstaller accepts only
    `.icns` on that platform and refuses a PNG rather than converting it — the
    exact wording is "which exists but is not in the correct format" — so the
    PNG fallback in the spec produced no application at all.
    """
    body = bytearray()
    for entry_type, size in _ICNS_ENTRIES:
        png = sizes.get(size)
        if png is None:
            continue
        body += entry_type + struct.pack(">I", 8 + len(png)) + png
    payload = b"icns" + struct.pack(">I", 8 + len(body)) + bytes(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def main() -> int:
    targets = [
        (EXTENSION_ICONS / "icon16.png", 16),
        (EXTENSION_ICONS / "icon32.png", 32),
        (EXTENSION_ICONS / "icon48.png", 48),
        (EXTENSION_ICONS / "icon128.png", 128),
        (APP_ICONS / "ixd-16.png", 16),
        (APP_ICONS / "ixd-32.png", 32),
        (APP_ICONS / "ixd-64.png", 64),
        (APP_ICONS / "ixd-128.png", 128),
        (APP_ICONS / "ixd-256.png", 256),
    ]
    for path, size in targets:
        write_png(path, size, size, render(size))
        print(f"  wrote {path.relative_to(ROOT)} ({size}x{size})")

    icns_path = APP_ICONS / "ixd.icns"
    write_icns(icns_path, {
        size: (APP_ICONS / f"ixd-{size}.png").read_bytes()
        for size in {size for _type, size in _ICNS_ENTRIES}
        if (APP_ICONS / f"ixd-{size}.png").exists()
    })
    print(f"  wrote {icns_path.relative_to(ROOT)} "
          f"({len(_ICNS_ENTRIES)} entries, {icns_path.stat().st_size} bytes)")
    print(f"{len(targets) + 1} icons generated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
