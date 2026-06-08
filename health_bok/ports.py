"""The three ports that isolate every external boundary.

Each external service is reached only through one of these seams, so the daily
job can be driven in tests with fakes while Postgres stays real (PRD #1 testing
decisions). The real adapters live in `health_bok.adapters`; importing this
module pulls in no third-party SDKs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Digest, FetchedTranscript


@runtime_checkable
class ContentSource(Protocol):
    """Acquires raw content + provenance for a Source (ADR-0006).

    YouTube is the first adapter behind this seam. Slice 1 needs only the
    single-video fetch; discovery (RSS) and backfill Candidate listing are added
    behind this same port in later slices.
    """

    def fetch_transcript(self, video_id: str) -> FetchedTranscript:
        """Fetch the video's Transcript (with timestamps) and full provenance."""
        ...


@runtime_checkable
class Summarizer(Protocol):
    """Turns a Transcript into a prose Summary (CONTEXT.md)."""

    def summarize(self, transcript: FetchedTranscript) -> str:
        """Return a prose Summary of the Transcript."""
        ...


@runtime_checkable
class DigestSender(Protocol):
    """Sends the daily Digest email."""

    def send(self, digest: Digest) -> None:
        """Deliver the Digest. Never called with an empty Digest."""
        ...
