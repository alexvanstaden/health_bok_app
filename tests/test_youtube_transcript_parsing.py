"""The YouTube adapter's caption adaptation (ADR-0006) — pure, no network.

`_fetch_segments` turns `youtube-transcript-api`'s response into
`TranscriptSegment`s. The library's 1.x API replaced the old static
`get_transcript(video_id)` with an instance `fetch(video_id)` that returns snippet
*objects* (with `.text`/`.start`/`.duration` attributes, not dicts). These guard
that contract and the no-captions → ``None`` fallback that sends the daily path to
Whisper.
"""

from __future__ import annotations

from types import SimpleNamespace

from health_bok.adapters.youtube import YouTubeContentSource


class _FakeApi:
    """Stands in for `youtube_transcript_api.YouTubeTranscriptApi` (1.x shape)."""

    snippets = [
        SimpleNamespace(text="Rapamycin extends lifespan.", start=12.0, duration=3.0),
        SimpleNamespace(text="In mice.", start=15.0, duration=1.5),
    ]

    def fetch(self, video_id, *args, **kwargs):  # noqa: D401 - mirrors the 1.x signature
        return list(self.snippets)


def test_fetch_segments_adapts_snippet_objects(monkeypatch):
    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _FakeApi)

    segments = YouTubeContentSource()._fetch_segments("vid123")

    assert [s.text for s in segments] == ["Rapamycin extends lifespan.", "In mice."]
    assert segments[0].start == 12.0 and segments[0].duration == 3.0


def test_fetch_segments_returns_none_when_no_captions(monkeypatch):
    from youtube_transcript_api import TranscriptsDisabled

    class _NoCaps:
        def fetch(self, video_id, *args, **kwargs):
            raise TranscriptsDisabled(video_id)

    monkeypatch.setattr("youtube_transcript_api.YouTubeTranscriptApi", _NoCaps)

    assert YouTubeContentSource()._fetch_segments("vid123") is None
