"""The background worker that drains the admission queue (ADR-0009).

Approval enqueues a job and returns; this worker does the slow part — extract →
normalize Concepts → admit — without blocking the request. It claims jobs one at a
time with `FOR UPDATE SKIP LOCKED` (so multiple workers are safe), walks the
Candidate `approved → processing → admitted`, and on any failure rolls the
admission back and drives the Candidate to `failed`, leaving it retryable. The
job's outcome (`done`/`failed`) and the Candidate's lifecycle state are both
visible in the Web App.

Depends only on the ports, the `ConceptNormalizer`, and the repository, so a fake
`Extractor`/`Embedder` over a real Postgres exercises the whole drain in tests.
The continuous polling loop lives in the entrypoint (`main.py`); this module is
the testable unit of work.
"""

from __future__ import annotations

import logging

from .admit import admit_candidate
from .concepts import ConceptNormalizer
from .ports import Extractor
from .repository import Repository

logger = logging.getLogger("health_bok.worker")


def process_next_job(
    *, extractor: Extractor, normalizer: ConceptNormalizer, repo: Repository
) -> bool:
    """Claim and run the next queued job. Returns ``False`` when none is queued.

    The claim and the `processing` mark commit together (a tiny window holding the
    job row lock), so the Candidate is visibly processing the instant the job
    leaves the queue. Admission then runs in its own transaction: on success the
    job is `done`; on failure everything it wrote is rolled back and the Candidate
    is driven to `failed` with the error recorded — never half-admitted.
    """
    job = repo.claim_next_job()
    if job is None:
        return False

    repo.set_admission(job.video_id, "processing")
    repo.commit()

    try:
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


def drain(
    *, extractor: Extractor, normalizer: ConceptNormalizer, repo: Repository
) -> int:
    """Process every currently-queued job; return how many were handled."""
    handled = 0
    while process_next_job(extractor=extractor, normalizer=normalizer, repo=repo):
        handled += 1
    return handled
