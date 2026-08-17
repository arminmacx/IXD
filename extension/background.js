/**
 * Internet Xtreme Downloader — background service worker.
 *
 * Responsibilities:
 *   1. Hold the Native Messaging channel to the desktop engine.
 *   2. Intercept browser downloads, cancel them, and hand the URL (plus
 *      cookies, referrer and user agent) to the engine instead.
 *   3. Serve the popup, the options page and the injected content scripts.
 *
 * MV3 service workers are terminated when idle, so the native port is created
 * lazily and transparently re-established on the next call.
 */

const HOST_NAME = "com.ixd.downloader";
const CALL_TIMEOUT_MS = 30000;

const DEFAULT_SETTINGS = {
  enabled: true,
  interceptDownloads: true,
  injectVideoButtons: true,
  minSizeBytes: 0,
  // Empty list = intercept every extension the browser would have downloaded.
  extensions: [],
  ignoredHosts: ["localhost", "127.0.0.1"],
  notifyOnAdd: true,
  preferredQuality: "1080p",
};

// ---------------------------------------------------------------------------
// native messaging transport
// ---------------------------------------------------------------------------
//
// A connected native port is a **process**. `chrome.runtime.connectNative`
// spawns the host and keeps it alive for as long as the port is open, and this
// worker is kept awake by a panel that reports what a page is playing every
// couple of seconds — so the port never closed, and the host never exited.
//
// Reported from Windows, where the manifest points straight at `ixd.exe`: the
// application was quit and Task Manager still listed `ixd.exe`, and only
// removing the extension from the browser ended it. That process was the host
// relay, not the application — but there is no way for anybody to tell those
// apart in a task list, and a download manager that is quit must leave nothing
// running whatever the name on it.
//
// So the port is released when there is nothing to relay: after a few seconds
// of quiet, and at once when the host reports the application is not running.
// Reconnecting is transparent — `getPort` rebuilds it on the next call.
let port = null;
let nextRequestId = 1;
const pending = new Map();

//: How long the port may sit idle before the host process is let go. Long
//: enough that a burst of calls shares one process; short enough that quitting
//: the application clears the task list within a breath.
const PORT_IDLE_MS = 5000;

//: How long "the application is not running" is believed for. While it holds,
//: the calls this extension makes on its own are answered here and no host is
//: spawned to be told the same thing again. Anything the *user* asked for
//: ignores it, because starting the application is the point of those.
const APP_DOWN_MS = 15000;

//: The calls the extension makes by itself: a panel refreshing, a page
//: announcing what it found, a log line, a status poll. None of them is worth
//: starting a process for, and together they are every call that arrives on a
//: timer. `extract` is here too, because the panel prefetches a page's
//: qualities on load — the same reason the host does not start on it.
const PASSIVE_COMMANDS = new Set([
  "ping", "stats", "list", "log", "can_handle", "extract", "browser_media_head",
]);

let portIdleTimer = null;
let appDownUntil = 0;

function touchPort() {
  if (portIdleTimer) clearTimeout(portIdleTimer);
  portIdleTimer = setTimeout(releasePort, PORT_IDLE_MS);
}

function releasePort() {
  if (portIdleTimer) {
    clearTimeout(portIdleTimer);
    portIdleTimer = null;
  }
  if (!port) return;
  // A call still in flight is a reply still owed; wait for it rather than
  // tearing the pipe out from under it.
  if (pending.size) {
    touchPort();
    return;
  }
  const closing = port;
  port = null;
  try {
    closing.disconnect();
  } catch (error) {
    /* already gone, which is the state we wanted */
  }
}

function getPort() {
  if (port) {
    touchPort();
    return port;
  }
  port = chrome.runtime.connectNative(HOST_NAME);

  port.onMessage.addListener((message) => {
    // The host answers this when it declines to start the application. Believe
    // it for a while: the alternative is spawning a process every time a page
    // ticks, which is the same defect wearing a shorter lifetime.
    if (message && message.not_running) {
      appDownUntil = Date.now() + APP_DOWN_MS;
    } else if (message && message.ok === true) {
      appDownUntil = 0;
    }
    const entry = message && message.id != null ? pending.get(message.id) : null;
    if (!entry) return;
    pending.delete(message.id);
    clearTimeout(entry.timer);
    entry.resolve(message);
    if (appDownUntil > Date.now()) releasePort();
    else touchPort();
  });

  port.onDisconnect.addListener(() => {
    const error = chrome.runtime.lastError;
    const reason = error ? error.message : "native host disconnected";
    port = null;
    if (portIdleTimer) {
      clearTimeout(portIdleTimer);
      portIdleTimer = null;
    }
    for (const [id, entry] of pending.entries()) {
      clearTimeout(entry.timer);
      entry.reject(new Error(reason));
      pending.delete(id);
    }
  });

  touchPort();
  return port;
}

//: Whether a command may be answered here, without a host process, because the
//: application is known to be down and nobody asked for anything.
function answerableWhileDown(command, params) {
  if (port) return false;                       // the process exists already
  if (Date.now() >= appDownUntil) return false;
  if (params && params.user_initiated) return false;
  return PASSIVE_COMMANDS.has(command);
}

function call(command, params = {}) {
  if (answerableWhileDown(command, params)) {
    return Promise.resolve({
      ok: false, error: "not running", not_running: true,
    });
  }
  return new Promise((resolve, reject) => {
    const id = nextRequestId++;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`"${command}" timed out`));
    }, CALL_TIMEOUT_MS);

    pending.set(id, { resolve, reject, timer });
    try {
      getPort().postMessage({ id, command, params });
    } catch (error) {
      clearTimeout(timer);
      pending.delete(id);
      port = null;
      reject(error);
    }
  });
}

async function callChecked(command, params) {
  const response = await call(command, params);
  if (!response || response.ok !== true) {
    throw new Error((response && response.error) || "the download manager reported an error");
  }
  return response.result;
}

// ---------------------------------------------------------------------------
// settings
// ---------------------------------------------------------------------------
async function getSettings() {
  const stored = await chrome.storage.local.get("settings");
  return { ...DEFAULT_SETTINGS, ...(stored.settings || {}) };
}

async function saveSettings(patch) {
  const merged = { ...(await getSettings()), ...patch };
  await chrome.storage.local.set({ settings: merged });
  return merged;
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function basename(path) {
  if (!path) return "";
  return path.split(/[\\/]/).pop() || "";
}

// Browser-internal pages carry schemes the download engine has no way to
// fetch. They are also exactly the pages the user is on while setting the
// extension up, so the failure has to explain itself rather than surfacing a
// raw "unsupported URL scheme" from deep inside the engine.
const RESTRICTED_SCHEMES = [
  "chrome:", "chrome-extension:", "chrome-search:", "chrome-untrusted:",
  "devtools:", "about:", "moz-extension:", "edge:", "extension:", "view-source:",
  "brave:", "opera:", "vivaldi:", "file:", "data:", "javascript:",
];

function restrictionFor(url) {
  if (!url) return "there is no address to download from.";
  let scheme = "";
  try {
    scheme = new URL(url).protocol.toLowerCase();
  } catch (error) {
    return `“${url}” is not a valid address.`;
  }
  if (scheme === "http:" || scheme === "https:") return "";
  if (RESTRICTED_SCHEMES.includes(scheme)) {
    return (
      "This is a browser page, not a web page — there is nothing here to " +
      "download. Open the site you want and try again."
    );
  }
  return `The engine cannot fetch “${scheme}” addresses.`;
}

function requireDownloadable(url) {
  const problem = restrictionFor(url);
  if (problem) throw new Error(problem);
  return url;
}

function extensionOf(nameOrUrl) {
  const name = basename((nameOrUrl || "").split("?")[0]);
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

async function cookieHeaderFor(url) {
  try {
    const cookies = await chrome.cookies.getAll({ url });
    return cookies.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch (error) {
    return "";
  }
}

function notify(title, message) {
  try {
    chrome.notifications.create({
      type: "basic",
      iconUrl: chrome.runtime.getURL("icons/icon128.png"),
      title,
      message,
    });
  } catch (error) {
    /* notifications are a nicety, never a hard dependency */
  }
}

//: No number on the toolbar icon.
//:
//: It carried the count of running downloads, polled from the application
//: every 1.5 seconds. Two things were wrong with that. The count was not
//: wanted — it was asked to be removed. And the poll went through the
//: messaging host, which started the application for *any* command: quitting
//: the application brought it back within seconds, so it could not be quit at
//: all. Ending the process by hand merely postponed it.
//:
//: Nothing here sets badge text now, and nothing polls. What a page holds is
//: shown by the panel on the page, which is where a person is looking.
async function showDetectedCount(_tabId, _force = false) {
  /* deliberately nothing: the icon carries no number */
}

//: Tell the page how much has been found on it.
//:
//: The hover panel needs a <video> element to attach to, and a player may
//: expose none — so on those pages the panel is driven by this instead of by
//: the markup. The badge alone was not enough: it is in the toolbar, and the
//: person is looking at the video.
const announcedByTab = new Map();

function announceCaptures(tabId) {
  if (tabId == null || tabId < 0) return;
  const count = (capturedFor(tabId) || []).length;
  if (announcedByTab.get(tabId) === count) return;
  announcedByTab.set(tabId, count);
  if (!count) announcedByTab.delete(tabId);
  try {
    // Frame 0 only: one chip on the page, not one per embedded player.
    chrome.tabs.sendMessage(
      tabId, { type: "ixdCaptured", count }, { frameId: 0 },
      () => void chrome.runtime.lastError,
    );
  } catch (error) {
    /* no content script on this page — a browser page, or one that blocks it */
  }
}

function shouldIntercept(item, settings) {
  if (!settings.enabled || !settings.interceptDownloads) return false;

  const url = item.finalUrl || item.url || "";
  if (!/^https?:\/\//i.test(url)) return false;      // blob:, data:, filesystem:

  let host = "";
  try {
    host = new URL(url).hostname;
  } catch (error) {
    return false;
  }
  if (settings.ignoredHosts.some((h) => host === h || host.endsWith(`.${h}`))) return false;

  if (settings.minSizeBytes > 0 && item.fileSize > 0 && item.fileSize < settings.minSizeBytes) {
    return false;
  }

  if (settings.extensions.length > 0) {
    const extension = extensionOf(item.filename || url);
    if (!settings.extensions.includes(extension)) return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// download interception
// ---------------------------------------------------------------------------
const handledDownloads = new Set();

chrome.downloads.onCreated.addListener(async (item) => {
  if (handledDownloads.has(item.id)) return;
  const settings = await getSettings();
  if (!shouldIntercept(item, settings)) return;

  handledDownloads.add(item.id);
  const url = item.finalUrl || item.url;

  // Stop the browser first so the two downloads never race for the same file.
  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (error) {
    /* the download may already have finished or been removed */
  }

  try {
    const cookies = await cookieHeaderFor(url);
    const result = await callChecked("add", {
      url,
      filename: basename(item.filename || ""),
      cookies,
      referrer: item.referrer || "",
      userAgent: navigator.userAgent,
      headers: {},
    });
    // `confirming` means the application put its own window on screen asking
    // where this is going. That window is the notification; a second one
    // saying "sent" would be announcing something that has not happened yet.
    if (settings.notifyOnAdd && !result.confirming) {
      notify("Sent to Internet Xtreme Downloader", result.filename || url);
    }
  } catch (error) {
    notify("Internet Xtreme Downloader unavailable", String(error.message || error));
    // Hand the download back to the browser so the user does not lose it.
    try {
      await chrome.downloads.download({ url, filename: basename(item.filename || "") || undefined });
    } catch (retryError) {
      /* nothing further we can do */
    }
  } finally {
    handledDownloads.delete(item.id);
  }
});

// ---------------------------------------------------------------------------
// media capture
//
// The player negotiates its own stream URLs, and those are unconditionally
// valid: fully signed, no initial-range restriction, exactly what the browser
// is already fetching. Watching for them costs nothing and gives the engine a
// URL that behaves, in cases where the extraction APIs hand back a restricted
// one. Observation only — no request is modified or blocked.
// ---------------------------------------------------------------------------
// Every request the browser makes, filtered by what it turns out to be rather
// than by where it goes. Watching one site's CDN meant that on every *other*
// site nothing was ever captured — and a modern player gives a page nothing
// else to find: the media is fetched by script and handed to the <video>
// element through a `blob:` URL, so the page holds no address for it at all.
// Scraping cannot see that. The request can.
const MEDIA_PATTERNS = ["<all_urls>"];
//: The site whose player protocol this extension understands in detail. The
//: request *body* is only read here — reading every body on every site to find
//: one site's token would be both wasteful and none of our business.
const YOUTUBE_MEDIA_PATTERNS = [
  "*://*.googlevideo.com/videoplayback*",
  "*://*.googlevideo.com/initplayback*",
];
//: Resource types worth inspecting. A player fetches its manifest as an
//: ordinary XHR and its media as `media`, and some sites use `object`.
//:
//: `main_frame`/`sub_frame` because navigating straight to a file is a
//: download, and those are few. `image` is *not* here even though IDM includes
//: it: IDM has a persistent background page, an MV3 service worker does not,
//: and routing every image on a page through it starves the messages the panel
//: is waiting on — measured as a hover panel stuck on "Reading the available
//: qualities" while the worker worked through a page's thumbnails.
const MEDIA_TYPES = [
  "media", "xmlhttprequest", "object", "other", "main_frame", "sub_frame",
];

// ---------------------------------------------------------------------------
// The headers the browser actually sent
//
// A media CDN decides by header. Reconstructing what it wants is guesswork —
// `Referer` alone is a good guess and not always the right one, because a
// player may sign its segment requests with an `Authorization` or a bespoke
// `X-…` header that no downloader could invent. The browser already sent the
// exact set that worked, so that is what is kept and replayed.
//
// `extraHeaders` is what makes Chrome disclose `Referer` and `Origin` at all;
// without it they are simply absent from `requestHeaders`. Firefox rejects the
// flag, so it is attempted and dropped.
// ---------------------------------------------------------------------------
const headersByRequest = new Map();
const MAX_TRACKED_REQUESTS = 300;

//: Headers that describe the connection rather than the request. Replaying
//: these would be wrong at best: the engine opens its own connection, does its
//: own content coding, and asks for its own byte ranges.
const NEVER_REPLAYED = new Set([
  "host", "connection", "content-length", "accept-encoding", "keep-alive",
  "proxy-authorization", "proxy-connection", "te", "trailer",
  "transfer-encoding", "upgrade", "range", "if-range", "if-none-match",
  "if-modified-since", "if-match", "if-unmodified-since",
  // Cookies already travel by their own route, scoped to the site's domain.
  // Sending them twice is how one of them ends up being the stale one.
  "cookie",
]);

//: Addresses that cannot be media, by their own extension.
//:
//: Keeping the headers of every request on a busy page is what made the
//: extension slow enough to look broken. Nothing here can ever be offered as a
//: download, so nothing here needs its headers remembered.
const NEVER_MEDIA_PATH =
  /\.(js|mjs|cjs|css|png|jpe?g|gif|webp|avif|svg|ico|bmp|woff2?|ttf|otf|eot|json|html?|xhtml|txt|map)$/;

function mightBeMedia(url) {
  try {
    const path = new URL(url).pathname.toLowerCase();
    return !NEVER_MEDIA_PATH.test(path);
  } catch (error) {
    return false;
  }
}

function rememberHeaders(details) {
  if (!details.requestHeaders) return;
  if (!mightBeMedia(details.url)) return;
  const kept = {};
  for (const header of details.requestHeaders) {
    if (!header || header.value == null) continue;
    if (NEVER_REPLAYED.has((header.name || "").toLowerCase())) continue;
    kept[header.name] = header.value;
  }
  headersByRequest.set(details.requestId, kept);
  if (headersByRequest.size > MAX_TRACKED_REQUESTS) {
    headersByRequest.delete(headersByRequest.keys().next().value);
  }
}

//: A manifest describes an entire stream, so it is the thing worth having —
//: one of these is a whole download, at every quality the site publishes.
const MANIFEST_EXTENSIONS = [".m3u8", ".mpd", ".ism", ".f4m"];
//: A complete file that can be fetched as it stands.
const FILE_EXTENSIONS = [
  ".mp4", ".webm", ".m4v", ".mov", ".mkv", ".avi", ".flv", ".ogv", ".3gp",
  ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".wav", ".weba",
];
//: One piece of a stream. Never offered: a viewer wants the film, not four
//: thousand four-second files, and the manifest that lists them is captured
//: separately. They are recognised only so they can be ignored knowingly.
const SEGMENT_EXTENSIONS = [".ts", ".m4s", ".cmfv", ".cmfa", ".aac.seg"];

//: Content types that say what an extensionless URL is. Plenty of players
//: serve `/manifest?id=…` with no extension at all, and the header is then the
//: only honest description of it.
const MANIFEST_TYPES = [
  "application/vnd.apple.mpegurl", "application/x-mpegurl",
  "audio/mpegurl", "audio/x-mpegurl", "application/dash+xml",
  "application/vnd.ms-sstr+xml", "video/vnd.mpeg.dash.mpd",
  // Real origins serve playlists under all of these. The first is a plain
  // invention of some CDN's and is common enough to be worth naming.
  "application/octet-stream-m3u8", "application/f4m+xml",
  "application/vnd.ms-sstr+xml", "application/x-mpegurl; charset=utf-8",
];
//: Content types that identify a *piece* of a stream rather than a stream. A
//: transport-stream segment fetched from an extensionless address (`/seg/1234`)
//: has nothing in its path to recognise, so without this it is offered as a
//: video and a thousand of them bury the manifest that lists them.
const SEGMENT_TYPES = ["video/mp2t", "video/iso.segment", "audio/iso.segment"];
//: Path shapes that mean "one piece of a stream" when the address carries no
//: extension at all. Deliberately narrow — these are whole path *segments*, so
//: an ordinary film called `chunky.mp4` is untouched, and anything with a real
//: media extension has already been decided above.
const SEGMENT_PATH = /(^|[/_-])(seg|segment|segments|frag|fragment|chunk|init)([/_-]|\d|$)/;
const CAPTURE_TTL_MS = 30 * 60 * 1000;
const capturedByTab = new Map();
//: Which frames of a tab are showing a panel on a player of their own.
const playersByTab = new Map();
//: Tabs whose player is using the server-driven protocol.
const serverDrivenTabs = new Set();
//: Pages already reported as DRM-protected, so the Log says it once rather
//: than once per track.
const drmReported = new Set();

//: What a URL turns out to be, or null when it is not media at all.
//:
//: Judged by the path, so a query string full of tokens cannot make an ".mp4"
//: look like something else — and segments are recognised in order to be
//: dropped, rather than left to flood the list.
//: What a media CDN says about a file inside its own query string.
//:
//: Instagram and Facebook publish a clip as **two `.mp4` files** — picture
//: without sound and sound without picture — served from the same CDN with
//: the same extension, so nothing in the path or the content type tells them
//: apart. Both were therefore captured as "video", the panel offered two rows
//: that looked identical, and the pairing that exists to avoid silent films
//: could never find an audio track to pair with. Reported as "the panel shows
//: only audio and no video at all".
//:
//: They do say which is which: `efg` is base64 JSON carrying a `vencode_tag`
//: — `…dash_baseline_1_v1` against `…dash_ln_heaac_vbr3_audio` — and an
//: `xpv_asset_id` that is the same for both halves of one clip. That is the
//: pairing key, taken from the addresses themselves rather than guessed.
function cdnAssetHint(parsed) {
  const efg = parsed.searchParams.get("efg");
  if (!efg) return null;
  try {
    const described = JSON.parse(atob(efg));
    const tag = String(described.vencode_tag || "");
    const asset = String(described.xpv_asset_id || described.video_id || "");
    if (!tag && !asset) return null;
    return { asset, audio: /audio/i.test(tag) };
  } catch (error) {
    return null;      // not the convention we know; judged the usual way
  }
}

//: Query parameters that name a *slice* rather than a file.
//:
//: A player asks for a long clip in pieces — `bytestart`/`byteend` on the same
//: signed address, dozens of times — and each piece was recorded as a download
//: of its own. Two files became thirty rows, and queueing any of them fetched
//: that slice alone: a few hundred kilobytes of a five-megabyte clip, which is
//: a file that plays nothing. The signature (`oh`) is identical across every
//: slice of one address, so it does not cover these and dropping them yields
//: the whole file.
const SLICE_PARAMS = ["bytestart", "byteend"];

function withoutSlice(raw) {
  try {
    const parsed = new URL(raw);
    if (!SLICE_PARAMS.some((name) => parsed.searchParams.has(name))) return raw;
    for (const name of SLICE_PARAMS) parsed.searchParams.delete(name);
    return parsed.toString();
  } catch (error) {
    return raw;
  }
}

function classifyMediaUrl(raw, contentType = "") {
  let parsed;
  try {
    parsed = new URL(raw);
  } catch (error) {
    return null;
  }
  if (!/^https?:$/.test(parsed.protocol)) return null;

  const path = parsed.pathname.toLowerCase();
  const type = (contentType || "").split(";")[0].trim().toLowerCase();

  if (SEGMENT_EXTENSIONS.some((ext) => path.endsWith(ext))) return null;
  if (SEGMENT_TYPES.includes(type)) return null;
  if (MANIFEST_EXTENSIONS.some((ext) => path.endsWith(ext)) ||
      MANIFEST_TYPES.includes(type)) {
    return { kind: "manifest", name: fileNameOf(parsed) };
  }
  if (FILE_EXTENSIONS.some((ext) => path.endsWith(ext))) {
    const hint = cdnAssetHint(parsed);
    const byName = path.match(/\.(mp3|m4a|aac|ogg|oga|opus|flac|wav|weba)$/);
    return {
      kind: (hint && hint.audio) || byName ? "audio" : "video",
      name: fileNameOf(parsed),
      asset: hint ? hint.asset : "",
    };
  }
  if (type.startsWith("video/") || type.startsWith("audio/")) {
    // An extensionless address that the header says is media: either the whole
    // file, or one piece of a stream whose manifest is worth far more. Only the
    // path can tell those apart here.
    if (SEGMENT_PATH.test(path)) return null;
    const hint = cdnAssetHint(parsed);
    return {
      kind: (hint && hint.audio) || type.startsWith("audio/") ? "audio" : "video",
      name: fileNameOf(parsed),
      asset: hint ? hint.asset : "",
    };
  }
  return null;
}

function fileNameOf(parsed) {
  const last = parsed.pathname.split("/").filter(Boolean).pop() || "";
  return decodeURIComponent(last) || parsed.hostname;
}

function parseMediaUrl(raw) {
  try {
    const parsed = new URL(raw);
    const query = parsed.searchParams;
    const itag = query.get("itag") || "";
    if (!itag) return null;
    return {
      url: raw,
      itag,
      size: Number(query.get("clen") || 0) || 0,
      mime: query.get("mime") || "",
      duration: Number(query.get("dur") || 0) || 0,
      // `ratebypass=yes` marks a URL the CDN serves in full *to an unattested
      // session*. It is recorded, but it deliberately does NOT decide whether
      // to keep the URL, because these URLs are not ours: the player fetched
      // them inside a session the site has already attested, and that session
      // is served past the point where our own requests are cut off. Filtering
      // them by a rule written for unattested traffic throws away the only
      // URLs worth capturing — which is the whole reason for capturing.
      ratebypass: query.get("ratebypass") === "yes",
      at: Date.now(),
    };
  } catch (error) {
    return null;
  }
}

// ---------------------------------------------------------------------------
// proof-of-origin tokens
//
// Some sites gate their API behind a proof of origin, generated by their own
// code in this browser for this session and passed in every request the player
// makes. Reading it out of the player's request body is the only way an
// external downloader can present the same credential — and it is the user's
// own session either way. Nothing is modified: the body is observed.
//
// It is not a way around a playback-rate ceiling. Where a site streams to its
// own player instead of serving a file, the token changes nothing; that was
// measured, including by replaying a browser's own session.
// ---------------------------------------------------------------------------
const tokensByTab = new Map();

// A proof of origin is minted *for one visitor identity*. Forwarding the token
// on its own leaves the engine presenting its own identity while holding a
// proof issued to the browser's — which the server does not accept, and which
// looks exactly like the token having no effect at all. The identity travels
// in the `X-Goog-Visitor-Id` header of the site's own API calls, so it is read
// from there and sent as a pair with the token.
const visitorByTab = new Map();
//: The streaming endpoint the page's own player is talking to.
const endpointByTab = new Map();
//: The last request the player sent, kept whole so its session can be reused.
const requestByTab = new Map();

function playerRequestFor(tabId) {
  const entry = requestByTab.get(tabId);
  if (!entry) return null;
  if (Date.now() - entry.at > CAPTURE_TTL_MS) {
    requestByTab.delete(tabId);
    return null;
  }
  return entry;
}
const API_PATTERNS = ["*://*.youtube.com/youtubei/v1/*"];

function endpointFor(tabId) {
  const entry = endpointByTab.get(tabId);
  if (!entry) return "";
  if (Date.now() - entry.at > CAPTURE_TTL_MS) {
    endpointByTab.delete(tabId);
    return "";
  }
  return entry.url;
}

function rememberVisitor(details) {
  if (details.tabId == null || details.tabId < 0) return;
  const headers = details.requestHeaders || [];
  for (const header of headers) {
    if (header.name.toLowerCase() !== "x-goog-visitor-id") continue;
    if (!header.value) return;
    visitorByTab.set(details.tabId, { visitor: header.value, at: Date.now() });
    return;
  }
}

function visitorFor(tabId) {
  const entry = visitorByTab.get(tabId);
  if (!entry) return "";
  if (Date.now() - entry.at > CAPTURE_TTL_MS) {
    visitorByTab.delete(tabId);
    return "";
  }
  return entry.visitor;
}

//: Field numbers inside the player's request message.
const FIELD_STREAMER_CONTEXT = 19;
const FIELD_PO_TOKEN = 2;

function readVarint(bytes, position) {
  let result = 0;
  let shift = 0;
  while (position < bytes.length) {
    const byte = bytes[position];
    position += 1;
    result += (byte & 0x7f) * Math.pow(2, shift);
    if (!(byte & 0x80)) return [result, position];
    shift += 7;
    if (shift > 56) break;
  }
  return [result, position];
}

/** Yield {field, wire, value} for one protobuf message. */
function* protobufFields(bytes) {
  let position = 0;
  while (position < bytes.length) {
    let tag;
    [tag, position] = readVarint(bytes, position);
    const field = tag >>> 3;
    const wire = tag & 0x07;
    if (wire === 0) {
      let value;
      [value, position] = readVarint(bytes, position);
      yield { field, wire, value };
    } else if (wire === 2) {
      let length;
      [length, position] = readVarint(bytes, position);
      if (position + length > bytes.length) return;
      yield { field, wire, value: bytes.subarray(position, position + length) };
      position += length;
    } else if (wire === 5) {
      position += 4;
    } else if (wire === 1) {
      position += 8;
    } else {
      return;                       // groups are not used here
    }
  }
}

function base64Url(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function extractPoToken(body) {
  try {
    for (const outer of protobufFields(body)) {
      if (outer.field !== FIELD_STREAMER_CONTEXT || outer.wire !== 2) continue;
      for (const inner of protobufFields(outer.value)) {
        if (inner.field === FIELD_PO_TOKEN && inner.wire === 2 && inner.value.length) {
          return base64Url(inner.value);
        }
      }
    }
  } catch (error) {
    /* an unexpected body shape is simply not a token */
  }
  return "";
}

function rememberToken(details) {
  if (details.tabId == null || details.tabId < 0) return;
  const raw = details.requestBody && details.requestBody.raw;
  if (!raw || !raw.length || !raw[0].bytes) return;

  const body = new Uint8Array(raw[0].bytes);

  // The whole request is kept, not just the token. Rebuilding a request from
  // our own player response produces a *different* session — different client
  // identity, different ustreamer configuration — and a proof of origin means
  // nothing outside the session it was minted for. Replaying the browser's own
  // request as the template is what makes the session the attested one.
  requestByTab.set(details.tabId, {
    body: base64Url(body), url: details.url, at: Date.now(),
  });

  const token = extractPoToken(body);
  if (!token) return;

  let host = "";
  try {
    host = new URL(details.url).hostname;
  } catch (error) {
    return;
  }
  tokensByTab.set(details.tabId, { token, host, at: Date.now() });
}

function tokenFor(tabId) {
  const entry = tokensByTab.get(tabId);
  if (!entry) return "";
  if (Date.now() - entry.at > CAPTURE_TTL_MS) {
    tokensByTab.delete(tabId);
    return "";
  }
  return entry.token;
}

// ---------------------------------------------------------------------------
// Whose page a request belongs to
//
// `tabId` is **-1** for anything fetched by a service worker, a shared worker
// or a dedicated worker, and a great many players run their entire transport
// inside one — the manifest and every segment alike. Dropping those requests
// left the capture map permanently empty on exactly the sites that need it, and
// a page that was visibly playing looked as though it held nothing at all.
//
// The request still says which origin asked for it (`initiator` on Chrome,
// `originUrl` on Firefox), so the tab is resolved from that. Frame addresses
// are recorded as they load — including sub-frames, because a player embedded
// from another host has its own origin and its worker's requests carry that one
// rather than the page's.
// ---------------------------------------------------------------------------
const tabForOrigin = new Map();     // origin -> { tabId, at }
let activeTabId = -1;

function originOf(url) {
  try {
    const parsed = new URL(url);
    return /^https?:$/.test(parsed.protocol) ? parsed.origin : "";
  } catch (error) {
    return "";
  }
}

//: The address each tab is on, so a capture can be stamped with the page it
//: belongs to rather than only with the number of the tab that fetched it.
//:
//: A numeric tab handle turned out to be the wrong identity twice over. A field
//: log showed the worker recording a capture and, twenty-one seconds later,
//: reporting *"0 remembered by the worker"* for the tab asking — so the
//: recording key and the lookup key were not the same. And a player's site is a
//: single-page application: pressing Radio rewrites the URL, which fired the
//: navigation handler and threw away every capture for a video that had not
//: changed at all.
const tabUrlById = new Map();

function noteTabUrl(tabId, url) {
  if (tabId == null || tabId < 0 || !/^https?:/i.test(url || "")) return;
  tabUrlById.set(tabId, url);
}

function noteFrameOrigin(tabId, url) {
  noteTabUrl(tabId, url);
  const origin = originOf(url);
  if (!origin || tabId == null || tabId < 0) return;
  const existing = tabForOrigin.get(origin);
  // The tab in view wins a tie: two tabs on one site is the ambiguous case, and
  // the one being looked at is the one whose badge and panel the user will read.
  if (existing && existing.tabId !== tabId && existing.tabId === activeTabId) {
    existing.at = Date.now();
    return;
  }
  tabForOrigin.set(origin, { tabId, at: Date.now() });
}

function forgetTab(tabId) {
  for (const [origin, entry] of tabForOrigin.entries()) {
    if (entry.tabId === tabId) tabForOrigin.delete(origin);
  }
}

function tabIdFor(details) {
  if (details.tabId != null && details.tabId >= 0) return details.tabId;
  const origin = originOf(details.initiator || details.originUrl || "");
  if (!origin) return -1;
  const entry = tabForOrigin.get(origin);
  if (!entry) return -1;
  if (Date.now() - entry.at > CAPTURE_TTL_MS) {
    tabForOrigin.delete(origin);
    return -1;
  }
  return entry.tabId;
}

function rememberMedia(details) {
  const tabId = tabIdFor(details);
  if (tabId < 0) return;
  details = details.tabId === tabId ? details : { ...details, tabId };

  // A POST is the player using the server-driven protocol, where the request
  // body — not the URL — says which stream and which byte range is wanted. It
  // cannot be replayed from the URL alone and is never offered as a download.
  //
  // The endpoint is still worth keeping. On a page served this way the player
  // fetches no plain media URL at all, so nothing else is ever captured, and
  // treating the POST as noise left the capture map permanently empty — which
  // is why the panel's "already loaded" list never appeared on YouTube. What
  // the browser has here is an *attested session*, and its endpoint is the
  // thing worth carrying over.
  if (details.method === "POST") {
    // Only that one protocol is understood well enough to say a POST means a
    // server-driven player. Everywhere else a POST is just a POST, and
    // treating it as a streaming endpoint would mark ordinary pages as
    // undownloadable.
    if (isYouTubeMedia(details.url)) {
      serverDrivenTabs.add(details.tabId);
      endpointByTab.set(details.tabId, { url: details.url, at: Date.now() });
    }
    return;
  }

  const entry = (isYouTubeMedia(details.url) ? parseMediaUrl(details.url) : null)
    || genericEntry(details.url, headerValue(details, "content-type"),
                    headerValue(details, "content-length"),
                    headerValue(details, "content-range"));
  if (!entry) return;
  const known = capturedByTab.get(details.tabId);
  if (!known || !known.has(entry.itag)) {
    // The tab it is filed under, named here as well as at the lookup. Without
    // both halves, a capture recorded and then not found is indistinguishable
    // from one never recorded at all — three sessions were spent there.
    report(`captured ${entry.kind || "media"} for tab ${details.tabId}: `
      + `${entry.url}`);
  }
  // What the browser actually sent for this exact request. It is the whole
  // difference between a URL that plays in the tab and the same URL refused
  // with 403 the moment anything else asks for it.
  const sent = headersByRequest.get(details.requestId);
  if (sent) entry.headers = sent;
  remember(details.tabId, entry);
  showDetectedCount(details.tabId);
  announceCaptures(details.tabId);
}

// ---------------------------------------------------------------------------
// Captures outlive this service worker
//
// A manifest-v3 service worker is **terminated after about thirty seconds of
// inactivity** and restarted on the next event. Every map in this file is
// ordinary memory, so all of it evaporates — and what evaporates here is the
// one route that works when the site refuses to describe its own streams.
//
// Measured from a field log rather than inferred from the specification: the
// player fetched a progressive stream at 11:57:14 and the extension recorded
// it; the download was clicked at 12:09:57, twelve minutes later, and the
// engine reported *"the browser sent 0 media address(es) for this page"*. The
// capture TTL is thirty minutes and the tab was never navigated, so nothing in
// this file cleared it. The worker had simply been recycled in between.
//
// That is the whole reason years of "the rescue never fires in the field while
// it fires in every test" — a test clicks within seconds of playing, and a
// person does not.
//
// `chrome.storage.session` is the right home: it lives in memory for the
// browser session, survives worker restarts, is never written to disk, and is
// cleared when the browser closes. Exactly the lifetime a capture should have.
// ---------------------------------------------------------------------------
const CAPTURE_STORE = "captures";
let captureRestore = null;
let captureSaveTimer = null;

function serialiseCaptures() {
  const out = {};
  for (const [tabId, forTab] of capturedByTab.entries()) {
    out[tabId] = [...forTab.values()];
  }
  return out;
}

//: Debounced, because `remember` runs once per media request and an adaptive
//: player makes several a second. The delay is short enough that a worker
//: shutting down still writes: the worker is kept alive while a timer of this
//: length is pending, and the write itself is what the next start reads.
function persistCaptures() {
  if (captureSaveTimer) return;
  captureSaveTimer = setTimeout(() => {
    captureSaveTimer = null;
    try {
      chrome.storage.session.set({ [CAPTURE_STORE]: serialiseCaptures() });
    } catch (error) {
      /* session storage is unavailable; captures are then worker-lived */
    }
  }, 400);
}

//: Merged, never assigned: a request may have been captured *since* this worker
//: started and before the restore finished, and that one is the newer of the
//: two. Overwriting the live map with the stored copy would lose exactly the
//: capture the user just made by pressing play.
function ensureCapturesRestored() {
  if (captureRestore) return captureRestore;
  captureRestore = (async () => {
    let stored = {};
    try {
      const bag = await chrome.storage.session.get(CAPTURE_STORE);
      stored = (bag && bag[CAPTURE_STORE]) || {};
    } catch (error) {
      stored = {};
    }
    for (const [tabId, entries] of Object.entries(stored)) {
      const id = Number(tabId);
      let forTab = capturedByTab.get(id);
      if (!forTab) {
        forTab = new Map();
        capturedByTab.set(id, forTab);
      }
      for (const entry of entries || []) {
        if (!entry || !entry.url) continue;
        const existing = forTab.get(entry.itag);
        if (!existing || (existing.at || 0) < (entry.at || 0)) {
          forTab.set(entry.itag, entry);
        }
      }
    }
  })();
  return captureRestore;
}

// Started at once, so a worker woken by a request has its captures back before
// anything asks for them; awaited where the answer must be complete.
ensureCapturesRestored();

//: How many captures to keep for one tab. A long viewing session touches a
//: great many URLs, and a list nobody can read is no better than none.
const MAX_CAPTURES_PER_TAB = 40;

function remember(tabId, entry) {
  let forTab = capturedByTab.get(tabId);
  if (!forTab) {
    forTab = new Map();
    capturedByTab.set(tabId, forTab);
  }
  // The page this belongs to, recorded now rather than inferred later. It is
  // what lets a capture be found again when the tab handle does not match.
  if (!entry.page) entry.page = tabUrlById.get(tabId) || "";
  const existing = forTab.get(entry.itag);
  // Keep the newest URL for each quality; they are refreshed as playback runs.
  if (!existing || existing.at < entry.at) forTab.set(entry.itag, entry);
  if (forTab.size > MAX_CAPTURES_PER_TAB) {
    // Drop the oldest, never the manifests: one of those is the whole stream,
    // and it is fetched once at the start — exactly the entry a cap keyed on
    // recency would throw away first.
    const disposable = [...forTab.entries()]
      .filter(([, value]) => value.kind !== "manifest")
      .sort((a, b) => a[1].at - b[1].at);
    while (forTab.size > MAX_CAPTURES_PER_TAB && disposable.length) {
      forTab.delete(disposable.shift()[0]);
    }
  }
  persistCaptures();
}

function isYouTubeMedia(url) {
  try {
    return /(^|\.)googlevideo\.com$/.test(new URL(url).hostname);
  } catch (error) {
    return false;
  }
}

function headerValue(details, name) {
  const headers = details.responseHeaders || [];
  const found = headers.find(
    (header) => (header.name || "").toLowerCase() === name);
  return found ? found.value || "" : "";
}

//: Below this, a media file is a sound effect rather than a download.
//:
//: YouTube's own interface offers `failure.mp3`, `no_input.mp3`, `open.mp3`
//: and `success.mp3` — each a few kilobytes, each correctly classified as
//: audio, and each offered in the panel beside the video. A person opening
//: that menu is looking for the film, and four beeps in front of it are worse
//: than nothing. Nothing anyone wants to download is this small.
const SMALLEST_WORTH_OFFERING = 512 * 1024;
//: The same idea for sound, which is smaller by nature. Well above every
//: interface effect measured (9–20 KB) and well below a real track.
const SMALLEST_AUDIO_WORTH_OFFERING = 128 * 1024;

//: A capture from any site, in the shape the panel already renders.
function genericEntry(url, contentType, contentLength, contentRange = "") {
  const what = classifyMediaUrl(url, contentType);
  if (!what) return null;
  // The file, not the slice of it the player happened to ask for.
  url = withoutSlice(url);
  // `Content-Range: bytes 0-823/237996` — the number after the slash is the
  // file. Judging a clip by the 824 bytes of its first slice threw it away as
  // a sound effect, which is half of why Instagram's audio and video both went
  // missing from the panel at different times.
  const whole = /\/\s*(\d+)\s*$/.exec(String(contentRange || ""));
  const size = whole ? Number(whole[1]) : (Number(contentLength || 0) || 0);
  // A manifest is bytes of text describing gigabytes of video, so its own size
  // says nothing; everything else is judged by it when it is known.
  //
  // Two exceptions, both measured. A track the CDN itself declares to be half
  // of a clip is never a stray sound: Instagram's audio half of a 32-second
  // post is 238 KB, and the flat half-megabyte floor threw it away — which is
  // why the video it belonged to could never be paired with its sound. And
  // sound on its own is legitimately smaller than video: the beeps this floor
  // exists to hide are nine to twenty kilobytes, so audio is judged against a
  // floor of its own rather than against a film's.
  const floor = what.asset ? 0
    : what.kind === "audio" ? SMALLEST_AUDIO_WORTH_OFFERING
    : SMALLEST_WORTH_OFFERING;
  if (what.kind !== "manifest" && size && size < floor) {
    return null;
  }
  return {
    url,
    // The identity a capture is kept under. For YouTube that is the itag; here
    // the URL itself is the only thing that distinguishes one from another —
    // with the byte range stripped, so a clip fetched in thirty pieces is one
    // entry and not thirty.
    itag: url,
    //: Which clip this is half of, when the CDN says so. Both halves of an
    //: Instagram post carry the same one, which is what lets the video be
    //: paired with its own sound rather than with whatever else is on the page.
    asset: what.asset || "",
    kind: what.kind,
    name: what.name,
    size,
    mime: (contentType || "").split(";")[0].trim(),
    at: Date.now(),
  };
}

try {
  chrome.webRequest.onSendHeaders.addListener(
    rememberMedia, { urls: MEDIA_PATTERNS, types: MEDIA_TYPES },
  );
  // The response is where an extensionless URL finally says what it is. Plenty
  // of players fetch `/manifest?id=…`, which no amount of looking at the
  // address can classify.
  chrome.webRequest.onHeadersReceived.addListener(
    rememberMedia, { urls: MEDIA_PATTERNS, types: MEDIA_TYPES },
    ["responseHeaders"],
  );
  // The request body is where the proof-of-origin token travels, and it is
  // only available on this event.
  chrome.webRequest.onBeforeRequest.addListener(
    rememberToken, { urls: YOUTUBE_MEDIA_PATTERNS }, ["requestBody"],
  );
  // The visitor identity the token was minted for.
  chrome.webRequest.onSendHeaders.addListener(
    rememberVisitor, { urls: API_PATTERNS }, ["requestHeaders"],
  );
  // The headers the browser sent, kept so the exact request can be reproduced.
  // `extraHeaders` is what makes Chrome disclose Referer and Origin; Firefox
  // rejects the flag, so the plain form is the fallback rather than the choice.
  try {
    chrome.webRequest.onBeforeSendHeaders.addListener(
      rememberHeaders, { urls: MEDIA_PATTERNS, types: MEDIA_TYPES },
      ["requestHeaders", "extraHeaders"],
    );
  } catch (error) {
    chrome.webRequest.onBeforeSendHeaders.addListener(
      rememberHeaders, { urls: MEDIA_PATTERNS, types: MEDIA_TYPES },
      ["requestHeaders"],
    );
  }
  // Which tab each frame's origin belongs to, so a request made by that frame's
  // worker — which carries no tab of its own — can still be attributed.
  chrome.webRequest.onBeforeRequest.addListener(
    (details) => noteFrameOrigin(details.tabId, details.url),
    { urls: MEDIA_PATTERNS, types: ["main_frame", "sub_frame"] },
  );
} catch (error) {
  /* the permission may be declined; extraction still works without it */
}

chrome.tabs.onRemoved.addListener((tabId) => {
  capturedByTab.delete(tabId);
  // Or the stored copy would hand this tab's captures back on the next worker
  // start, long after the tab itself is gone.
  persistCaptures();
  serverDrivenTabs.delete(tabId);
  tokensByTab.delete(tabId);
  visitorByTab.delete(tabId);
  endpointByTab.delete(tabId);
  requestByTab.delete(tabId);
  forgetTab(tabId);
  announcedByTab.delete(tabId);
  playersByTab.delete(tabId);
});
// The badge belongs to whichever tab is being looked at, so switching tabs has
// to redraw it — otherwise it keeps reporting what the previous page found.
chrome.tabs.onActivated.addListener(({ tabId }) => {
  activeTabId = tabId;
  showDetectedCount(tabId, true);
});
chrome.tabs.onUpdated.addListener((tabId, changes, tab) => {
  // A navigation invalidates everything captured for the previous page — but
  // only a navigation to a *different* page. A player's site is a single-page
  // application: pressing Radio rewrites `watch?v=X` to
  // `watch?v=X&list=RD…&start_radio=1`, which fired this handler and threw away
  // every capture for a video that had not changed. Seen in a field log as the
  // player fetching a stream and the download twenty seconds later finding
  // nothing at all.
  const wasOn = tabUrlById.get(tabId) || "";
  // The tab's address is kept current either way, so the next comparison is
  // against where it actually is rather than where it last navigated *away*
  // from.
  if (changes.url) noteTabUrl(tabId, changes.url);
  if (changes.url && !samePage(wasOn, changes.url)) {
    capturedByTab.delete(tabId);
    persistCaptures();
    serverDrivenTabs.delete(tabId);
    tokensByTab.delete(tabId);
    visitorByTab.delete(tabId);
    endpointByTab.delete(tabId);
    requestByTab.delete(tabId);
    forgetTab(tabId);
    playersByTab.delete(tabId);
    noteFrameOrigin(tabId, changes.url);
    showDetectedCount(tabId, true);
    announceCaptures(tabId);
  } else if (tab && tab.url) {
    noteFrameOrigin(tabId, tab.url);
  }
});
// A service worker restart loses every map above, so the tabs already open have
// to be read back or their workers' requests would go unattributed until the
// user navigated.
try {
  chrome.tabs.query({}, (tabs) => {
    for (const tab of tabs || []) {
      noteFrameOrigin(tab.id, tab.url || "");
      if (tab.active) activeTabId = tab.id;
    }
  });
} catch (error) {
  /* the permission may be declined; attribution then needs a navigation */
}

//: Adaptive itags whose media is audio. Captured URLs carry no manifest, so
//: the kind has to be read from the stream identity or the declared MIME type.
const AUDIO_ITAGS = new Set([
  "139", "140", "141", "171", "172", "249", "250", "251", "256", "258",
  "325", "328", "380",
]);

function isAudioStream(entry) {
  // What it was classified as when it was captured comes first. A CDN may
  // serve a sound-only file as `video/mp4` — Instagram does — so the content
  // type is the weaker witness of the two.
  if (entry.kind === "audio") return true;
  if (entry.mime && entry.mime.startsWith("audio/")) return true;
  return AUDIO_ITAGS.has(String(entry.itag));
}

function isVideoOnly(entry) {
  // Asked first, for the same reason: `video/mp4` on a track that is known to
  // be sound must not make it a video wanting a companion of its own.
  if (isAudioStream(entry)) return false;
  // The progressive itags carry both tracks and need no companion; everything
  // else the player fetches is a single-kind adaptive stream.
  if (entry.mime && entry.mime.startsWith("video/")) return true;
  return !["18", "22", "37", "59"].includes(String(entry.itag));
}

function bestCapturedAudio(streams, video) {
  // Prefer an audio track that shares the video's container: an MP4 video and
  // a WebM/Opus track cannot become one file, so a lower-bitrate AAC track is
  // the better companion.
  const wantsMp4 = !(video.mime || "").includes("webm");
  let audio = streams.filter(isAudioStream);
  if (!audio.length) return null;
  // On a page holding several clips — a feed — the sound of the wrong one is
  // worse than no sound at all. When the CDN names the clip each half belongs
  // to, only that clip's own audio is a candidate.
  if (video.asset) {
    const sameClip = audio.filter((entry) => entry.asset === video.asset);
    if (sameClip.length) audio = sameClip;
    else return null;
  }
  const matching = audio.filter(
    (entry) => (entry.mime || "").includes("webm") !== wantsMp4,
  );
  const pool = matching.length ? matching : audio;
  return pool.reduce((best, entry) => (entry.size > best.size ? entry : best));
}

// ---------------------------------------------------------------------------
// Fetching on the application's behalf
//
// Measured on 2026-08-12, one address, one machine, one second: the desktop
// application 403, `curl` with browser headers 403, `curl` bare 403 — and
// Chrome itself **200** for the addresses its own player minted. The requests
// that address answers are the browser's, so when the application is refused it
// says so in its reply and this makes the request instead, handing the bytes
// back a block at a time.
//
// The application stays the download manager: it names the file, owns the row,
// writes the bytes and publishes the result. This is the socket, and nothing
// else.
// ---------------------------------------------------------------------------
//: How much is handed over per message. Large enough that a film is not a
//: million round trips, small enough to stay well inside the native-messaging
//: ceiling when the relay is in the path.
const BROWSER_FETCH_BLOCK = 512 * 1024;

//: Transfers a page is reading for this worker, by request id.
//:
//: The worker's own `fetch` carries the extension's origin and no referrer;
//: the page's carries the origin, referrer and request context the player
//: used. A media CDN decides on exactly those, so the page is asked first and
//: this is where its blocks arrive on their way to the application.
const pageFetches = new Map();
let pageFetchSequence = 0;

//: Ask the tab's own document to fetch `url`, streaming what it reads into
//: `onBlock`. Resolves with the number of bytes handed over.
//:
//: Rejects when no content script answers — the caller then falls back to the
//: worker's own request, which is better than nothing on a page this
//: extension does not run in.
function fetchInPage(tabId, url, headers, onBlock) {
  return new Promise((resolve, reject) => {
    if (!(tabId >= 0)) {
      reject(new Error("no tab to fetch from"));
      return;
    }
    const id = `pf${++pageFetchSequence}`;
    pageFetches.set(id, {
      onBlock,
      sent: 0,
      finish: (error) => {
        pageFetches.delete(id);
        if (error) reject(error);
        else resolve(id);
      },
    });
    try {
      chrome.tabs.sendMessage(tabId, {
        type: "ixdFetch", id, url, headers: headers || {},
      }, { frameId: 0 }, (response) => {
        const failure = chrome.runtime.lastError;
        if (failure || !response || !response.started) {
          const state = pageFetches.get(id);
          if (state) {
            pageFetches.delete(id);
            reject(new Error((failure && failure.message)
              || "the page did not take the request"));
          }
        }
      });
    } catch (error) {
      pageFetches.delete(id);
      reject(error);
    }
  });
}

async function fetchForApplication(instruction, tabId) {
  let url = instruction && instruction.url;
  if (!url) throw new Error("nothing to fetch");

  // The application's own address for this rendition is frequently one the
  // origin will not serve to anybody — YouTube hands out `videoplayback`
  // addresses whose `n` parameter its player transforms before use, and
  // nothing outside the player does that. Measured: an address straight from
  // extraction is refused to the application, to `curl` and to Chrome itself,
  // while the player's own address for the same stream plays.
  //
  // So when this worker holds the player's address for the same rendition,
  // that is the one fetched. It was minted by the session that is served.
  const wanted = String((instruction && instruction.itag) || "");
  if (wanted) {
    for (const entry of capturedFor(tabId) || []) {
      if (String(entry.itag) === wanted && entry.url) {
        if (entry.url !== url) {
          report(`using the player's own address for itag ${wanted} rather `
            + "than the one extraction produced");
        }
        url = entry.url;
        break;
      }
    }
  }

  const begun = await callChecked("browser_stream_begin", {
    url,
    filename: instruction.filename || "",
    title: instruction.title || "",
    size: instruction.size || 0,
    referrer: instruction.referrer || "",
  });
  const id = begun.id;
  report(`fetching for the application: ${instruction.title || url}`);

  let sent = 0;
  try {
    // Whole blocks, so a stream that arrives in small pieces does not become
    // one message per piece.
    let pending = [];
    let pendingSize = 0;
    const flush = async () => {
      if (!pendingSize) return;
      const joined = new Uint8Array(pendingSize);
      let at = 0;
      for (const piece of pending) { joined.set(piece, at); at += piece.length; }
      pending = [];
      pendingSize = 0;
      await callChecked("browser_stream_chunk", {
        id, data: base64OfBytes(joined),
      });
      sent += joined.length;
    };
    const take = async (bytes) => {
      pending.push(bytes);
      pendingSize += bytes.length;
      if (pendingSize >= BROWSER_FETCH_BLOCK) await flush();
    };

    // This worker first, and the page only if it cannot.
    //
    // The page looked like the better context — it is the one the player
    // fetched from — and the field says otherwise: a content script's request
    // is subject to CORS, so a media address that answers no preflight fails
    // there as a bare "Failed to fetch", with no status to report and nothing
    // to distinguish it from a refusal. This worker's request is exempt by
    // host permission, so it at least comes back with an answer.
    //
    // Neither is a route to YouTube. Measured 2026-08-12, in a real browser:
    // the address the player itself minted is **403 on a second fetch from the
    // youtube.com page that minted it**, cookies, origin and referrer intact.
    // The address is not replayable by anybody, so no amount of moving the
    // request around will serve it. What the player *receives* is the only
    // copy of that media there is.
    let served = false;
    let refusal = null;
    try {
      const response = await fetch(url, {
        credentials: "include",
        headers: instruction.headers || {},
      });
      if (!response.ok) {
        refusal = new Error(`the browser was refused too: HTTP ${response.status}`);
      } else {
        const reader = response.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          await take(value);
        }
        served = true;
      }
    } catch (error) {
      refusal = error;
    }

    if (!served) {
      report(`this extension was refused (${refusal.message || refusal}); `
        + "asking the page to fetch it instead");
      try {
        await fetchInPage(tabId, url, instruction.headers, take);
        served = true;
      } catch (error) {
        report(`the page could not fetch it either (${error.message || error})`);
      }
    }
    if (!served) throw refusal;
    await flush();
    const finished = await callChecked("browser_stream_end", { id, ok: true });
    report(`handed over ${sent} bytes: ${finished.path || ""}`);
    return finished;
  } catch (error) {
    await callChecked("browser_stream_end", {
      id, ok: false, error: String(error.message || error),
    }).catch(() => {});
    throw error;
  }
}

//: Base64 without blowing the stack on a large block — `String.fromCharCode`
//: applied to a whole megabyte throws `RangeError`.
function base64OfBytes(bytes) {
  let binary = "";
  const step = 0x8000;
  for (let index = 0; index < bytes.length; index += step) {
    binary += String.fromCharCode.apply(
      null, bytes.subarray(index, index + step));
  }
  return btoa(binary);
}

//: The inverse, for a block a page has read on this worker's behalf.
function bytesOfBase64(text) {
  const binary = atob(text || "");
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index++) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

//: Whether two addresses are the same page as far as captures are concerned.
//: A player's site is a single-page application: moving from one video to the
//: next changes the query string and nothing else, so an origin-and-path
//: comparison would call two different videos the same page.
function samePage(one, other) {
  try {
    const a = new URL(one);
    const b = new URL(other);
    if (a.origin !== b.origin || a.pathname !== b.pathname) return false;
    return (a.searchParams.get("v") || "") === (b.searchParams.get("v") || "");
  } catch (error) {
    return one === other;
  }
}

//: What the tab captured, but only when it belongs to the page being asked
//: about.
//:
//: The engine consults these *before* asking the site anything, so a capture
//: from the wrong page is no longer a missed opportunity — it is a download of
//: the wrong video under the right title, which is the one defect nobody finds
//: until they watch the file. The map is cleared on navigation, so this only
//: catches the case that survives it: a request naming a page the tab has
//: already left, from a panel that has not caught up or a popup speaking for
//: another tab.
function capturesForPage(tabId, senderTab, pageUrl) {
  const current = (senderTab && senderTab.url) || "";
  if (current && pageUrl && !samePage(current, pageUrl)) {
    // Said out loud. This guard withholding everything and the tab simply
    // having nothing both arrive at the engine as "0 media addresses", and
    // they are opposite defects.
    report(`captures withheld: the request names ${pageUrl} and the tab is `
      + `on ${current}`);
    return [];
  }
  const usable = (entry) => entry.kind !== "manifest" && entry.url;
  const mine = (capturedFor(tabId) || []).filter(usable);
  if (mine.length) return mine;

  // Nothing under this tab's number, which is not the same thing as nothing
  // for this page. A field log showed the worker recording a capture and then
  // reporting none for the tab that asked, so the recording key and the lookup
  // key were not the same — and a capture stamped with the page it was made on
  // can be found whichever handle it was filed under.
  const elsewhere = [];
  for (const [otherTab, forTab] of capturedByTab.entries()) {
    if (otherTab === tabId) continue;
    for (const entry of forTab.values()) {
      if (usable(entry) && entry.page && pageUrl
          && samePage(entry.page, pageUrl)) {
        elsewhere.push(entry);
      }
    }
  }
  if (elsewhere.length) {
    report(`captures found under another tab handle for the same page `
      + `(${elsewhere.length})`);
  }
  return elsewhere;
}

//: Media addresses read from the page's own resource timeline and sent up by
//: the content script. Turned into the same records as anything this worker
//: saw, so nothing downstream has to know where they came from.
//: Everything worth sending with a download, from both sources, and a line in
//: the log saying what each of them held.
//:
//: Three sessions were spent on "the browser sent 0 media addresses" without
//: being able to tell *which* half was empty — the worker's memory, the page's
//: timeline, or the guard withholding both. One line answers it, and it is
//: printed once per download rather than once per request.
function capturesToSend(tabId, senderTab, pageUrl, pageMedia, pageTotal) {
  const held = capturesForPage(tabId, senderTab, pageUrl);
  const fromPage = pageTimelineCaptures(pageMedia);
  const merged = mergeCaptureLists(held, fromPage);
  const itags = merged.map((entry) => entry.itag).filter(Boolean).join(",");
  const filed = [...capturedByTab.keys()].join(",") || "none";
  report(`captures for tab ${tabId}: ${held.length} remembered by the worker, `
    + `${fromPage.length} in the page's timeline of ${pageTotal || 0} `
    + `resources, ${merged.length} sent`
    + (itags ? ` (itags ${itags})` : "")
    + `; tabs holding captures: ${filed}`);
  return merged;
}

function pageTimelineCaptures(list) {
  const found = [];
  for (const item of list || []) {
    const url = typeof item === "string" ? item : (item && item.url) || "";
    if (!url) continue;
    const entry = isYouTubeMedia(url) ? parseMediaUrl(url) : null;
    if (!entry) continue;
    if (!entry.size && item && item.size) entry.size = item.size;
    found.push(entry);
  }
  return found;
}

//: The worker's own record wins on a tie: it is the only one carrying the
//: headers the browser actually sent, and a media CDN decides by header.
function mergeCaptureLists(primary, extra) {
  const byItag = new Map();
  for (const entry of primary) byItag.set(entry.itag, entry);
  for (const entry of extra) {
    if (!byItag.has(entry.itag)) byItag.set(entry.itag, entry);
  }
  return [...byItag.values()];
}

function capturedFor(tabId) {
  const forTab = capturedByTab.get(tabId);
  if (!forTab) return [];
  const now = Date.now();
  const live = [];
  for (const [itag, entry] of forTab.entries()) {
    if (now - entry.at > CAPTURE_TTL_MS) forTab.delete(itag);
    else live.push(entry);
  }
  return live.sort((a, b) => b.size - a.size);
}

// ---------------------------------------------------------------------------
// context menus
// ---------------------------------------------------------------------------
const MENU_LINK = "ixd-download-link";
const MENU_MEDIA = "ixd-download-media";
const MENU_PAGE = "ixd-download-page-media";

function buildMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: MENU_LINK,
      title: "Download link with IXD",
      contexts: ["link"],
    });
    chrome.contextMenus.create({
      id: MENU_MEDIA,
      title: "Download this media with IXD",
      contexts: ["video", "audio", "image"],
    });
    chrome.contextMenus.create({
      id: MENU_PAGE,
      title: "Download video on this page with IXD",
      contexts: ["page"],
    });
  });
}

// ---------------------------------------------------------------------------
// The toolbar button hands the page over
//
// The extension is a relay, not an application. It watches every request, works
// out what is media, keeps the session that made it work — and then gets out of
// the way: clicking it sends what it found to the desktop application, which is
// where a person chooses a quality and where the download list already lives.
// There is no popup, because there was never anything a second interface could
// say that the application does not say better.
// ---------------------------------------------------------------------------
async function handOver(tab) {
  if (!tab || !tab.id) return;
  const streams = capturedFor(tab.id) || [];
  // A manifest describes the whole stream at every quality the site publishes,
  // so it is the better thing to hand over than the page — and better than any
  // single file. Failing that, the page itself, which the extractors can read.
  const manifest = streams.find((entry) => entry.kind === "manifest");
  const chosen = manifest || streams[0] || null;
  const url = chosen ? chosen.url : (tab.url || "");
  const problem = restrictionFor(url);
  if (problem) {
    notify("Internet Xtreme Downloader", problem);
    return;
  }
  try {
    await callChecked("present", {
      url,
      pageUrl: tab.url || "",
      title: tab.title || "",
      cookies: await cookieHeaderFor(url),
      userAgent: navigator.userAgent,
      referrer: tab.url || "",
      headers: (chosen && chosen.headers) || {},
      // Everything else found on the page, so choosing a different one in the
      // application does not run into the refusal this one got past.
      streams: streams.map((entry) => entry.url),
    });
  } catch (error) {
    notify("Internet Xtreme Downloader", String(error.message || error));
  }
}

chrome.action.onClicked.addListener(handOver);

// Keep the count on the icon current while transfers are running.
//
// A service worker is not permanently alive, so this is a best effort rather
// than a clock: it ticks while the worker is up — which is whenever anything
// is happening — and the number is refreshed on the next message otherwise.

// ---------------------------------------------------------------------------
// One log, both halves
//
// Half of what goes wrong is on this side of the bridge, where the only witness
// is a service-worker console the user has to know how to open. Sending it to
// the application puts the browser's account and the engine's in one place, in
// order — which is what turns a real test on a real site into evidence rather
// than an impression.
//
// Deliberately best-effort: logging must never be able to break a download, and
// it is dropped silently when the application is not running.
// ---------------------------------------------------------------------------
let logQueue = Promise.resolve();

function report(message, level) {
  logQueue = logQueue
    .then(() => call("log", { message: String(message).slice(0, 2000), level: level || "info" }))
    .catch(() => {});
}

chrome.runtime.onInstalled.addListener(() => {
  buildMenus();
  getSettings().then((settings) => chrome.storage.local.set({ settings }));
});
chrome.runtime.onStartup.addListener(buildMenus);

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  try {
    if (info.menuItemId === MENU_LINK && info.linkUrl) {
      await sendPlainDownload(info.linkUrl, tab);
    } else if (info.menuItemId === MENU_MEDIA && info.srcUrl) {
      await sendPlainDownload(info.srcUrl, tab);
    } else if (info.menuItemId === MENU_PAGE) {
      await sendMediaPage(info.pageUrl || (tab && tab.url), tab);
    }
  } catch (error) {
    notify("Internet Xtreme Downloader", String(error.message || error));
  }
});

async function sendPlainDownload(url, tab) {
  requireDownloadable(url);
  const cookies = await cookieHeaderFor(url);
  const result = await callChecked("add", {
    url,
    cookies,
    referrer: (tab && tab.url) || "",
    userAgent: navigator.userAgent,
  });
  // See the note in the interception listener: when the application is asking
  // where the file goes, its window is the notification.
  if (!result.confirming) {
    notify("Sent to Internet Xtreme Downloader", result.filename || url);
  }
}

async function sendMediaPage(pageUrl, tab) {
  requireDownloadable(pageUrl);
  const settings = await getSettings();
  const result = await callChecked("add_media", {
    url: pageUrl,
    quality: settings.preferredQuality,
    cookies: await cookieHeaderFor(pageUrl),
    userAgent: navigator.userAgent,
    poToken: tokenFor(tab ? tab.id : -1),
    visitorData: visitorFor(tab ? tab.id : -1),
  });
  notify("Sent to Internet Xtreme Downloader", result.filename || pageUrl);
}

// ---------------------------------------------------------------------------
// extraction cache
//
// The hover panel needs the quality list *before* the user clicks, so pages
// are analysed speculatively. Results are cached per URL and shared between
// the content script, the popup and the context menus, which keeps a hovering
// mouse from firing one extraction per frame.
// ---------------------------------------------------------------------------
const CACHE_TTL_MS = 5 * 60 * 1000;
// Failures are remembered only briefly. Caching them for as long as successes
// would mean that starting the desktop application, or reconnecting, appeared
// to change nothing for minutes afterwards.
const FAILURE_TTL_MS = 15 * 1000;
const extractionCache = new Map();
const extractionInflight = new Map();

function cacheKey(url) {
  try {
    const parsed = new URL(url);
    parsed.hash = "";
    return parsed.toString();
  } catch (error) {
    return url;
  }
}

async function extractCached(url, options = {}) {
  const force = Boolean(options.force);
  requireDownloadable(url);
  // The page is part of the identity of an extraction, not a detail of it: the
  // same CDN address analysed from two different pages is two different
  // requests as far as the origin's hotlink check is concerned.
  const key = cacheKey(url) + "|" + (options.referrer || "");

  // The proof of origin is deliberately **not** part of the key. It was, and
  // one arriving between the panel's speculative analysis and the click made
  // the click a cache miss — measured on a real page as the same video
  // extracted three times in two minutes, 14 seconds each. It is only worth
  // repeating the work when a token has been *gained*, which is the one case
  // where the answer can differ.
  const wantsToken = Boolean(options.poToken);
  if (!force) {
    const hit = extractionCache.get(key);
    const ttl = hit && hit.error ? FAILURE_TTL_MS : CACHE_TTL_MS;
    const stale = hit && wantsToken && !hit.hadToken;
    if (hit && !stale && Date.now() - hit.at < ttl) {
      if (hit.error) throw new Error(hit.error);
      return hit.info;
    }
    const inflight = extractionInflight.get(key);
    if (inflight) return inflight;
  }

  const started = Date.now();
  const promise = (async () => {
    try {
      const info = await callChecked("extract", {
        url,
        // A click, rather than the panel looking ahead. The messaging host
        // starts the application for this and not for a prefetch, so opening
        // a video page cannot resurrect an application that was quit.
        user_initiated: Boolean(options.userInitiated),
        cookies: await cookieHeaderFor(url),
        userAgent: navigator.userAgent,
        referrer: options.referrer || "",
        headers: options.headers || {},
        poToken: options.poToken || "",
        visitorData: options.visitorData || "",
      });
      extractionCache.set(key, { info, at: Date.now(), hadToken: wantsToken });
      report(`extract ok in ${Date.now() - started} ms: ${url} `
             + `(${(info.formats || []).length} formats)`);
      return info;
    } catch (error) {
      const message = String(error.message || error);
      extractionCache.set(key, { error: message, at: Date.now(), hadToken: wantsToken });
      report(`extract failed after ${Date.now() - started} ms: ${url} — ${message}`,
             "error");
      throw error;
    } finally {
      extractionInflight.delete(key);
    }
  })();

  extractionInflight.set(key, promise);
  return promise;
}

// ---------------------------------------------------------------------------
// messages from the popup, options page and content scripts
// ---------------------------------------------------------------------------
//: Ask the page's own panel what state it is in.
//:
//: A content script that never ran, one whose panel is switched off, and one
//: that ran and found nothing are indistinguishable from the toolbar, and each
//: needs a different answer.
function panelState(tabId) {
  if (tabId == null || tabId < 0) return Promise.resolve("no page");
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    setTimeout(() => finish("not answering"), 1500);
    try {
      chrome.tabs.sendMessage(tabId, { type: "ixdPing" }, { frameId: 0 },
        (reply) => {
          if (chrome.runtime.lastError || !reply) {
            finish("not running on this page");
            return;
          }
          finish(reply.enabled === false
            ? "switched off in options"
            : `running · showing ${reply.count} · ${reply.attached}`);
        });
    } catch (error) {
      finish("not running on this page");
    }
  });
}

//: Which tab a message is about.
//:
//: A content script identifies itself; the popup does not — it has no tab of
//: its own, and everything captured is recorded per tab, so asking from the
//: popup used to resolve to nothing and lose the credentials, the referrer and
//: the capture list all at once.
async function tabOf(sender) {
  if (sender && sender.tab && sender.tab.id >= 0) return sender.tab;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    return tab || null;
  } catch (error) {
    return null;
  }
}

//: The headers the browser sent when it fetched this exact address.
//:
//: A capture is a CDN address, and a CDN decides by header — so replaying the
//: set that already worked is the difference between a stream that plays in the
//: tab and the same stream refused with 403 to everything else. Nothing is
//: invented: if the address was never seen, nothing is sent.
function capturedHeadersFor(tabId, url) {
  for (const entry of capturedFor(tabId) || []) {
    if (entry.url === url && entry.headers) return entry.headers;
  }
  return {};
}

//: The page a request is being made on behalf of.
//:
//: Hotlink protection is the rule on a media CDN: a manifest or a segment is
//: served to a request that came from the site's own page and refused with 403
//: to one that arrives from nowhere. Nothing the extension sent carried this,
//: which is why a captured `.m3u8` handed to the engine came back 403 while the
//: same address played perfectly in the tab it was captured from.
function pageUrlOf(message, tab) {
  const candidate = (message && message.pageUrl) || (tab && tab.url) || "";
  return /^https?:/i.test(candidate) ? candidate : "";
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    try {
      // Before anything reads a capture. A message is often the very event
      // that wakes this worker, and until the stored copy is merged back the
      // maps are empty — which reaches the engine as "the browser sent 0 media
      // addresses" and reads exactly like a page that was never played.
      await ensureCapturesRestored();
      const senderTab = await tabOf(sender);
      const senderTabId = senderTab ? senderTab.id : -1;
      const pageUrl = pageUrlOf(message, senderTab);
      switch (message.type) {
        case "ping":
          sendResponse({ ok: true, result: await callChecked("ping") });
          break;
        case "stats":
          sendResponse({ ok: true, result: await callChecked("stats") });
          break;
        case "list":
          sendResponse({ ok: true, result: await callChecked("list") });
          break;
        case "pause":
          sendResponse({ ok: true, result: await callChecked("pause", { id: message.id }) });
          break;
        case "resume":
          sendResponse({ ok: true, result: await callChecked("resume", { id: message.id }) });
          break;
        case "remove":
          sendResponse({ ok: true, result: await callChecked("remove", { id: message.id }) });
          break;
        case "addUrl":
          requireDownloadable(message.url);
          sendResponse({
            ok: true,
            result: await callChecked("add", {
              url: message.url,
              cookies: await cookieHeaderFor(message.url),
              userAgent: navigator.userAgent,
              referrer: pageUrl,
              headers: capturedHeadersFor(senderTabId, message.url),
            }),
          });
          break;
        case "addMedia": {
          requireDownloadable(message.url);
          const settings = await getSettings();
          // The application answers with an instruction when the address is
          // refused to it and served to this browser: fetch it here and hand
          // the bytes over. It stays a download of the application's — its
          // name, its row, its file — and this is only the request.
          const queued = await callChecked("add_media", {
              url: message.url,
              format_id: message.formatId || "",
              quality: message.quality || settings.preferredQuality,
              cookies: await cookieHeaderFor(message.url),
              userAgent: navigator.userAgent,
              referrer: pageUrl,
              headers: capturedHeadersFor(senderTabId, message.url),
              // Every stream on a site is called `master.m3u8`, so a playlist
              // named after its address is named after nothing. The page is
              // what a person recognises the download by.
              title: message.title || (senderTab && senderTab.title) || "",
              // Which container the person picked, when the row offered a
              // choice. Empty means "whatever the site serves".
              container: message.container || "",
              poToken: tokenFor(senderTabId),
              visitorData: visitorFor(senderTabId),
              // The player's own request, so the engine can continue that
              // session rather than opening an unattested one of its own.
              playerRequest: (playerRequestFor(senderTabId) || {}).body || "",
              playerEndpoint: endpointFor(senderTabId),
              // Every plain media address the player was seen fetching, with
              // what identifies each one. The engine consults these *before*
              // asking the site anything: these URLs were signed for a session
              // the site has already accepted, so they are served when an
              // extraction request from the same machine is refused outright.
              //
              // Whole entries, not bare URLs. Sending only the address left
              // the engine parsing an itag out of a query string and able to
              // recognise nothing but a progressive stream — so a player that
              // used adaptive streams throughout, which is now the ordinary
              // case, gave it a list it could make no file out of. The itag,
              // MIME type and size are what let it pair a video track with the
              // audio the player loaded beside it; the headers are what make
              // the replay work at all.
              captured: capturesToSend(senderTabId, senderTab, pageUrl,
                                       message.pageMedia,
                                       message.pageResourceTotal)
                .map((entry) => ({
                  url: entry.url,
                  itag: String(entry.itag || ""),
                  mime: entry.mime || "",
                  size: entry.size || 0,
                  headers: entry.headers || {},
                })),
          });
          sendResponse({
            ok: true,
            result: queued && queued.browser_fetch
              ? await fetchForApplication(queued.browser_fetch, senderTabId)
              : queued,
          });
          break;
        }
        case "extract":
          sendResponse({
            ok: true,
            result: await extractCached(message.url, {
              force: Boolean(message.force),
              userInitiated: Boolean(message.userInitiated),
              referrer: pageUrl,
              headers: capturedHeadersFor(senderTabId, message.url),
              poToken: tokenFor(senderTabId),
              visitorData: visitorFor(senderTabId),
            }),
          });
          break;
        // What the extension has actually managed to observe for this tab.
        // Whether a proof of origin was captured decides whether a download
        // can pass the point where the server stops an unattested session,
        // and until now that was invisible from outside — leaving "it stops
        // at a third" as the only symptom of several different causes.
        case "diagnostics": {
          // The popup is not a tab, so it names the page's tab explicitly;
          // a content script is one and speaks for itself.
          const tabId = sender.tab
            ? sender.tab.id
            : (typeof message.tabId === "number" ? message.tabId : -1);
          const token = tokenFor(tabId);
          sendResponse({
            ok: true,
            result: {
              tabId,
              poToken: token ? `${token.slice(0, 12)}… (${token.length} chars)` : "",
              hasToken: Boolean(token),
              visitorData: visitorFor(tabId) ? "captured" : "",
              endpoint: endpointFor(tabId) ? "captured" : "",
              serverDriven: serverDrivenTabs.has(tabId),
              capturedStreams: (capturedFor(tabId) || []).map((e) => e.itag),
              // Whether the in-page panel is running at all. "No panel on this
              // site" has several causes that look identical from outside — the
              // script never injected, the setting is off, or it injected and
              // found nothing — and they need different answers.
              panel: await panelState(tabId),
            },
          });
          break;
        }
        // What was detected for the tab the user is looking at. The panel asks
        // through `captured`, which identifies the tab by the content script
        // that sent it — but a page whose player exposes no <video> gets no
        // panel at all, and that is exactly the page whose media only the
        // request log knows about. The popup has no sender tab, so it asks
        // here and the active tab is resolved for it.
        case "pageMedia": {
          const [tab] = await chrome.tabs.query(
            { active: true, currentWindow: true });
          const tabId = tab ? tab.id : -1;
          sendResponse({
            ok: true,
            result: {
              url: tab ? tab.url || "" : "",
              title: tab ? tab.title || "" : "",
              streams: capturedFor(tabId),
              serverDriven: serverDrivenTabs.has(tabId),
            },
          });
          break;
        }
        // Which frames of this tab have a player of their own.
        //
        // A page's player usually lives in an iframe, and that frame draws the
        // panel over its own video — correctly placed, because it is the frame
        // that knows where the video is. The top frame must not then add a
        // second control for the same thing, so it asks before showing its
        // page-level chip.
        // A block the page has read on this worker's behalf. Answered only
        // once it has been handed on, so the page's reader is paced by the
        // application rather than by the origin.
        case "ixdFetchChunk": {
          const state = pageFetches.get(message.id);
          if (!state) {
            sendResponse({ ok: false, error: "no such transfer" });
            break;
          }
          const bytes = bytesOfBase64(message.data || "");
          state.sent += bytes.length;
          await state.onBlock(bytes);
          sendResponse({ ok: true, result: { received: state.sent } });
          break;
        }
        case "ixdFetchDone": {
          const state = pageFetches.get(message.id);
          if (state) {
            state.finish(message.ok
              ? null
              : new Error(message.error || "the page's fetch failed"));
          }
          sendResponse({ ok: true, result: { closed: true } });
          break;
        }
        // The head of a response the page hook teed. Handed straight on: the
        // application reads this format already, and a service worker is the
        // wrong place to learn a protocol.
        // The page hook saying it exists. Its silence and its absence looked
        // the same from the log, and they are different faults.
        case "ixdDrm": {
          // Once per page: a player asks for a key system on every track.
          const where = message.pageUrl || pageUrl || "";
          if (!drmReported.has(where)) {
            drmReported.add(where);
            report(`${where} plays DRM-protected media (${message.system || "EME"})`
              + " — its stream is encrypted and cannot be downloaded by anything"
              + " outside the browser's own decryption module.");
          }
          sendResponse({ ok: true, result: { noted: true } });
          break;
        }
        case "ixdTeeAlive": {
          report(`the page hook is running on ${pageUrl || message.pageUrl}`
            + (message.detail ? ` — ${message.detail}` : ""));
          sendResponse({ ok: true, result: { noted: true } });
          break;
        }
        case "ixdTeed": {
          sendResponse({
            ok: true,
            result: await callChecked("browser_media_head", {
              url: message.url || "",
              page_url: pageUrl || message.pageUrl || "",
              total: message.total || 0,
              data: message.data || "",
            }).catch(() => ({ taken: false })),
          });
          break;
        }
        case "ixdPlayer": {
          const frameId = sender && sender.frameId != null ? sender.frameId : 0;
          let frames = playersByTab.get(senderTabId);
          if (!frames) {
            frames = new Set();
            playersByTab.set(senderTabId, frames);
          }
          if (message.has) frames.add(frameId);
          else frames.delete(frameId);
          sendResponse({ ok: true, result: { anyPlayer: frames.size > 0 } });
          // A frame that has just lost or found one changes the answer for
          // every other frame, and the top frame is the one that acts on it.
          if (frameId !== 0) {
            try {
              chrome.tabs.sendMessage(
                senderTabId, { type: "ixdPlayers", anyPlayer: frames.size > 0 },
                { frameId: 0 }, () => void chrome.runtime.lastError,
              );
            } catch (error) {
              /* no content script in the top frame */
            }
          }
          break;
        }
        case "captured": {
          const tabId = senderTabId;
          sendResponse({
            ok: true,
            result: {
              streams: capturedFor(tabId),
              serverDriven: serverDrivenTabs.has(tabId),
              // The tab's title, not the frame's. A player is nearly always in
              // an iframe, and the frame's own document is titled after the
              // embed — or not at all — so a panel naming what it found from
              // `document.title` fell back to the playlist's filename, which
              // is `master.m3u8` or `playlist.m3u8` on every site there is.
              title: (senderTab && senderTab.title) || "",
              pageUrl: (senderTab && senderTab.url) || "",
            },
          });
          break;
        }
        case "addCaptured": {
          const streams = capturedFor(senderTabId) || [];
          const entry = streams.find((item) => item.itag === message.itag);
          // eslint-disable-next-line no-unused-expressions
          if (!entry) throw new Error("that stream is no longer available");
          // An adaptive stream is video *or* audio, never both, so sending one
          // on its own produces a silent film. The player has usually fetched
          // the matching audio already, so it is sent with it and the engine
          // combines the two into a single file — which is the only shape
          // anyone actually wants.
          const wantsAudio = isVideoOnly(entry);
          const audio = wantsAudio ? bestCapturedAudio(streams, entry) : null;
          sendResponse({
            ok: true,
            result: await callChecked("add_pair", {
              url: entry.url,
              audioUrl: audio ? audio.url : "",
              headers: entry.headers || {},
              title: message.title || "",
              filename: message.filename || "",
              cookies: await cookieHeaderFor(entry.url),
              referrer: pageUrl,
              userAgent: navigator.userAgent,
            }),
          });
          break;
        }
        case "canHandle":
          requireDownloadable(message.url);
          sendResponse({ ok: true, result: await callChecked("can_handle", { url: message.url }) });
          break;
        case "getSettings":
          sendResponse({ ok: true, result: await getSettings() });
          break;
        case "saveSettings":
          sendResponse({ ok: true, result: await saveSettings(message.patch || {}) });
          break;
        default:
          sendResponse({ ok: false, error: `unknown message type: ${message.type}` });
      }
    } catch (error) {
      sendResponse({ ok: false, error: String(error.message || error) });
    }
  })();
  return true;      // keep the channel open for the async reply
});
