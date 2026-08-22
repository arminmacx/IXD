/**
 * Tests for the parts of the extension that are pure logic.
 *
 * Run with:  node tests/test_extension.js
 *
 * The proof-of-origin token is read out of the player's own request body, and
 * getting that wrong is silent — an empty token simply means the download
 * stalls a minute in with no explanation. So the parser is exercised against a
 * body of exactly the shape the engine builds.
 */

"use strict";

const fs = require("fs");
const path = require("path");

let passed = 0;
let failed = 0;

//: Checks that finish on a promise rather than in a straight line. The summary
//: is printed from a callback at the end of the file, and without this an
//: asynchronous check reports *after* the totals — where a failure is counted
//: by nobody and the run still exits 0.
const pendingAsync = [];

function check(name, condition, detail) {
  if (condition) {
    passed += 1;
    console.log(`  PASS  ${name}`);
  } else {
    failed += 1;
    console.log(`  FAIL  ${name} ${detail === undefined ? "" : detail}`);
  }
}

// ---------------------------------------------------------------------------
// Load the token-reading helpers out of the service worker. They cannot simply
// be imported: the file is written for a browser, not for Node.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// What the toolbar button hands to the application.
//
// The extension is a relay: it watches, keeps the session that made an address
// work, and passes it on. A manifest is the better thing to hand over than any
// single file, because it describes the whole stream at every quality the site
// publishes — and better than the page, which the extractors may or may not be
// able to read.
// ---------------------------------------------------------------------------
function orderForPopup(streams) {
  return streams.slice().sort((a, b) => {
    if ((a.kind === "manifest") !== (b.kind === "manifest")) {
      return a.kind === "manifest" ? -1 : 1;
    }
    return (b.size || 0) - (a.size || 0);
  });
}

console.log("\n[what is handed over]");
{
  const streams = [
    { kind: "video", name: "trailer.mp4", size: 4_000_000, url: "https://c/trailer.mp4" },
    { kind: "manifest", name: "master.m3u8", size: 0, url: "https://c/master.m3u8" },
    { kind: "audio", name: "theme.mp3", size: 9_000_000, url: "https://c/theme.mp3" },
  ];
  const ordered = orderForPopup(streams);
  check("a manifest is offered first, whatever its size",
    ordered[0].kind === "manifest", ordered.map((e) => e.kind).join(","));
  check("and the rest follow by size",
    ordered[1].name === "theme.mp3" && ordered[2].name === "trailer.mp4",
    ordered.map((e) => e.name).join(","));
  check("nothing is dropped", ordered.length === 3, String(ordered.length));
}

// ---------------------------------------------------------------------------
// Media classification: what makes a request worth offering as a download.
//
// This is what decides whether the extension sees anything at all on a site
// that is not YouTube. It used to watch one CDN, so on every other site the
// captured list was permanently empty — and a modern player leaves nothing in
// the page to find, because it fetches the media by script and hands the
// <video> element a `blob:` URL. The request is the only place it is visible.
// ---------------------------------------------------------------------------
function loadClassifier() {
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "background.js"), "utf8");
  const from = text.indexOf("const MANIFEST_EXTENSIONS");
  const to = text.indexOf("function parseMediaUrl");
  if (from < 0 || to < 0) return null;
  const module = { exports: {} };
  const factory = new Function(
    `${text.slice(from, to)}; return { classifyMediaUrl, fileNameOf };`);
  return factory();
}

const classifier = loadClassifier();
console.log("\n[media classification]");
if (!classifier) {
  check("the classifier could be loaded", false, "markers not found");
} else {
  const { classifyMediaUrl } = classifier;
  const kindOf = (url, type) => {
    const found = classifyMediaUrl(url, type);
    return found ? found.kind : null;
  };

  // A manifest is the prize: one of these is the whole stream at every
  // quality the site publishes.
  check("an HLS playlist is recognised",
    kindOf("https://cdn.example.com/hls/movie/index.m3u8") === "manifest");
  check("a DASH manifest is recognised",
    kindOf("https://cdn.example.com/dash/movie/manifest.mpd") === "manifest");
  check("a playlist with a query string is still a playlist",
    kindOf("https://cdn.example.com/x/master.m3u8?token=abc&e=1699") === "manifest");
  check("an extensionless manifest is recognised by its content type",
    kindOf("https://cdn.example.com/api/manifest?id=42",
      "application/vnd.apple.mpegurl") === "manifest");

  // Whole files.
  check("an mp4 is a video", kindOf("https://cdn.example.com/a/b.mp4") === "video");
  check("an mkv is a video", kindOf("https://cdn.example.com/a/b.mkv") === "video");
  check("an mp3 is audio", kindOf("https://cdn.example.com/a/b.mp3") === "audio");
  check("an opus file is audio", kindOf("https://cdn.example.com/a/b.opus") === "audio");
  check("an extensionless file is recognised by its content type",
    kindOf("https://cdn.example.com/stream/9182", "video/mp4") === "video");

  // Segments must never be offered: a viewer wants the film, not four
  // thousand four-second files. The manifest listing them is captured instead.
  check("an HLS segment is ignored",
    kindOf("https://cdn.example.com/hls/movie/seg-00042.ts") === null);
  check("a DASH segment is ignored",
    kindOf("https://cdn.example.com/dash/movie/chunk-1-00042.m4s") === null);

  // Everything else on a page is not media, and offering it would bury what is.
  check("a script is not media",
    kindOf("https://cdn.example.com/app.js", "application/javascript") === null);
  check("an image is not media",
    kindOf("https://cdn.example.com/poster.jpg", "image/jpeg") === null);
  check("a stylesheet is not media",
    kindOf("https://cdn.example.com/app.css", "text/css") === null);
  check("an API call is not media",
    kindOf("https://api.example.com/v1/user", "application/json") === null);
  check("a blob URL is not something that can be fetched",
    kindOf("blob:https://example.com/9a8b-7c6d") === null);

  // A segment with nothing in its address to recognise. `/seg/1234` served as
  // `video/mp4` used to be offered as a whole film, and a stream is thousands
  // of them — which buries the manifest that is the thing actually worth
  // having.
  check("a transport-stream segment is ignored by its content type",
    kindOf("https://cdn.example.com/live/1234", "video/mp2t") === null);
  check("an extensionless segment is ignored by its path",
    kindOf("https://cdn.example.com/video/segment-000123", "video/mp4") === null);
  check("an initialisation segment is ignored",
    kindOf("https://cdn.example.com/v/init", "video/mp4") === null);
  check("…but a whole file whose name merely contains those letters is not",
    kindOf("https://cdn.example.com/films/chunky-monkey", "video/mp4") === "video");
  check("a Smooth Streaming manifest is recognised",
    kindOf("https://cdn.example.com/movie/Manifest",
      "application/vnd.ms-sstr+xml") === "manifest");

  // The name shown to a person.
  check("a capture is named by its file",
    (classifyMediaUrl("https://cdn.example.com/videos/The%20Film.mp4") || {}).name
      === "The Film.mp4");
}

// ---------------------------------------------------------------------------
// Which tab a request belongs to when it has none of its own.
//
// `tabId` is -1 for anything a service worker fetched, and a great many players
// run their whole transport inside one — manifest and segments alike. Dropping
// those left the capture map permanently empty on exactly the sites that need
// it, while the page was visibly playing. The origin that asked is what
// resolves it.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// The headers the browser actually sent.
//
// A media CDN decides by header, and reconstructing what it wants is guesswork:
// a player may sign its segment requests with an `Authorization` or a bespoke
// `X-…` header that nothing could invent. The browser already sent a set that
// worked, so it is kept with the capture and replayed — which is how a
// commercial download manager succeeds where a reconstructed request is
// refused.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// What the panel does with a captured entry.
//
// A playlist is a *description* of a stream, not the stream. Queueing it as a
// file downloads the playlist text — a couple of hundred bytes that land as a
// video and play nothing, which is exactly what was reported: "the download
// only downloads 257 b".
// ---------------------------------------------------------------------------
function routeFor(entry) {
  return entry.kind === "manifest" ? "addMedia" : "addCaptured";
}

//: Which address the quality menu should be built from. What the player
//: fetched beats what the page can be scraped for: a page carries trailers,
//: previews and advertising alongside its film, and scraping cannot tell them
//: apart — one report was a menu offering "Video" that downloaded 62 KB of
//: something else.
function extractionOrder(streams, pageUrl) {
  const manifest = streams.find((entry) => entry.kind === "manifest");
  return manifest ? [manifest.url, pageUrl] : [pageUrl];
}

// ---------------------------------------------------------------------------
// A sound effect is not a download.
//
// Measured on a real page: YouTube's own interface fetches failure.mp3,
// no_input.mp3, open.mp3 and success.mp3, each a few kilobytes and each
// correctly classified as audio — so the panel offered four beeps beside the
// video the person actually came for.
// ---------------------------------------------------------------------------
function loadPairing() {
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "background.js"), "utf8");
  const helpers = text.slice(text.indexOf("const AUDIO_ITAGS"),
                             text.indexOf("// ------", text.indexOf("function bestCapturedAudio")));
  return new Function(helpers + "; return { isVideoOnly, bestCapturedAudio };")();
}

function loadGenericEntry() {
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "background.js"), "utf8");
  const classifier = text.slice(
    text.indexOf("const MANIFEST_EXTENSIONS"), text.indexOf("function parseMediaUrl"));
  const entry = text.slice(
    text.indexOf("//: Below this, a media file is a sound effect"),
    text.indexOf("try {", text.indexOf("//: Below this, a media file is a sound effect")));
  return new Function(classifier + entry + "; return { genericEntry };")();
}

// ---------------------------------------------------------------------------
// Every map it clears is a map it has.
//
// `badgeDueAt.delete(tabId)` shipped in two handlers with nothing anywhere
// declaring it — a leftover from a badge-timing map that was removed. Chrome
// reported `ReferenceError: badgeDueAt is not defined` on every tab close and
// every navigation, and everything after the throw in those handlers never
// ran: the players map, the frame origins, the badge redraw. A worker that
// throws in a listener looks exactly like one that is merely quiet.
// ---------------------------------------------------------------------------
console.log("\n[every map it clears is a map it has]");
{
  const source = fs.readFileSync(
    path.join(__dirname, "..", "extension", "background.js"), "utf8");
  const declared = new Set();
  for (const match of source.matchAll(
      /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/g)) {
    declared.add(match[1]);
  }
  // Function parameters and named functions count as declared too.
  for (const match of source.matchAll(/function\s+([A-Za-z_$][\w$]*)/g)) {
    declared.add(match[1]);
  }
  const builtins = new Set([
    "chrome", "console", "Map", "Set", "JSON", "Object", "Array", "Promise",
    "URL", "URLSearchParams", "Date", "Math", "String", "Number", "navigator",
    "self", "globalThis", "performance", "crypto", "TextEncoder", "TextDecoder",
    "atob", "btoa", "fetch", "Uint8Array", "ArrayBuffer", "Error", "RegExp",
  ]);
  const used = new Set();
  for (const match of source.matchAll(
      /(?<![.\w$])([A-Za-z_$][\w$]*)\.(?:delete|set|get|has|add|clear)\(/g)) {
    used.add(match[1]);
  }
  const missing = [...used].filter(
    (name) => !declared.has(name) && !builtins.has(name)
      && !/^(?:this|entry|item|message|details|response|result|payload|state|target|node|event|request|window|document|headers|params|options|settings|streams|info|tab|data|map|store|cache|list|found|seen|known|value|record|row|panel|menu|shadow|host|element|box|worker|port|task|group|parsed|url|args|self)$/.test(name),
  );
  check("every map the worker clears is one it declares",
    missing.length === 0, missing.join(", "));
}

console.log("\n[a sound effect is not a download]");
{
  const { genericEntry } = loadGenericEntry();
  check("an interface beep is not offered",
    genericEntry("https://www.youtube.com/s/search/audio/success.mp3",
      "audio/mpeg", "9450") === null);
  check("nor is a tiny sting with no extension",
    genericEntry("https://cdn.example/sfx/1", "audio/mpeg", "20000") === null);
  check("a real audio track is offered",
    (genericEntry("https://cdn.example/album/track.mp3",
      "audio/mpeg", "8400000") || {}).kind === "audio");
  check("a film is offered", (genericEntry("https://cdn.example/film.mp4",
    "video/mp4", "400000000") || {}).kind === "video");
  check("a manifest is offered whatever it weighs, because its own size says "
    + "nothing about the stream it describes",
    (genericEntry("https://cdn.example/master.m3u8",
      "application/vnd.apple.mpegurl", "412") || {}).kind === "manifest");
  check("and a file whose size the origin did not state is still offered",
    (genericEntry("https://cdn.example/film.mp4", "video/mp4", "") || {}).kind
      === "video");
}

// ---------------------------------------------------------------------------
// Two files, one clip.
//
// Reported from Instagram: the panel offered audio and no video at all. The
// URLs in the user's log are the fixture — the CDN publishes picture and sound
// as two `.mp4` addresses, fetched in slices, and nothing in the path or the
// content type separates them. `efg` does: base64 JSON naming the encoding
// (`dash_baseline_1_v1` against `dash_ln_heaac_vbr3_audio`) and the asset both
// halves belong to.
// ---------------------------------------------------------------------------
console.log("\n[two files, one clip]");
{
  const { genericEntry } = loadGenericEntry();
  const efgFor = (tag, asset) => encodeURIComponent(Buffer.from(JSON.stringify({
    vencode_tag: tag, video_id: null, xpv_asset_id: asset,
    client_name: "ig", duration_s: 32,
  })).toString("base64"));
  const address = (name, tag, asset, slice) =>
    `https://scontent-cdg4-1.cdninstagram.com/o1/v/t2/f2/m86/${name}.mp4`
    + `?_nc_cat=111&efg=${efgFor(tag, asset)}&oh=00_AQHicnm1&oe=6A82DE74${slice}`;

  const video = address("AQM-hMPD", "ig-xpvds.clips.c1-C3.dash_baseline_1_v1",
                        1836684161075181, "&bytestart=1009&byteend=1311787");
  const audio = address("AQPBAOhc", "ig-xpvds.clips.c1-C3.dash_ln_heaac_vbr3_audio",
                        1836684161075181, "&bytestart=0&byteend=823");
  const other = address("AQMz0wbD", "ig-xpvds.clips.c2-C3.dash_ln_heaac_vbr3_audio",
                        907553225268869, "&bytestart=205979&byteend=255387");

  const asVideo = genericEntry(video, "video/mp4", "1310779");
  const asAudio = genericEntry(audio, "video/mp4", "824", "bytes 0-823/237996");
  check("the picture half is video", asVideo && asVideo.kind === "video",
    JSON.stringify(asVideo));
  check("and the sound half is audio, though it is also an .mp4 served as "
    + "video/mp4", asAudio && asAudio.kind === "audio", JSON.stringify(asAudio));
  check("its size is the file's, not the 824-byte slice that was asked for",
    asAudio && asAudio.size === 237996, String(asAudio && asAudio.size));
  check("both know which clip they belong to",
    asVideo && asAudio && asVideo.asset === asAudio.asset && asVideo.asset,
    `${asVideo && asVideo.asset} / ${asAudio && asAudio.asset}`);

  // A clip fetched in thirty pieces is one download, not thirty — and the
  // address kept is the whole file rather than the slice.
  const first = genericEntry(video, "video/mp4", "1310779");
  const second = genericEntry(
    video.replace("bytestart=1009&byteend=1311787", "bytestart=1311788&byteend=2805294"),
    "video/mp4", "1493507");
  check("every slice of one address is the same entry",
    first.itag === second.itag, `${first.itag}\n${second.itag}`);
  check("and the address kept is the file, not the slice",
    !/bytestart|byteend/.test(first.url), first.url);

  // The pairing the panel does with them.
  const pairing = loadPairing();
  const streams = [asVideo, asAudio,
    genericEntry(other, "video/mp4", "49408", "bytes 205979-255387/255388")];
  check("a video-only capture is recognised as needing sound",
    pairing.isVideoOnly(asVideo) === true);
  const companion = pairing.bestCapturedAudio(streams, asVideo);
  check("and it is paired with its own clip's audio",
    companion && companion.itag === asAudio.itag,
    companion ? companion.url : "nothing");
  const strayAudio = genericEntry(other, "video/mp4", "49408",
    "bytes 205979-255387/255388");
  check("never with another clip's",
    pairing.bestCapturedAudio([asVideo, strayAudio], asVideo) === null);
}

console.log("\n[what the panel does with what it found]");
{
  check("a captured playlist goes through media extraction",
    routeFor({ kind: "manifest", url: "https://c/master.m3u8" }) === "addMedia");
  check("a captured file is queued as a file",
    routeFor({ kind: "video", url: "https://c/movie.mp4" }) === "addCaptured");
  check("and so is captured audio",
    routeFor({ kind: "audio", url: "https://c/track.m4a" }) === "addCaptured");

  const withManifest = extractionOrder([
    { kind: "video", url: "https://c/ad.mp4" },
    { kind: "manifest", url: "https://c/master.m3u8" },
  ], "https://site/watch");
  check("the playlist that is playing is asked first",
    withManifest[0] === "https://c/master.m3u8", withManifest.join(" "));
  check("and the page remains the fallback",
    withManifest[1] === "https://site/watch");
  check("with nothing captured, the page is all there is",
    extractionOrder([], "https://site/watch").join(",") === "https://site/watch");
}

// ---------------------------------------------------------------------------
// One playlist per host.
//
// A player fetches the master and then the variant it chose, and both are
// captured — so offering each in two containers put *four* rows on screen for
// one video. Reported with a screenshot.
// ---------------------------------------------------------------------------
function loadManifestFilter() {
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "content", "video_inject.js"), "utf8");
  const from = text.indexOf("function oneManifestPerHost");
  const to = text.indexOf("function describeCaptured", from);
  return new Function(text.slice(from, to) + "; return oneManifestPerHost;")();
}

// ---------------------------------------------------------------------------
// The captured playlists belong under a menu that has nothing better to show,
// and nowhere else. On a Twitch VOD they appeared above a six-quality menu,
// twice each, labelled with the size of the playlist text (2.9 KB).
// ---------------------------------------------------------------------------
function loadCaptureRule() {
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "content", "video_inject.js"), "utf8");
  const from = text.indexOf("function capturesWorthShowing");
  const to = text.indexOf("function describeCaptured", from);
  return new Function(text.slice(from, to) + "; return capturesWorthShowing;")();
}

// ---------------------------------------------------------------------------
// A local moved into a helper and left an undeclared identifier behind.
// `node --check` cannot see it — the syntax is perfect — and the expression it
// sits in short-circuits before reaching it in the common case, so the panel
// worked everywhere except the one path the identifier existed for. This is a
// source assertion, not a behavioural one: it pins the shape that broke.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// "There is nothing on this page" is a fact about the page. Remembering it for
// only fifteen seconds meant re-deriving it every time somebody navigated back:
// of 93.2 s spent extracting in one user's log, 40.2 s went on seven pages that
// can never yield media, four of them repeats inside a minute.
// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// While the qualities are being read, the menu says so and shows nothing else.
// The captures used to be listed above the note — the same title six times,
// two of them labelled with the size of a playlist file — and then replaced.
// ---------------------------------------------------------------------------
console.log("\n[nothing but the note while the qualities are read]");
{
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "content", "video_inject.js"), "utf8");
  const start = text.indexOf("function openMenu(");
  const body = text.slice(start, text.indexOf("\n  function ", start + 1));
  const waiting = body.indexOf("state.waiting");
  const capturesDrawn = body.indexOf("capturesWorthShowing(");
  check("openMenu still handles the waiting state", waiting > 0);
  check("and it returns before any capture is drawn",
    waiting > 0 && capturesDrawn > waiting,
    `waiting at ${waiting}, captures at ${capturesDrawn}`);
  check("there is only one waiting branch left",
    (body.match(/state\.waiting/g) || []).length === 1);
}

console.log("\n[a verdict is remembered, a failure to connect is not]");
{
  const worker = fs.readFileSync(
    path.join(__dirname, "..", "extension", "background.js"), "utf8");

  check("the application's own 'no' is marked as a verdict",
    /error\.verdict = true/.test(worker));
  check("and the cache keeps verdicts for as long as successes",
    /const VERDICT_TTL_MS = CACHE_TTL_MS/.test(worker));
  check("while an unreachable application still clears quickly",
    /const FAILURE_TTL_MS = 15 \* 1000/.test(worker));

  // The choice itself: verdict -> long, transport failure -> short.
  const from = worker.indexOf("const hit = extractionCache.get(key);");
  const decision = worker.slice(from, from + 260);
  check("the two are told apart when the cache is read",
    /hit\.verdict \? VERDICT_TTL_MS : FAILURE_TTL_MS/.test(decision), decision);
  check("and the flag is stored with the failure",
    /verdict: Boolean\(error && error\.verdict\)/.test(worker));
}

console.log("\n[the menu is headed by something a person recognises]");
{
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "content", "video_inject.js"), "utf8");
  const from = text.indexOf("function menuHeading");
  const to = text.indexOf("function capturesWorthShowing", from);
  const heading = new Function(
    "tabTitle", "document",
    text.slice(from, to) + "; return menuHeading;");

  const onPage = heading("A Chess Stream", { title: "ignored" });
  check("a manifest filename is replaced by the page's name",
    onPage("1909970769.M3U8") === "A Chess Stream", onPage("1909970769.M3U8"));
  check("and so is any other bare media filename",
    onPage("index-dvr.ts") === "A Chess Stream");
  check("a real title is kept as it is",
    onPage("wao vs koba stream") === "wao vs koba stream");
  check("a title with a dot in it is still a title",
    onPage("Episode 1. The Beginning") === "Episode 1. The Beginning");

  const bare = heading("", { title: "" });
  check("with no page name either, the filename beats nothing",
    bare("1909970769.M3U8") === "1909970769.M3U8");
  check("and with nothing at all there is still a heading",
    bare("") === "Available streams");
}

console.log("\n[every local openMenu reads is declared in openMenu]");
{
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "content", "video_inject.js"), "utf8");
  const start = text.indexOf("function openMenu(");
  // Bounded by the next top-level function in the same scope.
  const end = text.indexOf("\n  function ", start + 1);
  const body = text.slice(start, end);
  check("openMenu was found", start > 0 && end > start);

  // Names that exist only as locals of some *other* function must not be read
  // here. `fromCapture` was exactly this, for two releases.
  for (const name of ["fromCapture", "listedCaptures", "choices", "captured"]) {
    const used = new RegExp(`\\b${name}\\b`).test(body);
    const declared = new RegExp(`(?:const|let|var)\\s+${name}\\b`).test(body)
      || new RegExp(`\\(\\s*${name}\\b`).test(body);
    check(`${name} is declared where it is read`, !used || declared,
      `used=${used} declared=${declared}`);
  }
}

console.log("\n[captures are shown only when the menu needs them]");
{
  const worthShowing = loadCaptureRule();
  const playlist = "https://cdn.example/v/360p30/index-dvr.m3u8";
  const captured = [{ kind: "manifest", url: playlist, size: 2986 }];

  check("nothing extracted — the captures are the only route",
    worthShowing(captured, [], playlist) === true);
  check("no captures at all — nothing to show",
    worthShowing([], [], playlist) === false);

  const realMenu = [
    { height: 1080, description: "1080p60" }, { height: 720, description: "720p60" },
    { height: 360, description: "360p" }, { height: 0, description: "Audio only" },
  ];
  check("a real quality menu stands on its own",
    worthShowing(captured, realMenu, playlist) === false);

  // The case the second clause was written for: a site whose media is only a
  // playlist extracts to one row named after the playlist file.
  const poorMenu = [{ height: 0, description: "master.m3u8" }];
  check("one nameless row still needs the captures beside it",
    worthShowing(captured, poorMenu, playlist) === true);
  // This used to require the menu to have been read out of a capture, and
  // that is what hid Instagram's clip: the extraction runs against the page,
  // scrapes one useless format, and both halves of the video sit in the
  // captures unshown. A poor menu needs them wherever it came from.
  check("and needs them just as much when the menu came from the page",
    worthShowing(captured, poorMenu, "https://example.com/watch") === true);
  // The wall of rows §303 was about is still suppressed, and by the clause
  // that was always doing that work: a menu naming real qualities stands
  // alone whether or not its source was captured.
  check("a real quality menu still suppresses them from a page source",
    worthShowing(captured, realMenu, "https://example.com/watch") === false);

  check("a single row that does name a resolution stands alone",
    worthShowing(captured, [{ height: 720, description: "720p" }], playlist) === false);
  check("and a resolution stated only in words is still a resolution",
    worthShowing(captured, [{ height: 0, description: "1080p60" }], playlist) === false);
}

console.log("\n[a quality row asks for the container it advertises]");
{
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "content", "video_inject.js"), "utf8");
  // The row is labelled "160p · mp4" from `shape`; without passing `shape` on
  // the click the engine named the file after the segments it found and a row
  // marked mp4 produced a `.ts`.
  check("the row hands its container to queue()",
    /__ixdHandler = \(\) => queue\(format\.format_id, shape\)/.test(text));
  check("and queue() forwards it to the application",
    /async function queue\(formatId, container\)/.test(text)
      && /container: container \|\| ""/.test(text));
}

console.log("\n[one playlist per host]");
{
  const oneManifestPerHost = loadManifestFilter();
  const rows = oneManifestPerHost([
    { kind: "manifest", url: "https://cdn.example/a/master.m3u8" },
    { kind: "manifest", url: "https://cdn.example/a/index-f1-v1-a1.m3u8" },
    { kind: "video", url: "https://cdn.example/a/trailer.mp4" },
    { kind: "manifest", url: "https://other.example/b/playlist.m3u8" },
  ]);
  check("the variant beside its own master is dropped",
    rows.filter((r) => r.kind === "manifest").length === 2, String(rows.length));
  check("and the one kept is the first, which is the master",
    rows[0].url.endsWith("/master.m3u8"), rows[0].url);
  check("a second host keeps its own",
    rows.some((r) => r.url.startsWith("https://other.example")));
  check("files are never filtered — they are not playlists",
    rows.some((r) => r.kind === "video"));
}

console.log("\n[the headers the browser sent]");
{
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "background.js"), "utf8");
  const from = text.indexOf("const headersByRequest = new Map();");
  const to = text.indexOf("//: A manifest describes an entire stream", from) >= 0
    ? text.indexOf("//: A manifest describes an entire stream", from)
    : text.indexOf("function classifyMediaUrl", from);
  const mod = new Function(
    text.slice(from, to) +
    "\nreturn { rememberHeaders, headersByRequest, mightBeMedia };",
  )();

  mod.rememberHeaders({
    requestId: "1",
    url: "https://cdn.example.net/hls/master.m3u8",
    requestHeaders: [
      { name: "Referer", value: "https://site.example/watch/1" },
      { name: "Origin", value: "https://site.example" },
      { name: "Authorization", value: "Bearer abc" },
      { name: "X-Playback-Session-Id", value: "9f2c" },
      { name: "Host", value: "cdn.example.net" },
      { name: "Accept-Encoding", value: "gzip" },
      { name: "Range", value: "bytes=0-1023" },
      { name: "Cookie", value: "sid=1" },
    ],
  });
  const kept = mod.headersByRequest.get("1");

  check("the page it came from is kept", kept.Referer === "https://site.example/watch/1");
  check("so is an Origin", kept.Origin === "https://site.example");
  check("a credential nothing could reconstruct is kept",
    kept.Authorization === "Bearer abc");
  check("and a bespoke player header", kept["X-Playback-Session-Id"] === "9f2c");
  check("the connection's own headers are not", kept.Host === undefined
    && kept["Accept-Encoding"] === undefined, JSON.stringify(kept));
  check("nor is a byte range, which the engine decides", kept.Range === undefined);
  check("nor the cookie, which travels scoped by its own route",
    kept.Cookie === undefined);

  // Keeping the headers of every request on a busy page is what made an MV3
  // service worker slow enough that the hover panel sat on "Reading the
  // available qualities" while it worked through a page's scripts and images.
  check("a script's headers are not worth remembering", (() => {
    mod.rememberHeaders({
      requestId: "js",
      url: "https://cdn.example.net/app.bundle.js",
      requestHeaders: [{ name: "Referer", value: "https://site.example/" }],
    });
    return !mod.headersByRequest.has("js");
  })());
  check("nor an image's", !mod.mightBeMedia("https://cdn.example.net/poster.jpg"));
  check("but an extensionless address might be a manifest",
    mod.mightBeMedia("https://cdn.example.net/api/manifest?id=42"));
  check("and a playlist certainly is",
    mod.mightBeMedia("https://cdn.example.net/hls/master.m3u8"));

  check("a request with no headers records nothing", (() => {
    mod.rememberHeaders({ requestId: "2", url: "https://cdn.example.net/a.m3u8" });
    return !mod.headersByRequest.has("2");
  })());
  check("a header with no value is skipped, not stored as undefined", (() => {
    mod.rememberHeaders({
      requestId: "3",
      url: "https://cdn.example.net/a.m3u8",
      requestHeaders: [{ name: "X-A", value: null }, { name: "X-B", value: "b" }],
    });
    const seen = mod.headersByRequest.get("3");
    return !("X-A" in seen) && seen["X-B"] === "b";
  })());
  check("the map is bounded, so a long session cannot grow it forever", (() => {
    for (let i = 0; i < 400; i += 1) {
      mod.rememberHeaders({
        requestId: `bulk-${i}`,
        url: `https://cdn.example.net/${i}.m3u8`,
        requestHeaders: [{ name: "X-N", value: String(i) }],
      });
    }
    return mod.headersByRequest.size <= 300;
  })());
}

console.log("\n[attributing a request with no tab]");
{
  const text = fs.readFileSync(
    path.join(__dirname, "..", "extension", "background.js"), "utf8");
  const from = text.indexOf("const tabForOrigin = new Map();");
  const to = text.indexOf("function rememberMedia", from);
  const attribution = new Function(
    "const CAPTURE_TTL_MS = 60000;\n" +
    text.slice(from, to) +
    "\nreturn { noteFrameOrigin, tabIdFor, forgetTab, tabForOrigin };",
  )();
  const { noteFrameOrigin, tabIdFor, forgetTab, tabForOrigin } = attribution;

  noteFrameOrigin(5, "https://watch.example.com/film/42");
  check("a request that carries its own tab keeps it",
    tabIdFor({ tabId: 5, initiator: "https://elsewhere.example" }) === 5);
  check("a worker's request is attributed by the origin that asked",
    tabIdFor({ tabId: -1, initiator: "https://watch.example.com" }) === 5);
  check("Firefox names the same field differently and is understood too",
    tabIdFor({ tabId: -1, originUrl: "https://watch.example.com/film/42" }) === 5);

  // A player embedded from another host has its own origin, and its worker's
  // requests carry that one rather than the page's — so sub-frames are recorded
  // as well, or an embedded player would go unattributed.
  noteFrameOrigin(5, "https://player.cdn.example/embed/42");
  check("an embedded player's own origin resolves to the page's tab",
    tabIdFor({ tabId: -1, initiator: "https://player.cdn.example" }) === 5);

  check("an origin nobody has loaded resolves to nothing",
    tabIdFor({ tabId: -1, initiator: "https://unknown.example" }) === -1);
  check("a request with neither a tab nor an origin is dropped",
    tabIdFor({ tabId: -1 }) === -1);
  check("a non-http origin is not an origin",
    tabIdFor({ tabId: -1, initiator: "chrome-extension://abc" }) === -1);

  check("an attribution older than the window is dropped", (() => {
    tabForOrigin.set("https://stale.example",
      { tabId: 12, at: Date.now() - 120000 });
    return tabIdFor({ tabId: -1, initiator: "https://stale.example" }) === -1;
  })());

  check("a closed tab takes its origins with it", (() => {
    forgetTab(5);
    return tabIdFor({ tabId: -1, initiator: "https://watch.example.com" }) === -1
      && tabIdFor({ tabId: -1, initiator: "https://player.cdn.example" }) === -1;
  })());
}

const source = fs.readFileSync(
  path.join(__dirname, "..", "extension", "background.js"), "utf8",
);
const start = source.indexOf("const FIELD_STREAMER_CONTEXT");
const end = source.indexOf("function rememberToken");
if (start < 0 || end < 0) {
  console.log("  FAIL  token helpers not found in background.js");
  process.exit(1);
}
global.btoa = (text) => Buffer.from(text, "binary").toString("base64");
const helpers = new Function(
  source.slice(start, end) +
  "\nreturn { extractPoToken, readVarint, protobufFields, base64Url };",
)();

// ---------------------------------------------------------------------------
// protobuf primitives
// ---------------------------------------------------------------------------
console.log("\n[protobuf varints]");
check("single byte", helpers.readVarint(new Uint8Array([1]), 0)[0] === 1);
check("two bytes", helpers.readVarint(new Uint8Array([0xac, 0x02]), 0)[0] === 300);
check("boundary 127", helpers.readVarint(new Uint8Array([0x7f]), 0)[0] === 127);
check("boundary 128", helpers.readVarint(new Uint8Array([0x80, 0x01]), 0)[0] === 128);
// Stream identifiers run well past 2^32, so the reader must not fall back on
// 32-bit bitwise arithmetic the way a naive implementation does.
const large = 1785049552831999;
const encodedLarge = [];
let remaining = large;
while (remaining > 127) {
  encodedLarge.push((remaining % 128) | 0x80);
  remaining = Math.floor(remaining / 128);
}
encodedLarge.push(remaining);
check("value beyond 32 bits",
  helpers.readVarint(new Uint8Array(encodedLarge), 0)[0] === large,
  String(helpers.readVarint(new Uint8Array(encodedLarge), 0)[0]));

// ---------------------------------------------------------------------------
// token extraction
// ---------------------------------------------------------------------------
console.log("\n[proof-of-origin token]");

function varint(value) {
  const out = [];
  while (value > 127) {
    out.push((value & 0x7f) | 0x80);
    value = Math.floor(value / 128);
  }
  out.push(value);
  return out;
}

function lengthDelimited(field, payload) {
  return [...varint((field << 3) | 2), ...varint(payload.length), ...payload];
}

const tokenBytes = [1, 2, 254, 255, ...Buffer.from("PROOF-OF-ORIGIN", "utf8"), 0, 7];
// StreamerContext: client info in field 1, the token in field 2.
const streamer = [
  ...lengthDelimited(1, [...varint((3 << 3) | 0), 5]),
  ...lengthDelimited(2, tokenBytes),
];
const body = new Uint8Array([
  ...lengthDelimited(1, [...varint((55 << 3) | 0), 2]),   // client state
  ...lengthDelimited(5, new Array(40).fill(0)),           // session config
  ...lengthDelimited(19, streamer),
]);

const expected = Buffer.from(tokenBytes).toString("base64")
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
check("token read from the request body",
  helpers.extractPoToken(body) === expected, helpers.extractPoToken(body));

check("a body without a token yields nothing",
  helpers.extractPoToken(new Uint8Array([...lengthDelimited(19, [])])) === "");
check("a body with no streamer context yields nothing",
  helpers.extractPoToken(new Uint8Array(lengthDelimited(1, [8, 1]))) === "");
check("malformed input is not fatal",
  helpers.extractPoToken(new Uint8Array([0xff, 0xff, 0xff, 0xff])) === "");
check("an empty token is not reported as a token",
  helpers.extractPoToken(
    new Uint8Array(lengthDelimited(19, lengthDelimited(2, []))),
  ) === "");

// The token must survive base64url without padding or unsafe characters.
console.log("\n[base64url]");
const encoded = helpers.base64Url(new Uint8Array([251, 255, 190, 0]));
check("uses the URL-safe alphabet", !/[+/=]/.test(encoded), encoded);
check("round-trips through Node",
  Buffer.from(encoded.replace(/-/g, "+").replace(/_/g, "/"), "base64")
    .equals(Buffer.from([251, 255, 190, 0])), encoded);

// ---------------------------------------------------------------------------
// Which captured stream URLs are worth keeping.
//
// This decided nothing for a long time, and did so invisibly: the test was
// `gir=yes`, a parameter present on capped *and* complete URLs alike, so every
// URL the player had already negotiated was thrown away — including the only
// one the CDN serves in full. `ratebypass=yes` is the parameter that actually
// marks a complete URL, and it is the same rule the engine applies.
// ---------------------------------------------------------------------------
console.log("\n[captured stream filter]");
const captureStart = source.indexOf("function parseMediaUrl");
const captureEnd = source.indexOf("// ------", captureStart);
const capture = new Function(
  source.slice(captureStart, captureEnd) + "\nreturn { parseMediaUrl };",
)();

const base = "https://r1---sn-x.googlevideo.com/videoplayback?itag=18&clen=100";
check("ratebypass is recorded when present",
  capture.parseMediaUrl(`${base}&ratebypass=yes&gir=yes`).ratebypass === true);
check("ratebypass is recorded when absent",
  capture.parseMediaUrl(`${base}&gir=yes`).ratebypass === false);
// A captured URL is never discarded for lacking `ratebypass`. That parameter
// describes what an *unattested* session is served, and these URLs come from
// the player's own attested session — which is served past the point our own
// requests are cut off. Filtering them by the unattested rule threw away the
// only URLs worth capturing.
check("a URL without ratebypass is still captured",
  capture.parseMediaUrl(`${base}&gir=yes`) !== null);
check("an adaptive video URL is captured",
  capture.parseMediaUrl(
    "https://r1---sn-x.googlevideo.com/videoplayback?itag=137&clen=999"
    + "&mime=video%2Fmp4",
  ).itag === "137");
check("a URL with no itag is not a stream",
  capture.parseMediaUrl("https://r1---sn-x.googlevideo.com/videoplayback?x=1")
    === null);
check("size and mime are read from the query",
  capture.parseMediaUrl(`${base}&ratebypass=yes&mime=video%2Fmp4`).size === 100);
check("a malformed URL is not fatal",
  capture.parseMediaUrl("not a url") === null);

// ---------------------------------------------------------------------------
// The page's own resource timeline as a second source of captures.
//
// The worker's memory does not survive its own eviction; the page's timeline
// does, because it belongs to the tab. Field logs show a capture recorded and
// the engine told "0 media addresses" forty-nine seconds later, so a source
// with the page's lifetime rather than the worker's is what closes it.
console.log("\n[the page's timeline is a second source]");
{
  const start = source.indexOf("function capturesToSend");
  const end = source.indexOf("function capturedFor");
  const reported = [];
  const scope = new Function(
    "isYouTubeMedia", "parseMediaUrl", "capturesForPage", "report",
    "capturedByTab",
    `${source.slice(start, end)};
     return { capturesToSend, pageTimelineCaptures, mergeCaptureLists };`,
  );

  // The real address shapes, in the form the page timeline hands them over.
  const play = (itag) => `https://rr3---sn-x.googlevideo.com/videoplayback`
    + `?expire=1&itag=${itag}&mime=video%2Fmp4&clen=500&sig=s`;

  const api = scope(
    (url) => /googlevideo\.com/.test(url),
    (raw) => {
      const q = new URL(raw).searchParams;
      return q.get("itag")
        ? { url: raw, itag: q.get("itag"), mime: q.get("mime") || "",
            size: Number(q.get("clen") || 0), at: 1 }
        : null;
    },
    () => [],                       // the worker remembers nothing
    (line) => reported.push(line),
    new Map(),                      // …and holds nothing under any handle
  );

  const fromPage = api.pageTimelineCaptures([
    { url: play("18"), size: 900 },
    { url: "https://www.youtube.com/s/search/audio/open.mp3", size: 10 },
    { url: "not a url" },
  ]);
  check("a media address in the timeline becomes a capture",
    fromPage.length === 1 && fromPage[0].itag === "18",
    JSON.stringify(fromPage.map((e) => e.itag)));
  check("a page's own sound effect is not one",
    !fromPage.some((e) => e.url.includes("search/audio")));
  check("and nothing unparseable gets through",
    fromPage.every((e) => e.url.startsWith("http")));

  // The point of the whole exercise: an empty worker no longer means nothing
  // is sent.
  const sent = api.capturesToSend(7, { url: "https://x/watch?v=a" },
    "https://x/watch?v=a", [{ url: play("18") }]);
  check("with the worker's memory gone, the page's timeline still supplies one",
    sent.length === 1 && sent[0].itag === "18", String(sent.length));
  check("and the log names what each source held",
    reported.some((line) => line.includes("0 remembered by the worker")
      && line.includes("1 in the page's timeline")),
    JSON.stringify(reported));

  // The worker's own record carries the headers the browser sent, and a CDN
  // decides by header — so it must win a tie rather than being replaced.
  const worker = { url: play("18") + "&fromWorker=1", itag: "18",
                   headers: { Referer: "https://x/" }, at: 2 };
  const merged = api.mergeCaptureLists([worker], [{ url: play("18"), itag: "18" }]);
  check("the worker's record wins a tie, because it has the headers",
    merged.length === 1 && merged[0].headers, JSON.stringify(merged));
  const both = api.mergeCaptureLists([worker],
    [{ url: play("140"), itag: "140" }]);
  check("and a stream only the page saw is added, not dropped",
    both.length === 2, String(both.length));
}

// Fetching on the application's behalf.
//
// Measured 2026-08-12: one address, one machine, one second — the application
// 403, `curl` 403 with and without browser headers, and Chrome served the
// addresses its own player minted. So when the application is refused it says
// so in its reply and the extension makes the request instead.
console.log("\n[fetching on the application's behalf]");
{
  const start = source.indexOf("const BROWSER_FETCH_BLOCK");
  const end = source.indexOf("//: Whether two addresses are the same page");
  const calls = [];
  const payload = new Uint8Array(1024 * 1024 + 7);
  for (let i = 0; i < payload.length; i += 1) payload[i] = i % 251;

  const api = new Function("callChecked", "report", "fetch", "btoa",
    `${source.slice(start, end)};
     return { fetchForApplication, base64OfBytes };`,
  )(
    async (name, params) => {
      calls.push({ name, params });
      if (name === "browser_stream_begin") return { id: 7 };
      if (name === "browser_stream_end") return { path: "/out/f.mp4" };
      return { received: 0 };
    },
    () => {},
    async () => ({
      ok: true,
      status: 200,
      body: {
        getReader() {
          // Delivered in small pieces, as a real network does.
          let at = 0;
          return {
            read: async () => {
              if (at >= payload.length) return { done: true };
              const piece = payload.subarray(at, at + 60_000);
              at += piece.length;
              return { done: false, value: piece };
            },
          };
        },
      },
    }),
    (binary) => Buffer.from(binary, "binary").toString("base64"),
  );

  check("a megabyte encodes without throwing",
    api.base64OfBytes(payload).length > 0);
  check("and round-trips exactly",
    Buffer.from(api.base64OfBytes(payload), "base64").equals(
      Buffer.from(payload)));

  pendingAsync.push((async () => {
    const finished = await api.fetchForApplication({
      url: "https://cdn/v?itag=18", title: "A Film", filename: "A Film.mp4",
    }, -1);
    const chunks = calls.filter((c) => c.name === "browser_stream_chunk");
    const rebuilt = Buffer.concat(
      chunks.map((c) => Buffer.from(c.params.data, "base64")));
    check("the transfer is opened before any bytes are sent",
      calls[0].name === "browser_stream_begin", calls[0].name);
    check("every byte the browser read reaches the application",
      rebuilt.equals(Buffer.from(payload)),
      `${rebuilt.length} of ${payload.length}`);
    check("…in whole blocks rather than one message per piece",
      chunks.length > 1 && chunks.length < 20, String(chunks.length));
    check("and the transfer is closed as a success",
      calls[calls.length - 1].name === "browser_stream_end"
      && calls[calls.length - 1].params.ok === true);
    check("the application's own path is what comes back",
      finished.path === "/out/f.mp4");
  })());

  // A browser that is itself refused must close the transfer, not leave a
  // half-written file behind.
  pendingAsync.push((async () => {
    const said = [];
    const failing = new Function("callChecked", "report", "fetch", "btoa",
      `${source.slice(start, end)}; return { fetchForApplication };`,
    )(
      async (name, params) => {
        said.push({ name, params });
        return name === "browser_stream_begin" ? { id: 9 } : {};
      },
      () => {},
      async () => ({ ok: false, status: 403 }),
      (b) => Buffer.from(b, "binary").toString("base64"),
    );
    try {
      await failing.fetchForApplication({ url: "https://cdn/x" }, -1);
      check("a refused browser fetch is reported, not swallowed", false,
        "it resolved");
    } catch (error) {
      check("a refused browser fetch is reported, not swallowed",
        String(error).includes("403"), String(error));
    }
    const last = said[said.length - 1];
    check("and the half-open transfer is closed as a failure",
      last.name === "browser_stream_end" && last.params.ok === false,
      JSON.stringify(last));
  })());
}

// The page fetches, not the worker.
//
// A request this worker makes carries the extension's origin and no referrer;
// the page's carries the origin, referrer and cookies the player's own request
// carried, and a media CDN decides on exactly those. The field log of
// 2026-08-12 caught the cost: the worker fetched, and "the browser was refused
// too: HTTP 403" — the same address the page is served without complaint.
console.log("\n[the page is what fetches]");
{
  const start = source.indexOf("const BROWSER_FETCH_BLOCK");
  const end = source.indexOf("//: Whether two addresses are the same page");
  const payload = Buffer.from("the page read these bytes".repeat(4000));

  // A tab that answers, and streams what it read back through the worker's
  // own message handling.
  const build = (tabAnswers) => {
    const calls = [];
    const sent = [];
    let inbox = null;
    const chrome = {
      runtime: { lastError: null },
      tabs: {
        sendMessage(tabId, message, options, callback) {
          if (!tabAnswers) {
            chrome.runtime.lastError = { message: "no content script" };
            callback(undefined);
            chrome.runtime.lastError = null;
            return;
          }
          inbox = message;
          callback({ ok: true, started: true });
        },
      },
    };
    const api = new Function("callChecked", "report", "fetch", "btoa",
      "chrome",
      `${source.slice(start, end)};
       return { fetchForApplication, pageFetches };`,
    )(
      async (name, params) => {
        calls.push({ name, params });
        if (name === "browser_stream_begin") return { id: 11 };
        if (name === "browser_stream_end") return { path: "/out/page.mp4" };
        return {};
      },
      (line) => sent.push(line),
      async () => {
        // Only the fallback reaches this, and it is refused — so a test that
        // passes proves the page's bytes were used, not the worker's.
        return { ok: false, status: 403 };
      },
      (binary) => Buffer.from(binary, "binary").toString("base64"),
      chrome,
    );
    return { api, calls, sent, chrome, take: () => inbox };
  };

  pendingAsync.push((async () => {
    const world = build(true);
    const transfer = world.api.fetchForApplication({
      url: "https://cdn/v?itag=137", title: "A Film",
    }, 42);

    // Let the request reach the tab, then play the page's part: two blocks and
    // a close, exactly as the content script sends them.
    await new Promise((resolve) => setImmediate(resolve));
    const asked = world.take();
    check("the page is asked to fetch, and for the same address",
      Boolean(asked) && asked.type === "ixdFetch"
      && asked.url === "https://cdn/v?itag=137",
      JSON.stringify(asked));

    const state = world.api.pageFetches.get(asked.id);
    check("and the worker is holding the transfer open for it",
      Boolean(state));
    await state.onBlock(payload.subarray(0, 1000));
    await state.onBlock(payload.subarray(1000));
    state.finish(null);

    const finished = await transfer;
    const chunks = world.calls.filter((c) => c.name === "browser_stream_chunk");
    const rebuilt = Buffer.concat(
      chunks.map((c) => Buffer.from(c.params.data, "base64")));
    check("every byte the page read reaches the application",
      rebuilt.equals(payload), `${rebuilt.length} of ${payload.length}`);
    check("the transfer is closed as a success",
      world.calls[world.calls.length - 1].name === "browser_stream_end"
      && world.calls[world.calls.length - 1].params.ok === true);
    check("and the application's own path comes back",
      finished.path === "/out/page.mp4");
  })());

  // A page this extension does not run in must not take the transfer down
  // with it: the worker's own request is worse, and it is still a request.
  pendingAsync.push((async () => {
    const world = build(false);
    try {
      await world.api.fetchForApplication({ url: "https://cdn/x" }, 42);
      check("a page that cannot fetch falls back to the worker", false,
        "it resolved, and the worker's fetch is a 403 here");
    } catch (error) {
      check("a page that cannot fetch falls back to the worker",
        String(error).includes("403"), String(error));
    }
    check("…and both refusals are named, not one silent one",
      world.sent.some((line) => line.includes("asking the page to fetch it"))
      && world.sent.some((line) => line.includes("could not fetch it either")),
      JSON.stringify(world.sent));
  })());
}

// The player's own address wins over the one extraction produced.
//
// Measured 2026-08-12: an address straight from extraction is 403 to the
// application, to `curl` and to Chrome itself, because YouTube's `n` parameter
// arrives obfuscated and is transformed by the player before use. The player's
// address for the same rendition plays. A field log caught the cost of not
// preferring it: the application delegated its own dead address and the
// browser was refused too.
console.log("\n[the player's own address is preferred]");
{
  const start = source.indexOf("const BROWSER_FETCH_BLOCK");
  const end = source.indexOf("//: Whether two addresses are the same page");
  const asked = [];
  const api = new Function("callChecked", "report", "fetch", "btoa",
    "capturedFor",
    `${source.slice(start, end)}; return { fetchForApplication };`,
  )(
    async (name) => (name === "browser_stream_begin" ? { id: 1 } : {}),
    () => {},
    async (u) => { asked.push(u); return { ok: false, status: 404 }; },
    (b) => Buffer.from(b, "binary").toString("base64"),
    () => [{ itag: "18", url: "https://player-minted/live?itag=18&n=solved" }],
  );

  pendingAsync.push((async () => {
    await api.fetchForApplication(
      { url: "https://extraction/dead?itag=18&n=raw", itag: "18" }, 3,
    ).catch(() => {});
    check("the address the player minted is the one fetched",
      asked[0] === "https://player-minted/live?itag=18&n=solved", asked[0]);

    asked.length = 0;
    await api.fetchForApplication(
      { url: "https://extraction/only?itag=99", itag: "99" }, 3,
    ).catch(() => {});
    check("and with no capture for that rendition, ours is used",
      asked[0] === "https://extraction/only?itag=99", asked[0]);
  })());
}

// A capture belongs to a page, not to a numeric tab handle.
//
// From the field log that finally made this visible:
//
//   12:37:00  captured media: …&itag=18…
//   12:37:21  captures for tab 793036171: 0 remembered by the worker, 0 in the
//             page's timeline, 0 sent
//
// The worker recorded a capture and, twenty-one seconds later, found none for
// the tab that asked. So the recording key and the lookup key were not the
// same, and a lookup that only knows a tab number cannot recover from that.
console.log("\n[a capture belongs to a page, not a tab number]");
{
  const start = source.indexOf("function samePage");
  const end = source.indexOf("function capturedFor");
  const reported = [];
  const captured = new Map();
  const scope = new Function(
    "capturedByTab", "capturedFor", "report",
    "isYouTubeMedia", "parseMediaUrl",
    `${source.slice(start, end)}; return { capturesForPage, capturesToSend };`,
  )(
    captured,
    (tabId) => [...(captured.get(tabId) || new Map()).values()],
    (line) => reported.push(line),
    () => false,
    () => null,
  );

  const page = "https://www.youtube.com/watch?v=bQPgzJJLbfA";
  const entry = { itag: "18", url: "https://cdn/v?itag=18", page, at: 1 };
  // Filed under one handle; asked for under another — the shape of the log.
  captured.set(11, new Map([["18", entry]]));

  const found = scope.capturesForPage(793036171, { url: page }, page);
  check("a capture filed under another handle is still found for the page",
    found.length === 1 && found[0].itag === "18", String(found.length));
  check("and the log says that is what happened",
    reported.some((line) => line.includes("another tab handle")),
    JSON.stringify(reported));

  // But only for the *same* page: this is the guard that stops the wrong
  // video being downloaded under the right title.
  const other = scope.capturesForPage(
    793036171, { url: "https://www.youtube.com/watch?v=OTHER" },
    "https://www.youtube.com/watch?v=OTHER");
  check("a capture from a different video is never borrowed",
    other.length === 0, String(other.length));

  // The tab's own captures still win, and no borrowing happens when it has any.
  captured.set(793036171, new Map([["140",
    { itag: "140", url: "https://cdn/a?itag=140", page, at: 2 }]]));
  const own = scope.capturesForPage(793036171, { url: page }, page);
  check("a tab holding its own captures uses those",
    own.length === 1 && own[0].itag === "140", JSON.stringify(own));
}

// Pressing Radio must not throw the video's captures away.
//
// A player's site is a single-page application: `watch?v=X` becomes
// `watch?v=X&list=RD…&start_radio=1` with no navigation at all. The handler
// cleared everything on any URL change, so the captures for a video that had
// not changed were discarded seconds before the download was clicked.
console.log("\n[a same-page URL change keeps its captures]");
{
  const start = source.indexOf("function samePage");
  const end = source.indexOf("function capturesForPage");
  const samePage = new Function(`${source.slice(start, end)}; return samePage;`)();

  check("adding a radio playlist is the same page",
    samePage("https://www.youtube.com/watch?v=bQPgzJJLbfA",
      "https://www.youtube.com/watch?v=bQPgzJJLbfA&list=RDbQPgzJJLbfA"
      + "&start_radio=1") === true);
  check("and so is picking up a timestamp",
    samePage("https://www.youtube.com/watch?v=X",
      "https://www.youtube.com/watch?v=X&t=42s") === true);
  check("while the next video is not",
    samePage("https://www.youtube.com/watch?v=X",
      "https://www.youtube.com/watch?v=Y") === false);

  // And the handler must actually consult it rather than clearing outright.
  check("the navigation handler compares before it clears",
    source.includes("if (changes.url && !samePage(wasOn, changes.url))"));
}

// A service worker is terminated after about thirty seconds of inactivity, and
// every map in background.js is ordinary memory. From a field log: the player
// fetched a progressive stream at 11:57:14 and it was recorded; the download
// was clicked at 12:09:57 and the engine reported "the browser sent 0 media
// address(es) for this page". The TTL is thirty minutes and the tab was never
// navigated — the worker had simply been recycled.
//
// This is why the rescue fired in every test and never in the field: a test
// clicks within seconds of playing, and a person does not.
console.log("\n[captures survive a worker restart]");
{
  // One in-memory store standing in for chrome.storage.session, shared across
  // two "worker lifetimes" exactly as the real one is.
  const sessionStore = {};
  const chromeMock = {
    storage: {
      session: {
        get: async (key) => (key in sessionStore
          ? { [key]: sessionStore[key] } : {}),
        set: async (bag) => Object.assign(sessionStore, bag),
      },
    },
  };

  const start = source.indexOf("const CAPTURE_STORE");
  const end = source.indexOf("function isYouTubeMedia");
  const body = source.slice(start, end);

  // A whole worker lifetime: fresh maps, the shared session store.
  // `capturedFor` lives further down the file and only applies the TTL, so the
  // map is read directly here — what is under test is what survives, not how
  // it is filtered afterwards.
  function bootWorker() {
    const capturedByTab = new Map();
    const worker = new Function("chrome", "capturedByTab", "setTimeout",
      "tabUrlById", `
      ${body}
      return { remember, ensureCapturesRestored, persistCaptures,
               serialiseCaptures };
    `)(chromeMock, capturedByTab, (fn) => fn(), new Map());
    worker.held = (tabId) => [...(capturedByTab.get(tabId) || new Map()).values()];
    return worker;
  }

  const first = bootWorker();
  first.remember(7, { itag: "18", url: "https://cdn/v?itag=18",
                      mime: "video/mp4", size: 5, at: Date.now() });
  first.remember(7, { itag: "140", url: "https://cdn/a?itag=140",
                      mime: "audio/mp4", size: 2, at: Date.now() });
  check("a capture is written to session storage",
    Boolean(sessionStore.captures && sessionStore.captures["7"]),
    JSON.stringify(Object.keys(sessionStore)));

  // The worker dies here. Everything above was memory; only the store remains.
  const second = bootWorker();
  check("a fresh worker starts with nothing", second.held(7).length === 0);

  pendingAsync.push((async () => {
    await second.ensureCapturesRestored();
    const back = second.held(7);
    check("and has them back once the store is read", back.length === 2,
      String(back.length));
    check("with the addresses intact",
      back.some((e) => e.itag === "18") && back.some((e) => e.itag === "140"),
      JSON.stringify(back.map((e) => e.itag)));

    // A capture made *while* the restore is in flight is the newer of the two
    // and must not be overwritten by the stored copy.
    const third = bootWorker();
    third.remember(7, { itag: "18", url: "https://cdn/NEWER?itag=18",
                        mime: "video/mp4", size: 9, at: Date.now() + 10_000 });
    await third.ensureCapturesRestored();
    const merged = third.held(7).find((e) => e.itag === "18");
    check("a capture newer than the stored one survives the restore",
      merged.url.includes("NEWER"), merged.url);
  })());
}

// Captures belong to a page, and a player's site is a single-page application.
//
// The engine now consults captures *before* asking the site anything, so one
// from the wrong page is no longer a missed opportunity — it is a download of
// the wrong video under the right title, which nobody discovers until they
// watch the file. Moving between videos changes only the query string, so an
// origin-and-path comparison would call two different videos the same page.
console.log("\n[captures belong to one page]");
{
  const start = source.indexOf("function samePage");
  const end = source.indexOf("function capturedFor");
  const samePage = new Function(
    `${source.slice(start, end)}; return samePage;`,
  )();

  check("the same video is the same page",
    samePage("https://www.youtube.com/watch?v=abc",
      "https://www.youtube.com/watch?v=abc&t=30") === true);
  check("a different video on the same path is not",
    samePage("https://www.youtube.com/watch?v=abc",
      "https://www.youtube.com/watch?v=xyz") === false);
  check("nor is a different site",
    samePage("https://www.youtube.com/watch?v=abc",
      "https://example.com/watch?v=abc") === false);
  check("nor a different path",
    samePage("https://www.youtube.com/watch?v=abc",
      "https://www.youtube.com/embed?v=abc") === false);
  check("an unparseable address falls back to comparing the text",
    samePage("not a url", "not a url") === true);
}

// Pairing a captured video with the audio the player already loaded.
//
// An adaptive stream is video *or* audio. Sending one on its own produced a
// silent film, which is the whole complaint. The companion has to share the
// video's container: an MP4 video and a WebM/Opus track cannot become one
// file, so a lower-bitrate AAC track is the better partner.
// ---------------------------------------------------------------------------
console.log("\n[captured pairing]");
const pairStart = source.indexOf("const AUDIO_ITAGS");
const pairEnd = source.indexOf("function capturedFor");
const pair = new Function(
  source.slice(pairStart, pairEnd) +
  "\nreturn { isAudioStream, isVideoOnly, bestCapturedAudio };",
)();

check("an adaptive audio itag is audio",
  pair.isAudioStream({ itag: "140", mime: "" }) === true);
check("an adaptive video itag is not audio",
  pair.isAudioStream({ itag: "137", mime: "" }) === false);
check("a declared mime wins over the itag table",
  pair.isAudioStream({ itag: "999", mime: "audio/webm" }) === true);
check("a progressive itag needs no companion",
  pair.isVideoOnly({ itag: "18", mime: "" }) === false);
check("an adaptive video itag needs a companion",
  pair.isVideoOnly({ itag: "137", mime: "" }) === true);

const pool = [
  { itag: "137", mime: "video/mp4", size: 900, url: "v" },
  { itag: "251", mime: "audio/webm", size: 800, url: "a-webm" },
  { itag: "140", mime: "audio/mp4", size: 500, url: "a-m4a" },
];
check("an mp4 video takes the mp4 audio, not the larger webm one",
  pair.bestCapturedAudio(pool, pool[0]).url === "a-m4a");
check("a webm video takes the webm audio",
  pair.bestCapturedAudio(pool, { itag: "248", mime: "video/webm" }).url
    === "a-webm");
check("no audio captured yields no companion",
  pair.bestCapturedAudio([pool[0]], pool[0]) === null);

// ---------------------------------------------------------------------------
// The visitor identity a proof of origin belongs to.
//
// A token is minted for one identity and is meaningless under another, so the
// two are captured together. Forwarding the token alone made the engine
// present its own identity while holding the browser's proof — which fails in
// the quietest way available: the server ignores the proof and the stream
// stops exactly where it would have stopped regardless.
// ---------------------------------------------------------------------------
console.log("\n[visitor identity]");
const visitorStart = source.indexOf("const visitorByTab");
const visitorEnd = source.indexOf("//: Field numbers", visitorStart);
const visitor = new Function(
  "const CAPTURE_TTL_MS = 60000;\n" +
  source.slice(visitorStart, visitorEnd) +
  "\nreturn { rememberVisitor, visitorFor, visitorByTab };",
)();

visitor.rememberVisitor({
  tabId: 7,
  requestHeaders: [{ name: "X-Goog-Visitor-Id", value: "CgtWSVNJVE9S" }],
});
check("the identity is read from the request header",
  visitor.visitorFor(7) === "CgtWSVNJVE9S");
check("a header name in any case is recognised", (() => {
  visitor.rememberVisitor({
    tabId: 8,
    requestHeaders: [{ name: "x-goog-visitor-id", value: "lower" }],
  });
  return visitor.visitorFor(8) === "lower";
})());
check("an unknown tab has no identity", visitor.visitorFor(99) === "");
check("a request without the header leaves nothing", (() => {
  visitor.rememberVisitor({ tabId: 9, requestHeaders: [{ name: "Accept", value: "*/*" }] });
  return visitor.visitorFor(9) === "";
})());
check("an identity older than the window is dropped", (() => {
  visitor.visitorByTab.set(10, { visitor: "stale", at: Date.now() - 120000 });
  return visitor.visitorFor(10) === "";
})());
check("a request with no tab is ignored", (() => {
  visitor.rememberVisitor({
    tabId: -1,
    requestHeaders: [{ name: "X-Goog-Visitor-Id", value: "nope" }],
  });
  return visitor.visitorFor(-1) === "";
})());


// ---------------------------------------------------------------------------
// The panel, booted and clicked.
//
// Everything above tests logic in isolation, and none of it could see the two
// defects that made the panel useless on every site for four sessions: a
// `restoreLabel()` that called itself — a stack overflow on the click path,
// which left the menu on "Reading the available qualities…" for ever — and
// capture-phase `stopPropagation` listeners that ended the event before it
// reached the panel's own handler.
//
// Both are invisible to a unit test and obvious to a click. So the script is
// booted against a DOM stub and clicked.
// ---------------------------------------------------------------------------
function stubElement(tag) {
  const el = {
    tagName: (tag || "div").toUpperCase(),
    children: [], parentElement: null, isConnected: true,
    listeners: {}, style: { cssText: "" }, _classes: new Set(),
    shadowRoot: null, innerHTML: "", textContent: "", id: "",
    offsetWidth: 128, offsetHeight: 30,
  };
  el.classList = {
    add: (...c) => c.forEach((x) => el._classes.add(x)),
    remove: (...c) => c.forEach((x) => el._classes.delete(x)),
    contains: (c) => el._classes.has(c),
    toggle: (c, on) => (on ? el._classes.add(c) : el._classes.delete(c)),
  };
  Object.defineProperty(el, "className", {
    get: () => [...el._classes].join(" "),
    set: (v) => { el._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  el.addEventListener = (kind, fn, capture) => {
    (el.listeners[kind] = el.listeners[kind] || []).push(
      { fn, capture: Boolean(capture) });
  };
  el.removeEventListener = () => {};
  el.appendChild = (child) => {
    child.parentElement = el; el.children.push(child); return child;
  };
  el.append = (...kids) => kids.forEach(el.appendChild);
  el.remove = () => {};
  el.querySelector = () => null;
  el.querySelectorAll = () => [];
  el.getBoundingClientRect = () => (
    { width: 640, height: 360, top: 40, left: 0, right: 640, bottom: 400 });
  el.matches = () => false;
  el.attachShadow = () => { el.shadowRoot = stubElement("shadow"); return el.shadowRoot; };
  Object.defineProperty(el, "lastElementChild",
    { get: () => el.children[el.children.length - 1] });
  return el;
}

function bootPanel() {
  const vm = require("vm");
  const source = fs.readFileSync(
    path.join(__dirname, "..", "extension", "content", "video_inject.js"), "utf8");
  const html = stubElement("html");
  const body = stubElement("body");
  html.appendChild(body);
  const sent = [];
  const sandbox = {
    document: {
      documentElement: html, body, readyState: "complete", hidden: false,
      addEventListener: () => {}, removeEventListener: () => {},
      createElement: stubElement, querySelectorAll: () => [], title: "A Film",
      getElementById: (id) => (id === "ixd-overlay-root"
        ? html.children.find((c) => c.id === id) || null : null),
    },
    window: {
      listeners: {},
      addEventListener(kind, fn, capture) {
        (this.listeners[kind] = this.listeners[kind] || []).push(
          { fn, capture: Boolean(capture) });
      },
      removeEventListener: () => {},
      innerWidth: 1280, innerHeight: 800,
      getComputedStyle: () => (
        { visibility: "visible", display: "block", opacity: "1" }),
    },
    location: {
      href: "https://site.example/watch/1",
      hostname: "site.example", pathname: "/watch/1",
    },
    chrome: {
      runtime: {
        lastError: null,
        sendMessage: (message, callback) => {
          sent.push(message);
          if (callback) setTimeout(() => callback({ ok: true, result: {} }), 0);
        },
        onMessage: { addListener: () => {} },
      },
    },
    setTimeout, clearTimeout, setInterval: () => 0, clearInterval: () => {},
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    MutationObserver: function MutationObserver() {
      this.observe = () => {}; this.disconnect = () => {};
    },
    console,
  };
  sandbox.window.top = sandbox.window;
  sandbox.getComputedStyle = sandbox.window.getComputedStyle;
  const failures = [];
  sandbox.__panelError = (error) => failures.push(error);
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox);
  return { html, sent, failures, sandbox };
}

function panelChecks(done) {
  console.log("\n[the panel, booted and clicked]");
  const { html, sent, failures, sandbox } = bootPanel();
  process.on("uncaughtException", (error) => failures.push(error));
  setTimeout(() => {
    const host = html.children.find((c) => c.id === "ixd-overlay-root");
    check("the overlay is attached to <html>, not <body>", Boolean(host));
    if (!host) return done();
    // A transform on the body would make it the containing block for a fixed
    // panel, capping the stacking context however large the z-index is.
    check("the host carries the stacking order, not just the panel",
      host.style.cssText.includes("z-index:2147483647"), host.style.cssText);

    const shadow = host.shadowRoot;
    const panel = shadow.children.find((c) => c.classList.contains("panel"));
    const menu = shadow.children.find((c) => c.classList.contains("menu"));
    check("there is a panel and a menu", Boolean(panel && menu));
    if (!panel || !menu) return done();

    const capturing = (element) => Object.entries(element.listeners)
      .filter(([, l]) => l.some((e) => e.capture)).map(([k]) => k);
    check("nothing on the panel listens in the capture phase",
      capturing(panel).length === 0, capturing(panel).join(","));
    check("nor on the menu", capturing(menu).length === 0,
      capturing(menu).join(","));

    // Clicks are delivered by one listener registered on `window` before the
    // page exists: stopping the event also stops it reaching our own
    // listeners, so the guard and the dispatcher have to be the same thing.
    const guards = (sandbox.window.listeners.click || []).filter((e) => e.capture);
    check("the click guard is registered on window, in the capture phase",
      guards.length === 1, String(guards.length));
    check("and the panel carries the handler it dispatches to",
      typeof panel.__ixdHandler === "function");

    let stopped = false;
    const before = sent.length;
    const event = {
      type: "click",
      preventDefault() {}, stopPropagation() {},
      stopImmediatePropagation() { stopped = true; },
      composedPath: () => [panel, host],
    };
    // Caught rather than allowed to escape: a stack overflow on this path took
    // the whole suite down with it, which reads as a crash rather than as the
    // failing check it is.
    let thrown = null;
    try {
      guards.forEach((entry) => entry.fn(event));
    } catch (error) {
      thrown = error;
    }
    setTimeout(() => {
      check("clicking the panel throws nothing",
        thrown === null, thrown && String(thrown));
      check("the click is kept from the page", stopped);
      check("and clicking the panel asks the application",
        sent.length > before, sent.map((m) => m.type).join(","));
      check("the click path runs to the end without throwing",
        sent.some((m) => m.type === "extract") && failures.length === 0,
        failures.length ? String(failures[0]) : sent.map((m) => m.type).join(","));
      done();
    }, 30);
  }, 30);
}

// ---------------------------------------------------------------------------
// The native port is a process.
//
// Reported from Windows: the application was quit and `ixd.exe` stayed in the
// task list until the extension was removed from the browser. That process was
// the native host, held open by a port this worker never closed.
// ---------------------------------------------------------------------------
console.log("\n[the native host is let go when there is nothing to relay]");
pendingAsync.push((async () => {
  const from = source.indexOf("let port = null;");
  const to = source.indexOf("async function callChecked");
  if (from < 0 || to < 0) {
    check("the transport block was found in background.js", false);
    return;
  }

  // Timers are supplied rather than waited on: the idle release is five
  // seconds, and a suite that sleeps through it is a suite nobody runs.
  const timers = new Map();
  let nextTimerId = 1;
  const setTimeoutStub = (fn, ms) => {
    const id = nextTimerId++;
    timers.set(id, { fn, ms });
    return id;
  };
  const clearTimeoutStub = (id) => { timers.delete(id); };
  const fireIdleTimers = () => {
    for (const [id, entry] of [...timers]) {
      if (entry.ms === 5000) { timers.delete(id); entry.fn(); }
    }
  };

  let spawned = 0;
  let disconnected = 0;
  let live = null;
  const chromeStub = {
    runtime: {
      lastError: null,
      connectNative() {
        spawned += 1;
        const posted = [];
        const onMessage = [];
        const onDisconnect = [];
        live = {
          posted,
          postMessage: (message) => posted.push(message),
          disconnect() { disconnected += 1; onDisconnect.forEach((fn) => fn()); },
          onMessage: { addListener: (fn) => onMessage.push(fn) },
          onDisconnect: { addListener: (fn) => onDisconnect.push(fn) },
          reply: (message) => onMessage.forEach((fn) => fn(message)),
        };
        return live;
      },
    },
  };

  const transport = new Function(
    "chrome", "HOST_NAME", "CALL_TIMEOUT_MS", "setTimeout", "clearTimeout",
    `${source.slice(from, to)};
     return { call, releasePort, isOpen: () => port !== null };`,
  )(chromeStub, "com.ixd.downloader", 30000, setTimeoutStub, clearTimeoutStub);

  // One call, answered.
  const first = transport.call("stats");
  check("a call spawns the host", spawned === 1 && transport.isOpen());
  live.reply({ id: live.posted[0].id, ok: true, result: {} });
  await first;
  check("and it is still connected while the answer is fresh",
    transport.isOpen() && disconnected === 0);

  // Quiet.
  fireIdleTimers();
  check("going quiet ends the host process",
    !transport.isOpen() && disconnected === 1);

  // And it comes back on its own.
  const second = transport.call("stats");
  check("the next call spawns it again, transparently", spawned === 2);
  live.reply({ id: live.posted[0].id, ok: false, error: "not running",
    not_running: true });
  const answer = await second;
  check("a 'not running' answer reaches the caller",
    answer && answer.not_running === true);
  check("and releases the host at once, without waiting for the idle timer",
    !transport.isOpen() && disconnected === 2);

  // While it is known down, the extension's own polling costs no process.
  const passive = await transport.call("extract", { url: "https://e/v" });
  check("a poll while the application is down starts nothing",
    spawned === 2 && passive.not_running === true);

  // What the user asked for still starts it — that is the whole point.
  transport.call("add", { url: "https://e/f.zip" });
  check("but a download the user asked for does start it", spawned === 3);
  live.reply({ id: live.posted[0].id, ok: true, result: { id: 1 } });
  fireIdleTimers();

  transport.call("extract", { url: "https://e/v", user_initiated: true });
  check("and so does a click on the panel", spawned === 4);
})());

// ---------------------------------------------------------------------------
// The panel, moved and closed.
//
// The report: on every page with a video the panel is simply there, over
// whatever it is over, with no way to move it and no way to say "not here".
// Both are pointer work, so both are driven here the way a pointer would drive
// them — through the same window guard the page's clicks go through, because
// that guard is what an event inside the overlay actually reaches first.
// ---------------------------------------------------------------------------
function moveAndCloseChecks(done) {
  console.log("\n[the panel, moved and closed]");
  const { html, sent, sandbox } = bootPanel();
  setTimeout(() => {
    const host = html.children.find((c) => c.id === "ixd-overlay-root");
    const shadow = host && host.shadowRoot;
    const panel = shadow && shadow.children.find((c) => c.classList.contains("panel"));
    if (!panel) {
      check("there is a panel to move", false);
      return done();
    }
    const closeButton = panel.children.find((c) => c.classList.contains("close"));
    check("the panel carries a close button", Boolean(closeButton));
    if (!closeButton) return done();
    check("and it is inside the panel, not loose in the shadow root",
      !shadow.children.includes(closeButton));
    check("the × answers a click of its own",
      Boolean(closeButton.__ixdEvents)
      && typeof closeButton.__ixdEvents.click === "function");
    // Named for pointerdown as well: the walk up the path stops at the first
    // node that wants the event, so pressing the × cannot start a drag of the
    // panel behind it.
    check("and it takes the pointerdown too, so pressing it is not a drag",
      typeof closeButton.__ixdEvents.pointerdown === "function");

    const fire = (kind, extra, path) => {
      const event = Object.assign({
        type: kind,
        preventDefault() {}, stopPropagation() {}, stopImmediatePropagation() {},
        composedPath: () => path || [panel, host],
      }, extra || {});
      (sandbox.window.listeners[kind] || [])
        .filter((entry) => entry.capture)
        .forEach((entry) => entry.fn(event));
      return event;
    };

    // The panel only positions itself while it is on screen.
    panel.classList.add("visible");

    fire("pointerdown", { button: 0, pointerId: 7, clientX: 100, clientY: 100 });
    // pointermove is not guarded — it fires far too often for a listener that
    // walks the composed path — so the drag registers its own while it lasts.
    const moving = (sandbox.window.listeners.pointermove || [])
      .filter((entry) => entry.capture);
    check("pressing the panel starts listening for the pointer",
      moving.length === 1, String(moving.length));

    const move = (x, y) => moving.forEach((entry) => entry.fn({
      type: "pointermove", pointerId: 7, clientX: x, clientY: y,
      preventDefault() {}, stopImmediatePropagation() {},
    }));

    // Two pixels is a click with a shaky hand, not a drag.
    move(101, 101);
    check("a pointer that barely moves does not move the panel",
      !panel.classList.contains("dragging"), panel.className);

    // The stub's panel starts at left 0, top 40.
    move(160, 150);
    check("a real drag moves it, and it says so",
      panel.classList.contains("dragging"), panel.className);
    check("the panel follows the pointer exactly",
      panel.style.left === "60px" && panel.style.top === "90px",
      `${panel.style.left},${panel.style.top}`);

    // Off the left edge of a 1280×800 window: it stops at the margin rather
    // than leaving the screen with the download button on it.
    move(-400, 150);
    check("and it is kept inside the window",
      panel.style.left === "10px", panel.style.left);

    move(160, 150);
    fire("pointerup", { pointerId: 7, clientX: 160, clientY: 150 });
    check("letting go ends the drag", !panel.classList.contains("dragging"),
      panel.className);
    check("and the panel stays where it was put",
      panel.style.left === "60px" && panel.style.top === "90px",
      `${panel.style.left},${panel.style.top}`);

    const afterDrag = sent.length;
    fire("click", { clientX: 160, clientY: 150 });
    setTimeout(() => {
      check("the click that ends a drag does not open the menu",
        sent.length === afterDrag,
        sent.slice(afterDrag).map((m) => m.type).join(","));

      // …and the next one does, because a swallowed click is swallowed once.
      fire("click", { clientX: 160, clientY: 150 });
      setTimeout(() => {
        check("a click after that opens it as usual", sent.length > afterDrag,
          sent.slice(afterDrag).map((m) => m.type).join(","));

        const afterMenu = sent.length;
        fire("click", { clientX: 200, clientY: 90 }, [closeButton, panel, host]);
        setTimeout(() => {
          check("clicking the × takes the panel away",
            !panel.classList.contains("visible"), panel.className);
          check("and it asks the application for nothing on the way out",
            sent.length === afterMenu,
            sent.slice(afterMenu).map((m) => m.type).join(","));
          done();
        }, 20);
      }, 20);
    }, 20);
  }, 30);
}

// ---------------------------------------------------------------------------
// A download the browser is picking back up is not a download beginning now.
//
// Reported: signing in to Windows opened the file-info window asking where to
// put something that had been paused the night before. `onCreated` fires for
// the items a browser restores at start-up as well as for the ones a click
// just made, and nothing told them apart.
// ---------------------------------------------------------------------------
console.log("\n[a restored download is not a new one]");
{
  // `shouldIntercept` calls `extensionOf`, so the slice starts there.
  const from = source.indexOf("function extensionOf");
  const to = source.indexOf("// ------", source.indexOf("function shouldIntercept"));
  const scope = new Function(
    source.slice(from, to)
    + "; return { restoredReason, shouldIntercept, FRESH_DOWNLOAD_MS };")();

  const now = Date.parse("2026-08-19T09:00:00.000Z");
  const settings = {
    enabled: true, interceptDownloads: true, ignoredHosts: [],
    minSizeBytes: 0, extensions: [],
  };
  const fresh = {
    id: 1, url: "https://example.com/film.mkv", filename: "film.mkv",
    state: "in_progress", paused: false, bytesReceived: 0,
    startTime: "2026-08-19T08:59:59.900Z", fileSize: 0,
  };

  check("a download that is beginning now is taken",
    scope.restoredReason(fresh, now) === "" &&
    scope.shouldIntercept(fresh, settings, now) === true);

  check("one the browser restored from last night is left alone",
    scope.shouldIntercept(
      { ...fresh, startTime: "2026-08-18T22:14:00.000Z" }, settings, now) === false);

  check("and the reason names the time it began",
    scope.restoredReason(
      { ...fresh, startTime: "2026-08-18T22:14:00.000Z" }, now)
      .includes("2026-08-18T22:14:00.000Z"));

  check("one the browser already has bytes of is left alone",
    scope.shouldIntercept({ ...fresh, bytesReceived: 4096 }, settings, now) === false);

  check("a paused one is left alone",
    scope.shouldIntercept({ ...fresh, paused: true }, settings, now) === false);

  check("an interrupted one is left alone",
    scope.shouldIntercept({ ...fresh, state: "interrupted" }, settings, now) === false);

  check("a completed one is left alone",
    scope.shouldIntercept({ ...fresh, state: "complete" }, settings, now) === false);

  // The clock is only consulted when the item carries one: a stub item in some
  // other test, and an older browser, both leave `startTime` out.
  check("an item with no start time is judged on its other signals",
    scope.restoredReason({ ...fresh, startTime: undefined }, now) === "");

  // Two minutes is far longer than the gap between a click and `onCreated`,
  // and far shorter than a session. Pinned so a later edit has to mean it.
  check("the freshness window is two minutes",
    scope.FRESH_DOWNLOAD_MS === 120000, String(scope.FRESH_DOWNLOAD_MS));
}

panelChecks(() => moveAndCloseChecks(async () => {
  await Promise.all(pendingAsync);
  console.log(`\n${"=".repeat(60)}`);
  console.log(`${passed} passed, ${failed} failed`);
  console.log("=".repeat(60));
  process.exit(failed ? 1 : 0);
}));
