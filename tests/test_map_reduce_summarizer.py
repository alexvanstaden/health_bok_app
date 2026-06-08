"""Map-reduce summarization of long Transcripts (issue #6).

A focused unit test of the chunk/reduce orchestration: drive `MapReduceSummarizer`
with a faked single-pass `Summarizer` and assert which path a Transcript takes by
its length. No Postgres and no LLM SDK — the wrapper depends only on the
`Summarizer` port, so the orchestration is exercised in isolation (PRD #1 testing
decisions). The fake returns a distinct, traceable Summary per call so a chunk
pass can be told apart from the reduce pass.
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok.models import FetchedTranscript, Provenance, TranscriptSegment
from health_bok.summarizer import MapReduceSummarizer

MAX_CHARS = 50
CHUNK_CHARS = 40
SEG_LEN = 30  # > 0 and < CHUNK_CHARS, so each segment lands in its own section

PROV = Provenance(
    video_id="vid123",
    title="A Very Long Podcast",
    channel_id="UC_long",
    channel_name="Longevity Lab",
    published_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
)


class RecordingSummarizer:
    """A faked `Summarizer` recording every Transcript it summarizes.

    Returns ``S1``, ``S2``, … in call order so the test can identify the reduce
    pass (its input is the chunk Summaries `S1 S2 …`) and assert the final result
    is the reduce pass's output.
    """

    def __init__(self) -> None:
        self.calls: list[FetchedTranscript] = []

    def summarize(self, transcript: FetchedTranscript) -> str:
        self.calls.append(transcript)
        return f"S{len(self.calls)}"


def _segment(index: int, length: int) -> TranscriptSegment:
    marker = f"seg{index:02d}"
    text = (marker + "-" * length)[:length]
    return TranscriptSegment(text=text, start=float(index), duration=1.0)


def _transcript(segment_count: int, *, seg_len: int = SEG_LEN) -> FetchedTranscript:
    return FetchedTranscript(
        provenance=PROV,
        segments=[_segment(i, seg_len) for i in range(segment_count)],
        source="captions",
    )


def test_short_transcript_takes_the_single_pass_path():
    """At or under the threshold, summarize once on the whole Transcript (AC 1)."""
    inner = RecordingSummarizer()
    summarizer = MapReduceSummarizer(inner, max_chars=MAX_CHARS, chunk_chars=CHUNK_CHARS)
    transcript = _transcript(1)  # 30 chars <= 50
    assert len(transcript.text) <= MAX_CHARS

    result = summarizer.summarize(transcript)

    # Exactly one call, on the original Transcript untouched — no chunking.
    assert len(inner.calls) == 1
    assert inner.calls[0] is transcript
    assert result == "S1"


def test_long_transcript_is_chunked_summarized_and_reduced():
    """Over the threshold: each section summarized, then reduced into one (AC 2)."""
    inner = RecordingSummarizer()
    summarizer = MapReduceSummarizer(inner, max_chars=MAX_CHARS, chunk_chars=CHUNK_CHARS)
    transcript = _transcript(4)  # 4*30 + 3 spaces = 123 chars > 50
    assert len(transcript.text) > MAX_CHARS

    result = summarizer.summarize(transcript)

    # Four section passes (one per segment, since each fills a chunk) plus one
    # reduce pass — the map-reduce path, not a single pass.
    assert len(inner.calls) == 5
    chunk_calls, reduce_call = inner.calls[:-1], inner.calls[-1]
    assert len(chunk_calls) == 4

    # No spoken content is dropped: the chunks together reconstruct the original.
    chunked_segments = [seg for call in chunk_calls for seg in call.segments]
    assert [s.text for s in chunked_segments] == [s.text for s in transcript.segments]
    # Every section is still the same video.
    assert all(call.provenance is PROV for call in chunk_calls)
    assert all(call.source == "captions" for call in chunk_calls)

    # The reduce pass summarizes the section Summaries (S1..S4), and its output is
    # the final Summary the job persists.
    assert [s.text for s in reduce_call.segments] == ["S1", "S2", "S3", "S4"]
    assert reduce_call.text == "S1 S2 S3 S4"
    assert reduce_call.provenance is PROV
    assert result == "S5"


def test_one_oversized_segment_does_not_break_the_pipeline():
    """A single segment longer than the chunk size still summarizes (issue #6).

    It cannot be split into multiple sections, so there is nothing to reduce; the
    pipeline falls back to a single pass rather than failing on transcript length.
    """
    inner = RecordingSummarizer()
    summarizer = MapReduceSummarizer(inner, max_chars=MAX_CHARS, chunk_chars=CHUNK_CHARS)
    transcript = _transcript(1, seg_len=200)  # one 200-char segment > max and chunk
    assert len(transcript.text) > MAX_CHARS

    result = summarizer.summarize(transcript)

    assert len(inner.calls) == 1
    assert inner.calls[0] is transcript
    assert result == "S1"
