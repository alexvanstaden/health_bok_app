"""In-memory fakes for the three ports.

They replace the external services (YouTube, Claude, Resend) at the same seams
the real adapters sit behind, so the integration test exercises the whole job
without any network — while Postgres stays real (PRD #1 testing decisions).
"""

from __future__ import annotations

import hashlib

from health_bok.models import (
    CandidateDetails,
    CandidateMetadata,
    CreatorIdentity,
    CreatorResolutionError,
    Digest,
    Extraction,
    FetchedAudio,
    FetchedTranscript,
    GroundedAnswer,
    RetrievedEvidence,
    TranscriptSegment,
)


class FakeContentSource:
    """Fakes the ContentSource port: RSS discovery, Transcript fetch, resolution.

    * `feeds` maps a channel_id to the video IDs its RSS feed returns, newest
      first — what the daily job diffs against the processed set.
    * `transcripts` maps a video_id to the FetchedTranscript its fetch returns;
      a single `transcript` is the fallback when a video isn't in the map.
    * `audio` maps a video_id to the FetchedAudio its audio download returns. A
      video listed here but *not* in `transcripts` has no captions, so
      `fetch_transcript` returns ``None`` for it and the job falls back to Whisper.
    * `errors` maps a channel_id *or* a video_id to an exception to raise when
      that channel is discovered or that video is fetched, so failure-isolation
      tests can make exactly one Creator or video blow up.
    * `identities` maps a reference (@handle or URL) to the CreatorIdentity it
      resolves to; an unmapped reference raises CreatorResolutionError.
    * `backcatalogue` maps a channel_id to the metadata-only CandidateMetadata
      list its back-catalogue listing returns — the whole catalogue, newest
      first, unfiltered, so the backfill caller (not the fake) honors the cutoff.
    * `details` maps a video_id to the CandidateDetails its lazy per-video detail
      fetch returns (issue #31) — the real description + accurate publish date.

    Calls are recorded (`discovered`, `fetched_video_ids`, `audio_fetched`,
    `resolved`, `listed`, `details_fetched`) so tests can assert what the job did
    and did not touch.
    """

    def __init__(
        self,
        transcript: FetchedTranscript | None = None,
        identities: dict[str, CreatorIdentity] | None = None,
        feeds: dict[str, list[str]] | None = None,
        transcripts: dict[str, FetchedTranscript] | None = None,
        audio: dict[str, FetchedAudio] | None = None,
        errors: dict[str, Exception] | None = None,
        backcatalogue: dict[str, list[CandidateMetadata]] | None = None,
        details: dict[str, CandidateDetails] | None = None,
    ):
        self._transcript = transcript
        self._transcripts = dict(transcripts or {})
        self._audio = dict(audio or {})
        self._feeds = dict(feeds or {})
        self._errors = dict(errors or {})
        self._identities = dict(identities or {})
        self._backcatalogue = dict(backcatalogue or {})
        self._details = dict(details or {})
        self.fetched_video_ids: list[str] = []
        self.audio_fetched: list[str] = []
        self.discovered: list[str] = []
        self.resolved: list[str] = []
        self.listed: list[str] = []
        self.details_fetched: list[str] = []

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

    def list_backcatalogue(self, channel_id: str) -> list[CandidateMetadata]:
        self.listed.append(channel_id)
        if channel_id in self._errors:
            raise self._errors[channel_id]
        return list(self._backcatalogue.get(channel_id, []))

    def fetch_candidate_details(self, video_id: str) -> CandidateDetails:
        self.details_fetched.append(video_id)
        if video_id in self._errors:
            raise self._errors[video_id]
        return self._details[video_id]

    def fetch_transcript(self, video_id: str) -> FetchedTranscript | None:
        self.fetched_video_ids.append(video_id)
        if video_id in self._errors:
            raise self._errors[video_id]
        if video_id in self._transcripts:
            return self._transcripts[video_id]
        # Known only as audio (no captions) -> signal absence for the Whisper path.
        if video_id in self._audio:
            return None
        return self._transcript

    def fetch_audio(self, video_id: str) -> FetchedAudio:
        self.audio_fetched.append(video_id)
        if video_id in self._errors:
            raise self._errors[video_id]
        return self._audio[video_id]


class FakeTranscriber:
    """Fakes the Whisper Transcriber: returns canned segments, records its calls.

    Records the video_id of every audio it transcribed (via the audio's
    provenance), so the fallback test can assert Whisper ran for exactly the
    caption-less videos and was never touched when captions were present.
    """

    def __init__(self, segments: list[TranscriptSegment] | None = None):
        self._segments = segments or [
            TranscriptSegment(text="whisper transcript", start=0.0, duration=1.0)
        ]
        self.transcribed: list[str] = []

    def transcribe(self, audio: FetchedAudio) -> list[TranscriptSegment]:
        self.transcribed.append(audio.provenance.video_id)
        return list(self._segments)


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


class FakeExtractor:
    """Fakes the Extractor port (Part 2): returns a canned Extraction, or raises.

    Drives the admit pipeline without the Claude API. Pass an `Extraction` to
    yield, or an `error` to raise so a failing extraction can be exercised
    (Candidate → `failed`, retryable). Records the video_ids it extracted.
    """

    def __init__(self, extraction: Extraction | None = None, *, error: Exception | None = None):
        self._extraction = extraction if extraction is not None else Extraction()
        self._error = error
        self.extracted: list[str] = []

    def extract(self, transcript: FetchedTranscript) -> Extraction:
        self.extracted.append(transcript.provenance.video_id)
        if self._error is not None:
            raise self._error
        return self._extraction


class FakeEmbedder:
    """Fakes the Embedder port (Part 2): emits controlled 1536-d vectors.

    `vectors` maps a mention's text to its leading coordinates; the rest are
    zero-padded to 1536, so a test can place mentions at chosen cosine distances
    over real pgvector and assert merge-vs-new at the normalization threshold. An
    unmapped text gets a deterministic, distinct, non-zero vector derived from its
    hash, so independent mentions never collide by accident.
    """

    DIMS = 1536

    def __init__(self, vectors: dict[str, list[float]] | None = None):
        self._vectors = {k: list(v) for k, v in (vectors or {}).items()}
        self.embedded: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.embedded.append(text)
        if text in self._vectors:
            return _pad(self._vectors[text], self.DIMS)
        return _hash_vector(text, self.DIMS)


class FakeQueryAnswerer:
    """Fakes the QueryAnswerer port (issue #17): synthesizes from the evidence it's
    given and cites it — or abstains — without the Claude API.

    Defaults to the grounded path: cites *every* retrieved Claim and returns a
    synthesized one-paragraph answer over them, so a test with coverage gets a
    cited answer. Knobs let a test force the other branches:

      * `abstain=True` — the answerer abstains even with evidence.
      * `cite_ids=[...]` — cite exactly these ids (e.g. `[]` to exercise the
        cite-or-abstain backstop; an id never retrieved to exercise grounding).
      * `extra_claim_ids=[...]` — cite the retrieved Claims *plus* these, so a test
        can assert a hallucinated id is dropped while the real ones survive.
      * `text=...` — fix the prose.

    Records each `(question, evidence)` it was asked, so a test can assert what
    retrieval surfaced (e.g. that personal-layer context reached the answerer).
    """

    def __init__(
        self,
        *,
        abstain: bool = False,
        text: str | None = None,
        cite_ids: list[int] | None = None,
        extra_claim_ids: list[int] | None = None,
    ):
        self._abstain = abstain
        self._text = text
        self._cite_ids = cite_ids
        self._extra = list(extra_claim_ids or [])
        self.calls: list[tuple[str, RetrievedEvidence]] = []

    def answer(self, question: str, evidence: RetrievedEvidence) -> GroundedAnswer:
        self.calls.append((question, evidence))
        if self._abstain:
            return GroundedAnswer(text="", cited_claim_ids=[], abstained=True)
        if self._cite_ids is not None:
            ids = list(self._cite_ids)
        else:
            ids = [c.id for c in evidence.claims] + self._extra
        text = self._text if self._text is not None else _synthesize(question, evidence)
        return GroundedAnswer(text=text, cited_claim_ids=ids, abstained=False)


class FakeStanceJudge:
    """Fakes the StanceJudge port (issue #18): returns a configured Stance per pair,
    without the Claude API — so the Impact engine is driven over *real* Concept-
    overlap candidates from a real Postgres.

    `stances` maps an anchor's rendered label (its `text`) to the Stance to return,
    so a test can make exactly one anchor `contradicts` while the rest fall to
    `default` — letting it assert bidirectional triggering, the `unrelated` discard,
    and per-anchor stances deterministically. Records every `(knowledge, anchor)`
    pair it judged, so a test can assert which candidates Concept overlap surfaced.
    """

    def __init__(
        self,
        *,
        default: str = "unrelated",
        stances: dict[str, str] | None = None,
    ):
        self._default = default
        self._stances = dict(stances or {})
        self.calls: list[tuple] = []

    def judge(self, knowledge, anchor) -> str:
        self.calls.append((knowledge, anchor))
        return self._stances.get(anchor.text, self._default)


class FakeConceptProposer:
    """Fakes the ConceptProposer port (issue #39): returns canned candidate terms for
    a Goal, or raises — so the new-Concept suggester is driven over a *real* Postgres
    catalogue without the Claude API.

    Pass `concepts` (the terms to propose for every Goal) or an `error` to raise, so
    the graceful-degrade path — an LLM failure yielding no new suggestions while the
    existing-Concept path keeps working — can be exercised. Records each
    `(title, detail)` it was asked, so a test can assert the Goal's title + detail
    reached the proposer.
    """

    def __init__(
        self,
        concepts: list[str] | None = None,
        *,
        error: Exception | None = None,
    ):
        self._concepts = list(concepts or [])
        self._error = error
        self.calls: list[tuple[str, str | None]] = []

    def propose(self, title: str, detail: str | None) -> list[str]:
        self.calls.append((title, detail))
        if self._error is not None:
            raise self._error
        return list(self._concepts)


def _synthesize(question: str, evidence: RetrievedEvidence) -> str:
    """A canned synthesized answer woven from the retrieved Claims (not a list)."""
    return "Based on your library: " + " ".join(c.text for c in evidence.claims)


def _pad(values: list[float], dims: int) -> list[float]:
    return [float(x) for x in values] + [0.0] * (dims - len(values))


def _hash_vector(text: str, dims: int) -> list[float]:
    """A stable, non-zero unit-ish vector seeded by the text's digest."""
    digest = hashlib.sha256(text.encode()).digest()
    # Spread the digest bytes across the leading coordinates; keep it non-zero so
    # pgvector's cosine ops are well-defined.
    head = [(b / 255.0) + 0.001 for b in digest[:16]]
    return _pad(head, dims)
