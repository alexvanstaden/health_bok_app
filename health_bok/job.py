"""The daily detection job (slice 3).

For every watched Creator, discover the channel's latest videos via its YouTube
RSS feed, diff them against the already-processed set, and drive only the new
videos through the spine: fetch Transcript → archive immutably → summarize →
persist. Every new Summary from the run — plus any summarized-but-unsent Summary
left behind by an earlier failed send — is bundled into a single Digest and
emailed via Resend, but only when there is something to send.

A video becomes "processed" only once its Transcript and Summary are committed
together, so re-running reprocesses nothing (idempotent). "Digest sent" is
tracked separately from "processed", so a failed send retries without
re-summarizing. One Creator's (or one video's) error is isolated: its
uncommitted work is rolled back and the run continues with the rest.

The orchestrator depends only on the port protocols and the repository, so it
imports no third-party SDK and can be driven in tests with fakes (PRD #1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .acquire import acquire_transcript
from .models import Digest, DigestItem
from .ports import ContentSource, DigestSender, Summarizer, Transcriber
from .repository import Repository

logger = logging.getLogger("health_bok.job")


@dataclass(frozen=True)
class RunFailure:
    """One isolated failure during a run — recorded, never fatal to the run."""

    scope: str  # the channel_id or video_id the failure is attributed to
    error: str


@dataclass(frozen=True)
class RunResult:
    """What the run did — for logging and for tests to assert on."""

    newly_processed: list[str] = field(default_factory=list)
    digest_sent: bool = False
    digest_item_count: int = 0
    failures: list[RunFailure] = field(default_factory=list)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _webapp_link(base_url: str, video_id: str) -> str | None:
    """Deep-link to a Candidate in the Web App review queue (ADR-0007).

    Anchors the queue at the specific Candidate row; ``None`` when no Web App base
    URL is configured, so the Digest degrades to source links alone.
    """
    if not base_url:
        return None
    return f"{base_url}/#candidate-{video_id}"


def run_job(
    *,
    content_source: ContentSource,
    transcriber: Transcriber,
    summarizer: Summarizer,
    digest_sender: DigestSender | None,
    repo: Repository,
    model: str,
    send_digest: bool = True,
    webapp_base_url: str = "",
    now=_utcnow,
) -> RunResult:
    """Run the daily detection pipeline across every watched Creator.

    Detects each Creator's new uploads by diffing its RSS feed against the
    already-processed set, processes only the new videos, and — unless
    `send_digest` is off — sends one Digest bundling everything not yet emailed
    (or no Digest at all on an empty day).

    The Digest is only a notification (ADR-0007): with `send_digest` off the
    pipeline still archives and summarizes, leaving the Summaries unsent so a
    later run with email re-enabled picks them up; the system stays fully usable
    with email switched off. When a `webapp_base_url` is given, each Digest item
    deep-links into the Web App's review queue, where approval actually happens.
    """
    processed = repo.processed_video_ids()
    newly_processed: list[str] = []
    failures: list[RunFailure] = []

    for creator in repo.list_creators():
        try:
            discovered = content_source.discover_videos(creator.channel_id)
        except Exception as exc:  # one Creator's discovery failure is isolated
            repo.rollback()
            logger.warning("discovery failed for %s: %s", creator.channel_id, exc)
            failures.append(RunFailure(scope=creator.channel_id, error=str(exc)))
            continue

        for video_id in discovered:
            if video_id in processed:
                continue  # already processed on an earlier run — skip (idempotent)
            try:
                _process_video(
                    video_id,
                    content_source=content_source,
                    transcriber=transcriber,
                    summarizer=summarizer,
                    repo=repo,
                    model=model,
                    now=now,
                )
            except Exception as exc:  # one video's failure must not abort the run
                repo.rollback()
                logger.warning("processing failed for %s: %s", video_id, exc)
                failures.append(RunFailure(scope=video_id, error=str(exc)))
                continue
            processed.add(video_id)
            newly_processed.append(video_id)

    if not send_digest:
        # Email is off (ADR-0007). Everything is archived and summarized; the
        # Summaries stay unsent for a later run, and the system is fully usable
        # through the Web App regardless.
        return RunResult(
            newly_processed=newly_processed, digest_sent=False, failures=failures
        )

    # Bundle every summarized-but-unsent Summary — the run's new ones plus any
    # left unsent by an earlier failed send — into a single Digest.
    pending = repo.unsent_summaries()
    digest = Digest(
        items=[
            DigestItem(
                title=s.title,
                url=s.url,
                summary=s.body,
                webapp_url=_webapp_link(webapp_base_url, s.video_id),
            )
            for s in pending
        ]
    )
    if digest.is_empty:
        # Nothing new and nothing pending -> no Digest is sent (user story 19).
        return RunResult(
            newly_processed=newly_processed, digest_sent=False, failures=failures
        )

    # If the send raises, the Summaries stay unmarked, so a later run retries the
    # send without re-summarizing (user story 24).
    digest_sender.send(digest)
    repo.mark_digest_sent([s.video_id for s in pending], sent_at=now())
    repo.commit()

    return RunResult(
        newly_processed=newly_processed,
        digest_sent=True,
        digest_item_count=len(pending),
        failures=failures,
    )


def _process_video(
    video_id: str,
    *,
    content_source: ContentSource,
    transcriber: Transcriber,
    summarizer: Summarizer,
    repo: Repository,
    model: str,
    now,
) -> None:
    """Drive one new video through the spine, committing it atomically.

    The Transcript and Summary commit together, so a video is durably
    "processed" only as a unit — a crash mid-way leaves nothing half-done, and a
    failed send later leaves the video processed but unsent (user stories 22, 24).
    """
    fetched = acquire_transcript(
        video_id, content_source=content_source, transcriber=transcriber
    )
    repo.archive_transcript(fetched, retrieved_at=now())
    summary = summarizer.summarize(fetched)
    repo.save_summary(video_id, summary, model=model, summarized_at=now())
    repo.commit()
