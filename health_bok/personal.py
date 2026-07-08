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
from dataclasses import dataclass
from datetime import datetime

from .concepts import ConceptNormalizer
from .models import ConceptMention
from .ports import ConceptProposer, Embedder
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

# Cap on new-Concept suggestions surfaced for a Goal (issue #39). The LLM proposes
# candidate terms from the Goal's title + detail; the genuinely new ones (those that
# don't resolve to an existing Concept) are capped here so the page proposes a
# curatable handful rather than a scattershot. Minting stays owner-confirmed.
NEW_CONCEPT_SUGGEST_LIMIT = 6

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


def suggest_new_goal_concepts(
    goal_id: int,
    *,
    proposer: ConceptProposer,
    normalizer: ConceptNormalizer,
    repo: Repository,
    limit: int = NEW_CONCEPT_SUGGEST_LIMIT,
) -> list[str]:
    """New (not-yet-existing) Concept terms a Goal concerns, for owner-curated
    minting (issue #39).

    The companion to `suggest_goal_concepts`: where that proposes Concepts that
    *already exist*, this proposes ones to **add**. An LLM pass reads the Goal's
    title + detail and proposes candidate terms; each is checked against the existing
    catalogue with the *same* conservative merge/adjudicate logic `ConceptNormalizer`
    uses (`normalizer.match`), and only a term that resolves to *no* existing Concept
    is surfaced — so the system never offers a near-duplicate of a hub it already has
    (a candidate that does resolve is dropped; the existing-Concept suggester covers
    those). Because the check shares the exact decision `resolve` makes, every term
    returned here is one that confirming — an attach through `attach_goal_concept`
    (issue #37) — will actually mint and attach. Nothing is minted here: this is
    read-only, the propose half of suggest-then-confirm, and the owner curates.

    Degrades gracefully: a missing Goal, or an LLM failure, yields an empty list —
    the existing-Concept path (a separate, deterministic call) keeps working and the
    Goal page never breaks. Terms are de-duplicated case-insensitively and capped at
    `limit`. Read-only: owns no transaction.
    """
    goal = repo.get_goal(goal_id)
    if goal is None:
        return []
    try:
        candidates = proposer.propose(goal.title, goal.detail)
    except Exception:
        logger.exception("concept proposer failed for goal %s", goal_id)
        return []

    new_terms: list[str] = []
    seen: set[str] = set()
    for term in candidates:
        term = term.strip()
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        # Conservative, and the same decision the confirm will make: a term that
        # resolves to an existing Concept is not "new" — drop it, no duplicate hub.
        if normalizer.match(ConceptMention(name=term)) is None:
            new_terms.append(term)
        if len(new_terms) >= limit:
            break
    return new_terms


def _goal_text(goal: Goal) -> str:
    """The text a Goal's Concept suggestions are inferred from — its title and
    detail (issue #38), the same words the owner described the Goal with."""
    parts = [goal.title]
    if goal.detail:
        parts.append(goal.detail)
    return "\n".join(p.strip() for p in parts if p and p.strip())


# The cutoff for *auto*-attaching a Goal↔Concept link (ADR-0014) — kept tighter than
# the on-screen suggestion cutoff (`SUGGEST_MAX_DISTANCE = 0.6`): a Concept the Goal's
# text matches closely is linked without the owner, while the 0.5–0.6 band still
# surfaces as an owner-confirmed suggestion on the Goal page. Raised 0.4 → 0.5 on
# 2026-07-08 alongside the hierarchy auto-confirm bump, so goal matching auto-attaches
# more of what the owner was confirming by hand. A wrong auto-link is corrected in
# place (ADR-0010), the safety net.
GOAL_AUTOATTACH_DISTANCE = 0.5


def auto_attach_goal_concepts_for_video(
    video_id: str,
    *,
    embedder: Embedder,
    repo: Repository,
    model: str,
    max_distance: float = GOAL_AUTOATTACH_DISTANCE,
) -> list[tuple[int, int]]:
    """Auto-link a just-admitted video's Concepts to the Goals they closely match (ADR-0014).

    The high-confidence tier of goal-matching, run by the worker after admission: for
    each Goal, embed its title + detail and match against the existing Concept
    embeddings over pgvector (the *same* retrieval `suggest_goal_concepts` uses), then
    attach — via the same `references` edge `attach_goal_concept` asserts — every
    Concept that is (a) within the tight `max_distance`, (b) one this video actually
    touched, and (c) not already linked. So a Goal stays current with the library
    without the owner clicking, while looser matches remain owner-confirmed
    suggestions on the Goal page. Idempotent (the edge unique constraint dedupes) and
    commits only when it attached something. Returns the (goal_id, concept_id) links
    made.
    """
    touched = set(repo.concept_ids_for_video(video_id))
    if not touched:
        return []
    attached: list[tuple[int, int]] = []
    for goal in repo.list_goals():
        text = _goal_text(goal)
        if not text:
            continue
        already = set(repo.concept_ids_for("goal", goal.id))
        embedding = embedder.embed(text)
        # The Goal's close Concept neighbourhood; intersected with this video's
        # Concepts below. A generous scan limit so a touched match is never ranked
        # out (within so tight a cutoff only a handful of Concepts ever qualify).
        nearest = repo.nearest_concepts(
            embedding, model=model, limit=200, max_distance=max_distance
        )
        for concept in nearest:
            if concept.concept_id in touched and concept.concept_id not in already:
                repo.add_edge(
                    "goal", goal.id, "concept", concept.concept_id, "references"
                )
                attached.append((goal.id, concept.concept_id))
    if attached:
        repo.commit()
        logger.info(
            "auto-attached %d Goal-Concept link(s) for %s", len(attached), video_id
        )
    return attached


@dataclass(frozen=True)
class GoalBackfillResult:
    """What a catalogue-wide goal auto-attach pass did (ADR-0014): how many Goals it
    scanned and every (goal_id, concept_id) link it newly attached."""

    goals_scanned: int
    attached: list[tuple[int, int]]


def auto_attach_goal_concepts(
    *,
    embedder: Embedder,
    repo: Repository,
    model: str,
    max_distance: float = GOAL_AUTOATTACH_DISTANCE,
) -> GoalBackfillResult:
    """Back-fill goal-matching across the *whole* catalogue (ADR-0014).

    The one-off / rerunnable companion to `auto_attach_goal_concepts_for_video`: where
    that only considers the Concepts a single just-admitted video touched, this matches
    every Goal against *every* existing Concept within the tight `max_distance` and
    attaches the ones not already linked — the same `references` edge, the same pgvector
    retrieval, the same cutoff. Use it to bring existing Goals current after seeding the
    catalogue or after raising `GOAL_AUTOATTACH_DISTANCE`, since the per-video path only
    ever sees new admissions.

    Idempotent and resumable: already-linked Concepts are skipped and the edge unique
    constraint dedupes, so re-running attaches only what is genuinely new. Commits once
    at the end, and only if it attached something. Returns the pass's counts + links.
    """
    attached: list[tuple[int, int]] = []
    goals = repo.list_goals()
    for goal in goals:
        text = _goal_text(goal)
        if not text:
            continue
        already = set(repo.concept_ids_for("goal", goal.id))
        embedding = embedder.embed(text)
        # A generous scan limit so a close match is never ranked out; within so tight a
        # cutoff only a handful of Concepts ever qualify per Goal anyway.
        nearest = repo.nearest_concepts(
            embedding, model=model, limit=200, max_distance=max_distance
        )
        for concept in nearest:
            if concept.concept_id not in already:
                repo.add_edge(
                    "goal", goal.id, "concept", concept.concept_id, "references"
                )
                attached.append((goal.id, concept.concept_id))
    if attached:
        repo.commit()
        logger.info(
            "goal backfill: auto-attached %d Goal-Concept link(s) across %d Goal(s)",
            len(attached), len(goals),
        )
    return GoalBackfillResult(goals_scanned=len(goals), attached=attached)


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
