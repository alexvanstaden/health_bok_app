"""Creator-management service: maintain the watch list of Creators.

The owner adds a Creator by @handle or URL and removes one by its stable
channel_id. Adding resolves the reference to a (channel_id, name) identity
*exactly once*, through the `ContentSource` port, and persists it; the daily job
thereafter keys off the stored channel_id and never re-resolves (PRD #1, user
stories 1-4). YouTube-specific reference parsing lives in the adapter behind the
port, so this layer stays source-agnostic (ADR-0006) and testable with a fake.

Adding a Creator also seeds its back-catalogue as metadata-only **Candidates**
(issue #7): the Creator and its Candidates are written in one transaction, so
the watch list and its backfill land atomically.
"""

from __future__ import annotations

from datetime import timedelta

from .backfill import DEFAULT_BACKFILL_CUTOFF, backfill_candidates
from .models import CreatorIdentity
from .ports import ContentSource
from .repository import Repository


def add_creator(
    reference: str,
    *,
    content_source: ContentSource,
    repo: Repository,
    cutoff: timedelta = DEFAULT_BACKFILL_CUTOFF,
) -> CreatorIdentity:
    """Add a Creator named by an @handle or URL to the watch list.

    Resolves the reference to its stable identity once via the ContentSource,
    persists it, and seeds the Creator's recent back-catalogue as metadata-only
    Candidates within `cutoff` (issue #7) — all in one transaction. Idempotent on
    channel_id: re-adding (even via a different @handle or URL that resolves to
    the same channel) updates the name and tops up Candidates but creates no
    duplicate. Propagates CreatorResolutionError without writing anything if the
    reference can't be resolved.
    """
    identity = content_source.resolve_creator(reference)
    creator_id = repo.add_creator(identity)
    backfill_candidates(
        creator_id,
        identity.channel_id,
        content_source=content_source,
        repo=repo,
        cutoff=cutoff,
    )
    repo.commit()
    return identity


def remove_creator(channel_id: str, *, repo: Repository) -> bool:
    """Remove a Creator from the watch list by its stable channel_id.

    Returns whether a Creator was actually removed. Keyed on the stored
    channel_id (shown by `list`), not the mutable @handle, so removal stays
    reliable even after a channel renames its handle.
    """
    removed = repo.remove_creator(channel_id)
    repo.commit()
    return removed
