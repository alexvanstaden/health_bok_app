"""YouTube adapter for the ContentSource port (ADR-0006).

Combines two free, key-less sources: `yt-dlp` for the video's metadata
(provenance) and `youtube-transcript-api` for its timestamped captions (the
Transcript). When a video has no captions, `fetch_transcript` returns ``None``
and the caller falls back to `fetch_audio` + the Whisper `Transcriber` — yt-dlp
also does the audio download here, keeping every YouTube concern behind this one
adapter while transcription stays a separate seam.
"""

from __future__ import annotations

import os
import tempfile
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from ..models import (
    CandidateMetadata,
    CreatorIdentity,
    CreatorResolutionError,
    FetchedAudio,
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

    def list_backcatalogue(self, channel_id: str) -> list[CandidateMetadata]:
        # Backfill listing (issue #7): enumerate the channel's uploads with
        # extract_flat so the whole back-catalogue is listed in one cheap pass —
        # no per-video extraction, no captions, no audio (user story 29). The
        # caller applies the recency cutoff. Flat entries carry ids + titles;
        # an exact publish date and description aren't always part of the flat
        # record, so they're filled best-effort — the owner curates by title in
        # the approval queue regardless.
        from yt_dlp import YoutubeDL

        url = f"{YOUTUBE_BASE}/channel/{channel_id}/videos"
        opts = {
            "quiet": True,
            "skip_download": True,
            "no_warnings": True,
            "extract_flat": True,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        candidates: list[CandidateMetadata] = []
        for entry in info.get("entries") or []:
            video_id = entry.get("id")
            if not video_id:
                continue
            candidates.append(
                CandidateMetadata(
                    video_id=video_id,
                    title=entry.get("title") or "",
                    description=entry.get("description") or "",
                    published_at=_entry_published_at(entry),
                )
            )
        return candidates

    def fetch_transcript(self, video_id: str) -> FetchedTranscript | None:
        # Captions are checked first: a caption-less video returns None without
        # the (wasted) metadata fetch, and the caller falls back to Whisper.
        segments = self._fetch_segments(video_id)
        if segments is None:
            return None
        provenance = self._fetch_provenance(video_id)
        return FetchedTranscript(
            provenance=provenance, segments=segments, source="captions"
        )

    def fetch_audio(self, video_id: str) -> FetchedAudio:
        # Only the daily path reaches here, and only for caption-less videos
        # (PRD #1, user stories 10, 29). yt-dlp grabs the smallest standalone
        # audio stream — no ffmpeg post-processing — which Whisper accepts as-is.
        from yt_dlp import YoutubeDL

        provenance = self._fetch_provenance(video_id)
        url = f"https://www.youtube.com/watch?v={video_id}"
        with tempfile.TemporaryDirectory() as workdir:
            opts = {
                "quiet": True,
                "no_warnings": True,
                "format": "bestaudio/best",
                "outtmpl": os.path.join(workdir, "%(id)s.%(ext)s"),
            }
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            with open(path, "rb") as handle:
                data = handle.read()
        return FetchedAudio(
            provenance=provenance, data=data, suffix=os.path.splitext(path)[1]
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

    def _fetch_segments(self, video_id: str) -> list[TranscriptSegment] | None:
        from youtube_transcript_api import YouTubeTranscriptApi

        try:
            from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled
        except ImportError:  # exception layout differs across library versions
            from youtube_transcript_api._errors import (
                NoTranscriptFound,
                TranscriptsDisabled,
            )

        try:
            raw = YouTubeTranscriptApi.get_transcript(video_id)
        except (TranscriptsDisabled, NoTranscriptFound):
            # No captions for this video — signal absence so the caller falls
            # back to Whisper. A genuine fetch failure still raises and is
            # isolated as a per-video error.
            return None
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


def _entry_published_at(entry: dict) -> datetime:
    """Best-effort publish date for a flat back-catalogue entry (issue #7).

    A flat entry may carry a `timestamp` (epoch seconds) or an `upload_date`;
    when it carries neither, fall back to now so an undated entry is
    conservatively kept by the recency cutoff — the owner still curates it at
    approval, so over-inclusion is the safe failure here.
    """
    timestamp = entry.get("timestamp")
    if timestamp is not None:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return _parse_upload_date(entry.get("upload_date"))
