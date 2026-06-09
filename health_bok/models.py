"""Domain types passed across the ports and into the store.

Vocabulary follows CONTEXT.md: a *Transcript* is the immutable raw content of a
video Source; a *Summary* is a disposable prose write-up; a *Digest* is the one
daily email bundling new Summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

YOUTUBE_WATCH = "https://www.youtube.com/watch?v="
# YouTube derives every video's thumbnail from its id, so a backfill Candidate can
# show a thumbnail without storing one (issue #15) — it is computed, like `url`.
YOUTUBE_THUMBNAIL = "https://i.ytimg.com/vi/"


def thumbnail_url(video_id: str) -> str:
    """The video's default thumbnail image URL, derived from its id (issue #15)."""
    return f"{YOUTUBE_THUMBNAIL}{video_id}/hqdefault.jpg"


class CreatorResolutionError(RuntimeError):
    """An @handle or URL could not be resolved to a Creator's identity.

    Raised by a ContentSource when a reference names no reachable channel (a typo,
    a deleted channel, or an unsupported URL), so adding a Creator fails loudly
    rather than persisting a half-identified row.
    """

    def __init__(self, reference: str):
        super().__init__(f"could not resolve a Creator from {reference!r}")
        self.reference = reference


@dataclass(frozen=True)
class CreatorIdentity:
    """A Creator's stable identity (CONTEXT.md, PRD #1 user story 4).

    The `channel_id` is YouTube's permanent identifier; the @handle used to add a
    Creator is a mutable alias resolved to this once at add-time (user story 2),
    so daily RSS polling and video attribution key off the stable id, never the
    handle. `name` is the channel's display name at resolution time.
    """

    channel_id: str
    name: str


@dataclass(frozen=True)
class Provenance:
    """Everything that traces a Transcript back to its origin (PRD #1).

    Stored per video so every downstream artifact stays attributable.
    `retrieved_at` is stamped by the archive step, not the source, so it is not
    part of what a ContentSource returns.
    """

    video_id: str
    title: str
    channel_id: str
    channel_name: str
    published_at: datetime

    @property
    def url(self) -> str:
        """Canonical watch URL — the Source's citable link (CONTEXT.md)."""
        return f"{YOUTUBE_WATCH}{self.video_id}"


@dataclass(frozen=True)
class TranscriptSegment:
    """One timestamped span of spoken text.

    Timestamps are retained so later deep-links (`watch?v=ID&t=843s`) back to the
    exact moment are possible (PRD #1, user story 12).
    """

    text: str
    start: float  # seconds from the start of the video
    duration: float  # seconds


@dataclass(frozen=True)
class FetchedTranscript:
    """A Transcript plus its provenance, as returned by a ContentSource.

    `source` records how the raw content was obtained ("captions" vs "whisper")
    so transcript reliability can be judged later (user story 32).
    """

    provenance: Provenance
    segments: list[TranscriptSegment]
    source: str = "captions"

    @property
    def text(self) -> str:
        """Full verbatim text — the segments joined for summarization."""
        return " ".join(segment.text for segment in self.segments)


@dataclass(frozen=True)
class FetchedAudio:
    """A caption-less video's downloaded audio plus provenance (PRD #1, story 10).

    Returned by a ContentSource only when a *new* video has no captions, so the
    daily job can fall back to Whisper transcription (user story 10). Carries the
    same provenance the caption path would have, because the failed caption fetch
    yields none. Only the daily path ever fetches audio — backfill never does
    (user story 29), so this never enters the cheap Creator-add path.
    """

    provenance: Provenance
    data: bytes
    suffix: str  # container/extension yt-dlp produced, e.g. ".m4a" — names the upload


@dataclass(frozen=True)
class CandidateMetadata:
    """A back-catalogue video known only by metadata — a *backfill Candidate*.

    Listed from a Creator's back-catalogue when the Creator is added (issue #7)
    and stored without a Transcript or Summary: a backfill Candidate in the sense
    of CONTEXT.md, known only by title, description, publish date, and URL until
    the owner approves it into the Body of Knowledge (ADR-0004). There is no
    `source` field on purpose — acquiring raw content (captions/Whisper) is
    exactly what backfill does *not* do (PRD #1, user story 29).
    """

    video_id: str
    title: str
    description: str
    published_at: datetime

    @property
    def url(self) -> str:
        """Canonical watch URL — the Candidate's citable link (CONTEXT.md)."""
        return f"{YOUTUBE_WATCH}{self.video_id}"

    @property
    def thumbnail_url(self) -> str:
        """Thumbnail image URL, so a backfill Candidate is judgeable at a glance."""
        return thumbnail_url(self.video_id)


def locator_url(video_url: str, locator_seconds: int) -> str:
    """A deep-link back to the exact moment a Claim/Protocol was asserted.

    The Source's canonical watch URL plus a `&t=NNNs` offset (ADR-0010, PRD #1
    user story 12), so the owner can jump straight to the evidence in context.
    """
    return f"{video_url}&t={locator_seconds}s"


@dataclass(frozen=True)
class ConceptMention:
    """A proposed reference to a Concept, as named by the Extractor.

    The raw mention text (e.g. "apoB", "zone 2 cardio") before normalization —
    the worker resolves it to an existing Concept or mints a new one (ADR-0008).
    `kind` is the Extractor's optional hint (supplement, mechanism…) used only
    when a *new* Concept is created.
    """

    name: str
    kind: str | None = None


@dataclass(frozen=True)
class ExtractedClaim:
    """One load-bearing Claim the Extractor drew from a Transcript (ADR-0010).

    `locator_seconds` grounds the Claim to the moment it was asserted; a Claim the
    Extractor could not ground carries ``None`` and is *dropped* at admit time,
    never smoothed over (ADR-0010 "grounded or dropped"). Scope qualifiers ("in
    mice", "at 5g/day") stay verbatim in `text` (ADR-0002). Sub-kind is a `type`.
    """

    text: str
    locator_seconds: int | None = None
    type: str = "finding"  # mechanism | principle | finding
    concepts: list[ConceptMention] = field(default_factory=list)

    @property
    def is_grounded(self) -> bool:
        return self.locator_seconds is not None


@dataclass(frozen=True)
class ExtractedProtocol:
    """A recommendation the Extractor drew from a Transcript (ADR-0010).

    Only admitted as a Protocol when *structured* — carrying at least one of
    dose/timing/frequency/duration alongside the action. An unstructured one is
    vague advice and is demoted to a Claim at admit time, never stored as a
    Protocol (ADR-0010, CONTEXT.md "Protocol").
    """

    action: str
    locator_seconds: int | None = None
    dose: str | None = None
    timing: str | None = None
    frequency: str | None = None
    duration: str | None = None
    concepts: list[ConceptMention] = field(default_factory=list)

    @property
    def is_structured(self) -> bool:
        """Whether it carries enough structure to be a Protocol, not a Claim."""
        return any((self.dose, self.timing, self.frequency, self.duration))

    @property
    def is_grounded(self) -> bool:
        return self.locator_seconds is not None


@dataclass(frozen=True)
class Extraction:
    """What an Extractor pulled from one Transcript: Claims + Protocols.

    The precision-first yield over a single Source (ADR-0010): the substantive
    assertions a creator actually argues, each grounded and qualifier-preserving,
    plus their proposed Concept mentions. The admit step turns this into persisted
    Claims, Protocols, Concepts, and edges.
    """

    claims: list[ExtractedClaim] = field(default_factory=list)
    protocols: list[ExtractedProtocol] = field(default_factory=list)


@dataclass(frozen=True)
class DigestItem:
    """One video's entry in the Digest: its Summary and a link to the source.

    `webapp_url` deep-links into the Web App, where the owner actually reviews and
    approves the Candidate — the Digest is only the notification that nudges them
    there (ADR-0007). It is optional so the daily job still assembles a Digest
    when no Web App base URL is configured.
    """

    title: str
    url: str
    summary: str
    webapp_url: str | None = None


@dataclass(frozen=True)
class Digest:
    """The daily email bundling new Summaries (CONTEXT.md).

    Sent only when it has at least one item; an empty Digest is never sent
    (PRD #1, user story 19).
    """

    items: list[DigestItem] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.items


# -- Natural-language query: grounded, cited answers (issue #17, ADR-0011) ----
#
# The primary way the owner *explores* the Body of Knowledge now that a visual
# graph is out of v1 scope (ADR-0009). Retrieval gathers the admitted evidence
# (Claims, Protocols) and personal-layer context (Goals, Markers, Decisions) that
# share a Concept with the owner's question; the `QueryAnswerer` port then
# synthesizes an answer grounded *only* in that evidence, citing the specific
# Claims behind it — or abstains. These types cross the port boundary, so they
# live here (with the other port types), keeping `ports` free of the store and its
# driver.


@dataclass(frozen=True)
class EvidenceClaim:
    """An admitted Claim offered to the answerer as citable evidence (ADR-0011).

    Carries everything a Citation needs — the Claim's id, its text and sub-kind,
    and the locator `deep_link` back to the exact moment in its Source — so an
    answer's citations are clickable through to Source + locator with no second
    read. The referenced Concept names ride along so the answerer can see why the
    Claim was retrieved.
    """

    id: int
    text: str
    type: str
    deep_link: str
    source_title: str
    concepts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceProtocol:
    """An admitted Protocol offered as context for an *actionable* answer (ADR-0011).

    Not itself a citation unit (citations are Claims, ADR-0011), but it tells the
    answerer what the owner's creators *recommend* — the "options" a question like
    "what are my options for lowering apoB" is asking for. Carries its structured
    parameters and a locator deep-link like the Claim above.
    """

    id: int
    action: str
    dose: str | None
    timing: str | None
    frequency: str | None
    duration: str | None
    deep_link: str
    source_title: str
    concepts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceGoal:
    """A Goal whose Concepts overlap the question — personal-layer context so the
    answer can speak to what the owner is trying to achieve (CONTEXT.md "Goal")."""

    id: int
    title: str
    detail: str | None
    concepts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvidenceMarker:
    """The owner's latest reading for a referenced Concept — the personal-layer
    context that makes an answer actionable ("given my last apoB reading").
    `out_of_range` is derived from the stored reference range, mirroring the Marker
    browser (CONTEXT.md "Marker"), never a stored flag.
    """

    concept: str
    value: float
    unit: str
    reference_low: float | None
    reference_high: float | None
    measured_at: datetime

    @property
    def out_of_range(self) -> bool:
        if self.reference_low is not None and self.value < self.reference_low:
            return True
        if self.reference_high is not None and self.value > self.reference_high:
            return True
        return False


@dataclass(frozen=True)
class EvidenceDecision:
    """A Decision whose Concepts overlap the question — what the owner is *already
    doing*, so an answer can account for it (CONTEXT.md "Decision")."""

    id: int
    action: str
    dose: str | None
    timing: str | None
    frequency: str | None
    duration: str | None
    note: str | None
    concepts: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievedEvidence:
    """Everything retrieval gathered for one question, handed to the answerer.

    Spans both the Body of Knowledge (Claims, Protocols) and the personal layer
    (Goals, Markers, Decisions), so an answer can be cited *and* actionable
    (ADR-0011). Claims are the citation unit: `has_citable_evidence` is the
    structural gate the query service abstains on — no admitted Claim covers the
    question, so there is nothing to ground an answer in.
    """

    claims: list[EvidenceClaim] = field(default_factory=list)
    protocols: list[EvidenceProtocol] = field(default_factory=list)
    goals: list[EvidenceGoal] = field(default_factory=list)
    markers: list[EvidenceMarker] = field(default_factory=list)
    decisions: list[EvidenceDecision] = field(default_factory=list)

    @property
    def claim_ids(self) -> set[int]:
        return {c.id for c in self.claims}

    @property
    def has_citable_evidence(self) -> bool:
        """Whether any admitted Claim was retrieved — the basis an answer can cite."""
        return bool(self.claims)

    @property
    def is_empty(self) -> bool:
        return not (
            self.claims or self.protocols or self.goals or self.markers or self.decisions
        )


@dataclass(frozen=True)
class GroundedAnswer:
    """What the `QueryAnswerer` port returns: prose grounded in the evidence plus
    the ids of the Claims it cites — or `abstained` when the evidence does not
    actually answer the question (ADR-0011).

    The service resolves `cited_claim_ids` back to full Citations against the
    retrieved evidence and enforces cite-or-abstain, so a hallucinated id can
    never become a citation and a non-abstaining answer always rests on ≥1
    admitted Claim.
    """

    text: str
    cited_claim_ids: list[int] = field(default_factory=list)
    abstained: bool = False


@dataclass(frozen=True)
class Citation:
    """One Claim an Answer rests on, resolved from the retrieved evidence (ADR-0011).

    Clickable through to its Source and locator (the timestamp deep-link for a
    video), so the owner can verify every grounded sentence against the moment it
    was asserted.
    """

    claim_id: int
    text: str
    type: str
    deep_link: str
    source_title: str


@dataclass(frozen=True)
class Answer:
    """The grounded, cited answer the Web App shows (ADR-0011).

    An Answer is *either* an abstention ("nothing in your library covers this",
    `abstained=True`, no citations) *or* synthesized prose resting on ≥1 Citation —
    never ungrounded prose. That cite-or-abstain invariant is enforced by the query
    service, not trusted from the model.
    """

    text: str
    citations: list[Citation] = field(default_factory=list)
    abstained: bool = False
