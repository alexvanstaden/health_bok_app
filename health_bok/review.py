"""Candidate review: the owner's video-grain decisions (ADR-0004, ADR-0007).

The only human gate into the Body of Knowledge is video-level approval (ADR-0004,
ADR-0010). These are the owner-driven transitions the Web App invokes:

  * **approve** — enqueue the admission job and return immediately; the worker
    does the slow extract → normalize → admit (ADR-0009). Idempotent: approving a
    Candidate already approved, processing, or admitted enqueues nothing.
  * **reject** — decline the Candidate and drop any queued job, without admitting
    anything.
  * **retry** — re-enqueue a Candidate whose extraction failed.

Each owns its transaction (it commits), so a request resolves durably and
returns. Like the rest of the domain this depends only on the repository, so it
is driven in tests against a real Postgres.
"""

from __future__ import annotations

import logging

from .repository import Repository

logger = logging.getLogger("health_bok.review")

# States from which the owner cannot (re-)approve: the work is already in flight
# or finished, so approving again must not enqueue a duplicate job.
_IN_FLIGHT = frozenset({"approved", "processing", "admitted"})


def approve_candidate(video_id: str, *, repo: Repository) -> bool:
    """Approve a Candidate and enqueue its admission job (ADR-0009).

    Returns whether a job was enqueued — ``False`` if the Candidate is already
    approved, processing, or admitted, so a double-click never queues twice.
    """
    if repo.admission_state(video_id) in _IN_FLIGHT:
        repo.rollback()
        return False
    repo.set_admission(video_id, "approved")
    repo.enqueue_job(video_id)
    repo.commit()
    logger.info("approved %s; admission job enqueued", video_id)
    return True


def reject_candidate(video_id: str, *, repo: Repository) -> bool:
    """Decline a Candidate, dropping any queued job (CONTEXT.md "Candidate").

    Returns whether the Candidate was rejected — ``False`` if it has already been
    admitted, since rejecting admitted knowledge is meaningless (curation is then
    by editing in place, ADR-0010).
    """
    if repo.admission_state(video_id) == "admitted":
        repo.rollback()
        return False
    repo.cancel_queued_jobs(video_id)
    repo.set_admission(video_id, "rejected")
    repo.commit()
    logger.info("rejected %s", video_id)
    return True


def retry_candidate(video_id: str, *, repo: Repository) -> bool:
    """Re-enqueue a Candidate whose extraction failed (ADR-0010).

    Returns whether a retry was enqueued — only a `failed` Candidate is retryable.
    """
    if repo.admission_state(video_id) != "failed":
        repo.rollback()
        return False
    repo.set_admission(video_id, "approved")
    repo.enqueue_job(video_id)
    repo.commit()
    logger.info("retrying %s; admission job re-enqueued", video_id)
    return True
