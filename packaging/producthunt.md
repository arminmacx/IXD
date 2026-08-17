# Product Hunt — the copy, and the honest read on it

**Nothing here has been submitted.** Product Hunt needs an account, a maker
profile and a scheduled launch day, all of which are yours to do. This is the
text to paste when you decide to.

---

## Should you?

Worth knowing before spending a launch on it — you get one first launch per
product, and a quiet one is hard to repeat:

* Product Hunt's audience is mostly **SaaS and AI tools**. Desktop utilities do
  land, but they need a strong visual and a clear one-liner.
* A launch does best when somebody is **there all day** answering comments. The
  ranking weights engagement, not just upvotes.
* Tuesday–Thursday, live at **00:01 Pacific**, is the standard advice — the day
  runs on Pacific time whatever your timezone.
* **Reddit first.** If the post there produces useful feedback and a few stars,
  the Product Hunt page has something to point at. If it produces nothing,
  fix that before spending the launch.

---

## Name

`Internet Xtreme Downloader`

## Tagline (60 characters max)

Pick one:

* `A free download manager that bundles nothing` — 44
* `Free download manager with no ffmpeg, no yt-dlp` — 47
* `Download manager and video downloader, zero dependencies` — 56

## Description (260 characters max)

> A free, open source download manager for Windows, Linux and macOS.
> Multi-threaded transfers, byte-exact resume, queues and a scheduler — plus
> video and audio from any page that streams them. No yt-dlp, no ffmpeg, no
> ads, no account.

(253 characters.)

## Topics

`Developer Tools` · `Open Source` · `Productivity` · `Windows` · `Linux` ·
`Mac`

## First comment — post this yourself the moment it goes live

Hello — I built this.

It started because every download manager that handles video shells out to
something else, and I wanted one that did not. HTTP, SOCKS5, HLS, DASH,
AES-128, MP4, Matroska and MPEG-TS are all implemented in the project itself,
so there is no bundled toolchain to keep up to date and nothing an upstream
release can break. One binary, one dependency (Qt).

What it does day to day: splits each download across several connections,
resumes exactly where it stopped after a drop, and puts a small panel on any
page that is playing video so you can pick a quality and send it straight to
the app. Queues, a scheduler, proxy routing and per-download speed limits are
all there.

It is free and stays free — no ads, no account, no paid tier, MIT licensed.

Two things I would rather say here than have you find out: the macOS build has
never been run on real hardware (it builds and packages on a macOS runner, and
that is all I can claim), and the Firefox extension is not signed yet so it
loads through `about:debugging`. Chrome and Edge are fine.

Happy to answer anything, and if something breaks there is a Log button that
captures both halves — paste it into an issue and it is usually obvious.

## Gallery

In order, and the first one is what most people will see:

1. `docs/screenshot-main.png` — the main window.
2. The in-page panel over a video with the quality menu open.
3. The download-file-info window (address, folder, Start / Download later).
4. The first-run guide, extension page.
5. The scheduler.

Product Hunt crops to 16:9, so keep the important part central and do not put
text near the edges.

## Links

* Website: https://arminmacx.github.io/IXD/
* GitHub: https://github.com/arminmacx/IXD
* Download: https://github.com/arminmacx/IXD/releases/latest
