"""MPEG-DASH manifest (MPD) parsing.

Supports the three ways a DASH representation can address its media:
``SegmentTemplate`` (with ``$Number$``/``$Time$`` and ``SegmentTimeline``),
``SegmentList``, and ``SegmentBase`` (a single file the engine downloads with
ordinary byte ranges instead of the segmented path).
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..core.errors import ExtractionError
from ..core.models import MediaFormat, MediaSegment

if TYPE_CHECKING:  # pragma: no cover
    from ..core.http_client import HttpClient


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_all(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _find(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    for child in element:
        if _local_name(child.tag) == name:
            return child
    return None


def parse_iso_duration(value: str) -> float:
    """``PT1H2M3.5S`` → seconds."""
    if not value:
        return 0.0
    match = re.match(
        r"^P(?:(\d+(?:\.\d+)?)Y)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)D)?"
        r"(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$",
        value.strip(),
    )
    if not match:
        return 0.0
    years, months, days, hours, minutes, seconds = (
        float(group) if group else 0.0 for group in match.groups()
    )
    return (years * 31536000 + months * 2592000 + days * 86400
            + hours * 3600 + minutes * 60 + seconds)


def expand_template(template: str, *, representation_id: str = "", number: int | None = None,
                    time_value: int | None = None, bandwidth: int | None = None) -> str:
    """Substitute DASH ``$Identifier$`` placeholders, honouring ``%0Nd`` formats."""
    def replace(match: re.Match) -> str:
        identifier = match.group(1)
        format_spec = match.group(2) or ""
        if identifier == "":
            return "$"
        values = {
            "RepresentationID": representation_id,
            "Number": number,
            "Time": time_value,
            "Bandwidth": bandwidth,
        }
        value = values.get(identifier)
        if value is None:
            return match.group(0)
        if format_spec:
            try:
                return ("{:" + format_spec.lstrip("%") + "}").format(int(value))
            except (ValueError, KeyError):
                return str(value)
        return str(value)

    return re.sub(r"\$(\w*)(%0\d+d)?\$", replace, template)


@dataclass(slots=True)
class _SegmentInfo:
    """Normalised addressing info collected down the MPD hierarchy."""

    template_media: str = ""
    template_init: str = ""
    timescale: int = 1
    duration: int = 0
    start_number: int = 1
    timeline: list[tuple[int, int]] = field(default_factory=list)  # (start_time, duration)
    list_urls: list[str] = field(default_factory=list)
    list_init: str = ""
    base_url: str = ""
    index_range: str = ""

    def merge(self, other: "_SegmentInfo") -> "_SegmentInfo":
        merged = _SegmentInfo(
            template_media=other.template_media or self.template_media,
            template_init=other.template_init or self.template_init,
            timescale=other.timescale if other.timescale != 1 else self.timescale,
            duration=other.duration or self.duration,
            start_number=other.start_number if other.start_number != 1 else self.start_number,
            timeline=other.timeline or self.timeline,
            list_urls=other.list_urls or self.list_urls,
            list_init=other.list_init or self.list_init,
            base_url=other.base_url or self.base_url,
            index_range=other.index_range or self.index_range,
        )
        return merged


def _read_segment_info(element: ElementTree.Element) -> _SegmentInfo:
    info = _SegmentInfo()

    template = _find(element, "SegmentTemplate")
    if template is not None:
        info.template_media = template.get("media", "")
        info.template_init = template.get("initialization", "")
        info.timescale = int(template.get("timescale", 1) or 1)
        info.duration = int(float(template.get("duration", 0) or 0))
        info.start_number = int(template.get("startNumber", 1) or 1)
        timeline = _find(template, "SegmentTimeline")
        if timeline is not None:
            current_time = 0
            for segment in _find_all(timeline, "S"):
                start = segment.get("t")
                if start is not None:
                    current_time = int(start)
                segment_duration = int(segment.get("d", 0) or 0)
                repeat = int(segment.get("r", 0) or 0)
                for _ in range(repeat + 1):
                    info.timeline.append((current_time, segment_duration))
                    current_time += segment_duration

    segment_list = _find(element, "SegmentList")
    if segment_list is not None:
        info.timescale = int(segment_list.get("timescale", info.timescale) or info.timescale)
        info.duration = int(float(segment_list.get("duration", info.duration) or info.duration))
        initialization = _find(segment_list, "Initialization")
        if initialization is not None:
            info.list_init = initialization.get("sourceURL", "")
        for segment_url in _find_all(segment_list, "SegmentURL"):
            media = segment_url.get("media")
            if media:
                info.list_urls.append(media)

    segment_base = _find(element, "SegmentBase")
    if segment_base is not None:
        info.index_range = segment_base.get("indexRange", "") or "present"

    base_url_element = _find(element, "BaseURL")
    if base_url_element is not None and (base_url_element.text or "").strip():
        info.base_url = base_url_element.text.strip()

    return info


def parse_mpd(text: str, manifest_url: str) -> list[MediaFormat]:
    """Parse an MPD into selectable formats with their segment lists resolved.

    Segment URLs are pure string expansion of the manifest — no extra network
    round-trips — so each returned format is self-contained and can be handed
    straight to the engine or serialised over IPC.
    """
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise ExtractionError(f"malformed MPD: {exc}") from exc

    if _local_name(root.tag) != "MPD":
        raise ExtractionError("not a DASH manifest")

    mpd_base = _find(root, "BaseURL")
    root_base = urllib.parse.urljoin(
        manifest_url, mpd_base.text.strip()
    ) if mpd_base is not None and (mpd_base.text or "").strip() else manifest_url

    total_duration = parse_iso_duration(root.get("mediaPresentationDuration", ""))
    formats: list[MediaFormat] = []

    for period in _find_all(root, "Period"):
        period_info = _read_segment_info(period)
        period_base = urllib.parse.urljoin(root_base, period_info.base_url) \
            if period_info.base_url else root_base

        for adaptation_set in _find_all(period, "AdaptationSet"):
            set_info = period_info.merge(_read_segment_info(adaptation_set))
            set_base = urllib.parse.urljoin(period_base, set_info.base_url) \
                if set_info.base_url else period_base
            set_mime = adaptation_set.get("mimeType", "")
            set_content = adaptation_set.get("contentType", "")

            for representation in _find_all(adaptation_set, "Representation"):
                info = set_info.merge(_read_segment_info(representation))
                base = urllib.parse.urljoin(set_base, info.base_url) \
                    if info.base_url else set_base

                mime = representation.get("mimeType", set_mime)
                codecs = representation.get("codecs", "")
                is_video = "video" in (mime or "") or "video" in (set_content or "")
                is_audio = "audio" in (mime or "") or "audio" in (set_content or "")
                if not is_video and not is_audio:
                    continue

                try:
                    bandwidth = int(representation.get("bandwidth", 0) or 0)
                except ValueError:
                    bandwidth = 0
                height = int(representation.get("height", 0) or 0)
                width = int(representation.get("width", 0) or 0)

                extension = "m4a" if is_audio and not is_video else "mp4"
                if "webm" in (mime or ""):
                    extension = "webm"

                # SegmentBase = one contiguous file: the engine can range it directly.
                direct_url = ""
                protocol = "dash"
                if info.index_range and not info.template_media and not info.list_urls:
                    direct_url = base
                    protocol = "https"

                media_format = MediaFormat(
                    format_id=representation.get("id", "") or f"dash-{bandwidth}",
                    url=direct_url,
                    ext=extension,
                    protocol=protocol,
                    width=width,
                    height=height,
                    fps=float(representation.get("frameRate", 0) or 0)
                    if str(representation.get("frameRate", "")).isdigit() else 0.0,
                    tbr=bandwidth / 1000.0,
                    vcodec=codecs if is_video else "none",
                    acodec=(codecs if is_audio else ("aac" if is_video and is_audio else "none")),
                    quality_label=f"{height}p" if height else "",
                    manifest_url=manifest_url,
                    note="dash",
                )
                if not direct_url:
                    try:
                        media_format.segments = build_segments(
                            info, base, total_duration, media_format
                        )
                    except ExtractionError:
                        # A representation we cannot address is simply not offered.
                        continue
                formats.append(media_format)

    if not formats:
        raise ExtractionError("MPD contains no audio or video representations")
    return formats


def build_segments(info: _SegmentInfo, base: str, total_duration: float,
                   fmt: MediaFormat) -> list[MediaSegment]:
    """Expand a representation's addressing scheme into concrete segments."""
    segments: list[MediaSegment] = []
    index = 0
    bandwidth = int(fmt.tbr * 1000)

    if info.template_init:
        segments.append(MediaSegment(
            index=index,
            url=urllib.parse.urljoin(base, expand_template(
                info.template_init, representation_id=fmt.format_id, bandwidth=bandwidth
            )),
            init=True,
        ))
        index += 1
    elif info.list_init:
        segments.append(MediaSegment(
            index=index, url=urllib.parse.urljoin(base, info.list_init), init=True
        ))
        index += 1

    if info.list_urls:
        for url in info.list_urls:
            segments.append(MediaSegment(
                index=index, url=urllib.parse.urljoin(base, url)
            ))
            index += 1
        return segments

    if info.template_media:
        if info.timeline:
            # $Number$ counts media segments only — the init segment is not one.
            for position, (start_time, duration) in enumerate(info.timeline):
                segments.append(MediaSegment(
                    index=index,
                    url=urllib.parse.urljoin(base, expand_template(
                        info.template_media,
                        representation_id=fmt.format_id,
                        number=info.start_number + position,
                        time_value=start_time,
                        bandwidth=bandwidth,
                    )),
                    duration=duration / max(1, info.timescale),
                ))
                index += 1
            return segments

        if info.duration > 0 and total_duration > 0:
            segment_seconds = info.duration / max(1, info.timescale)
            count = max(1, int(total_duration / segment_seconds + 0.999))
            for offset in range(count):
                segments.append(MediaSegment(
                    index=index,
                    url=urllib.parse.urljoin(base, expand_template(
                        info.template_media,
                        representation_id=fmt.format_id,
                        number=info.start_number + offset,
                        time_value=int(offset * info.duration),
                        bandwidth=bandwidth,
                    )),
                    duration=segment_seconds,
                ))
                index += 1
            return segments

    if not segments:
        raise ExtractionError(
            f"cannot determine the segment layout for DASH format {fmt.format_id}"
        )
    return segments


def fetch_formats(client: "HttpClient", manifest_url: str,
                  headers: dict[str, str] | None = None) -> list[MediaFormat]:
    return parse_mpd(client.get_text(manifest_url, headers), manifest_url)
