"""Domain types passed across the ports and into the store.

Vocabulary follows CONTEXT.md: a *Transcript* is the immutable raw content of a
video Source; a *Summary* is a disposable prose write-up; a *Digest* is the one
daily email bundling new Summaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

YOUTUBE_WATCH = "https://www.youtube.com/watch?v="


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
class DigestItem:
    """One video's entry in the Digest: its Summary and a link to the source."""

    title: str
    url: str
    summary: str


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
