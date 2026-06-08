"""In-memory fakes for the three ports.

They replace the external services (YouTube, Claude, Resend) at the same seams
the real adapters sit behind, so the integration test exercises the whole job
without any network — while Postgres stays real (PRD #1 testing decisions).
"""

from __future__ import annotations

from health_bok.models import Digest, FetchedTranscript


class FakeContentSource:
    """Returns a canned Transcript and records what was requested."""

    def __init__(self, transcript: FetchedTranscript):
        self._transcript = transcript
        self.fetched_video_ids: list[str] = []

    def fetch_transcript(self, video_id: str) -> FetchedTranscript:
        self.fetched_video_ids.append(video_id)
        return self._transcript


class FakeSummarizer:
    """Returns a canned Summary and records the Transcripts it was given."""

    def __init__(self, summary: str):
        self._summary = summary
        self.summarized: list[FetchedTranscript] = []

    def summarize(self, transcript: FetchedTranscript) -> str:
        self.summarized.append(transcript)
        return self._summary


class FakeDigestSender:
    """Captures every Digest it is asked to send."""

    def __init__(self) -> None:
        self.sent: list[Digest] = []

    def send(self, digest: Digest) -> None:
        self.sent.append(digest)
