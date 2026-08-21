"""Extractor unit tests — manifest parsing, page scraping, signature deciphering.

These are deterministic and offline.  Live-network extraction is exercised by
``tests/test_live.py``, which is opt-in because it depends on remote services.

Run with:  python -m tests.test_extractors
"""

from __future__ import annotations

import base64
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ixd.core.errors import ExtractionError
from ixd.core.protobuf import Message
from ixd.extractors import dash, hls
from ixd.extractors.base import select_format
from ixd.extractors.generic import GenericExtractor, looks_like_media_url
from ixd.extractors.youtube import SignatureDecipher, YouTubeExtractor

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name} {detail}")


# ----------------------------------------------------------------------
def test_hls() -> None:
    print("\n[HLS]")
    master = """#EXTM3U
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="English",URI="audio/en.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"
1080/index.m3u8
"""
    variants = hls.parse_master(master, "https://cdn.test/v/master.m3u8")
    check("master yields both variants plus audio", len(variants) == 3, str(len(variants)))
    check("resolution parsed", any(v.height == 1080 and v.width == 1920 for v in variants))
    check("variant URL resolved against base",
          any(v.url == "https://cdn.test/v/1080/index.m3u8" for v in variants))

    media = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-KEY:METHOD=AES-128,URI="https://k.test/key.bin",IV=0x0123456789ABCDEF0123456789ABCDEF
#EXTINF:9.009,
seg0.ts
#EXTINF:9.009,
seg1.ts
#EXT-X-KEY:METHOD=NONE
#EXTINF:3.003,
seg2.ts
#EXT-X-ENDLIST
"""
    segments = hls.parse_media_playlist(media, "https://cdn.test/v/720/index.m3u8")
    check("segment count", len(segments) == 3, str(len(segments)))
    check("key applies to following segments",
          segments[0].key_url == "https://k.test/key.bin" and segments[1].key_url is not None)
    check("METHOD=NONE clears the key", segments[2].key_url is None)
    check("explicit IV retained", segments[0].key_iv == "0x0123456789ABCDEF0123456789ABCDEF")
    check("duration summed", abs(hls.total_duration(segments) - 21.021) < 0.01)

    # Absent IV must default to the media sequence number.
    no_iv = """#EXTM3U
#EXT-X-MEDIA-SEQUENCE:5
#EXT-X-KEY:METHOD=AES-128,URI="k"
#EXTINF:4,
a.ts
"""
    defaulted = hls.parse_media_playlist(no_iv, "https://cdn.test/p.m3u8")
    check("IV defaults to media sequence", defaulted[0].key_iv == f"{5:032x}",
          str(defaulted[0].key_iv))

    byterange = """#EXTM3U
#EXTINF:4.0,
#EXT-X-BYTERANGE:1000@0
all.ts
#EXTINF:4.0,
#EXT-X-BYTERANGE:2000
all.ts
"""
    ranged = hls.parse_media_playlist(byterange, "https://cdn.test/p.m3u8")
    check("byterange offsets chain",
          [s.byte_range for s in ranged] == [(0, 999), (1000, 2999)],
          str([s.byte_range for s in ranged]))

    try:
        hls.parse_media_playlist(
            '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="k"\n#EXTINF:4,\na.ts\n',
            "https://cdn.test/p.m3u8",
        )
        check("unsupported encryption rejected", False, "no error raised")
    except ExtractionError:
        check("unsupported encryption rejected", True)


def test_dash() -> None:
    print("\n[DASH]")
    check("ISO duration parsed", dash.parse_iso_duration("PT1H2M3.5S") == 3723.5)
    check("template padding honoured",
          dash.expand_template("s-$Number%05d$.m4s", number=7) == "s-00007.m4s")
    check("representation id substituted",
          dash.expand_template("$RepresentationID$/x", representation_id="v1") == "v1/x")

    mpd = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT0H1M0.000S">
 <Period>
  <AdaptationSet mimeType="video/mp4" contentType="video">
   <SegmentTemplate media="$RepresentationID$/seg-$Number%05d$.m4s"
     initialization="$RepresentationID$/init.mp4" timescale="1000" duration="4000" startNumber="1"/>
   <Representation id="v720" bandwidth="1200000" width="1280" height="720" codecs="avc1.4d401f"/>
   <Representation id="v1080" bandwidth="2500000" width="1920" height="1080" codecs="avc1.640028"/>
  </AdaptationSet>
  <AdaptationSet mimeType="audio/mp4" contentType="audio">
   <SegmentTemplate media="$RepresentationID$/a-$Number$.m4s"
     initialization="$RepresentationID$/ainit.mp4" timescale="1000" duration="4000" startNumber="1"/>
   <Representation id="a128" bandwidth="128000" codecs="mp4a.40.2"/>
  </AdaptationSet>
 </Period>
</MPD>"""
    formats = dash.parse_mpd(mpd, "https://d.test/m/manifest.mpd")
    check("all representations parsed", len(formats) == 3, str(len(formats)))
    video = next(f for f in formats if f.format_id == "v1080")
    check("init + 15 media segments", len(video.segments) == 16, str(len(video.segments)))
    check("init segment flagged", video.segments[0].init is True)
    check("numbering starts at startNumber",
          video.segments[1].url.endswith("v1080/seg-00001.m4s"), video.segments[1].url)
    audio = next(f for f in formats if f.format_id == "a128")
    check("audio classified audio-only", audio.has_audio and not audio.has_video)

    timeline_mpd = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT12S">
 <Period><AdaptationSet mimeType="video/mp4">
  <SegmentTemplate media="s-$Time$.m4s" initialization="i.mp4" timescale="1000">
   <SegmentTimeline><S t="0" d="4000" r="2"/></SegmentTimeline>
  </SegmentTemplate>
  <Representation id="r1" bandwidth="900000" width="640" height="360"/>
 </AdaptationSet></Period></MPD>"""
    timeline = dash.parse_mpd(timeline_mpd, "https://d.test/m.mpd")[0]
    names = [s.url.rsplit("/", 1)[-1] for s in timeline.segments]
    check("SegmentTimeline expands with repeats",
          names == ["i.mp4", "s-0.m4s", "s-4000.m4s", "s-8000.m4s"], str(names))

    list_mpd = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" mediaPresentationDuration="PT8S">
 <Period><AdaptationSet mimeType="video/mp4">
  <Representation id="r1" bandwidth="500000" width="320" height="240">
   <SegmentList duration="4000" timescale="1000">
    <Initialization sourceURL="init.mp4"/>
    <SegmentURL media="s1.m4s"/><SegmentURL media="s2.m4s"/>
   </SegmentList>
  </Representation>
 </AdaptationSet></Period></MPD>"""
    listed = dash.parse_mpd(list_mpd, "https://d.test/m.mpd")[0]
    check("SegmentList honoured", len(listed.segments) == 3, str(len(listed.segments)))


def test_generic() -> None:
    print("\n[generic scraper]")
    check("media URL recognised", looks_like_media_url("https://x.test/a/b.mp4?t=1"))
    check("manifest URL recognised", looks_like_media_url("https://x.test/a/b.m3u8"))
    check("page URL not media", not looks_like_media_url("https://x.test/watch/1"))

    page = """<html><head><title>  My   Video Page </title>
    <meta property="og:video:secure_url" content="https://cdn.test/og.mp4">
    <meta property="og:image" content="https://cdn.test/thumb.jpg"></head>
    <body><video src="/rel/inline.mp4"></video>
    <script type="application/ld+json">{"contentUrl":"https://cdn.test/ld.mp4"}</script>
    <script>var s="https:\\/\\/cdn.test\\/stream\\/master.m3u8?x=1";</script>
    </body></html>"""
    extractor = GenericExtractor.__new__(GenericExtractor)
    found = extractor._collect(page, "https://site.test/watch/1")
    check("og:video found", "https://cdn.test/og.mp4" in found)
    check("relative <video> resolved", "https://site.test/rel/inline.mp4" in found)
    check("json-ld contentUrl found", "https://cdn.test/ld.mp4" in found)
    check("escaped inline m3u8 found",
          "https://cdn.test/stream/master.m3u8?x=1" in found, str(list(found)))
    check("title normalised",
          extractor._page_title(page) == "My Video Page", extractor._page_title(page))


def test_youtube_helpers() -> None:
    print("\n[YouTube helpers]")
    for url, expected in (
        ("https://www.youtube.com/watch?v=aqz-KE-bpKQ", "aqz-KE-bpKQ"),
        ("https://youtu.be/aqz-KE-bpKQ?t=30", "aqz-KE-bpKQ"),
        ("https://www.youtube.com/shorts/aqz-KE-bpKQ", "aqz-KE-bpKQ"),
        ("https://www.youtube.com/embed/aqz-KE-bpKQ", "aqz-KE-bpKQ"),
        ("https://www.youtube.com/live/aqz-KE-bpKQ", "aqz-KE-bpKQ"),
    ):
        check(f"video id from {url.split('/')[-1][:18]}",
              YouTubeExtractor.video_id(url) == expected)

    check("claims watch URLs",
          YouTubeExtractor.matches("https://www.youtube.com/watch?v=aqz-KE-bpKQ"))
    check("ignores unrelated URLs",
          not YouTubeExtractor.matches("https://example.com/video.mp4"))

    # Balanced-JSON slicing must survive braces and escapes inside strings.
    extractor = YouTubeExtractor.__new__(YouTubeExtractor)
    blob = 'var x = {"a":{"b":"}{ \\" nested"},"c":[1,2]}; var y=2;'
    sliced = extractor._balanced_json(blob, blob.index("{"))
    check("balanced JSON slice", sliced == '{"a":{"b":"}{ \\" nested"},"c":[1,2]}', sliced)

    # A faithful miniature of the player's transform pipeline.
    player_js = """
    var Xq={
      wT:function(a){a.reverse()},
      pO:function(a,b){a.splice(0,b)},
      Rd:function(a,b){var c=a[0];a[0]=a[b%a.length];a[b%a.length]=c}
    };
    zA=function(a){a=a.split("");Xq.wT(a,1);Xq.pO(a,3);Xq.Rd(a,2);return a.join("")};
    """
    decipher = SignatureDecipher(player_js)
    check("three operations parsed", len(decipher.operations) == 3,
          str(decipher.operations))
    # Apply the same program by hand: reverse, drop 3, swap 0 and 2.
    source = "abcdefgh"
    expected = list(source)[::-1]
    del expected[:3]
    expected[0], expected[2] = expected[2], expected[0]
    check("signature transform matches reference",
          decipher.decipher(source) == "".join(expected),
          f"{decipher.decipher(source)} != {''.join(expected)}")

    # The same pipeline as a recent player writes it: every string lifted into
    # one table and referred to by index. `a.split("")` becomes
    # `a[A[21]](A[3])`, which is why a search for a literal `split("")` found
    # nine call sites in the real player and not one of them a descrambler.
    entries = ["f%d" % n for n in range(60)]
    entries[3] = ""
    entries[9] = "splice"
    entries[13] = "length"
    entries[21] = "split"
    entries[29] = "reverse"
    entries[48] = "join"
    table = ";".join(entries)
    hoisted = """
    var A='%s'.split(";");
    var Xq={
      wT:function(a){a[A[29]]()},
      pO:function(a,b){a[A[9]](0,b)},
      Rd:function(a,b){var c=a[0];a[0]=a[b%%a[A[13]]];a[b%%a[A[13]]]=c}
    };
    zA=function(a){a=a.split("");Xq.wT(a,1);Xq.pO(a,3);Xq.Rd(a,2);return a.join("")};
    """ % table
    indexed = SignatureDecipher(hoisted)
    check("a hoisted string table is read",
          indexed.table[0] == "A" and indexed.table[1][29] == "reverse",
          str(indexed.table[0]))
    check("and the helper is classified through it",
          [kind for kind, _ in indexed.operations] == ["reverse", "splice", "swap"],
          str(indexed.operations))
    check("so the transform still matches the reference",
          indexed.decipher(source) == "".join(expected),
          indexed.decipher(source))

    # And when the player computes those indices while it runs — `A[D^3864]`,
    # with D coming from the call — the names exist nowhere in the file. That
    # is not "the function moved"; it is unreadable without executing it, and
    # saying so is what stops the next session searching for a pattern.
    computed = ("var A='a;b;c'.split(\";\");"
                "var P1={LZ:function(J,W){J[A[9]](0,W)}};"
                "x=function(H,D){Y=H[A[D^3845]](A[3]);P1[A[D^3864]](Y,D^3897);"
                "return Y[A[D^3872]](A[3])};")
    try:
        SignatureDecipher(computed)
        check("a run-time-indexed player is named as such", False, "it parsed")
    except ExtractionError as exc:
        check("a run-time-indexed player is named as such",
              "computes its signature function" in str(exc), str(exc)[:70])


def test_format_selection() -> None:
    print("\n[format selection]")
    from ixd.core.models import MediaFormat

    formats = [
        MediaFormat("a", "u1", height=2160, vcodec="vp9", acodec="none"),
        MediaFormat("b", "u2", height=1080, vcodec="h264", acodec="aac"),
        MediaFormat("c", "u3", height=720, vcodec="h264", acodec="aac"),
        MediaFormat("d", "u4", height=0, vcodec="none", acodec="aac", tbr=160),
    ]
    check("prefers progressive at the cap",
          select_format(formats, "1080p").format_id == "b")
    check("never exceeds the requested height",
          select_format(formats, "720p").format_id == "c")
    from ixd.extractors.base import best_audio
    check("best audio picked", best_audio(formats).format_id == "d")

    # The requested resolution outranks the convenience of a ready-muxed
    # stream. Ranking progressive first meant every request came back as the
    # 360p progressive copy, because on YouTube that is now the only
    # progressive stream offered — so asking for 1080p, 720p or 480p all
    # produced the same 360p file, which is exactly what a user sees as "the
    # quality menu does nothing".
    adaptive = [
        MediaFormat("prog360", "u1", height=360, vcodec="h264", acodec="aac"),
        MediaFormat("v1080", "u2", height=1080, vcodec="h264", acodec="none"),
        MediaFormat("v720", "u3", height=720, vcodec="h264", acodec="none"),
        MediaFormat("a140", "u4", height=0, vcodec="none", acodec="aac", tbr=130),
    ]
    check("1080p is not answered with the progressive 360p copy",
          select_format(adaptive, "1080p").format_id == "v1080")
    check("720p picks the 720p track, not the progressive one",
          select_format(adaptive, "720p").format_id == "v720")
    check("360p still prefers the progressive copy at that height",
          select_format(adaptive, "360p").format_id == "prog360")

    # Deliverability still comes first: half a file at the requested height is
    # worth less than a whole one a size down. This is the case that makes a
    # long YouTube video fall back to 360p rather than stall a third of the way
    # through 1080p.
    capped = [
        MediaFormat("prog360", "u1", height=360, vcodec="h264", acodec="aac"),
        MediaFormat("v1080", "u2", height=1080, vcodec="h264", acodec="none",
                    restricted=True),
        MediaFormat("v144", "u3", height=144, vcodec="h264", acodec="none",
                    restricted=True),
    ]
    check("a capped stream loses to one that arrives whole",
          select_format(capped, "1080p").format_id == "prog360")
    check("a capped stream loses even at the requested height",
          select_format(capped, "144p").format_id == "prog360")

    from ixd.extractors.base import quality_shortfall
    # ---- which container, when a quality comes in more than one ---------
    #
    # The numbers are the real 1080p60 renditions of the video this was
    # reported on. Nothing but bitrate used to separate the first two, and they
    # sit 0.3% apart — so the container was decided by an accident, and the
    # accident chose the file 46% larger and the codec fewer things play.
    vp9 = MediaFormat("303", "u1", ext="webm", height=1080, fps=60,
                      vcodec="vp09", acodec="none", tbr=4346, filesize=198_035_097)
    h264 = MediaFormat("299", "u2", ext="mp4", height=1080, fps=60,
                       vcodec="avc1", acodec="none", tbr=4334, filesize=135_996_996)
    av1 = MediaFormat("399", "u3", ext="mp4", height=1080, fps=60,
                      vcodec="av01", acodec="none", tbr=2297, filesize=73_071_798)
    ladder = [vp9, h264, av1]

    check("the MP4 rendition wins when MP4 is preferred",
          select_format(ladder, "1080p", False, "mp4").format_id == "299",
          select_format(ladder, "1080p", False, "mp4").format_id)
    check("and the WebM one when WebM is",
          select_format(ladder, "1080p", False, "webm").format_id == "303",
          select_format(ladder, "1080p", False, "webm").format_id)
    check("with no preference, bitrate decides as before",
          select_format(ladder, "1080p", False, "").format_id == "303",
          select_format(ladder, "1080p", False, "").format_id)
    # Among two MP4s, bitrate still decides — H.264 over the smaller AV1 copy.
    check("the preference does not override bitrate within a container",
          select_format([h264, av1], "1080p", False, "mp4").format_id == "299",
          select_format([h264, av1], "1080p", False, "mp4").format_id)
    # A tie-break, never a filter: a resolution published in one container only
    # is still the right answer for that resolution.
    only_webm = MediaFormat("315", "u4", ext="webm", height=2160, fps=60,
                            vcodec="vp09", acodec="none", tbr=20000)
    check("a resolution offered in one container only is still chosen",
          select_format([only_webm, h264], "2160p", False, "mp4").format_id == "315",
          select_format([only_webm, h264], "2160p", False, "mp4").format_id)

    check("a met request reports no shortfall",
          quality_shortfall(adaptive[1], "1080p") == 0)
    check("a fallback reports how far it fell",
          quality_shortfall(capped[0], "1080p") == 720)


def test_original_audio_beats_a_dubbing() -> None:
    """The language of the finished file must not be decided by bitrate.

    A video may publish machine dubbings beside its original audio. They share
    a container, a codec, comparable bitrates — and, on YouTube, the *same
    itag*. Picking on bitrate therefore chose a language at random, and the
    substitution is invisible until the file is played: a viewer who asked for
    a video got one dubbed into another language.
    """
    print("\n[the original audio track wins]")
    from ixd.core.models import MediaFormat
    from ixd.extractors.base import best_audio, best_muxable_audio
    from ixd.extractors.youtube import YouTubeExtractor

    video = MediaFormat("137", "u", ext="mp4", height=1080,
                        vcodec="h264", acodec="none")
    original = MediaFormat("140", "a1", ext="m4a", vcodec="none", acodec="aac",
                           tbr=130, audio_is_default=True,
                           audio_language="en", audio_kind="original")
    dubbed = MediaFormat("140", "a2", ext="m4a", vcodec="none", acodec="aac",
                         tbr=190, audio_is_default=False,
                         audio_language="de", audio_kind="dubbed")

    check("a louder dubbing does not outrank the original",
          best_muxable_audio([video, dubbed, original], video) is original)
    check("nor does it when picking audio on its own",
          best_audio([dubbed, original]) is original)
    check("order of discovery does not decide it",
          best_muxable_audio([video, original, dubbed], video) is original)

    # Among tracks of the same kind, bitrate decides as before.
    louder = MediaFormat("141", "a3", ext="m4a", vcodec="none", acodec="aac",
                         tbr=256, audio_is_default=True,
                         audio_language="en", audio_kind="original")
    check("bitrate still decides between equals",
          best_muxable_audio([video, original, louder], video) is louder)

    # Reading the site's own markings, in both places it states them.
    track = YouTubeExtractor._audio_track
    check("an explicit default is honoured",
          track({"audioTrack": {"id": "en.4", "audioIsDefault": True}})
          == (True, "en", "", "en.4", "", ""))
    check("an entry naming no track at all is the ordinary one",
          track({}) == (True, "", "", "", "", ""))
    check("a non-default track is not promoted by silence",
          track({"audioTrack": {"id": "de.4", "audioIsDefault": False}})
          == (False, "de", "", "de.4", "", ""))

    # ---- the form the site actually publishes --------------------------
    #
    # These tags are base64url-encoded protobuf, not `name=value:name=value`
    # text. This test asserted the text form for two sessions, invented it,
    # and passed throughout — while a real value decoded to nothing, left a
    # German auto-dub looking like an untagged original, and handed the user a
    # video in the wrong language. Twice.
    #
    # The value below is not constructed. It is the tag string recorded
    # against the audio track of the download that was reported wrong.
    from ixd.extractors.youtube import _parse_xtags

    reported = "ChQKBWFjb250EgtkdWJiZWQtYXV0bwoNCgRsYW5nEgVkZS1ERQ"
    check("the tags the site really sends are decoded",
          _parse_xtags(reported) == {"acont": "dubbed-auto", "lang": "de-DE"},
          str(_parse_xtags(reported)))
    check("and that dubbing is recognised as one",
          track({"xtags": reported}) == (False, "de-DE", "dubbed", "", "",
                                         reported),
          str(track({"xtags": reported})))

    encoded_original = base64.urlsafe_b64encode(
        b"\n\x11\n\x05acont\x12\x08original\n\r\n\x04lang\x12\x05en-US"
    ).decode().rstrip("=")
    check("an original is recognised from the same encoding",
          track({"xtags": encoded_original})
          == (True, "en-US", "original", "", "", encoded_original),
          str(track({"xtags": encoded_original})))

    # The whole point, in the shape it failed: a louder dubbing against an
    # original that carries no tags at all, which is the ordinary publication.
    def tagged(xtags: str, tbr: float) -> MediaFormat:
        fields = dict(zip(YouTubeExtractor._AUDIO_TRACK_FIELDS,
                          track({"xtags": xtags})))
        return MediaFormat("140", f"a-{tbr}", ext="m4a", vcodec="none",
                           acodec="aac", tbr=tbr, **fields)

    louder_dub = tagged(reported, 132)
    untagged_original = tagged("", 128)
    check("a louder real dubbing loses to an untagged original",
          best_muxable_audio([video, louder_dub, untagged_original], video)
          is untagged_original,
          str(best_muxable_audio([video, louder_dub, untagged_original],
                                 video).audio_language))

    # The plain form is still read, since accepting it costs nothing.
    check("the text form is still understood",
          track({"xtags": "acont=dubbed-auto:lang=de"})
          == (False, "de", "dubbed", "", "", "acont=dubbed-auto:lang=de"))
    check("and nonsense is simply no tags at all",
          track({"xtags": "!!! not anything !!!"})
          == (True, "", "", "", "", "!!! not anything !!!"),
          str(track({"xtags": "!!! not anything !!!"})))


def test_every_quality_of_a_stream_reaches_the_menu() -> None:
    """A viewer must get the choice the playlist actually offers.

    Real playlists routinely declare only a bandwidth and a codec list —
    ``RESOLUTION`` is optional in HLS. Two rules together reduced such a stream
    to a single low-quality entry: variant parsing gave it no label, and the
    menu keeps one row per *height*, so every quality collapsed into one and
    whichever came first won. On a four-quality stream that meant being offered
    the 232k copy and nothing else.
    """
    print("\n[every quality of a stream reaches the menu]")
    from ixd.extractors import hls
    from ixd.service import DownloadService

    # The shape Apple's own reference stream publishes: no RESOLUTION anywhere,
    # and one audio-only variant among the video ones.
    playlist = "\n".join([
        "#EXTM3U",
        '#EXT-X-STREAM-INF:BANDWIDTH=232370,CODECS="mp4a.40.2, avc1.4d4015"',
        "gear1/prog_index.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=649879,CODECS="mp4a.40.2, avc1.4d401e"',
        "gear2/prog_index.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=1927833,CODECS="mp4a.40.2, avc1.4d401f"',
        "gear4/prog_index.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=41457,CODECS="mp4a.40.2"',
        "gear0/prog_index.m3u8",
    ])
    variants = hls.parse_master(playlist, "https://cdn.example.com/x/master.m3u8")

    check("every variant is parsed", len(variants) == 4, str(len(variants)))
    check("the audio-only variant is not called a video one",
          [v.has_video for v in variants] == [True, True, True, False],
          str([(v.quality_label, v.has_video) for v in variants]))
    check("a variant with no resolution is still labelled",
          all(v.quality_label for v in variants),
          str([v.quality_label for v in variants]))

    shown = DownloadService.presentable_formats(variants, "mp4")
    check("every quality reaches the menu, not just one",
          len(shown) == 4, f"{len(shown)} of 4")
    check("and the best is offered first",
          shown[0].tbr > shown[1].tbr,
          str([v.quality_label for v in shown]))
    check("the audio-only one is still offered, for a music stream",
          any(not v.has_video for v in shown),
          str([(v.quality_label, v.has_video) for v in shown]))

    # A stream that *does* state its resolutions is unaffected.
    with_resolution = "\n".join([
        "#EXTM3U",
        '#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e"',
        "360.m3u8",
        '#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1920x1080,CODECS="avc1.640028"',
        "1080.m3u8",
    ])
    labelled = hls.parse_master(with_resolution, "https://cdn.example.com/x/m.m3u8")
    check("a stream that states its resolutions is labelled by them",
          [v.quality_label for v in labelled] == ["360p", "1080p"],
          str([v.quality_label for v in labelled]))
    rows = DownloadService.presentable_formats(labelled, "mp4")
    check("and offers one row per resolution, tallest first",
          [v.height for v in rows] == [1080, 360], str([v.height for v in rows]))


def test_a_page_that_delegates_its_player_is_followed() -> None:
    """Most sites do not hold their own media, and scraping harder never helps.

    The page a viewer is on is very often a wrapper: the player lives on
    another host inside an ``<iframe>``, and the address of the video is in a
    configuration object handed to a player library rather than in any markup.
    Scraping only the first page finds nothing on exactly the sites that need
    it most, because there was never anything there to find.

    Served locally, end to end: a wrapper page with a real player embed and an
    advertising one beside it, a player page with no ``<video>`` element at
    all, and an HLS playlist at the end of it.
    """
    print("\n[a page that delegates its player is followed]")
    import http.server
    import threading

    from ixd.core.http_client import HttpClient
    from ixd.core.net import NetworkProfile
    from ixd.extractors.generic import GenericExtractor

    wrapper = ('<html><head><title>Episode 1</title></head><body>'
               '<iframe src="{player}" allowfullscreen></iframe>'
               '<iframe src="https://doubleclick.net/ads?x=1"></iframe>'
               '</body></html>')
    # A player configuration, which is the shape nearly every library shares —
    # and deliberately no <video> element, because these players build one at
    # run time and there is nothing in the served markup to find.
    player = ('<html><body><script>jwplayer("v").setup({{'
              ' file: "{media}", image: "/poster.jpg" }});</script></body></html>')

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.do_GET(head=True)

        def do_GET(self, head=False):
            host = self.headers.get("Host", "")
            if self.path.startswith("/watch"):
                body = wrapper.format(player=f"http://{host}/embed/9182").encode()
                kind = "text/html"
            elif self.path.startswith("/embed"):
                body = player.format(media=f"http://{host}/hls/master.m3u8").encode()
                kind = "text/html"
            elif self.path.endswith(".m3u8"):
                body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n360.m3u8\n"
                kind = "application/vnd.apple.mpegurl"
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        client = HttpClient(NetworkProfile())
        info = GenericExtractor(client, {}).extract(f"{base}/watch/ep-1")
        check("the wrapper page's own title is kept",
              info.title == "Episode 1", info.title)
        check("the media behind the embed is found",
              len(info.formats) == 1, str(len(info.formats)))
        found = info.formats[0]
        check("and it is the playlist, recognised as HLS",
              found.protocol == "m3u8" and found.url.endswith("master.m3u8"),
              f"{found.protocol} {found.url}")
        check("found through the player's configuration, not the markup",
              "config" in found.note, found.note)

        # An advertising iframe sits beside the real one on every such page.
        # Following it wastes a request and can only ever return junk.
        embeds = GenericExtractor._embeds(
            wrapper.format(player=f"{base}/embed/1"), base)
        check("advertising embeds are not followed",
              all("doubleclick" not in url for url in embeds), str(embeds))
        check("the real embed still is",
              any(url.endswith("/embed/1") for url in embeds), str(embeds))
    finally:
        server.shutdown()


def test_a_stream_is_named_after_its_media_not_its_playlist() -> None:
    """A download taken from a manifest must not be called ``.m3u8``.

    Capturing what a player fetches means the URL handed to the engine is very
    often a playlist, and the title taken from it already ends in ``.m3u8``.
    Appending the format's extension produced ``stream.m3u8.m3u8`` — a correct
    MPEG-TS file that no player opens by name. What lands on disk is the media
    the playlist described, so that is what it is named after: the segments say
    whether they concatenate into a transport stream or an MP4.
    """
    print("\n[a stream is named after its media]")
    from ixd.core.models import MediaFormat, MediaInfo, MediaSegment
    from ixd.extractors import output_extension, suggested_filename

    ts = [MediaSegment(index=0, url="https://cdn/x/seg-00001.ts")]
    fragmented = [MediaSegment(index=0, url="https://cdn/x/chunk-1-00001.m4s")]

    def named(title: str, ext: str, segments=None) -> str:
        return suggested_filename(MediaInfo(title=title, formats=[]),
                                  MediaFormat("f", "u", ext=ext), segments)

    check("TS segments make a transport stream",
          named("movie.m3u8", "m3u8", ts) == "movie.ts",
          named("movie.m3u8", "m3u8", ts))
    check("fragmented-MP4 segments make an MP4",
          named("movie.m3u8", "m3u8", fragmented) == "movie.mp4",
          named("movie.m3u8", "m3u8", fragmented))
    check("a DASH manifest is never the file extension",
          named("Some Film", "mpd", fragmented) == "Some Film.mp4",
          named("Some Film", "mpd", fragmented))
    check("the playlist suffix is not doubled",
          ".m3u8" not in named("stream.m3u8", "m3u8", ts),
          named("stream.m3u8", "m3u8", ts))
    check("an ordinary format is left exactly as it was",
          named("Some Film", "mp4") == "Some Film.mp4")
    check("a title that already carries its extension keeps just one",
          named("track.mp3", "mp3") == "track.mp3", named("track.mp3", "mp3"))
    check("with nothing to go on, MP4 is the assumption",
          output_extension(MediaFormat("f", "u", ext="m3u8")) == "mp4")


def test_analysis_does_not_download() -> None:
    """Analysing a link must inspect it, not transfer it.

    This is a regression guard for a real defect: the page scraper used to read
    the response body of *any* URL it was handed, so pressing "Analyse" on a
    direct file link downloaded the file — the interface said "analysing" while
    the disk filled up.
    """
    print("\n[analysis does not transfer the file]")
    from tests.fixtures import TestOrigin
    from ixd.core.http_client import HttpClient
    from ixd.core.net import NetworkProfile

    payload = b"\x00" * (6 << 20)      # 6 MiB of non-HTML
    with TestOrigin(payload) as origin:
        client = HttpClient(NetworkProfile())

        # 1. A binary the URL does not advertise as media.
        url = origin.url("/installer.bin")
        info = GenericExtractor(client).extract(url)
        check("binary recognised without a transfer",
              origin.state.bytes_served < 4096,
              f"{origin.state.bytes_served} bytes served")
        check("reported as a single direct stream",
              len(info.formats) == 1 and info.formats[0].format_id == "direct",
              str([f.format_id for f in info.formats]))
        check("size discovered from the probe",
              info.formats[0].filesize == len(payload),
              str(info.formats[0].filesize))
        check("not mislabelled as playable media",
              not info.formats[0].has_video and not info.formats[0].has_audio,
              f"{info.formats[0].vcodec}/{info.formats[0].acodec}")

        # 2. An actual page still gets scraped.
        origin.state.bytes_served = 0
        origin.state.routes["/page.html"] = (
            b'<html><head><title>Clip</title></head><body>'
            b'<video src="/movie.mp4"></video></body></html>',
            "text/html; charset=utf-8",
        )
        page = GenericExtractor(client).extract(origin.url("/page.html"))
        check("HTML pages are still scraped", len(page.formats) == 1,
              str([f.url for f in page.formats]))
        check("scraped page title read", page.title == "Clip", page.title)

        # 3. A page-typed body larger than the cap is refused, not swallowed.
        origin.state.content_type = "text/html"
        origin.state.bytes_served = 0
        try:
            GenericExtractor(client).extract(origin.url("/huge.html"))
            check("oversized document refused", False, "no error raised")
        except ExtractionError as exc:
            check("oversized document refused", "too large" in str(exc), str(exc))
        check("oversized document not transferred",
              origin.state.bytes_served < 4096,
              f"{origin.state.bytes_served} bytes served")


def test_youtube_session_warmup() -> None:
    """The watch page must never be requested with an empty cookie jar.

    Measured against the live site: a cold jar is answered with HTTP 429 on the
    *first* request, which is not a rate limit in any useful sense — a browser
    simply never arrives without the cookies a previous visit set. Fetching the
    home page first turns that into a normal 200. This asserts the ordering,
    and that a caller-supplied browser session is not pointlessly re-warmed.
    """
    print("\n[YouTube session warm-up]")
    from ixd.core.http_client import CookieJar
    from ixd.extractors.youtube import YouTubeExtractor

    class FakeClient:
        def __init__(self, cookie: str = "") -> None:
            self.requested: list[str] = []
            self.cookies = CookieJar()
            if cookie:
                self.cookies.load_header(cookie, "youtube.com")

        def get_text(self, url, headers=None, limit=0, **kwargs):
            self.requested.append(url)
            self.headers = headers or {}
            return "<html></html>"

    client = FakeClient()
    extractor = YouTubeExtractor(client)
    extractor._watch_page("abcdefghijk")
    check("home page is fetched first", len(client.requested) == 2,
          str(client.requested))
    if len(client.requested) == 2:
        check("warm-up targets the site root",
              client.requested[0] == "https://www.youtube.com/", client.requested[0])
        check("then the watch page",
              client.requested[1].endswith("watch?v=abcdefghijk"), client.requested[1])
    check("request looks like a browser navigation",
          client.headers.get("Sec-Fetch-Mode") == "navigate"
          and "Upgrade-Insecure-Requests" in client.headers,
          str(sorted(client.headers)))

    # A second page in the same extraction must not warm up again.
    before = len(client.requested)
    extractor._pages.clear()
    extractor._watch_page("bcdefghijkl")
    check("warm-up happens once per extraction",
          len(client.requested) - before == 1, str(client.requested[before:]))

    # A caller that already supplied the user's browser session needs no warm-up.
    warm = FakeClient(cookie="__Secure-YNID=abc; VISITOR_INFO1_LIVE=xyz")
    YouTubeExtractor(warm)._watch_page("abcdefghijk")
    check("a supplied browser session is used as-is",
          len(warm.requested) == 1 and warm.requested[0].endswith("watch?v=abcdefghijk"),
          str(warm.requested))


def test_youtube_restricted_urls_rank_last() -> None:
    """A stream that cannot be fetched in full must not win "best available".

    ``ratebypass=yes`` is what decides it: measured against a live video, the
    URL carrying it answers at every offset from head to tail, while one
    without it is refused about a third of the way in. A lower-quality stream
    that can actually be downloaded is worth more than a higher-quality one
    that cannot.
    """
    print("\n[YouTube restricted-stream ranking]")
    from ixd.extractors.youtube import CLIENTS, YouTubeExtractor

    client = CLIENTS[0]
    extractor = YouTubeExtractor(client=None)      # no requests are made here
    streaming = {
        "formats": [
            {"itag": 18,
             "url": "https://rr1.googlevideo.com/videoplayback?itag=18&ratebypass=yes",
             "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"',
             "height": 360, "bitrate": 500000, "contentLength": "1000"},
        ],
        "adaptiveFormats": [
            {"itag": 137,
             "url": "https://rr1.googlevideo.com/videoplayback?itag=137&gir=yes",
             "mimeType": 'video/mp4; codecs="avc1.640028"',
             "height": 1080, "bitrate": 4000000, "contentLength": "9000"},
        ],
    }
    formats = extractor._formats_from(streaming, "abcdefghijk", client)
    check("both streams parsed", len(formats) == 2, str(len(formats)))
    if len(formats) == 2:
        check("the unrestricted stream sorts first",
              formats[0].format_id == "18", formats[0].format_id)
        check("the restricted one is flagged and labelled",
              formats[1].restricted and "partial access" in formats[1].note,
              formats[1].note)
        check("the unrestricted one is not flagged",
              not formats[0].restricted, formats[0].note)

    from ixd.extractors.base import select_format
    chosen = select_format(formats, "1080p", prefer_progressive=False)
    check("selection avoids the restricted stream",
          chosen is not None and chosen.format_id == "18",
          chosen.format_id if chosen else "none")

    # When every option is restricted there is nothing to fall back to, and
    # starting a transfer that cannot finish is worse than refusing it.
    only_restricted = [f for f in formats if f.restricted]
    fallback = select_format(only_restricted, "1080p", prefer_progressive=False)
    check("a restricted stream is still returned when it is the only option",
          fallback is not None and fallback.restricted,
          fallback.format_id if fallback else "none")


#: The audio half of a real response, unmodified.
#:
#: Every entry reads ``itag 140``. What tells them apart is the tag string,
#: and the site lists its machine dubbings *before* the original — which is
#: exactly why anything keyed on the itag keeps German and discards English.
#:
#: Taken from the player response for the video the user reported: eleven
#: entries, eight auto-dubbed languages and the original published three times
#: over — plain, loudness-compressed (``drc``) and volume-boosted (``vb``).
#: Their bitrates sit within 300 bits per second of each other, so nothing that
#: falls through to a bitrate tie-break can pick the right one except by luck.
_REAL_AUDIO_TRACKS: tuple[tuple[str, dict | None, int], ...] = (
    ("ChQKBWFjb250EgtkdWJiZWQtYXV0bwoNCgRsYW5nEgVkZS1ERQ",
     {"displayName": "German (DE)", "id": "de-DE.10",
      "audioIsDefault": False, "isAutoDubbed": True}, 131667),
    ("ChQKBWFjb250EgtkdWJiZWQtYXV0bwoNCgRsYW5nEgVlcy1VUw",
     {"displayName": "Spanish (US)", "id": "es-US.10",
      "audioIsDefault": False, "isAutoDubbed": True}, 131663),
    ("ChQKBWFjb250EgtkdWJiZWQtYXV0bwoKCgRsYW5nEgJoaQ",
     {"displayName": "Hindi", "id": "hi.10",
      "audioIsDefault": False, "isAutoDubbed": True}, 131579),
    ("ChQKBWFjb250EgtkdWJiZWQtYXV0bwoKCgRsYW5nEgJpdA",
     {"displayName": "Italian", "id": "it.10",
      "audioIsDefault": False, "isAutoDubbed": True}, 131676),
    ("ChQKBWFjb250EgtkdWJiZWQtYXV0bwoNCgRsYW5nEgVubC1OTA",
     {"displayName": "Dutch (NL)", "id": "nl-NL.10",
      "audioIsDefault": False, "isAutoDubbed": True}, 131536),
    ("ChQKBWFjb250EgtkdWJiZWQtYXV0bwoKCgRsYW5nEgJwbA",
     {"displayName": "Polish", "id": "pl.10",
      "audioIsDefault": False, "isAutoDubbed": True}, 131669),
    ("ChQKBWFjb250EgtkdWJiZWQtYXV0bwoNCgRsYW5nEgVwdC1CUg",
     {"displayName": "Portuguese (BR)", "id": "pt-BR.10",
      "audioIsDefault": False, "isAutoDubbed": True}, 131564),
    ("ChQKBWFjb250EgtkdWJiZWQtYXV0bwoKCgRsYW5nEgJ1aw",
     {"displayName": "Ukrainian", "id": "uk.10",
      "audioIsDefault": False, "isAutoDubbed": True}, 131542),
    ("ChEKBWFjb250EghvcmlnaW5hbAoICgNkcmMSATEKDQoEbGFuZxIFZW4tVVM",
     {"displayName": "English (US) original", "id": "en-US.4",
      "audioIsDefault": True}, 131429),
    ("ChEKBWFjb250EghvcmlnaW5hbAoNCgRsYW5nEgVlbi1VUw",
     {"displayName": "English (US) original", "id": "en-US.4",
      "audioIsDefault": True}, 131400),
    ("ChEKBWFjb250EghvcmlnaW5hbAoNCgRsYW5nEgVlbi1VUwoHCgJ2YhIBMQ",
     {"displayName": "English (US) original", "id": "en-US.4",
      "audioIsDefault": True}, 131470),
)

#: The tag string of the track the user was actually given, three times over.
_THE_GERMAN_DUB = _REAL_AUDIO_TRACKS[0][0]
#: The one that should have been chosen: the plain original, no processing.
_THE_ENGLISH_ORIGINAL = _REAL_AUDIO_TRACKS[9][0]


def _dubbed_response() -> dict:
    """A watch-page response for a video with machine dubbings."""
    response = _sabr_only_response()
    adaptive = [entry for entry in response["streamingData"]["adaptiveFormats"]
                if not str(entry.get("mimeType", "")).startswith("audio/")]
    for xtags, track, bitrate in _REAL_AUDIO_TRACKS:
        adaptive.append({
            "itag": 140, "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
            "bitrate": bitrate, "contentLength": "9000000",
            "xtags": xtags, "audioTrack": track,
        })
    response["streamingData"]["adaptiveFormats"] = adaptive
    return response


def test_every_audio_language_survives_extraction() -> None:
    """The original must reach the ranking, not be discarded before it.

    This is the German-soundtrack defect, and it was never the ranking. An
    itag names a rendition — "128k AAC" — and every audio language of a dubbed
    video is published under that same itag. Extraction deduplicated its
    collected formats on ``format_id-height-acodec-source``; for audio the
    height is zero and the codec is shared, so all eleven entries collapsed to
    whichever came first. The site lists its dubbings first, so the English
    original was thrown away *before* any code that prefers it ever ran.

    That is why two sessions of ranking fixes changed nothing: they were
    sorting a list the right answer had already been removed from. The rule
    they encode is correct and is asserted below — but it can only be reached
    if every track survives to be ranked, which is what this test pins.
    """
    print("\n[every audio language survives extraction]")
    import copy

    from ixd.extractors.base import best_muxable_audio
    from ixd.service import DownloadService

    class PageOnly(YouTubeExtractor):
        def _player_response_from_page(self, video_id, refresh=False):
            return copy.deepcopy(_dubbed_response())

        def _call_player(self, video_id, client, *args, **kwargs):
            raise RuntimeError("Sign in to confirm you're not a bot")

    info = PageOnly(client=None, options={}).extract(
        "https://www.youtube.com/watch?v=abcdefghijk")
    audio = [media for media in info.formats
             if media.has_audio and not media.has_video]

    check("every published track survives, not just the first",
          len(audio) == len(_REAL_AUDIO_TRACKS),
          f"{len(audio)} of {len(_REAL_AUDIO_TRACKS)}")
    languages = {media.audio_language for media in audio}
    check("the original's language is among them", "en-US" in languages,
          str(sorted(languages)))
    check("and so is the dub that was being handed over", "de-DE" in languages,
          str(sorted(languages)))

    # Now the ranking, on the real list rather than a constructed one.
    video = next(media for media in info.formats if media.height == 1080)
    chosen = best_muxable_audio(info.formats, video)
    check("the track paired with the video is the original",
          chosen is not None and chosen.audio_tags == _THE_ENGLISH_ORIGINAL,
          f"{chosen.audio_language} {chosen.audio_kind} "
          f"{chosen.audio_variant or 'plain'}" if chosen else "none")
    check("which is not the German dub the user was given",
          chosen is not None and chosen.audio_tags != _THE_GERMAN_DUB)
    check("and is the plain mix, not the compressed or boosted one",
          chosen is not None and chosen.audio_variant == "",
          chosen.audio_variant if chosen else "none")

    # The panel collapses audio to one row. It used to choose that row by file
    # size — and every language of a dubbed video is the same recording
    # re-voiced, so the sizes match and the language was decided by rounding.
    shown = DownloadService.presentable_formats(info.formats)
    offered = next(media for media in shown
                   if media.has_audio and not media.has_video)
    check("the single audio entry offered is the original too",
          offered.audio_tags == _THE_ENGLISH_ORIGINAL,
          f"{offered.audio_language} {offered.audio_kind}")

    # A stream is identified by its track as well as its itag, everywhere. A
    # header fetched for one language and written in front of another's media
    # produces a file that no player blames on the language.
    from ixd.extractors.youtube import _stream_identity

    identities = {_stream_identity(media) for media in audio}
    check("each track is its own stream, not one stream eleven times",
          len(identities) == len(audio),
          f"{len(identities)} identities for {len(audio)} tracks")


def _sabr_only_response(length_seconds: str = "600") -> dict:
    """A player response shaped the way YouTube now answers a watch page.

    One fetchable URL, for the 360p progressive stream, and every rendition
    above it published only through the streaming endpoint.
    """
    import base64 as _base64

    return {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"title": "Test", "lengthSeconds": length_seconds},
        "playerConfig": {"mediaCommonConfig": {"mediaUstreamerRequestConfig": {
            "videoPlaybackUstreamerConfig":
                _base64.urlsafe_b64encode(b"ustreamer-config").decode()}}},
        "streamingData": {
            "serverAbrStreamingUrl":
                "https://rr1.googlevideo.com/videoplayback?sabr=1",
            "formats": [
                {"itag": 18,
                 "url": "https://rr1.googlevideo.com/videoplayback"
                        "?itag=18&ratebypass=yes",
                 "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"',
                 "width": 640, "height": 360, "bitrate": 500000,
                 "qualityLabel": "360p", "contentLength": "7034721"},
            ],
            "adaptiveFormats": [
                {"itag": 137, "mimeType": 'video/mp4; codecs="avc1.640028"',
                 "width": 1920, "height": 1080, "fps": 30, "bitrate": 4000000,
                 "qualityLabel": "1080p", "contentLength": "50000000"},
                {"itag": 136, "mimeType": 'video/mp4; codecs="avc1.4d401f"',
                 "width": 1280, "height": 720, "fps": 30, "bitrate": 2000000,
                 "qualityLabel": "720p", "contentLength": "25000000"},
                {"itag": 140, "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
                 "bitrate": 128000, "contentLength": "9000000"},
            ],
        },
    }


def test_every_rendition_reaches_the_menu() -> None:
    """A complete 360p link must not hide the qualities above it.

    This is the shape of nearly every watch page now: ``streamingData``
    publishes a fetchable URL for the 360p progressive stream and nothing but
    a streaming endpoint for 720p, 1080p and the audio tracks. Server-driven
    formats were only built when *no* usable URL came back, or when every one
    that did was restricted — and that one 360p link is neither, so the higher
    renditions were dropped before the panel ever saw them and the quality
    menu offered a single entry. The renditions a direct link does not cover
    are what decides it, not whether any link came back at all.
    """
    print("\n[every rendition reaches the menu]")
    import copy

    from ixd.service import DownloadService

    def extracted(response: dict, options: dict) -> list:
        class PageOnly(YouTubeExtractor):
            """Only the watch page answers — the API clients are gated."""

            def _player_response_from_page(self, video_id, refresh=False):
                return copy.deepcopy(response)

            def _call_player(self, video_id, client, *args, **kwargs):
                raise RuntimeError("Sign in to confirm you're not a bot")

        info = PageOnly(client=None, options=options).extract(
            "https://www.youtube.com/watch?v=abcdefghijk")
        return DownloadService.presentable_formats(info.formats)

    shown = extracted(_sabr_only_response(), {})
    heights = [media.height for media in shown]
    check("every rendition on the page is offered",
          [h for h in heights if h] == [1080, 720, 360], str(heights))
    check("the audio track is offered too",
          any(media.has_audio and not media.has_video for media in shown),
          str(heights))
    check("the menu reads tallest first", heights == sorted(heights, reverse=True),
          str(heights))
    server_driven = {media.height for media in shown if media.sabr}
    check("the renditions with no link of their own are server-driven",
          server_driven >= {1080, 720}, str(sorted(server_driven)))
    check("the one rendition with a link keeps it",
          next(m for m in shown if m.height == 360).sabr == {},
          str(next(m for m in shown if m.height == 360).sabr))

    # Without a proof of origin the streaming session is stopped about a
    # minute in, so the taller renditions are offered but honestly labelled.
    partial = [media.height for media in shown if media.restricted]
    check("an unattested session marks them partial", 1080 in partial, str(partial))

    attested = extracted(_sabr_only_response(), {"po_token": "a-proof-of-origin"})
    check("a proof of origin makes them whole",
          not any(media.restricted for media in attested),
          str([(m.height, m.restricted) for m in attested]))

    # A video short enough to fit inside one session needs no proof.
    brief = extracted(_sabr_only_response("20"), {})
    check("a short video needs no proof",
          not any(media.restricted for media in brief),
          str([(m.height, m.restricted) for m in brief]))


def test_the_walk_does_not_stop_at_the_first_whole_stream() -> None:
    """Stopping at a complete 360p copy is what emptied the quality menu.

    The client walk used to end as soon as any complete stream was in hand,
    and the 360p progressive one is complete on virtually every video — so
    the identities that publish the taller renditions were never asked. It
    now stops only when the *tallest* rendition can be had whole, and not
    when the only thing offering it is the watch page's own streaming
    session, which the streaming server refuses outright.
    """
    print("\n[the walk does not stop at the first whole stream]")
    import copy

    asked: list[str] = []

    def walk(response: dict) -> list:
        class Counting(YouTubeExtractor):
            def _player_response_from_page(self, video_id, refresh=False):
                asked.append("watch-page")
                return copy.deepcopy(response)

            def _call_player(self, video_id, client, *args, **kwargs):
                asked.append(client.key)
                return copy.deepcopy(response)

        return Counting(client=None, options={}).extract(
            "https://www.youtube.com/watch?v=abcdefghijk").formats

    walk(_sabr_only_response())
    check("a whole 360p copy does not end the walk", len(asked) > 1, str(asked))
    # The identities are asked concurrently, and *after* the watch page. Both
    # halves matter: asking them at once is what took the wait before the
    # quality menu from 12.2 seconds to 5.4, and asking them before the page
    # would arrive cold — which is answered HTTP 429 on the very first request
    # — and would use client versions that had not been re-stamped from the
    # live page config, which is what stops them ageing out.
    check("no identity is asked before the watch page",
          "watch-page" in asked and all(
              asked.index("watch-page") < asked.index(name)
              for name in set(asked) if name != "watch-page"),
          str(asked))

    check("the walk starts at the watch page", asked[0] == "watch-page", str(asked))

    # When the page does publish a complete link for every rendition there is
    # nothing left to look for, and asking further identities is waste.
    whole = _sabr_only_response()
    del whole["streamingData"]["serverAbrStreamingUrl"]
    for entry in whole["streamingData"]["adaptiveFormats"]:
        entry["url"] = ("https://rr1.googlevideo.com/videoplayback"
                        f"?itag={entry['itag']}&ratebypass=yes")
    asked.clear()
    formats = walk(whole)
    check("a fully served page is asked once", asked == ["watch-page"], str(asked))
    check("and every rendition is still there",
          {f.height for f in formats} == {1080, 720, 360, 0},
          str(sorted(f.height for f in formats)))


def test_protobuf() -> None:
    """The hand-written protobuf codec must round-trip and match the spec."""
    print("\n[protobuf codec]")
    from ixd.core.protobuf import Message, encode_varint, parse

    # Values from the Protocol Buffers encoding documentation.
    check("varint 1", encode_varint(1) == b"\x01")
    check("varint 300", encode_varint(300) == b"\xac\x02", encode_varint(300).hex())
    check("varint 0", encode_varint(0) == b"\x00")
    check("varint 127 is one byte", encode_varint(127) == b"\x7f")
    check("varint 128 is two bytes", encode_varint(128) == b"\x80\x01")

    built = (Message()
             .varint(1, 137)
             .varint(2, 1785049552865902)
             .string(3, "tag")
             .raw(4, b"\x00\xff")
             .message(5, Message().varint(1, 9))
             .boolean(6, True))
    fields = parse(built.to_bytes())
    check("varint field round-trips", fields[1] == 137, str(fields.get(1)))
    check("large varint round-trips", fields[2] == 1785049552865902, str(fields.get(2)))
    check("string round-trips", fields[3] == b"tag", str(fields.get(3)))
    check("bytes round-trip", fields[4] == b"\x00\xff", str(fields.get(4)))
    check("nested message round-trips", parse(fields[5])[1] == 9, str(fields.get(5)))
    check("boolean round-trips", fields[6] == 1, str(fields.get(6)))

    repeated = Message().varint(1, 1).varint(1, 2).varint(1, 3)
    check("repeated fields collapse to a list",
          parse(repeated.to_bytes())[1] == [1, 2, 3],
          str(parse(repeated.to_bytes()).get(1)))

    check("a None value writes nothing", len(Message().varint(1, None)) == 0)


def test_sabr_framing() -> None:
    """UMP framing and media reassembly, against a constructed response.

    The UMP varint is *not* the protobuf one — its width lives in the leading
    bits of the first byte. Reading it with a protobuf reader appears to work
    for small values and then silently drifts, so both widths are pinned here.
    """
    print("\n[server-driven streaming: framing]")
    from ixd.core.protobuf import Message
    from ixd.extractors.sabr import (
        PART_MEDIA,
        PART_MEDIA_HEADER,
        SabrFormat,
        SabrStream,
        iter_parts,
        read_varint,
    )

    check("1-byte varint", read_varint(b"\x33", 0) == (51, 1))
    check("2-byte varint", read_varint(b"\xb4\x02", 0) == (180, 2),
          str(read_varint(b"\xb4\x02", 0)))
    check("2-byte varint is not the protobuf reading",
          read_varint(b"\xb4\x02", 0)[0] != 308)
    check("3-byte varint",
          read_varint(b"\xc0\x01\x00", 0) == ((0xC0 & 0x1F) | (1 << 5), 3),
          str(read_varint(b"\xc0\x01\x00", 0)))

    def ump(part_type: int, payload: bytes) -> bytes:
        # Sizes here stay under 128, so the single-byte form is correct.
        return bytes([part_type, len(payload)]) + payload

    # ---- the same framing, arriving a few bytes at a time ---------------
    #
    # The transfer reads a reply incrementally so that each block reaches the
    # file as it arrives. Buffering the whole reply first meant no progress was
    # recorded for as long as the reply took to arrive and then all of it at
    # once — a transfer that was moving the entire time but showed as stalling
    # every few seconds, which is what was reported. Splitting a reply at every
    # possible boundary is what proves the incremental reader.
    from ixd.extractors.sabr import iter_parts_streaming, varint_width

    check("varint widths are agreed between the two readers",
          all(varint_width(first) == read_varint(bytes([first]) + b"\x00" * 4, 0)[1]
              for first in (0x00, 0x33, 0x7F, 0x80, 0xB4, 0xC0, 0xE0, 0xF0)))

    long_part = bytes(range(256)) * 3          # forces a multi-byte length
    # The 2-byte form: the leading bits mark the width, the low six bits of the
    # first byte carry the bottom of the value and the second byte the rest.
    long_header = bytes([PART_MEDIA,
                         0x80 | (len(long_part) & 0x3F), len(long_part) >> 6])
    whole = (ump(PART_MEDIA_HEADER, b"abc")
             + long_header + long_part
             + ump(PART_MEDIA, b"tail"))
    expected = list(iter_parts(whole))
    check("the constructed reply parses whole", len(expected) == 3,
          str([(t, len(p)) for t, p in expected]))

    for piece_size in (1, 2, 3, 5, 7, 64, 1000):
        pieces = [whole[i:i + piece_size] for i in range(0, len(whole), piece_size)]
        got = list(iter_parts_streaming(iter(pieces)))
        check(f"streamed in {piece_size}-byte pieces gives the same parts",
              got == expected,
              f"{[(t, len(p)) for t, p in got]}")

    # A reply that stops mid-part is a broken reply, and has to say so rather
    # than hand back a short block as though it were complete.
    truncated = whole[:len(whole) - 3]
    failed = False
    try:
        list(iter_parts_streaming(iter([truncated])))
    except ValueError:
        failed = True
    check("a reply cut short is refused, not silently truncated", failed)

    header = (Message().varint(1, 7).varint(3, 134)
              .varint(6, 1181).varint(9, 4)
              .message(15, Message().varint(1, 0).varint(2, 81920).varint(3, 15360)))
    body = b"abcdefghij"
    response = ump(PART_MEDIA_HEADER, header.to_bytes()) + \
        ump(PART_MEDIA, bytes([7]) + body)

    kinds = [part_type for part_type, _ in iter_parts(response)]
    check("parts split cleanly", kinds == [PART_MEDIA_HEADER, PART_MEDIA], str(kinds))

    stream = SabrStream(None, "https://example.invalid/stream", b"cfg",
                        SabrFormat(itag=134, last_modified=1, size=len(body)))
    written: dict[int, bytes] = {}
    result = stream._consume(response, lambda offset, data: written.update({offset: data}))
    check("media lands at the offset its header names",
          written == {1181: body}, str(written))
    check("byte count reported", result.bytes_written == len(body),
          str(result.bytes_written))
    check("sequence number tracked", result.sequence == 4, str(result.sequence))
    check("player clock derived from the time range",
          result.player_time_ms == int(81920 * 1000 / 15360),
          str(result.player_time_ms))

    # A header for a different stream must be ignored entirely.
    other = (Message().varint(1, 8).varint(3, 251).varint(6, 0)).to_bytes()
    mixed = ump(PART_MEDIA_HEADER, other) + ump(PART_MEDIA, bytes([8]) + b"zzzz")
    written.clear()
    ignored = stream._consume(mixed, lambda offset, data: written.update({offset: data}))
    check("another stream's media is not written",
          not written and ignored.bytes_written == 0, str(written))

    # The initialisation segment, which is what the field log of 2026-08-12
    # was missing: 20,391,931 bytes delivered — 100% of the media — and 965
    # bytes absent *at byte zero*, four attempts running.
    #
    # It is announced with `is_init_seg` and without repeating the itag, and
    # the itag filter above therefore dropped the header. A body is only
    # placeable through the header that precedes it, so the segment went with
    # it, silently, and the file was refused at the end for the want of it.
    said: list[str] = []
    opening = SabrStream(None, "https://example.invalid/stream", b"cfg",
                         SabrFormat(itag=134, last_modified=1, size=4000),
                         log=said.append)
    init_header = (Message().varint(1, 3).varint(6, 0).varint(8, 1)).to_bytes()
    # Kept under 128 bytes because `ump` above writes single-byte lengths; the
    # segment's size is not what is under test, only where it lands.
    init = ump(PART_MEDIA_HEADER, init_header) + \
        ump(PART_MEDIA, bytes([3]) + b"\x00" * 100)
    written.clear()
    got = opening._consume(init, lambda offset, data: written.update({offset: data}))
    check("an initialisation segment with no itag is written, not dropped",
          list(written) == [0] and len(written[0]) == 100, str(list(written)))
    check("and counted", got.bytes_written == 100, str(got.bytes_written))
    check("and described in the log, so a gap at zero is diagnosable",
          any("initialisation segment" in line for line in said), str(said))

    # A named mismatch is still a mismatch: the relaxation is for an omitted
    # itag, not for a header that says it belongs to something else.
    said.clear()
    foreign = (Message().varint(1, 4).varint(3, 140).varint(6, 0)
               .varint(8, 1)).to_bytes()
    written.clear()
    opening._consume(ump(PART_MEDIA_HEADER, foreign)
                     + ump(PART_MEDIA, bytes([4]) + b"xxxx"),
                     lambda offset, data: written.update({offset: data}))
    check("another stream's initialisation segment is still refused",
          not written, str(written))
    check("…and the refusal is reported rather than silent",
          any("only 134 is kept" in line for line in said), str(said))
    # Once per foreign itag, not once per segment: one download put sixty
    # identical lines in the log, which buries the two that matter.
    opening._consume(ump(PART_MEDIA_HEADER, foreign)
                     + ump(PART_MEDIA, bytes([4]) + b"xxxx"),
                     lambda offset, data: written.update({offset: data}))
    check("…and said once, however many segments arrive",
          len([line for line in said if "is kept" in line]) == 1, str(said))

    # The format description — UMP part 42, 82 bytes, ten times a session in
    # the field log of 2026-08-12. It carries no media: it names the byte
    # ranges this stream's opening occupies, and the session then begins at the
    # byte immediately after them. That is the missing 965 bytes stated by the
    # server itself, so it is read rather than counted.
    from ixd.extractors.sabr import PART_FORMAT_INIT

    said.clear()
    description = (Message()
                   .message(2, Message().varint(1, 134))
                   .raw(5, b'video/mp4; codecs="avc1.640028"')
                   .message(6, Message().varint(1, 0).varint(2, 964))
                   .message(7, Message().varint(1, 965).varint(2, 1180)))
    opening._consume(ump(PART_FORMAT_INIT, description.to_bytes()),
                     lambda offset, data: None)
    check("the stream's initialisation range is read from the description",
          opening.init_range == (0, 964), str(opening.init_range))
    check("and its index range with it",
          opening.index_range == (965, 1180), str(opening.index_range))
    check("and it is stated plainly, including that none of it is sent",
          any("sends none of it" in line for line in said), str(said))

    # Another stream's description must not move this one's opening.
    opening._consume(
        ump(PART_FORMAT_INIT,
            (Message().message(2, Message().varint(1, 251))
             .message(6, Message().varint(1, 0).varint(2, 500))).to_bytes()),
        lambda offset, data: None)
    check("another stream's description is ignored",
          opening.init_range == (0, 964), str(opening.init_range))

    # A shape this application does not know is reported as it arrived rather
    # than guessed at: a wrong reading here places a file's opening at an
    # offset that is merely plausible.
    said.clear()
    unknown = SabrStream(None, "https://example.invalid/stream", b"cfg",
                         SabrFormat(itag=134, last_modified=1, size=10),
                         log=said.append)
    unknown._consume(ump(PART_FORMAT_INIT,
                         Message().varint(99, 1).to_bytes()),
                     lambda offset, data: None)
    check("an unrecognised description is reported, not assumed",
          unknown.init_range is None
          and any("does not know" in line for line in said), str(said))


def test_the_published_index_decides_where_to_continue() -> None:
    """A byte is turned into a position by the stream's own index, not a guess.

    Positions in this protocol are *times*. Converting a byte offset to one by
    dividing the running time by the length is right only at constant bitrate,
    and real media is not: measured against a live video the estimate was 2.3
    seconds of playback out, which on a long file is megabytes. A session asked
    to continue at byte 95,499,262 delivered from 103,813,481 — past the gap it
    was sent to fill — and no repetition helps, because the same estimate gives
    the same answer.

    The stream publishes the exact mapping in the ``sidx`` of the header that
    is fetched before any media. This checks the parser against a real box and
    the seek against the index it produces.
    """
    print("\n[the published index decides where to continue]")
    import struct

    from ixd.core.mp4 import parse_sidx
    from ixd.extractors.sabr import SabrFormat, SabrStream

    # Equal-sized pieces of unequal duration: variable bitrate, in the plainest
    # form, and the case a linear estimate cannot get right.
    sizes = [100_000, 100_000, 100_000, 100_000]
    durations = [4000, 1000, 4000, 1000]
    references = b"".join(
        struct.pack(">III", size, duration, 0x90000000)
        for size, duration in zip(sizes, durations))
    payload = (struct.pack(">B", 0) + b"\x00\x00\x00"
               + struct.pack(">I", 1) + struct.pack(">I", 1000)
               + struct.pack(">I", 0) + struct.pack(">I", 0)
               + struct.pack(">HH", 0, len(sizes)) + references)
    sidx = struct.pack(">I", len(payload) + 8) + b"sidx" + payload
    header = struct.pack(">I", 16) + b"ftyp" + b"iso5" + b"\x00" * 4 + sidx

    index = parse_sidx(header)
    check("every reference is read", len(index) == 4, str(len(index)))
    check("the first piece starts where the box ends",
          index[0][0] == len(header), f"{index[0][0]} vs {len(header)}")
    check("times accumulate from the declared durations",
          [entry[1] for entry in index] == [0, 4000, 5000, 9000],
          str([entry[1] for entry in index]))
    check("a header with no sidx yields nothing, rather than a wrong answer",
          parse_sidx(b"\x00\x00\x00\x10ftypisom\x00\x00\x00\x00") == [])

    total = len(header) + sum(sizes)
    stream = SabrStream(None, "https://x", b"",
                        SabrFormat(itag=1, last_modified=0, size=total),
                        duration_ms=sum(durations))

    # Without the index: the estimate, which spreads the running time evenly
    # over the bytes and therefore lands in the wrong piece.
    stream._seek_to_byte(index[2][0])
    estimated = stream._player_time_ms
    check("the estimate misses the piece it was aimed at",
          estimated != index[2][1],
          f"estimate {estimated} happened to equal the true {index[2][1]}")

    # With it: the published start of the piece that byte is inside.
    stream.index = index
    for position, (offset, start_ms, _duration) in enumerate(index):
        stream._seek_to_byte(offset)
        check(f"piece {position} is asked for at its published start",
              stream._player_time_ms == start_ms,
              f"{stream._player_time_ms} vs {start_ms}")

    # A byte in the middle of a piece asks from that piece's start, never the
    # next one — asking from the end would skip exactly what is wanted.
    stream._seek_to_byte(index[1][0] + 50_000)
    check("a byte inside a piece asks from that piece's beginning",
          stream._player_time_ms == index[1][1],
          f"{stream._player_time_ms} vs {index[1][1]}")


def test_a_session_context_is_handed_back() -> None:
    """The server issues a context mid-session and expects it on every request.

    Windows field log, 2026-08-13, on a video of 56,141,099 bytes:

        the server sent a part this application does not read: type 57, 92 bytes
        the server sent a part this application does not read: type 67, 2 bytes
        parts this application does not read: type 57 ×2, type 67 ×2
        the streaming endpoint answered 31 bytes … first bytes 2c 1d 0a 15
        73 61 62 72 2e 6d 61 6c 66 6f 72 6d
        Failed: the streaming server stopped after 0 of 56,141,099 bytes (0%)

    Those 31 bytes decode as a UMP part carrying `sabr.malformed_config`: the
    server judging the request against a configuration the client no longer
    matched. Type 57 is that configuration being changed — twice — and nothing
    here read it, so every request after it described a session that had moved
    on without us.

    A session that is never sent one behaves exactly as before, which is why
    this is safe to send: the contexts are echoed only when they exist.
    """
    print("\n[a session context is handed back]")
    from ixd.core.protobuf import Message, parse
    from ixd.extractors.sabr import (PART_SABR_CONTEXT_UPDATE,
                                     PART_SABR_CONTEXT_SENDING_POLICY,
                                     SabrFormat, SabrStream, part_name)

    stream = SabrStream(None, "https://example.invalid/s", b"config",
                        SabrFormat(itag=137, last_modified=1, size=10))

    before = parse(stream._build_request())
    check("a session with no context sends none",
          5 not in parse(before[19]), str(sorted(parse(before[19]))))

    # As the server sends it: type in field 1, the opaque value in field 3.
    update = (Message().varint(1, 4)
              .varint(2, 1)
              .raw(3, b"\x0a\x09opaque-ctx")
              .to_bytes())
    stream._read_context_update(update)
    check("the context is kept", stream.sabr_contexts.get(4) == b"\x0a\x09opaque-ctx",
          str(stream.sabr_contexts))

    after = parse(stream._build_request())
    streamer = parse(after[19])
    check("and goes back inside the streamer context", 5 in streamer,
          str(sorted(streamer)))
    entry = parse(streamer[5])
    check("named by the type the server gave it", entry.get(1) == 4, str(entry))
    check("carrying the value unchanged",
          entry.get(2) == b"\x0a\x09opaque-ctx", str(entry))

    # Two contexts, both sent, and the player's own captured streamer context
    # kept whole — repeated fields concatenate, so it is appended rather than
    # taken apart and rebuilt.
    captured = Message().message(1, Message().varint(3, 5)).raw(2, b"po").to_bytes()
    replayed = SabrStream(None, "https://example.invalid/s", b"config",
                          SabrFormat(itag=137, last_modified=1, size=10),
                          streamer_context=captured)
    replayed._read_context_update(update)
    replayed._read_context_update(
        Message().varint(1, 7).raw(3, b"second").to_bytes())
    body = parse(replayed._build_request())
    merged = parse(body[19])
    check("the captured context survives intact", merged.get(2) == b"po", str(merged))
    check("with both session contexts appended",
          isinstance(merged.get(5), list) and len(merged[5]) == 2,
          str(merged.get(5)))

    # Withdrawn by the server: stop sending it.
    replayed._read_context_policy(Message().varint(2, 7).to_bytes())
    check("a withdrawn context is dropped", 7 not in replayed.sabr_contexts,
          str(sorted(replayed.sabr_contexts)))

    # A part in a shape this does not recognise is reported, not silently
    # counted — that is what cost the session in the field.
    lines = []
    noisy = SabrStream(None, "https://example.invalid/s", b"config",
                       SabrFormat(itag=137, last_modified=1, size=10),
                       log=lines.append)
    noisy._read_context_update(Message().varint(9, 1).to_bytes())
    check("an unrecognised shape is written down, with its fields",
          any("does not recognise" in line and "9=1" in line for line in lines),
          str(lines))

    check("and the parts have names in the log now",
          part_name(PART_SABR_CONTEXT_UPDATE).startswith("sabr context update")
          and part_name(PART_SABR_CONTEXT_SENDING_POLICY).startswith(
              "sabr context sending policy")
          and part_name(67) == "type 67",
          part_name(PART_SABR_CONTEXT_UPDATE))


def test_sabr_request_shape() -> None:
    """The request must name the stream in the right slot and carry the token.

    Both details are silent when wrong. An audio track announced in the video
    slot leaves the request self-contradictory and the server answers with no
    media at all — which looked exactly like a network problem. And without a
    proof-of-origin token the server serves about a minute and then refuses,
    reporting protection status 3, so the token has to survive the trip from
    the browser to the request body intact.
    """
    print("\n[server-driven request shape]")
    import base64

    from ixd.core.engine import _decode_po_token
    from ixd.core.protobuf import parse
    from ixd.extractors.sabr import SabrFormat, SabrStream

    token = b"\x01\x02\xfe\xffPROOF-OF-ORIGIN\x00\x07"
    video = SabrStream(None, "https://example.invalid/s", b"config",
                       SabrFormat(itag=137, last_modified=999, size=10),
                       po_token=token)
    fields = parse(video._build_request())
    check("the session configuration is sent", fields.get(5) == b"config")
    check("video is announced in the video slot", 17 in fields, str(sorted(fields)))
    check("the audio slot is left alone for video", 16 not in fields,
          str(sorted(fields)))

    streamer = parse(fields[19])
    check("the token travels in the streamer context",
          streamer.get(2) == token, str(streamer.get(2)))

    # Field 2 is the *selected* formats — what the client already holds and has
    # initialised. Naming the wanted stream there is what made the server skip
    # its initialisation segment, which cost every 1080p download on
    # 2026-08-12: 100% of the media delivered, 965 bytes missing at byte zero.
    #
    # The server's own format description said so. It initialised itag 251 —
    # never asked for — announcing `6={1=0, 2=258}` as its init range, and
    # never described 137, which the request named in field 2 *and* in the
    # preferred-video slot. It then sent 137 from sequence 1, byte 965.
    check("a fresh session does not claim to hold the stream already",
          2 not in fields, str(sorted(fields)))
    check("…while still naming it, so the server knows what to send",
          17 in fields, str(sorted(fields)))

    # A continuation does hold it, and says so: that claim is what stops the
    # server sending the whole stream again from the top.
    resuming = SabrStream(None, "https://example.invalid/s", b"config",
                          SabrFormat(itag=137, last_modified=999, size=10))
    resuming.restore([[0, 5000]], player_ms=4000, sequence=3)
    resumed = parse(resuming._build_request())
    check("a continuation that holds the opening claims it",
          2 in resumed, str(sorted(resumed)))

    # The claim is about the *opening*, not about holding anything.
    #
    # Every worker in a parallel pass is told the whole coverage map so it asks
    # only for its own stretch — so "I hold something" is true of all sixteen,
    # all sixteen claimed to be initialised, and none was sent the
    # initialisation segment. The field log has the first media header at
    # `start 2,034 sequence 1`, 2,034 bytes missing at byte zero, on a download
    # whose single-session path had already been fixed.
    worker = SabrStream(None, "https://example.invalid/s", b"config",
                        SabrFormat(itag=137, last_modified=999, size=90_000))
    worker.restore([[40_000, 90_000]], player_ms=9000, sequence=12)
    split = parse(worker._build_request())
    check("a worker holding a later stretch does not claim the opening",
          2 not in split, str(sorted(split)))
    check("…and still names the stream it wants",
          17 in split, str(sorted(split)))

    audio = SabrStream(None, "u", b"c",
                       SabrFormat(itag=140, last_modified=1, size=1, is_audio=True))
    audio_fields = parse(audio._build_request())
    check("audio is announced in the audio slot", 16 in audio_fields,
          str(sorted(audio_fields)))
    check("the video slot is left alone for audio", 17 not in audio_fields,
          str(sorted(audio_fields)))

    # The browser hands the token over base64url-encoded and unpadded.
    encoded = base64.urlsafe_b64encode(token).decode().rstrip("=")
    check("the browser's encoding is decoded back exactly",
          _decode_po_token(encoded) == token)
    check("an unusable token does not raise",
          _decode_po_token("!!! not base64 !!!") == b"")

    # Without a token the field must be absent rather than empty, so the
    # server sees a plain unattested request rather than a malformed one.
    plain = parse(SabrStream(None, "u", b"c",
                             SabrFormat(itag=137, last_modified=1, size=1)
                             )._build_request())
    check("no token means no token field", 2 not in parse(plain[19]),
          str(sorted(parse(plain[19]))))


def test_gap_is_never_published() -> None:
    """A stream that reaches its end with a hole in it is not a finished file.

    Completion was measured by the highest offset written, which is right about
    a re-sent initialisation segment and wrong about a skipped block: if the
    server misses a range and then delivers the rest, the end arrives while the
    hole remains. The file is sparse, so the hole reads back as zeros — and the
    result is a video that plays for a few seconds, freezes, and goes on
    producing sound, which is exactly what was reported.
    """
    print("\n[a gap is never published]")
    from ixd.extractors.sabr import SabrFormat, SabrStream

    stream = SabrStream(None, "u", b"c", SabrFormat(itag=137, last_modified=1,
                                                    size=1000))
    check("nothing written means everything missing",
          stream.missing(1000) == [(0, 1000)], str(stream.missing(1000)))

    stream._cover(0, 400)
    check("a partial file reports the remainder",
          stream.missing(1000) == [(400, 1000)], str(stream.missing(1000)))

    # The end arrives, but 400..600 never did.
    stream._cover(600, 1000)
    check("reaching the end does not hide a hole",
          stream.missing(1000) == [(400, 600)], str(stream.missing(1000)))

    stream._cover(400, 600)
    check("filling the hole completes the file",
          stream.missing(1000) == [], str(stream.missing(1000)))

    # Ranges arriving out of order, overlapping and touching, must all merge.
    other = SabrStream(None, "u", b"c", SabrFormat(itag=1, last_modified=1,
                                                   size=100))
    for start, end in ((50, 70), (0, 20), (60, 100), (20, 50)):
        other._cover(start, end)
    check("out-of-order and overlapping ranges merge",
          other.missing(100) == [], str(other._covered))

    # A re-sent initialisation segment must not be counted as new coverage.
    repeat = SabrStream(None, "u", b"c", SabrFormat(itag=1, last_modified=1,
                                                    size=100))
    repeat._cover(0, 40)
    repeat._cover(0, 40)
    check("a repeated range does not fill the rest",
          repeat.missing(100) == [(40, 100)], str(repeat.missing(100)))


def test_interrupted_transfer_resumes() -> None:
    """An interrupted server-driven transfer must continue, not begin again.

    Positions in this protocol are times, not byte offsets, so a session that
    is not told what it already holds opens at the beginning. With a fixed
    allowance per session that meant a transfer stopped at 98% spent the whole
    of its next attempt re-fetching bytes it had, and stopped again before ever
    reaching the part that was missing — so it could not be resumed at all.
    """
    print("\n[an interrupted transfer resumes]")
    from ixd.extractors.sabr import SabrFormat, SabrStream

    size = 1_000_000
    fmt = SabrFormat(itag=137, last_modified=1, size=size)

    # An attempt that reached 98% and stopped.
    first = SabrStream(None, "u", b"c", fmt, duration_ms=100_000)
    first._cover(0, 980_000)
    saved = first.coverage()
    check("what was held is reported for storage",
          saved == [[0, 980_000]], str(saved))

    # The next attempt is handed that back.
    second = SabrStream(None, "u", b"c", fmt, duration_ms=100_000)
    second.restore(saved)
    check("the restored transfer knows what it has",
          second.missing(size) == [(980_000, size)], str(second.missing(size)))
    check("and knows how far it got", second._end == 980_000, str(second._end))

    # Resuming asks from the missing byte, not from the beginning: 98% of the
    # way through a 100-second stream is 98 seconds in.
    second._seek_to_byte(980_000)
    check("the session opens at the missing byte, not at zero",
          second._player_time_ms == 98_000, str(second._player_time_ms))
    check("and does not claim to hold a later position",
          second._sequence == 0, str(second._sequence))

    # The exact position is preferred over converting a byte offset, because
    # the conversion needs the stream's running time and an older transfer may
    # never have recorded one. Without it the position works out as zero, the
    # session opens at the beginning, and every reply re-sends bytes already
    # held — the transfer runs at full speed while progress, which is the
    # furthest byte reached, does not move at all.
    exact = SabrStream(None, "u", b"c", fmt)          # no duration recorded
    exact.restore(saved, player_ms=98_000)
    check("the recorded position is kept for the resume",
          exact._resume_ms == 98_000, str(exact._resume_ms))

    without = SabrStream(None, "u", b"c", fmt)        # no duration, no position
    without.restore(saved)
    without._seek_to_byte(980_000)
    check("without either, the position would be zero — the stuck case",
          without._player_time_ms == 0, str(without._player_time_ms))

    # Data that is already held counts for nothing, however much of it arrives.
    counting = SabrStream(None, "u", b"c", fmt)
    counting._cover(0, 1000)
    check("a wholly re-sent block gains nothing",
          counting._uncovered_within(0, 1000) == 0)
    check("a partly new block gains only the new part",
          counting._uncovered_within(500, 1500) == 500)
    check("a wholly new block gains all of it",
          counting._uncovered_within(2000, 2500) == 500)

    # A stored map that is damaged must not stop the transfer starting.
    third = SabrStream(None, "u", b"c", fmt)
    third.restore([[0, 100], "nonsense", [None, 5], [200, 150], [300, 400]])
    check("a damaged entry is skipped, the sound ones kept",
          third.missing(size) == [(100, 300), (400, size)],
          str(third.missing(size)))
    check("nothing stored means nothing restored",
          SabrStream(None, "u", b"c", fmt).coverage() == [])


def _ump_varint(value: int) -> bytes:
    """Encode one UMP varint — the width lives in the first byte's high bits."""
    if value < 0x80:
        return bytes([value])
    if value < 0x4000:
        return bytes([0x80 | (value & 0x3F), value >> 6])
    if value < 0x200000:
        return bytes([0xC0 | (value & 0x1F)]) + (value >> 5).to_bytes(2, "little")
    if value < 0x10000000:
        return bytes([0xE0 | (value & 0x0F)]) + (value >> 4).to_bytes(3, "little")
    return b"\xf0" + value.to_bytes(4, "little")


def _ump_part(part_type: int, payload: bytes) -> bytes:
    return _ump_varint(part_type) + _ump_varint(len(payload)) + payload


class _FakeStreamingServer:
    """A streaming endpoint that behaves the way the real one was measured to.

    The essential behaviour, and the one the resume path has to satisfy: the
    server has no memory of a session between connections, so *where it starts
    is decided by what the request declares it already holds*. A request that
    declares nothing is answered from the beginning, however far along the
    playback clock in it may point. That is not a quirk to be worked around —
    it is what a fresh request means — and it is exactly what a resumed
    transfer used to send.
    """

    SEGMENT = 65536

    def __init__(self, size: int, itag: int = 137, duration_ms: int = 600_000,
                 per_session: int = 0, skip: int = -1,
                 honour_clock: bool = False) -> None:
        self.size = size
        self.itag = itag
        self.duration_ms = duration_ms
        #: How much media one session hands over before falling silent, which
        #: is the ceiling that makes resuming necessary in the first place.
        #: Unbounded unless a test is about that ceiling.
        self.per_session = per_session or (1 << 60)
        #: A segment index the server declines to send once, to exercise the
        #: wind-back that fills a hole.
        self.skip = skip
        #: Whether a request that declares nothing is started from the
        #: playback clock it carries rather than from the beginning. The real
        #: endpoint has been seen to do both; a transfer has to work either
        #: way, so both are exercised.
        self.honour_clock = honour_clock
        self.served = 0
        self.requests: list[dict] = []
        self.segments = (size + self.SEGMENT - 1) // self.SEGMENT
        #: Bytes per unit of the reported time range, chosen so that the whole
        #: stream runs for exactly ``duration_ms`` — the same relation the
        #: transfer assumes when it converts an offset back into a position.
        self.scale = max(1, size * 1000 // duration_ms)

    # -- the client side of HttpClient.post, as SabrStream uses it ------
    def post(self, url: str, body: bytes, headers: dict) -> Any:
        payload = self._answer(body)

        class Response:
            """A reply delivered the way a socket delivers one: in pieces.

            The transfer reads a reply incrementally and writes each part as it
            arrives, so a stub that hands over the whole thing at once would
            never exercise the case that matters — a UMP part, or the varint
            announcing one, split across two reads. The piece size is therefore
            deliberately small and not a divisor of anything.
            """

            PIECE = 7

            def __init__(self_inner):
                self_inner._position = 0

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *exc):
                return False

            def read_all(self_inner, _limit: int) -> bytes:
                return payload

            def read(self_inner, amount: int = -1) -> bytes:
                if self_inner._position >= len(payload):
                    return b""
                size = len(payload) if amount < 0 else min(amount, self_inner.PIECE)
                piece = payload[self_inner._position:self_inner._position + size]
                self_inner._position += len(piece)
                return piece

        return Response()

    def _answer(self, body: bytes) -> bytes:
        from ixd.core.protobuf import parse

        fields = parse(body)
        state = parse(fields[1]) if isinstance(fields.get(1), bytes) else {}
        held = fields.get(3)
        declared = parse(held) if isinstance(held, bytes) else None
        self.requests.append({
            "player_ms": state.get(28, 0),
            "declares_held": declared is not None,
            "sequence": (declared or {}).get(5, 0),
        })

        if declared is not None:
            index = declared.get(5, 0)
        elif self.honour_clock:
            # Nothing declared, but the clock is taken at its word.
            byte = int(self.size * (state.get(28, 0) / self.duration_ms))
            index = min(max(0, byte // self.SEGMENT), self.segments)
        else:
            # A session that declares nothing holds nothing, and is answered
            # from the start of the stream whatever its clock says.
            index = 0

        out = b""
        for _ in range(8):
            if index >= self.segments or self.served >= self.per_session:
                break
            if index == self.skip:
                self.skip = -1          # withheld once, not withheld forever
                index += 1
                continue
            start = index * self.SEGMENT
            end = min(start + self.SEGMENT, self.size)
            block = bytes(((start + n) % 251 for n in range(end - start)))
            time_range = (Message().varint(1, start)
                          .varint(2, end - start).varint(3, self.scale))
            header = (Message().varint(1, 1).varint(3, self.itag)
                      .varint(6, start).varint(9, index + 1)
                      .message(15, time_range))
            out += _ump_part(20, header.to_bytes())
            out += _ump_part(21, bytes([1]) + block)
            self.served += end - start
            index += 1
        return out


def _fetch(server: "_FakeStreamingServer", into: dict,
           restore: tuple | None = None) -> tuple:
    """Run one transfer against ``server``; return (error, stream)."""
    from ixd.extractors.sabr import SabrFormat, SabrStream

    stream = SabrStream(server, "https://example.invalid/s", b"cfg",
                        SabrFormat(itag=server.itag, last_modified=1,
                                   size=server.size),
                        duration_ms=server.duration_ms)
    if restore is not None:
        stream.restore(*restore)

    def write(offset: int, data: bytes) -> None:
        into[offset] = data

    try:
        stream.download(write)
        return "", stream
    except Exception as exc:  # noqa: BLE001 - the message is the assertion
        return str(exc), stream


def test_the_stream_header_always_has_a_source() -> None:
    """The opening kilobytes must have somewhere to come from.

    A stream's initialisation and index segments never travel over the
    streaming session — the server assumes a player fetched them itself — so
    they are taken from an ordinary URL. That URL was read from the entry that
    produced the server-driven format, and an entry that publishes no URL is
    precisely why the format is server-driven in the first place. So those few
    kilobytes had no source at all: the transfer fetched the whole file, then
    refused it for a hole at byte zero. A hundred and twenty megabytes for the
    sake of three and a half thousand.
    """
    print("\n[the stream header always has a source]")
    import base64 as _base64
    import copy

    header_ranges = {"initRange": {"start": "0", "end": "744"},
                     "indexRange": {"start": "745", "end": "3484"}}

    def response(with_urls: bool, server_driven: bool) -> dict:
        adaptive = []
        for itag, height in ((137, 1080), (140, 0)):
            entry = {
                "itag": itag,
                "mimeType": ('audio/mp4; codecs="mp4a.40.2"' if not height
                             else 'video/mp4; codecs="avc1.640028"'),
                "bitrate": 4000000 if height else 128000,
                "contentLength": "50000000" if height else "9000000",
                **copy.deepcopy(header_ranges),
            }
            if height:
                entry.update(width=1920, height=height, qualityLabel="1080p")
            if with_urls:
                entry["url"] = ("https://rr1.googlevideo.com/videoplayback"
                                f"?itag={itag}&gir=yes")
            adaptive.append(entry)
        streaming: dict = {"adaptiveFormats": adaptive, "formats": []}
        if server_driven:
            streaming["serverAbrStreamingUrl"] = \
                "https://rr1.googlevideo.com/videoplayback?sabr=1"
        return {
            "playabilityStatus": {"status": "OK"},
            "videoDetails": {"title": "Test", "lengthSeconds": "600"},
            "playerConfig": {"mediaCommonConfig": {
                "mediaUstreamerRequestConfig": {
                    "videoPlaybackUstreamerConfig":
                        _base64.urlsafe_b64encode(b"cfg").decode()}}},
            "streamingData": streaming,
        }

    # The shape that failed: the page is server-driven and publishes no URL,
    # while a mobile identity describes the same renditions with one.
    class Mixed(YouTubeExtractor):
        def _player_response_from_page(self, video_id, refresh=False):
            return response(with_urls=False, server_driven=True)

        def _call_player(self, video_id, client, *args, **kwargs):
            if client.key != "android":
                raise RuntimeError("Sign in to confirm you're not a bot")
            return response(with_urls=True, server_driven=False)

    info = Mixed(client=None, options={}).extract(
        "https://www.youtube.com/watch?v=abcdefghijk")
    driven = [media for media in info.formats if media.sabr]
    check("the renditions are still server-driven", len(driven) == 2,
          str([(m.format_id, bool(m.sabr)) for m in info.formats]))
    check("each one knows where its header ends",
          all(media.sabr.get("header_end") == 3484 for media in driven),
          str([media.sabr.get("header_end") for media in driven]))
    check("and each one has somewhere to fetch it from",
          all(media.sabr.get("header_url") for media in driven),
          str([media.sabr.get("header_url") for media in driven]))
    check("the source is the link published for that same rendition",
          all(f"itag={media.format_id}" in media.sabr["header_url"]
              for media in driven),
          str([media.sabr.get("header_url") for media in driven]))

    # When nothing anywhere publishes a URL there is genuinely no source, and
    # that has to stay visible rather than be papered over.
    class Nowhere(YouTubeExtractor):
        def _player_response_from_page(self, video_id, refresh=False):
            return response(with_urls=False, server_driven=True)

        def _call_player(self, video_id, client, *args, **kwargs):
            raise RuntimeError("Sign in to confirm you're not a bot")

    bare = Nowhere(client=None, options={}).extract(
        "https://www.youtube.com/watch?v=abcdefghijk")
    check("a rendition with no link anywhere reports no header source",
          all(not media.sabr.get("header_url")
              for media in bare.formats if media.sabr),
          str([media.sabr.get("header_url") for media in bare.formats]))

    # Unless the video publishes a DASH manifest, which describes the same
    # streams a second time — one representation per itag, each with a plain
    # link. It is fetched only when something is actually missing one.
    manifest = """<?xml version="1.0"?>
<MPD xmlns="urn:mpeg:dash:schema:mpd:2011" type="static"
     mediaPresentationDuration="PT600S">
  <Period>
    <AdaptationSet mimeType="video/mp4">
      <Representation id="137" codecs="avc1.640028" bandwidth="4000000"
                      width="1920" height="1080">
        <BaseURL>https://rr1.googlevideo.com/videoplayback?itag=137&amp;mpd=1</BaseURL>
        <SegmentBase indexRange="745-3484">
          <Initialization range="0-744"/>
        </SegmentBase>
      </Representation>
    </AdaptationSet>
    <AdaptationSet mimeType="audio/mp4">
      <Representation id="140" codecs="mp4a.40.2" bandwidth="128000">
        <BaseURL>https://rr1.googlevideo.com/videoplayback?itag=140&amp;mpd=1</BaseURL>
        <SegmentBase indexRange="745-3484">
          <Initialization range="0-744"/>
        </SegmentBase>
      </Representation>
    </AdaptationSet>
  </Period>
</MPD>"""

    fetched: list[str] = []

    class FakeClient:
        def get_text(self, url, headers=None, **kwargs) -> str:
            fetched.append(url)
            return manifest

    class WithManifest(Nowhere):
        def _player_response_from_page(self, video_id, refresh=False):
            answer = response(with_urls=False, server_driven=True)
            answer["streamingData"]["dashManifestUrl"] = \
                "https://www.youtube.com/api/manifest/dash/id/abcdefghijk"
            return answer

    rescued = WithManifest(client=FakeClient(), options={}).extract(
        "https://www.youtube.com/watch?v=abcdefghijk")
    check("the manifest is consulted when a header has nowhere to come from",
          len(fetched) == 1, str(fetched))
    check("and it supplies the missing link for every rendition",
          all("mpd=1" in media.sabr.get("header_url", "")
              for media in rescued.formats if media.sabr),
          str([media.sabr.get("header_url") for media in rescued.formats]))

    # And it is not fetched when nothing needs it.
    fetched.clear()

    class HasLinksAndManifest(Mixed):
        def _player_response_from_page(self, video_id, refresh=False):
            answer = response(with_urls=False, server_driven=True)
            answer["streamingData"]["dashManifestUrl"] = \
                "https://www.youtube.com/api/manifest/dash/id/abcdefghijk"
            return answer

    HasLinksAndManifest(client=FakeClient(), options={}).extract(
        "https://www.youtube.com/watch?v=abcdefghijk")
    check("a manifest is not fetched when every header already has a source",
          fetched == [], str(fetched))


def test_a_stopped_transfer_resumes_where_it_stopped() -> None:
    """A resumed session must declare what it holds, not just where it is.

    The request field that tells the server what is already held is written
    only when a segment index is known, and a resumed transfer had none: it
    was rebuilt from the coverage map and a playback clock, so its first
    request was indistinguishable from a first request and the server answered
    it the same way — with the file from byte zero. Every reply then arrived
    full of bytes already held, the furthest byte reached never moved, and the
    transfer failed having re-sent a hundred megabytes and gained nothing.
    That is the reported failure, in the server's own terms.
    """
    print("\n[a stopped transfer resumes where it stopped]")
    size = 3_000_000

    # A first attempt that is cut off partway, which is the ordinary case:
    # a session hands over a bounded amount of media and then falls silent.
    first_server = _FakeStreamingServer(size, per_session=1_200_000)
    held: dict[int, bytes] = {}
    error, first = _fetch(first_server, held)
    check("the first attempt is cut short", "stopped after" in error, error)
    check("but it did fetch the opening", first._end >= 1_000_000, str(first._end))
    check("and it recorded a segment index", first._sequence > 0, str(first._sequence))

    saved = (first.coverage(), int(first._buffered_ms), int(first._sequence))

    # The continuation, against a new session that remembers nothing.
    second_server = _FakeStreamingServer(size)
    error, second = _fetch(second_server, held, restore=saved)
    check("the continuation completes the file", error == "", error)
    check("and it reaches the end", second._end == size, str(second._end))
    check("with no holes left", second.missing(size) == [],
          str(second.missing(size)))

    # A continuation with nothing left to fetch — the state a transfer is in
    # when its only gap was the header, and the header is fetched over an
    # ordinary link before the session opens — must not open one at all.
    idle_server = _FakeStreamingServer(size)
    error, idle = _fetch(idle_server, {}, restore=([[0, size]], 1000, 4))
    check("a continuation with nothing missing asks for nothing",
          error == "" and idle_server.requests == [],
          error or str(idle_server.requests))

    opening = second_server.requests[0]
    check("its first request declares what is held",
          opening["declares_held"], str(opening))
    check("and names the segment the last attempt reached",
          opening["sequence"] == saved[2], str(opening))
    check("so the server is never asked for byte zero again",
          all(request["sequence"] > 0 for request in second_server.requests),
          str(second_server.requests[:3]))

    # The same continuation without the segment index — the state an older
    # transfer left behind, and the exact shape of the reported failure. The
    # session is bounded, as a real one is, so re-sending the opening spends
    # the whole allowance before it ever reaches the part that is missing.
    third_server = _FakeStreamingServer(size, per_session=1_200_000)
    error, third = _fetch(third_server, {}, restore=(saved[0], saved[1], 0))
    check("without it the continuation fails", error != "", "it succeeded")
    check("having gained nothing that was missing",
          third.missing(size) == first.missing(size),
          f"{third.missing(size)} vs {first.missing(size)}")
    check("because it opened by declaring nothing, as a first attempt does",
          not third_server.requests[0]["declares_held"],
          str(third_server.requests[0]))


def test_a_pause_is_not_the_server_giving_up() -> None:
    """Being asked to stop must not be reported as the origin stopping.

    Falling out of the request loop on ``should_stop`` reached the checks that
    describe a spent session, so a pause was announced as “the streaming server
    stopped after N of M bytes and would not continue” — the origin blamed for
    the user's own click. Worse, when the pause landed before that session had
    gained anything, the engine had no progress to justify continuing and
    failed the download outright instead of pausing it.
    """
    print("\n[a pause is not the server giving up]")
    from ixd.core.errors import CancelledError
    from ixd.extractors.sabr import SabrFormat, SabrStream

    # Large enough that a couple of replies leave plenty outstanding, so the
    # pause genuinely interrupts rather than racing a finished transfer.
    size = 5_000_000

    def paused_after(replies: int) -> tuple[str, str]:
        server = _FakeStreamingServer(size)
        stream = SabrStream(server, "https://example.invalid/s", b"cfg",
                            SabrFormat(itag=137, last_modified=1, size=size),
                            duration_ms=100_000)
        seen = {"n": 0}

        def stop() -> bool:
            # Stop once the requested number of replies has come back.
            return seen["n"] >= replies

        def write(offset: int, data: bytes) -> None:
            seen["n"] = len(server.requests)

        try:
            stream.download(write, should_stop=stop)
            return "", ""
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__, str(exc)

    # Paused after some of the file has arrived.
    kind, message = paused_after(2)
    check("a pause part-way is a cancellation, not a failure",
          kind == "CancelledError", f"{kind}: {message}")
    check("and it does not blame the server",
          "would not continue" not in message, message)

    # Paused before this session had gained anything at all — the case that
    # failed the download rather than pausing it.
    kind, message = paused_after(0)
    check("a pause before any progress is also a cancellation",
          kind == "CancelledError", f"{kind}: {message}")

    check("and CancelledError is what the engine treats as a pause",
          issubclass(CancelledError, Exception))


def test_a_wind_back_survives_to_the_wire() -> None:
    """A position the transfer chooses must be the position it asks for.

    The reported clock followed whatever had already arrived, and a wind-back
    to fill a hole is by definition a move *backwards* — so the later of the
    two won and the request went out pointing past the very gap it was sent to
    fetch. The seek was performed, logged, and had no effect on the wire.
    """
    print("\n[a wind-back survives to the wire]")
    from ixd.extractors.sabr import SabrFormat, SabrStream

    stream = SabrStream(None, "u", b"c",
                        SabrFormat(itag=137, last_modified=1, size=1_000_000),
                        duration_ms=100_000)
    # A transfer that has already taken playback to 90 seconds, winding back
    # to a hole at 10% of the file — 10 seconds in.
    stream._buffered_ms = 90_000
    stream._sequence = 40
    stream._seek_to_byte(100_000)
    check("the seek computes the earlier position",
          stream._player_time_ms == 10_000, str(stream._player_time_ms))
    check("and it is pinned for the next request",
          stream._pinned_ms == 10_000, str(stream._pinned_ms))

    # End to end, against a server that skips a block and *does* start from
    # the clock it is given — so a request pointing past the hole is answered
    # past the hole, and the wind-back is the only thing that fills it.
    #
    # The wall clock is held far ahead of the media clock for the duration,
    # which is the ordinary state of a transfer that has been running for a
    # while and the condition under which the old rule discarded every seek.
    import ixd.extractors.sabr as sabr_module

    class _StuckClock:
        calls = 0

        @classmethod
        def monotonic(cls) -> float:
            cls.calls += 1
            return 0.0 if cls.calls == 1 else 500.0

    size = 1_000_000
    server = _FakeStreamingServer(size, duration_ms=100_000, skip=4,
                                  honour_clock=True)
    written: dict[int, bytes] = {}
    real_time = sabr_module.time
    sabr_module.time = _StuckClock          # type: ignore[assignment]
    try:
        error, filled = _fetch(server, written)
    finally:
        sabr_module.time = real_time
    check("the skipped block is asked for again and the file completed",
          error == "" and filled.missing(size) == [],
          error or str(filled.missing(size)))
    check("and no request was ever sent past the end of the stream",
          all(request["player_ms"] <= 100_000 for request in server.requests),
          str([r["player_ms"] for r in server.requests][:5]))

    # The same hole, against a server that advances only on what a request
    # declares. Winding back by abandoning the segment index and asking by
    # clock alone makes the session start over from the beginning, and the
    # allowance runs out before the hole is reached. Declaring the segment
    # *before* the hole asks for exactly the one missing block — which is the
    # ordinary request this protocol advances on, aimed one segment back.
    strict = _FakeStreamingServer(1_000_000, duration_ms=100_000, skip=4,
                                  per_session=1_100_000)
    error, targeted = _fetch(strict, {})
    check("the hole is filled without restarting the stream",
          error == "" and targeted.missing(1_000_000) == [],
          error or str(targeted.missing(1_000_000)))
    rewound = [request for request in strict.requests
               if request["sequence"] == 4]
    check("the wind-back declares the segment before the hole",
          bool(rewound), str([r["sequence"] for r in strict.requests]))
    check("and the session was never sent back to the beginning",
          all(request["sequence"] > 0 for request in strict.requests[1:]),
          str([r["sequence"] for r in strict.requests]))


def test_captured_session_is_replayed() -> None:
    """The player's own session must go back out unchanged.

    A proof of origin is minted against the client identity recorded beside it,
    so a request rebuilt from our own player response is a *different* session
    and the proof does not carry to it. Reassembling the context by hand — even
    copying the token into it — fails silently: the stream stops exactly where
    an unattested one would have, which is why three separate sessions
    concluded that tokens do not help.
    """
    print("\n[the player's session is replayed, not rebuilt]")
    import base64

    from ixd.core.protobuf import Message, parse
    from ixd.extractors.sabr import SabrFormat, SabrStream, stream_from_context

    # A context as the browser would have sent it: an identity we would never
    # construct ourselves, plus a proof beside it.
    captured = (Message()
                .message(1, Message().varint(3, 7777))
                .raw(2, b"BROWSER-MINTED-PROOF")
                .to_bytes())

    stream = SabrStream(None, "https://example.invalid/s", b"config",
                        SabrFormat(itag=137, last_modified=1, size=10),
                        client_id=5, po_token=b"OUR-OWN-TOKEN",
                        streamer_context=captured)
    fields = parse(stream._build_request())
    check("the captured context is sent verbatim", fields.get(19) == captured,
          repr(fields.get(19)))

    inner = parse(fields[19])
    check("the player's client identity survives",
          parse(inner[1]).get(3) == 7777, str(parse(inner[1])))
    check("our own token does not displace the captured proof",
          inner.get(2) == b"BROWSER-MINTED-PROOF", repr(inner.get(2)))

    # Without a capture the request is built as before, under our identity.
    plain = parse(SabrStream(None, "u", b"c",
                             SabrFormat(itag=137, last_modified=1, size=1),
                             client_id=5, po_token=b"OUR-OWN-TOKEN",
                             )._build_request())
    check("without a capture our own context is built",
          parse(plain[19]).get(2) == b"OUR-OWN-TOKEN", repr(parse(plain[19])))

    # And it survives the trip through JSON, which is how it reaches the engine.
    rebuilt = stream_from_context(None, {
        "endpoint": "https://example.invalid/s",
        "config": base64.b64encode(b"config").decode(),
        "itag": 137, "last_modified": 1, "size": 10,
        "streamer_context": base64.b64encode(captured).decode(),
    })
    check("the context survives storage as text",
          rebuilt.streamer_context == captured, repr(rebuilt.streamer_context))
    check("an unreadable context is simply unused",
          stream_from_context(None, {
              "endpoint": "u", "config": "", "itag": 1,
              "streamer_context": "!!! not base64 !!!",
          }).streamer_context is None)


def test_attested_endpoint_preference() -> None:
    """A proof of origin has to be spent on the session it was minted for.

    The token is generated by the *web* player, for the web client identity and
    the browser's visitor id. Preferring a mobile endpoint — a session opened
    under a different client, with a different ustreamer configuration — means
    presenting the proof somewhere it does not apply, and the refusal is
    silent: the stream stops where an unattested one would have stopped. That
    makes a working token indistinguishable from a useless one, which is how an
    earlier session concluded tokens do not help.
    """
    print("\n[endpoint preference follows the token]")
    from ixd.core.http_client import HttpClient
    from ixd.core.models import MediaFormat
    from ixd.core.net import NetworkProfile

    def entry(source: str) -> MediaFormat:
        return MediaFormat("137", "https://example.invalid/s", height=1080,
                           vcodec="h264", acodec="none",
                           sabr={"source": source, "itag": 137})

    offered = [entry("android"), entry("web"), entry("ios")]

    client = HttpClient(NetworkProfile())
    plain = YouTubeExtractor(client, {})
    kept = [f for f in plain._prefer_usable_sabr(list(offered)) if f.sabr]
    check("without a token the mobile endpoint is preferred",
          kept and kept[0].sabr["source"] == "android",
          str([f.sabr["source"] for f in kept]))

    attested = YouTubeExtractor(client, {"po_token": "a-proof-of-origin"})
    kept = [f for f in attested._prefer_usable_sabr(list(offered)) if f.sabr]
    check("with a token the web endpoint is preferred",
          kept and kept[0].sabr["source"] == "web",
          str([f.sabr["source"] for f in kept]))


def test_webm_muxer() -> None:
    """Parsing and muxing WebM, checked against files built here.

    This exists because a real download failed on it. Everything at 60fps, and
    everything above 1080p, is published as VP9 or AV1 with Opus in a WebM
    container — and handing that pair to the ISOBMFF muxer reports "no moov
    box — this is not an MP4", which is true and useless. A 208 MB download
    completed, could not be joined, and was left as two files.

    The assertions are about what a demuxer depends on: that both tracks are
    present under *distinct* numbers, that every frame survives byte for byte,
    that no block is placed before the cluster that contains it, and that the
    file declares its own length correctly.
    """
    print("\n[WebM muxer]")
    import struct
    import tempfile

    from ixd.core import webm

    def build(track_type: int, codec: str, frames: list[tuple[int, bytes, bool]],
              extra: bytes = b"") -> bytes:
        """A single-track WebM. Both inputs number their track 1, as YouTube's do."""
        header = webm.element(webm.ID_EBML, b"".join((
            webm.uint_element(0x4286, 1), webm.uint_element(0x42F7, 1),
            webm.uint_element(0x42F2, 4), webm.uint_element(0x42F3, 8),
            webm.element(0x4282, b"webm"),
            webm.uint_element(0x4287, 2), webm.uint_element(0x4285, 2),
        )))
        info = webm.element(webm.ID_INFO, (
            webm.uint_element(webm.ID_TIMESTAMP_SCALE, 1_000_000)
            + webm.float_element(webm.ID_DURATION, 4000.0)
        ))
        entry = webm.element(webm.ID_TRACK_ENTRY, (
            webm.uint_element(webm.ID_TRACK_NUMBER, 1)
            + webm.uint_element(webm.ID_TRACK_UID, 1)
            + webm.uint_element(webm.ID_TRACK_TYPE, track_type)
            + webm.element(webm.ID_CODEC_ID, codec.encode())
            + extra
        ))
        tracks = webm.element(webm.ID_TRACKS, entry)

        clusters = b""
        for base in (0, 2000):
            inside = [(t, data, key) for t, data, key in frames
                      if base <= t < base + 2000]
            if not inside:
                continue
            body = webm.uint_element(webm.ID_TIMESTAMP, base)
            for timestamp, data, key in inside:
                block = (webm.encode_vint(1)
                         + struct.pack(">h", timestamp - base)
                         + bytes((0x80 if key else 0x00,)) + data)
                body += webm.element(webm.ID_SIMPLE_BLOCK, block)
            clusters += webm.element(webm.ID_CLUSTER, body)
        return header + webm.element(webm.ID_SEGMENT, info + tracks + clusters)

    video_frames = [(index * 100, bytes([0x40 + index]) * (20 + index),
                     index % 10 == 0) for index in range(40)]
    audio_frames = [(index * 80, bytes([0x80 + (index % 60)]) * (11 + index % 7), True)
                    for index in range(50)]

    with tempfile.TemporaryDirectory() as root:
        video_path = Path(root) / "video.webm"
        audio_path = Path(root) / "audio.webm"
        output = Path(root) / "joined.webm"
        video_path.write_bytes(build(1, "V_VP9", video_frames))
        # Opus carries a CodecPrivate and a codec delay, and losing either
        # makes a file that opens and plays noise.
        audio_path.write_bytes(build(2, "A_OPUS", audio_frames, extra=(
            webm.element(0x63A2, b"OpusHead" + b"\x01\x02" + b"\x00" * 9)
            + webm.uint_element(0x56AA, 6_500_000)
        )))

        parsed = webm.read_track(video_path)
        check("the track header is read", parsed.codec_id == "V_VP9"
              and parsed.track_type == 1, f"{parsed.codec_id}/{parsed.track_type}")
        check("every block is indexed",
              len(parsed.blocks) == len(video_frames),
              f"{len(parsed.blocks)} of {len(video_frames)}")
        check("keyframes are recognised",
              sum(1 for b in parsed.blocks if b.keyframe) == 4,
              str(sum(1 for b in parsed.blocks if b.keyframe)))

        webm.mux(video_path, audio_path, output)

        data = output.read_bytes()
        check("the output is Matroska", data[:4] == b"\x1a\x45\xdf\xa3")

        # Re-read it the way a demuxer would.
        blocks: dict[int, list[tuple[int, int, bytes]]] = {1: [], 2: []}
        codecs: list[str] = []
        numbers: list[int] = []
        segment_size = [0]

        def walk(start: int, end: int) -> None:
            for eid, body, body_end in webm._iter_children(data, start, end):
                if eid == webm.ID_SEGMENT:
                    segment_size[0] = body_end - body
                    walk(body, body_end)
                elif eid in (webm.ID_TRACKS, webm.ID_TRACK_ENTRY):
                    walk(body, body_end)
                elif eid == webm.ID_TRACK_NUMBER:
                    numbers.append(int.from_bytes(data[body:body_end], "big"))
                elif eid == webm.ID_CODEC_ID:
                    codecs.append(data[body:body_end].decode())
                elif eid == webm.ID_CLUSTER:
                    cluster_time = 0
                    for cid, cb, ce in webm._iter_children(data, body, body_end):
                        if cid == webm.ID_TIMESTAMP:
                            cluster_time = int.from_bytes(data[cb:ce], "big")
                        elif cid == webm.ID_SIMPLE_BLOCK:
                            number, after = webm.read_vint(data, cb)
                            relative = struct.unpack(">h", data[after:after + 2])[0]
                            blocks[number].append(
                                (cluster_time + relative, relative, data[after + 3:ce]))

        walk(0, len(data))

        check("both tracks are present, under different numbers",
              sorted(numbers) == [1, 2], str(numbers))
        check("and each keeps its own codec",
              codecs == ["V_VP9", "A_OPUS"], str(codecs))
        # The segment's declared length has to reach the last byte of the
        # file: it is written as a placeholder before the clusters exist and
        # patched afterwards, so getting it wrong is entirely possible and
        # leaves players truncating the file at whatever it does say.
        segment_end = data.find(webm.encode_id(webm.ID_SEGMENT)) + \
            len(webm.encode_id(webm.ID_SEGMENT)) + 8 + segment_size[0]
        check("the segment declares a length reaching the end of the file",
              segment_end == len(data), f"{segment_end} vs {len(data)}")

        for number, source in ((1, video_frames), (2, audio_frames)):
            got = blocks[number]
            check(f"track {number}: every frame is there",
                  len(got) == len(source), f"{len(got)} of {len(source)}")
            check(f"track {number}: timestamps are unchanged",
                  [t for t, _, _ in got] == [t for t, _, _ in source],
                  "a timestamp moved")
            check(f"track {number}: frame bytes are unchanged",
                  [payload for _, _, payload in got] == [d for _, d, _ in source],
                  "a frame was altered")
            check(f"track {number}: no block precedes its cluster",
                  all(relative >= 0 for _, relative, _ in got),
                  "a negative cluster offset was written")

        # Interleaving is the point of muxing: a player must not have to read
        # the whole video before it finds any audio.
        check("audio appears alongside the opening video, not after it",
              bool(blocks[2]) and blocks[2][0][0] <= blocks[1][2][0],
              f"first audio at {blocks[2][0][0] if blocks[2] else '-'}ms, "
              f"third video frame at {blocks[1][2][0]}ms")


def test_mp4_muxer() -> None:
    """Parsing and muxing MP4, checked against a file built here.

    The interesting failure is not a crash but a file that a lenient reader
    accepts and a real demuxer rejects — an eight-byte omission in ``tkhd``
    did exactly that. So the assertions check the byte layout of the boxes a
    demuxer relies on, not merely that the structure round-trips.
    """
    print("\n[MP4 muxer]")
    import struct
    import tempfile

    from ixd.core import mp4

    # A minimal but structurally complete MP4 with one silent "audio" track and
    # one "video" track, each with two samples of known bytes.
    def build(kind: bytes, payloads: list[bytes], timescale: int) -> bytes:
        media = b"".join(payloads)
        sizes = [len(p) for p in payloads]
        header_size = [0]

        def moov(mdat_offset: int) -> bytes:
            stsd = mp4._box(b"stsd", struct.pack(">IBI", 0, 0, 1)[:8] + b"\x00" * 8)
            stts = mp4._full(b"stts", 0, 0, struct.pack(">III", 1, len(payloads), 512))
            stsc = mp4._full(b"stsc", 0, 0, struct.pack(">IIII", 1, 1, len(payloads), 1))
            stsz = mp4._full(b"stsz", 0, 0,
                             struct.pack(">II", 0, len(sizes))
                             + struct.pack(f">{len(sizes)}I", *sizes))
            stco = mp4._full(b"stco", 0, 0, struct.pack(">II", 1, mdat_offset))
            stbl = mp4._box(b"stbl", stsd + stts + stsc + stsz + stco)
            mdhd = mp4._full(b"mdhd", 0, 0,
                             struct.pack(">IIIIHH", 0, 0, timescale,
                                         512 * len(payloads), 0x55C4, 0))
            hdlr = mp4._full(b"hdlr", 0, 0,
                             struct.pack(">I4s", 0, kind) + b"\0" * 12 + b"h\0")
            minf = mp4._box(b"minf", stbl)
            mdia = mp4._box(b"mdia", mdhd + hdlr + minf)
            tkhd = mp4._full(b"tkhd", 0, 7,
                             struct.pack(">IIIII", 0, 0, 1, 0, 512 * len(payloads))
                             + b"\0" * 8 + struct.pack(">hhhh", 0, 0, 0, 0)
                             + mp4._MATRIX + struct.pack(">II", 0, 0))
            return mp4._box(b"moov", mp4._box(b"trak", tkhd + mdia))

        ftyp = mp4._box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isom")
        header_size[0] = len(ftyp) + len(moov(0)) + 8
        return ftyp + moov(header_size[0]) + mp4._box(b"mdat", media)

    root = Path(tempfile.mkdtemp(prefix="ixd-mp4-"))
    try:
        video_payloads = [b"V" * 300, b"v" * 200]
        audio_payloads = [b"A" * 50, b"a" * 70]
        (root / "v.mp4").write_bytes(build(b"vide", video_payloads, 15360))
        (root / "a.mp4").write_bytes(build(b"soun", audio_payloads, 44100))

        handle, track = mp4.open_track(root / "v.mp4", b"vide")
        try:
            check("video track located", track.kind == b"vide", track.kind.decode())
            check("samples expanded", len(track.samples) == 2, str(len(track.samples)))
            check("sample sizes read",
                  [s.size for s in track.samples] == [300, 200],
                  str([s.size for s in track.samples]))
            handle.seek(track.samples[0].offset)
            check("sample data addressable", handle.read(300) == video_payloads[0])
        finally:
            handle.close()

        output = mp4.mux(root / "v.mp4", root / "a.mp4", root / "out.mp4")
        check("output written", output.exists())

        out = open(output, "rb")
        try:
            size = out.seek(0, 2)
            boxes = mp4.parse_boxes(out, 0, size)
            kinds = [b.kind for b in boxes]
            check("output starts with ftyp then moov then mdat",
                  kinds == [b"ftyp", b"moov", b"mdat"],
                  str([k.decode() for k in kinds]))

            moov_box = next(b for b in boxes if b.kind == b"moov")
            traks = moov_box.find_all(b"trak")
            check("both tracks present", len(traks) == 2, str(len(traks)))

            # A version-0 tkhd payload is exactly 84 bytes: 4 of version and
            # flags, then 80 of body. Dropping the eight reserved bytes that
            # follow the duration yields 76 and shifts the matrix and the
            # dimensions — which a lenient parser still reads, and a real
            # demuxer rejects outright.
            tkhd = traks[0].find(b"tkhd")
            check("tkhd has the full ISO layout",
                  tkhd is not None and tkhd.payload_size == 84,
                  str(tkhd.payload_size if tkhd else "missing"))

            written = {}
            for trak in traks:
                parsed = mp4.read_track(out, trak)
                written[parsed.kind] = [
                    (s.offset, s.size) for s in parsed.samples
                ]
            check("video samples preserved",
                  [s for _o, s in written[b"vide"]] == [300, 200],
                  str(written.get(b"vide")))
            check("audio samples preserved",
                  [s for _o, s in written[b"soun"]] == [50, 70],
                  str(written.get(b"soun")))

            for kind, payloads in ((b"vide", video_payloads), (b"soun", audio_payloads)):
                for (offset, length), expected in zip(written[kind], payloads):
                    out.seek(offset)
                    if out.read(length) != expected:
                        check(f"{kind.decode()} payload intact", False, f"at {offset}")
                        break
                else:
                    check(f"{kind.decode()} payload intact", True)
        finally:
            out.close()

        # A fragmented file is read through its fragments, not its (empty)
        # sample tables — that is the shape adaptive streaming delivers.
        (root / "frag.mp4").write_bytes(_fragmented(mp4, struct))
        handle, fragmented = mp4.open_track(root / "frag.mp4", b"vide")
        try:
            check("fragmented input is read", len(fragmented.samples) == 2,
                  str(len(fragmented.samples)))
            check("fragment sample sizes read",
                  [s.size for s in fragmented.samples] == [40, 60],
                  str([s.size for s in fragmented.samples]))
            handle.seek(fragmented.samples[0].offset)
            check("fragment sample data addressable",
                  handle.read(40) == b"F" * 40)
            check("sync flags decoded from the fragment",
                  [s.sync for s in fragmented.samples] == [True, False],
                  str([s.sync for s in fragmented.samples]))
        finally:
            handle.close()

        # A file with neither sample tables nor fragments is still refused.
        (root / "empty.mp4").write_bytes(
            mp4._box(b"ftyp", b"isom" + struct.pack(">I", 0x200) + b"isom")
            + mp4._box(b"moov", b"")
        )
        try:
            mp4.open_track(root / "empty.mp4")
            check("a file with no track is refused", False, "no error raised")
        except mp4.Mp4Error:
            check("a file with no track is refused", True)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _fragmented(mp4, struct, kind: bytes = b"vide",
                sizes: tuple[int, int] = (40, 60),
                payload: bytes = b"F" * 40 + b"G" * 60) -> bytes:
    """A minimal fragmented MP4: init segment plus one moof/mdat pair."""
    stsd = mp4._box(b"stsd", struct.pack(">IBI", 0, 0, 1)[:8] + b"\x00" * 8)
    stbl = mp4._box(b"stbl", stsd)
    mdhd = mp4._full(b"mdhd", 0, 0, struct.pack(">IIIIHH", 0, 0, 15360, 0, 0x55C4, 0))
    hdlr = mp4._full(b"hdlr", 0, 0, struct.pack(">I4s", 0, kind) + b"\0" * 12 + b"h\0")
    mdia = mp4._box(b"mdia", mdhd + hdlr + mp4._box(b"minf", stbl))
    tkhd = mp4._full(b"tkhd", 0, 7,
                     struct.pack(">IIIII", 0, 0, 1, 0, 0) + b"\0" * 8
                     + struct.pack(">hhhh", 0, 0, 0, 0) + mp4._MATRIX
                     + struct.pack(">II", 0, 0))
    moov = mp4._box(b"moov", mp4._box(b"trak", tkhd + mdia))
    ftyp = mp4._box(b"ftyp", b"iso5" + struct.pack(">I", 0x200) + b"iso5")

    # trun flags 0x0701 = data offset present, plus a duration, a size and
    # flags for every sample. The second sample is marked non-sync.
    def _trun(data_offset: int) -> bytes:
        return mp4._full(b"trun", 0, 0x0701,
                         struct.pack(">I", 2)      # sample count
                         + struct.pack(">i", data_offset)
                         + struct.pack(">III", 512, sizes[0], 0)
                         + struct.pack(">III", 512, sizes[1], 0x00010000))

    tfhd = mp4._full(b"tfhd", 0, 0x020000, struct.pack(">I", 1))
    moof = mp4._box(b"moof", mp4._box(b"traf", tfhd + _trun(0)))

    # The samples sit right after the mdat header, measured from the moof.
    moof = mp4._box(b"moof", mp4._box(b"traf", tfhd + _trun(len(moof) + 8)))
    return ftyp + moov + moof + mp4._box(b"mdat", payload)


def test_two_fragmented_tracks_become_one_file() -> None:
    """The shape a server-driven download actually produces, joined.

    Both tracks of an adaptive quality arrive fragmented — sample tables empty,
    everything described by ``moof``/``trun``. The muxer was exercised on
    fragmented input only for *reading*; the join itself was always tested with
    ordinary sample tables, which is not what the transfer hands it. This is
    the last step before the user gets the file and the first one they would
    notice failing, so it is tested in the form it actually meets.
    """
    print("\n[two fragmented tracks become one file]")
    import shutil
    import struct
    import tempfile
    from pathlib import Path as _Path

    from ixd.core import mp4

    root = _Path(tempfile.mkdtemp(prefix="ixd-frag-"))
    try:
        video_payload = b"F" * 40 + b"G" * 60
        audio_payload = b"S" * 25 + b"T" * 35

        (root / "v.mp4").write_bytes(_fragmented(mp4, struct))
        (root / "a.mp4").write_bytes(
            _fragmented(mp4, struct, kind=b"soun", sizes=(25, 35),
                        payload=audio_payload)
        )

        output = mp4.mux(root / "v.mp4", root / "a.mp4", root / "out.mp4")
        check("a file is produced", output.exists())

        out = open(output, "rb")
        try:
            size = out.seek(0, 2)
            boxes = mp4.parse_boxes(out, 0, size)
            kinds = [b.kind for b in boxes]
            check("it is an ordinary progressive file, not a fragmented one",
                  kinds == [b"ftyp", b"moov", b"mdat"],
                  str([k.decode() for k in kinds]))

            traks = next(b for b in boxes if b.kind == b"moov").find_all(b"trak")
            check("both tracks survive the join", len(traks) == 2, str(len(traks)))

            written = {}
            for trak in traks:
                parsed = mp4.read_track(out, trak)
                written[parsed.kind] = [(s.offset, s.size) for s in parsed.samples]
            check("the video's fragmented samples are described in the output",
                  [s for _o, s in written.get(b"vide", [])] == [40, 60],
                  str(written.get(b"vide")))
            check("and the audio's",
                  [s for _o, s in written.get(b"soun", [])] == [25, 35],
                  str(written.get(b"soun")))

            for kind, payload in ((b"vide", video_payload),
                                  (b"soun", audio_payload)):
                cursor = 0
                intact = True
                for offset, length in written[kind]:
                    out.seek(offset)
                    if out.read(length) != payload[cursor:cursor + length]:
                        intact = False
                        break
                    cursor += length
                check(f"{kind.decode()} bytes are unchanged by the join", intact,
                      str(written[kind]))
        finally:
            out.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_muxable_audio_choice() -> None:
    """Pairing must pick an audio track that can share the video's container."""
    print("\n[muxable audio selection]")
    from ixd.core.models import MediaFormat
    from ixd.extractors import best_audio, best_muxable_audio

    video = MediaFormat(format_id="137", url="v", ext="mp4", height=1080,
                        vcodec="avc1", acodec="none")
    opus = MediaFormat(format_id="251", url="a1", ext="webm", tbr=160,
                       vcodec="none", acodec="opus")
    aac = MediaFormat(format_id="140", url="a2", ext="m4a", tbr=130,
                      vcodec="none", acodec="mp4a")
    formats = [video, opus, aac]

    check("plain best-audio still prefers the higher bitrate",
          best_audio(formats).format_id == "251")
    chosen = best_muxable_audio(formats, video)
    check("muxable choice prefers the compatible container",
          chosen is not None and chosen.format_id == "140",
          chosen.format_id if chosen else "none")
    # There is no falling back to a track that cannot share the container.
    # Doing so used to look like resilience and was the opposite: both tracks
    # were fetched in full — hundreds of megabytes on a long video — and the
    # join then failed, because no file holds an MP4 video beside an Opus
    # stream. Nothing pairable means nothing pairable, said before the
    # transfer rather than after it.
    check("an incompatible track is not offered as a fallback",
          best_muxable_audio([video, opus], video) is None,
          str(best_muxable_audio([video, opus], video)))

    # The same rule the other way round, which is the case that was reported:
    # a WebM video must not be handed the MP4 audio track.
    webm_video = MediaFormat(format_id="303", url="v2", ext="webm", height=1080,
                             fps=60, vcodec="vp9", acodec="none")
    paired = best_muxable_audio([webm_video, opus, aac], webm_video)
    check("a WebM video pairs with the Opus track, never the MP4 one",
          paired is not None and paired.format_id == "251",
          paired.format_id if paired else "none")


def test_system_proxy() -> None:
    print("\n[system proxy]")
    import os

    from ixd.core.system_proxy import detect, host_is_bypassed

    saved = {k: os.environ.get(k) for k in ("https_proxy", "http_proxy", "no_proxy")}
    try:
        os.environ["https_proxy"] = "socks5://alice:secret@10.0.0.5:1080"
        os.environ["no_proxy"] = ".corp.local, 192.168.0.1"
        os.environ.pop("http_proxy", None)

        detected = detect()
        check("environment proxy detected", detected.configured, detected.describe())
        if detected.proxy is not None:
            check("scheme parsed", detected.proxy.scheme.value == "socks5",
                  detected.proxy.scheme.value)
            check("host and port parsed",
                  (detected.proxy.host, detected.proxy.port) == ("10.0.0.5", 1080),
                  f"{detected.proxy.host}:{detected.proxy.port}")
            check("credentials parsed",
                  (detected.proxy.username, detected.proxy.password) == ("alice", "secret"),
                  detected.proxy.username)
        check("loopback always bypassed", "127.0.0.1" in detected.bypass,
              str(detected.bypass))
        check("declared bypass entries kept", ".corp.local" in detected.bypass,
              str(detected.bypass))
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    rules = ("localhost", ".example.com", "*.cdn.net", "10.0.0.1")
    for host, expected in (
        ("localhost", True),
        ("example.com", True),          # a leading dot still covers the apex
        ("files.example.com", True),
        ("example.com.attacker.net", False),
        ("edge.cdn.net", True),
        ("cdn.net.other.org", False),
        ("10.0.0.1", True),
        ("10.0.0.2", False),
    ):
        check(f"bypass {host} -> {expected}",
              host_is_bypassed(host, rules) is expected)


def test_extension_identity() -> None:
    print("\n[chrome extension identity]")
    import base64
    import hashlib
    import json

    from ixd.core.chromeid import (
        extension_id_from_der,
        extension_id_from_manifest_key,
        generate_key_pair,
        is_extension_id,
        public_key_der,
    )

    # The published rule: SHA-256 of the DER public key, first 16 bytes, hex
    # digits mapped from 0-f onto a-p.
    der = public_key_der(0xC0FFEE1234567890ABCDEF, 65537)
    digest = hashlib.sha256(der).hexdigest()[:32]
    expected = "".join("abcdefghijklmnop"[int(c, 16)] for c in digest)
    check("id follows the documented derivation",
          extension_id_from_der(der) == expected, extension_id_from_der(der))
    check("id is 32 characters of a-p", is_extension_id(extension_id_from_der(der)))

    manifest_key, private_pem, identifier = generate_key_pair(1024)
    check("generated key round-trips to the same id",
          extension_id_from_manifest_key(manifest_key) == identifier, identifier)
    check("public key is valid base64 DER",
          base64.b64decode(manifest_key)[:1] == b"\x30")
    check("private key is PEM", private_pem.startswith("-----BEGIN PRIVATE KEY-----"))

    # The shipped manifest must carry a key, or registration cannot happen
    # before the extension is loaded.
    manifest_path = (Path(__file__).resolve().parents[1] / "extension"
                     / "manifest.chrome.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    check("shipped manifest carries a key", bool(manifest.get("key")))
    if manifest.get("key"):
        check("shipped manifest yields a valid id",
              is_extension_id(extension_id_from_manifest_key(manifest["key"])))



def test_a_stream_is_named_after_what_it_is() -> None:
    """The segments decide the container, not the variant's declared extension.

    A master playlist declares codecs, never a container, so an HLS variant is
    built with `ext="mp4"` before the playlist behind it has been read at all.
    Trusting that guess wrote an **MPEG-TS stream into a file called `.mp4`** —
    bytes that are perfectly correct and that most players refuse by name. That
    is the whole of "it downloads and the video is not playable at all", and it
    is invisible unless the bytes are checked against the name.
    """
    print("\n[a stream is named after what it is]")
    from ixd.core.models import MediaFormat, MediaSegment
    from ixd.extractors import output_extension

    variant = MediaFormat("hls-720", "https://cdn/v.m3u8", ext="mp4",
                          protocol="m3u8", height=720)

    ts = [MediaSegment(index=i, url=f"https://cdn/seg{i}.ts") for i in range(3)]
    check("transport-stream pieces make a transport stream",
          output_extension(variant, ts) == "ts", output_extension(variant, ts))

    fmp4 = [MediaSegment(index=0, url="https://cdn/init.mp4", init=True),
            MediaSegment(index=1, url="https://cdn/seg1.m4s")]
    check("fragmented-MP4 pieces make an MP4",
          output_extension(variant, fmp4) == "mp4")

    webm = [MediaSegment(index=0, url="https://cdn/seg0.webm")]
    check("WebM pieces make a WebM", output_extension(variant, webm) == "webm")

    audio = MediaFormat("hls-audio", "https://cdn/a.m3u8", ext="m4a",
                        protocol="m3u8", vcodec="none")
    check("an audio stream keeps its own container",
          output_extension(audio, [MediaSegment(index=0, url="https://cdn/a0.m4a")])
          == "m4a")

    hashed = [MediaSegment(index=i, url=f"https://cdn/{i:08x}") for i in range(3)]
    check("segments whose addresses say nothing are a transport stream, "
          "because fragmented MP4 would have declared an initialisation segment",
          output_extension(variant, hashed) == "ts",
          output_extension(variant, hashed))
    with_init = [MediaSegment(index=0, url="https://cdn/00", init=True),
                 MediaSegment(index=1, url="https://cdn/01")]
    check("…and one that does declare one is not",
          output_extension(variant, with_init) == "mp4",
          output_extension(variant, with_init))

    check("with no segments to consult, the declared extension still stands",
          output_extension(variant, []) == "mp4")
    check("and a playlist's own suffix is never the answer",
          output_extension(
              MediaFormat("direct", "https://cdn/master.m3u8", ext="m3u8",
                          protocol="m3u8"), []) == "mp4")


def test_the_initialisation_segment_does_not_shift_every_iv() -> None:
    """An absent IV is the *media* sequence number, which counts media only.

    RFC 8216 §5.2. `EXT-X-MAP` is appended to the same list as the media
    segments, so counting list positions gave every segment after it an IV one
    too high — and the whole file decrypted to noise: correct-looking bytes, the
    right size, unplayable. Exactly the reported symptom, and silent.
    """
    print("\n[the initialisation segment does not shift every IV]")
    from ixd.extractors.hls import parse_media_playlist

    playlist = (
        "#EXTM3U\n"
        "#EXT-X-VERSION:6\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXT-X-KEY:METHOD=AES-128,URI=\"key.bin\"\n"
        "#EXT-X-MAP:URI=\"init.mp4\"\n"
        "#EXTINF:4.0,\n0.m4s\n"
        "#EXTINF:4.0,\n1.m4s\n"
        "#EXTINF:4.0,\n2.m4s\n"
    )
    segments = parse_media_playlist(playlist, "https://cdn/x/")
    media = [s for s in segments if not s.init]

    check("the initialisation segment is first", segments[0].init is True)
    check("and it is not counted as a media segment", len(media) == 3,
          str(len(media)))
    ivs = [s.key_iv for s in media]
    check("the first media segment's IV is sequence 0",
          ivs[0] == f"{0:032x}", str(ivs[0]))
    check("the second is sequence 1", ivs[1] == f"{1:032x}", str(ivs[1]))
    check("the third is sequence 2", ivs[2] == f"{2:032x}", str(ivs[2]))

    # And a playlist that starts partway through a live window still counts
    # from the number it declares.
    shifted = parse_media_playlist(
        playlist.replace("#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-MEDIA-SEQUENCE:17"),
        "https://cdn/x/")
    first = [s for s in shifted if not s.init][0]
    check("a declared media sequence is where the count starts",
          first.key_iv == f"{17:032x}", str(first.key_iv))

    # An explicit IV always wins; nothing is derived when one is published.
    explicit = parse_media_playlist(
        playlist.replace('URI="key.bin"', 'URI="key.bin",IV=0x0123'), "https://cdn/x/")
    check("an explicit IV is used as published",
          [s for s in explicit if not s.init][0].key_iv == "0x0123")


def test_a_manifest_is_a_list_of_qualities_not_a_file() -> None:
    """A stream published at five resolutions was offered as one row.

    `hls.parse_master` and `dash.parse_mpd` were written, tested and never
    called by anything on the extraction path — so outside YouTube and Vimeo,
    which build their own format lists, the panel showed "the video" for a site
    that had published 1080p, 720p and 480p. Measured against a real public
    stream before the fix: one unlabelled format. After: five, with heights.
    """
    print("\n[a manifest is a list of qualities, not a file]")
    from tests.fixtures import TestOrigin
    from ixd.core.http_client import HttpClient
    from ixd.core.net import NetworkProfile
    from ixd.extractors.generic import GenericExtractor

    master = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=8200000,RESOLUTION=1920x1080,CODECS=\"avc1.640028,mp4a.40.2\"\n"
        "v1080.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=4200000,RESOLUTION=1280x720,CODECS=\"avc1.4d401f,mp4a.40.2\"\n"
        "v720.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=854x480,CODECS=\"avc1.4d401e,mp4a.40.2\"\n"
        "v480.m3u8\n"
    )
    media = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
             "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:6.0,\nseg0.ts\n#EXT-X-ENDLIST\n")

    with TestOrigin(b"\x00" * 4096) as origin:
        client = HttpClient(NetworkProfile())
        origin.state.routes["/master.m3u8"] = (master.encode(), "application/vnd.apple.mpegurl")
        origin.state.routes["/only.m3u8"] = (media.encode(), "application/vnd.apple.mpegurl")
        origin.state.routes["/page.html"] = (
            b'<html><head><title>A Film</title></head><body>'
            b'<script>var player = {"file": "/master.m3u8"};</script>'
            b'</body></html>',
            "text/html; charset=utf-8",
        )

        # 1. The manifest asked for directly — which is what the panel asks
        #    about, because it is what the player was seen fetching.
        info = GenericExtractor(client).extract(origin.url("/master.m3u8"))
        heights = sorted(f.height for f in info.formats)
        check("every rendition is offered, not the manifest as one file",
              len(info.formats) == 3, str([f.format_id for f in info.formats]))
        check("and each one knows its size on screen",
              heights == [480, 720, 1080], str(heights))
        check("each keeps the manifest it came from",
              all(f.manifest_url.endswith("/master.m3u8") for f in info.formats),
              str([f.manifest_url for f in info.formats]))
        check("and each is still an HLS stream",
              all(f.protocol == "m3u8" for f in info.formats),
              str([f.protocol for f in info.formats]))

        # 2. A page that only mentions the manifest gets the same treatment.
        page = GenericExtractor(client).extract(origin.url("/page.html"))
        check("a page pointing at a manifest offers its qualities too",
              len(page.formats) == 3, str([f.format_id for f in page.formats]))

        # 3. A media playlist is one rendition and must stay one row: expanding
        #    it would offer a menu of segments.
        single = GenericExtractor(client).extract(origin.url("/only.m3u8"))
        check("a media playlist is still a single download",
              len(single.formats) == 1, str([f.format_id for f in single.formats]))

        # 4. An origin that refuses the manifest falls back rather than failing.
        origin.state.routes["/gone.m3u8"] = (b"", "application/vnd.apple.mpegurl")
        fallback = GenericExtractor(client).extract(origin.url("/gone.m3u8"))
        check("an unreadable manifest still yields something to download",
              len(fallback.formats) == 1, str([f.format_id for f in fallback.formats]))


def test_a_webm_stream_publishes_an_index_too() -> None:
    """Matroska keeps its index in `Cues`, not in a `sidx`.

    Only the ISOBMFF form was ever read, so every WebM stream reported "this
    stream publishes no segment index" and ran on a single connection however
    many were configured — the format the panel offers under "webm", reported
    from the field on 2026-08-12.
    """
    print("\n[a webm stream publishes an index too]")
    from ixd.core.engine import _index_in
    from ixd.core.webm import (
        ID_CUE_CLUSTER_POSITION, ID_CUE_POINT, ID_CUE_TIME, ID_CUE_TRACK,
        ID_CUE_TRACK_POSITIONS, ID_CUES, ID_INFO, ID_SEGMENT,
        ID_TIMESTAMP_SCALE, element, parse_cues, uint_element,
    )

    def cue(ms: int, position: int) -> bytes:
        return element(ID_CUE_POINT,
                       uint_element(ID_CUE_TIME, ms)
                       + element(ID_CUE_TRACK_POSITIONS,
                                 uint_element(ID_CUE_TRACK, 1)
                                 + uint_element(ID_CUE_CLUSTER_POSITION, position)))

    header = element(ID_SEGMENT,
                     element(ID_INFO, uint_element(ID_TIMESTAMP_SCALE, 1_000_000))
                     + element(ID_CUES, cue(0, 100) + cue(2000, 50_000)
                               + cue(4000, 90_000)))

    index = parse_cues(header)
    check("every cue point is read", len(index) == 3, str(len(index)))
    check("start times come back in milliseconds",
          [entry[1] for entry in index] == [0, 2000, 4000],
          str([entry[1] for entry in index]))
    check("durations are the gap to the next cue",
          [entry[2] for entry in index][:2] == [2000, 2000],
          str([entry[2] for entry in index]))
    check("and the last one's length is left unstated",
          index[-1][2] == 0, str(index[-1][2]))
    check("byte offsets are relative to the segment, not the file",
          index[0][0] > 100 and index[1][0] - index[0][0] == 49_900,
          str([entry[0] for entry in index]))

    # An index is a bonus in both containers: nothing readable means one
    # session, exactly as before, and never an exception.
    check("an unreadable header yields no index rather than raising",
          parse_cues(b"") == [] and parse_cues(b"\x00\x01not matroska") == [])

    # The engine asks one question and gets an answer for either container.
    check("the engine's reader finds a Matroska index",
          len(_index_in(header)) == 3, str(len(_index_in(header))))
    check("…and still finds an ISOBMFF one", _index_in(b"") == [])

# ----------------------------------------------------------------------
_TWITCH_MASTER = """#EXTM3U
#EXT-X-TWITCH-INFO:NODE="video-edge-1.fra01"
#EXT-X-MEDIA:TYPE=VIDEO,GROUP-ID="chunked",NAME="1080p60",AUTOSELECT=YES,DEFAULT=YES
#EXT-X-STREAM-INF:BANDWIDTH=6397504,CODECS="avc1.64002A,mp4a.40.2",\
RESOLUTION=1920x1080,FRAME-RATE=60.000,VIDEO="chunked"
https://cdn.example.net/hash_chan_1_2/chunked/index-dvr.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3438093,CODECS="avc1.4D4020,mp4a.40.2",\
RESOLUTION=1280x720,FRAME-RATE=60.000,VIDEO="720p60"
https://cdn.example.net/hash_chan_1_2/720p60/index-dvr.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=294686,CODECS="avc1.4D400C,mp4a.40.2",\
RESOLUTION=284x160,FRAME-RATE=30.000,VIDEO="160p30"
https://cdn.example.net/hash_chan_1_2/160p30/index-dvr.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=216000,CODECS="mp4a.40.2",VIDEO="audio_only"
https://cdn.example.net/hash_chan_1_2/audio_only/index-dvr.m3u8
""".replace("\\\n", "")


def test_a_twitch_vod_is_a_quality_menu_not_one_rendition() -> None:
    """The master playlist is what carries the resolutions.

    The extension captures whichever *media* playlist the player fetched, and a
    media playlist has no RESOLUTION and no BANDWIDTH in it — so the panel had
    one nameless format and "1080p" silently became 360p (context.md §3.82).
    """
    print("\n[a Twitch VOD is a quality menu, not one rendition]")
    from ixd.extractors.twitch import TwitchExtractor

    formats = hls.parse_master(_TWITCH_MASTER, "https://usher.example/vod/v2/1.m3u8")
    for media in formats:
        TwitchExtractor._label(media, 1139.0)

    check("every rendition is found", len(formats) == 4, str(len(formats)))
    labels = [f.quality_label for f in formats]
    check("each is named by its height",
          {"1080p", "720p", "160p"} <= set(labels), str(labels))
    check("the audio rendition is not called a resolution",
          "Audio only" in labels, str(labels))
    check("and it is offered as audio",
          any(f.ext == "m4a" for f in formats if f.quality_label == "Audio only"))

    tallest = max(formats, key=lambda f: f.height)
    check("the source rendition is 1080p60",
          (tallest.height, tallest.fps) == (1080, 60.0),
          f"{tallest.height}p{tallest.fps:g}")
    check("and is marked as the source", tallest.note == "source", tallest.note)

    # 6,397,504 bit/s over 1139 s ≈ 910,853,632 bytes.
    check("a size is published rather than 'size not published'",
          860 <= tallest.filesize / 1048576 <= 880,
          f"{tallest.filesize / 1048576:.1f} MB")

    chosen = select_format(formats, "1080p")
    check("asking for 1080p yields 1080p", chosen.height == 1080, str(chosen.height))
    chosen = select_format(formats, "720p")
    check("asking for 720p yields 720p", chosen.height == 720, str(chosen.height))


def test_twitch_claims_recordings_and_leaves_live_alone() -> None:
    """A live playlist is a sliding window, so downloading one is not a download.

    Claiming a live channel page would turn "grab this stream" into "grab the
    last forty seconds of it", which is worse than not claiming it at all.
    """
    print("\n[Twitch claims recordings and leaves live channels alone]")
    from ixd.extractors.twitch import TwitchExtractor as T

    claimed = (
        "https://www.twitch.tv/videos/2851508926",
        "https://twitch.tv/videos/12345",
        "https://www.twitch.tv/somechannel/video/999",
        "https://d3stzm2eumvgb4.cloudfront.net/h_chan_1_2/chunked/index-dvr.m3u8",
        "https://d3.cloudfront.net/h_chan_1_2/720p60/highlight-2851508926.m3u8",
    )
    for url in claimed:
        check(f"claims {url.split('/')[-1][:34]}", T.matches(url), url)

    for url in ("https://www.twitch.tv/otplol_",
                "https://www.twitch.tv/directory/game/Chess",
                "https://usher.ttvnw.net/api/v2/channel/hls/otplol_.m3u8"):
        check(f"leaves {url.split('/')[-1][:30]} alone", not T.matches(url), url)

    check("the video id is read from a page URL",
          T.video_id("https://www.twitch.tv/videos/2851508926") == "2851508926")
    check("and from the channel form",
          T.video_id("https://www.twitch.tv/chan/video/42") == "42")
    check("a live page has none", T.video_id("https://www.twitch.tv/chan") == "")


def test_a_twitch_recording_knows_its_own_length_and_channel() -> None:
    """Everything the fallback route needs is in the URL and the playlist."""
    print("\n[a Twitch recording knows its own length and channel]")
    from ixd.extractors.twitch import TwitchExtractor as T, _STORAGE_PLAYLIST

    url = ("https://d3stzm2eumvgb4.cloudfront.net/"
           "b35b67d3c038e70b17d2_waolol1_75329948354_7164302421/"
           "360p30/highlight-2851508926.m3u8")
    match = _STORAGE_PLAYLIST.match(url)
    check("a storage playlist is recognised", match is not None)
    if match:
        check("its rendition is read", match.group("quality") == "360p30",
              match.group("quality"))
        title = T._storage_title(match.group("base"), match.group("name"))
        check("and the channel becomes the name",
              title == "waolol1 - highlight 2851508926", title)
        check("a past broadcast is named for its channel too",
              T._storage_title(match.group("base"), "index-dvr") == "waolol1 - Twitch",
              T._storage_title(match.group("base"), "index-dvr"))

    playlist = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
                "#EXT-X-TWITCH-TOTAL-SECS:1139.0\n#EXTINF:10.000,\n0.ts\n")
    check("the playlist states its own length",
          T._total_seconds(playlist) == 1139.0, str(T._total_seconds(playlist)))
    check("and a playlist without one says zero",
          T._total_seconds("#EXTM3U\n") == 0.0)


def main() -> int:
    print("=" * 68)
    print("Internet Xtreme Downloader — extractor test suite")
    print("=" * 68)
    for test in (test_a_twitch_vod_is_a_quality_menu_not_one_rendition,
                 test_twitch_claims_recordings_and_leaves_live_alone,
                 test_a_twitch_recording_knows_its_own_length_and_channel,
                 test_hls, test_dash, test_generic, test_analysis_does_not_download,
                 test_a_stream_is_named_after_what_it_is,
                 test_the_initialisation_segment_does_not_shift_every_iv,
                 test_a_page_that_delegates_its_player_is_followed,
                 test_a_stream_is_named_after_its_media_not_its_playlist,
                 test_every_quality_of_a_stream_reaches_the_menu,
                 test_original_audio_beats_a_dubbing,
                 test_every_audio_language_survives_extraction,
                 test_youtube_session_warmup, test_youtube_restricted_urls_rank_last,
                 test_every_rendition_reaches_the_menu,
                 test_the_walk_does_not_stop_at_the_first_whole_stream,
                 test_protobuf, test_sabr_framing,
                 test_a_webm_stream_publishes_an_index_too,
        test_a_manifest_is_a_list_of_qualities_not_a_file,
                 test_the_published_index_decides_where_to_continue,
                 test_sabr_request_shape,
        test_a_session_context_is_handed_back, test_attested_endpoint_preference,
                 test_the_stream_header_always_has_a_source,
                 test_a_stopped_transfer_resumes_where_it_stopped,
                 test_a_pause_is_not_the_server_giving_up,
                 test_a_wind_back_survives_to_the_wire,
                 test_captured_session_is_replayed, test_gap_is_never_published,
                 test_interrupted_transfer_resumes,
                 test_mp4_muxer, test_webm_muxer, test_two_fragmented_tracks_become_one_file,
                 test_muxable_audio_choice,
                 test_system_proxy, test_extension_identity, test_youtube_helpers,
                 test_format_selection):
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILED.append(f"{test.__name__} raised {exc}")

    print("\n" + "=" * 68)
    print(f"{len(PASSED)} passed, {len(FAILED)} failed")
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    print("=" * 68)
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
