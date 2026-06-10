"""Backfill Candidate population (slice 6, issue #7).

When a Creator is added, list its back-catalogue through the `ContentSource` port
and store each past upload as a metadata-only **Candidate** — title, description,
publish date, URL — within a configurable recency cutoff. No Transcript is
fetched, no Summary is generated, and Whisper is never called: a backfill
Candidate stays metadata-only until the owner approves it into the Body of
Knowledge (CONTEXT.md; ADR-0004; PRD #1, user story 29). Approval and the
downstream processing it triggers are a later, out-of-scope concern.

Like the daily job, this depends only on the port protocol and the repository,
so it runs in tests against a faked back-catalogue with a real Postgres.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .config import DEFAULT_BACKFILL_CUTOFF_DAYS
from .ports import ContentSource
from .repository import Repository, StoredCandidate

logger = logging.getLogger("health_bok.backfill")

# The default recency window, as a timedelta — the single source of the number
# is config's day count, so the CLI default and this in-process default agree.
DEFAULT_BACKFILL_CUTOFF = timedelta(days=DEFAULT_BACKFILL_CUTOFF_DAYS)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def backfill_candidates(
    creator_id: int,
    channel_id: str,
    *,
    content_source: ContentSource,
    repo: Repository,
    cutoff: timedelta = DEFAULT_BACKFILL_CUTOFF,
    now=_utcnow,
) -> list[str]:
    """Store metadata-only Candidates for a Creator's recent back-catalogue.

    Lists the channel's whole back-catalogue, keeps only uploads published within
    `cutoff` of now, and persists each as a metadata-only Candidate (idempotent
    on video_id). Returns the video IDs *newly* stored this run — a re-run over an
    already-backfilled catalogue returns an empty list. Does **not** commit — the
    caller owns the transaction boundary, so a Creator and its Candidates land
    atomically.
    """
    threshold = now() - cutoff
    stored: list[str] = []
    for candidate in content_source.list_backcatalogue(channel_id):
        if candidate.published_at < threshold:
            continue  # older than the recency cutoff — skip (issue #7)
        if repo.add_candidate(creator_id, candidate):
            stored.append(candidate.video_id)
    logger.info("backfilled %d new candidate(s) for %s", len(stored), channel_id)
    return stored


def fetch_candidate_details(
    video_id: str,
    *,
    content_source: ContentSource,
    repo: Repository,
) -> StoredCandidate | None:
    """Lazily fetch + persist one backfill Candidate's real description and date (issue #31).

    Lets the owner pull the per-video detail the cheap one-pass listing omitted, on
    demand, for a single Candidate: one per-video extraction obtains the real
    description and the accurate publish date, both are stored on the Candidate, then
    it commits and returns the updated Candidate so the Web App can show them in place.
    Idempotent and safe to re-run — a Candidate that already carries details is updated,
    not duplicated. Returns ``None`` if no Candidate with that video_id exists, so the
    caller can answer 404; the expensive fetch is skipped in that case.
    """
    if repo.get_backfill_candidate(video_id) is None:
        return None
    details = content_source.fetch_candidate_details(video_id)
    repo.update_candidate_details(
        video_id, description=details.description, published_at=details.published_at
    )
    repo.commit()
    return repo.get_backfill_candidate(video_id)
