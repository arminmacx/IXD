"""The extension, in a browser, on a page.

Run with:  python -m tests.test_browser

This exists because four consecutive sessions shipped a content script that
passed every offline test and did nothing whatsoever in a browser. A function
that called itself, a listener registered in the capture phase, an inherited
CSS property: each was invisible to unit tests and obvious to one click.

Nothing here is mocked. A real browser loads the real extension, opens a page
served over real HTTP with a real ``<video>`` and a real HLS playlist, and the
panel is found, clicked and read exactly as a person would.

The suite skips — it does not fail — when no Chrome-family browser is
installed, because that is a property of the machine and not of the code.

It is patient on purpose. With the desktop application not already running, the
first `extract` includes the messaging host cold-starting it, which is tens of
seconds — real behaviour, and not something the panel should be blamed for.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.browser import Browser, find_browser  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name} {detail}")


# ---------------------------------------------------------------------------
# A page that behaves like the ones this keeps going wrong on: a player that
# covers its own <video>, an overlay above everything that opens a popup on any
# click, and media fetched by script rather than written into the markup.
# ---------------------------------------------------------------------------
PAGE = """<!doctype html>
<html><head><title>A Real Film</title><style>
  body { margin: 0; background: #111; }
  #player { position: relative; width: 960px; height: 540px; }
  video { width: 100%; height: 100%; background: #000; }
  /* The player's own chrome, over the video — this is what stops a hover from
     ever reaching the element. */
  #chrome { position: absolute; inset: 0; z-index: 500; }
  /* And the overlay an advertising-funded site puts over the whole page. */
  #ads { position: fixed; inset: 0; z-index: 2147483647; }
</style></head>
<body>
  <div id="player">
    <video id="v" src="/clip.mp4" muted></video>
    <div id="chrome"></div>
  </div>
  <div id="ads"></div>
  <script>
    window.__adClicks = 0;
    // Capture on both `window` and `document`, which is what a site that opens
    // a popup on any click actually does. `window` is the one that matters:
    // capture order follows the tree, so a listener there runs before anything
    // on the document — and among two listeners on `window` itself, the
    // earlier registration wins. That is the whole reason the content script
    // runs at `document_start`: this inline script is the competition.
    for (const kind of ["pointerdown", "mousedown", "click"]) {
      window.addEventListener(kind, () => { window.__adClicks += 1; }, true);
      document.addEventListener(kind, () => { window.__adClicks += 1; }, true);
    }
    // Media fetched by script, the way a real player does it.
    fetch("/master.m3u8").then((r) => r.text()).then((t) => {
      window.__playlist = t.length;
    });
  </script>
</body></html>
"""

MASTER = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS=\"avc1.4d401e,mp4a.40.2\"\n"
    "low.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,CODECS=\"avc1.4d401f,mp4a.40.2\"\n"
    "high.m3u8\n"
)
MEDIA = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n"
    "#EXTINF:4.0,\nseg0.ts\n#EXTINF:4.0,\nseg1.ts\n#EXT-X-ENDLIST\n"
)


class Origin(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # noqa: D102 - quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's contract
        path = self.path.split("?", 1)[0]
        if path in ("/", "/watch"):
            body, kind = PAGE.encode(), "text/html; charset=utf-8"
        elif path == "/master.m3u8":
            body, kind = MASTER.encode(), "application/vnd.apple.mpegurl"
        elif path in ("/low.m3u8", "/high.m3u8"):
            body, kind = MEDIA.encode(), "application/vnd.apple.mpegurl"
        elif path.endswith(".ts"):
            body, kind = b"\x47\x40\x00\x10" + b"\x00" * 1020, "video/mp2t"
        elif path == "/clip.mp4":
            body, kind = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 512, "video/mp4"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# The desktop application has to be running.
#
# The extension talks to it through the messaging host, and with nothing
# listening the host cold-starts one — tens of seconds, during which the panel
# is legitimately still waiting. That failed this suite twice and both times the
# code was fine, which is the worst kind of test: it teaches the wrong lesson.
# So the suite brings the application up itself when it is not already there,
# and takes it down again.
# ---------------------------------------------------------------------------
def endpoint_file() -> Path:
    import sys as _sys
    _sys.path.insert(0, str(REPO))
    from ixd import config                            # noqa: PLC0415
    return Path(config.DATA_DIR) / "ipc.json"


def already_serving() -> bool:
    """Whether something is actually answering, not merely claiming to be.

    A killed instance leaves its endpoint file behind, and taking that as proof
    meant the suite talked to nothing and blamed the panel for the silence.
    """
    endpoint = endpoint_file()
    if not endpoint.exists():
        return False
    try:
        details = json.loads(endpoint.read_text())
        with socket.create_connection(
                (details["host"], int(details["port"])), 2) as sock:
            sock.sendall((json.dumps({
                "token": details["token"], "id": 1, "command": "ping",
                "params": {},
            }) + "\n").encode())
            return b'"ok": true' in sock.recv(4096)
    except Exception:  # noqa: BLE001 - anything at all means it is not serving
        endpoint.unlink(missing_ok=True)
        return False


def ensure_application() -> subprocess.Popen | None:
    """Start the application if it is not already serving. Returns what to stop."""
    if already_serving():
        return None
    packaged = REPO / "dist" / "ixd" / "ixd"
    command = ([str(packaged), "--background"] if packaged.exists()
               else [sys.executable, "-m", "ixd", "--background"])
    process = subprocess.Popen(
        command, cwd=str(REPO), stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.time() + 45
    while time.time() < deadline:
        if already_serving():
            return process
        if process.poll() is not None:
            return None
        time.sleep(0.5)
    return process


def serve() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ---------------------------------------------------------------------------
# Reaching into the panel. It lives in a shadow root, which is exactly why the
# page cannot deform it — and it means the test has to go the same way a person
# does, through the host element.
# ---------------------------------------------------------------------------
PANEL = """(() => {
  const host = document.getElementById("ixd-overlay-root");
  if (!host || !host.shadowRoot) return null;
  const panel = host.shadowRoot.querySelector(".panel");
  const menu = host.shadowRoot.querySelector(".menu");
  return {
    host: getComputedStyle(host).zIndex,
    parent: host.parentElement ? host.parentElement.tagName : "",
    visible: Boolean(panel && panel.classList.contains("visible")),
    label: panel ? panel.textContent.trim() : "",
    menuOpen: Boolean(menu && menu.classList.contains("visible")),
    menuPointer: menu ? getComputedStyle(menu).pointerEvents : "",
    panelPointer: panel ? getComputedStyle(panel).pointerEvents : "",
    items: menu ? [...menu.querySelectorAll(".item")].map((b) => b.textContent.trim()) : [],
    notes: menu ? [...menu.querySelectorAll(".note")].map((b) => b.textContent.trim()) : [],
    ads: window.__adClicks || 0,
  };
})()"""

CLICK = """(() => {
  const host = document.getElementById("ixd-overlay-root");
  const panel = host && host.shadowRoot && host.shadowRoot.querySelector(".panel");
  if (!panel) return "no panel";
  // Dispatched on the label, which is what a pointer actually lands on — the
  // panel's own children are the event targets, and that distinction is what
  // a capture-phase listener on the panel destroys.
  const target = panel.querySelector(".label") || panel;
  for (const kind of ["pointerdown", "mousedown", "mouseup", "click"]) {
    target.dispatchEvent(new MouseEvent(kind, {
      bubbles: true, composed: true, cancelable: true,
    }));
  }
  return "clicked";
})()"""

TOPMOST = """(() => {
  const host = document.getElementById("ixd-overlay-root");
  const panel = host && host.shadowRoot && host.shadowRoot.querySelector(".panel");
  if (!panel) return "no panel";
  const box = panel.getBoundingClientRect();
  const x = Math.round(box.left + box.width / 2);
  const y = Math.round(box.top + box.height / 2);
  const hit = document.elementFromPoint(x, y);
  return hit ? (hit.id || hit.tagName) : "nothing";
})()"""


def test_the_panel_appears_and_opens() -> None:
    """The whole point: a real page, a real click, a real menu."""
    print("\n[the panel, in a browser, on a page]")
    started = ensure_application()
    check("the desktop application is serving", already_serving(),
          "it could not be started, so the panel has nothing to talk to")
    server, origin = serve()
    try:
        with Browser(REPO / "extension") as browser:
            session = browser.open(f"{origin}/watch")
            # The panel finds the player rather than waiting to be hovered, so
            # its arrival is the assertion — no pointer has moved.
            state = browser.wait_for(
                session,
                f"({PANEL}) && ({PANEL}).visible ? ({PANEL}) : null",
                seconds=20,
            )
            check("the panel appears on the player with no hover at all",
                  bool(state and state["visible"]),
                  json.dumps(state) if state else "never appeared")
            if not state:
                return

            check("the overlay is a child of <html>",
                  state["parent"] == "HTML", state["parent"])
            check("the host carries the top stacking order",
                  state["host"] == "2147483647", state["host"])
            check("the panel is clickable", state["panelPointer"] == "auto",
                  state["panelPointer"])

            # The site's own overlay covers the viewport at the maximum
            # z-index. Whatever is on top at the panel's centre is what a click
            # will land on, and it has to be ours.
            topmost = browser.evaluate(session, TOPMOST)
            check("nothing of the page's is on top of the panel",
                  topmost in ("ixd-overlay-root", "DIV"), str(topmost))

            before = browser.evaluate(session, "window.__adClicks || 0")
            browser.evaluate(session, CLICK)
            opened = browser.wait_for(
                session,
                f"({PANEL}) && ({PANEL}).menuOpen ? ({PANEL}) : null",
                seconds=15,
            )
            check("clicking the panel opens the menu",
                  bool(opened and opened["menuOpen"]),
                  json.dumps(opened) if opened else "the menu never opened")
            if opened:
                check("and the menu is clickable, not merely visible",
                      opened["menuPointer"] == "auto", opened["menuPointer"])
                check("the click never reached the page",
                      opened["ads"] == before,
                      f"{before} -> {opened['ads']}")
                check("the menu says something",
                      bool(opened["items"] or opened["notes"]),
                      json.dumps(opened))

            # And the click path must run to the *end*. A stack overflow left
            # the pending note on screen for ever, which from outside is a menu
            # that opened and then said nothing — so the assertion is that the
            # menu stops saying "Reading…" and offers something.
            settled = browser.wait_for(
                session,
                f"(() => {{ const s = {PANEL}; return s && s.menuOpen"
                f" && (s.items.length || s.notes.some(n => !n.startsWith('Reading')))"
                f" ? s : null; }})()",
                # Longer than the panel's own extraction ceiling: with no
                # desktop application running — which is the state this suite
                # deliberately runs in — the honest end of the click path is the
                # ceiling expiring and the reason being shown.
                seconds=45,
            )
            check("the click path runs to the end and the menu fills in",
                  bool(settled),
                  json.dumps(opened) if opened else "never settled")
    finally:
        server.shutdown()
        if started is not None:
            started.terminate()
            try:
                started.wait(timeout=10)
            except subprocess.TimeoutExpired:
                started.kill()


def main() -> int:
    print("=" * 68)
    print("Internet Xtreme Downloader — browser test suite")
    print("=" * 68)
    if not find_browser():
        print("\n  SKIP  no Chrome-family browser is installed on this machine")
        return 0
    try:
        test_the_panel_appears_and_opens()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        FAILED.append(f"the browser run raised {exc}")

    print("\n" + "=" * 68)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
