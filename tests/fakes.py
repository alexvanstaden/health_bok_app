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
    """Fakes the ContentSource port: RSS discovery, Transcript fetch, resolution.

    * `feeds` maps a channel_id to the video IDs its RSS feed returns, newest
      first — what the daily job diffs against the processed set.
    * `transcripts` maps a video_id to the FetchedTranscript its fetch returns;
      a single `transcript` is the fallback when a video isn't in the map.
    * `errors` maps a channel_id *or* a video_id to an exception to raise when
      that channel is discovered or that video is fetched, so failure-isolation
      tests can make exactly one Creator or video blow up.
    * `identities` maps a reference (@handle or URL) to the CreatorIdentity it
      resolves to; an unmapped reference raises CreatorResolutionError.

    Calls are recorded (`discovered`, `fetched_video_ids`, `resolved`) so tests
    can assert what the job did and did not touch.
    """

    def __init__(
        self,
        transcript: FetchedTranscript | None = None,
        identities: dict[str, CreatorIdentity] | None = None,
        feeds: dict[str, list[str]] | None = None,
        transcripts: dict[str, FetchedTranscript] | None = None,
        errors: dict[str, Exception] | None = None,
    ):
        self._transcript = transcript
        self._transcripts = dict(transcripts or {})
        self._feeds = dict(feeds or {})
        self._errors = dict(errors or {})
        self._identities = dict(identities or {})
        self.fetched_video_ids: list[str] = []
        self.discovered: list[str] = []
        self.resolved: list[str] = []

    def resolve_creator(self, reference: str) -> CreatorIdentity:
        self.resolved.append(reference)
        try:
            return self._identities[reference]
        except KeyError:
            raise CreatorResolutionError(reference) from None

    def discover_videos(self, channel_id: str) -> list[str]:
        self.discovered.append(channel_id)
        if channel_id in self._errors:
            raise self._errors[channel_id]
        return list(self._feeds.get(channel_id, []))

    def fetch_transcript(self, video_id: str) -> FetchedTranscript:
        self.fetched_video_ids.append(video_id)
        if video_id in self._errors:
            raise self._errors[video_id]
        return self._transcripts.get(video_id, self._transcript)


class FakeSummarizer:
    """Returns a canned Summary and records the Transcripts it was given."""

    def __init__(self, summary: str):
        self._summary = summary
        self.summarized: list[FetchedTranscript] = []

    def summarize(self, transcript: FetchedTranscript) -> str:
        self.summarized.append(transcript)
        return self._summary


class FakeDigestSender:
    """Captures every Digest it is asked to send.

    `fail_times` makes the first N sends raise before recording, so tests can
    exercise a failed send that a later run retries (PRD #1, user story 24).
    """

    def __init__(self, fail_times: int = 0) -> None:
        self.sent: list[Digest] = []
        self._remaining_failures = fail_times

    def send(self, digest: Digest) -> None:
        if self._remaining_failures > 0:
            self._remaining_failures -= 1
            raise RuntimeError("digest send failed")
        self.sent.append(digest)
