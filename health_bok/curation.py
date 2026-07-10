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
from collections.abc import Sequence
from dataclasses import dataclass

import psycopg

from .ports import Embedder, HierarchyProposer
from .repository import Repository

logger = logging.getLogger("health_bok.curation")

# Suggestion knobs for `broader-of` parent proposals (ADR-0013). The Concept's name
# is embedded and matched against the existing Concept embeddings over pgvector to
# form the *nearby cluster* the LLM then filters down to genuinely broader parents.
# Conservative cutoff so an isolated Concept yields no scattershot cluster.
BROADER_SUGGEST_MAX_DISTANCE = 0.6
BROADER_SUGGEST_LIMIT = 8
# The two-tier confidence gate for *automatic* roll-up (ADR-0014). A suggestion the
# LLM proposed **and** whose parent sits within this cosine distance is confident
# enough to propose-and-confirm without the owner (auto-organizing the graph); a
# suggestion the LLM proposed but that sits in the looser band
# (this .. `BROADER_SUGGEST_MAX_DISTANCE`) is proposed *unconfirmed*, landing in the
# review queue for one-click confirm/reject. In-place correction is the safety net.
#
# Tuned up over 2026-07-08 (0.35 → 0.5 → 0.6) after the first backfill: the LLM
# proposer had already vetted the queued band, the owner was accepting almost all of
# it, and the tight cutoff left hundreds of good links waiting on manual clicks. At 0.6
# this now meets `BROADER_SUGGEST_MAX_DISTANCE`, so *every* parent the proposer offers
# within embedding range auto-confirms — the proposer's agreement is the real gate and
# distance no longer gates the auto path. The review queue keeps only proposals whose
# stored-embedding distance drifted past 0.6, plus manual ones; in-place correction
# (ADR-0010) is the safety net for a wrong auto-link.
BROADER_AUTOCONFIRM_DISTANCE = 0.6


@dataclass(frozen=True)
class BroaderSuggestion:
    """A proposed broader parent for a Concept, with the cosine distance that
    ranked it — so a caller can tier auto-confirm vs review (ADR-0013/0014).

    Carries `.id`/`.name` (the broader Concept) like a `ConceptRef` so existing
    read paths are unaffected, plus the `.distance` the two-tier gate needs.
    """

    id: int
    name: str
    distance: float


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
) -> list[BroaderSuggestion]:
    """Broader Concepts a Concept could roll up under, for one-click confirm (ADR-0013).

    The propose half of suggest-then-confirm: the Concept's name is embedded and
    matched against the existing Concept embeddings over pgvector (the *same*
    retrieval normalization and query use), forming a nearby cluster the LLM then
    filters down to genuinely broader parents. Only existing Concepts are proposed
    (no minting); self, Concepts already a parent, and any descendant (which would
    close a cycle) are excluded, so confirming a suggestion is always a legal edge.

    Each suggestion carries the cosine distance that ranked its parent, so a caller
    can apply the two-tier confidence gate (ADR-0014): auto-confirm a close parent,
    queue a looser one. The on-screen suggester ignores the distance.

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
    # Keep both the id and the distance for each candidate parent by name, so the
    # tier decision (auto-confirm vs review) has the distance the LLM's pick sat at.
    by_name = {
        n.name: (n.concept_id, n.distance)
        for n in nearest
        if n.concept_id != concept_id
    }
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
    suggestions: list[BroaderSuggestion] = []
    for name in parent_names:
        candidate = by_name.get(name)
        if candidate is None:
            continue
        parent_id, distance = candidate
        if parent_id in existing_parents or parent_id in descendants:
            continue
        suggestions.append(BroaderSuggestion(id=parent_id, name=name, distance=distance))
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


def attach_broader_of(broader_id: int, narrower_id: int, *, repo: Repository) -> bool:
    """Manually attach a broader parent, landing CONFIRMED immediately (issue #87).

    Hierarchy is the one Concept→Concept link the owner curates (ADR-0013), so a hand
    attach skips the two-tier review queue (ADR-0014) — that gate vets the *system's*
    guesses, not the owner's — and is visible to roll-up at once. Returns ``False`` if
    either Concept is gone. The DB cycle-guard rejects an attach that would close a
    loop (it raises; the caller's transaction rolls back). Idempotent: re-attaching an
    existing pair (proposed or confirmed) leaves it confirmed.
    """
    if repo.get_concept(broader_id) is None or repo.get_concept(narrower_id) is None:
        repo.rollback()
        return False
    repo.attach_broader_of(broader_id, narrower_id)
    repo.commit()
    logger.info("attached %s broader-of %s (confirmed)", broader_id, narrower_id)
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


# -- Manual merge: the owner collapses several Concepts onto one (issue #86) ----
#
# De-duplication merges automatically (ADR-0014); this is the same write driven by
# hand. Concepts are the one entity that MAY be merged/normalized (CONTEXT.md
# "Concept"). The owner multi-selects hubs on /concepts, picks the survivor, and may
# rename it in the same step. Every merged-away hub is folded onto the survivor and
# deleted so *nothing is lost*: Claims, Protocols, Markers, Goal references,
# `broader-of` edges, lateral Relationships (with evidence), and Impacts all re-point
# to the survivor. The whole thing is one transaction — a merge that would close a
# `broader-of` cycle fails whole, leaving no half-merged graph.


@dataclass(frozen=True)
class MergeResult:
    """What a manual merge did — the survivor, what folded onto it, and whether it
    was renamed — for logging and test assertions."""

    survivor_id: int
    merged_away: list[int]
    renamed: bool


def merge_concepts(
    survivor_id: int,
    drop_ids: Sequence[int],
    *,
    new_name: str | None = None,
    repo: Repository,
) -> MergeResult | None:
    """Fold every `drop` Concept onto `survivor`, optionally renaming it (issue #86).

    Reuses `Repository.merge_concepts` — the de-dup write that already guarantees
    nothing is lost — once per merged-away hub, then applies the optional rename, all
    in *one* transaction so 3+ concepts collapse atomically. Returns ``None`` (a no-op
    rollback) if the survivor or any drop is missing, or if no distinct drop is given.

    A merge that would close a `broader-of` cycle raises `psycopg.errors.RaiseException`
    from the DB cycle-guard; the whole transaction is rolled back first, so the caller
    (the API endpoint, surfacing a 409) sees no partial state.
    """
    drops = [d for d in dict.fromkeys(drop_ids) if d != survivor_id]
    if not drops:
        repo.rollback()
        return None
    try:
        for drop in drops:
            if not repo.merge_concepts(survivor_id, drop):
                repo.rollback()
                return None
        renamed = False
        if new_name is not None:
            if not repo.rename_concept(survivor_id, new_name):
                repo.rollback()
                return None
            renamed = True
    except psycopg.errors.RaiseException:
        # Repointing a `broader-of` edge would close a loop — fail whole (issue #86).
        repo.rollback()
        raise
    repo.commit()
    logger.info(
        "merged concept(s) %s into %s%s",
        drops, survivor_id, " (renamed)" if renamed else "",
    )
    return MergeResult(survivor_id=survivor_id, merged_away=drops, renamed=renamed)
