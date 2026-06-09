"""In-place curation of the Body of Knowledge (ADR-0010).

Video-approval is the only gate into the Body of Knowledge; curation continues
*opportunistically in place* afterwards — when the owner is browsing and spots an
extraction error, they fix it there, rather than via a standing review queue
(ADR-0010). These are the owner-driven writes the Web App invokes on an admitted
Claim or Protocol:

  * **edit** — correct a Claim/Protocol's content. The edit is recorded as a
    *protected version*: a later re-extraction supersede (ADR-0005) must not
    silently clobber a hand-corrected entity. The protection flag is set in the
    repository write, so it can never be forgotten here.
  * **delete** — remove a Claim/Protocol and the edges that hang off it, so no
    dangling edge survives.

Each owns its transaction (it commits on success, rolls back a no-op), so a
request resolves durably and returns whether it acted. Like the rest of the
domain this depends only on the repository, so it is driven in tests against a
real Postgres.
"""

from __future__ import annotations

import logging

from .repository import Repository

logger = logging.getLogger("health_bok.curation")


def edit_claim(
    claim_id: int, *, text: str, type: str, locator_seconds: int, repo: Repository
) -> bool:
    """Edit a Claim in place and protect it (ADR-0010). ``False`` if it's gone."""
    edited = repo.update_claim(
        claim_id, text=text, type=type, locator_seconds=locator_seconds
    )
    if edited:
        repo.commit()
        logger.info("edited claim %s (now protected)", claim_id)
    else:
        repo.rollback()
    return edited


def edit_protocol(
    protocol_id: int,
    *,
    action: str,
    dose: str | None,
    timing: str | None,
    frequency: str | None,
    duration: str | None,
    locator_seconds: int,
    repo: Repository,
) -> bool:
    """Edit a Protocol in place and protect it (ADR-0010). ``False`` if it's gone."""
    edited = repo.update_protocol(
        protocol_id,
        action=action,
        dose=dose,
        timing=timing,
        frequency=frequency,
        duration=duration,
        locator_seconds=locator_seconds,
    )
    if edited:
        repo.commit()
        logger.info("edited protocol %s (now protected)", protocol_id)
    else:
        repo.rollback()
    return edited


def delete_claim(claim_id: int, *, repo: Repository) -> bool:
    """Delete a Claim and its dangling edges (issue #14). ``False`` if it's gone."""
    deleted = repo.delete_claim(claim_id)
    if deleted:
        repo.commit()
        logger.info("deleted claim %s", claim_id)
    else:
        repo.rollback()
    return deleted


def delete_protocol(protocol_id: int, *, repo: Repository) -> bool:
    """Delete a Protocol and its dangling edges (issue #14). ``False`` if it's gone."""
    deleted = repo.delete_protocol(protocol_id)
    if deleted:
        repo.commit()
        logger.info("deleted protocol %s", protocol_id)
    else:
        repo.rollback()
    return deleted
