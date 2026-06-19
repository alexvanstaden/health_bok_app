"""The three ports that isolate every external boundary.

Each external service is reached only through one of these seams, so the daily
job can be driven in tests with fakes while Postgres stays real (PRD #1 testing
decisions). The real adapters live in `health_bok.adapters`; importing this
module pulls in no third-party SDKs.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import (
    CandidateDetails,
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

    def discover_playlist_videos(self, playlist_id: str) -> list[str]:
        """Return a playlist's recent video IDs, newest first (issue #69).

        The one-off "Process me" ingestion source: the unlisted playlist's public
        RSS feed, read with no auth, exactly like `discover_videos` reads a channel's.
        The daily job diffs these against everything already known and drives the new
        ones through the same spine as watched-Creator uploads. Raises on a feed it
        cannot reach, so the playlist's failure is isolated like a single Creator's.
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

    def fetch_candidate_details(self, video_id: str) -> CandidateDetails:
        """Lazily fetch one backfill Candidate's real description + accurate date (issue #31).

        A single per-video extraction that recovers what the cheap one-pass backfill
        listing omits — the per-video description and the accurate publish date — run
        *only* when the owner asks for it on a metadata-only Candidate, so the expensive
        per-video fetch never enters the cheap Creator-add listing path (user story 29).
        Still no Transcript and no Whisper: this stays metadata, just better metadata.
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
class ChatModel(Protocol):
    """One provider-neutral LLM turn: a system + user prompt in, text out (ADR-0012).

    The shared transport seam behind every chat-backed adapter — the Summarizer,
    Extractor, QueryAnswerer, StanceJudge, and ConceptProposer. Each of those owns
    its prompts and its parsing; only this call differs between providers, so
    swapping OpenAI for Anthropic (or a fake in tests) is one factory away
    (`health_bok.llm`) and never touches the feature adapters.
    """

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        """Return the model's text reply to `system` + `user`."""
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
class ConceptProposer(Protocol):
    """Proposes candidate Concept *terms* a Goal concerns, for owner-curated minting
    (issue #39).

    The LLM half of the "when should a new Concept be added?" assist: given a Goal's
    title + detail, return short canonical Concept names the Goal is about. It only
    *proposes* — each term is then checked against the existing catalogue with the
    same conservative logic `ConceptNormalizer` uses, so a term that resolves to an
    existing Concept is dropped and only a genuinely new one is surfaced as an "add
    new Concept?" suggestion the owner confirms. The model never mints a Concept; the
    approval gate stays with the owner (ADR-0004).

    The Claude API in production; behind its own port so the suggester is driven in
    tests with a fake over a *real* Postgres catalogue (PRD #1 testing decisions),
    and so a model failure degrades to "no new suggestions" without touching the
    existing-Concept path.
    """

    def propose(self, title: str, detail: str | None) -> list[str]:
        """Return candidate Concept terms for a Goal — short canonical names, no
        prose. An empty list is a valid answer (the Goal suggests no new Concept)."""
        ...


@runtime_checkable
class HierarchyProposer(Protocol):
    """Proposes broader `broader-of` parents for a Concept, for owner-curated roll-up
    (ADR-0013).

    The LLM half of the hierarchy assist (mirroring `ConceptProposer`): given a
    Concept's name and a short list of *nearby existing Concepts* (the embedding-
    cluster the caller supplies over pgvector), return which of those are genuinely
    *broader* — the Concepts this one should roll up under ("Brain" for "Brain
    metabolism"). It proposes only from the nearby set, so a proposal always names a
    Concept that already exists; the caller filters out self, existing parents, and
    any that would close a cycle, and the owner confirms before roll-up sees the
    edge (ADR-0004, user story 19). The model never confirms a parent.

    The Claude API in production; behind its own port so the suggester is driven in
    tests with a fake over a *real* Postgres catalogue (PRD #1 testing decisions),
    and so a model failure degrades to "no suggestions" without breaking the page.
    """

    def propose(self, concept_name: str, nearby: list[str]) -> list[str]:
        """Return the broader parents for `concept_name`, drawn from `nearby` — short
        canonical Concept names, no prose. An empty list is valid (no clear parent)."""
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
