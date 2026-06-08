"""YouTube adapter for the ContentSource port (ADR-0006).

Combines two free, key-less sources: `yt-dlp` for the video's metadata
(provenance) and `youtube-transcript-api` for its timestamped captions (the
Transcript). The Whisper fallback for caption-less videos lands in a later
slice; this slice covers the free caption path only.
"""

from __future__ import annotations

import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ..models import (
    CreatorIdentity,
    CreatorResolutionError,
    FetchedTranscript,
    Provenance,
    TranscriptSegment,
)

YOUTUBE_BASE = "https://www.youtube.com"

# YouTube publishes each channel's latest uploads as an Atom feed keyed by the
# stable channel_id — keyless and free, exactly what the daily diff needs.
RSS_FEED = "https://www.youtube.com/feeds/videos.xml?channel_id="
_FEED_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
}
_FEED_TIMEOUT = 30


class YouTubeContentSource:
    """Fetches a video's Transcript and provenance from YouTube."""

    def resolve_creator(self, reference: str) -> CreatorIdentity:
        # Imported lazily so importing the module needs no network or binary.
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError

        url = _channel_url(reference)
        # extract_flat + a zero-length playlist window asks yt-dlp for the
        # channel's own metadata without enumerating its (possibly thousands of)
        # videos — backfill listing is a separate, later concern.
        opts = {
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
            "extract_flat": True,
            "playlist_items": "0",
        }
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError as exc:
            raise CreatorResolutionError(reference) from exc

        channel_id = info.get("channel_id") or info.get("id")
        name = info.get("channel") or info.get("uploader") or info.get("title")
        if not channel_id or not name:
            raise CreatorResolutionError(reference)
        return CreatorIdentity(channel_id=channel_id, name=name)

    def discover_videos(self, channel_id: str) -> list[str]:
        # Parse the channel's Atom upload feed with the stdlib — no API key, no
        # quota, no third-party SDK. Returns the latest video IDs newest-first;
        # the daily job diffs them against the already-processed set.
        url = f"{RSS_FEED}{channel_id}"
        request = urllib.request.Request(url, headers={"User-Agent": "health-bok"})
        with urllib.request.urlopen(request, timeout=_FEED_TIMEOUT) as response:
            feed = response.read()
        root = ET.fromstring(feed)
        return [
            element.text
            for element in root.iterfind("atom:entry/yt:videoId", _FEED_NS)
            if element.text
        ]

    def fetch_transcript(self, video_id: str) -> FetchedTranscript:
        provenance = self._fetch_provenance(video_id)
        segments = self._fetch_segments(video_id)
        return FetchedTranscript(
            provenance=provenance, segments=segments, source="captions"
        )

    def _fetch_provenance(self, video_id: str) -> Provenance:
        # Imported lazily so importing the module needs no network or binary.
        from yt_dlp import YoutubeDL

        url = f"https://www.youtube.com/watch?v={video_id}"
        opts = {"quiet": True, "skip_download": True, "no_warnings": True}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        return Provenance(
            video_id=info["id"],
            title=info["title"],
            channel_id=info["channel_id"],
            channel_name=info.get("channel") or info.get("uploader"),
            published_at=_parse_upload_date(info.get("upload_date")),
        )

    def _fetch_segments(self, video_id: str) -> list[TranscriptSegment]:
        from youtube_transcript_api import YouTubeTranscriptApi

        raw = YouTubeTranscriptApi.get_transcript(video_id)
        return [
            TranscriptSegment(
                text=entry["text"],
                start=float(entry["start"]),
                duration=float(entry.get("duration", 0.0)),
            )
            for entry in raw
        ]


def _channel_url(reference: str) -> str:
    """Normalize a Creator reference to a channel URL yt-dlp can resolve.

    Accepts a full URL (channel, @handle, or legacy /c//user page), an
    `@handle`, or a bare handle, so the owner can paste whatever YouTube gave
    them.
    """
    ref = reference.strip()
    if ref.startswith(("http://", "https://")):
        return ref
    if ref.startswith("@"):
        return f"{YOUTUBE_BASE}/{ref}"
    return f"{YOUTUBE_BASE}/@{ref}"


def _parse_upload_date(upload_date: str | None) -> datetime:
    """yt-dlp gives `upload_date` as 'YYYYMMDD'; treat it as a UTC date."""
    if not upload_date:
        return datetime.now(timezone.utc)
    return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
