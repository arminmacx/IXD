"""Application entry point.

Modes
-----
``python -m ixd``                  launch the GUI (default)
``python -m ixd --background``     run headless: engine, scheduler and control socket
``python -m ixd --hidden``         run with the window hidden, ready to be shown
``python -m ixd --native-host``    act as the browser's Native Messaging host
``python -m ixd --add URL``        hand a URL to the running instance (or start one)

A single instance owns the control socket; a second launch simply focuses the
first one instead of starting a competing engine on the same database.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from . import __version__, config


#: How a browser identifies itself when it spawns a native messaging host.
#:
#: Chrome-family browsers pass the calling extension's origin as the first
#: argument; Firefox passes the manifest's path and then the add-on id. Neither
#: passes `--native-host`, because neither knows about it — the flag is ours,
#: and on Linux a shell shim adds it.
#:
#: On Windows that shim is a `.bat`, which means `cmd.exe` sits in the middle of
#: a **binary** pipe carrying length-prefixed messages. Recognising the browser's
#: own argument lets the manifest point straight at the executable and takes
#: `cmd.exe` out of the path entirely — one fewer thing between the browser and
#: the host, on the platform where the host was reported not to start at all.
def _launched_by_a_browser(argv: list[str]) -> bool:
    for argument in argv:
        if argument.startswith(("chrome-extension://", "moz-extension://")):
            return True
        # Firefox hands over the manifest it read, which is ours and is named
        # after the host. Anything else ending in `.json` is not this.
        if argument.lower().endswith(".json") and "ixd" in argument.lower():
            return True
    return False


def _parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ixd",
        description="Internet Xtreme Downloader — accelerated, resumable downloads.",
    )
    parser.add_argument("urls", nargs="*", help="URLs to queue on startup")
    parser.add_argument("--background", "--daemon", action="store_true",
                        help="run without a window at all (headless daemon)")
    parser.add_argument("--hidden", action="store_true",
                        help="run with the window hidden, ready to be shown")
    parser.add_argument("--native-host", action="store_true",
                        help="run as the browser native messaging host")
    parser.add_argument("--add", action="append", default=[],
                        help="queue a URL in the running instance and exit")
    parser.add_argument("--media", action="store_true",
                        help="treat --add URLs as media pages to extract")
    # The other half of an update: run by the *staged* copy, never by a user.
    # A program cannot replace the folder it is running from, so the new build
    # is what waits for the old one to exit and does the swap.
    parser.add_argument("--apply-update", metavar="FOLDER", default="",
                        help=argparse.SUPPRESS)
    parser.add_argument("--wait-for", type=int, default=0,
                        help=argparse.SUPPRESS)
    parser.add_argument("--version", action="version", version=f"Internet Xtreme Downloader {__version__}")
    # A browser's own argument is not one argparse knows, and it must not be
    # treated as a URL to queue either.
    if _launched_by_a_browser(argv):
        namespace = parser.parse_args([])
        namespace.native_host = True
        return namespace
    return parser.parse_args(argv)


def _run_native_host() -> int:
    from .ipc.native_host import main as native_main
    return native_main()


def _send_to_running_instance(urls: list[str], media: bool) -> bool:
    """Forward URLs to an existing instance. True when it accepted them."""
    from .ipc.server import IPCClient, is_running

    if not is_running():
        return False
    try:
        with IPCClient(timeout=10.0) as client:
            for url in urls:
                command = "add_media" if media else "add"
                response = client.call(command, {"url": url})
                if not response.get("ok"):
                    print(f"error: {response.get('error')}", file=sys.stderr)
            if not urls:
                client.call("ping")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"could not reach the running instance: {exc}", file=sys.stderr)
        return False


def _build_service(stage=None):
    """Bring the engine up, reporting each step to whatever is on screen.

    `stage` is called with the name of what is about to happen. The splash uses
    it to say something true rather than to animate over a guess; everything
    else passes nothing and it does nothing.
    """
    from .service import DownloadService

    def say(message: str) -> None:
        if stage is not None:
            stage(message)

    config.ensure_dirs()
    service = DownloadService()
    say("Starting the transfer engine…")
    service.start()
    say("Checking the browser integration…")
    _register_browser_integration(service)
    _register_autostart(service)
    return service


def _register_autostart(service) -> None:
    """Keep the session's copy of the setting current, silently.

    Same reasoning as the browser manifests: refreshed on every start rather
    than written once, so a rebuilt or moved application keeps starting with
    the session instead of pointing it at a path that no longer exists.
    """
    try:
        from . import autostart

        autostart.apply(service.settings.get_bool("launch_at_startup", False))
    except Exception as exc:  # noqa: BLE001 - never block start-up on this
        service.db.log_event(f"Launch at startup: {exc}", level="warning")


def _register_browser_integration(service) -> None:
    """Keep the browsers' native-messaging manifests current, silently.

    Doing this on every start rather than asking the user to run an installer
    is what makes the extension "just connect": the manifest is rewritten
    whenever the application moves, is rebuilt, or a browser is installed for
    the first time.
    """
    if not service.settings.get_bool("browser_integration", True):
        return
    try:
        from . import integration

        # Written out on **every** start, not only when a registration turns
        # out to be stale. `ensure_registered` is a no-op once the manifests
        # point at the current launcher — which is the normal case — and the
        # extension was only ever materialised inside that no-op. So a new
        # version of the application shipped a new extension that the folder
        # the browser loads never received: reported as an extension stuck on
        # the previous version through repeated launches and reloads.
        try:
            folder = integration.extension_dir()
            firefox = integration.firefox_extension_dir()
            # Which of the two locations was chosen, and why. A field report
            # that says "it is still in AppData" cannot be answered without
            # this: the fallback and the preferred path look identical from
            # outside, and a copy left over from an older version looks like a
            # copy this launch just wrote.
            root = integration.extension_root()
            if root == integration.installation_dir():
                service.db.log_event(
                    f"Extension folders live with the application, in {root}")
            else:
                service.db.log_event(
                    f"The installation at {integration.installation_dir()} is "
                    f"not writable, so the extension folders are in {root}")
            for gone in integration.retire_legacy_extension_copies(root):
                service.db.log_event(
                    f"Removed an extension folder left by an older version: "
                    f"{gone} — if a browser was loading from there, point it "
                    f"at {folder} instead")
            # The manifest is named, not assumed. A folder that exists and a
            # folder a browser can load are different things, and the
            # difference — a missing `manifest.json` — is what a browser
            # reports as a corrupted extension. Both outcomes are logged so a
            # field report says which one happened.
            for label, path in (("Chrome", folder), ("Firefox", firefox)):
                manifest = path / "manifest.json"
                if manifest.is_file():
                    service.db.log_event(
                        f"{label} extension ready at {path} "
                        f"(manifest.json, {manifest.stat().st_size} bytes)")
                else:
                    service.db.log_event(
                        f"{label} extension at {path} has no manifest.json — "
                        "the browser will refuse it", level="error")
        except Exception as exc:  # noqa: BLE001 - registration still matters
            service.db.log_event(f"Could not write the extension out: {exc}",
                                 level="warning")

        result = integration.ensure_registered()
    except Exception as exc:  # noqa: BLE001 - never block start-up on this
        service.db.log_event(f"Browser integration check failed: {exc}", level="warning")
        return

    if result is None:
        return
    if result.registered:
        service.db.log_event(
            "Browser integration registered for: "
            + ", ".join(entry.split(" → ")[0] for entry in result.registered)
        )
    for warning in result.warnings:
        service.db.log_event(f"Browser integration: {warning}", level="warning")
    for error in result.errors:
        service.db.log_event(f"Browser integration: {error}", level="error")


def _start_ipc(service):
    """Bind the control socket, which is what makes this the only instance.

    A busy port almost always means another copy of this application already
    owns the engine and the database. Answering that by binding an *ephemeral*
    port and publishing it started a second engine on the same state — two
    processes fetching the same downloads, one showing the window and the other
    doing the work. So a refusal is checked before it is worked around: if
    something answers a ping, this process has lost the race and says so.

    A port held by an unrelated program is the only case an ephemeral port is
    the right answer, and then it is taken.
    """
    from .ipc.server import IPCServer, is_running

    try:
        server = IPCServer(service)
    except OSError as exc:
        if is_running():
            raise AlreadyRunning() from exc
        try:
            server = IPCServer(service, port=0)
        except OSError as second:
            print(f"warning: control socket unavailable ({second})", file=sys.stderr)
            return None
    server.start()
    return server


class AlreadyRunning(RuntimeError):
    """Another instance owns the control socket, so this one must not run.

    Raised rather than returned because every caller has already built a
    service by this point: it has to be shut down, not merely abandoned, or
    the database keeps a second writer for the life of the process.
    """


def run_background(urls: list[str], media: bool) -> int:
    """Headless mode: engine + scheduler + control socket, no Qt."""
    service = _build_service()
    try:
        server = _start_ipc(service)
    except AlreadyRunning:
        # The browser's host launches this when it cannot reach an instance,
        # and two of those can arrive at once. The loser exits rather than
        # becoming a second engine on one database.
        service.shutdown()
        print("Internet Xtreme Downloader is already running")
        return 0

    for url in urls:
        try:
            if media:
                service.add_media(url)
            else:
                service.add_url(url)
        except Exception as exc:  # noqa: BLE001
            print(f"could not queue {url}: {exc}", file=sys.stderr)

    stop = threading.Event()

    def handle_signal(_signum, _frame):
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), handle_signal)
            except (ValueError, OSError):
                pass

    port = server.port if server else "n/a"
    print(f"Internet Xtreme Downloader {__version__} running in the background "
          f"(control port {port}). Press Ctrl+C to stop.")
    try:
        while not stop.wait(1.0):
            pass
    finally:
        if server is not None:
            server.stop()
        service.shutdown()
    return 0


def run_gui(urls: list[str], media: bool, start_hidden: bool) -> int:
    """Normal mode: the full Qt interface."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from .ui.main_window import MainWindow
    from .ui.theme import DARK, apply_theme

    from .desktop import DESKTOP_FILE_NAME, ensure_desktop_entry
    from .ui.widgets.tray import application_icon

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, False)
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Internet Xtreme Downloader")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("IXD")
    app.setQuitOnLastWindowClosed(False)      # the tray keeps the engine alive

    # The application icon, not merely the window's. Only the tray was ever
    # given one, which is why the status area showed the real icon and the
    # window, the dock and the switcher showed a placeholder.
    app.setWindowIcon(application_icon())
    # And the name a desktop actually resolves the icon *through*. Under
    # Wayland a window carries an app id rather than a picture: the compositor
    # looks up ``<app id>.desktop`` and takes the icon from there, so an
    # unset name leaves it deriving one from the executable — "python3" from a
    # source run — and no icon can be found under that. Setting the window
    # icon alone cannot fix this, which is why it looked like the icon was
    # missing rather than unnamed.
    app.setDesktopFileName(DESKTOP_FILE_NAME)
    ensure_desktop_entry()

    # Something on screen while this takes its second or two. Not shown for a
    # hidden start: there is no window coming, so a splash would announce a
    # launch the user did not make and then vanish.
    splash = None
    if not start_hidden:
        try:
            from .ui.widgets.splash import SplashScreen

            splash = SplashScreen(DARK)
            splash.show()
            splash.step("Starting…")
        except Exception:  # noqa: BLE001 - never block a launch on decoration
            splash = None

    def stage(message: str) -> None:
        if splash is not None:
            splash.step(message)

    stage("Opening the download database…")
    service = _build_service(stage)
    palette = DARK.with_accent(service.settings.get("accent", "#5B8CFF"))
    apply_theme(app, palette)

    stage("Building the interface…")
    window = MainWindow(service, palette)
    try:
        server = _start_ipc(service)
    except AlreadyRunning:
        # Another instance took the socket between the check at start-up and
        # this bind. It owns the engine; this process must not keep a second
        # one alive on the same database.
        service.shutdown()
        print("Internet Xtreme Downloader is already running")
        try:
            from .ipc.server import IPCClient
            with IPCClient(timeout=5.0) as client:
                client.call("focus")
        except Exception:  # noqa: BLE001
            pass
        return 0
    if server is not None:
        server.register("focus", lambda params: _focus(window))
        # The extension hands a page over; the choosing happens here. The
        # service has already remembered the session that made the address
        # work, so the dialog that opens is not starting from nothing.
        server.register("present", lambda params: _present(service, window, params))

    for url in urls:
        try:
            if media:
                service.add_media(url)
            else:
                service.add_url(url)
        except Exception as exc:  # noqa: BLE001
            print(f"could not queue {url}: {exc}", file=sys.stderr)

    if not start_hidden and not service.settings.get_bool("start_minimized", False):
        window.show()

    if splash is not None:
        # Held to a minimum on screen, faded out, and the window raised from
        # under it — a splash that closes the instant the window appears reads
        # as a flicker rather than as a start.
        splash.finish(window if window.isVisible() else None)

    def cleanup() -> None:
        if server is not None:
            server.stop()
        service.shutdown()

    app.aboutToQuit.connect(cleanup)
    return app.exec()


def _present(service, window, params: dict) -> dict:
    """Open the Add dialog on an address the browser handed over."""
    from PySide6.QtCore import QTimer

    result = service.handle_command("present", params)
    if not result.get("ok"):
        raise RuntimeError(result.get("error", "could not accept the page"))
    url = params.get("url", "") or ""
    QTimer.singleShot(0, lambda: (window.showNormal(), window.raise_(),
                                  window.activateWindow(),
                                  window.open_add_dialog(url)))
    return {"opened": True, "url": url}


def _focus(window) -> bool:
    """Bring the existing window forward (invoked over IPC)."""
    from PySide6.QtCore import QTimer

    QTimer.singleShot(0, lambda: (window.showNormal(), window.raise_(),
                                  window.activateWindow()))
    return True


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(list(sys.argv[1:] if argv is None else argv))

    if arguments.apply_update:
        from pathlib import Path
        from . import updater_ui

        # Its own process, its own small window, and nothing else loaded: this
        # is what is still running while the application's files are moved.
        return updater_ui.run(Path(arguments.apply_update),
                              wait_for=arguments.wait_for)

    if arguments.native_host:
        return _run_native_host()

    urls = list(arguments.urls) + list(arguments.add)

    # A second launch should never start a competing engine on the same state.
    if _send_to_running_instance(urls, arguments.media):
        if urls:
            print(f"handed {len(urls)} URL(s) to the running instance")
        else:
            from .ipc.server import IPCClient
            try:
                with IPCClient(timeout=5.0) as client:
                    client.call("focus")
            except Exception:  # noqa: BLE001
                pass
            print("Internet Xtreme Downloader is already running")
        return 0

    if arguments.background:
        return run_background(urls, arguments.media)

    try:
        return run_gui(urls, arguments.media, start_hidden=arguments.hidden)
    except ImportError as exc:
        print(
            f"the graphical interface is unavailable ({exc}); "
            "falling back to background mode",
            file=sys.stderr,
        )
        return run_background(urls, arguments.media)


if __name__ == "__main__":
    raise SystemExit(main())
