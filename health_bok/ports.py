"""The three ports that isolate every external boundary.

Each external service is reached only through one of these seams, so the daily
job can be driven in tests with fakes while Postgres stays real (PRD #1 testing
decisions). The real adapters live in `health_bok.adapters`; importing this
module pulls in no third-party SDKs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    CandidateMetadata,
    CreatorIdentity,
    Digest,
    Extraction,
    FetchedAudio,
    FetchedTranscript,
    TranscriptSegment,
)


@runtime_checkable
class ContentSource(Protocol):
    """Acquires raw content + provenance for a Source (ADR-0006).

    YouTube is the first adapter behind this seam. Slice 1 needs only the
    single-video fetch; discovery (RSS) and backfill Candidate listing are added
    behind this same port in later slices.
    """

    def resolve_creator(self, reference: str) -> CreatorIdentity:
        """Resolve an @handle or channel URL to a Creator's stable identity.

        Called once when a Creator is added (PRD #1, user story 2); the watch list
        then stores the returned `channel_id` and the daily job never re-resolves.
        Raises CreatorResolutionError if the reference names no reachable channel.
        """
        ...

    def discover_videos(self, channel_id: str) -> list[str]:
        """Return the channel's recent video IDs, newest first (PRD #1, story 5).

        The daily job diffs these against the already-processed set to find new
        uploads. YouTube's RSS feed surfaces only the latest handful of videos,
        which is all a daily run needs; full back-catalogue listing is a separate,
        later concern. Raises on a feed it cannot reach, so the job can isolate a
        single Creator's failure without aborting the run.
        """
        ...

    def list_backcatalogue(self, channel_id: str) -> list[CandidateMetadata]:
        """List the Creator's whole back-catalogue as metadata-only Candidates.

        Called once when a Creator is added (issue #7) to seed *backfill*
        Candidates. Each past upload is returned as metadata only — title,
        description, publish date, URL — with **no** Transcript fetched and
        Whisper never called (ADR-0004; user story 29). The recency cutoff is
        applied by the caller, not here, so this stays a dumb full lister; that
        is also what lets a test assert the cutoff against a fake that returns
        the whole catalogue. Distinct from `discover_videos`, which surfaces only
        the latest handful for the daily diff.
        """
        ...

    def fetch_transcript(self, video_id: str) -> FetchedTranscript | None:
        """Fetch the video's caption Transcript (with timestamps) and provenance.

        Returns ``None`` when the video has no captions, so the daily job can fall
        back to Whisper (PRD #1, user stories 9-10): free captions are preferred,
        and only their genuine absence triggers the paid audio path. A `None` here
        is "no captions", not "fetch failed" — a real failure still raises, so it
        is isolated like any other per-video error rather than silently downgraded.
        """
        ...

    def fetch_audio(self, video_id: str) -> FetchedAudio:
        """Download a caption-less video's audio (+ provenance) for Whisper.

        Called only on the daily path, and only after `fetch_transcript` returned
        ``None`` — backfill never fetches audio (user story 29). Supplies the
        provenance the absent-caption path otherwise lacks, so the Transcript the
        Whisper transcription produces stays fully attributable.
        """
        ...


@runtime_checkable
class Transcriber(Protocol):
    """Transcribes a caption-less video's audio into segments (Whisper, daily only).

    The one external boundary added by slice 4 (the OpenAI Whisper API). Kept
    behind its own seam — separate from the YouTube `ContentSource` that downloads
    the audio — so the captions-vs-Whisper fallback decision can be driven in
    tests with fakes, and so no third-party SDK leaks into the orchestrator.
    """

    def transcribe(self, audio: FetchedAudio) -> list[TranscriptSegment]:
        """Transcribe `audio.data` into timestamped segments."""
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


@runtime_checkable
class Extractor(Protocol):
    """Pulls the Body-of-Knowledge layer out of a Transcript (ADR-0010).

    The Part-2 seam added by this slice (the Claude API in production). Precision-
    first: only the substantive, load-bearing assertions a creator actually
    argues, each grounded to a locator and preserving scope qualifiers; vague
    advice stays a Claim rather than becoming a Protocol. Kept behind its own port
    so the admit pipeline can be driven in tests with a fake, no SDK in the
    orchestrator (PRD #1 testing decisions).
    """

    def extract(self, transcript: FetchedTranscript) -> Extraction:
        """Return the Claims and Protocols (with Concept mentions) of a Transcript."""
        ...


@runtime_checkable
class Embedder(Protocol):
    """Embeds text into a 1536-d vector for Concept normalization (ADR-0008).

    OpenAI `text-embedding-3-small` in production. Behind its own seam so Concept
    normalization runs in tests against a `FakeEmbedder` emitting controlled
    vectors over a real pgvector, asserting merge-vs-new at the threshold.
    """

    def embed(self, text: str) -> list[float]:
        """Return the embedding of `text` — a list of 1536 floats."""
        ...
