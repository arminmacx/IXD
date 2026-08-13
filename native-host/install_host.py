"""Register (or remove) the Native Messaging host manifest.

Everything is discovered automatically: which browsers are installed, whether
they are distribution packages, snaps or flatpaks, where each one expects its
manifest, and what ID Chrome will give the bundled extension.

    python native-host/install_host.py             # register everywhere
    python native-host/install_host.py --verify     # prove the round trip works
    python native-host/install_host.py --status     # show what is registered
    python native-host/install_host.py --uninstall

``--extension-id`` is still accepted for a hand-modified unpacked copy whose
manifest ``key`` was removed, but it is no longer required: the shipped
manifest carries a fixed public key, so the ID is derived locally.
"""

from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ixd import integration                                   # noqa: E402
from ixd.core.browsers import HOST_NAME, all_browsers          # noqa: E402


def show_status() -> int:
    rows = integration.status()
    identifier = integration.bundled_extension_id()
    print(f"Native messaging host: {HOST_NAME}")
    print(f"Extension ID         : {identifier or '(no key in the manifest)'}")
    launcher = integration.launcher_path()
    binary = integration.frozen_executable()
    print(f"Launcher             : {launcher}"
          f"{'' if launcher.exists() else '  (not written yet)'}")
    print(f"Self-contained build : {binary or 'no — running from source'}")

    if not rows:
        print("\nNo browser profiles were found.")
        return 0

    print("\nBrowsers:")
    for row in rows:
        state = "registered" if row["registered"] else "NOT registered"
        sandbox = " [sandboxed]" if row["sandboxed"] else ""
        print(f"  • {row['name']:<26} {state}{sandbox}")
        print(f"      manifest: {row['host_dir']}")
        print(f"      launcher: {row['launcher']}")
    return 0


def _exercise(launcher: Path) -> tuple[bool, str]:
    """Run one launcher exactly as a browser would and read its reply."""
    request = json.dumps({"id": 1, "command": "ping", "params": {}}).encode("utf-8")
    payload = struct.pack("@I", len(request)) + request

    try:
        completed = subprocess.run(
            [str(launcher)], input=payload, capture_output=True, timeout=90,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"could not execute the launcher: {exc}"

    output = completed.stdout
    if len(output) < 4:
        detail = completed.stderr.decode("utf-8", "replace")[:400].strip()
        return False, f"no framed reply. {detail or 'No diagnostic output.'}"

    (length,) = struct.unpack("@I", output[:4])
    try:
        response = json.loads(output[4:4 + length].decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return False, f"malformed reply ({exc})"

    if response.get("ok") is True:
        result = response.get("result") or {}
        return True, (f"version {result.get('version')} "
                      f"(pid {result.get('pid')})")
    return False, str(response.get("error"))


def verify() -> int:
    """Prove the whole chain works, for every launcher actually registered.

    This is the only check worth anything: it exercises the shim, the
    interpreter or frozen binary behind it, the control socket and the service.
    Confirming that a file landed in a plausible directory proves nothing —
    that is exactly what used to pass while the browser was being refused
    permission to execute it.
    """
    launchers = integration.registered_launchers()
    if not launchers:
        print("No browsers are registered yet. Run this script without --verify.")
        return 1

    failures = 0
    for launcher in launchers:
        if not launcher.exists():
            print(f"FAILED  {launcher} — not written yet")
            failures += 1
            continue
        ok, detail = _exercise(launcher)
        print(f"{'OK     ' if ok else 'FAILED '} {launcher}\n         {detail}")
        failures += 0 if ok else 1

    if failures:
        return 1
    print("\nEvery registered launcher answered.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--extension-id", action="append", default=[],
                        help="additional Chrome extension ID to allow (repeatable)")
    parser.add_argument("--all-browsers", action="store_true",
                        help="write manifests even for browsers that appear absent")
    parser.add_argument("--chromium-only", action="store_true",
                        help="skip Firefox")
    parser.add_argument("--host-dir", action="append", default=[],
                        help="also write the manifest to this directory, for a "
                             "browser installed somewhere unusual (repeatable)")
    parser.add_argument("--status", action="store_true", help="show the current state")
    parser.add_argument("--verify", action="store_true",
                        help="launch the host the way a browser does and check the reply")
    parser.add_argument("--uninstall", action="store_true", help="remove the manifests")
    arguments = parser.parse_args()

    if arguments.status:
        return show_status()

    if arguments.uninstall:
        removed = integration.uninstall()
        print("Removed:" if removed else "Nothing to remove.")
        for entry in removed:
            print(f"  • {entry}")
        return 0

    families = ("chromium",) if arguments.chromium_only else ("chromium", "firefox")
    result = integration.install(
        arguments.extension_id,
        include_missing=arguments.all_browsers,
        families=families,
        extra_dirs=arguments.host_dir,
    )
    print(result.render())

    if result.registered:
        print("\nLoad the extension:")
        print(f"  chrome://extensions → Developer mode → Load unpacked → "
              f"{integration.extension_dir()}")
        print("  (the manifest already carries the matching key, so no further "
              "registration is needed)")

    if arguments.verify:
        print()
        return verify()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
