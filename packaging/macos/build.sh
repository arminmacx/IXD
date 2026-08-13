#!/usr/bin/env bash
# =============================================================================
#  Internet Xtreme Downloader — build for macOS
#
#      ./packaging/macos/build.sh
#
#  Finds a usable Python, installs one through Homebrew if there is none,
#  creates the virtual environment, installs the dependencies and produces
#  "Internet Xtreme Downloader.app" and a .dmg.
#
#  Everything it prints is also written to build-macos.log.
#
#  Options
#    --no-install   never install anything; fail instead if Python is missing
#    --with-tests   run the offline test suites before building
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1
LOG="$ROOT/build-macos.log"

ALLOW_INSTALL=1
WITH_TESTS=0
for argument in "$@"; do
    case "$argument" in
        --no-install) ALLOW_INSTALL=0 ;;
        --with-tests) WITH_TESTS=1 ;;
        -h|--help) sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $argument"; exit 2 ;;
    esac
done

: > "$LOG"
say()  { printf '%s\n' "$*"; printf '%s\n' "$*" >> "$LOG"; }
run()  { "$@" >> "$LOG" 2>&1; }
fail() {
    say ""
    say "FAILED: $*"
    # The last stretch of the log, because "the reason is in a file" is no use
    # to anyone reading this over a CI runner's shoulder — and not much use to
    # a person either.
    if [ -s "$LOG" ]; then
        say ""
        say "--- last 40 lines of $(basename "$LOG") ---"
        tail -n 40 "$LOG"
    fi
    say ""
    say "The whole log is in $LOG"
    exit 1
}

say "Internet Xtreme Downloader — macOS build"
say "Working in $ROOT"
say ""

# --- 1. a Python we can build with -------------------------------------------
# macOS ships a `python3` shim that is really the Command Line Tools stub: it
# exists on PATH and prompts for an install when run. `usable` therefore runs
# it rather than trusting that it is there.
usable() {
    command -v "$1" >/dev/null 2>&1 || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1
}

PYTHON=""
# An explicit choice wins over discovery — see the Linux script for why.
if [ -n "${IXD_PYTHON:-}" ] && usable "$IXD_PYTHON"; then
    PYTHON="$IXD_PYTHON"
fi
if [ -z "$PYTHON" ]; then
    for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
        if usable "$candidate"; then PYTHON="$candidate"; break; fi
    done
fi

if [ -z "$PYTHON" ]; then
    [ "$ALLOW_INSTALL" -eq 1 ] || fail "Python 3.11+ is missing and --no-install was given."
    if command -v brew >/dev/null 2>&1; then
        say "Python 3.11 or newer was not found. Installing it with Homebrew..."
        run brew install python@3.13 || fail "brew could not install Python"
        # Homebrew on Apple silicon installs under /opt/homebrew, which is not
        # on the PATH of a plain login shell until the shell profile is read.
        for prefix in /opt/homebrew/bin /usr/local/bin; do
            [ -d "$prefix" ] && PATH="$prefix:$PATH"
        done
        export PATH
        for candidate in python3.13 python3; do
            if usable "$candidate"; then PYTHON="$candidate"; break; fi
        done
    fi
    if [ -z "$PYTHON" ]; then
        fail "Python 3.11 or newer is required and could not be installed automatically.

       Install Homebrew and run this script again:
         /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"

       Or install Python directly from https://www.python.org/downloads/macos/
       and run this script again."
    fi
fi
say "Python: $($PYTHON -c 'import sys,platform; print(sys.version.split()[0], platform.machine())')"

# --- 2. the environment ------------------------------------------------------
VENV="$ROOT/.venv"
VPY="$VENV/bin/python"
if [ ! -x "$VPY" ]; then
    say "Creating the environment (this happens once)..."
    run "$PYTHON" -m venv "$VENV" || fail "could not create $VENV"
fi
[ -x "$VPY" ] || fail "$VPY is missing"

say "Installing dependencies..."
run "$VPY" -m pip install --upgrade pip
if ! run "$VPY" -m pip install -r "$ROOT/requirements.txt"; then
    say "The pinned PySide6 has no wheel for this Python; trying the latest."
    run "$VPY" -m pip install PySide6 pyinstaller || fail "dependencies would not install"
fi
run "$VPY" -c 'import PySide6, PyInstaller; print("PySide6", PySide6.__version__, "PyInstaller", PyInstaller.__version__)'

# --- 3. tests, if asked -------------------------------------------------------
if [ "$WITH_TESTS" -eq 1 ]; then
    say "Running the offline suites..."
    for suite in tests.test_engine tests.test_extractors tests.test_integration; do
        say "  $suite"
        run env QT_QPA_PLATFORM=offscreen "$VPY" -m "$suite" || fail "$suite reported failures"
    done
fi

# --- 4. build -----------------------------------------------------------------
say "Building (a few minutes)..."
run "$VPY" "$ROOT/packaging/build.py" --package || fail "the build did not complete"

# --- 5. and did it actually produce anything? --------------------------------
APP="$ROOT/dist/Internet Xtreme Downloader.app"
if [ ! -d "$APP" ] && [ ! -e "$ROOT/dist/ixd/ixd" ]; then
    fail "the build reported success but neither the .app nor dist/ixd/ixd is there"
fi

say ""
say "Done."
[ -d "$APP" ] && say "  Application : dist/Internet Xtreme Downloader.app"
DMG="$(ls -1 "$ROOT/dist"/*.dmg 2>/dev/null | head -1)"
[ -n "$DMG" ] && say "  Disk image  : $(printf '%s' "$DMG" | sed "s|$ROOT/||")"
say "  Extension   : dist/ixd-extension-chrome-1.0.0.zip"
say ""
say "The .app is not signed or notarised. On first launch macOS will refuse it:"
say "right-click the app → Open, or clear the quarantine flag with"
say "  xattr -dr com.apple.quarantine \"dist/Internet Xtreme Downloader.app\""
