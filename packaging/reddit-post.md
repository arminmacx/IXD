# Reddit post — v1.0.18

Written to be posted as-is. Plain language, no marketing voice, and it says
what does not work — which on Reddit is the difference between a post people
try and a post people report.

---

## Where to post, in order

| Subreddit | Why | Read the rules first |
|---|---|---|
| **r/opensource** | The best fit. Self-promotion is allowed when the project is genuinely open source and you wrote it. | Flair as a project/showcase post. |
| **r/software** | People asking for a download manager end up here constantly. | No link-only posts; the text below is enough. |
| **r/DataHoarder** | Multi-connection downloads and resume are exactly their problem. Lead with resume and integrity, not video. | Read their self-promo rule — some weeks are stricter. |
| **r/Python** | It is a PySide6/stdlib project with no dependencies, which is the angle there. | They want technical substance, so lead with "no yt-dlp, no ffmpeg". |
| **r/linux** or **r/linuxmasterrace** | The Linux build is the one you actually test. | r/linux dislikes announcements; post there only if it gets traction elsewhere. |

Post to **one** subreddit first, answer the comments for a day, then post to the
next. All at once reads as spam and the filters treat it that way.

Do **not** post to r/DataHoarder and r/opensource on the same day from a new
account — that pattern is what gets auto-removed.

---

## Title

Pick one. The first is the safest.

* `I made a free download manager that doesn't bundle yt-dlp or ffmpeg — everything is implemented in the app`
* `IXD — a free, open source download manager for Windows, Linux and macOS (no external binaries)`
* `Spent a while building a download manager with no dependencies except Qt. It downloads video too.`

---

## Body

I have been working on a download manager called Internet Xtreme Downloader,
and it is at a point where other people trying it would be useful.

It does the usual accelerated-download things — it splits a file across several
connections, resumes exactly where it stopped after a drop or a crash, and has
queues and a scheduler. There is a browser extension that catches downloads and
puts a small panel on pages that are playing video, so you can pick a quality
and it goes straight to the app.

The part that took the longest: **it does not bundle anything.** No yt-dlp, no
ffmpeg, no curl. HTTP, SOCKS5, HLS, DASH, AES-128, MP4, Matroska and MPEG-TS
are all written in the project itself. That was mostly stubbornness at the
start, but it turned out to matter — nothing breaks when an external tool
changes, and the whole thing is one binary with one dependency (Qt).

It is free and it stays free. No ads, no account, no paid tier, nothing held
back. MIT licensed, and the builds on the releases page are made by CI from the
same commit.

**What works:** ordinary downloads, YouTube including 1080p and above (video
and audio are separate streams up there, so it fetches both and combines them),
HLS and DASH sites, and sites nobody wrote support for — it reads what the
player is actually fetching rather than needing the site to be known.

**What does not, or not yet:**

* The macOS build compiles and packages but nobody has run it on real hardware.
  If you have a Mac, I would like to know either way.
* The Firefox extension has to be loaded through `about:debugging` because it
  is not signed yet, which means Firefox forgets it when you close it. Chrome
  and Edge load it normally.
* It needs a fairly recent Linux — glibc 2.38 — so Ubuntu 22.04 and Debian 12
  cannot run the current build. That is on my list.
* There is no AppImage yet, despite what the build script implies.

If something does not work, there is a Log button in the toolbar that captures
both the app's side and the browser extension's side in order. Pasting that
into an issue usually answers the question immediately.

GitHub: https://github.com/arminmacx/IXD
Site: https://arminmacx.github.io/IXD/

Happy to answer anything.

---

## Answering comments

Things that will come up, and short honest answers:

* **"How is this different from IDM?"** — It is free, it is open source, it
  runs on Linux and macOS as well, and it does not bundle external binaries.
  IDM is more mature and has been doing this for twenty years.
* **"Why not just use yt-dlp?"** — For downloading video, yt-dlp is excellent
  and supports far more sites. This is a download manager first that happens to
  handle video, and it is a GUI you install once. It is not a replacement.
* **"Is it a virus / why is Windows warning me?"** — The binaries are not code
  signed; a certificate costs money and I have not bought one. The builds come
  from CI, the workflow is in the repo, and you can build it yourself.
* **"Python? Why?"** — Because it is what got it finished. The transfer engine
  is the standard library, the UI is Qt, and the packaged build is one folder.
* **"Does it phone home?"** — No telemetry, no account. It contacts GitHub only
  when you ask it to check for a new version.
