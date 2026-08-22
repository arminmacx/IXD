/**
 * Floating download panel for video pages.
 *
 * Behaves the way IDM's does: hovering a video fades in a small panel over the
 * top of it; clicking the panel drops down the list of available qualities;
 * picking one queues that exact stream and the panel confirms. It can be
 * dragged anywhere in the window, and the × in its corner puts it away until
 * the page is reloaded.
 *
 * Two decisions matter for reliability:
 *
 *   1. **Nothing is injected into the page's own DOM tree.** The panel lives in
 *      a shadow root attached to a single fixed-position host element and is
 *      moved over whichever video is hovered. Sites like YouTube rebuild their
 *      player subtree constantly and would throw an injected button away; they
 *      also ship aggressive global CSS that would deform it. A shadow root is
 *      immune to both.
 *
 *   2. **Qualities are fetched before the click, not after.** The page is
 *      analysed speculatively as soon as a video appears, and the background
 *      worker caches the result. By the time the user opens the menu the list
 *      is usually already there, so choosing a quality is one click with no
 *      spinner in between.
 */

(() => {
  "use strict";

  if (window.__ixdDownloadPanelLoaded) return;
  window.__ixdDownloadPanelLoaded = true;

  // -------------------------------------------------------------------
  // Keeping our clicks away from the page — registered first, on purpose
  // -------------------------------------------------------------------
  //: A page listening in the **capture** phase on `document` sees a click
  //: before anything inside the panel does, because capture runs from the
  //: window downwards. Stopping propagation on the panel is therefore too
  //: late: an advertising overlay had already counted the click and opened
  //: its popup. Proven in a browser, not argued — `tests/test_browser.py`
  //: asserts the page's counter does not move.
  //:
  //: The only way to be earlier than the page is to be registered earlier
  //: than the page, which is why the content script runs at `document_start`
  //: and why this block is the first thing in the file. Among listeners on
  //: the same target and phase, registration order decides, and at
  //: `document_start` no page script has run yet.
  //:
  //: `stopImmediatePropagation` rather than `stopPropagation`: the page may
  //: have registered on `window` as well, and only the immediate form stops a
  //: sibling listener on the same target.
  const GUARDED_EVENTS = [
    "pointerdown", "pointerup", "mousedown", "mouseup", "click", "auxclick",
    "dblclick", "contextmenu", "touchstart", "touchend", "wheel",
  ];
  //: `pointermove` is deliberately **not** guarded. It fires continuously, and
  //: a listener that walks `composedPath()` on every one of them is the shape
  //: of the defect that froze YouTube's main thread. The drag registers its own
  //: `pointermove` listener while a drag is actually happening, and takes it
  //: off again when the pointer comes up.

  function insideOverlay(event) {
    const host = document.getElementById("ixd-overlay-root");
    if (!host) return false;
    const path = event.composedPath ? event.composedPath() : [];
    return path.includes(host);
  }

  //: Stopping the event also stops it reaching our own listeners — propagation
  //: is a property of the event, not of a world — so the overlay's clicks are
  //: **delivered from here**, by walking the composed path for the handler the
  //: element was given. One listener, registered before the page exists, is
  //: both the guard and the dispatcher.
  //: An element inside the overlay says which events it wants by carrying them
  //: on `__ixdEvents`; the panel's click keeps its own `__ixdHandler` because
  //: that is the one the tests reach for. The **first** node on the path that
  //: names the event gets it and the walk stops, so the close button's
  //: `pointerdown` is what keeps a click on the × from starting a drag of the
  //: panel behind it.
  function deliver(event) {
    const path = event.composedPath ? event.composedPath() : [];
    for (const node of path) {
      if (!node) continue;
      const bound = node.__ixdEvents && node.__ixdEvents[event.type];
      if (typeof bound === "function") {
        bound(event);
        return;
      }
      if (event.type === "click" && typeof node.__ixdHandler === "function") {
        node.__ixdHandler(event);
        return;
      }
    }
  }

  for (const kind of GUARDED_EVENTS) {
    window.addEventListener(kind, (event) => {
      if (!insideOverlay(event)) return;
      event.stopImmediatePropagation();
      deliver(event);
    }, true);
  }

  const MIN_WIDTH = 320;          // ignore avatars, grid previews and bumpers
  const MIN_HEIGHT = 180;
  const HIDE_DELAY_MS = 420;
  const REPOSITION_MS = 250;

  let shadow = null;
  let overlayHost = null;
  let panel = null;
  let menu = null;
  let label = null;
  let closeButton = null;

  let currentVideo = null;
  let hideTimer = null;
  let repositionTimer = null;
  let menuOpen = false;
  let lastUrl = location.href;
  let prefetched = "";

  // How much media the request log has found for this tab. A player may expose
  // no <video> element at all — it can build one late, keep it in a closed
  // shadow root, or paint into a canvas — and then there is nothing for a hover
  // panel to attach to however well the page is playing. What the player
  // *fetched* is known regardless, so that is what puts the panel on screen.
  let capturedCount = 0;
  //: The address the quality list in the menu was built from.
  //:
  //: It is not always the page. When the page yields nothing, the menu is
  //: built from the captured manifest — and queueing a choice against
  //: `location.href` then asks the engine to extract a page that has already
  //: been shown to hold no media. Reported as "I click Best available and it
  //: says no embedded media found", with the failure in the log to prove it.
  let menuSource = "";
  //: The *tab's* title. A player is nearly always in an iframe, whose document
  //: is titled after the embed or not at all — so naming a capture from
  //: `document.title` produced "every quality · playlist.m3u8" on every site
  //: where the panel was doing its job.
  let tabTitle = "";
  // One chip per page, not one per embedded player: the count is the tab's.
  const IS_TOP = window.top === window;

  function chipWanted() {
    return IS_TOP && capturedCount > 0 && !playerInTab && !dismissed;
  }

  //: Whether any frame of this tab has a player of its own.
  //:
  //: A page's player usually lives in an iframe, and that frame draws the panel
  //: over its own video — correctly placed, because it is the frame that knows
  //: where the video is. The top frame must not then add a second control for
  //: the same thing, so the chip is only for a tab where nothing was found.
  let playerInTab = false;
  let reportedPlayer = null;

  function reportPlayer(has) {
    if (reportedPlayer === has) return;
    reportedPlayer = has;
    try {
      chrome.runtime.sendMessage(
        { type: "ixdPlayer", has },
        (reply) => {
          void chrome.runtime.lastError;
          if (!reply || reply.ok !== true) return;
          const anywhere = Boolean(reply.result && reply.result.anyPlayer);
          if (anywhere === playerInTab) return;
          playerInTab = anywhere;
          if (!currentVideo) {
            if (chipWanted()) showPageChip();
            else if (panel) panel.classList.remove("visible", "page");
          }
        },
      );
    } catch (error) {
      /* messaging unavailable: the chip then behaves as it did before */
    }
  }

  //: True while the pointer is on the panel or its menu, so a rescan cannot
  //: pull it out from under the click that is happening.
  let pointerHeld = false;

  // -------------------------------------------------------------------
  // messaging
  // -------------------------------------------------------------------
  //: A reply must always arrive, one way or the other. An MV3 service worker
  //: can be torn down mid-request, in which case the callback is simply never
  //: invoked — without this ceiling the panel would sit on "Reading…" forever
  //: and the user would see nothing happen at all.
  const REQUEST_TIMEOUT_MS = 45000;
  //: Extraction has its own, shorter ceiling. Forty-five seconds of a pending
  //: note is not a slow answer to a person, it is a broken one, and the reasons
  //: it can hang — a service worker torn down mid-request, an origin that never
  //: closes the connection — are all better reported than waited out.
  const EXTRACT_TIMEOUT_MS = 25000;

  //: Every media address this page has fetched, read from the page's own
  //: resource timeline.
  //:
  //: The extension's service worker records the same requests, but it is
  //: **terminated after about thirty seconds of inactivity** and its memory
  //: goes with it — so by the time someone plays a video, watches a minute of
  //: it and decides to download, the worker frequently remembers nothing. Field
  //: logs show exactly that: a capture recorded, and forty-nine seconds later
  //: the engine told the browser sent nothing.
  //:
  //: `performance.getEntriesByType("resource")` has none of that lifetime. It
  //: belongs to the page, lives as long as the tab does, and is populated by
  //: the player's own fetches whether this extension was awake for them or not.
  //: The URL of a cross-origin resource is in it; only its timing detail is
  //: withheld, and the URL is the whole of what is wanted here.
  function pageMediaAddresses() {
    try {
      const entries = performance.getEntriesByType("resource") || [];
      const found = [];
      for (const entry of entries) {
        const url = entry.name || "";
        if (!/^https?:/i.test(url)) continue;
        if (!/googlevideo\.com\/videoplayback/i.test(url)
            && !/\.(m3u8|mpd|mp4|webm|m4a|m4s)(\?|$)/i.test(url)) continue;
        found.push({
          url,
          size: entry.encodedBodySize || entry.transferSize || 0,
        });
      }
      // The newest first, and capped: a long viewing session touches a great
      // many addresses and the engine only needs the ones worth a file.
      const media = found.reverse().slice(0, 60);
      // How many entries there were *in total* travels with them. An empty
      // media list can mean two opposite things — this page fetched no plain
      // media address, or this script is not looking at the page that did —
      // and only the total tells them apart. A watch page with hundreds of
      // resources and no media means the player fetched it somewhere this
      // timeline cannot see, such as inside a worker.
      media.total = entries.length;
      return media;
    } catch (error) {
      return [];
    }
  }

  function send(message, timeoutMs = REQUEST_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn(value);
      };
      const timer = setTimeout(
        () => finish(reject, new Error(
          "the download manager did not answer. Is Internet Xtreme Downloader running?"
        )),
        timeoutMs,
      );

      try {
        chrome.runtime.sendMessage(message, (response) => {
          const error = chrome.runtime.lastError;
          if (error) finish(reject, new Error(error.message));
          else if (!response || response.ok !== true) {
            finish(reject, new Error((response && response.error) || "request failed"));
          } else finish(resolve, response.result);
        });
      } catch (error) {
        finish(reject, error);
      }
    });
  }

  // -------------------------------------------------------------------
  // overlay construction
  // -------------------------------------------------------------------
  const STYLE = `
    :host { all: initial; }
    * { box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont,
        "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }

    .panel {
      position: fixed;
      z-index: 2147483647;
      display: flex;
      align-items: center;
      gap: 7px;
      padding: 7px 11px 7px 9px;
      border-radius: 9px;
      border: 1px solid rgba(255,255,255,.14);
      background: rgba(18,20,28,.86);
      backdrop-filter: blur(9px) saturate(140%);
      -webkit-backdrop-filter: blur(9px) saturate(140%);
      color: #eef1f7;
      font-size: 12.5px;
      font-weight: 600;
      letter-spacing: .1px;
      line-height: 1;
      cursor: pointer;
      box-shadow: 0 8px 26px rgba(0,0,0,.45);
      opacity: 0;
      transform: translateY(-5px);
      transition: opacity .16s ease, transform .16s ease;
      pointer-events: none;
      user-select: none;
      white-space: nowrap;
      /* The panel is dragged with the same pointer a touch screen scrolls
         with, and without this the browser claims the gesture first. */
      touch-action: none;
    }
    .panel.visible { opacity: 1; transform: translateY(0); pointer-events: auto; }
    /* Under the pointer the panel is being moved, not read: the fade that
       makes it appear would otherwise animate every step of the drag. */
    .panel.dragging { transition: none; cursor: grabbing; opacity: 1; }

    /* The way out. Small, in the corner, and hanging over the edge so it is
       never mistaken for part of the label. */
    .close {
      position: absolute;
      top: -8px;
      right: -8px;
      width: 18px;
      height: 18px;
      padding: 0;
      border-radius: 50%;
      border: 1px solid rgba(255,255,255,.20);
      background: rgba(30,34,46,.98);
      color: #c6cee2;
      display: grid;
      place-items: center;
      cursor: pointer;
      opacity: 0;
      transform: scale(.75);
      transition: opacity .14s ease, transform .14s ease,
                  background .14s ease, color .14s ease;
      pointer-events: none;
      font-family: inherit;
    }
    /* Visible whenever the panel is, rather than only on hover: a control
       nobody can see is one nobody knows they have. */
    .panel.visible .close { opacity: .8; transform: none; pointer-events: auto; }
    .panel.visible:hover .close { opacity: 1; }
    .panel.dragging .close { opacity: 0; pointer-events: none; }
    .close:hover { background: #e0464c; color: #fff; border-color: rgba(255,255,255,.34); }
    .close svg { width: 8px; height: 8px; display: block; }
    .panel:hover { background: rgba(24,27,37,.94); border-color: rgba(91,140,255,.55); }
    /* Present on the player from the moment there is one, and out of the way
       until it is wanted. Waiting for a hover is what left a playing page with
       nothing on it at all. */
    .panel.page { opacity: .55; }
    .panel.page.visible { opacity: .55; }
    .panel.page.visible:hover { opacity: 1; }
    .panel.resting.visible { opacity: .5; }
    .panel.resting.visible:hover { opacity: 1; }

    .icon { width: 15px; height: 15px; flex: none; color: #5B8CFF; }
    .caret { width: 9px; height: 9px; opacity: .6; flex: none; }
    .panel.busy .icon { animation: pulse 1s ease-in-out infinite; }
    @keyframes pulse { 0%,100% { opacity: 1 } 50% { opacity: .35 } }

    .menu {
      position: fixed;
      z-index: 2147483647;
      min-width: 244px;
      max-width: 380px;
      max-height: 60vh;
      overflow-y: auto;
      overscroll-behavior: contain;
      padding: 6px;
      border-radius: 11px;
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(16,18,26,.97);
      backdrop-filter: blur(14px);
      -webkit-backdrop-filter: blur(14px);
      box-shadow: 0 18px 48px rgba(0,0,0,.55);
      color: #eef1f7;
      display: none;
    }
    /* pointer-events is an inherited property and the host sets it to none,
       so a zero-sized box never swallows a click meant for the page. Every
       part of the overlay that IS meant to be clicked has to say so, and the
       menu saying nothing left it visible and inert. */
    .menu.visible { display: block; pointer-events: auto; }

    .menu-title {
      padding: 7px 9px 8px;
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .6px;
      color: #8d97ad;
      border-bottom: 1px solid rgba(255,255,255,.07);
      margin-bottom: 4px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .item {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      width: 100%;
      padding: 8px 9px;
      border: 0;
      border-radius: 7px;
      background: transparent;
      color: #eef1f7;
      font-size: 12.5px;
      text-align: left;
      cursor: pointer;
      font-family: inherit;
    }
    .item:hover { background: rgba(91,140,255,.18); }
    .item .size { font-size: 11px; color: #8d97ad; flex: none; font-variant-numeric: tabular-nums; }
    .item.best { font-weight: 700; }
    .item.best .size { color: #4ec9a0; }
    .item.restricted { opacity: .62; }
    .item.restricted .size { color: #e6b566; }
    .note { padding: 9px; font-size: 12px; color: #8d97ad; line-height: 1.45; }
    .note.error { color: #ff7a7a; }

    .toast {
      position: fixed;
      left: 50%;
      bottom: 34px;
      transform: translate(-50%, 12px);
      z-index: 2147483647;
      padding: 10px 16px;
      border-radius: 9px;
      border: 1px solid rgba(255,255,255,.12);
      background: rgba(18,20,28,.94);
      backdrop-filter: blur(10px);
      -webkit-backdrop-filter: blur(10px);
      color: #eef1f7;
      font-size: 12.5px;
      box-shadow: 0 12px 34px rgba(0,0,0,.5);
      opacity: 0;
      transition: opacity .2s ease, transform .2s ease;
      pointer-events: none;
      max-width: 70vw;
    }
    .toast.visible { opacity: 1; transform: translate(-50%, 0); }
    .toast.error { border-color: rgba(255,122,122,.5); color: #ffc9c9; }
  `;

  const DOWNLOAD_ICON =
    '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">' +
    '<path fill="currentColor" d="M12 3v10.6l3.3-3.3 1.4 1.4L12 17.4 7.3 11.7l1.4-1.4L12 13.6V3z"/>' +
    '<path fill="currentColor" d="M4 19h16v2H4z"/></svg>';

  const CARET_ICON =
    '<svg class="caret" viewBox="0 0 12 12" aria-hidden="true">' +
    '<path fill="currentColor" d="M2 4l4 4 4-4z"/></svg>';

  const CLOSE_ICON =
    '<svg viewBox="0 0 12 12" aria-hidden="true">' +
    '<path fill="none" stroke="currentColor" stroke-width="1.9" ' +
    'stroke-linecap="round" d="M3.3 3.3l5.4 5.4M8.7 3.3l-5.4 5.4"/></svg>';

  function ensureOverlay() {
    if (shadow) return;

    const host = document.createElement("div");
    host.id = "ixd-overlay-root";
    // The z-index has to be on the **host**, not only on the panel inside it.
    // A shadow root paints inside its host's stacking context, so a panel with
    // `z-index: 2147483647` sits wherever the host sits in the page's order —
    // and a site's own overlay, later in the DOM, paints straight over it. The
    // click then lands on that overlay, which on an advertising-funded site is
    // exactly what opens a popup: the user is clicking the page, not us.
    //
    // `pointer-events: none` on the host, `auto` on the panel: the host spans
    // nothing but must never become a click target of its own.
    host.style.cssText =
      "all:initial;position:fixed;top:0;left:0;width:0;height:0;" +
      "z-index:2147483647;pointer-events:none;";
    overlayHost = host;
    // `documentElement`, not `body`: a `transform`, `filter` or `will-change`
    // on the body makes it the containing block for `position: fixed`, which
    // puts the panel inside the page's stacking context however large its
    // z-index is. `<html>` is the one element sites do not do that to.
    (document.documentElement || document.body).appendChild(host);

    shadow = host.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    shadow.appendChild(style);

    panel = document.createElement("div");
    panel.className = "panel";
    panel.innerHTML = `${DOWNLOAD_ICON}<span class="label">Download</span>${CARET_ICON}`;
    label = panel.querySelector(".label");
    panel.addEventListener("mouseenter", () => {
      pointerHeld = true;
      cancelHide();
      panel.classList.remove("resting");
    });
    panel.addEventListener("mouseleave", () => {
      // A pointer that has outrun the panel it is dragging has not left it.
      if (drag) return;
      pointerHeld = false;
      if (currentVideo && !menuOpen) panel.classList.add("resting");
      else scheduleHide();
    });
    closeButton = document.createElement("button");
    closeButton.className = "close";
    closeButton.title = "Hide this panel until the page is reloaded";
    if (closeButton.setAttribute) {
      closeButton.setAttribute("aria-label", "Hide the download panel");
    }
    closeButton.innerHTML = CLOSE_ICON;
    // Named before the panel is reached on the way up, so pressing the × can
    // never be the start of a drag of the panel underneath it.
    closeButton.__ixdEvents = { click: onCloseClick, pointerdown: swallow };
    panel.appendChild(closeButton);

    shadow.appendChild(panel);

    panel.__ixdHandler = onPanelClick;
    panel.__ixdEvents = { pointerdown: onPanelPointerDown, pointerup: onDragUp };

    menu = document.createElement("div");
    menu.className = "menu";
    menu.addEventListener("mouseenter", () => {
      pointerHeld = true;
      cancelHide();
    });
    menu.addEventListener("mouseleave", () => {
      pointerHeld = false;
      if (!currentVideo) scheduleHide();
    });
    shadow.appendChild(menu);
  }

  function toast(text, isError) {
    ensureOverlay();
    const element = document.createElement("div");
    element.className = "toast" + (isError ? " error" : "");
    element.textContent = text;
    shadow.appendChild(element);
    requestAnimationFrame(() => element.classList.add("visible"));
    setTimeout(() => {
      element.classList.remove("visible");
      setTimeout(() => element.remove(), 300);
    }, isError ? 6000 : 3400);
  }

  // -------------------------------------------------------------------
  // placement
  // -------------------------------------------------------------------
  // Containers that are, by construction, a preview of *another* page rather
  // than this page's media. Hovering a video grid should not offer a download.
  const PREVIEW_CONTAINERS = [
    "a[href]",
    "ytd-thumbnail",
    "ytd-video-preview",
    "ytd-rich-item-renderer #dismissible",
    "ytd-compact-video-renderer",
    "ytd-reel-item-renderer",
    "#inline-preview-player",
    "[class*='thumbnail']",
    "[class*='Thumbnail']",
    "[data-testid*='preview']",
  ].join(",");

  // Pages that host a real, downloadable video. Anything else on these sites is
  // a feed, a channel or a search result, where every video on screen is a
  // preview of somewhere else.
  const PAGE_RULES = [
    { host: /(^|\.)youtube\.com$|(^|\.)youtube-nocookie\.com$/,
      path: /^\/(watch|shorts\/|live\/|embed\/|v\/)/ },
    { host: /(^|\.)youtu\.be$/, path: /^\/[\w-]{6,}/ },
  ];

  function pageHostsMedia() {
    const host = location.hostname.toLowerCase();
    for (const rule of PAGE_RULES) {
      if (rule.host.test(host)) return rule.path.test(location.pathname);
    }
    return true;      // unknown site: judge by the element alone
  }

  function isPreview(video) {
    // `closest` crosses shadow boundaries poorly, so walk a bounded number of
    // ancestors — deep enough for a player's chrome, shallow enough that the
    // page wrapper never matches.
    let node = video;
    for (let depth = 0; node && depth < 12; depth += 1) {
      if (node.matches && node.matches(PREVIEW_CONTAINERS)) return true;
      node = node.parentElement;
    }
    return false;
  }

  function isEligible(video) {
    if (!video || video.tagName !== "VIDEO") return false;
    if (!pageHostsMedia()) return false;

    const rect = video.getBoundingClientRect();
    if (rect.width < MIN_WIDTH || rect.height < MIN_HEIGHT) return false;
    // Off-screen or hidden players should not sprout a control.
    if (rect.bottom < 0 || rect.top > window.innerHeight) return false;
    const style = window.getComputedStyle(video);
    if (style.visibility === "hidden" || style.display === "none") return false;
    if (style.opacity === "0") return false;

    return !isPreview(video);
  }

  // -------------------------------------------------------------------
  // finding the player
  //
  // Waiting to be hovered was the mistake. A person opens a page, the video is
  // playing, and there is nothing on it — the panel only existed while the
  // pointer happened to be over the element, and on a player that covers its
  // <video> with its own chrome, or keeps it in a shadow root, that never
  // happened at all.
  //
  // IDM does not wait either: it scans for the largest video-shaped element on
  // the page and tracks it with resize, intersection and mutation observers.
  // Same rule here — the panel goes on the player as soon as there is one.
  // -------------------------------------------------------------------
  //: Aspect ratios a player has. A tall sliver is a decoration, and a very wide
  //: one is a banner; between those, anything is somebody's video.
  const MIN_ASPECT = 0.74;
  const MAX_ASPECT = 3.2;

  //: How deep to look for shadow-root players, and how many hosts to open. A
  //: page has a handful of custom elements; this is bounded so a full scan on a
  //: large document stays cheap enough to run on a mutation.
  const SHADOW_DEPTH = 4;
  const SHADOW_HOSTS = 60;

  function collectVideos(root, found, depth) {
    if (!root || depth > SHADOW_DEPTH || found.length > 40) return found;
    if (root.querySelectorAll) {
      for (const video of root.querySelectorAll("video")) found.push(video);
      let opened = 0;
      for (const host of root.querySelectorAll("*")) {
        if (!host.shadowRoot) continue;
        if ((opened += 1) > SHADOW_HOSTS) break;
        collectVideos(host.shadowRoot, found, depth + 1);
      }
    }
    return found;
  }

  function pickLargest(videos) {
    let best = null;
    let bestArea = 0;
    for (const video of videos) {
      if (!isEligible(video)) continue;
      const rect = video.getBoundingClientRect();
      const aspect = rect.width / rect.height;
      if (aspect < MIN_ASPECT || aspect > MAX_ASPECT) continue;
      const area = rect.width * rect.height;
      if (area <= bestArea) continue;
      bestArea = area;
      best = video;
    }
    return best;
  }

  //: How often the shadow-root search may run. It is the expensive one.
  const DEEP_SCAN_INTERVAL_MS = 2500;
  let nextDeepScan = 0;

  //: The biggest player-shaped video in this frame, or nothing.
  //:
  //: Two passes, and the order matters more than it looks. `querySelectorAll`
  //: over the light DOM is an indexed lookup and costs nothing; walking every
  //: element to find shadow hosts is O(document) and has to recurse. Running
  //: the second one on every mutation **froze YouTube's main thread** — the
  //: page mutates continuously, so the scan never stopped, and the panel's own
  //: reply could not be processed on a thread that was never free. The menu sat
  //: on "Reading the available qualities…" with the answer already delivered.
  //:
  //: So the deep search only runs when the light DOM holds nothing at all, and
  //: no more than once every few seconds.
  function findPlayer() {
    const light = pickLargest(document.querySelectorAll("video"));
    if (light) return light;
    const now = Date.now();
    if (now < nextDeepScan) return null;
    nextDeepScan = now + DEEP_SCAN_INTERVAL_MS;
    return pickLargest(
      collectVideos(document.body || document.documentElement, [], 0));
  }

  //: Being last in the document is the other half of being on top: two
  //: elements at the same z-index are ordered by document position, and a
  //: single-page app appends its overlays after we were injected.
  function keepOnTop() {
    if (!overlayHost) return;
    const parent = document.documentElement || document.body;
    if (parent && overlayHost.parentNode === parent
        && parent.lastElementChild !== overlayHost) {
      parent.appendChild(overlayHost);
    }
  }

  function position() {
    if (!panel || !panel.classList.contains("visible")) return;
    keepOnTop();

    const width = panel.offsetWidth || 128;
    const height = panel.offsetHeight || 30;
    let left;
    let top;

    // Whatever the panel is anchored to, a player that has gone takes the
    // panel with it — including one the user has dragged elsewhere.
    if (currentVideo && (!currentVideo.isConnected || !isEligible(currentVideo))) {
      hideNow();
      return;
    }

    if (pinned) {
      // Placed by hand, and only ever corrected for a viewport it no longer
      // fits in — resizing the window must not lose the panel off an edge.
      left = clamp(pinned.left, MARGIN, window.innerWidth - width - MARGIN);
      top = clamp(pinned.top, MARGIN, window.innerHeight - height - MARGIN);
    } else if (currentVideo) {
      const rect = currentVideo.getBoundingClientRect();
      left = Math.max(8, Math.min(window.innerWidth - width - 8, rect.right - width - 12));
      top = Math.max(8, rect.top + 12);
    } else if (chipWanted()) {
      // Clear of the fixed header nearly every site puts across the top.
      left = Math.max(8, window.innerWidth - width - 16);
      top = 96;
    } else {
      return;
    }

    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;

    if (menuOpen) {
      const menuWidth = menu.offsetWidth || 260;
      const menuHeight = menu.offsetHeight || 220;
      const menuLeft = Math.max(8, Math.min(window.innerWidth - menuWidth - 8,
        left + width - menuWidth));
      // Below the panel, unless the panel has been dragged low enough that
      // below is off the screen — then above it, which is where a menu near
      // the bottom of a window belongs anyway.
      let menuTop = top + height + 7;
      if (menuTop + menuHeight > window.innerHeight - 8) {
        menuTop = top - menuHeight - 7;
        if (menuTop < 8) menuTop = Math.max(8, window.innerHeight - menuHeight - 8);
      }
      menu.style.left = `${menuLeft}px`;
      menu.style.top = `${menuTop}px`;
    }
  }

  // -------------------------------------------------------------------
  // moving it, and putting it away
  //
  // Reported by the user: on every page holding a video the panel is simply
  // *there*, and there is no way to move it off what it is covering or to say
  // "not on this page". IDM's has both — a corner × and a panel you can pick
  // up — so this has both.
  //
  // Neither is remembered. A closed panel comes back on the next page or the
  // next reload, and a dragged one goes back to the player, because a control
  // that silently never returns is a support request nobody can answer. The
  // permanent switch already exists and is in the extension's options.
  // -------------------------------------------------------------------
  const MARGIN = 10;
  //: Pointer travel that separates a click on the panel from a drag of it.
  const DRAG_SLOP = 4;
  //: How long a swallowed click stays swallowed. A drag that ends outside the
  //: window may produce no click at all, and the flag must not then eat the
  //: user's next real one.
  const SWALLOW_MS = 400;

  //: Where the panel was dragged to, in viewport coordinates, or null while it
  //: still follows the player.
  let pinned = null;
  //: Set by the ×. Nothing may put the panel back on screen until the page
  //: navigates or is reloaded.
  let dismissed = false;
  let drag = null;
  let swallowClick = false;
  let swallowTimer = null;
  let saidWhyHidden = false;

  function clamp(value, low, high) {
    if (high < low) return low;
    return Math.max(low, Math.min(high, value));
  }

  function swallow(event) {
    if (event && event.preventDefault) event.preventDefault();
  }

  function onPanelPointerDown(event) {
    if (event.button !== undefined && event.button !== 0) return;
    if (event.preventDefault) event.preventDefault();
    const rect = panel.getBoundingClientRect();
    drag = {
      id: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      left: rect.left,
      top: rect.top,
      moved: false,
    };
    // Captured, so a pointer that outruns the panel keeps reporting to it.
    // Both routes are then registered anyway: with capture every move and up
    // is retargeted to the panel and arrives through the overlay guard, and
    // without it — an old browser, a refused capture — they arrive on window.
    try {
      if (panel.setPointerCapture) panel.setPointerCapture(event.pointerId);
    } catch (error) {
      /* uncaptured: the window listeners below are the route */
    }
    window.addEventListener("pointermove", onDragMove, true);
    window.addEventListener("pointerup", onDragUp, true);
    window.addEventListener("pointercancel", endDrag, true);
  }

  function onDragMove(event) {
    if (!drag) return;
    if (event.pointerId !== undefined && drag.id !== undefined
        && event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved) {
      if (Math.abs(dx) + Math.abs(dy) < DRAG_SLOP) return;
      drag.moved = true;
      panel.classList.add("dragging");
      panel.classList.remove("resting");
    }
    // The page must not see the drag: a site with its own pointer handling
    // would start selecting text or running a gesture of its own underneath.
    if (event.preventDefault) event.preventDefault();
    if (event.stopImmediatePropagation) event.stopImmediatePropagation();
    pinned = { left: drag.left + dx, top: drag.top + dy };
    position();
  }

  function onDragUp(event) {
    if (!drag) return;
    if (drag.moved) {
      // The click that follows the release is the drag's own, and it would
      // otherwise open the menu the moment the panel was put down.
      swallowClick = true;
      if (swallowTimer) clearTimeout(swallowTimer);
      swallowTimer = setTimeout(() => { swallowClick = false; }, SWALLOW_MS);
    }
    endDrag(event);
  }

  function endDrag(event) {
    if (!drag) return;
    try {
      if (panel.releasePointerCapture && drag.id !== undefined) {
        panel.releasePointerCapture(drag.id);
      }
    } catch (error) {
      /* the capture was already lost with the pointer */
    }
    drag = null;
    if (panel) panel.classList.remove("dragging");
    window.removeEventListener("pointermove", onDragMove, true);
    window.removeEventListener("pointerup", onDragUp, true);
    window.removeEventListener("pointercancel", endDrag, true);
    // `mouseleave` is suppressed for the length of a drag — a pointer that has
    // outrun the panel has not left it — so the flag it maintains has to be
    // put right here. Left stuck true it stops every rescan for good, and the
    // panel would never find the next video on the page.
    pointerHeld = false;
    if (panel && event && typeof event.clientX === "number") {
      const rect = panel.getBoundingClientRect();
      pointerHeld = event.clientX >= rect.left && event.clientX <= rect.right
        && event.clientY >= rect.top && event.clientY <= rect.bottom;
    }
    position();
  }

  function onCloseClick(event) {
    if (event && event.preventDefault) event.preventDefault();
    if (event && event.stopPropagation) event.stopPropagation();
    dismiss();
  }

  function dismiss() {
    dismissed = true;
    endDrag();
    cancelHide();
    closeMenu();
    currentVideo = null;
    pointerHeld = false;
    if (repositionTimer) {
      clearInterval(repositionTimer);
      repositionTimer = null;
    }
    if (panel) panel.classList.remove("visible", "page", "resting", "busy");
    if (!saidWhyHidden) {
      saidWhyHidden = true;
      toast("Panel hidden until you reload. To switch it off everywhere, "
            + "use the extension's options.");
    }
  }

  function show(video, resting) {
    if (dismissed) return;
    ensureOverlay();
    cancelHide();
    if (currentVideo !== video) {
      closeMenu();
      currentVideo = video;
      setLabel("Download");
    }
    panel.classList.remove("page");
    // Resting means "found, not pointed at": present on the player from the
    // moment there is one, and dim until it is wanted. Waiting for a hover is
    // what left a playing page with nothing on it.
    panel.classList.toggle("resting", Boolean(resting) && !menuOpen);
    panel.classList.add("visible");
    position();
    if (!repositionTimer) repositionTimer = setInterval(position, REPOSITION_MS);
    prefetch();
  }

  //: Put the panel on whatever this frame is playing, or take it away.
  //:
  //: Called on load, on mutation, on resize and on a slow tick, because there
  //: is no one event that means "the player is ready": a single-page app builds
  //: it after navigation, a lazy one builds it on play, and a custom element
  //: builds it inside a shadow root.
  function refreshPlayer() {
    // A drag is not a moment to go looking for a different player: a touch
    // drag fires no `mouseenter`, so `pointerHeld` alone does not cover it.
    if (menuOpen || pointerHeld || drag) return;
    // The player already on screen is nearly always still the right one, and
    // checking that is two property reads. Searching again on every mutation
    // is what made a busy page unusable.
    if (currentVideo && currentVideo.isConnected && isEligible(currentVideo)) {
      position();
      return;
    }
    const player = findPlayer();
    if (player) {
      show(player, true);
      reportPlayer(true);
      return;
    }
    reportPlayer(false);
    if (currentVideo) hideNow();
    else if (chipWanted()) showPageChip();
  }

  //: The panel with nothing to hang off: driven by what the page was seen
  //: fetching rather than by the markup, which is the only thing that works on
  //: a player exposing no <video> element.
  function showPageChip() {
    if (dismissed) return;
    if (!chipWanted()) {
      if (panel) panel.classList.remove("visible", "page");
      return;
    }
    ensureOverlay();
    panel.classList.add("page", "visible");
    restoreLabel();
    position();
    prefetch();
  }

  function scheduleHide() {
    cancelHide();
    hideTimer = setTimeout(() => {
      if (!menuOpen) hideNow();
    }, HIDE_DELAY_MS);
  }

  function cancelHide() {
    if (hideTimer) {
      clearTimeout(hideTimer);
      hideTimer = null;
    }
  }

  function hideNow() {
    cancelHide();
    closeMenu();
    currentVideo = null;
    if (panel) panel.classList.remove("resting");
    if (repositionTimer) {
      clearInterval(repositionTimer);
      repositionTimer = null;
    }
    // Leaving a video does not mean there is nothing to download: the page
    // itself may still be playing something the request log found.
    if (chipWanted()) {
      showPageChip();
      return;
    }
    if (panel) panel.classList.remove("visible", "page");
  }

  function setLabel(text, busy) {
    if (!label) return;
    label.textContent = text;
    panel.classList.toggle("busy", Boolean(busy));
  }

  function restoreLabel() {
    if (!currentVideo && chipWanted()) {
      setLabel(capturedCount === 1
        ? "1 video on this page"
        : `${capturedCount} videos on this page`);
      return;
    }
    setLabel("Download");
  }

  // -------------------------------------------------------------------
  // extraction and the quality menu
  // -------------------------------------------------------------------
  function prefetch() {
    // A feed, a search page or a site's front page holds no media by
    // construction, and analysing one can only fail. It did, repeatedly, and
    // filled the log with "no embedded media found on https://www.youtube.com/"
    // every time someone navigated back from a video.
    if (!pageHostsMedia()) return;
    if (prefetched === location.href) return;
    prefetched = location.href;
    send({ type: "extract", url: location.href }).catch(() => {
      /* speculative: a failure here is reported only if the user clicks */
    });
  }

  function closeMenu() {
    menuOpen = false;
    if (menu) {
      menu.classList.remove("visible");
      menu.innerHTML = "";
    }
  }

  async function onPanelClick(event) {
    event.preventDefault();
    event.stopPropagation();

    // Putting the panel down is not a request to open it.
    if (swallowClick) {
      swallowClick = false;
      if (swallowTimer) {
        clearTimeout(swallowTimer);
        swallowTimer = null;
      }
      return;
    }

    if (menuOpen) {
      closeMenu();
      return;
    }

    // Open the menu immediately with a "working" note. A click must always
    // produce something visible: if the reply is slow, or never comes, the
    // user still sees that the click registered and, eventually, why it
    // failed — rather than a button that appears to do nothing.
    setLabel("Reading…", true);
    openMenu(null, { pending: true });

    // Streams the player has already fetched are known-good: fully signed by
    // the site itself and never restricted to an opening slice.
    //
    // They are asked for **first and shown as soon as they arrive**, because
    // extraction can take ten seconds and a menu that says only "Reading the
    // available qualities…" for that long is indistinguishable from one that
    // is never going to say anything else. What is already known goes on
    // screen; the qualities fill in underneath when they come.
    const seen = (await send({ type: "captured" }, 8000).catch(() => null)) || {};
    const streams = seen.streams || [];
    if (seen.title) tabTitle = seen.title;
    if (streams.length) {
      openMenu(null, { captured: streams, waiting: true });
    }

    // What the player *fetched* beats what the page can be scraped for. A page
    // carries trailers, previews and advertising alongside its film, and
    // scraping cannot tell which is which — one report was a menu offering
    // "Video", which downloaded 62 KB of something else entirely. A captured
    // manifest is not a guess: it is the stream that is playing.
    const manifest = streams.find((entry) => entry.kind === "manifest");
    const order = manifest
      ? [manifest.url, location.href]
      : [location.href];

    let info = null;
    let failure = null;
    menuSource = "";
    for (const target of order) {
      try {
        const found = await send({ type: "extract", url: target,
                                  userInitiated: true },
                                 EXTRACT_TIMEOUT_MS);
        if (found && (found.formats || []).length) {
          info = found;
          menuSource = target;
          failure = null;
          break;
        }
      } catch (error) {
        failure = failure || error;
      }
    }
    // A click that opened the menu, then a click that closed it: the answer is
    // no longer wanted, and re-rendering would reopen it under the pointer.
    if (!menuOpen) return;

    // Most sites are not ones the application knows by name, and their player
    // leaves nothing in the page to read: the media is fetched by script and
    // the <video> element is handed a `blob:` URL. What the player fetched is
    // the only description of the stream that exists — and a *manifest* among
    // those describes the whole thing at every quality the site publishes, so
    // extracting it is what turns "one captured file" into a resolution menu.
    restoreLabel();
    if (info && (info.formats || []).length) {
      openMenu(info, {
        captured: streams,
        serverDriven: Boolean(seen.serverDriven),
      });
      return;
    }
    if (streams.length) {
      openMenu(null, {
        captured: streams,
        // The site refused to describe its own streams — which is what a
        // "prove you are not a robot" challenge amounts to — and the browser
        // has already passed that check. What it loaded is the answer.
        note: failure
          ? "This site would not list its streams for the download manager. "
            + "What your browser has already loaded is below, and that is the "
            + "route that works here."
          : "",
      });
      return;
    }
    // Nothing observed and nothing extractable: a plain <video src> page is
    // the remaining case, and its source is readable straight off the element.
    const direct = directSource();
    if (direct) {
      closeMenu();
      await queueDirect(direct);
      return;
    }
    openMenu(null, {
      error: failure
        ? String(failure.message || failure)
        : "Nothing playable was found on this page yet. Start the video and "
          + "try again — what the player fetches is what can be downloaded.",
    });
  }

  function directSource() {
    if (!currentVideo) return "";
    const candidate = currentVideo.currentSrc || currentVideo.src || "";
    if (/^https?:/i.test(candidate)) return candidate;
    const source = currentVideo.querySelector("source[src]");
    const nested = source ? source.getAttribute("src") : "";
    if (!nested) return "";
    try {
      const resolved = new URL(nested, location.href).toString();
      return /^https?:/i.test(resolved) ? resolved : "";
    } catch (error) {
      return "";
    }
  }

  async function queueDirect(url) {
    try {
      const result = await send({ type: "addUrl", url });
      toast(result.confirming
        ? "IXD is asking where to save it"
        : `Sent to IXD: ${result.filename || "download"}`);
    } catch (error) {
      toast(String(error.message || error), true);
    }
  }

  function formatSize(bytes) {
    if (!bytes || bytes <= 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let index = 0;
    let size = Number(bytes);
    while (size >= 1024 && index < units.length - 1) {
      size /= 1024;
      index += 1;
    }
    return `${size.toFixed(size >= 100 || index === 0 ? 0 : 1)} ${units[index]}`;
  }

  function openMenu(info, state) {
    ensureOverlay();
    menu.innerHTML = "";
    menuOpen = true;
    menu.classList.add("visible");

    if (state && state.pending) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "Reading the available qualities…";
      menu.appendChild(note);
      position();
      return;
    }

    const captured = (state && state.captured) || [];

    // Still reading: say so, and nothing else.
    //
    // The captures used to be listed *above* the "Reading…" note, on the
    // reasoning that something clickable beats an empty box. In practice they
    // are what the menu is about to replace, so every quality menu on every
    // site opened as a wall of near-identical rows — the same title six times
    // over, two of them labelled with the size of a playlist file — which then
    // vanished. The user, on seeing it once more: "its what i dont want to see
    // which still exist everywhere even on youtube."
    //
    // They are not lost: extraction that ends with nothing calls back here
    // without `waiting`, and then they are the menu.
    if (state && state.waiting) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "Reading the available qualities…";
      menu.appendChild(note);
      position();
      return;
    }

    if (state && state.error && !captured.length) {
      const heading = document.createElement("div");
      heading.className = "menu-title";
      heading.textContent = "Cannot download this video";
      const note = document.createElement("div");
      note.className = "note error";
      note.textContent = state.error;
      menu.append(heading, note);
      position();
      return;
    }

    // Captured streams are listed only when nothing else can be.
    //
    // They used to head the menu unconditionally — "already loaded by the
    // player" — and on YouTube that put a dead row above the live ones: the
    // captured `videoplayback` address is refused on a second fetch even to
    // the page that minted it, measured 2026-08-12. Removing them outright was
    // the wrong correction. On a site whose extraction fails — and the field
    // log has `no embedded media found on megaplay.buzz` beside a captured
    // `master.m3u8` — the captures are the *only* route, and the menu came up
    // empty: "2 videos on this page", nothing under it.
    //
    // So: when extraction has produced qualities to choose between, they are
    // what the menu shows. When it has not, the captures are shown, because a
    // row that might work beats no row at all.
    // Captures are listed when they are the only route, **and** when the
    // extraction was read out of one of them.
    //
    // That second clause is what names the file. A site whose media is a
    // playlist extracts to one format called after the playlist, so the menu
    // read "MASTER.M3U8 · Video · m3u8" — one row, named after a file nobody
    // asked for — where it used to offer the video's own name twice: as the
    // site serves it (`.ts`) and rewrapped (`.mp4`). Those two rows come from
    // `describeCaptured`, and suppressing them took the naming and the
    // container choice with them.
    //
    // YouTube is untouched by this: there the extraction comes from the watch
    // page, not from a capture, so its captured `videoplayback` address — 403
    // to everyone, §271 — still stays out of the menu.
    //
    // And that second clause is *narrow*: it is for a menu that cannot stand
    // on its own. Once a site has a real extractor the same captures become
    // pure noise — on a Twitch VOD the panel listed the two playlists the
    // player had fetched, twice each, above a perfectly good six-quality menu,
    // and labelled them "2.9 KB", which is the size of the playlist *text* and
    // not of anything anybody wants to download. So the captures are shown
    // only while the extracted menu has nothing better: a single row, or rows
    // that name no resolution at all.
    // Also needed further down, where a menu of one row that *is* the capture
    // listed above would say nothing new. `capturesWorthShowing` works this
    // out for itself; this is the same question asked in this scope, and
    // leaving it to that function's local left an undeclared identifier here.
    const fromCapture = captured.some((entry) => entry.url === menuSource);
    let listedCaptures = false;
    if (capturesWorthShowing(captured, (info || {}).formats || [], menuSource)) {
      listedCaptures = true;
      const heading = document.createElement("div");
      heading.className = "menu-title";
      heading.textContent = "Playing on this page";
      menu.appendChild(heading);
      const nativeExt = "ts";
      for (const entry of oneManifestPerHost(captured).slice(0, 8)) {
        // A playlist is offered twice: as the site serves it, and as an MP4.
        const containers = entry.kind === "manifest" ? ["mp4", ""] : [""];
        for (const container of containers) {
          const item = document.createElement("button");
          item.className = "item";
          item.type = "button";
          const name = document.createElement("span");
          name.textContent = describeCaptured(entry, container || nativeExt,
                                              container === "");
          const size = document.createElement("span");
          size.className = "size";
          size.textContent = formatSize(entry.size);
          item.append(name, size);
          item.__ixdHandler = () => queueCaptured(entry, container);
          menu.appendChild(item);
        }
      }
    }

    if (captured.length) {
      if (state && state.note) {
        const explain = document.createElement("div");
        explain.className = "note";
        explain.textContent = state.note;
        menu.appendChild(explain);
      }
      if (!info) {
        position();
        return;
      }
    }

    // When the qualities were read out of a capture **that is listed above**,
    // the second list is the first one again under a different name. It stays
    // only when it says something that row does not: more than one quality.
    //
    // `listedCaptures` is what makes that true. The condition used to assume a
    // row was drawn above, and once captures became conditional (§303) that
    // assumption was wrong exactly when it mattered: a manifest-only site
    // extracts to **one** format, so `choices.length < 2` held, and the menu
    // returned having drawn nothing at all — "2 videos on this page" over an
    // empty box, which is the second report of this panel coming up blank.
    const choices = (info.formats || []).filter((f) => f.kind !== "audio");
    if (listedCaptures && fromCapture && choices.length < 2) {
      position();
      return;
    }

    const title = document.createElement("div");
    title.className = "menu-title";
    const heading = menuHeading(info.title);
    title.textContent = heading;
    title.title = heading;
    menu.appendChild(title);

    const best = document.createElement("button");
    best.className = "item best";
    best.type = "button";
    best.innerHTML = '<span>Best available</span><span class="size">auto</span>';
    best.__ixdHandler = () => queue("");
    menu.appendChild(best);

    // Video qualities first, audio-only afterwards under its own heading:
    // someone opening this menu is choosing a quality, not a track.
    const all = (info.formats || []).slice(0, 40);
    const videos = all.filter((format) => format.kind !== "audio");
    const audios = all.filter((format) => format.kind === "audio");

    const addItem = (format) => {
      const item = document.createElement("button");
      item.className = "item";
      item.type = "button";
      const name = document.createElement("span");
      // What it is, beside what it looks like. "Video" on its own says nothing
      // when a playlist declares no resolution, and the container is what
      // decides whether the file will open at all on some players.
      const shape = (format.ext || "").toLowerCase();
      const described = format.description || format.format_id;
      name.textContent = shape && !described.toLowerCase().includes(shape)
        ? `${described} · ${shape}` : described;
      const size = document.createElement("span");
      size.className = "size";
      // A stream the site will only serve part of is worth saying plainly,
      // rather than letting the download fail a few megabytes in.
      size.textContent = format.complete === false
        ? "partial only" : formatSize(format.filesize);
      if (format.complete === false) {
        item.classList.add("restricted");
        item.title = "The site will only serve an opening portion of this one.";
      }
      item.append(name, size);
      // The container this row is *advertising* travels with the click. The
      // row says "160p · mp4"; without this it asked for no container at all,
      // the engine named the file after the segments it found — MPEG-TS on
      // Twitch and most HLS sites — and a menu entry marked mp4 produced a
      // `.ts`. The engine rewraps ts→mp4 when the name asks for it, and
      // corrects the name when the bytes turn out to be something it cannot
      // reconcile, so stating the choice is safe as well as honest.
      item.__ixdHandler = () => queue(format.format_id, shape);
      menu.appendChild(item);
    };

    videos.forEach(addItem);

    if (audios.length) {
      const heading = document.createElement("div");
      heading.className = "menu-title";
      heading.textContent = "Audio only";
      menu.appendChild(heading);
      audios.forEach(addItem);
    }
    const formats = all;

    if (!formats.length) {
      const note = document.createElement("div");
      note.className = "note";
      note.textContent = "Only the automatic selection is available for this page.";
      menu.appendChild(note);
    }

    // Every stream on offer being partial is worth saying out loud, because
    // the reason is not visible from the list itself.
    if (formats.length && formats.every((format) => format.complete === false)) {
      const note = document.createElement("div");
      note.className = "note error";
      note.textContent = (state && state.serverDriven)
        ? "This site is streaming to its own player rather than serving files, "
          + "and only hands out the opening minute to anything else. Signing in "
          + "to the site in this browser is what lifts that."
        : "The site is only offering the opening portion of each stream here.";
      menu.appendChild(note);
    }

    position();
  }

  //: One playlist per host.
  //:
  //: A player fetches the master and then the variant it chose, and both are
  //: captured — so offering each of them in two containers put *four* rows on
  //: screen for one video. The master is fetched first and describes the whole
  //: stream, so the earliest per host is the one kept.
  function oneManifestPerHost(entries) {
    const seen = new Set();
    return entries.filter((entry) => {
      if (entry.kind !== "manifest") return true;
      let host = "";
      try {
        host = new URL(entry.url).host;
      } catch (error) {
        return true;
      }
      if (seen.has(host)) return false;
      seen.add(host);
      return true;
    });
  }

  //: Should the "Playing on this page" section be drawn at all?
  //:
  //: Captures are the only route on a site with no extractor, so they are
  //: shown whenever extraction produced nothing. They are also shown when the
  //: extraction was read *out of* one of them and came back with a menu that
  //: cannot stand on its own — one nameless row called after a playlist file —
  //: because those two rows are what give the video its own name and the
  //: choice between the site's container and an MP4.
  //:
  //: They are *not* shown next to a real quality menu. On a Twitch VOD that
  //: put the two playlists the player had fetched, twice each, above a
  //: six-quality menu, each labelled "2.9 KB" — the size of the playlist text,
  //: not of any video. Same media, described worse, with a misleading size.
  //: What to call this menu.
  //:
  //: An extraction read out of a manifest address has no page to take a name
  //: from, so it falls back to the address's last path segment — and the menu
  //: was headed `1909970769.M3U8`, which names a file nobody asked for and
  //: tells a person nothing about what they are about to download. The page's
  //: own title is what they recognise it by, and the panel has it.
  function menuHeading(extracted) {
    const name = String(extracted || "").trim();
    const looksLikeAFile = /\.(m3u8|mpd|ts|mp4|m4a|webm)$/i.test(name);
    if (name && !looksLikeAFile) return name;
    const page = String(tabTitle || document.title || "").trim();
    return page || name || "Available streams";
  }

  function capturesWorthShowing(captured, formats, source) {
    if (!captured || !captured.length) return false;
    if (!formats || !formats.length) return true;
    //: **Not gated on the extraction having come from a capture.**
    //:
    //: It was, and that hid the captures exactly where they were needed. On
    //: Instagram the extraction runs against the *page* and scrapes one
    //: useless format out of it, while the browser has already fetched both
    //: halves of the clip from a CDN — so `source` was never one of the
    //: captured addresses, this returned false, and the menu offered a single
    //: row reading "Video · m4a": a video row carrying an audio container,
    //: with the real video nowhere in it. Reported as "i cannot download the
    //: video", and the Log showed both halves captured and correctly
    //: classified the whole time.
    //:
    //: `standsAlone` below is what stops the wall of rows §303 was about, and
    //: it does that job on its own: an extraction that names several formats
    //: or any real resolution suppresses the captures whatever their source.
    const namesRealQualities = formats.some(
      (format) => Number(format.height || 0) > 0
        // No trailing \b: Twitch names its own renditions "1080p60" and
        // "720p60", and a word boundary after the `p` never matches those.
        || /\b\d{3,4}p/i.test(String(format.description || "")));
    const standsAlone = formats.length > 1 || namesRealQualities;
    return !standsAlone;
  }

  function describeCaptured(entry, container, original) {
    // A capture from an ordinary site is named by its file, because that is
    // what a person recognises; only YouTube's are identified by an itag, and
    // there the itag is genuinely the clearest name a stream has.
    if (entry.kind === "manifest") {
      // The playlist's filename is `master.m3u8` on every site there is, so it
      // identifies nothing. The page's own title is what a person recognises,
      // and the extension is the choice being offered — so the row is named
      // like the file it will produce.
      const title = (tabTitle || document.title || "").trim()
        || entry.name.replace(/\.[^.]+$/, "");
      const suffix = container ? `.${container}` : "";
      return original ? `${title}${suffix} — as the site serves it`
        : `${title}${suffix}`;
    }
    if (entry.name) {
      const kind = entry.kind === "audio" ? "audio" : "video";
      return `${kind} · ${entry.name}`;
    }
    const kind = (entry.mime || "").startsWith("audio") ? "audio" : "video";
    const codec = (entry.mime || "").split(";")[0].split("/")[1] || "";
    return `${kind}${codec ? ` · ${codec}` : ""} · itag ${entry.itag}`;
  }

  async function queueCaptured(entry, container) {
    closeMenu();
    // Acknowledged at once, for the same reason `queue` is: a manifest goes
    // through media extraction, and that is the better part of ten seconds on
    // some sites. Holding the panel on "Sending…" for it made the one path
    // that already answered instantly look broken next to this one.
    restoreLabel();
    toast("Sent to IXD");
    const title = (document.title || "")
      .replace(/\s*[-|·—]\s*YouTube\s*$/, "").trim();
    try {
      // A playlist is a *description* of a stream, not the stream. Fetching it
      // as a file downloads the playlist text — a couple of hundred bytes that
      // land as a video and play nothing, which is exactly what was reported.
      // It has to go through media extraction, the same as any other stream.
      const result = entry.kind === "manifest"
        ? await send({ type: "addMedia", url: entry.url, title, container,
                       pageMedia: pageMediaAddresses(),
                       pageResourceTotal: (pageMediaAddresses().total || 0) })
        // A captured media URL is a bare request with no name in it, so the
        // page's own title is what the file should be called.
        : await send({ type: "addCaptured", itag: entry.itag, title });
      // Named once the engine knows the name, which is worth saying; the
      // acknowledgement above has already happened.
      if (result && result.filename) toast(`Sent to IXD: ${result.filename}`);
    } catch (error) {
      restoreLabel();
      toast(String(error.message || error), true);
    }
  }

  async function queue(formatId, container) {
    closeMenu();
    // Told at once, not when the engine has finished thinking.
    //
    // Choosing a quality used to hold the panel on "Sending…" for as long as
    // the engine took to analyse, probe and open the stream — the better part
    // of ten seconds on some sites. A person clicks a quality and moves on;
    // making them watch a button is the difference between a download manager
    // and a waiting room. The request is still awaited, but only so that a
    // *failure* can be reported.
    restoreLabel();
    toast("Sent to IXD");
    try {
      const result = await send({
        // The address the menu was built from — which is the captured manifest
        // when the page itself yielded nothing.
        type: "addMedia",
        url: menuSource || location.href,
        formatId,
        // What this page itself fetched. The service worker's own record is
        // frequently gone by now; the page's is not.
        pageMedia: pageMediaAddresses(),
        // Array properties do not survive the messaging boundary, so the
        // total travels as a field of its own.
        pageResourceTotal: (pageMediaAddresses().total || 0),
        title: (tabTitle || document.title || "")
          .replace(/\s*[-|·—]\s*YouTube\s*$/, "").trim(),
        // Empty for "Best available", which is a request to decide everything
        // including the packaging.
        container: container || "",
      });
      if (result && result.filename) toast(`Queued ${result.filename}`);
    } catch (error) {
      restoreLabel();
      toast(String(error.message || error), true);
    }
  }

  // -------------------------------------------------------------------
  // wiring
  // -------------------------------------------------------------------
  //: Find a <video> inside a subtree, crossing open shadow roots.
  //:
  //: `querySelector` stops at a shadow boundary, and a web-component player
  //: keeps its <video> inside one — so on those the hovered container appeared
  //: to hold no video and no panel ever came up. The search is bounded because
  //: it runs on a pointer event: only elements that actually have a shadow root
  //: are descended into, which on an ordinary page is none.
  function videoIn(root, depth) {
    if (!root || depth > 4) return null;
    if (root.tagName === "VIDEO") return isEligible(root) ? root : null;
    if (root.querySelectorAll) {
      for (const video of root.querySelectorAll("video")) {
        if (isEligible(video)) return video;
      }
    }
    const hosts = root.querySelectorAll ? root.querySelectorAll("*") : [];
    let examined = 0;
    for (const host of hosts) {
      if (!host.shadowRoot) continue;
      if ((examined += 1) > 24) break;
      const found = videoIn(host.shadowRoot, depth + 1);
      if (found) return found;
    }
    return null;
  }

  function videoUnder(target) {
    if (!target) return null;
    if (target.tagName === "VIDEO") return target;
    // The player chrome usually sits on top of the <video>, so search the
    // hovered container as well as its ancestors.
    let node = target;
    for (let depth = 0; node && depth < 6; depth += 1) {
      const video = videoIn(node, 0);
      if (video) return video;
      node = node.parentElement;
    }
    return null;
  }

  function onPointerOver(event) {
    const path = event.composedPath ? event.composedPath() : [event.target];
    for (const node of path) {
      if (node && node.tagName === "VIDEO" && isEligible(node)) {
        show(node, false);
        return;
      }
    }
    const video = videoUnder(event.target);
    if (video) {
      show(video, false);
      return;
    }
    // Pointing elsewhere no longer takes the panel away: it belongs to the
    // player, not to the pointer. It only dims back down.
    if (currentVideo && !menuOpen) {
      if (panel) panel.classList.add("resting");
    }
  }

  function onScrollOrResize() {
    if (menuOpen) closeMenu();
    position();
  }

  //: What the request log has found for this page, which is the only signal
  //: that works when the player exposes nothing to hover.
  function setCapturedCount(count) {
    const next = Number(count) || 0;
    if (next === capturedCount) return;
    capturedCount = next;
    if (currentVideo) return;          // a hovered video stays in charge
    if (chipWanted()) showPageChip();
    else hideNow();
  }

  async function refreshCaptured() {
    if (!IS_TOP) return;
    try {
      const seen = await send({ type: "captured" }, 8000);
      if (seen && seen.title) tabTitle = seen.title;
      setCapturedCount(((seen && seen.streams) || []).length);
    } catch (error) {
      /* the desktop app may be closed; the count is not worth reporting */
    }
  }

  //: Answer for the panel's own state.
  //:
  //: Registered before anything can decline to run, because "the script never
  //: injected", "the panel is switched off" and "it ran and found nothing" look
  //: identical from the toolbar and need different answers.
  function answerPing(enabled) {
    try {
      chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
        if (!message || message.type !== "ixdPing") return undefined;
        sendResponse({
          enabled,
          count: capturedCount,
          // "closed by the user" is reported separately from "nothing to
          // show", because from the toolbar the two look the same and the
          // answer to them is not.
          attached: dismissed ? "closed by the user"
            : currentVideo ? (pinned ? "over a video, moved by the user"
                                     : "over a video")
            : (panel && panel.classList.contains("visible")
              ? "page chip" : "nothing to show"),
        });
        return undefined;
      });
    } catch (error) {
      /* messaging unavailable: nothing can be reported either way */
    }
  }

  //: Fetch a media address **in the page's own context**, on the worker's
  //: behalf, and hand the bytes back a block at a time.
  //:
  //: The worker can call `fetch` itself, and did — but a request it makes
  //: carries the extension's origin, no referrer, and none of the page's
  //: request context. A media CDN decides by exactly those, so the delegated
  //: fetch was refused where the player's own request for the same address is
  //: served. This runs inside the page, which is the whole point: it is the
  //: same context the player fetched from, and it is what IDM's own page-world
  //: hook is positioned for.
  //:
  //: Each block is acknowledged before the next is read, so the reader is
  //: paced by how fast the application writes rather than by how fast the
  //: origin sends — a megabyte of messages queued at a stalled receiver is how
  //: a service worker runs out of memory.
  function answerFetch() {
    try {
      chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
        if (!message || message.type !== "ixdFetch") return undefined;
        const id = message.id;
        (async () => {
          try {
            const response = await fetch(message.url, {
              credentials: "include",
              headers: message.headers || {},
            });
            if (!response.ok) {
              throw new Error(`the page was refused too: HTTP ${response.status}`);
            }
            const reader = response.body.getReader();
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              if (!value || !value.length) continue;
              await send({ type: "ixdFetchChunk", id, data: base64Of(value) },
                         120000);
            }
            await send({ type: "ixdFetchDone", id, ok: true }, 120000);
          } catch (error) {
            await send({
              type: "ixdFetchDone", id, ok: false,
              error: String((error && error.message) || error),
            }, 30000).catch(() => {});
          }
        })();
        // Answered immediately: the transfer reports itself through
        // `ixdFetchChunk`, and holding this channel open for the length of a
        // download is what the message port is least able to do.
        sendResponse({ ok: true, started: true });
        return undefined;
      });
    } catch (error) {
      /* messaging unavailable: the worker's own fetch remains the route */
    }
  }

  //: Base64 of a byte array, in pieces — `String.fromCharCode` applied to a
  //: whole block throws `RangeError` once the block is large enough.
  function base64Of(bytes) {
    let binary = "";
    const step = 0x8000;
    for (let index = 0; index < bytes.length; index += step) {
      binary += String.fromCharCode.apply(
        null, bytes.subarray(index, index + step));
    }
    return btoa(binary);
  }

  //: Carry what the page hook teed through to the worker.
  //:
  //: `content/page_tee.js` runs in the page's own world and can see what the
  //: player's responses *contain*; it cannot talk to this extension. This can,
  //: and does nothing else — the bytes are not inspected here, because the
  //: application already has a reader for them and a content script is the
  //: wrong place for a protocol.
  function relayTeed() {
    try {
      window.addEventListener("message", (event) => {
        const message = event && event.data;
        if (!message || message.__ixdTee !== true) return;
        if (event.source !== window) return;      // only this page's own hook
        if (message.drm) {
          send({
            type: "ixdDrm", system: message.system || "",
            pageUrl: location.href,
          }, 20000).catch(() => {});
          return;
        }
        if (message.hello) {
          send({
            type: "ixdTeeAlive", detail: message.detail || "",
            pageUrl: location.href,
          }, 20000).catch(() => {});
          return;
        }
        send({
          type: "ixdTeed", url: message.url, total: message.total,
          data: message.data, pageUrl: location.href,
        }, 20000).catch(() => {});
      });
    } catch (error) {
      /* no page hook on this browser: every other route still works */
    }
  }

  async function boot() {
    answerFetch();
    relayTeed();
    let settings = { injectVideoButtons: true };
    try {
      settings = await send({ type: "getSettings" });
    } catch (error) {
      /* the desktop app may be closed; the panel still works on demand */
    }
    if (settings && settings.injectVideoButtons === false) {
      answerPing(false);
      return;
    }
    answerPing(true);

    ensureOverlay();

    document.addEventListener("pointerover", onPointerOver, true);
    document.addEventListener("pointerdown", (event) => {
      if (!menuOpen) return;
      const path = event.composedPath ? event.composedPath() : [];
      if (!path.includes(menu) && !path.includes(panel)) closeMenu();
    }, true);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    document.addEventListener("fullscreenchange", () => hideNow());

    // The player is looked for rather than waited for. There is no single
    // event that means "it is ready": a single-page app builds it after
    // navigation, a lazy one on play, a custom element inside a shadow root.
    let scanQueued = false;
    const scanSoon = () => {
      if (scanQueued) return;
      scanQueued = true;
      setTimeout(() => {
        scanQueued = false;
        refreshPlayer();
      }, 250);
    };

    try {
      new MutationObserver(scanSoon).observe(
        document.documentElement, { childList: true, subtree: true });
    } catch (error) {
      /* the periodic scan below is then the only route */
    }
    window.addEventListener("resize", scanSoon);
    document.addEventListener("play", scanSoon, true);
    document.addEventListener("loadedmetadata", scanSoon, true);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) scanSoon();
    });
    // A backstop, because a player built entirely inside a closed shadow root
    // produces no mutation this frame can see.
    setInterval(refreshPlayer, 2000);
    refreshPlayer();

    // Single-page apps swap the video without a navigation event.
    setInterval(() => {
      if (location.href !== lastUrl) {
        lastUrl = location.href;
        prefetched = "";
        capturedCount = 0;
        reportedPlayer = null;
        // A new page, in a single-page app as much as anywhere else: a panel
        // closed on the last one is not closed on this one.
        dismissed = false;
        saidWhyHidden = false;
        hideNow();
        refreshCaptured();
        scanSoon();
      }
    }, 900);

    window.addEventListener("yt-navigate-finish", () => {
      prefetched = "";
      capturedCount = 0;
      reportedPlayer = null;
      dismissed = false;
      saidWhyHidden = false;
      hideNow();
      refreshCaptured();
    });

    // The background worker announces what it captures as it captures it, so
    // the chip appears the moment the player fetches anything.
    try {
      chrome.runtime.onMessage.addListener((message) => {
        if (message && message.type === "ixdCaptured") setCapturedCount(message.count);
        if (message && message.type === "ixdPlayers") {
          // Another frame found or lost a player, which changes whether this
          // frame's page-level chip is the only way in.
          playerInTab = Boolean(message.anyPlayer);
          if (!currentVideo) {
            if (chipWanted()) showPageChip();
            else if (panel) panel.classList.remove("visible", "page");
          }
        }
      });
    } catch (error) {
      /* messaging unavailable: the poll below is then the only route */
    }
    // …and it is asked directly as well, because an announcement made before
    // this script was injected — or while its service worker was restarting —
    // is one nobody heard.
    refreshCaptured();
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshCaptured();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
