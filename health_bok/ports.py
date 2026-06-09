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
    GroundedAnswer,
    ImpactAnchor,
    ImpactKnowledge,
    RetrievedEvidence,
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
    vectors over a real pgvector, asserting merge-vs-new at the threshold. The
    same port embeds a free-text *question* for grounded query retrieval (issue
    #17), so query and Concept normalization share one embedding path (ADR-0011).
    """

    def embed(self, text: str) -> list[float]:
        """Return the embedding of `text` — a list of 1536 floats."""
        ...


@runtime_checkable
class QueryAnswerer(Protocol):
    """Synthesizes a grounded, cited answer from retrieved evidence (ADR-0011).

    The Part-2 seam for natural-language query (issue #17): given the owner's
    question and the Claims/Protocols/personal-layer context retrieval gathered,
    return a `GroundedAnswer` resting *only* on that evidence — citing the specific
    Claims behind it, or abstaining when the evidence does not cover the question.
    Strictly grounded and cite-or-abstain: never the model's own general medical
    knowledge, never a blend (ADR-0011). The Claude API in production; behind its
    own port so the query service is driven in tests with a fake over a *real*
    Postgres retrieval (PRD #1 testing decisions).
    """

    def answer(self, question: str, evidence: RetrievedEvidence) -> GroundedAnswer:
        """Answer `question` grounded in `evidence`, citing Claim ids — or abstain.

        Returning citation ids the service did not retrieve is harmless: it filters
        them against the retrieved Claims so a hallucinated id never becomes a
        Citation. An answer that cites nothing (and is not an explicit abstention)
        is treated as an abstention — cite-or-abstain has no third state.
        """
        ...


@runtime_checkable
class StanceJudge(Protocol):
    """Judges the Stance of one knowledge↔anchor pair for change detection (issue #18).

    The Part-2 seam for the Impact engine: candidate generation pairs a newly-
    arrived Claim/Protocol with an existing anchor (a Decision, Goal, or Marker)
    that shares a Concept with it, and this judge decides whether the knowledge
    `reinforces`, `contradicts`, `refines`, or opens an `opportunity` against the
    anchor — or is merely `unrelated`, in which case the engine raises nothing.

    The LLM pass is what keeps the inbox honest: Concept overlap alone floods the
    owner with the merely-related, so a stance is *judged*, not inferred from the
    overlap (CONTEXT.md "Stance"). The Claude API in production; behind its own port
    so the engine is driven in tests with a fake over *real* Concept-overlap
    candidates from a real Postgres (PRD #1 testing decisions).
    """

    def judge(self, knowledge: ImpactKnowledge, anchor: ImpactAnchor) -> str:
        """Return the Stance for this pair — one of `reinforces | contradicts |
        refines | opportunity`, or `unrelated` to discard it.

        An unrecognized return is treated as `unrelated` by the engine, so a sloppy
        judgement can never mint an out-of-vocabulary Impact.
        """
        ...
