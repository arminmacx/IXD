"""Generate the extension's permanent identity.

Run once.  It creates an RSA key pair, writes the public half into
``extension/manifest.chrome.json`` as the ``"key"`` field and stores the private
half in ``packaging/extension-key.pem`` (needed only if the extension is ever
packed into a ``.crx``).

Embedding the key fixes the extension's Chrome ID forever, which is what lets
the desktop application register the native-messaging host *before* the user
loads the extension.  Without it the ID changes with the unpacked directory
path and the whole integration has to be wired up by hand.

    python packaging/make_extension_key.py          # generate if missing
    python packaging/make_extension_key.py --force  # regenerate (changes the ID)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ixd.core.chromeid import extension_id_from_manifest_key, generate_key_pair

MANIFEST = ROOT / "extension" / "manifest.chrome.json"
PRIVATE_KEY = ROOT / "packaging" / "extension-key.pem"
ID_FILE = ROOT / "extension" / "chrome-extension-id.txt"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true",
                        help="replace an existing key (the extension ID changes)")
    parser.add_argument("--bits", type=int, default=2048, help="RSA key size")
    arguments = parser.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    existing = manifest.get("key", "")

    if existing and not arguments.force:
        identifier = extension_id_from_manifest_key(existing)
        print(f"Key already present. Extension ID: {identifier}")
        ID_FILE.write_text(identifier + "\n", encoding="utf-8")
        return 0

    print(f"Generating a {arguments.bits}-bit RSA key pair…")
    manifest_key, private_pem, identifier = generate_key_pair(arguments.bits)

    # "key" must precede the rest for readability; rebuild in a stable order.
    ordered = {"manifest_version": manifest["manifest_version"], "key": manifest_key}
    ordered.update({k: v for k, v in manifest.items() if k not in ordered})
    MANIFEST.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")

    PRIVATE_KEY.write_text(private_pem, encoding="utf-8")
    PRIVATE_KEY.chmod(0o600)
    ID_FILE.write_text(identifier + "\n", encoding="utf-8")

    print(f"Extension ID: {identifier}")
    print(f"Private key : {PRIVATE_KEY}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
