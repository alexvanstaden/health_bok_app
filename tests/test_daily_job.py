"""Top-seam integration tests for the slice-3 daily detection job (issue #4).

Drives the whole daily job across multiple Creators with the three ports faked
and a real ephemeral Postgres, asserting only on what gets persisted and on the
captured Digest — never on internals (PRD #1 testing decisions). Covers the four
behaviours the slice turns on: RSS detection vs the processed set, idempotent
re-run, the empty day, and per-Creator failure isolation, plus the retriable
failed send.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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

HUBERMAN = CreatorIdentity(channel_id="UC_huberman", name="Huberman Lab")
ATTIA = CreatorIdentity(channel_id="UC_attia", name="Peter Attia MD")

BASE_PUBLISHED = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def _transcript(video_id: str, creator: CreatorIdentity, *, day: int) -> FetchedTranscript:
    provenance = Provenance(
        video_id=video_id,
        title=f"Video {video_id}",
        channel_id=creator.channel_id,
        channel_name=creator.name,
        published_at=BASE_PUBLISHED + timedelta(days=day),
    )
    return FetchedTranscript(
        provenance=provenance,
        segments=[TranscriptSegment(text=f"content of {video_id}", start=0.0, duration=1.0)],
        source="captions",
    )


def _seed(repo: Repository, *creators: CreatorIdentity) -> None:
    for creator in creators:
        repo.add_creator(creator)
    repo.commit()


def _run(repo, content_source, summarizer, digest_sender):
    return run_job(
        content_source=content_source,
        transcriber=FakeTranscriber(),
        summarizer=summarizer,
        digest_sender=digest_sender,
        repo=repo,
        model=MODEL,
    )


def _processed_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM processing_state WHERE summarized_at IS NOT NULL")
        return cur.fetchone()[0]


def test_detects_only_new_videos_and_bundles_them_into_one_digest(conn):
    """New videos across Creators are detected by diff and bundled (AC 1, 2)."""
    repo = Repository(conn)
    _seed(repo, HUBERMAN, ATTIA)

    # "old1" was processed and emailed on a prior run; the feeds also list it
    # again today, so the job must skip it and process only the genuinely new.
    transcripts = {
        "old1": _transcript("old1", HUBERMAN, day=0),
        "new_h": _transcript("new_h", HUBERMAN, day=2),
        "new_a": _transcript("new_a", ATTIA, day=3),
    }
    first_source = FakeContentSource(
        feeds={HUBERMAN.channel_id: ["old1"], ATTIA.channel_id: []},
        transcripts=transcripts,
    )
    _run(repo, first_source, FakeSummarizer("s"), FakeDigestSender())

    # Today both feeds surface new uploads (newest first), Huberman's also still
    # listing the already-processed "old1".
    source = FakeContentSource(
        feeds={HUBERMAN.channel_id: ["new_h", "old1"], ATTIA.channel_id: ["new_a"]},
        transcripts=transcripts,
    )
    summarizer = FakeSummarizer("today's summary")
    digest_sender = FakeDigestSender()

    result = _run(repo, source, summarizer, digest_sender)

    # Only the two new videos are processed; "old1" is skipped, not re-fetched.
    assert set(result.newly_processed) == {"new_h", "new_a"}
    assert source.fetched_video_ids == ["new_h", "new_a"]
    assert "old1" not in source.fetched_video_ids

    # Exactly one Digest, bundling exactly the two new videos, each linking out.
    assert len(digest_sender.sent) == 1
    digest = digest_sender.sent[0]
    assert {item.url for item in digest.items} == {
        "https://www.youtube.com/watch?v=new_h",
        "https://www.youtube.com/watch?v=new_a",
    }
    assert result.digest_item_count == 2
    assert all(item.summary == "today's summary" for item in digest.items)


def test_rerun_same_day_processes_nothing_and_sends_no_second_digest(conn):
    """A repeat run is idempotent: nothing reprocessed, no second email (AC 4)."""
    repo = Repository(conn)
    _seed(repo, HUBERMAN)
    transcripts = {"v1": _transcript("v1", HUBERMAN, day=1)}

    first = FakeContentSource(feeds={HUBERMAN.channel_id: ["v1"]}, transcripts=transcripts)
    first_result = _run(repo, first, FakeSummarizer("s"), FakeDigestSender())
    assert first_result.digest_sent is True

    second_source = FakeContentSource(
        feeds={HUBERMAN.channel_id: ["v1"]}, transcripts=transcripts
    )
    second_summarizer = FakeSummarizer("s")
    second_sender = FakeDigestSender()
    result = _run(repo, second_source, second_summarizer, second_sender)

    assert result.newly_processed == []
    assert result.digest_sent is False
    assert second_source.fetched_video_ids == []  # not re-fetched
    assert second_summarizer.summarized == []  # not re-summarized
    assert second_sender.sent == []  # no second Digest

    # No duplicate rows were written on the repeat run.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM transcripts")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT count(*) FROM summaries")
        assert cur.fetchone()[0] == 1


def test_no_digest_is_sent_on_an_empty_day(conn):
    """Creators exist but no feed has anything new -> no Digest (AC 3)."""
    repo = Repository(conn)
    _seed(repo, HUBERMAN, ATTIA)

    source = FakeContentSource(feeds={HUBERMAN.channel_id: [], ATTIA.channel_id: []})
    summarizer = FakeSummarizer("s")
    digest_sender = FakeDigestSender()

    result = _run(repo, source, summarizer, digest_sender)

    assert result.newly_processed == []
    assert result.digest_sent is False
    assert digest_sender.sent == []
    assert summarizer.summarized == []


def test_one_creators_failure_does_not_abort_the_others(conn):
    """A Creator that errors is isolated; the rest still produce a Digest (AC 6)."""
    repo = Repository(conn)
    _seed(repo, HUBERMAN, ATTIA)
    transcripts = {"good": _transcript("good", ATTIA, day=1)}

    # Huberman's discovery raises; Attia's video must still be processed + sent.
    source = FakeContentSource(
        feeds={ATTIA.channel_id: ["good"]},
        transcripts=transcripts,
        errors={HUBERMAN.channel_id: RuntimeError("feed unreachable")},
    )
    digest_sender = FakeDigestSender()

    result = _run(repo, source, FakeSummarizer("s"), digest_sender)

    assert result.newly_processed == ["good"]
    assert [f.scope for f in result.failures] == [HUBERMAN.channel_id]
    assert result.digest_sent is True
    assert {item.url for item in digest_sender.sent[0].items} == {
        "https://www.youtube.com/watch?v=good"
    }


def test_video_level_transcript_error_is_isolated_within_a_creator(conn):
    """One video erroring doesn't lose its Creator's other new videos (AC 6)."""
    repo = Repository(conn)
    _seed(repo, HUBERMAN)
    transcripts = {"ok": _transcript("ok", HUBERMAN, day=2)}

    source = FakeContentSource(
        feeds={HUBERMAN.channel_id: ["bad", "ok"]},
        transcripts=transcripts,
        errors={"bad": RuntimeError("no transcript")},
    )
    digest_sender = FakeDigestSender()

    result = _run(repo, source, FakeSummarizer("s"), digest_sender)

    assert result.newly_processed == ["ok"]
    assert [f.scope for f in result.failures] == ["bad"]
    # The failed video left nothing half-written.
    assert _processed_count(conn) == 1
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM videos WHERE video_id = 'bad'")
        assert cur.fetchone()[0] == 0


def test_failed_send_is_retried_without_resummarizing(conn):
    """Send state is separate from processing state, so a send retries cheap (AC 5)."""
    repo = Repository(conn)
    _seed(repo, HUBERMAN)
    transcripts = {"v1": _transcript("v1", HUBERMAN, day=1)}

    # First run summarizes the video but the Digest send fails.
    failing_sender = FakeDigestSender(fail_times=1)
    first_summarizer = FakeSummarizer("only summary")
    with pytest.raises(RuntimeError):
        _run(
            repo,
            FakeContentSource(feeds={HUBERMAN.channel_id: ["v1"]}, transcripts=transcripts),
            first_summarizer,
            failing_sender,
        )
    assert first_summarizer.summarized != []  # it did summarize once
    assert failing_sender.sent == []  # but nothing was delivered

    # Second run: the same feed, but the video is already processed, so it is not
    # re-summarized; the unsent Summary is picked up and the Digest now sends.
    retry_source = FakeContentSource(
        feeds={HUBERMAN.channel_id: ["v1"]}, transcripts=transcripts
    )
    retry_summarizer = FakeSummarizer("should not be used")
    retry_sender = FakeDigestSender()
    result = _run(repo, retry_source, retry_summarizer, retry_sender)

    assert result.newly_processed == []  # nothing new processed
    assert retry_summarizer.summarized == []  # not re-summarized (no API spend)
    assert retry_source.fetched_video_ids == []  # not re-fetched
    assert result.digest_sent is True
    assert len(retry_sender.sent) == 1
    item = retry_sender.sent[0].items[0]
    assert item.summary == "only summary"  # the original Summary, re-sent
    assert item.url == "https://www.youtube.com/watch?v=v1"

    # Exactly one Summary exists — the retry did not create a second.
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM summaries")
        assert cur.fetchone()[0] == 1
