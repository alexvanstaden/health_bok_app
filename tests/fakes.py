"""In-memory fakes for the three ports.

They replace the external services (YouTube, Claude, Resend) at the same seams
the real adapters sit behind, so the integration test exercises the whole job
without any network — while Postgres stays real (PRD #1 testing decisions).
"""

from __future__ import annotations

from health_bok.models import (
    CreatorIdentity,
    CreatorResolutionError,
    Digest,
    FetchedTranscript,
)


class FakeContentSource:
    """Fakes the ContentSource port: canned Transcript + handle resolution.

    `identities` maps a reference (@handle or URL) to the CreatorIdentity it
    resolves to; an unmapped reference raises CreatorResolutionError, mirroring
    the real adapter. Every resolution is recorded in `resolved` so tests can
    assert a Creator is resolved exactly once.
    """

    def __init__(
        self,
        transcript: FetchedTranscript | None = None,
        identities: dict[str, CreatorIdentity] | None = None,
    ):
        self._transcript = transcript
        self.fetched_video_ids: list[str] = []
        self._identities = dict(identities or {})
        self.resolved: list[str] = []

    def resolve_creator(self, reference: str) -> CreatorIdentity:
        self.resolved.append(reference)
        try:
            return self._identities[reference]
        except KeyError:
            raise CreatorResolutionError(reference) from None

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
