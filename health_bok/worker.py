"""The background worker that drains the admission queue (ADR-0009).

Approval enqueues a job and returns; this worker does the slow part without
blocking the request. For a daily Candidate that is just extract → normalize
Concepts → admit; for a backfill Candidate it is **transcribe-if-needed first** —
a backfill Candidate is stored metadata-only, so the worker acquires its
Transcript (free captions, else paid Whisper) and archives it before extracting
(issue #15). It claims jobs one at a time with `FOR UPDATE SKIP LOCKED` (so
multiple workers are safe), walks the Candidate `approved → processing →
admitted`, and on any failure rolls the admission back and drives the Candidate to
`failed`, leaving it retryable. The job's outcome (`done`/`failed`) and the
Candidate's lifecycle state are both visible in the Web App.

Depends only on the ports, the `ConceptNormalizer`, and the repository, so fake
`ContentSource`/`Transcriber`/`Extractor`/`Embedder` over a real Postgres
exercise the whole drain in tests. The continuous polling loop lives in the
entrypoint (`main.py`); this module is the testable unit of work.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .acquire import acquire_transcript
from .admit import admit_candidate
from .concepts import ConceptNormalizer
from .ports import ContentSource, Extractor, Transcriber
from .repository import Repository

logger = logging.getLogger("health_bok.worker")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def process_next_job(
    *,
    content_source: ContentSource,
    transcriber: Transcriber,
    extractor: Extractor,
    normalizer: ConceptNormalizer,
    repo: Repository,
) -> bool:
    """Claim and run the next queued job. Returns ``False`` when none is queued.

    The claim and the `processing` mark commit together (a tiny window holding the
    job row lock), so the Candidate is visibly processing the instant the job
    leaves the queue. A backfill Candidate is then transcribed-if-needed and its
    Transcript archived in its own committed step, so a later extraction failure
    never re-transcribes (re-paying Whisper). Admission runs last in its own
    transaction: on success the job is `done`; on failure everything admission
    wrote is rolled back and the Candidate is driven to `failed` with the error
    recorded — never half-admitted, and the archived Transcript survives for retry.
    """
    job = repo.claim_next_job()
    if job is None:
        return False

    repo.set_admission(job.video_id, "processing")
    repo.commit()

    try:
        _ensure_transcript_archived(
            job.video_id,
            content_source=content_source,
            transcriber=transcriber,
            repo=repo,
        )
        admit_candidate(
            job.video_id, extractor=extractor, normalizer=normalizer, repo=repo
        )
        repo.mark_job_done(job.id)
        repo.commit()
    except Exception as exc:  # one failed admission must not poison the worker
        repo.rollback()
        repo.set_admission(job.video_id, "failed", error=str(exc))
        repo.mark_job_failed(job.id, error=str(exc))
        repo.commit()
        logger.warning("admission failed for %s: %s", job.video_id, exc)
    return True


def _ensure_transcript_archived(
    video_id: str,
    *,
    content_source: ContentSource,
    transcriber: Transcriber,
    repo: Repository,
) -> None:
    """Make sure the Candidate has an archived Transcript before extraction.

    A daily Candidate already does, so this is a no-op for it. A backfill Candidate
    does not (it was stored metadata-only, issue #15): acquire one transcribe-if-
    needed — free captions, else Whisper (PRD #1, user stories 9-10, 29) — and
    archive it in its own committed transaction, so the immutable Transcript is
    durable the moment it is acquired and a later extraction failure retries
    extraction alone, never the (paid) transcription.
    """
    if repo.load_fetched_transcript(video_id) is not None:
        return
    fetched = acquire_transcript(
        video_id, content_source=content_source, transcriber=transcriber
    )
    repo.archive_transcript(fetched, retrieved_at=_utcnow())
    repo.commit()


def drain(
    *,
    content_source: ContentSource,
    transcriber: Transcriber,
    extractor: Extractor,
    normalizer: ConceptNormalizer,
    repo: Repository,
) -> int:
    """Process every currently-queued job; return how many were handled."""
    handled = 0
    while process_next_job(
        content_source=content_source,
        transcriber=transcriber,
        extractor=extractor,
        normalizer=normalizer,
        repo=repo,
    ):
        handled += 1
    return handled
