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

from .ports import Embedder, HierarchyProposer
from .repository import ConceptRef, Repository

logger = logging.getLogger("health_bok.curation")

# Suggestion knobs for `broader-of` parent proposals (ADR-0013). The Concept's name
# is embedded and matched against the existing Concept embeddings over pgvector to
# form the *nearby cluster* the LLM then filters down to genuinely broader parents.
# Conservative cutoff so an isolated Concept yields no scattershot cluster.
BROADER_SUGGEST_MAX_DISTANCE = 0.6
BROADER_SUGGEST_LIMIT = 8


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


# -- Hierarchy: the owner-curated `broader-of` taxonomy (ADR-0013) -------------
#
# Roll-up is the one Concept→Concept link the owner curates (it is judgement, not
# evidence): the system *proposes* broader parents and the owner *confirms* — the
# same suggest-then-confirm shape as Goal-Concept attach/detach. A proposed edge
# stays invisible to roll-up until confirmed, so a wrong guess never silently
# corrupts a subtree (user story 19).


def suggest_broader_of(
    concept_id: int,
    *,
    proposer: HierarchyProposer,
    embedder: Embedder,
    repo: Repository,
    model: str,
    limit: int = BROADER_SUGGEST_LIMIT,
    max_distance: float = BROADER_SUGGEST_MAX_DISTANCE,
) -> list[ConceptRef]:
    """Broader Concepts a Concept could roll up under, for one-click confirm (ADR-0013).

    The propose half of suggest-then-confirm: the Concept's name is embedded and
    matched against the existing Concept embeddings over pgvector (the *same*
    retrieval normalization and query use), forming a nearby cluster the LLM then
    filters down to genuinely broader parents. Only existing Concepts are proposed
    (no minting); self, Concepts already a parent, and any descendant (which would
    close a cycle) are excluded, so confirming a suggestion is always a legal edge.

    Degrades gracefully: a missing Concept, or an LLM failure, yields an empty list.
    Read-only: owns no transaction.
    """
    concept = repo.get_concept(concept_id)
    if concept is None:
        return []
    embedding = embedder.embed(concept.name)
    nearest = repo.nearest_concepts(
        embedding, model=model, limit=limit + 1, max_distance=max_distance
    )
    by_name = {n.name: n.concept_id for n in nearest if n.concept_id != concept_id}
    if not by_name:
        return []
    try:
        parent_names = proposer.propose(concept.name, list(by_name))
    except Exception:
        logger.exception("hierarchy proposer failed for concept %s", concept_id)
        return []

    existing_parents = {p.id for p in repo.broader_parents(concept_id)}
    # A descendant cannot be a parent — it would close a cycle (user story 17).
    descendants = set(repo.descendant_concept_ids(concept_id))
    suggestions: list[ConceptRef] = []
    for name in parent_names:
        parent_id = by_name.get(name)
        if parent_id is None or parent_id in existing_parents or parent_id in descendants:
            continue
        suggestions.append(ConceptRef(id=parent_id, name=name))
        if len(suggestions) >= limit:
            break
    return suggestions


def propose_broader_of(broader_id: int, narrower_id: int, *, repo: Repository) -> bool:
    """Record a proposed `broader-of` edge — a suggestion, invisible to roll-up.

    Returns ``False`` if either Concept is gone. The DB cycle-guard rejects a
    proposal that would close a loop (it raises; the caller's transaction rolls
    back). Idempotent: re-proposing an existing pair is a no-op.
    """
    if repo.get_concept(broader_id) is None or repo.get_concept(narrower_id) is None:
        repo.rollback()
        return False
    repo.propose_broader_of(broader_id, narrower_id)
    repo.commit()
    logger.info("proposed %s broader-of %s", broader_id, narrower_id)
    return True


def confirm_broader_of(broader_id: int, narrower_id: int, *, repo: Repository) -> bool:
    """Confirm a proposed `broader-of` edge, making it visible to roll-up (ADR-0013).

    Returns ``False`` if no such edge was proposed.
    """
    if repo.confirm_broader_of(broader_id, narrower_id):
        repo.commit()
        logger.info("confirmed %s broader-of %s", broader_id, narrower_id)
        return True
    repo.rollback()
    return False


def reject_broader_of(broader_id: int, narrower_id: int, *, repo: Repository) -> bool:
    """Reject (delete) a proposed-or-confirmed `broader-of` edge. ``False`` if absent."""
    if repo.reject_broader_of(broader_id, narrower_id):
        repo.commit()
        logger.info("rejected %s broader-of %s", broader_id, narrower_id)
        return True
    repo.rollback()
    return False
