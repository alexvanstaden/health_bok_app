"""Map-reduce summarization for long Transcripts (issue #6).

Some Sources are multi-hour podcasts whose Transcripts are too long to summarize
faithfully in one pass. This module wraps a single-pass `Summarizer` so the daily
job stays length-agnostic: short Transcripts take the unchanged single-pass path,
while long ones are split into sections, each section summarized, and those
section Summaries reduced into one final prose Summary.

The orchestration depends only on the `Summarizer` port (CONTEXT.md), not on any
LLM SDK — the inner summarizer is `ChatSummarizer` in production and a fake in
tests, so the chunk/reduce logic is exercised without a network (PRD #1 testing
decisions). Length is measured in characters of the Transcript text, a cheap
deterministic proxy that needs no tokenizer.
"""

from __future__ import annotations

from .models import FetchedTranscript, TranscriptSegment
from .ports import Summarizer


class MapReduceSummarizer:
    """A `Summarizer` that map-reduces long Transcripts over a length threshold.

    Wraps an inner single-pass `Summarizer`. A Transcript whose text is at or
    under `max_chars` is summarized in one pass, exactly as before. A longer one
    is chunked on segment boundaries into pieces of at most `chunk_chars`, each
    chunk is summarized into a section Summary, and those section Summaries are
    themselves summarized — through the very same inner `Summarizer` — into the
    final Summary. Transcript length therefore never breaks the pipeline.
    """

    def __init__(self, inner: Summarizer, *, max_chars: int, chunk_chars: int):
        if chunk_chars <= 0 or max_chars <= 0:
            raise ValueError("max_chars and chunk_chars must be positive")
        self._inner = inner
        self._max_chars = max_chars
        self._chunk_chars = chunk_chars

    def summarize(self, transcript: FetchedTranscript) -> str:
        """Return a prose Summary, single-pass or map-reduced by length."""
        if len(transcript.text) <= self._max_chars:
            return self._inner.summarize(transcript)

        chunks = _chunk(transcript, self._chunk_chars)
        if len(chunks) <= 1:
            # One giant segment (or a threshold below the chunk size) can leave a
            # single chunk; there is nothing to reduce, so summarize it directly.
            return self._inner.summarize(transcript)

        section_summaries = [self._inner.summarize(chunk) for chunk in chunks]
        return self._inner.summarize(_reduce_transcript(transcript, section_summaries))


def _chunk(transcript: FetchedTranscript, chunk_chars: int) -> list[FetchedTranscript]:
    """Split a Transcript into section Transcripts on segment boundaries.

    Segments are never split — accumulated until the next one would push the
    section past `chunk_chars`, then a new section starts. A lone segment longer
    than `chunk_chars` becomes its own section rather than being dropped, so no
    spoken content is ever lost (issue #6: length must never break the pipeline).
    Each section carries the original provenance and source: it is the same video.
    """
    sections: list[list[TranscriptSegment]] = []
    current: list[TranscriptSegment] = []
    current_len = 0
    for segment in transcript.segments:
        # Joined text uses a single space between segments (FetchedTranscript.text).
        added = len(segment.text) + (1 if current else 0)
        if current and current_len + added > chunk_chars:
            sections.append(current)
            current, current_len = [], 0
            added = len(segment.text)
        current.append(segment)
        current_len += added
    if current:
        sections.append(current)
    return [_with_segments(transcript, segs) for segs in sections]


def _reduce_transcript(
    original: FetchedTranscript, section_summaries: list[str]
) -> FetchedTranscript:
    """A synthetic Transcript whose segments are the section Summaries.

    Feeding the section Summaries back through the same `Summarizer` keeps the
    reduce step on the single seam the job already depends on. Each section
    Summary becomes one segment; timestamps are irrelevant to the reduce pass, so
    they are zeroed. Provenance is preserved so the final pass still knows the
    video it is summarizing.
    """
    segments = [
        TranscriptSegment(text=summary, start=0.0, duration=0.0)
        for summary in section_summaries
    ]
    return _with_segments(original, segments)


def _with_segments(
    transcript: FetchedTranscript, segments: list[TranscriptSegment]
) -> FetchedTranscript:
    return FetchedTranscript(
        provenance=transcript.provenance,
        segments=segments,
        source=transcript.source,
    )
