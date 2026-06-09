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
