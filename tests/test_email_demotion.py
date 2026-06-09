"""The Digest is a notification, not the product (ADR-0007).

Two guarantees: the daily pipeline stays fully usable with email switched off
(it still archives + summarizes, just sends nothing), and when email is on each
Digest item deep-links into the Web App review queue alongside the source link.
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok.job import run_job
from health_bok.models import FetchedTranscript, Provenance, TranscriptSegment
from health_bok.repository import Repository
from tests.fakes import (
    FakeContentSource,
    FakeDigestSender,
    FakeSummarizer,
    FakeTranscriber,
)

MODEL = "claude-sonnet-4-6"
CHANNEL_ID = "UC_test_channel"
VIDEO_ID = "abc123XYZ"


def _transcript() -> FetchedTranscript:
    return FetchedTranscript(
        provenance=Provenance(
            video_id=VIDEO_ID,
            title="Zone 2 Cardio Explained",
            channel_id=CHANNEL_ID,
            channel_name="Longevity Lab",
            published_at=datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
        ),
        segments=[TranscriptSegment(text="Zone 2.", start=0.0, duration=2.0)],
        source="captions",
    )


def _seed(repo: Repository) -> FakeContentSource:
    from health_bok.models import CreatorIdentity

    repo.add_creator(CreatorIdentity(channel_id=CHANNEL_ID, name="Longevity Lab"))
    repo.commit()
    return FakeContentSource(_transcript(), feeds={CHANNEL_ID: [VIDEO_ID]})


def test_pipeline_is_fully_usable_with_email_off(conn):
    repo = Repository(conn)
    content_source = _seed(repo)
    sender = FakeDigestSender()

    result = run_job(
        content_source=content_source,
        transcriber=FakeTranscriber(),
        summarizer=FakeSummarizer("summary text"),
        digest_sender=sender,
        repo=repo,
        model=MODEL,
        send_digest=False,  # email switched off
    )

    # The content is still processed and persisted...
    assert result.newly_processed == [VIDEO_ID]
    assert repo.get_summary(VIDEO_ID) is not None
    # ...but nothing is emailed, and the Summary stays unsent for later.
    assert result.digest_sent is False
    assert sender.sent == []


def test_digest_item_deep_links_into_the_web_app(conn):
    repo = Repository(conn)
    content_source = _seed(repo)
    sender = FakeDigestSender()

    run_job(
        content_source=content_source,
        transcriber=FakeTranscriber(),
        summarizer=FakeSummarizer("summary text"),
        digest_sender=sender,
        repo=repo,
        model=MODEL,
        webapp_base_url="https://bok.example.ts.net",
    )

    assert len(sender.sent) == 1
    item = sender.sent[0].items[0]
    assert item.webapp_url == f"https://bok.example.ts.net/#candidate-{VIDEO_ID}"
    assert item.url == "https://www.youtube.com/watch?v=abc123XYZ"  # source still there
