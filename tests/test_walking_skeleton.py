"""Deep end-to-end assertions for one video through the daily job.

The slice-1 invariants — full provenance archived, Transcript stored immutably
with timestamps, prose Summary persisted, a one-item Digest sent linking to the
source — still hold, now exercised through the slice-3 daily job: a single
Creator whose RSS feed surfaces one new video. Drives the whole job with the
three ports faked and a real ephemeral Postgres, asserting only on what gets
persisted and on the captured Digest (PRD #1 testing decisions).
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from health_bok.job import run_job
from health_bok.models import (
    CreatorIdentity,
    FetchedTranscript,
    Provenance,
    TranscriptSegment,
)
from health_bok.repository import Repository
from tests.fakes import (
    FakeContentSource,
    FakeDigestSender,
    FakeSummarizer,
    FakeTranscriber,
)

MODEL = "claude-sonnet-4-6"
SUMMARY_TEXT = "This video argues that zone-2 cardio improves mitochondrial density."

CHANNEL_ID = "UC_test_channel"
VIDEO_ID = "abc123XYZ"
PUBLISHED_AT = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)


def make_transcript() -> FetchedTranscript:
    provenance = Provenance(
        video_id=VIDEO_ID,
        title="Zone 2 Cardio Explained",
        channel_id=CHANNEL_ID,
        channel_name="Longevity Lab",
        published_at=PUBLISHED_AT,
    )
    segments = [
        TranscriptSegment(text="Welcome back to the channel.", start=0.0, duration=2.5),
        TranscriptSegment(text="Today we talk about zone 2.", start=2.5, duration=3.0),
    ]
    return FetchedTranscript(provenance=provenance, segments=segments, source="captions")


def seed_creator(repo: Repository) -> None:
    """Put the Creator on the watch list so the daily job discovers it."""
    repo.add_creator(CreatorIdentity(channel_id=CHANNEL_ID, name="Longevity Lab"))
    repo.commit()


def run(repo, content_source, summarizer, digest_sender):
    return run_job(
        content_source=content_source,
        transcriber=FakeTranscriber(),
        summarizer=summarizer,
        digest_sender=digest_sender,
        repo=repo,
        model=MODEL,
    )


def test_walking_skeleton_archives_summarizes_and_sends_digest(conn):
    transcript = make_transcript()
    content_source = FakeContentSource(transcript, feeds={CHANNEL_ID: [VIDEO_ID]})
    summarizer = FakeSummarizer(SUMMARY_TEXT)
    digest_sender = FakeDigestSender()
    repo = Repository(conn)
    seed_creator(repo)

    result = run(repo, content_source, summarizer, digest_sender)

    # --- The job reports it processed the one video and sent one Digest. -----
    assert result.newly_processed == [VIDEO_ID]
    assert result.digest_sent is True
    assert result.digest_item_count == 1
    assert result.failures == []

    # --- Full provenance is archived in Postgres (AC 1). ---------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT v.video_id, v.url, v.title, v.published_at, v.retrieved_at, "
            "v.transcript_source, c.name, c.channel_id "
            "FROM videos v JOIN creators c ON c.id = v.creator_id"
        )
        row = cur.fetchone()
    assert row[0] == VIDEO_ID
    assert row[1] == "https://www.youtube.com/watch?v=abc123XYZ"
    assert row[2] == "Zone 2 Cardio Explained"
    assert row[3] == PUBLISHED_AT
    assert row[4] is not None  # retrieved_at stamped at archive time
    assert row[5] == "captions"
    assert row[6] == "Longevity Lab"
    assert row[7] == CHANNEL_ID

    # --- The Transcript is archived with its timestamps (AC 1). --------------
    segments = repo.load_transcript_segments(VIDEO_ID)
    assert [s.text for s in segments] == [
        "Welcome back to the channel.",
        "Today we talk about zone 2.",
    ]
    assert segments[0].start == 0.0 and segments[1].start == 2.5

    # --- A prose Summary is persisted alongside the Transcript (AC 2). -------
    archived = repo.get_summary(VIDEO_ID)
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


def test_transcript_is_immutable(conn):
    """The archived Transcript cannot be mutated (ADR-0001, AC 7)."""
    repo = Repository(conn)
    repo.archive_transcript(make_transcript(), retrieved_at=datetime.now(timezone.utc))
    repo.commit()

    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE transcripts SET segments = '[]'::jsonb WHERE video_id = %s",
                (VIDEO_ID,),
            )
    conn.rollback()

    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM transcripts WHERE video_id = %s", (VIDEO_ID,))
    conn.rollback()
