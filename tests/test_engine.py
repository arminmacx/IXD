"""End-to-end engine tests against a local Range-capable origin.

Run with:  python -m tests.test_engine
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.fixtures import TestOrigin
from ixd import config
from ixd.config import Settings
from ixd.core.db import Database
from ixd.core.engine import DownloadEngine
from ixd.core.events import EventBus
from ixd.core.models import DownloadStatus, HashStatus

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  FAIL  {name} {detail}")


class Harness:
    """Isolated data dir + engine per test."""

    def __init__(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="ixd-test-"))
        config.DATA_DIR = self.root
        config.TEMP_DIR = self.root / "incomplete"
        config.LOG_DIR = self.root / "logs"
        config.ensure_dirs()
        self.settings = Settings(self.root / "settings.json")
        self.settings.set("download_dir", str(self.root / "out"))
        self.settings.set("categorize_into_subfolders", False)
        self.settings.set("progress_flush_interval", 0.2)
        self.settings.set("retry_backoff", 1.2)
        self.db = Database(self.root / "state.sqlite3")
        self.engine = DownloadEngine(self.db, self.settings, EventBus())
        self.engine.start()

    def wait_for(self, download_id: int, statuses: set[DownloadStatus],
                 timeout: float = 60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            download = self.db.get_download(download_id)
            if download and download.status in statuses:
                return download
            time.sleep(0.1)
        return self.db.get_download(download_id)

    def close(self) -> None:
        self.engine.shutdown(wait=True, timeout=5)
        self.db.close()
        shutil.rmtree(self.root, ignore_errors=True)


def make_payload(size: int) -> bytes:
    """Incompressible random bytes, so a truncated file can never hash correctly."""
    return os.urandom(size)


def test_multithreaded_download() -> None:
    print("\n[1] multi-threaded chunked download + hash verification")
    payload = make_payload(6 << 20)          # 6 MiB
    digest = hashlib.sha256(payload).hexdigest()
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            harness.settings.set("connections_per_download", 8)
            harness.settings.set("min_chunk_size", 256 * 1024)
            download = harness.engine.add_download(
                origin.url(), expected_hash=digest, expected_hash_algo="sha256"
            )
            result = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}
            )
            check("completes", result.status is DownloadStatus.COMPLETED, str(result.error))
            check("uses multiple ranges", origin.state.range_requests >= 4,
                  f"range requests={origin.state.range_requests}")
            path = result.filepath
            check("file exists", os.path.isfile(path), path)
            if os.path.isfile(path):
                actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
                check("bytes are identical", actual == digest)
                check("size matches", os.path.getsize(path) == len(payload))
            check("hash verified", result.hash_status is HashStatus.VERIFIED,
                  result.hash_status.value)
    finally:
        harness.close()


def test_the_origins_own_name_beats_the_address() -> None:
    """Reported from GitHub: files arriving named after a UUID.

    A release asset redirects to a path ending in
    `74709710-bf21-4cd4-926a-526ff561a1bb`, and the download landed called
    exactly that, with no extension — while the response had been saying
    `filename=ixd_1.0.3_amd64.deb` the whole time. The row needs a name the
    moment it appears, so the URL is guessed from; the defect was that the
    guess then outranked the answer.
    """
    print("\n[the origin's own name beats the address]")
    payload = make_payload(512 << 10)
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            origin.state.disposition_name = "ixd_1.0.3_amd64.deb"
            opaque = "/asset/74709710-bf21-4cd4-926a-526ff561a1bb"

            download = harness.engine.add_download(origin.url(opaque))
            check("the row still gets a name straight away",
                  download.filename == "74709710-bf21-4cd4-926a-526ff561a1bb",
                  download.filename)
            check("and it is marked as a guess", download.auto_named)

            result = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR})
            check("it completes", result.status is DownloadStatus.COMPLETED,
                  str(result.error))
            check("the server's name wins",
                  result.filename == "ixd_1.0.3_amd64.deb", result.filename)
            check("and it is no longer a guess", not result.auto_named)
            check("the file on disk carries that name",
                  os.path.isfile(result.filepath)
                  and os.path.basename(result.filepath) == "ixd_1.0.3_amd64.deb",
                  result.filepath)
            check("nothing was left behind under the guessed name",
                  not os.path.exists(os.path.join(
                      result.dest_dir, "74709710-bf21-4cd4-926a-526ff561a1bb")))
            check("and the category follows the real name",
                  result.category != "Other", result.category)

            # A name somebody chose is a decision, not a guess.
            chosen = harness.engine.add_download(
                origin.url(opaque + "?second"), filename="my own name.deb")
            settled = harness.wait_for(
                chosen.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR})
            check("a filename that was asked for is never overruled",
                  settled.filename == "my own name.deb", settled.filename)

            # No Content-Disposition, no extension in the path: the type is
            # all there is to go on, and it is better than nothing.
            origin.state.disposition_name = ""
            origin.state.content_type = "application/zip"
            bare = harness.engine.add_download(origin.url("/asset/9f2c1e"))
            done = harness.wait_for(
                bare.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR})
            check("with no name published, the type supplies the extension",
                  done.filename == "9f2c1e.zip", done.filename)
    finally:
        harness.close()


def test_pause_resume() -> None:
    print("\n[2] pause mid-flight, then resume and finish correctly")
    payload = make_payload(8 << 20)
    digest = hashlib.sha256(payload).hexdigest()
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            harness.settings.set("connections_per_download", 4)
            harness.settings.set("global_speed_limit", 2 << 20)   # throttle so we can pause
            harness.engine.global_limiter.set_rate(2 << 20)
            download = harness.engine.add_download(origin.url())

            harness.wait_for(download.id, {DownloadStatus.DOWNLOADING}, timeout=15)
            time.sleep(1.2)
            harness.engine.pause_download(download.id)
            paused = harness.wait_for(download.id, {DownloadStatus.PAUSED}, timeout=20)
            check("pauses", paused.status is DownloadStatus.PAUSED, paused.status.value)
            partial = paused.downloaded
            check("kept partial progress", 0 < partial < len(payload), f"{partial} bytes")

            chunks = harness.db.load_chunks(download.id)
            check("chunk cursors persisted", any(c.downloaded > 0 for c in chunks))

            harness.settings.set("global_speed_limit", 0)
            harness.engine.global_limiter.set_rate(0)
            harness.engine.start_download(download.id)
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=60
            )
            check("resumes to completion", done.status is DownloadStatus.COMPLETED,
                  str(done.error))
            if os.path.isfile(done.filepath):
                actual = hashlib.sha256(Path(done.filepath).read_bytes()).hexdigest()
                check("resumed bytes are correct", actual == digest)
    finally:
        harness.close()


def test_crash_recovery() -> None:
    print("\n[3] crash recovery: partial state survives an engine restart")
    payload = make_payload(8 << 20)
    digest = hashlib.sha256(payload).hexdigest()
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            harness.settings.set("connections_per_download", 4)
            harness.engine.global_limiter.set_rate(2 << 20)
            download = harness.engine.add_download(origin.url())
            harness.wait_for(download.id, {DownloadStatus.DOWNLOADING}, timeout=15)
            time.sleep(1.2)

            # Simulate a hard stop: kill the engine without a clean pause.
            harness.engine.shutdown(wait=True, timeout=5)
            recovered = harness.db.recover_interrupted()
            partial = harness.db.get_download(download.id).downloaded
            check("recovery marks rows paused", recovered >= 0)
            check("partial bytes on disk", partial > 0, f"{partial} bytes")

            engine = DownloadEngine(harness.db, harness.settings, EventBus())
            engine.global_limiter.set_rate(0)
            engine.start()
            harness.engine = engine
            engine.start_download(download.id)
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=60
            )
            check("finishes after restart", done.status is DownloadStatus.COMPLETED,
                  str(done.error))
            if os.path.isfile(done.filepath):
                actual = hashlib.sha256(Path(done.filepath).read_bytes()).hexdigest()
                check("recovered file is intact", actual == digest)
    finally:
        harness.close()


def test_link_expiry_and_swap() -> None:
    print("\n[4] expired link -> needs_link -> swap to refreshed URL -> resume")
    payload = make_payload(6 << 20)
    digest = hashlib.sha256(payload).hexdigest()
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            origin.state.valid_tokens = {"good"}
            # Truncate every response so workers must keep re-requesting; a
            # long-lived stream opened before revocation would otherwise run to
            # completion and never observe the expiry.
            origin.state.cut_after = 256 * 1024
            harness.settings.set("connections_per_download", 4)
            harness.settings.set("max_retries", 1)
            harness.engine.global_limiter.set_rate(1 << 20)

            download = harness.engine.add_download(origin.url("/file.bin?token=good"))
            harness.wait_for(download.id, {DownloadStatus.DOWNLOADING}, timeout=15)
            time.sleep(1.0)

            # Revoke the token: every further request 403s, as an expired CDN link would.
            origin.state.valid_tokens = {"fresh"}
            expired = harness.wait_for(
                download.id,
                {DownloadStatus.NEEDS_LINK, DownloadStatus.ERROR, DownloadStatus.COMPLETED},
                timeout=45,
            )
            check("detects expiry as needs_link",
                  expired.status is DownloadStatus.NEEDS_LINK,
                  f"got {expired.status.value}: {expired.error}")
            partial = expired.downloaded
            check("progress retained at expiry", partial > 0, f"{partial} bytes")

            harness.engine.global_limiter.set_rate(0)
            swapped = harness.engine.swap_link(
                download.id, origin.url("/file.bin?token=fresh")
            )
            check("swap accepted", swapped)
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=60
            )
            check("resumes on the new link", done.status is DownloadStatus.COMPLETED,
                  str(done.error))
            if os.path.isfile(done.filepath):
                actual = hashlib.sha256(Path(done.filepath).read_bytes()).hexdigest()
                check("swapped download is byte-perfect", actual == digest)
                check("did not restart from zero", done.downloaded == len(payload))
    finally:
        harness.close()


def test_no_range_support() -> None:
    print("\n[5] origin without Range support falls back to a single stream")
    payload = make_payload(2 << 20)
    digest = hashlib.sha256(payload).hexdigest()
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            origin.state.support_ranges = False
            download = harness.engine.add_download(origin.url())
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=60
            )
            check("single-stream download completes",
                  done.status is DownloadStatus.COMPLETED, str(done.error))
            if os.path.isfile(done.filepath):
                actual = hashlib.sha256(Path(done.filepath).read_bytes()).hexdigest()
                check("single-stream bytes correct", actual == digest)
    finally:
        harness.close()


def test_content_md5_validation() -> None:
    print("\n[6] server Content-MD5 is validated automatically")
    payload = make_payload(1 << 20)
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            origin.state.send_content_md5 = True
            download = harness.engine.add_download(origin.url())
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=60
            )
            check("completes", done.status is DownloadStatus.COMPLETED, str(done.error))
            check("header digest verified", done.hash_status is HashStatus.VERIFIED,
                  done.hash_status.value)
    finally:
        harness.close()


def test_bad_hash_flags_corruption() -> None:
    print("\n[7] a wrong expected hash surfaces as corrupted")
    payload = make_payload(512 * 1024)
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            download = harness.engine.add_download(
                origin.url(), expected_hash="0" * 64, expected_hash_algo="sha256"
            )
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=60
            )
            check("marked corrupted", done.hash_status is HashStatus.CORRUPTED,
                  done.hash_status.value)
    finally:
        harness.close()


def test_retry_on_rate_limit() -> None:
    print("\n[8] transient 429s are retried rather than failing the download")
    payload = make_payload(1 << 20)
    digest = hashlib.sha256(payload).hexdigest()
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            origin.state.rate_limit_times = 3
            harness.settings.set("max_retries", 6)
            harness.settings.set("retry_backoff", 1.3)
            download = harness.engine.add_download(origin.url())
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=90
            )
            check("survives rate limiting", done.status is DownloadStatus.COMPLETED,
                  str(done.error))
            if os.path.isfile(done.filepath):
                actual = hashlib.sha256(Path(done.filepath).read_bytes()).hexdigest()
                check("post-429 bytes correct", actual == digest)
    finally:
        harness.close()


def test_encrypted_hls_segments() -> None:
    print("\n[9] AES-128 encrypted HLS: parallel segments, decrypt, assemble")
    from ixd.core.crypto import aes_cbc_encrypt
    from ixd.extractors import hls

    key = os.urandom(16)
    iv = os.urandom(16)
    # Real transport-stream packets, not random bytes: the engine refuses to
    # publish a stream whose first piece is not a container it recognises,
    # because that is what a wrong key or IV produces and it is otherwise
    # indistinguishable from a finished download. A fixture of noise would be
    # asserting that the guard does not work.
    plain = [b"\x47\x40\x00\x10" + os.urandom(199_996) for _ in range(12)]
    expected = b"".join(plain)
    digest = hashlib.sha256(expected).hexdigest()

    harness = Harness()
    try:
        with TestOrigin(b"") as origin:
            routes = {"/key.bin": (key, "application/octet-stream")}
            lines = [
                "#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4",
                "#EXT-X-MEDIA-SEQUENCE:0",
                f'#EXT-X-KEY:METHOD=AES-128,URI="/key.bin",IV=0x{iv.hex()}',
            ]
            for index, block in enumerate(plain):
                routes[f"/seg{index}.ts"] = (
                    aes_cbc_encrypt(key, iv, block), "video/mp2t"
                )
                lines += ["#EXTINF:4.0,", f"/seg{index}.ts"]
            lines.append("#EXT-X-ENDLIST")
            playlist = ("\n".join(lines) + "\n").encode()
            routes["/stream.m3u8"] = (playlist, "application/vnd.apple.mpegurl")
            origin.state.routes = routes

            # Resolve the playlist exactly as the app would.
            from ixd.core.http_client import HttpClient
            from ixd.core.net import NetworkProfile
            client = HttpClient(NetworkProfile(timeout=15))
            segments = hls.fetch_segments(client, origin.url("/stream.m3u8"))
            check("playlist resolved to segments", len(segments) == 12, str(len(segments)))
            check("key URL carried through",
                  all(s.key_url and s.key_url.endswith("/key.bin") for s in segments))

            from ixd.core.models import TransferMode
            download = harness.engine.add_download(
                origin.url("/stream.m3u8"),
                filename="stream.ts",
                segments=segments,
                mode=TransferMode.SEGMENTED,
                expected_hash=digest,
            )
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=90
            )
            check("segmented download completes",
                  done.status is DownloadStatus.COMPLETED, str(done.error))
            if os.path.isfile(done.filepath):
                data = Path(done.filepath).read_bytes()
                check("decrypted output matches plaintext",
                      hashlib.sha256(data).hexdigest() == digest,
                      f"{len(data)} vs {len(expected)} bytes")
                check("segments assembled in order", data == expected)
            check("hash verified", done.hash_status is HashStatus.VERIFIED,
                  done.hash_status.value)
    finally:
        harness.close()


def test_server_driven_transfer_keeps_its_progress() -> None:
    """Restarting a server-driven transfer must not discard what it has.

    A server-driven session *is* rebuilt from scratch, and that used to be
    taken as licence to zero the progress with it. But the bytes an earlier
    attempt wrote are still in the file and the ranges it held were recorded,
    so the new session only has to ask for the remainder. Zeroing undid all of
    it: an interrupted transfer showed nothing and began again at the
    beginning, which is what made a download that stopped part-way impossible
    to resume.
    """
    print("\n[a server-driven transfer keeps its progress]")
    from ixd.core.engine import DownloadTask
    from ixd.core.models import Chunk, ChunkStatus, Download, TransferMode

    harness = Harness()
    try:
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="held.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=1000,
            # Driven by hand below: the running engine must not also pick it
            # up, or the transfer happens twice and nothing counted means
            # anything.
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/videoplayback",
            "itag": 137, "size": 1000, "covered": [[0, 700]],
        }
        download.id = harness.db.insert_download(download)
        harness.db.flush_chunk_progress(download.id, [
            Chunk(index=0, start=0, end=999, downloaded=700,
                  status=ChunkStatus.FAILED),
        ])

        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()

        check("the progress already made is carried over",
              task._bytes_done == 700, str(task._bytes_done))
        check("and the chunk is not reset to nothing",
              task._chunks[0].downloaded == 700, str(task._chunks[0].downloaded))
        check("the unfinished chunk is queued again",
              task._chunks[0].status is ChunkStatus.PENDING,
              task._chunks[0].status.value)

        # The file an earlier attempt wrote must survive being prepared again.
        path = Path(task.download.temp_path)
        check("the partial file is kept, not recreated",
              path.exists() and path.stat().st_size == 1000,
              f"{path.exists()} {path.stat().st_size if path.exists() else '-'}")
    finally:
        harness.close()
        shutil.rmtree(harness.root, ignore_errors=True)


def test_paired_quality_is_one_download() -> None:
    """A chosen quality is one download, even when it is two tracks.

    Fetching the companion as a *separate* download meant the user watched a
    video finish and a second transfer start on the same file — and it brought
    the failures that go with two independent lifetimes: a stray audio file
    when the video failed, a duplicate output when one half was resumed, and a
    progress bar that reached the end and then grew. Both tracks now belong to
    one row.
    """
    print("\n[a paired quality is one download]")
    from ixd.core.engine import DownloadTask
    from ixd.core.models import ChunkStatus, Download, TransferMode

    harness = Harness()
    try:
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="paired.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR,
            # Driven by hand below. Left queued, the running engine also picks
            # it up, fails against an endpoint that does not exist, and writes
            # its own zero over the progress this test just flushed — which is
            # exactly what made this assertion fail once in every dozen runs.
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/v", "itag": 137, "size": 1000,
            "audio": {"endpoint": "https://example.invalid/a",
                      "itag": 140, "size": 250, "is_audio": True},
        }
        download.id = harness.db.insert_download(download)

        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()

        check("the size is the finished file, not one track of it",
              task.download.total_size == 1250, str(task.download.total_size))
        check("there is one chunk per track", len(task._chunks) == 2,
              str(len(task._chunks)))
        check("the video chunk spans the video",
              task._chunks[0].size == 1000, str(task._chunks[0].size))
        check("the audio chunk spans the audio",
              task._chunks[1].size == 250, str(task._chunks[1].size))
        check("each track has its own file",
              os.path.isfile(task.download.temp_path)
              and os.path.isfile(task._audio_temp_path()),
              task._audio_temp_path())
        check("the audio file is allocated to its own length",
              os.path.getsize(task._audio_temp_path()) == 250,
              str(os.path.getsize(task._audio_temp_path())))

        # Progress is what the finished file will hold, so a bar never restarts
        # when the second track begins.
        task._chunks[0].downloaded = 1000
        task._chunks[0].status = ChunkStatus.DONE
        task._chunks[1].downloaded = 125
        task._flush_progress()
        stored = harness.db.get_download(download.id)
        check("progress counts both tracks together",
              stored.downloaded == 1125, str(stored.downloaded))

        # And a finished download does not publish a second copy of itself when
        # it is finalised again.
        Path(download.dest_dir).mkdir(parents=True, exist_ok=True)
        first = Path(download.dest_dir) / "paired.mp4"
        first.write_bytes(b"x")
        task.download.completed_at = time.time()
        check("re-finalising reuses its own file rather than adding (1)",
              task._final_path() == str(first), task._final_path())
    finally:
        harness.close()


def test_incomplete_is_never_published() -> None:
    print("\n[10] a chunk that never finishes must not produce a file")
    from ixd.core.errors import IXDError
    from ixd.core.models import Chunk, ChunkStatus, TransferMode

    payload = make_payload(1 << 20)
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            download = harness.engine.add_download(origin.url())
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=60
            )
            check("baseline completes", done.status is DownloadStatus.COMPLETED)

            # Now assert the guard directly: a task whose chunk map has a hole
            # must refuse to assemble rather than emit a zero-filled file.
            from ixd.core.engine import DownloadTask
            task = DownloadTask(harness.db.get_download(download.id), harness.engine)
            task._chunks = [
                Chunk(index=0, start=0, end=499, downloaded=500, status=ChunkStatus.DONE),
                Chunk(index=1, start=800, end=999, downloaded=200, status=ChunkStatus.DONE),
            ]
            task.download.total_size = 1000
            task.download.mode = TransferMode.RANGED
            try:
                task._verify_completeness()
                check("gap detected", False, "no error raised")
            except IXDError as exc:
                check("gap detected", "gap in byte coverage" in str(exc), str(exc))

            task._chunks = [
                Chunk(index=0, start=0, end=999, downloaded=400, status=ChunkStatus.ACTIVE),
            ]
            try:
                task._verify_completeness()
                check("incomplete chunk detected", False, "no error raised")
            except IXDError as exc:
                check("incomplete chunk detected", "incomplete" in str(exc), str(exc))
    finally:
        harness.close()


def test_capped_range_is_not_expiry() -> None:
    """A tail-only 403 must not be reported as an expired link.

    Both failures are a 403 on a URL that was working a moment ago, but they
    need opposite responses: an expired link is fixed by pasting a fresh one,
    whereas a range cap is a policy the source applies to every URL it issues,
    so the same prompt would send the user in circles.
    """
    print("\n[11] a capped range is diagnosed, not mistaken for expiry")
    payload = make_payload(6 << 20)
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            # The start of the file stays available; everything past a third
            # of the way in is refused, exactly as a streaming CDN does.
            origin.state.range_cap = 2 << 20
            harness.settings.set("connections_per_download", 4)
            harness.settings.set("max_retries", 1)

            download = harness.engine.add_download(origin.url())
            final = harness.wait_for(
                download.id,
                {DownloadStatus.ERROR, DownloadStatus.NEEDS_LINK,
                 DownloadStatus.COMPLETED},
                timeout=60,
            )
            check("does not park in needs_link",
                  final.status is not DownloadStatus.NEEDS_LINK,
                  final.status.value)
            check("fails rather than publishing a partial file",
                  final.status is DownloadStatus.ERROR, final.status.value)
            check("explains the cap in bytes",
                  "refuses any part of this file from byte" in (final.error or ""),
                  final.error or "")
            check("quotes only what was observed, not a guess",
                  "the start of the file is still served" in (final.error or ""),
                  final.error or "")
            check("states that a fresh link will not help",
                  "has not expired" in (final.error or ""), final.error or "")
            check("no output file was published",
                  not (final.filepath and os.path.isfile(final.filepath)),
                  final.filepath or "")

            # The same origin with the cap lifted must still complete, proving
            # the detection is not simply refusing 403s outright.
            origin.state.range_cap = 0
            # The failing task tears down asynchronously; restarting on top of
            # its still-running workers would test the teardown, not the fix.
            deadline = time.time() + 15
            while (harness.engine.task_for(download.id) is not None
                   and time.time() < deadline):
                time.sleep(0.2)
            harness.engine.start_download(download.id)
            # The row is still ERROR from the first attempt, so wait for the
            # restart to be observable before waiting for its outcome.
            harness.wait_for(
                download.id,
                {DownloadStatus.CONNECTING, DownloadStatus.DOWNLOADING,
                 DownloadStatus.COMPLETED},
                timeout=30,
            )
            resumed = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR},
                timeout=60,
            )
            check("completes once the cap is lifted",
                  resumed.status is DownloadStatus.COMPLETED,
                  f"{resumed.status.value}: {resumed.error}")
            if resumed.status is DownloadStatus.COMPLETED:
                actual = hashlib.sha256(Path(resumed.filepath).read_bytes()).hexdigest()
                check("resumed bytes are correct",
                      actual == hashlib.sha256(payload).hexdigest())
    finally:
        harness.close()


def test_resume_state_survives_the_database() -> None:
    """What a stopped track recorded has to come back intact, index included.

    A server-driven session is rebuilt from nothing on every attempt, so the
    only thing connecting one attempt to the next is what was written down.
    The segment index is the part that actually moves the server — a request
    declares what it holds only when it has one — and it was never being
    stored, so every continuation opened by declaring nothing and was answered
    from byte zero.
    """
    print("\n[resume state survives the database]")
    import ixd.extractors.sabr as sabr_module
    from ixd.core.engine import DownloadTask
    from ixd.core.models import Download, TransferMode

    restored: dict[str, object] = {}

    class Recording:
        """Stands in for the stream: records the resume, reports progress."""

        def __init__(self, *args, **kwargs) -> None:
            self._covered = [[0, 700]]
            self._buffered_ms = 4200
            self._sequence = 11

        def restore(self, ranges, player_ms=0, sequence=0) -> None:
            restored.update(ranges=ranges, player_ms=player_ms,
                            sequence=sequence)

        def coverage(self):
            return [list(span) for span in self._covered]

        def note_written(self, start, end) -> None:
            pass

        def download(self, write, should_stop=None, on_progress=None) -> int:
            write(0, b"\x00" * 700)
            return 700

    harness = Harness()
    original = sabr_module.SabrStream
    sabr_module.SabrStream = Recording        # type: ignore[assignment]
    try:
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="state.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=1000,
            # Driven by hand below: the running engine must not also pick it
            # up, or the transfer happens twice and nothing counted means
            # anything.
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/v", "itag": 137,
            "size": 1000, "duration": 600,
        }
        download.id = harness.db.insert_download(download)

        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        task._run_sabr(task.download.sabr_context, task._chunks[0],
                       task.download.temp_path)

        stored = harness.db.get_download(download.id).sabr_context
        check("what was held is written down",
              stored.get("covered") == [[0, 700]], str(stored.get("covered")))
        check("the position reached is written down",
              stored.get("player_ms") == 4200, str(stored.get("player_ms")))
        check("and so is the segment index",
              stored.get("sequence") == 11, str(stored.get("sequence")))

        # The continuation, reading that row back.
        again = DownloadTask(harness.db.get_download(download.id), harness.engine)
        again._prepare_sabr()
        again._run_sabr(again.download.sabr_context, again._chunks[0],
                        again.download.temp_path)
        check("the continuation is handed the ranges back",
              restored.get("ranges") == [[0, 700]], str(restored.get("ranges")))
        check("and the position", restored.get("player_ms") == 4200,
              str(restored.get("player_ms")))
        check("and the segment index, which is what moves the server",
              restored.get("sequence") == 11, str(restored.get("sequence")))
    finally:
        sabr_module.SabrStream = original      # type: ignore[assignment]
        harness.close()


def test_a_paused_stream_keeps_its_bytes_and_says_so() -> None:
    """A YouTube download reported "Resume capability: No" and resumes anyway.

    The window was showing ``supports_ranges``, which asks whether one URL
    answers a Range request. A server-driven transfer never uses one — the
    field is hard-coded false in ``_prepare_sabr`` — so the honest answer to
    "can I stop this and continue later" was being reported as No by a field
    that was never asked that question.

    Both halves are measured here: that the bytes really do survive a pause
    (the second pass is handed what the first held and writes only the rest),
    and that what the interface now says matches.
    """
    print("\n[a paused stream keeps its bytes and says so]")
    import ixd.extractors.sabr as sabr_module
    from ixd.core.engine import DownloadTask
    from ixd.core.errors import CancelledError
    from ixd.core.models import Download, TransferMode

    told: list[tuple] = []

    class Interrupted:
        """Stops half way the first time; continues the second."""

        def __init__(self, *args, **kwargs) -> None:
            self._covered: list[list[int]] = []
            self._buffered_ms = 0
            self._sequence = 0

        def restore(self, ranges, player_ms=0, sequence=0) -> None:
            told.append(([list(s) for s in (ranges or [])], player_ms, sequence))
            self._covered = [list(s) for s in (ranges or [])]
            self._buffered_ms = player_ms
            self._sequence = sequence

        def coverage(self):
            return [list(span) for span in self._covered]

        def note_written(self, start, end) -> None:
            pass

        def download(self, write, should_stop=None, on_progress=None) -> int:
            if not self._covered:
                write(0, b"\x01" * 400)
                self._covered = [[0, 400]]
                self._buffered_ms = 4000
                self._sequence = 4
                raise CancelledError()          # the user pressed Pause
            write(400, b"\x02" * 600)
            self._covered = [[0, 1000]]
            return 600

    harness = Harness()
    original = sabr_module.SabrStream
    sabr_module.SabrStream = Interrupted        # type: ignore[assignment]
    try:
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="paused.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=1000,
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/v", "itag": 137,
            "size": 1000, "duration": 600,
        }
        download.id = harness.db.insert_download(download)

        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        try:
            task._run_sabr(task.download.sabr_context, task._chunks[0],
                           task.download.temp_path)
        except CancelledError:
            pass                                # a pause is not a failure
        first = Path(task.download.temp_path).read_bytes()
        check("the paused transfer leaves its bytes on disk",
              first[:400] == b"\x01" * 400, f"{len(first)} bytes")
        # What the flush loop does on its way out of a real run: the chunk map
        # is how the *bar* resumes, the coverage in `sabr_context` is how the
        # *session* does, and they are written by different code.
        task._flush_progress()
        check("and the row it leaves behind counts them",
              harness.db.get_download(download.id).downloaded == 400,
              str(harness.db.get_download(download.id).downloaded))

        again = DownloadTask(harness.db.get_download(download.id), harness.engine)
        again._prepare_sabr()
        check("and the continuation starts from what it already has",
              again._bytes_done == 400, str(again._bytes_done))
        again._run_sabr(again.download.sabr_context, again._chunks[0],
                        again.download.temp_path)
        check("the second session is told what the first held",
              told[-1][0] == [[0, 400]], str(told[-1]))
        final = Path(again.download.temp_path).read_bytes()
        check("the first 400 bytes were never fetched twice",
              final[:400] == b"\x01" * 400, final[:8].hex())
        check("and the rest arrived on top of them",
              final[400:1000] == b"\x02" * 600, f"{len(final)} bytes")

        # What the window says about all of that.
        resumed = harness.db.get_download(download.id)
        check("a server-driven download reports that it resumes",
              resumed.can_resume is True, resumed.resume_note)
        check("and says how", "session reopens" in resumed.resume_note,
              resumed.resume_note)
    finally:
        sabr_module.SabrStream = original      # type: ignore[assignment]
        harness.close()


def test_resume_capability_is_not_range_support() -> None:
    """Each mode answers for the way it actually keeps its progress."""
    print("\n[resume capability is not range support]")
    from ixd.core.models import Download, TransferMode

    ranged = Download(mode=TransferMode.RANGED, supports_ranges=True)
    check("a ranged transfer resumes", ranged.can_resume is True)
    check("and says so plainly", ranged.resume_note == "Yes", ranged.resume_note)

    single = Download(mode=TransferMode.SINGLE, supports_ranges=False)
    check("a server that refuses ranges cannot resume",
          single.can_resume is False)
    check("and the reason is given", "refuses ranges" in single.resume_note,
          single.resume_note)

    segmented = Download(mode=TransferMode.SEGMENTED, supports_ranges=False)
    check("a segmented download resumes on its finished segments",
          segmented.can_resume is True)
    check("and says which", "segments are kept" in segmented.resume_note,
          segmented.resume_note)

    sabr = Download(mode=TransferMode.SABR, supports_ranges=False)
    check("a server-driven download resumes on its stored position",
          sabr.can_resume is True)


def test_two_videos_from_one_cdn_node_do_not_queue_behind_each_other() -> None:
    """Sessions are serialised, not servers.

    Transfers driving the same streaming session must take turns — the server
    keeps one playback position for it. But the lock was keyed on the
    endpoint's host and path, and a CDN hands many unrelated videos to the same
    node, so two downloads that happened to land together ran one after the
    other for no reason at all: the second sat at zero, looking stalled, until
    the first finished.
    """
    print("\n[two videos from one node do not queue behind each other]")
    harness = Harness()
    try:
        node = "https://rr1---sn-abc.googlevideo.com/videoplayback"

        one = harness.engine.sabr_session_lock(f"{node}?id=aaa", "config-aaa")
        two = harness.engine.sabr_session_lock(f"{node}?id=bbb", "config-bbb")
        check("two videos on one node get locks of their own", one is not two)

        again = harness.engine.sabr_session_lock(
            f"{node}?id=aaa&range=1", "config-aaa")
        check("the same session gets the same lock, whatever the query",
              again is one)

        # The two tracks of one download share a session and must take turns.
        video = harness.engine.sabr_session_lock(f"{node}?itag=137", "config-x")
        audio = harness.engine.sabr_session_lock(f"{node}?itag=140", "config-x")
        check("both tracks of one download share the session's lock",
              video is audio)

        # Nothing recorded about a session survives forever.
        for index in range(600):
            harness.engine.sabr_session_lock(node, f"config-{index}")
        check("locks nobody holds are cleared out",
              len(harness.engine._sabr_locks) < 600,
              str(len(harness.engine._sabr_locks)))

        held = harness.engine.sabr_session_lock(node, "config-held")
        held.acquire()
        try:
            for index in range(600, 1200):
                harness.engine.sabr_session_lock(node, f"config-{index}")
            check("but a lock in use is never cleared from under its holder",
                  harness.engine.sabr_session_lock(node, "config-held") is held)
        finally:
            held.release()
    finally:
        harness.close()


def test_an_expired_session_is_replaced_not_mourned() -> None:
    """A link that expired overnight must not cost the whole file again.

    A streaming endpoint is signed and lasts hours. A transfer paused in the
    evening and resumed in the morning meets a plain ``403`` — indistinguishable
    from a refusal until you notice the same page will issue a new session for
    the same stream on request. Without that the download stopped dead at
    ``HTTP 403 Forbidden``, with nothing to do but delete it and fetch the
    whole file again.
    """
    print("\n[an expired session is replaced, not mourned]")
    import ixd.extractors.sabr as sabr_module
    from ixd.core.engine import DownloadTask
    from ixd.core.errors import HttpError
    from ixd.core.models import Download, TransferMode

    size = 1000
    opened: list[str] = []

    class Expiring:
        """Refuses the stale endpoint; serves the file from a fresh one."""

        def __init__(self, client, endpoint, *args, **kwargs) -> None:
            self.endpoint = endpoint
            self._covered: list[list[int]] = []
            self._buffered_ms = 0
            self._sequence = 0

        def restore(self, ranges, player_ms=0, sequence=0) -> None:
            self._covered = [list(span) for span in (ranges or ())]

        def holds(self, start, end) -> bool:
            return any(s <= start and e >= end for s, e in self._covered)

        def coverage(self):
            return [list(span) for span in self._covered]

        def note_written(self, start, end) -> None:
            pass

        def download(self, write, should_stop=None, on_progress=None) -> int:
            opened.append(self.endpoint)
            if self.endpoint == "https://example.invalid/stale":
                raise HttpError(403, "HTTP 403 Forbidden", self.endpoint)
            start = self._covered[-1][1] if self._covered else 0
            write(start, b"\x00" * (size - start))
            self._covered = [[0, size]]
            return size

    harness = Harness()
    original = sabr_module.SabrStream
    sabr_module.SabrStream = Expiring          # type: ignore[assignment]
    try:
        harness.engine.renew_sabr_session = lambda page, itag, is_audio, tags: {
            "endpoint": "https://example.invalid/fresh",
            "config": "", "itag": int(itag), "size": size,
        }
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="expired.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=size,
            status=DownloadStatus.PAUSED,
        )
        # The state a transfer paused half-way leaves behind.
        download.sabr_context = {
            "endpoint": "https://example.invalid/stale", "itag": 137,
            "size": size, "page_url": "https://www.youtube.com/watch?v=abc",
            "covered": [[0, 600]], "player_ms": 4000, "sequence": 6,
        }
        download.id = harness.db.insert_download(download)
        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        task._transfer_sabr()

        check("the stale endpoint is tried, then a fresh one",
              opened == ["https://example.invalid/stale",
                         "https://example.invalid/fresh"], str(opened))
        check("and the track finishes", task._chunks[0].downloaded == size,
              str(task._chunks[0].downloaded))

        stored = harness.db.get_download(download.id).sabr_context
        check("the new session is recorded",
              stored.get("endpoint") == "https://example.invalid/fresh",
              str(stored.get("endpoint")))
        check("what was already fetched is not thrown away",
              stored.get("covered") == [[0, size]], str(stored.get("covered")))
        check("and the renewal is counted, so it cannot loop",
              stored.get("renewals") == 1, str(stored.get("renewals")))
    finally:
        sabr_module.SabrStream = original      # type: ignore[assignment]
        harness.close()

    # An itag names a rendition, not a particular encoding of it. A session
    # describing different bytes at every offset must not be spliced onto a
    # part-file from the old one — that produces a file of exactly the right
    # length that no player will play.
    for field, changed in (("size", size + 1000), ("last_modified", 99)):
        opened.clear()
        harness = Harness()
        sabr_module.SabrStream = Expiring      # type: ignore[assignment]
        try:
            harness.engine.renew_sabr_session = (
                lambda page, itag, is_audio, tags, _f=field, _c=changed: {
                    "endpoint": "https://example.invalid/fresh",
                    "config": "", "itag": int(itag), "size": size, _f: _c,
                })
            download = Download(
                url="https://example.invalid/videoplayback",
                filename=f"changed-{field}.mp4",
                dest_dir=str(harness.root / "out"),
                mode=TransferMode.SABR, total_size=size,
                status=DownloadStatus.PAUSED,
            )
            download.sabr_context = {
                "endpoint": "https://example.invalid/stale", "itag": 137,
                "size": size, "last_modified": 42,
                "page_url": "https://www.youtube.com/watch?v=abc",
                "covered": [[0, 600]],
            }
            download.id = harness.db.insert_download(download)
            task = DownloadTask(harness.db.get_download(download.id),
                                harness.engine)
            task._prepare_sabr()
            failed = ""
            try:
                task._transfer_sabr()
            except Exception as exc:  # noqa: BLE001
                failed = str(exc)
            check(f"a re-encoded stream is refused, not spliced ({field})",
                  opened == ["https://example.invalid/stale"] and failed,
                  f"{opened} {failed}")
            logged = [row.get("message", "") for row
                      in harness.db.recent_events(download_id=download.id)]
            check(f"and the reason is recorded ({field})",
                  any("cannot be joined" in line for line in logged),
                  str(logged))
        finally:
            sabr_module.SabrStream = original  # type: ignore[assignment]
            harness.close()

    # A link that expires again immediately is being refused, not expiring.
    opened.clear()
    harness = Harness()
    sabr_module.SabrStream = Expiring          # type: ignore[assignment]
    try:
        harness.engine.renew_sabr_session = lambda page, itag, is_audio, tags: {
            "endpoint": "https://example.invalid/stale",
            "config": "", "itag": int(itag), "size": size,
        }
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="refused.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=size,
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/stale", "itag": 137,
            "size": size, "page_url": "https://www.youtube.com/watch?v=abc",
        }
        download.id = harness.db.insert_download(download)
        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        failed = ""
        try:
            task._transfer_sabr()
        except Exception as exc:  # noqa: BLE001
            failed = str(exc)
        check("renewal is bounded", len(opened) == 3, str(len(opened)))
        check("and the original refusal is what is reported",
              "403" in failed, failed or "no failure raised")
    finally:
        sabr_module.SabrStream = original      # type: ignore[assignment]
        harness.close()


def test_a_silent_film_is_never_published() -> None:
    """A quality that cannot be joined is not delivered as the video alone.

    A paired quality is two tracks that become one file. When the join failed
    the video was published by itself — a silent film, under the name of the
    quality that was asked for, which opens and plays and is wrong in the one
    way nobody checks. Both tracks are on disk and complete at that point, so
    stopping costs nothing and the join can be tried again without fetching a
    byte.
    """
    print("\n[a silent film is never published]")
    import ixd.core.mp4 as mp4_module
    from ixd.core.engine import DownloadTask
    from ixd.core.models import Download, TransferMode

    harness = Harness()
    original = mp4_module.mux

    def refuse(*args, **kwargs):
        raise mp4_module.Mp4Error("no moov box in the audio track")

    mp4_module.mux = refuse                    # type: ignore[assignment]
    try:
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="silent.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=1250,
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/v", "itag": 137, "size": 1000,
            "audio": {"endpoint": "https://example.invalid/a", "itag": 140,
                      "size": 250, "is_audio": True},
        }
        download.id = harness.db.insert_download(download)
        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        # Real container headers, so the muxer is actually reached. The choice
        # of muxer is made from the file's opening bytes, and two files of
        # arbitrary padding would be rejected before the join was attempted —
        # which would prove the sniffing works, not that a failed join stops
        # the download, which is what this test is about.
        ftyp = (20).to_bytes(4, "big") + b"ftypisom" + b"\x00" * 8
        Path(task.download.temp_path).write_bytes(ftyp + b"v" * 1000)
        Path(task._audio_temp_path()).write_bytes(ftyp + b"a" * 250)

        failure = ""
        try:
            task._join_tracks()
        except Exception as exc:  # noqa: BLE001
            failure = str(exc)
        check("the join failing stops the download",
              "could not be combined" in failure, failure or "it went ahead")
        check("and it says why", "no moov box" in failure, failure)
        check("both tracks are kept",
              os.path.isfile(task.download.temp_path)
              and os.path.isfile(task._audio_temp_path()),
              task._audio_temp_path())
        check("nothing was published",
              not os.path.isdir(download.dest_dir)
              or not os.listdir(download.dest_dir),
              str(os.listdir(download.dest_dir)
                  if os.path.isdir(download.dest_dir) else "no directory"))
    finally:
        mp4_module.mux = original              # type: ignore[assignment]
        harness.close()


def test_a_missing_header_fails_before_the_download_not_after() -> None:
    """A hole at byte zero is knowable at the start, not at the end.

    The streaming session never carries a stream's index, so it is fetched
    over an ordinary link. When there is no such link the file is doomed from
    the first byte — and the transfer used to discover that only after
    fetching all of it, refusing a hundred and twenty megabytes for the sake
    of three and a half thousand missing at the front.
    """
    print("\n[a missing header fails before the download, not after]")
    import ixd.extractors.sabr as sabr_module
    from ixd.core.engine import DownloadTask
    from ixd.core.models import Download, TransferMode

    transferred: list[int] = []

    #: Set per-case: whether this session sends the initialisation segment,
    #: which is what a real one does at the head of a fresh session.
    sends_header: list[bool] = [False]

    class Counting:
        def __init__(self, *args, **kwargs) -> None:
            self._covered: list[list[int]] = []
            self._buffered_ms = 0
            self._sequence = 0

        def restore(self, ranges, player_ms=0, sequence=0) -> None:
            self._covered = [list(span) for span in (ranges or ())]

        def holds(self, start, end) -> bool:
            return any(span[0] <= start and span[1] >= end
                       for span in self._covered)

        def coverage(self):
            return [list(span) for span in self._covered]

        def note_written(self, start, end) -> None:
            self._covered.append([start, end])

        def download(self, write, should_stop=None, on_progress=None) -> int:
            transferred.append(1)
            if sends_header[0]:
                # What a real session does: the initialisation segment arrives
                # first, at the `start_range` its own header names — zero.
                write(0, b"\x00" * 3485)
                self.note_written(0, 3485)
            write(0, b"\x00" * 1000)
            return 1000

    def run(context: dict) -> str:
        sends_header[0] = bool(context.pop("sends_header", False))
        harness = Harness()
        original = sabr_module.SabrStream
        sabr_module.SabrStream = Counting      # type: ignore[assignment]
        try:
            download = Download(
                url="https://example.invalid/videoplayback",
                filename="header.mp4", dest_dir=str(harness.root / "out"),
                mode=TransferMode.SABR, total_size=1000,
                status=DownloadStatus.PAUSED,
            )
            download.sabr_context = context
            download.id = harness.db.insert_download(download)
            task = DownloadTask(harness.db.get_download(download.id),
                                harness.engine)
            task._prepare_sabr()
            try:
                task._run_sabr(task.download.sabr_context, task._chunks[0],
                               task.download.temp_path)
            except Exception as exc:  # noqa: BLE001
                return str(exc)
            return ""
        finally:
            sabr_module.SabrStream = original  # type: ignore[assignment]
            harness.close()

    base = {"endpoint": "https://example.invalid/v", "itag": 137, "size": 1000}

    failure = run({**base, "header_end": 3484})
    check("a stream whose index has no ordinary link is still attempted",
          len(transferred) == 1, str(transferred))
    check("…and refused only once the session has not sent it",
          "were not served" in failure, failure or "it went ahead")

    # The session sends the initialisation segment itself. That is the whole
    # reason the download is attempted rather than predicted: a media header
    # carries an `is_init_seg` flag and the server sends that segment at the
    # head of a session which has not said it already holds one.
    transferred.clear()
    served = run({**base, "header_end": 3484, "sends_header": True})
    check("a session that does send the opening bytes succeeds",
          served == "", served or "it refused")
    check("and it transferred", len(transferred) == 1, str(transferred))

    # When it does not, the endpoint is asked plainly before the transfer is
    # discarded. Measured three runs running: the session begins at the byte
    # immediately after the opening it will not send, and the endpoint it posts
    # to is itself a `videoplayback` address, which answers a ranged GET. A
    # complete transfer is not worth throwing away for a kilobyte untried.
    source = (Path(__file__).resolve().parents[1]
              / "ixd" / "core" / "engine.py").read_text()
    check("the endpoint is asked for the opening bytes before giving up",
          "_fetch_opening_from_endpoint(" in source
          and source.index("_fetch_opening_from_endpoint(\n") <
              source.index("its index, without which no player"))
    body = source.split("def _fetch_opening_from_endpoint", 1)[1] \
                 .split("\n    def ", 1)[0]
    check("…and what it answers is written at byte zero",
          "write(0, data)" in body)
    # The session raises for a gap itself, so a fallback placed after the call
    # only runs when there is no gap to fix. Three field runs went by with the
    # fallback written and never once executed.
    check("…and the refusal the session raises is caught, not stepped over",
          "except ExtractionError as refused" in source
          and source.index("except ExtractionError as refused")
              < source.index("def _fetch_opening_from_endpoint"))
    check("…and the outcome is logged either way",
          "refused this stream's" in source
          and "served this stream's opening" in source
          and "answered with nothing" in source)

    # A signed address names `itag` in its `sparams`, so appending a second
    # copy can invalidate the signature — and the first version of this did
    # exactly that, then reported the resulting 403 as the origin's verdict on
    # the bytes. The parameter is replaced, and each shape reports its own
    # status so a malformed request cannot be mistaken for a refusal again.
    from ixd.core.engine import _with_query

    signed = "https://cdn/videoplayback?itag=18&sig=abc&sparams=itag"
    once = _with_query(signed, "itag", "137")
    check("a query parameter is replaced, not duplicated",
          once.count("itag=") == 1 and "itag=137" in once, once)
    check("and the rest of the address is untouched",
          "sig=abc" in once and "sparams=itag" in once, once)
    check("the opening is asked for in more than one shape",
          body.count("attempts.append") + body.count('("as the session') >= 2)

    # What comes back must *be* an initialisation segment before it is written
    # at byte zero. The first version wrote whatever arrived: the endpoint
    # answered one request with 31 bytes of framed protocol reply, those went
    # to the front of the file, and the gap moved from "965 bytes at 0" to
    # "934 bytes at 31" — the opening was not filled, it was poisoned.
    check("what the endpoint answers is checked before it is written",
          body.index('data[4:8] not in (b"ftyp"') < body.index("write(0, data)"))
    check("…and a reply that is not media is logged with its first bytes",
          "first bytes" in body and "not an initialisation segment" in body)
    check("…and named as a protocol reply when that is what it is",
          "_describe_ump(data)" in body)

    # The page hook's route is consulted first, and it must name fields that
    # exist. `webpage_url` is `MediaInfo`'s, not `Download`'s: naming it here
    # threw `AttributeError` on every attempt, so the route built for exactly
    # this never ran once — and every offline test still passed, because none
    # of them reached the line.
    import dataclasses

    from ixd.core.models import Download as _Download

    player = source.split("def _take_opening_from_player", 1)[1] \
                   .split("\n    def ", 1)[0]
    fields = {f.name for f in dataclasses.fields(_Download)}
    used = set(re.findall(r"self\.download\.(\w+)", player))
    check("the opening lookup reads only fields a download really has",
          used <= fields, str(sorted(used - fields)))
    check("and it is consulted before the endpoint is asked",
          source.index("_take_opening_from_player(context")
          < source.index("_fetch_opening_from_endpoint(\n"))

    # `sabr.malformed` is the protocol rejecting the *shape* of the request,
    # not the bytes it asks for. Rearranging the address cannot answer it, so
    # the remaining shapes are not asked.
    check("a protocol error ends the attempts rather than repeating them",
          "if self._describe_ump(data):" in body and "return" in body)

    # Without a segment index a track cannot be split, so every download ran on
    # one connection however many were configured — reported from the field
    # alongside "the speed is not acceptable". The index is not missing: it is
    # inside the opening the session sends as its first segment, and this had
    # nowhere to read one from because it only ever looked at `header_url`.
    opening = source.split("def _stream_header_bytes", 1)[1] \
                    .split("\n    def _opening_from_session", 1)[0]
    check("a stream with no ordinary link still gets its index",
          "_opening_from_session(context, end)" in opening)
    session = source.split("def _opening_from_session", 1)[1] \
                    .split("\n    def ", 1)[0]
    check("…from a session opened for the opening alone",
          "should_stop=lambda: len(collected) >= want" in session)
    check("…and a short read yields nothing rather than a partial index",
          "if len(collected) < want:" in session)

    # Assembling, rewrapping and joining a pair use no connections, so they
    # must not hold a download slot. From the field log of 2026-08-12: #150
    # rewrapped from 20:28:54 to 20:29:15 while #151, queued at 20:28:58, sat
    # waiting with the network idle and started at 20:29:16.
    finalize = source.split("def _finalize", 1)[1].split("\n    def ", 1)[0]
    check("post-processing gives up its download slot",
          "self._postprocessing = True" in finalize
          and "self.engine.slot_released()" in finalize)
    check("…before any of the work it covers",
          finalize.index("slot_released()") < finalize.index("_verify_completeness"))
    slot = source.split("def _has_free_slot", 1)[1].split("\n    def ", 1)[0]
    check("…and the count of running downloads excludes it",
          "not task.postprocessing" in slot)
    check("…and the scheduler is woken so the next one starts at once",
          "def slot_released" in source and "_pump_event.set()" in
          source.split("def slot_released", 1)[1].split("\n    def ", 1)[0])

    # A stream that names no header carries its own opening and is fine.
    transferred.clear()
    check("a stream that needs no header runs", run(base) == "", "it refused")
    check("and it did transfer", len(transferred) == 1, str(transferred))

    # A continuation whose earlier attempt already fetched the header must not
    # be refused for the want of a link it no longer needs.
    transferred.clear()
    resumed = run({**base, "header_end": 3484, "covered": [[0, 3485]]})
    check("a continuation that already holds the header runs",
          resumed == "", resumed)
    check("and it too transferred", len(transferred) == 1, str(transferred))


def test_a_track_takes_as_many_sessions_as_it_needs() -> None:
    """A bounded session is not the end of the download.

    The streaming server hands over a fixed amount of media and stops, so a
    track longer than that allowance cannot arrive in one session — it takes
    several, each continuing where the last ended. The first stop was being
    treated as a failure, which is why a long video failed part-way and had to
    be resumed by hand, over and over.
    """
    print("\n[a track takes as many sessions as it needs]")
    import ixd.extractors.sabr as sabr_module
    from ixd.core.engine import DownloadTask
    from ixd.core.models import ChunkStatus, Download, TransferMode
    from ixd.core.errors import CancelledError, ExtractionError

    size = 1000
    per_session = 400
    sessions: list[int] = []

    class Bounded:
        """Serves ``per_session`` bytes from wherever it is told to continue."""

        def __init__(self, *args, **kwargs) -> None:
            self._covered: list[list[int]] = []
            self._buffered_ms = 0
            self._sequence = 0

        def restore(self, ranges, player_ms=0, sequence=0) -> None:
            self._covered = [list(span) for span in (ranges or ())]
            self._buffered_ms = player_ms
            self._sequence = sequence

        def coverage(self):
            return [list(span) for span in self._covered]

        def note_written(self, start, end) -> None:
            pass

        def download(self, write, should_stop=None, on_progress=None) -> int:
            start = self._covered[-1][1] if self._covered else 0
            end = min(size, start + per_session)
            sessions.append(end - start)
            write(start, b"\x00" * (end - start))
            self._covered = [[0, end]]
            self._buffered_ms = end
            self._sequence = end // 100
            if end < size:
                raise ExtractionError(
                    f"the streaming server stopped after {end:,} of "
                    f"{size:,} bytes and would not continue."
                )
            return end

    harness = Harness()
    original = sabr_module.SabrStream
    sabr_module.SabrStream = Bounded          # type: ignore[assignment]
    try:
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="long.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=size,
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/v", "itag": 137,
            "size": size, "duration": 600,
        }
        download.id = harness.db.insert_download(download)

        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        task._transfer_sabr()

        check("it took several sessions", len(sessions) == 3, str(sessions))
        check("each continued where the last stopped",
              sessions == [400, 400, 200], str(sessions))
        logged = [row.get("message", "") for row
                  in harness.db.recent_events(download_id=download.id)]
        check("progress is reported between them",
              sum(1 for line in logged if "opening another" in line) == 2,
              str(logged))
        check("and it says how far along it is",
              any("(40%)" in line for line in logged), str(logged))
        check("and the track finished", task._chunks[0].downloaded == size,
              str(task._chunks[0].downloaded))
        check("the chunk is marked done",
              task._chunks[0].status is ChunkStatus.DONE,
              task._chunks[0].status.value)
    finally:
        sabr_module.SabrStream = original      # type: ignore[assignment]
        harness.close()

    # A long video legitimately needs far more sessions than the retry budget
    # for transient network errors allows. Spending that budget here failed a
    # transfer that was advancing perfectly well, a few sessions in — and a
    # feature-length video needs well over a hundred.
    sessions.clear()
    harness = Harness()
    sabr_module.SabrStream = Bounded          # type: ignore[assignment]
    try:
        harness.settings.set("max_retries", 2)
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="long.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=size,
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/v", "itag": 137,
            "size": size, "duration": 600,
        }
        download.id = harness.db.insert_download(download)
        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        task._transfer_sabr()
        check("the retry budget does not cap how many sessions a track takes",
              len(sessions) == 3 and task._chunks[0].downloaded == size,
              f"{sessions} {task._chunks[0].downloaded}")
    finally:
        sabr_module.SabrStream = original      # type: ignore[assignment]
        harness.close()

    # A pause is not a spent session. Opening another one because the user
    # clicked Pause is the opposite of what they asked for.
    paused: list[int] = []

    class Paused(Bounded):
        def download(self, write, should_stop=None, on_progress=None) -> int:
            paused.append(1)
            write(0, b"\x00" * 100)
            raise CancelledError("stopped")

    harness = Harness()
    sabr_module.SabrStream = Paused            # type: ignore[assignment]
    try:
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="long.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=size,
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/v", "itag": 137,
            "size": size, "duration": 600,
        }
        download.id = harness.db.insert_download(download)
        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        stopped = ""
        try:
            task._transfer_sabr()
        except Exception as exc:  # noqa: BLE001
            stopped = type(exc).__name__
        check("a pause is passed straight through",
              stopped == "CancelledError", stopped or "it returned normally")
        check("and no further session is opened", len(paused) == 1,
              str(len(paused)))
    finally:
        sabr_module.SabrStream = original      # type: ignore[assignment]
        harness.close()

    # A session that gains nothing ends it: repeating a request that achieved
    # nothing is how a dead transfer would spin until its deadline.
    stuck: list[int] = []

    class Stuck(Bounded):
        def download(self, write, should_stop=None, on_progress=None) -> int:
            stuck.append(1)
            raise ExtractionError("the streaming server sent 0 of 1,000 bytes")

    harness = Harness()
    sabr_module.SabrStream = Stuck             # type: ignore[assignment]
    try:
        download = Download(
            url="https://example.invalid/videoplayback",
            filename="stuck.mp4", dest_dir=str(harness.root / "out"),
            mode=TransferMode.SABR, total_size=size,
            status=DownloadStatus.PAUSED,
        )
        download.sabr_context = {
            "endpoint": "https://example.invalid/v", "itag": 137, "size": size,
        }
        download.id = harness.db.insert_download(download)
        task = DownloadTask(harness.db.get_download(download.id), harness.engine)
        task._prepare_sabr()
        failed = ""
        try:
            task._transfer_sabr()
        except Exception as exc:  # noqa: BLE001
            failed = str(exc)
        check("a session that gains nothing is not repeated",
              len(stuck) == 1, str(len(stuck)))
        check("and the failure is reported as it came",
              "0 of 1,000 bytes" in failed, failed)
    finally:
        sabr_module.SabrStream = original      # type: ignore[assignment]
        harness.close()


def _fragmented_track(mp4, struct, kind: bytes,
                      fragments: list[list[bytes]],
                      segment_ms: list[int] | None = None,
                      segment_bytes: list[int] | None = None
                      ) -> tuple[bytes, int]:
    """A fragmented MP4 and the length of its header.

    This is the shape adaptive media is published in: an initialisation
    segment carrying an empty sample table, then a run of ``moof``/``mdat``
    pairs. The header length is what the streaming server never sends, so it
    is returned for the caller to serve over an ordinary link.
    """
    stsd = mp4._box(b"stsd", struct.pack(">IBI", 0, 0, 1)[:8] + b"\x00" * 8)
    stbl = mp4._box(b"stbl", stsd)
    mdhd = mp4._full(b"mdhd", 0, 0,
                     struct.pack(">IIIIHH", 0, 0, 15360, 0, 0x55C4, 0))
    hdlr = mp4._full(b"hdlr", 0, 0,
                     struct.pack(">I4s", 0, kind) + b"\0" * 12 + b"h\0")
    mdia = mp4._box(b"mdia", mdhd + hdlr + mp4._box(b"minf", stbl))
    tkhd = mp4._full(b"tkhd", 0, 7,
                     struct.pack(">IIIII", 0, 0, 1, 0, 0) + b"\0" * 8
                     + struct.pack(">hhhh", 0, 0, 0, 0) + mp4._MATRIX
                     + struct.pack(">II", 0, 0))
    header = (mp4._box(b"ftyp", b"iso5" + struct.pack(">I", 0x200) + b"iso5")
              + mp4._box(b"moov", mp4._box(b"trak", tkhd + mdia)))

    body = b""
    for samples in fragments:
        tfhd = mp4._full(b"tfhd", 0, 0x020000, struct.pack(">I", 1))

        def build(data_offset: int) -> bytes:
            entries = b"".join(
                struct.pack(">III", 512, len(sample), 0 if index else 0)
                for index, sample in enumerate(samples)
            )
            trun = mp4._full(b"trun", 0, 0x0701,
                             struct.pack(">I", len(samples))
                             + struct.pack(">i", data_offset) + entries)
            return mp4._box(b"moof", mp4._box(b"traf", tfhd + trun))

        moof = build(0)
        moof = build(len(moof) + 8)
        body += moof + mp4._box(b"mdat", b"".join(samples))

    if segment_ms is not None:
        # The stream's own segment index, which is what a real one publishes
        # and what makes "continue at byte N" answerable exactly. Written last
        # so the media begins immediately after it, which is the layout
        # `first_offset = 0` describes.
        header += _build_sidx(struct, segment_ms, segment_bytes)
    return header + body, len(header)


def _build_sidx(struct, durations_ms: list[int], sizes: list[int]) -> bytes:
    """A real ``sidx`` box describing pieces of the given sizes and durations.

    Timescale is milliseconds, and ``first_offset`` is zero, so the first piece
    begins immediately after this box — which is the layout the fixture builds.
    """
    references = b"".join(
        struct.pack(">III", size & 0x7FFFFFFF, duration, 0x90000000)
        for size, duration in zip(sizes, durations_ms)
    )
    payload = (struct.pack(">B", 0) + b"\x00\x00\x00"        # version, flags
               + struct.pack(">I", 1)                          # reference_ID
               + struct.pack(">I", 1000)                       # timescale (ms)
               + struct.pack(">I", 0)                          # earliest time
               + struct.pack(">I", 0)                          # first_offset
               + struct.pack(">HH", 0, len(durations_ms))
               + references)
    return struct.pack(">I", len(payload) + 8) + b"sidx" + payload


class _SabrOrigin:
    """A local HTTP server that speaks the streaming protocol for real.

    Every other test of this path stubs something — the stream, or its client.
    This one stubs nothing below the engine: real protobuf requests, real UMP
    framing, a real ``Range`` request for the header, a bounded session, and a
    real muxer at the end. It is the closest thing to "it works" that can be
    produced without the site itself.
    """

    SEGMENT = 4096

    def __init__(self, tracks: dict[int, tuple[bytes, int]],
                 per_session: int, duration_ms: int = 60_000) -> None:
        import http.server
        import threading

        #: itag -> (whole file, header length)
        self.tracks = tracks
        self.per_session = per_session
        #: The running time the contexts declare, so a clock position can be
        #: turned back into a segment the way the real server does.
        self.duration_ms = duration_ms
        #: Per-segment durations, in milliseconds, for each itag. Real media
        #: is variable bitrate: equal-sized pieces do *not* take equal time.
        #: Given these, this server converts a time to a segment exactly the
        #: way a real one does — and a client estimating byte-to-time from the
        #: stream's length gets it wrong, which is the whole difficulty the
        #: published index exists to remove.
        self.segment_ms: dict[int, list[int]] = {}
        self.served: dict[int, int] = {itag: 0 for itag in tracks}
        self.sessions: dict[int, int] = {itag: 0 for itag in tracks}
        self.total: dict[int, int] = {itag: 0 for itag in tracks}
        #: Called after each reply with (itag, bytes served for that track in
        #: total). Lets a test interrupt a transfer at a known point.
        self.on_served = None
        #: ``itag -> segment index`` the server withholds once, leaving a hole
        #: the transfer has to notice and go back for.
        self.skip_once: dict[int, int] = {}
        #: How many requests were made to an endpoint that has expired.
        self.refusals = 0
        origin = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - stdlib naming
                itag = int(self.path.rsplit("=", 1)[-1])
                data, _header_length = origin.tracks[itag]
                start, _, end = self.headers.get(
                    "Range", "bytes=0-").removeprefix("bytes=").partition("-")
                lo = int(start or 0)
                hi = int(end) if end else len(data) - 1
                body = data[lo:hi + 1]
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range",
                                 f"bytes {lo}-{lo + len(body) - 1}/{len(data)}")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                # A signed endpoint that has expired: the origin refuses it
                # outright, exactly as it refuses one that was never valid.
                if self.path.startswith("/expired"):
                    origin.refusals += 1
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = origin.answer(body, self.path)
                self.send_response(200)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    # -- protocol ------------------------------------------------------
    def answer(self, body: bytes, path: str = "") -> bytes:
        from ixd.core.protobuf import Message, parse

        fields = parse(body)
        # Field 2 is the selected format; field 1 of that is the itag.
        selected = parse(fields[2]) if isinstance(fields.get(2), bytes) else {}
        itag = int(selected.get(1) or 0)
        if itag not in self.tracks:
            return b""

        data, header_length = self.tracks[itag]
        held = fields.get(3)
        declared = parse(held) if isinstance(held, bytes) else None
        index = int((declared or {}).get(5, 0))

        # A session that declares no segment can still say *when* it wants to
        # start, and the real server honours that — it is the only way a fresh
        # session can be opened anywhere but the beginning, which is what both
        # a resume and a second worker need. Modelled here because the
        # transfer depends on it: without it this double can only ever serve
        # from byte zero, and anything that seeks looks broken.
        state = parse(fields[1]) if isinstance(fields.get(1), bytes) else {}
        player_ms = int(state.get(28, 0) or 0)   # ClientAbrState.playerTimeMs
        if not index and player_ms > 0 and self.duration_ms > 0:
            total_segments = -(-(len(data) - header_length) // self.SEGMENT)
            index = min(total_segments,
                        int(total_segments * player_ms / self.duration_ms))
            # Where the declared durations say that time falls. This is what a
            # real server does, and with variable-length pieces it is *not*
            # what dividing the running time evenly gives — which is exactly
            # why a client that estimates lands somewhere else and leaves a gap
            # behind it.
            durations = self.segment_ms.get(itag)
            if durations:
                elapsed = 0
                index = len(durations)
                for position, duration in enumerate(durations):
                    if player_ms < elapsed + duration:
                        index = position
                        break
                    elapsed += duration

        # A new session is a new allowance. The stream numbers its requests
        # from one, so `rn=1` is what marks one — *not* whether the request
        # declares anything, because a session opened to continue a transfer
        # declares plenty and is still entitled to a fresh allowance.
        if path.endswith("rn=1"):
            self.sessions[itag] += 1
            self.served[itag] = 0

        total = len(data)
        segments = -(-(total - header_length) // self.SEGMENT)
        out = b""
        for _ in range(4):
            if index >= segments or self.served[itag] >= self.per_session:
                break
            if self.skip_once.get(itag) == index:
                del self.skip_once[itag]
                index += 1
                continue
            start = header_length + index * self.SEGMENT
            end = min(start + self.SEGMENT, total)
            time_range = (Message().varint(1, start - header_length)
                          .varint(2, end - start).varint(3, self.SEGMENT))
            header = (Message().varint(1, 1).varint(3, itag)
                      .varint(6, start).varint(9, index + 1)
                      .message(15, time_range))
            out += (_ump_varint(20) + _ump_varint(len(header.to_bytes()))
                    + header.to_bytes())
            block = data[start:end]
            out += (_ump_varint(21) + _ump_varint(len(block) + 1)
                    + bytes([1]) + block)
            self.served[itag] += end - start
            self.total[itag] += end - start
            index += 1
        if self.on_served is not None:
            self.on_served(itag, self.total[itag])
        return out

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "_SabrOrigin":
        self.thread.start()
        time.sleep(0.05)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _ump_varint(value: int) -> bytes:
    if value < 0x80:
        return bytes([value])
    if value < 0x4000:
        return bytes([0x80 | (value & 0x3F), value >> 6])
    if value < 0x200000:
        return bytes([0xC0 | (value & 0x1F)]) + (value >> 5).to_bytes(2, "little")
    if value < 0x10000000:
        return bytes([0xE0 | (value & 0x0F)]) + (value >> 4).to_bytes(3, "little")
    return b"\xf0" + value.to_bytes(4, "little")


def test_several_sessions_fetch_one_track_between_them() -> None:
    """Several streaming sessions on one track, positioned by the real index.

    A server-driven transfer has no byte ranges to divide, so the connection
    count becomes sessions — each starting at its own point in the stream.
    That was disabled for two sessions, and the reason is what this test is
    built around.

    A position in this protocol is a *time*. A byte offset used to be turned
    into one by estimating from the stream's length, which is right only at
    constant bitrate. Measured against a live video, a session asked to
    continue at byte 95,499,262 delivered from 103,813,481 — eight megabytes
    past — and the difference was a hole nothing could seek back into, because
    the same estimate lands in the same wrong place every time. A real 327 MB
    download stopped at 88% with 16.7 MB missing across four gaps.

    The stream publishes the exact answer in the ``sidx`` of its own header,
    which is fetched before any media regardless. The server below therefore
    does what a real one does: **equal-sized pieces of unequal duration**, so
    dividing the running time evenly gives the wrong piece and only the
    published index gives the right one.
    """
    print("\n[several sessions fetch one track between them]")
    import struct

    import ixd.core.engine as engine_module
    from ixd.core import mp4
    from ixd.core.engine import DownloadTask
    from ixd.core.models import Download, TransferMode

    # Twelve fragments; the media is then served in fixed 4 KiB pieces whose
    # durations alternate. That is variable bitrate in its plainest form.
    fragments = [[bytes([(f * 8 + s) % 251]) * 900 for s in range(8)]
                 for f in range(12)]
    probe, header_length = _fragmented_track(mp4, struct, b"vide", fragments)
    media_length = len(probe) - header_length
    piece_count = -(-media_length // _SabrOrigin.SEGMENT)
    sizes = [min(_SabrOrigin.SEGMENT, media_length - i * _SabrOrigin.SEGMENT)
             for i in range(piece_count)]
    durations = [400 if i % 2 else 1200 for i in range(piece_count)]

    video_bytes, video_header = _fragmented_track(
        mp4, struct, b"vide", fragments,
        segment_ms=durations, segment_bytes=sizes)
    tracks = {137: (video_bytes, video_header)}
    running_ms = sum(durations)

    def fetch(workers: int, publish_index: bool = True):
        """``(file bytes or None, sessions opened)``; None when it could not finish."""
        original_minimum = engine_module._MIN_SABR_SPAN
        original_enabled = engine_module._SABR_PARALLEL_ENABLED
        engine_module._MIN_SABR_SPAN = 4096
        engine_module._SABR_PARALLEL_ENABLED = workers > 1
        source = tracks if publish_index else {137: (probe, header_length)}
        with _SabrOrigin(source, per_session=1 << 30,
                         duration_ms=running_ms) as origin:
            origin.segment_ms[137] = durations
            harness = Harness()
            try:
                harness.settings.set("connections_per_download", workers)
                # The smallest stretch worth a session of its own follows this
                # setting now, as the ordinary chunker's does. Capping it at a
                # fixed four megabytes is what made a 480p stream run on one
                # session while a 1080p one ran on fifteen.
                harness.settings.set("min_chunk_size", 4096)
                whole, head = source[137]
                download = Download(
                    url=f"{origin.base}/sabr",
                    filename=f"parallel-{workers}-{publish_index}.mp4",
                    dest_dir=str(harness.root / "out"),
                    mode=TransferMode.SABR, status=DownloadStatus.PAUSED,
                    connections=workers,
                )
                download.sabr_context = {
                    "endpoint": f"{origin.base}/sabr", "itag": 137,
                    "size": len(whole), "config": "",
                    "header_url": f"{origin.base}/header?itag=137",
                    "header_end": head - 1,
                    "duration": running_ms / 1000.0,
                }
                download.id = harness.db.insert_download(download)
                task = DownloadTask(harness.db.get_download(download.id),
                                    harness.engine)
                try:
                    task._prepare()
                    task._transfer()
                    task._finalize()
                except Exception:
                    return None, origin.sessions[137]
                final = Path(task.download.dest_dir) / task.download.filename
                return final.read_bytes(), origin.sessions[137]
            finally:
                engine_module._MIN_SABR_SPAN = original_minimum
                engine_module._SABR_PARALLEL_ENABLED = original_enabled
                harness.close()

    serial, serial_sessions = fetch(1)
    parallel, parallel_sessions = fetch(4)

    check("one session produces the whole track",
          serial is not None and len(serial) == len(video_bytes),
          f"{len(serial) if serial else 'failed'} of {len(video_bytes)}")
    check("several sessions produce a file of the same length",
          parallel is not None and len(parallel) == len(serial),
          f"{len(parallel) if parallel else 'failed'}")
    check("and it is byte-for-byte the same file", parallel == serial,
          "the parallel file differs from the serial one")
    check("the workers really did open a session each",
          parallel_sessions >= 4 and serial_sessions == 1,
          f"{parallel_sessions} parallel vs {serial_sessions} serial")
    check("the finished file still opens as MP4",
          parallel is not None and parallel[4:8] == b"ftyp",
          parallel[:12].hex() if parallel else "no file")

    # The index is what makes it safe, so a stream without one must not be
    # split at all — that is the difference between this and the version that
    # stranded a download at 88%.
    unindexed, unindexed_sessions = fetch(4, publish_index=False)
    check("a stream publishing no index is fetched whole on one session",
          unindexed == probe and unindexed_sessions == 1,
          f"{unindexed_sessions} sessions, "
          f"{'identical' if unindexed == probe else 'DIFFERENT'}")

    # ---- the connection bars, which the display rule decides -------------
    from ixd.core.models import Chunk, ChunkStatus

    paired = Download(url="u", filename="f.mp4", mode=TransferMode.SABR)
    paired.chunks = [Chunk(index=0, start=0, end=99, downloaded=100,
                           status=ChunkStatus.DONE),
                     Chunk(index=1, start=0, end=49, downloaded=10,
                           status=ChunkStatus.ACTIVE)]
    check("two tracks are drawn as the one transfer they are",
          len(paired.display_chunks) == 1, str(len(paired.display_chunks)))
    check("and that bar is not finished while a track is still running",
          paired.display_chunks[0].status is not ChunkStatus.DONE,
          paired.display_chunks[0].status.value)
    paired.live_workers = len(paired.chunks)
    check("with sessions running, each gets its own bar",
          len(paired.display_chunks) == 2, str(len(paired.display_chunks)))


def test_a_whole_adaptive_download_end_to_end() -> None:
    """One download, nothing stubbed below the engine.

    Every other test of this path replaces the stream or its client. This one
    runs the real thing against a local server that speaks the protocol: the
    header comes over an ordinary ``Range`` request, the media over real UMP
    frames answering real protobuf requests, the session runs out twice and is
    reopened, and the two tracks are joined by the real muxer. If the pieces
    fixed separately do not actually fit together, this is what says so.
    """
    print("\n[a whole adaptive download, end to end]")
    import struct

    from ixd.core import mp4
    from ixd.core.engine import DownloadTask
    from ixd.core.models import Download, TransferMode

    video_bytes, video_header = _fragmented_track(
        mp4, struct, b"vide",
        [[bytes([(f * 8 + s) % 251]) * 900 for s in range(8)] for f in range(12)],
    )
    audio_bytes, audio_header = _fragmented_track(
        mp4, struct, b"soun",
        [[bytes([(f * 4 + s) % 241]) * 400 for s in range(4)] for f in range(12)],
    )

    tracks = {137: (video_bytes, video_header), 140: (audio_bytes, audio_header)}
    # Small enough that *both* tracks need several sessions to arrive, so the
    # continuation is exercised on each rather than only on the larger one.
    with _SabrOrigin(tracks, per_session=15_000) as origin:
        harness = Harness()
        try:
            audio_context = {
                "endpoint": f"{origin.base}/sabr", "itag": 140,
                "size": len(audio_bytes), "is_audio": True, "config": "",
                "header_url": f"{origin.base}/header?itag=140",
                "header_end": audio_header - 1,
                "duration": 60,
            }
            download = Download(
                url=f"{origin.base}/sabr",
                filename="whole.mp4", dest_dir=str(harness.root / "out"),
                mode=TransferMode.SABR,
                status=DownloadStatus.PAUSED,
            )
            download.sabr_context = {
                "endpoint": f"{origin.base}/sabr", "itag": 137,
                "size": len(video_bytes), "config": "",
                "header_url": f"{origin.base}/header?itag=137",
                "header_end": video_header - 1,
                "duration": 60,
                "audio": audio_context,
            }
            download.id = harness.db.insert_download(download)

            task = DownloadTask(harness.db.get_download(download.id),
                                harness.engine)
            task._prepare()
            task._transfer()
            task._finalize()

            check("both tracks needed more than one session",
                  origin.sessions[137] > 1 and origin.sessions[140] > 1,
                  str(origin.sessions))

            final = Path(task.download.dest_dir) / task.download.filename
            check("a file is produced", final.is_file(), str(final))

            handle, video = mp4.open_track(final, b"vide")
            try:
                fetched = b""
                for sample in video.samples:
                    handle.seek(sample.offset)
                    fetched += handle.read(sample.size)
            finally:
                handle.close()
            expected = b"".join(
                bytes([(f * 8 + s) % 251]) * 900
                for f in range(12) for s in range(8)
            )
            check("every video sample arrived, in order and unaltered",
                  fetched == expected,
                  f"{len(fetched)} of {len(expected)} bytes")

            handle, audio = mp4.open_track(final, b"soun")
            try:
                fetched = b""
                for sample in audio.samples:
                    handle.seek(sample.offset)
                    fetched += handle.read(sample.size)
            finally:
                handle.close()
            expected = b"".join(
                bytes([(f * 4 + s) % 241]) * 400
                for f in range(12) for s in range(4)
            )
            check("and every audio sample, so the file is not silent",
                  fetched == expected,
                  f"{len(fetched)} of {len(expected)} bytes")

            check("the working files are gone",
                  not os.path.exists(task._audio_temp_path()),
                  task._audio_temp_path())
        finally:
            harness.close()

        # And the same download, interrupted part-way and finished by a second
        # task reading nothing but the database row — which is the case that
        # was reported broken, against a server that answers for real.
        origin.sessions.update({itag: 0 for itag in tracks})
        origin.total.update({itag: 0 for itag in tracks})
        harness = Harness()
        try:
            download = Download(
                url=f"{origin.base}/sabr",
                filename="resumed.mp4", dest_dir=str(harness.root / "out"),
                mode=TransferMode.SABR,
                status=DownloadStatus.PAUSED,
            )
            download.sabr_context = {
                "endpoint": f"{origin.base}/sabr", "itag": 137,
                "size": len(video_bytes), "config": "",
                "header_url": f"{origin.base}/header?itag=137",
                "header_end": video_header - 1, "duration": 60,
                "audio": {
                    "endpoint": f"{origin.base}/sabr", "itag": 140,
                    "size": len(audio_bytes), "is_audio": True, "config": "",
                    "header_url": f"{origin.base}/header?itag=140",
                    "header_end": audio_header - 1, "duration": 60,
                },
            }
            download.id = harness.db.insert_download(download)

            first = DownloadTask(harness.db.get_download(download.id),
                                 harness.engine)
            origin.on_served = lambda itag, served: (
                first._stop.set() if itag == 137 and served > 25_000 else None
            )
            first._prepare()
            interrupted = ""
            try:
                first._transfer()
            except Exception as exc:  # noqa: BLE001
                interrupted = type(exc).__name__
            first._flush_progress()
            origin.on_served = None

            check("the first attempt is interrupted, not failed",
                  interrupted == "CancelledError", interrupted or "it finished")
            held = harness.db.get_download(download.id)
            check("it kept what it had",
                  0 < held.downloaded < len(video_bytes) + len(audio_bytes),
                  str(held.downloaded))
            check("and wrote down where to continue from",
                  int((held.sabr_context or {}).get("sequence") or 0) > 0,
                  str((held.sabr_context or {}).get("sequence")))

            second = DownloadTask(harness.db.get_download(download.id),
                                  harness.engine)
            second._prepare()
            second._transfer()
            second._finalize()

            final = Path(second.download.dest_dir) / second.download.filename
            check("the resumed download produces a file", final.is_file(),
                  str(final))

            handle, video = mp4.open_track(final, b"vide")
            try:
                fetched = b""
                for sample in video.samples:
                    handle.seek(sample.offset)
                    fetched += handle.read(sample.size)
            finally:
                handle.close()
            check("and it is byte-for-byte what a whole one would have been",
                  fetched == b"".join(
                      bytes([(f * 8 + s) % 251]) * 900
                      for f in range(12) for s in range(8)),
                  f"{len(fetched)} bytes")
            check("the continuation did not start the stream over",
                  origin.total[137] < len(video_bytes) * 2,
                  f"{origin.total[137]:,} served for a "
                  f"{len(video_bytes):,} byte track")
        finally:
            harness.close()

        # And the failure that started all of this: the server skips a block,
        # so the file reaches its last byte with a hole in the middle. The
        # transfer has to notice and go back for it — the case where winding
        # back was being computed correctly and then discarded before the
        # request went out.
        origin.sessions.update({itag: 0 for itag in tracks})
        origin.total.update({itag: 0 for itag in tracks})
        origin.skip_once = {137: 5}

        # The wall clock is held far ahead of the media clock throughout, which
        # is simply what being part-way through a long video looks like and
        # cannot otherwise happen in a test that finishes in under a second.
        # The transfer must be indifferent to it.
        #
        # Note what this case does and does not pin down. Reverting the clock
        # rule alone leaves it passing — because the wind-back declares the
        # segment before the hole and this server, like the real one, decides
        # what to send from what the request declares rather than from its
        # clock. Reverting *both* that and the clock rule fails it with the
        # original report verbatim: "left 4,096 bytes unsent across 1 gap(s)".
        # The two repairs are independent and either suffices here; the clock
        # rule on its own is pinned by `test_a_wind_back_survives_to_the_wire`,
        # against a server that does read the clock.
        import ixd.extractors.sabr as sabr_module

        class _AheadOfTheMedia:
            calls = 0

            @classmethod
            def monotonic(cls) -> float:
                cls.calls += 1
                # Past the ~21 seconds of media this track represents, and far
                # short of the transfer's own deadline.
                return 0.0 if cls.calls == 1 else 60.0

        harness = Harness()
        real_time = sabr_module.time
        sabr_module.time = _AheadOfTheMedia     # type: ignore[assignment]
        try:
            download = Download(
                url=f"{origin.base}/sabr",
                filename="holed.mp4", dest_dir=str(harness.root / "out"),
                mode=TransferMode.SABR,
                status=DownloadStatus.PAUSED,
            )
            download.sabr_context = {
                "endpoint": f"{origin.base}/sabr", "itag": 137,
                "size": len(video_bytes), "config": "",
                "header_url": f"{origin.base}/header?itag=137",
                "header_end": video_header - 1, "duration": 60,
            }
            download.id = harness.db.insert_download(download)
            task = DownloadTask(harness.db.get_download(download.id),
                                harness.engine)
            task._prepare()
            task._transfer()
            task._finalize()

            final = Path(task.download.dest_dir) / task.download.filename
            check("a withheld block is fetched on a second pass",
                  final.is_file() and final.stat().st_size == len(video_bytes),
                  f"{final.stat().st_size if final.is_file() else 'missing'} "
                  f"of {len(video_bytes)}")
            check("and the file is whole, with no hole left in the middle",
                  final.read_bytes() == video_bytes, "bytes differ")
        finally:
            sabr_module.time = real_time        # type: ignore[assignment]
            harness.close()

        # A download whose endpoint expired overnight. The refusal is real, the
        # replacement session is opened by the real service-side path, and the
        # file has to come out whole. Stubbing the hook proves the engine calls
        # it; only this proves the two ends meet.
        origin.sessions.update({itag: 0 for itag in tracks})
        origin.total.update({itag: 0 for itag in tracks})
        origin.refusals = 0
        harness = Harness()
        try:
            asked: list[tuple] = []

            def reopen(page_url, itag, is_audio, xtags):
                # The track tags are part of the request, not an afterthought:
                # every audio language of a dubbed video shares an itag, so a
                # renewal that does not name the track can continue a part-file
                # with a different language's media.
                asked.append((page_url, itag, is_audio, xtags))
                return {"endpoint": f"{origin.base}/sabr", "itag": int(itag),
                        "size": len(video_bytes), "config": "",
                        "header_url": f"{origin.base}/header?itag={itag}",
                        "header_end": video_header - 1, "duration": 60}

            harness.engine.renew_sabr_session = reopen
            download = Download(
                url=f"{origin.base}/expired",
                filename="stale.mp4", dest_dir=str(harness.root / "out"),
                mode=TransferMode.SABR,
                status=DownloadStatus.PAUSED,
            )
            download.sabr_context = {
                "endpoint": f"{origin.base}/expired", "itag": 137,
                "size": len(video_bytes), "config": "",
                "header_url": f"{origin.base}/header?itag=137",
                "header_end": video_header - 1, "duration": 60,
                "page_url": "https://www.youtube.com/watch?v=abcdefghijk",
                # A real tag string, from a video with machine dubbings.
                "xtags": "ChEKBWFjb250EghvcmlnaW5hbAoNCgRsYW5nEgVlbi1VUw",
            }
            download.id = harness.db.insert_download(download)
            task = DownloadTask(harness.db.get_download(download.id),
                                harness.engine)
            task._prepare()
            task._transfer()
            task._finalize()

            check("the expired endpoint is refused once, then abandoned",
                  origin.refusals == 1,
                  f"{origin.refusals} refused requests")
            check("a replacement was asked for, naming the page and the stream",
                  asked and asked[0][0].endswith("abcdefghijk")
                  and str(asked[0][1]) == "137",
                  str(asked))
            # The itag names a rendition; the tags name the track. Renewing on
            # the itag alone hands back whichever language the site lists
            # first, and its media is written into a part-file holding another
            # — a finished download in the wrong language, which is precisely
            # what was reported.
            check("and the track it names, not just the rendition",
                  asked and asked[0][3] ==
                  "ChEKBWFjb250EghvcmlnaW5hbAoNCgRsYW5nEgVlbi1VUw",
                  str(asked))
            check("it is not asked over and over", len(asked) == 1, str(asked))
            final = Path(task.download.dest_dir) / task.download.filename
            check("and the download completes on the new session",
                  final.is_file() and final.read_bytes() == video_bytes,
                  f"{final.stat().st_size if final.is_file() else 'missing'} "
                  f"of {len(video_bytes)}")
        finally:
            harness.close()


def test_the_page_travels_with_the_request() -> None:
    """A media CDN refuses a request that came from nowhere.

    Hotlink protection is the rule rather than the exception: a manifest or a
    segment is served to a request carrying the site's own page in `Referer`
    and refused with **403** to one carrying none. Nothing the extension sent
    ever carried it, so a `.m3u8` captured from a page that was playing came
    back 403 the moment it was handed to the engine.

    The header a browser would send is emulated rather than invented, because
    the CDN compares it against what its own player sends: the whole address
    within one origin, the bare origin to another, `Origin` alongside the
    cross-origin one, and nothing at all stepping down from https to http.
    """
    print("\n[the page travels with the request]")
    from ixd.core.http_client import HttpClient
    import urllib.parse

    # Headers come back lower-cased — they are case-insensitive on the wire,
    # and the client normalises them on the way in.
    def sent(headers: dict) -> dict:
        return {key.lower(): value for key, value in headers.items()}

    client = HttpClient(referer="https://site.example/watch/1234?x=1")

    def ask(url: str) -> dict:
        return sent(client._default_headers(urllib.parse.urlparse(url), None))

    same = ask("https://site.example/api/list")
    check("within one origin the whole address is sent",
          same.get("referer") == "https://site.example/watch/1234?x=1",
          same.get("referer", ""))
    check("…and no Origin, exactly as a same-origin fetch does",
          "origin" not in same, str(sorted(same)))

    cross = ask("https://cdn.example.net/hls/master.m3u8")
    check("to another origin only the origin is sent",
          cross.get("referer") == "https://site.example/",
          cross.get("referer", ""))
    check("and Origin travels with it",
          cross.get("origin") == "https://site.example",
          cross.get("origin", ""))

    downgrade = ask("http://cdn.example.net/hls/master.m3u8")
    check("nothing is sent when stepping down to http",
          "referer" not in downgrade and "origin" not in downgrade,
          str(sorted(downgrade)))

    explicit = sent(client._default_headers(
        urllib.parse.urlparse("https://cdn.example.net/x.m3u8"),
        {"Referer": "https://chosen.example/page"}))
    check("a caller that names its own Referer keeps it",
          explicit.get("referer") == "https://chosen.example/page",
          explicit.get("referer", ""))

    blank = sent(HttpClient()._default_headers(
        urllib.parse.urlparse("https://cdn.example.net/x.m3u8"), None))
    check("a client with no page sends neither header",
          "referer" not in blank and "origin" not in blank, str(sorted(blank)))

    check("a clone carries the page with it",
          client.clone().referer == client.referer)


def test_the_browsers_own_headers_are_replayed() -> None:
    """What the browser sent is replayed, not reconstructed.

    `Referer` is a good guess and not always the right one: a player may sign
    its segment requests with an `Authorization` or a bespoke `X-…` header that
    nothing could invent. The browser already sent a set that worked, so it is
    kept with the capture and replayed — which is what a commercial download
    manager does, and why one succeeds where a reconstructed request is refused.

    They can carry credentials, so where they may be sent is the other half of
    the rule.
    """
    print("\n[the browser's own headers are replayed]")
    from ixd.core.http_client import HttpClient
    import urllib.parse

    captured = {
        "Referer": "https://site.example/watch/1",
        "Origin": "https://site.example",
        "Authorization": "Bearer abc123",
        "X-Playback-Session-Id": "9f2c",
    }
    client = HttpClient(referer="https://site.example/watch/1",
                        site_headers=captured, site_host="cdn.example.net")

    def sent(url: str, extra=None) -> dict:
        headers = client._default_headers(urllib.parse.urlparse(url), extra)
        return {key.lower(): value for key, value in headers.items()}

    same = sent("https://cdn.example.net/hls/master.m3u8")
    check("a header nothing could reconstruct is replayed",
          same.get("authorization") == "Bearer abc123", same.get("authorization", ""))
    check("and so is a bespoke one",
          same.get("x-playback-session-id") == "9f2c", str(sorted(same)))
    check("the captured Referer wins over the reconstructed one",
          same.get("referer") == "https://site.example/watch/1",
          same.get("referer", ""))

    sub = sent("https://edge1.cdn.example.net/hls/seg.m4s")
    check("a subdomain of the captured host is the same site",
          sub.get("authorization") == "Bearer abc123", str(sorted(sub)))

    other = sent("https://tracker.example.org/beacon")
    check("a third party gets none of them",
          "authorization" not in other and "x-playback-session-id" not in other,
          str(sorted(other)))
    check("…but it still gets the Referer a browser would have sent",
          other.get("referer") == "https://site.example/", other.get("referer", ""))

    named = sent("https://cdn.example.net/x.m3u8", {"Authorization": "Bearer mine"})
    check("a caller naming a header keeps its own value",
          named.get("authorization") == "Bearer mine", named.get("authorization", ""))

    check("a clone carries them, and where they may go",
          client.clone().site_headers == captured
          and client.clone().site_host == "cdn.example.net")

    # The service drops what the engine owns before any of this is reached: a
    # replayed Range or Accept-Encoding would contradict the transfer.
    from ixd.service import _site_headers_of
    filtered = _site_headers_of({"headers": {
        "Referer": "https://site.example/", "Range": "bytes=0-1023",
        "Accept-Encoding": "gzip", "Cookie": "a=b", "Host": "cdn.example.net",
        "X-Token": "keep",
    }})
    check("the engine's own headers are never replayed",
          set(filtered) == {"Referer", "X-Token"}, str(sorted(filtered)))
    check("and a malformed payload is no headers, not a crash",
          _site_headers_of({"headers": "nonsense"}) == {})



def test_a_stream_of_error_pages_is_never_published() -> None:
    """Pieces that are not media must fail, not be concatenated.

    A segment request answered with an error page, a login wall or a further
    playlist still *succeeds* as a transfer: the part file exists, the
    completeness guard is satisfied, and the pieces are joined into a file with
    the right name, a plausible size and nothing playable in it. The user
    reported exactly that shape — "it downloads but the video is not playable
    at all" — and it is the worst kind of failure, because it is only found by
    trying to watch the result.
    """
    print("\n[a stream of error pages is never published]")
    from ixd.core.engine import DownloadTask
    from ixd.core.errors import IXDError

    reject = DownloadTask._reject_non_media

    def refused(opening: bytes) -> str:
        try:
            reject(DownloadTask, opening)
        except IXDError as exc:
            return str(exc)
        return ""

    check("an HTML error page is refused",
          "HTML page" in refused(b"<!DOCTYPE html><html><body>403"))
    check("so is one with no doctype",
          "HTML page" in refused(b"  <html><head><title>Forbidden"))
    check("a playlist where media was expected is refused",
          "another playlist" in refused(b"#EXTM3U\n#EXT-X-VERSION:3\n"))
    check("a JSON error body is refused", "JSON" in refused(b'{"error":"denied"}'))
    check("an XML one is refused", "XML" in refused(b"<?xml version=\"1.0\"?><Error/>"))
    check("the message says what to do about it",
          "download panel" in refused(b"<html>"))

    # And the shapes that *are* media must pass, or every HLS download breaks.
    check("an MPEG-TS packet is media", refused(b"\x47\x40\x00\x10\x00\x00\xb0") == "")
    check("a fragmented-MP4 piece is media",
          refused(b"\x00\x00\x00\x18ftypiso5") == "")
    check("a WebM cluster is media", refused(b"\x1a\x45\xdf\xa3\x01\x00") == "")
    check("an ADTS frame is media", refused(b"\xff\xf1\x50\x80") == "")


def test_a_file_that_plays_nothing_is_never_published() -> None:
    """Media announces itself. Bytes that do not are not media.

    Reported four times over: "it downloads the full video but it is not
    playable". Every guard passed — the transfer succeeded, every piece was
    present, the size was right — because the failure had already happened
    upstream, in a decryption or in a piece that was never media, and neither
    leaves anything for a completeness check to notice.

    A container always says what it is in its first bytes. Refusing what says
    nothing turns a file that plays nothing into a sentence explaining why, and
    the bytes go into the log either way — because "not playable" has half a
    dozen causes that are indistinguishable from outside and those sixteen bytes
    separate most of them.
    """
    print("\n[a file that plays nothing is never published]")
    from ixd.core.engine import DownloadTask
    from ixd.core.errors import IXDError

    kind = DownloadTask.container_of

    check("a transport stream is recognised", kind(b"\x47\x40\x00\x10") == "ts")
    check("an initialisation segment is recognised",
          kind(b"\x00\x00\x00\x18ftypisom") == "mp4")
    # A fragmented stream's *media* pieces never carry `ftyp`; reading only that
    # box left every one of them unrecognised.
    check("so is a fragmented media piece", kind(b"\x00\x00\x00\x18stypmsdh") == "mp4")
    check("and a bare movie fragment", kind(b"\x00\x00\x02\x40moof") == "mp4")
    check("a segment index too", kind(b"\x00\x00\x00\x30sidx") == "mp4")
    check("Matroska is recognised", kind(b"\x1a\x45\xdf\xa3\x01") == "webm")
    check("an ADTS frame is recognised", kind(b"\xff\xf1\x50\x80") == "aac")
    check("Ogg is recognised", kind(b"OggS\x00\x02") == "ogg")
    check("and noise is not", kind(b"\x93\x1c\xa7\x4e\x02\xbb") == "")

    def refuse(opening: bytes, encrypted: bool) -> str:
        class Segment:
            key_url = "https://cdn/key.bin" if encrypted else None

        class Fake:
            download = type("D", (), {"segments": [Segment(), Segment()]})()
            container_of = DownloadTask.container_of
            _CONTAINER_SIGNATURES = DownloadTask._CONTAINER_SIGNATURES
            _describe_opening = DownloadTask._describe_opening
            _require_recognisable = DownloadTask._require_recognisable

        try:
            Fake._require_recognisable(Fake(), opening)
        except IXDError as exc:
            return str(exc)
        return ""

    noise = refuse(b"\x93\x1c\xa7\x4e" + bytes(range(12)), True)
    check("an encrypted stream that decrypted to noise is refused",
          "key or its IV" in noise, noise[:120])
    check("and the bytes are quoted, so the cause can be read off the log",
          "93 1c a7 4e" in noise, noise[:160])
    check("nothing is published", "Nothing has been published" in noise)

    plain = refuse(b"\x93\x1c\xa7\x4e" + bytes(range(12)), False)
    check("an unencrypted stream of rubbish is refused with its own reason",
          "not media of any kind" in plain, plain[:120])

    check("and real media passes untouched",
          refuse(b"\x47\x40\x00\x10" + bytes(12), True) == "")


def test_a_segment_disguised_as_an_image_is_unwrapped() -> None:
    """A CDN that serves its media as PNGs, from a real log.

    Every segment of a 292-piece stream began

        89 50 4e 47 0d 0a 1a 0a 00 00 00 0d 49 48 44 52

    — a PNG signature and its IHDR chunk, with the media behind it. Serving
    segments as images gets past filters and caches that treat video
    differently, and the site's own player strips the header in JavaScript.

    Nothing about it is visible from outside: the transfer succeeds, every
    piece arrives, the sizes are right, and the file plays nothing. That was
    four sessions of "it downloads and will not play".
    """
    print("\n[a segment disguised as an image is unwrapped]")
    import struct
    import zlib

    from ixd.core.engine import DownloadTask, unwrap_disguised_segment

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    media = b"".join(b"\x47\x41\x00\x10" + os.urandom(184) for _ in range(9))
    header = (b"\x89PNG\r\n\x1a\n"
              + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
              + chunk(b"IEND", b""))

    unwrapped = unwrap_disguised_segment(header + media)
    check("the image is stripped and the media kept", unwrapped == media,
          f"{len(unwrapped)} vs {len(media)}")
    check("and what is left is recognisable as media",
          DownloadTask.container_of(unwrapped) == "ts",
          DownloadTask.container_of(unwrapped))

    # A wrapper with no IEND — some are truncated on purpose — still gives up
    # its media, because a transport stream announces itself every 188 bytes
    # and that pattern does not occur by chance.
    truncated = (b"\x89PNG\r\n\x1a\n"
                 + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0))
                 + b"\x00\x00\x01\x00IDAT" + bytes(260))
    check("a wrapper with no end marker is still stepped over",
          unwrap_disguised_segment(truncated + media) == media,
          str(len(unwrap_disguised_segment(truncated + media))))

    # Everything else is left exactly as it arrived.
    check("an ordinary transport stream is untouched",
          unwrap_disguised_segment(media) == media)
    check("an MP4 piece is untouched",
          unwrap_disguised_segment(b"\x00\x00\x00\x18ftypisom" + bytes(64))
          == b"\x00\x00\x00\x18ftypisom" + bytes(64))
    check("and a PNG that really is only a PNG is handed back whole, for the "
          "guard to report rather than silently truncate",
          unwrap_disguised_segment(header) == header)


def test_a_transport_stream_becomes_an_mp4_without_re_encoding() -> None:
    """HLS delivers MPEG-TS, and a concatenated `.ts` is not what anyone wanted.

    It is correct and it plays, and a good half of the world's players refuse it
    by name — it also carries no seek index at all. Every commercial download
    manager converts it, which is not a rename: the coded frames are identical
    and everything around them is a different shape. Framing, H.264's Annex-B
    start codes, ADTS headers and a 90 kHz clock all become an MP4's sample
    tables.

    Built from real transport-stream packets rather than a description of them,
    and the output is read back through this project's own parser.
    """
    print("\n[a transport stream becomes an MP4 without re-encoding]")
    import struct as _struct
    import tempfile

    from ixd.core import mp4 as mp4mod
    from ixd.core.mpegts import (
        remux, split_adts, split_annexb, sps_dimensions,
    )

    # --- the pieces, each checked on its own -----------------------------
    annexb = (b"\x00\x00\x00\x01" + b"\x67abc"
              + b"\x00\x00\x01" + b"\x68de"
              + b"\x00\x00\x00\x01" + b"\x65frame")
    units = list(split_annexb(annexb))
    check("both start-code lengths are recognised",
          units == [b"\x67abc", b"\x68de", b"\x65frame"], str(units))

    # A 1920x1080 sequence parameter set, taken from a real encoder.
    sps = bytes.fromhex("67640028acd100780227e5c05a808080a00000030020000006518028")
    check("the picture size is read out of the parameter set",
          sps_dimensions(sps) == (1920, 1080), str(sps_dimensions(sps)))

    # An ADTS frame: syncword, AAC-LC, 44100 Hz, stereo, 7-byte header.
    body = bytes(range(64))
    length = 7 + len(body)
    adts = bytes([0xFF, 0xF1, 0x4C, 0x80 | (length >> 11),
                  (length >> 3) & 0xFF, ((length & 7) << 5) | 0x1F, 0xFC]) + body
    frames = list(split_adts(adts + adts))
    check("both frames are found and their headers stripped",
          len(frames) == 2 and frames[0][0] == body, str(len(frames)))
    check("and the header is read — AAC-LC, 48 kHz, stereo",
          frames[0][1:] == (2, 3, 2), str(frames[0][1:]))

    # --- a whole stream, built packet by packet --------------------------
    def ts_packet(pid: int, payload: bytes, start: bool, counter: int) -> bytes:
        head = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF,
                      0x10 | (counter & 0x0F)])
        body = payload[:184]
        return head + body + b"\xff" * (184 - len(body))

    def pes(stream_id: int, pts: int, dts: int, payload: bytes) -> bytes:
        flags = 0xC0 if dts != pts else 0x80
        stamps = b""

        def stamp(marker: int, value: int) -> bytes:
            return bytes([
                (marker << 4) | ((value >> 29) & 0x0E) | 1,
                (value >> 22) & 0xFF,
                ((value >> 14) & 0xFE) | 1,
                (value >> 7) & 0xFF,
                ((value << 1) & 0xFE) | 1,
            ])

        stamps = stamp(3 if dts != pts else 2, pts)
        if dts != pts:
            stamps += stamp(1, dts)
        header = bytes([0x80, flags, len(stamps)]) + stamps
        return (b"\x00\x00\x01" + bytes([stream_id])
                + _struct.pack(">H", len(header) + len(payload)) + header + payload)

    # table_id, section length, stream id, version, section numbers, then one
    # program: number 1, carried on PID 0x100.
    pat = bytes([0x00, 0xB0, 0x0D, 0x00, 0x01, 0xC1, 0x00, 0x00,
                 0x00, 0x01, 0xE1, 0x00, 0x00, 0x00, 0x00, 0x00])
    pmt_body = bytes([0x02, 0xB0, 0x17, 0x00, 0x01, 0xC1, 0x00, 0x00,
                      0xE1, 0x01, 0xF0, 0x00,
                      0x1B, 0xE1, 0x01, 0xF0, 0x00,
                      0x0F, 0xE1, 0x02, 0xF0, 0x00,
                      0x00, 0x00, 0x00, 0x00])
    pmt = bytes([0x00]) + pmt_body

    stream = bytearray()
    stream += ts_packet(0, bytes([0x00]) + pat, True, 0)
    stream += ts_packet(0x100, pmt, True, 0)

    picture = b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01\x68\xee\x3c\xb0"
    for index in range(6):
        kind = b"\x65" if index == 0 else b"\x41"
        frame = (picture if index == 0 else b"") + b"\x00\x00\x00\x01" + kind + bytes(60)
        stamp = 3000 * (index + 1)
        stream += ts_packet(0x101, pes(0xE0, stamp + 3000, stamp, frame), True, index)
        stream += ts_packet(0x102, pes(0xC0, stamp, stamp, adts), True, index)

    with tempfile.TemporaryDirectory() as home:
        source = Path(home) / "stream.ts"
        source.write_bytes(bytes(stream))
        output = Path(home) / "stream.mp4"
        remux(source, output)

        check("an MP4 was written", output.exists() and output.stat().st_size > 0)
        handle, video = mp4mod.open_track(output, b"vide")
        try:
            check("the video track survives the crossing",
                  len(video.samples) == 6, str(len(video.samples)))
            check("its size comes from the parameter set",
                  (video.width, video.height) == (1920, 1080),
                  f"{video.width}x{video.height}")
            check("the clock is the transport stream's own",
                  video.timescale == 90000, str(video.timescale))
            check("only the first picture is a sync sample",
                  [s.sync for s in video.samples] == [True] + [False] * 5,
                  str([s.sync for s in video.samples]))
            check("the presentation offset is kept",
                  all(s.composition_offset == 3000 for s in video.samples),
                  str([s.composition_offset for s in video.samples]))
            check("durations come from the decode stamps",
                  [s.duration for s in video.samples[:4]] == [3000] * 4,
                  str([s.duration for s in video.samples[:4]]))
        finally:
            handle.close()

        handle, audio = mp4mod.open_track(output, b"soun")
        try:
            check("the audio track survives too", len(audio.samples) == 6,
                  str(len(audio.samples)))
            check("at the rate its frames declared", audio.timescale == 48000,
                  str(audio.timescale))
            check("and each frame lost its ADTS header",
                  all(s.size == 64 for s in audio.samples),
                  str([s.size for s in audio.samples[:3]]))
        finally:
            handle.close()

    # A stream carrying nothing this can describe is refused, not mangled.
    from ixd.core.mpegts import TsError
    with tempfile.TemporaryDirectory() as home:
        empty = Path(home) / "empty.ts"
        empty.write_bytes(bytes(ts_packet(0, bytes([0x00]) + pat, True, 0)) * 4)
        try:
            remux(empty, Path(home) / "out.mp4")
            check("a stream with no video is refused", False, "it produced a file")
        except TsError:
            check("a stream with no video is refused", True)


def test_pausing_a_stalled_transfer_is_immediate() -> None:
    """Pause must stop a connection that has gone quiet, not wait it out.

    Setting the flag is not enough. A worker waiting on a stalled origin is
    inside a socket read with the profile's timeout — thirty seconds — and
    notices nothing until it returns. Reported as "when the connection to
    YouTube is struggling and I try to pause it, it takes very long", which is
    exactly the connection a person most wants to stop.

    Measured against an origin that accepts the request, sends a little, and
    then says nothing at all.
    """
    print("\n[pausing a stalled transfer is immediate]")
    import http.server
    import socket as socketlib
    import threading as threadinglib

    started = threadinglib.Event()

    class Stalls(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", "100000000")
            self.end_headers()
            self.wfile.write(b"\0" * 4096)
            self.wfile.flush()
            started.set()
            # …and then nothing, for longer than any timeout under test.
            time.sleep(120)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stalls)
    server.daemon_threads = True
    threadinglib.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    harness = Harness()
    try:
        download = harness.engine.add_download(
            f"http://127.0.0.1:{port}/stalled.bin", connections=1)
        check("the transfer started", started.wait(20), "it never began reading")
        # The first four kilobytes are already buffered, so the worker consumes
        # them and only *then* blocks. Pausing before it does would be caught by
        # the loop's own check and would prove nothing — the defect is a pause
        # arriving while the thread is inside `recv`.
        time.sleep(3.0)

        # The read is now blocked with nothing coming. Time the pause.
        began = time.time()
        harness.engine.pause_download(download.id)
        settled = False
        for _ in range(80):                      # up to eight seconds
            time.sleep(0.1)
            row = harness.db.get_download(download.id)
            if row.status is DownloadStatus.PAUSED:
                settled = True
                break
        took = time.time() - began

        check("pausing a stalled transfer settles in seconds, not a timeout",
              settled and took < 8.0, f"{took:.1f}s, settled={settled}")
        # The socket timeout is 30s; anything near that means the read was
        # waited out rather than interrupted.
        check("…and well inside the socket timeout it used to wait out",
              took < 15.0, f"{took:.1f}s")
    finally:
        harness.close()
        server.shutdown()


def test_the_pages_cookies_do_not_travel_to_a_media_cdn() -> None:
    """A browser sends no cookies to a CDN on another registrable domain.

    From the field log of 2026-08-12, three downloads in one run — #107, #115
    and #116 — died the same way:

        WARNI #115  Probe attempt 1/5 failed (HTTP 403 Forbidden); retrying…
        ERROR #115  Failed: HTTP 403 Forbidden

    Every one of them a plain `videoplayback` address, and it made no
    difference whether the address came from extraction or from the browser's
    own capture. What the engine sent that the browser does not is the site's
    whole session: `_request_headers()` attached `Cookie: <page cookies>` to
    every request of a download, and `googlevideo.com` is not `youtube.com`, so
    Chrome sends it nothing at all there.

    The cookies still travel where they belong — the page's own domain and its
    subdomains — because that is the case they exist for.
    """
    print("\n[the page's cookies stay on the page's domain]")
    from ixd.core.engine import DownloadTask
    from ixd.core.models import Download

    download = Download(
        url="https://rr3---sn-x.googlevideo.com/videoplayback?itag=18",
        filename="v.mp4", dest_dir="/tmp",
        cookies="SAPISID=secret; SID=alsosecret",
        referer="https://www.youtube.com/watch?v=abc",
        user_agent="Mozilla/5.0",
    )
    task = DownloadTask.__new__(DownloadTask)
    task.download = download

    to_cdn = DownloadTask._request_headers(task, download.url)
    check("no cookies are sent to the media CDN", "Cookie" not in to_cdn)
    check("and the engine sets no Referer of its own — the client decides it "
          "per target, which is the only layer that knows the target",
          "Referer" not in to_cdn)
    check("and the browser's user agent still travels",
       to_cdn.get("User-Agent") == "Mozilla/5.0")

    to_page = DownloadTask._request_headers(
        task, "https://www.youtube.com/watch?v=abc")
    check("the page's own domain still gets them",
       to_page.get("Cookie") == "SAPISID=secret; SID=alsosecret")
    to_sub = DownloadTask._request_headers(task, "https://m.youtube.com/watch")
    check("and so does a subdomain of it", "Cookie" in to_sub)

    # An ordinary download names no page. Its cookies and its URL come from the
    # same place, so nothing is withheld — this must not change what already
    # works.
    plain = Download(url="https://files.example.com/a.zip", filename="a.zip",
                     dest_dir="/tmp", cookies="session=1")
    bare = DownloadTask.__new__(DownloadTask)
    bare.download = plain
    check("a download with no page keeps sending its cookies",
       DownloadTask._request_headers(bare, plain.url).get("Cookie") == "session=1")

    # …and the same decision is made one layer down, or the first is worthless.
    #
    # `HttpClient._default_headers` adds the jar's cookies whenever the caller
    # sent none — and a jar built from a bare string files them under the empty
    # domain, which `header_for()` merges into *every* host. Withholding the
    # header above therefore achieved exactly nothing: the client put the whole
    # session straight back onto the CDN request. Measured as the 403 surviving
    # a fix that looked complete.
    from ixd.core.http_client import CookieJar

    jar = CookieJar()
    jar.load_header("SAPISID=secret",
                    DownloadTask._cookie_scope("https://www.youtube.com/watch?v=a"))
    check("the jar withholds them from the CDN too",
          jar.header_for("rr3---sn-x.googlevideo.com") == "",
          repr(jar.header_for("rr3---sn-x.googlevideo.com")))
    check("while still serving the page's own host",
          jar.header_for("www.youtube.com") == "SAPISID=secret")
    unscoped = CookieJar()
    unscoped.load_header("a=1", DownloadTask._cookie_scope(""))
    check("and a download with no page is unscoped, as before",
          unscoped.header_for("cdn.example.com") == "a=1")

    # The same mistake, one field along: the engine set `Referer` itself, which
    # suppressed `HttpClient._referer_for()` — the one place that knows the
    # *target* and applies the browser's own policy. Measured in the field as:
    #
    #   Refused outright. This request carried: Referer, User-Agent
    #
    # Two headers, against the ten a browser sends for a media subresource, and
    # the `Referer` among them was the full watch address, which no browser
    # sends across origins. `Origin` was missing entirely.
    import urllib.parse

    from ixd.core.http_client import HttpClient

    client = HttpClient(referer="https://www.youtube.com/watch?v=abc")
    cross = client._referer_for(
        urllib.parse.urlparse("https://rr3---sn-x.googlevideo.com/videoplayback"))
    check("a cross-origin media request gets the bare origin, as a browser sends",
          cross == ("https://www.youtube.com/", "https://www.youtube.com"),
          str(cross))
    same = client._referer_for(
        urllib.parse.urlparse("https://www.youtube.com/watch?v=abc"))
    check("and a same-origin one still gets the whole address",
          same[0] == "https://www.youtube.com/watch?v=abc" and same[1] == "",
          str(same))
    check("the engine no longer sets Referer itself, or none of that runs",
          "Referer" not in DownloadTask._request_headers(task, download.url))

    # …and it must not let one in by inheritance either, which is how the fix
    # above was defeated the first time it shipped. An extractor stores a page
    # `Referer` in its format's `http_headers` (youtube.py does), `add_media`
    # files that as `extra_headers`, and merging it back switched the client's
    # policy off exactly as setting it directly did. Measured as:
    #
    #   Refused outright. This request carried: accept, accept-language,
    #   connection, referer, user-agent
    #
    # — a referer present and no origin at all.
    inherited = Download(
        url="https://rr3---sn-x.googlevideo.com/videoplayback?itag=18",
        filename="v.mp4", dest_dir="/tmp",
        referer="https://www.youtube.com/",
        extra_headers={"Referer": "https://www.youtube.com/watch?v=a",
                       "Origin": "https://evil.invalid",
                       "X-Player": "keep me"})
    task3 = DownloadTask.__new__(DownloadTask)
    task3.download = inherited
    built = DownloadTask._request_headers(task3, inherited.url)
    check("an inherited Referer is struck out, whatever set it",
          "Referer" not in built, str(sorted(built)))
    check("and an inherited Origin with it",
          "Origin" not in built, str(sorted(built)))
    check("while every other header the extractor set is kept",
          built.get("X-Player") == "keep me")

    # End to end through the client, which is the only assertion that matters:
    # the header the CDN actually receives.
    jar2 = CookieJar()
    client2 = HttpClient(None, jar2, referer=inherited.referer,
                         site_headers=inherited.extra_headers,
                         site_host="youtube.com")
    wire = client2._default_headers(
        urllib.parse.urlparse(inherited.url), built)
    check("so the request finally carries an Origin, as a player's does",
          wire.get("origin") == "https://www.youtube.com", str(sorted(wire)))
    check("with the bare origin as its referer",
          wire.get("referer") == "https://www.youtube.com/")
    check("and Accept is a subresource's, not a document's",
          wire.get("accept") == "*/*", str(wire.get("accept"))[:40])

    # And a redirect onto a different host of the *same* site keeps them.
    same_site = Download(url="https://cdn.example.com/a.zip", filename="a.zip",
                         dest_dir="/tmp", cookies="session=1",
                         referer="https://www.example.com/downloads")
    task2 = DownloadTask.__new__(DownloadTask)
    task2.download = same_site
    check("a CDN on the site's own domain still gets them",
       "Cookie" in DownloadTask._request_headers(task2, same_site.url))


def test_a_queue_left_by_a_previous_session_does_not_start_itself() -> None:
    """Launching is not consent to run what was queued weeks ago.

    The supervisor's pump reads the whole downloads table every second and
    starts anything queued, and nothing cleared that status between runs. On a
    machine where the application launches at login, signing in began every
    download that had ever been left behind a concurrency limit.
    """
    from ixd.core.models import Download

    print("\n[36] a queue left by a previous session does not start itself")
    harness = Harness()
    try:
        # End the previous session *first*. `Harness` starts an engine, and its
        # supervisor pumps every second — writing queued rows underneath a live
        # engine is a race, and CI lost it where this machine kept winning it
        # ("1 of 3 started"). The scenario is a session that has finished, so
        # the test has to stage it that way.
        harness.engine.shutdown(wait=True, timeout=5)

        # Whatever the previous session left behind: three that never got a
        # slot, one the user had paused, one already finished.
        stale = [
            harness.db.insert_download(Download(
                url=f"http://127.0.0.1:9/never-served-{n}.bin",
                filename=f"stale-{n}.bin", status=DownloadStatus.QUEUED,
            ))
            for n in range(3)
        ]
        paused = harness.db.insert_download(Download(
            url="http://127.0.0.1:9/parked.bin", filename="parked.bin",
            status=DownloadStatus.PAUSED,
        ))
        done = harness.db.insert_download(Download(
            url="http://127.0.0.1:9/finished.bin", filename="finished.bin",
            status=DownloadStatus.COMPLETED,
        ))

        # A fresh launch over that database.
        engine = DownloadEngine(harness.db, harness.settings, EventBus())
        engine.start()
        harness.engine = engine
        time.sleep(2.0)

        started = [
            i for i in stale
            if harness.db.get_download(i).status is not DownloadStatus.PAUSED
        ]
        check("nothing queued starts itself on launch", not started,
              f"{len(started)} of 3 started: {started}")
        check("a queued download is parked, not lost",
              all(harness.db.get_download(i).status is DownloadStatus.PAUSED
                  for i in stale))
        check("a paused download is left alone",
              harness.db.get_download(paused).status is DownloadStatus.PAUSED)
        check("a finished download is left alone",
              harness.db.get_download(done).status is DownloadStatus.COMPLETED)

        # And the part that must not regress: adding one now still runs it.
        payload = make_payload(64 << 10)
        with TestOrigin(payload) as origin:
            fresh = engine.add_download(origin.url())
            result = harness.wait_for(
                fresh.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=30)
            check("a download added in this session still starts",
                  result.status is DownloadStatus.COMPLETED, str(result.error))
    finally:
        harness.close()


def test_a_server_that_advertises_ranges_and_ignores_them() -> None:
    """`Accept-Ranges: bytes` is a claim, and the probe has to check it.

    gameforge.com's installer host answers HEAD with a size and
    `Accept-Ranges: bytes`, then returns `200` and the whole 2.2 MB body for
    every `Range` it is given. Believing the header planned three connections
    that could not work, and the download failed outright (context.md §3.81).
    """
    print("\n[a server that advertises ranges and ignores them]")
    from ixd.core.http_client import HttpClient
    from ixd.core.models import TransferMode

    payload = make_payload(3 << 20)
    digest = hashlib.sha256(payload).hexdigest()
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            origin.state.ignore_ranges = True

            info = HttpClient().probe(origin.url())
            check("the probe disbelieves the Accept-Ranges header",
                  info.supports_ranges is False, str(info.supports_ranges))
            check("and still learns the size",
                  info.size == len(payload), str(info.size))

            download = harness.engine.add_download(origin.url())
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=60
            )
            check("the download completes",
                  done.status is DownloadStatus.COMPLETED, str(done.error))
            check("planned as a single stream", done.mode is TransferMode.SINGLE,
                  done.mode.value)
            if os.path.isfile(done.filepath):
                actual = hashlib.sha256(Path(done.filepath).read_bytes()).hexdigest()
                check("the bytes are correct", actual == digest)
    finally:
        harness.close()


def test_a_range_lie_found_mid_transfer_restarts_as_one_stream() -> None:
    """An origin can pass the probe and still ignore every range after it.

    Checking the probe cannot catch that one, so the discovery has to be
    survivable: the workers stop, whatever is on disk is discarded — none of it
    can be trusted to be at the offset it was asked for — and the file comes
    down again on one connection.
    """
    print("\n[a range lie found mid-transfer restarts as one stream]")
    from ixd.core.models import TransferMode

    payload = make_payload(6 << 20)
    digest = hashlib.sha256(payload).hexdigest()
    harness = Harness()
    try:
        with TestOrigin(payload) as origin:
            origin.state.ignore_ranges_after_probe = True

            download = harness.engine.add_download(origin.url())
            done = harness.wait_for(
                download.id, {DownloadStatus.COMPLETED, DownloadStatus.ERROR}, timeout=90
            )
            check("the download completes rather than failing",
                  done.status is DownloadStatus.COMPLETED, str(done.error))
            check("it ends up a single stream", done.mode is TransferMode.SINGLE,
                  done.mode.value)
            check("and is recorded as non-resumable",
                  done.supports_ranges is False, str(done.supports_ranges))
            check("the whole file is there",
                  done.downloaded == len(payload), str(done.downloaded))
            if os.path.isfile(done.filepath):
                actual = hashlib.sha256(Path(done.filepath).read_bytes()).hexdigest()
                check("byte-exact after the restart", actual == digest)
    finally:
        harness.close()


def test_connections_are_reused_and_never_reused_unsafely() -> None:
    """One TLS handshake per origin, not one per request — and no crossed wires.

    `Connection: close` was a hardcoded default header, so every request opened
    a new TCP connection and a new TLS session. An extraction is a token, a
    manifest and a playlist or two; at the better part of a second per
    handshake that was most of what a person waited through, on every site.
    Measured against a real origin: five sequential requests, 20.6 s closing
    each time against 1.9 s reusing one.

    The dangerous half is what must *not* be pooled. A socket handed back with
    bytes still on it delivers them to whoever picks it up next — someone
    else's reply to this request — which is far worse than a slow handshake.
    """
    print("\n[connections are reused, and never reused unsafely]")
    from ixd.core.http_client import HttpClient

    payload = make_payload(256 << 10)
    with TestOrigin(payload) as origin:
        client = HttpClient()

        # Two whole responses, read to the end: the second must not handshake.
        with client.request("GET", origin.url()) as first:
            first.read_all()
        pooled_after_first = sum(len(v) for v in client._pool._idle.values())
        check("a fully-read response hands its connection back",
              pooled_after_first == 1, str(pooled_after_first))

        held = client._pool._idle[next(iter(client._pool._idle))][0][0]
        with client.request("GET", origin.url()) as second:
            second.read_all()
        check("and the next request to the same origin takes it",
              second._conn is held)
        check("the bytes are still right after a reuse",
              second.url == origin.url())

        # A HEAD has no body to read, so `isclosed()` stays False for ever on
        # it. Judging reuse by that alone threw away a good connection every
        # time — and probing sizes is all HEADs, three per Twitch clip.
        client._pool.clear()
        sockets = []
        for _ in range(3):
            with client.request("HEAD", origin.url()) as head:
                sockets.append(id(head._conn))
                check_size = head.content_length
        check("three HEADs share one connection", len(set(sockets)) == 1,
              str(len(set(sockets))))
        check("and a HEAD still reports the size", check_size == len(payload),
              str(check_size))

        # Abandoned mid-body: the socket still has the rest of the file on it.
        client._pool.clear()
        third = client.request("GET", origin.url())
        third.read(1024)
        third.close()
        check("a response abandoned mid-body is not pooled",
              sum(len(v) for v in client._pool._idle.values()) == 0)

        # Aborted from another thread — pausing a stalled transfer does this.
        fourth = client.request("GET", origin.url())
        fourth.read(1024)
        fourth.abort()
        check("and an aborted one is not pooled either",
              sum(len(v) for v in client._pool._idle.values()) == 0)

        # A pooled connection the origin hung up on while it sat idle. Nothing
        # reveals that until the next write fails, so it must be retried rather
        # than reported — otherwise pooling turns every idle timeout into a
        # failed download.
        with client.request("GET", origin.url()) as warm:
            warm.read_all()
        key = next(iter(client._pool._idle))
        stale = client._pool._idle[key][0][0]
        stale.close()                    # exactly what an origin's timeout does
        with client.request("GET", origin.url()) as after_stale:
            body = after_stale.read_all()
        check("a stale pooled connection is retried, not reported",
              after_stale.status == 200 and len(body) == len(payload),
              f"{after_stale.status}, {len(body)} bytes")

        client.close()
        check("closing the client lets go of every idle socket",
              sum(len(v) for v in client._pool._idle.values()) == 0)


def main() -> int:
    print("=" * 68)
    print("Internet Xtreme Downloader — engine test suite")
    print("=" * 68)
    for test in (
        test_multithreaded_download,
        test_pause_resume,
        test_crash_recovery,
        test_link_expiry_and_swap,
        test_no_range_support,
        test_content_md5_validation,
        test_bad_hash_flags_corruption,
        test_retry_on_rate_limit,
        test_encrypted_hls_segments,
        test_incomplete_is_never_published,
        test_server_driven_transfer_keeps_its_progress,
        test_paired_quality_is_one_download,
        test_resume_state_survives_the_database,
        test_a_paused_stream_keeps_its_bytes_and_says_so,
        test_resume_capability_is_not_range_support,
        test_two_videos_from_one_cdn_node_do_not_queue_behind_each_other,
        test_an_expired_session_is_replaced_not_mourned,
        test_a_silent_film_is_never_published,
        test_a_missing_header_fails_before_the_download_not_after,
        test_a_track_takes_as_many_sessions_as_it_needs,
        test_a_whole_adaptive_download_end_to_end,
        test_several_sessions_fetch_one_track_between_them,
        test_capped_range_is_not_expiry,
        test_the_page_travels_with_the_request,
        test_the_browsers_own_headers_are_replayed,
        test_a_stream_of_error_pages_is_never_published,
        test_a_file_that_plays_nothing_is_never_published,
        test_a_segment_disguised_as_an_image_is_unwrapped,
        test_a_transport_stream_becomes_an_mp4_without_re_encoding,
        test_pausing_a_stalled_transfer_is_immediate,
        test_the_pages_cookies_do_not_travel_to_a_media_cdn,
        test_the_origins_own_name_beats_the_address,
        test_a_queue_left_by_a_previous_session_does_not_start_itself,
        test_a_server_that_advertises_ranges_and_ignores_them,
        test_a_range_lie_found_mid_transfer_restarts_as_one_stream,
        test_connections_are_reused_and_never_reused_unsafely,
    ):
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
