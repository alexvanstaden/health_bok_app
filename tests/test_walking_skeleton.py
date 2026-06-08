"""Top-seam integration test for the slice-1 walking skeleton.

Drives the whole daily job end-to-end with the three ports faked and a real
ephemeral Postgres, asserting on what gets persisted and on the captured Digest
— never on internal implementation details (PRD #1 testing decisions). This is
the primary test and establishes the port + fake + real-Postgres convention.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from health_bok.job import run_job
from health_bok.models import FetchedTranscript, Provenance, TranscriptSegment
from health_bok.repository import Repository
from tests.fakes import FakeContentSource, FakeDigestSender, FakeSummarizer

MODEL = "claude-sonnet-4-6"
SUMMARY_TEXT = "This video argues that zone-2 cardio improves mitochondrial density."

PUBLISHED_AT = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)


def make_transcript() -> FetchedTranscript:
    provenance = Provenance(
        video_id="abc123XYZ",
        title="Zone 2 Cardio Explained",
        channel_id="UC_test_channel",
        channel_name="Longevity Lab",
        published_at=PUBLISHED_AT,
    )
    segments = [
        TranscriptSegment(text="Welcome back to the channel.", start=0.0, duration=2.5),
        TranscriptSegment(text="Today we talk about zone 2.", start=2.5, duration=3.0),
    ]
    return FetchedTranscript(provenance=provenance, segments=segments, source="captions")


def run(repo, content_source, summarizer, digest_sender):
    return run_job(
        "abc123XYZ",
        content_source=content_source,
        summarizer=summarizer,
        digest_sender=digest_sender,
        repo=repo,
        model=MODEL,
    )


def test_walking_skeleton_archives_summarizes_and_sends_digest(conn):
    transcript = make_transcript()
    content_source = FakeContentSource(transcript)
    summarizer = FakeSummarizer(SUMMARY_TEXT)
    digest_sender = FakeDigestSender()
    repo = Repository(conn)

    result = run(repo, content_source, summarizer, digest_sender)

    # --- The job reports it processed the one video and sent one Digest. -----
    assert result.newly_processed == ["abc123XYZ"]
    assert result.digest_sent is True
    assert result.digest_item_count == 1

    # --- Full provenance is archived in Postgres (AC 1). ---------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.video_id, v.url, v.title, v.published_at, v.retrieved_at, "
            "v.transcript_source, c.name, c.channel_id "
            "FROM videos v JOIN creators c ON c.id = v.creator_id"
        )
        row = cur.fetchone()
    assert row[0] == "abc123XYZ"
    assert row[1] == "https://www.youtube.com/watch?v=abc123XYZ"
    assert row[2] == "Zone 2 Cardio Explained"
    assert row[3] == PUBLISHED_AT
    assert row[4] is not None  # retrieved_at stamped at archive time
    assert row[5] == "captions"
    assert row[6] == "Longevity Lab"
    assert row[7] == "UC_test_channel"

    # --- The Transcript is archived with its timestamps (AC 1). --------------
    segments = repo.load_transcript_segments("abc123XYZ")
    assert [s.text for s in segments] == [
        "Welcome back to the channel.",
        "Today we talk about zone 2.",
    ]
    assert segments[0].start == 0.0 and segments[1].start == 2.5

    # --- A prose Summary is persisted alongside the Transcript (AC 2). -------
    archived = repo.get_summary("abc123XYZ")
    assert archived is not None
    assert archived.body == SUMMARY_TEXT

    # --- A one-item Digest with the Summary and a link was sent (AC 3). ------
    assert len(digest_sender.sent) == 1
    digest = digest_sender.sent[0]
    assert len(digest.items) == 1
    item = digest.items[0]
    assert item.summary == SUMMARY_TEXT
    assert item.url == "https://www.youtube.com/watch?v=abc123XYZ"
    assert item.title == "Zone 2 Cardio Explained"


def test_rerun_is_idempotent_and_sends_no_second_digest(conn):
    repo = Repository(conn)
    transcript = make_transcript()

    first_source = FakeContentSource(transcript)
    first_summarizer = FakeSummarizer(SUMMARY_TEXT)
    first_sender = FakeDigestSender()
    run(repo, first_source, first_summarizer, first_sender)

    # Re-run the same day with fresh fakes.
    second_source = FakeContentSource(transcript)
    second_summarizer = FakeSummarizer(SUMMARY_TEXT)
    second_sender = FakeDigestSender()
    result = run(repo, second_source, second_summarizer, second_sender)

    # Nothing is re-fetched or re-summarized, and no second Digest goes out.
    assert result.newly_processed == []
    assert result.digest_sent is False
    assert second_source.fetched_video_ids == []
    assert second_summarizer.summarized == []
    assert second_sender.sent == []

    # Exactly one Transcript and one Summary exist — no duplicates.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transcripts")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM summaries")
        assert cur.fetchone()[0] == 1


def test_transcript_is_immutable(conn):
    """The archived Transcript cannot be mutated (ADR-0001, AC 7)."""
    repo = Repository(conn)
    repo.archive_transcript(make_transcript(), retrieved_at=datetime.now(timezone.utc))
    repo.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transcripts SET segments = '[]'::jsonb WHERE video_id = %s",
                ("abc123XYZ",),
            )
    conn.rollback()

    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transcripts WHERE video_id = %s", ("abc123XYZ",))
    conn.rollback()
