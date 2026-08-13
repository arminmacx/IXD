"""Application entry point.

Modes
-----
``python -m ixd``                  launch the GUI (default)
``python -m ixd --background``     run headless: engine, scheduler and control socket
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
                        help="run without a window (tray/daemon mode)")
    parser.add_argument("--native-host", action="store_true",
                        help="run as the browser native messaging host")
    parser.add_argument("--add", action="append", default=[],
                        help="queue a URL in the running instance and exit")
    parser.add_argument("--media", action="store_true",
                        help="treat --add URLs as media pages to extract")
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


def _build_service():
    from .service import DownloadService

    config.ensure_dirs()
    service = DownloadService()
    service.start()
    _register_browser_integration(service)
    return service


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
    """Bind the control socket, tolerating a busy port."""
    from .ipc.server import IPCServer

    try:
        server = IPCServer(service)
    except OSError:
        # Port in use: fall back to an ephemeral one and publish that instead.
        try:
            server = IPCServer(service, port=0)
        except OSError as exc:
            print(f"warning: control socket unavailable ({exc})", file=sys.stderr)
            return None
    server.start()
    return server


def run_background(urls: list[str], media: bool) -> int:
    """Headless mode: engine + scheduler + control socket, no Qt."""
    service = _build_service()
    server = _start_ipc(service)

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

    service = _build_service()
    palette = DARK.with_accent(service.settings.get("accent", "#5B8CFF"))
    apply_theme(app, palette)

    window = MainWindow(service, palette)
    server = _start_ipc(service)
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
        return run_gui(urls, arguments.media, start_hidden=False)
    except ImportError as exc:
        print(
            f"the graphical interface is unavailable ({exc}); "
            "falling back to background mode",
            file=sys.stderr,
        )
        return run_background(urls, arguments.media)


if __name__ == "__main__":
    raise SystemExit(main())
