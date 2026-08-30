"""A record of a freeze, written while the application is frozen.

A field report on 2026-08-30 came with no evidence at all: the window stopped
answering, the reporter force-quit it, and the log held nothing but the next
launch's start-up lines.  That is structural, not bad luck.  The log lives in
SQLite and is written *by* the threads that stall, so the one event worth
recording is the one event that cannot record itself.

Two independent recorders, because the interesting failures are exactly the
ones that disable a recorder:

* ``faulthandler.dump_traceback_later`` runs in **C, on its own thread, with
  no need for the GIL**.  It is re-armed by every heartbeat from the Qt main
  thread, so it only ever fires when those heartbeats stop.  This is the one
  that survives total interpreter starvation -- the case where nothing written
  in Python would ever run.
* a Python thread that adds what C cannot see: the version, the AES backend,
  which downloads were running and how far in.  It is best-effort by
  definition, and says so in the file when it is late.

Everything is flushed and ``fsync``-ed as it is written, because the next
thing that happens to this process is being killed from a task manager.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

#: How long the main thread may go without a heartbeat before this is a stall
#: worth recording. A slow repaint is tenths of a second; Windows itself calls
#: a window unresponsive at five.
DEFAULT_THRESHOLD = 5.0

#: The heartbeat interval. Frequent enough that the threshold means what it
#: says, cheap enough to be invisible -- it is a timestamp assignment.
BEAT_INTERVAL = 1.0

#: Reports are appended to one file. Past this it is rotated once, so a machine
#: that stalls repeatedly cannot fill a disk.
MAX_BYTES = 2 << 20

FILENAME = "stalls.log"


class StallWatchdog:
    """Notices that the UI thread has stopped, and writes down what it found."""

    def __init__(
        self,
        data_dir: Path | str,
        threshold: float = DEFAULT_THRESHOLD,
        describe: Callable[[], str] | None = None,
        on_report: Callable[[str], None] | None = None,
    ) -> None:
        self.path = Path(data_dir) / FILENAME
        self.threshold = max(1.0, float(threshold))
        #: Supplies the application-level context -- version, backend, what was
        #: downloading. Called from the watchdog thread, never the main one.
        self.describe = describe
        #: Called when a stall ends, so the ordinary log can carry a pointer to
        #: the file. Best-effort: by then the application is answering again.
        self.on_report = on_report

        self._beat = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: Any = None
        self._reported_at = 0.0
        self._stall_began = 0.0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self) -> bool:
        """Begin watching. Returns whether the C-level recorder came up.

        The result is reported by the caller either way. A recorder that was
        never reached and one that believes it worked look identical
        otherwise, which is how the Windows taskbar bar stayed dead for two
        releases (CLAUDE.md rule 8).
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_large()
            # Line-buffered append: faulthandler writes to the descriptor
            # directly, so anything this module writes must already be out.
            self._handle = open(self.path, "a", buffering=1, encoding="utf-8",
                                errors="replace")
        except OSError:
            self._handle = None

        armed = self._arm()
        self._thread = threading.Thread(target=self._watch, name="ixd-watchdog",
                                        daemon=True)
        self._thread.start()
        return armed

    def stop(self) -> None:
        self._stop.set()
        try:
            faulthandler.cancel_dump_traceback_later()
        except Exception:  # noqa: BLE001 - shutting down regardless
            pass
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    # ------------------------------------------------------------------
    # the heartbeat
    # ------------------------------------------------------------------
    def beat(self) -> None:
        """Called from the Qt main thread. Must stay trivial."""
        self._beat = time.monotonic()
        self._arm()

    def _arm(self) -> bool:
        """(Re)start the C-level timer. Firing it means the main thread died."""
        if self._handle is None:
            return False
        try:
            faulthandler.dump_traceback_later(
                self.threshold, repeat=True, file=self._handle, exit=False)
            return True
        except (RuntimeError, ValueError, AttributeError):
            # No usable descriptor (a windowed build with redirected streams,
            # say). The Python thread below still runs; it is simply the only
            # recorder left, and the report says so.
            return False

    # ------------------------------------------------------------------
    # the Python-side recorder
    # ------------------------------------------------------------------
    def _watch(self) -> None:
        while not self._stop.wait(0.5):
            stalled_for = time.monotonic() - self._beat
            if stalled_for < self.threshold:
                if self._stall_began:
                    self._finish(stalled_for)
                continue
            if not self._stall_began:
                self._stall_began = time.monotonic() - stalled_for
            # One report when it starts, then at widening intervals, so a long
            # freeze shows whether it is stuck in one place or moving.
            since_report = time.monotonic() - self._reported_at
            if self._reported_at and since_report < min(60.0, self.threshold * 4):
                continue
            self._reported_at = time.monotonic()
            self._write_report(stalled_for)

    def _finish(self, stalled_for: float) -> None:
        held = self._reported_at - self._stall_began
        self._stall_began = 0.0
        self._reported_at = 0.0
        self._emit(f"--- the main thread answered again after {max(held, 0):.1f}s ---\n")
        if self.on_report is not None:
            try:
                self.on_report(
                    f"The window stopped responding for {max(held, 0):.0f}s. "
                    f"Stack traces were written to {self.path}.")
            except Exception:  # noqa: BLE001 - a log line is not worth a crash
                pass

    def _write_report(self, stalled_for: float) -> None:
        lines = [
            "\n" + "=" * 70,
            f"STALL  {time.strftime('%Y-%m-%d %H:%M:%S')}  "
            f"main thread silent for {stalled_for:.1f}s",
            "=" * 70,
        ]
        try:
            from . import __version__

            lines.append(f"version   {__version__}")
        except Exception:  # noqa: BLE001
            pass
        lines.append(f"platform  {sys.platform}  python {sys.version.split()[0]}")
        try:
            from .core.crypto import aes_backend_report

            lines.append(f"aes       {aes_backend_report()[1]}")
        except Exception:  # noqa: BLE001
            pass
        if self.describe is not None:
            try:
                lines.append(self.describe())
            except Exception as error:  # noqa: BLE001
                lines.append(f"(state unavailable: {error!r})")
        lines.append("")
        lines.append("--- python stacks, as seen from the watchdog thread ---")
        self._emit("\n".join(lines) + "\n")
        self._emit(self._stacks())
        self._emit(
            "--- end; C-level stacks for this stall follow if faulthandler "
            "fired ---\n")

    @staticmethod
    def _stacks() -> str:
        """Every thread's stack. The one that is not moving is the answer."""
        names = {t.ident: t.name for t in threading.enumerate()}
        out: list[str] = []
        for ident, frame in sys._current_frames().items():
            out.append(f"\nthread {names.get(ident, '?')} ({ident}):")
            out.extend(
                "  " + line.rstrip()
                for line in traceback.format_stack(frame)
            )
        return "\n".join(out) + "\n"

    # ------------------------------------------------------------------
    def _emit(self, text: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(text)
            self._handle.flush()
            os.fsync(self._handle.fileno())   # the next event may be a kill
        except (OSError, ValueError):
            pass

    def _rotate_if_large(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                previous = self.path.with_suffix(".log.1")
                previous.unlink(missing_ok=True)
                self.path.rename(previous)
        except OSError:
            pass
