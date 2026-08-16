// Runs in the **page's own world**, not this extension's.
//
// This is the one thing `idm/document.js` does that nothing else here did. It
// wraps `fetch` and `XMLHttpRequest` where the player's own code can be seen,
// and tees what a response *contains* — not where it came from.
//
// Why it has to exist, measured over 2026-08-12 and not argued:
//
//   * a `videoplayback` address is refused on a second fetch, to this
//     application, to `curl`, to this extension's worker, and to the
//     youtube.com page that minted it — so no address can be replayed;
//   * the server-driven session delivers 100% of a stream's media and never
//     its opening 965 bytes, three runs running;
//   * the streaming endpoint answers an ordinary GET with 31 bytes of framed
//     protocol, not media.
//
// Every route that does not involve seeing what the player receives is shut.
// The player *does* receive those bytes — it plays the video — so they are
// taken from there.
//
// Deliberately dumb: it copies bytes and posts them. It parses no protocol,
// knows no itags, and decides nothing. The application already has a full
// reader for this format, and a hook in a page someone is browsing is the last
// place to put logic that could throw.
(() => {
  "use strict";

  //: How much of a response is kept. A session's opening — the piece that is
  //: never sent again — is at the head of its first reply, so this only has to
  //: be large enough to contain it with its framing. Keeping whole replies
  //: would mean copying a film through three message hops for the sake of a
  //: kilobyte at the front.
  const HEAD_BYTES = 128 * 1024;

  //: One post per response, and never the same response twice.
  //: (Announcements are throttled separately, above.)
  const seen = new Set();
  //: A page that loads a hundred segments must not send a hundred messages.
  //: Only the first few responses per address shape carry an opening.
  const MAX_POSTS = 24;
  let posted = 0;

  //: Media this is worth doing for. `videoplayback` is the server-driven
  //: endpoint; the rest are the ordinary shapes a player fetches.
  function worthTeeing(url) {
    if (typeof url !== "string" || !url) return false;
    const wanted = url.includes("videoplayback")
      || url.includes(".googlevideo.com/")
      || /\.(m3u8|mpd|m4s|ts|mp4|webm)(\?|$)/i.test(url);
    if (wanted) {
      considered += 1;
      announce("the player is fetching media through a hooked call");
    }
    return wanted;
  }

  //: Say once that this is running, and what it has seen.
  //:
  //: Its absence and its silence were indistinguishable from outside — a
  //: session went by with no line from this hook at all, which could equally
  //: have meant the extension was not reloaded, the MAIN world was
  //: unavailable, or the player fetched nothing worth copying. Three different
  //: faults, one symptom.
  let announced = false;
  let considered = 0;
  function announce(detail) {
    if (announced) return;
    announced = true;
    try {
      window.postMessage({
        __ixdTee: true, hello: true,
        url: String(location.href).slice(0, 300),
        detail: String(detail || ""),
      }, "*");
    } catch (error) {
      /* a page that will not take a message is one this cannot report from */
    }
  }

  //: Encrypted media announces itself, and it is the one honest answer to
  //: "why did nothing appear on this page".
  //:
  //: A player that asks for a content-decryption module is playing media the
  //: browser will only ever hand to that module: Spotify, and every other
  //: service that streams under Widevine or PlayReady. Nothing this
  //: application can capture is of any use, because the bytes on the wire are
  //: ciphertext and the key is not ours to have. Reported so the Log answers
  //: the question instead of being empty, and never acted on.
  try {
    const askForKeys = navigator.requestMediaKeySystemAccess;
    if (typeof askForKeys === "function") {
      navigator.requestMediaKeySystemAccess = function (system, ...rest) {
        try {
          window.postMessage({
            __ixdTee: true, drm: true,
            url: String(location.href).slice(0, 300),
            system: String(system || ""),
          }, "*");
        } catch (error) {
          /* a page that will not take a message is one this cannot report from */
        }
        return askForKeys.call(this, system, ...rest);
      };
    }
  } catch (error) {
    /* a page that has frozen navigator keeps its own copy; nothing to report */
  }

  function post(url, buffer) {
    if (posted >= MAX_POSTS) return;
    try {
      const bytes = new Uint8Array(buffer, 0, Math.min(buffer.byteLength,
                                                       HEAD_BYTES));
      if (!bytes.length) return;
      let binary = "";
      const step = 0x8000;
      for (let at = 0; at < bytes.length; at += step) {
        binary += String.fromCharCode.apply(
          null, bytes.subarray(at, at + step));
      }
      posted += 1;
      window.postMessage({
        __ixdTee: true,
        url: String(url).slice(0, 2000),
        total: buffer.byteLength,
        data: btoa(binary),
      }, "*");
    } catch (error) {
      /* a page that cannot be teed is a page that still works */
    }
  }

  // -- fetch ------------------------------------------------------------
  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = function (...args) {
      const result = originalFetch.apply(this, args);
      try {
        const request = args[0];
        const url = typeof request === "string" ? request
          : (request && request.url) || "";
        if (!worthTeeing(url)) return result;
        return result.then((response) => {
          try {
            // The clone is what makes this safe: the page reads its own copy,
            // untouched, and this reads a second one. Consuming the original
            // would break the player outright.
            if (response && response.ok && response.body && !seen.has(response)) {
              seen.add(response);
              response.clone().arrayBuffer()
                .then((buffer) => post(url, buffer))
                .catch(() => {});
            }
          } catch (error) {
            /* nothing here may affect what the page receives */
          }
          return response;
        });
      } catch (error) {
        return result;
      }
    };
  }

  // -- XMLHttpRequest ---------------------------------------------------
  const open = XMLHttpRequest.prototype.open;
  const send = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url, ...rest) {
    try {
      this.__ixdUrl = url;
    } catch (error) {
      /* a frozen request object is simply not teed */
    }
    return open.call(this, method, url, ...rest);
  };
  XMLHttpRequest.prototype.send = function (...args) {
    try {
      const url = this.__ixdUrl;
      if (worthTeeing(url)) {
        this.addEventListener("load", () => {
          try {
            const body = this.response;
            if (body instanceof ArrayBuffer) post(url, body);
          } catch (error) {
            /* a response type this cannot read is left alone */
          }
        });
      }
    } catch (error) {
      /* as above: never break the page's own request */
    }
    return send.apply(this, args);
  };

  // Said as soon as the wrapping is in place, before any media is fetched, so
  // "the hook is not installed" and "the hook saw nothing" are separable from
  // the log alone. The page's own script may run before this and be missed;
  // that is worth knowing too, and only this line can show it.
  // On a timer, not on `load`.
  //
  // The first version announced from `window.load`, and the only page that
  // ever reported was an `accounts.youtube.com` iframe — the watch page said
  // nothing at all. A watch page is a single-page application: it is reached
  // by soft navigation, `load` fired long before this script was injected into
  // the new document, and the listener was waiting for an event that had
  // already happened. The one page whose report mattered was the one that
  // could not make it.
  setTimeout(() => {
    // Only the page someone is watching, or any frame that actually saw media.
    // This runs in every frame, and a video page carries a dozen advertising
    // and captcha iframes — each of which dutifully reported that it had seen
    // no media, which is true, expected, and forty lines of a log the user has
    // to read.
    const isTop = (() => {
      try {
        return window.top === window;
      } catch (error) {
        return false;          // cross-origin: not the page being watched
      }
    })();
    if (!considered && !isTop) return;
    announce(considered
      ? `${considered} media call(s) seen`
      : "installed, but the player has not fetched media through fetch or "
        + "XMLHttpRequest on this page");
  }, 4000);
})();
