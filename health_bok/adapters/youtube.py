"""YouTube adapter for the ContentSource port (ADR-0006).

Combines two free, key-less sources: `yt-dlp` for the video's metadata
(provenance) and `youtube-transcript-api` for its timestamped captions (the
Transcript). The Whisper fallback for caption-less videos lands in a later
slice; this slice covers the free caption path only.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..models import FetchedTranscript, Provenance, TranscriptSegment


class YouTubeContentSource:
    """Fetches a video's Transcript and provenance from YouTube."""

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


def _parse_upload_date(upload_date: str | None) -> datetime:
    """yt-dlp gives `upload_date` as 'YYYYMMDD'; treat it as a UTC date."""
    if not upload_date:
        return datetime.now(timezone.utc)
    return datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
