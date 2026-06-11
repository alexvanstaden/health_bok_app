"""The personal layer: record Goals, Markers, Decisions and link them (issue #16).

The owner-specific layer of what the owner *wants, measures, and does* (CONTEXT.md
"Personal Layer"), recorded through guided forms and linked to the impersonal
evidence layer by Concept overlap. These are the owner-driven writes the Web App
invokes:

  * **record a Goal** — a stable intention or risk, plus the Concepts it concerns.
  * **record a Marker reading** — an append-only dated snapshot referencing a
    Concept; never an overwrite (the database enforces immutability).
  * **record a Decision** — a time-bound adoption carrying its *own* actual
    parameters, distinct from the Protocol it implements so deviation is
    first-class. Adopting a Protocol pre-fills the Decision and asserts the
    `implements` link, inheriting the Protocol's Concepts so the suggester has
    overlap to work with immediately.
  * **link a Decision** — confirm (or detach) a connection to a Protocol it
    implements, a Goal it serves, a Marker that motivated it, or a Claim that
    supports it. The suggestions come from Concept overlap (ADR-0008); the owner
    confirms each one.

Concept mentions on a Goal/Marker/Decision are resolved through the **same**
`ConceptNormalizer` (and `Embedder`) the admit pipeline uses (Slice 8), so the
personal layer and the Body of Knowledge share one canonical set of Concepts and
overlap is meaningful. Each function owns its transaction (it commits on success,
rolls back a no-op). Like the rest of the domain it depends only on the repository
and the ports, so it is driven in tests against a real Postgres with a fake
Embedder.
"""

from __future__ import annotations

import logging
from datetime import datetime

from .concepts import ConceptNormalizer
from .models import ConceptMention
from .ports import Embedder
from .repository import Goal, NearestConcept, Repository, SuggestedLink

logger = logging.getLogger("health_bok.personal")

# Suggestion knobs for existing-Concept suggestions on a Goal (issue #38). The
# Goal's text is embedded and matched against the existing Concept embeddings over
# pgvector; only Concepts within this cosine distance are proposed, so a Goal whose
# text matches nothing in the catalogue yields no suggestions rather than latching
# onto the merely least-distant Concept. Conservative by design (ADR-0008): this
# path only ever proposes Concepts that already exist — no minting, no LLM.
SUGGEST_MAX_DISTANCE = 0.6
SUGGEST_LIMIT = 8

# How each link target maps onto an `edges` row. `out` means the Decision is the
# edge's source (decision → protocol/goal/marker); `in` means it is the
# destination (a Claim points at the Decision it supports). The edge `kind`s were
# reserved when `edges` was created (ADR-0008), so no schema change is needed.
_LINKS: dict[str, tuple[str, str]] = {
    "protocol": ("implements", "out"),
    "goal": ("serves", "out"),
    "marker": ("motivated_by", "out"),
    "claim": ("supports", "in"),
}


class UnknownLinkTarget(ValueError):
    """A Decision cannot be linked to the given target type (issue #16)."""


# -- Goals ------------------------------------------------------------------


def record_goal(
    *,
    title: str,
    detail: str | None,
    concepts: list[str],
    normalizer: ConceptNormalizer,
    repo: Repository,
) -> int:
    """Record a Goal and the Concepts it concerns (CONTEXT.md "Goal")."""
    goal_id = repo.add_goal(title=title, detail=detail)
    _link_concepts("goal", goal_id, concepts, normalizer, repo)
    repo.commit()
    logger.info("recorded goal %s %r", goal_id, title)
    return goal_id


def delete_goal(goal_id: int, *, repo: Repository) -> bool:
    """Delete a Goal and its dangling edges (issue #16). ``False`` if it's gone."""
    deleted = repo.delete_goal(goal_id)
    if deleted:
        repo.commit()
        logger.info("deleted goal %s", goal_id)
    else:
        repo.rollback()
    return deleted


def attach_goal_concept(
    goal_id: int, *, name: str, normalizer: ConceptNormalizer, repo: Repository
) -> bool:
    """Attach a Concept to a Goal by normalized term (issue #37).

    The owner picks a Concept from the catalogue or types a term that isn't in it;
    either way the term is resolved through the *same* `ConceptNormalizer` the
    create form and admit pipeline use, so an existing Concept is reused and a
    genuinely new one minted — the personal layer and the Body of Knowledge keep one
    canonical Concept set (CONTEXT.md "Concept"). Asserts a `references` edge from
    the Goal to the Concept, idempotently: re-adding an already-attached Concept does
    not duplicate the edge (the `edges` unique constraint dedupes). Returns ``False``
    if the Goal is gone; raises `ValueError` for an empty term.
    """
    name = name.strip()
    if not name:
        repo.rollback()
        raise ValueError("a Concept name is required")
    if repo.get_goal(goal_id) is None:
        repo.rollback()
        return False
    concept_id = normalizer.resolve(ConceptMention(name=name))
    repo.add_edge("goal", goal_id, "concept", concept_id, "references")
    repo.commit()
    logger.info("attached concept %s to goal %s", concept_id, goal_id)
    return True


def detach_goal_concept(goal_id: int, *, concept_id: int, repo: Repository) -> bool:
    """Detach a Concept from a Goal (issue #37). ``False`` if it wasn't attached.

    Removing the last Concept leaves the Goal valid — an empty Concept set is
    allowed (CONTEXT.md "Goal"; an unmet Goal is still a Goal).
    """
    removed = repo.remove_edge("goal", goal_id, "concept", concept_id, "references")
    if removed:
        repo.commit()
        logger.info("detached concept %s from goal %s", concept_id, goal_id)
    else:
        repo.rollback()
    return removed


def suggest_goal_concepts(
    goal_id: int,
    *,
    embedder: Embedder,
    repo: Repository,
    model: str,
    limit: int = SUGGEST_LIMIT,
    max_distance: float = SUGGEST_MAX_DISTANCE,
) -> list[NearestConcept]:
    """Existing Concepts a Goal most likely concerns, for one-click attach (#38).

    The suggest half of suggest-then-confirm on the deterministic path: the Goal's
    title + detail are embedded and matched against the existing Concept embeddings
    over pgvector (`nearest_concepts`), the *same* retrieval normalization and the
    Impact engine use (ADR-0008). So every suggestion resolves to a Concept that
    *already exists* — none is minted and the LLM is never called. Concepts already
    attached to the Goal are excluded, so confirming a suggestion (through
    `attach_goal_concept`, issue #37) drops it from the next pass. A Goal whose text
    matches nothing within `max_distance` — or one with no text — yields an empty
    list, not an error. Read-only: owns no transaction.
    """
    goal = repo.get_goal(goal_id)
    if goal is None:
        return []
    text = _goal_text(goal)
    if not text:
        return []
    embedding = embedder.embed(text)
    attached = set(repo.concept_ids_for("goal", goal_id))
    # Over-fetch by the attached count so excluding them can never starve the list.
    nearest = repo.nearest_concepts(
        embedding, model=model, limit=limit + len(attached), max_distance=max_distance
    )
    suggestions = [c for c in nearest if c.concept_id not in attached]
    return suggestions[:limit]


def _goal_text(goal: Goal) -> str:
    """The text a Goal's Concept suggestions are inferred from — its title and
    detail (issue #38), the same words the owner described the Goal with."""
    parts = [goal.title]
    if goal.detail:
        parts.append(goal.detail)
    return "\n".join(p.strip() for p in parts if p and p.strip())


# -- Markers ----------------------------------------------------------------


def record_marker(
    *,
    concept: str,
    value: float,
    unit: str,
    reference_low: float | None,
    reference_high: float | None,
    measured_at: datetime,
    normalizer: ConceptNormalizer,
    repo: Repository,
) -> int:
    """Record one Marker reading referencing a Concept (CONTEXT.md "Marker").

    Append-only: every reading is a new dated snapshot, never an overwrite, so a
    Concept's history is a true time-series. The Concept mention is normalized to a
    canonical Concept just like a Claim's, so a reading shares Concepts with the
    evidence layer.
    """
    concept_id = normalizer.resolve(ConceptMention(name=concept.strip()))
    reading_id = repo.add_marker_reading(
        concept_id=concept_id,
        value=value,
        unit=unit,
        reference_low=reference_low,
        reference_high=reference_high,
        measured_at=measured_at,
    )
    repo.commit()
    logger.info("recorded marker reading %s for concept %s", reading_id, concept_id)
    return reading_id


# -- Decisions --------------------------------------------------------------


def record_decision(
    *,
    action: str,
    dose: str | None,
    timing: str | None,
    frequency: str | None,
    duration: str | None,
    started_at: datetime,
    ended_at: datetime | None,
    note: str | None,
    concepts: list[str],
    implements_protocol_id: int | None,
    normalizer: ConceptNormalizer,
    repo: Repository,
) -> int:
    """Record a Decision with its own actual parameters (CONTEXT.md "Decision").

    When `implements_protocol_id` is given — the "adopt a Protocol" path — the
    `implements` edge is asserted and the Protocol's Concepts are inherited as the
    Decision's references, so Concept-overlap suggestions are immediately useful.
    Any Concepts the owner typed are added on top (deduped).
    """
    decision_id = repo.add_decision(
        action=action,
        dose=dose,
        timing=timing,
        frequency=frequency,
        duration=duration,
        started_at=started_at,
        ended_at=ended_at,
        note=note,
    )
    seen: set[int] = set()
    if implements_protocol_id is not None:
        repo.add_edge(
            "decision", decision_id, "protocol", implements_protocol_id, "implements"
        )
        for concept_id in repo.concept_ids_for("protocol", implements_protocol_id):
            repo.add_edge("decision", decision_id, "concept", concept_id, "references")
            seen.add(concept_id)
    for name in concepts:
        name = name.strip()
        if not name:
            continue
        concept_id = normalizer.resolve(ConceptMention(name=name))
        if concept_id not in seen:
            repo.add_edge("decision", decision_id, "concept", concept_id, "references")
            seen.add(concept_id)
    repo.commit()
    logger.info("recorded decision %s %r", decision_id, action)
    return decision_id


def link_decision(
    decision_id: int, *, target_type: str, target_id: int, repo: Repository
) -> bool:
    """Confirm a link from a Decision to a Protocol/Goal/Marker/Claim (issue #16).

    The confirm half of suggest-then-confirm: asserts the matching edge. Idempotent
    (the `edges` unique constraint dedupes a re-confirm). Returns ``False`` if the
    Decision is gone; raises `UnknownLinkTarget` for a target type that cannot link
    to a Decision.
    """
    kind, direction = _edge_for(target_type)
    if repo.get_decision(decision_id) is None:
        repo.rollback()
        return False
    _assert_link(repo, decision_id, target_type, target_id, kind, direction)
    repo.commit()
    logger.info("linked decision %s -> %s %s (%s)", decision_id, target_type, target_id, kind)
    return True


def unlink_decision(
    decision_id: int, *, target_type: str, target_id: int, repo: Repository
) -> bool:
    """Detach a previously-confirmed link from a Decision. ``False`` if absent."""
    kind, direction = _edge_for(target_type)
    if direction == "out":
        removed = repo.remove_edge("decision", decision_id, target_type, target_id, kind)
    else:
        removed = repo.remove_edge(target_type, target_id, "decision", decision_id, kind)
    if removed:
        repo.commit()
        logger.info("unlinked decision %s -/-> %s %s", decision_id, target_type, target_id)
    else:
        repo.rollback()
    return removed


def delete_decision(decision_id: int, *, repo: Repository) -> bool:
    """Delete a Decision and its dangling edges (issue #16). ``False`` if it's gone."""
    deleted = repo.delete_decision(decision_id)
    if deleted:
        repo.commit()
        logger.info("deleted decision %s", decision_id)
    else:
        repo.rollback()
    return deleted


def suggest_decision_links(decision_id: int, *, repo: Repository) -> list[SuggestedLink]:
    """The Protocols, Claims, and Goals relevant to a Decision by Concept overlap.

    A read-only pass over the graph (no transaction to own) — the suggest half of
    suggest-then-confirm; the Web App renders each as confirmable.
    """
    return repo.decision_link_suggestions(decision_id)


# -- helpers ----------------------------------------------------------------


def _edge_for(target_type: str) -> tuple[str, str]:
    try:
        return _LINKS[target_type]
    except KeyError:
        raise UnknownLinkTarget(
            f"cannot link a {target_type!r} to a Decision"
        ) from None


def _assert_link(
    repo: Repository,
    decision_id: int,
    target_type: str,
    target_id: int,
    kind: str,
    direction: str,
) -> None:
    if direction == "out":
        repo.add_edge("decision", decision_id, target_type, target_id, kind)
    else:  # a Claim points *at* the Decision it supports
        repo.add_edge(target_type, target_id, "decision", decision_id, kind)


def _link_concepts(
    src_type: str,
    src_id: int,
    names: list[str],
    normalizer: ConceptNormalizer,
    repo: Repository,
) -> None:
    """Normalize each Concept mention and assert a `references` edge to it."""
    seen: set[int] = set()
    for name in names:
        name = name.strip()
        if not name:
            continue
        concept_id = normalizer.resolve(ConceptMention(name=name))
        if concept_id not in seen:
            repo.add_edge(src_type, src_id, "concept", concept_id, "references")
            seen.add(concept_id)
