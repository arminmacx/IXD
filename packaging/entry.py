"""Frozen-application entry point.

PyInstaller needs a plain script rather than ``python -m ixd``.  This also
normalises ``sys.path`` when running from a one-folder bundle so that the
packaged ``extension`` and ``native-host`` data files remain discoverable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _bundle_root() -> Path:
    """Directory containing the bundled data files."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def main() -> int:
    root = _bundle_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    # Let the native-host installer find the packaged binary.
    if getattr(sys, "frozen", False):
        os.environ.setdefault("IXD_EXECUTABLE", sys.executable)

    from ixd.__main__ import main as application_main

    return application_main()


if __name__ == "__main__":
    raise SystemExit(main())
