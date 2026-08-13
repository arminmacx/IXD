#!/usr/bin/env bash
# =============================================================================
#  Internet Xtreme Downloader — build for Linux
#
#      ./packaging/linux/build.sh
#
#  Finds a usable Python, installs one through the system package manager if
#  there is none, creates the virtual environment, installs the dependencies
#  and produces dist/ixd/ plus a .deb and an AppDir.
#
#  Everything it prints is also written to build-linux.log. If a build fails,
#  that file is the thing to send back.
#
#  Options
#    --no-install   never install anything; fail instead if Python is missing
#    --with-tests   run the offline test suites before building
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT" || exit 1
LOG="$ROOT/build-linux.log"
MIN_PY="3.11"

ALLOW_INSTALL=1
WITH_TESTS=0
for argument in "$@"; do
    case "$argument" in
        --no-install) ALLOW_INSTALL=0 ;;
        --with-tests) WITH_TESTS=1 ;;
        -h|--help) sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

say "Internet Xtreme Downloader — Linux build"
say "Working in $ROOT"
say ""

# --- 1. a Python we can build with -------------------------------------------
# Newest first: the project is developed on 3.14, and anything from 3.11 up has
# the syntax and the standard library this code uses.
usable() {
    [ -x "$(command -v "$1" 2>/dev/null)" ] || return 1
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
        >/dev/null 2>&1
}

PYTHON=""
# An explicit choice wins over discovery. Continuous integration pins the
# interpreter it has just installed, and a machine with several Pythons can
# name the one to build against instead of arguing with the search order.
if [ -n "${IXD_PYTHON:-}" ] && usable "$IXD_PYTHON"; then
    PYTHON="$IXD_PYTHON"
fi
if [ -z "$PYTHON" ]; then
    for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
        if usable "$candidate"; then PYTHON="$candidate"; break; fi
    done
fi

install_python() {
    # The package name for the venv module differs per distribution, and on
    # Debian and Ubuntu it is a *separate* package — python3 alone gives you an
    # interpreter that cannot create the environment this build needs.
    local sudo=""
    [ "$(id -u)" -ne 0 ] && sudo="sudo"
    say "Python $MIN_PY or newer was not found. Installing it..."
    say "(this needs administrator rights and will ask for your password)"

    if command -v apt-get >/dev/null 2>&1; then
        run $sudo apt-get update
        run $sudo apt-get install -y python3 python3-venv python3-pip
    elif command -v dnf >/dev/null 2>&1; then
        run $sudo dnf install -y python3 python3-pip
    elif command -v pacman >/dev/null 2>&1; then
        run $sudo pacman -Sy --noconfirm python python-pip
    elif command -v zypper >/dev/null 2>&1; then
        run $sudo zypper --non-interactive install python3 python3-pip
    elif command -v apk >/dev/null 2>&1; then
        run $sudo apk add --no-cache python3 py3-pip
    else
        fail "no supported package manager found (apt, dnf, pacman, zypper, apk).
       Install Python $MIN_PY or newer yourself and run this again."
    fi
}

if [ -z "$PYTHON" ]; then
    [ "$ALLOW_INSTALL" -eq 1 ] || fail "Python $MIN_PY+ is missing and --no-install was given."
    install_python
    for candidate in python3.14 python3.13 python3.12 python3.11 python3; do
        if usable "$candidate"; then PYTHON="$candidate"; break; fi
    done
    [ -n "$PYTHON" ] || fail "Python was installed but still cannot be found on PATH."
fi
say "Python: $($PYTHON -c 'import sys,platform; print(sys.version.split()[0], platform.machine())')"

# --- 2. the environment ------------------------------------------------------
VENV="$ROOT/.venv"
VPY="$VENV/bin/python"
if [ ! -x "$VPY" ]; then
    say "Creating the environment (this happens once)..."
    if ! run "$PYTHON" -m venv "$VENV"; then
        # Debian and Ubuntu ship venv separately and say so only in the error.
        say "venv is unavailable; installing the package that provides it..."
        if [ "$ALLOW_INSTALL" -eq 1 ] && command -v apt-get >/dev/null 2>&1; then
            sudo_prefix=""; [ "$(id -u)" -ne 0 ] && sudo_prefix="sudo"
            run $sudo_prefix apt-get install -y "python3-venv" \
                "$($PYTHON -c 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}-venv")')"
        fi
        run "$PYTHON" -m venv "$VENV" || fail "could not create $VENV"
    fi
fi
[ -x "$VPY" ] || fail "$VPY is missing"

say "Installing dependencies..."
run "$VPY" -m pip install --upgrade pip
if ! run "$VPY" -m pip install -r "$ROOT/requirements.txt"; then
    # A pinned Qt has no wheel for every Python; the application does not
    # require that exact version, so the newest one is a fair second attempt.
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
missing=0
for artefact in "dist/ixd/ixd" "dist/ixd-extension-chrome-1.0.0.zip"; do
    if [ ! -e "$ROOT/$artefact" ]; then say "MISSING: $artefact"; missing=1; fi
done
[ "$missing" -eq 0 ] || fail "the build reported success but the files are not there"

say ""
say "Done."
say "  Application : dist/ixd/ixd"
say "  Package     : $(ls -1 "$ROOT/dist"/*.deb 2>/dev/null | head -1 | sed "s|$ROOT/||")"
say "  Extension   : dist/ixd-extension-chrome-1.0.0.zip"
say ""
say "Load the extension: chrome://extensions → Developer mode → Load unpacked"
say "→ pick ~/.local/share/ixd/extension"
