"""Creator-management service: maintain the watch list of Creators.

The owner adds a Creator by @handle or URL and removes one by its stable
channel_id. Adding resolves the reference to a (channel_id, name) identity
*exactly once*, through the `ContentSource` port, and persists it; the daily job
thereafter keys off the stored channel_id and never re-resolves (PRD #1, user
stories 1-4). YouTube-specific reference parsing lives in the adapter behind the
port, so this layer stays source-agnostic (ADR-0006) and testable with a fake.
"""

from __future__ import annotations

from .models import CreatorIdentity
from .ports import ContentSource
from .repository import Repository


def add_creator(
    reference: str, *, content_source: ContentSource, repo: Repository
) -> CreatorIdentity:
    """Add a Creator named by an @handle or URL to the watch list.

    Resolves the reference to its stable identity once via the ContentSource,
    then persists it. Idempotent on channel_id: re-adding (even via a different
    @handle or URL that resolves to the same channel) updates the name but
    creates no duplicate. Propagates CreatorResolutionError without writing
    anything if the reference can't be resolved.
    """
    identity = content_source.resolve_creator(reference)
    repo.add_creator(identity)
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
