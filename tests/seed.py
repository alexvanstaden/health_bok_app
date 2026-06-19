"""Test helper: seed a processed daily Candidate (Transcript + Summary).

A *daily* Candidate is a video that has already been through the Part-1 pipeline —
archived Transcript, persisted Summary — and now awaits the owner's approval into
the Body of Knowledge. The Part-2 admission tests start from exactly that state,
so this reuses the real repository writes to reach it.
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok.models import (
    FetchedTranscript,
    Provenance,
    TranscriptSegment,
)
from health_bok.repository import Repository

DEFAULT_PUBLISHED_AT = datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc)


def seed_processed_video(
    repo: Repository,
    *,
    video_id: str,
    channel_id: str = "UC_test_channel",
    channel_name: str = "Longevity Lab",
    title: str = "Zone 2 Cardio Explained",
    published_at: datetime = DEFAULT_PUBLISHED_AT,
    segments: list[TranscriptSegment] | None = None,
    summary: str | None = "A prose summary of the video.",
    retrieved_at: datetime | None = None,
) -> None:
    """Archive a Transcript (and optionally a Summary) for `video_id` and commit.

    The default reaches a daily Candidate — Transcript + Summary. Pass `summary=None`
    to skip the summarize step and leave the video without a Summary, as a backfill
    admission does (issue #79).

    `retrieved_at` (the "date added") defaults to now; pass an explicit value to
    seed a deterministic newest-first ordering (the Logs page, issue #33).
    """
    if segments is None:
        segments = [
            TranscriptSegment(text="Today we cover zone 2.", start=0.0, duration=3.0),
            TranscriptSegment(text="And a few protocols.", start=3.0, duration=3.0),
        ]
    fetched = FetchedTranscript(
        provenance=Provenance(
            video_id=video_id,
            title=title,
            channel_id=channel_id,
            channel_name=channel_name,
            published_at=published_at,
        ),
        segments=segments,
        source="captions",
    )
    now = datetime.now(timezone.utc)
    repo.archive_transcript(fetched, retrieved_at=retrieved_at or now)
    if summary is not None:
        repo.save_summary(
            video_id, summary, model="claude-sonnet-4-6", summarized_at=now
        )
    repo.commit()
