#!/bin/sh
# Convenience launcher: prefers the project venv, falls back to python3.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -x "$DIR/.venv/bin/python" ]; then
    exec "$DIR/.venv/bin/python" -m ixd "$@"
fi
exec python3 -m ixd "$@"
