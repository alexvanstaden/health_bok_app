"""De-duplicate the Concept catalogue (ADR-0014): collapse near-identical hubs.

The Extractor mints hyper-specific Concept mentions and the normalizer is
conservative, so the catalogue accretes near-duplicates ("Alzheimer's disease",
"Alzheimer's", "Alzheimer's disease pathology"). This one-off pass walks the whole
catalogue and, for each Concept, finds its nearest other Concept over pgvector:

  * within the *merge* distance (unmistakably the same) → merge outright;
  * within the *adjudication* band (plausibly the same) → ask the optional LLM
    `Adjudicator`, merging only when it is confident;
  * otherwise → leave them separate (a wrong merge silently corrupts the graph,
    while a spurious duplicate is cheap to merge later, ADR-0010).

Merging repoints every reference through `Repository.merge_concepts` and keeps the
lower (older) id as canonical. Idempotent and resumable — a merged-away Concept is
gone from pgvector, so a re-run simply finds fewer duplicates. Depends only on the
Embedder port and the repository, so it runs in tests over a real Postgres +
pgvector with a fake embedder and a scripted adjudicator, no network.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import psycopg

from .concepts import (
    DEFAULT_ADJUDICATE_DISTANCE,
    DEFAULT_MERGE_DISTANCE,
    Adjudicator,
)
from .models import ConceptMention
from .ports import Embedder
from .repository import Repository

logger = logging.getLogger("health_bok.dedup")


@dataclass(frozen=True)
class DedupResult:
    """What a dedup pass did — for logging and test assertions."""

    merged: list[tuple[int, int]] = field(default_factory=list)  # (keep_id, drop_id)
    reviewed_not_merged: list[tuple[int, int]] = field(default_factory=list)
    skipped_cycle: list[tuple[int, int]] = field(default_factory=list)
    concepts_scanned: int = 0


def dedup_catalogue(
    *,
    embedder: Embedder,
    repo: Repository,
    model: str,
    adjudicator: Adjudicator | None = None,
    merge_distance: float = DEFAULT_MERGE_DISTANCE,
    adjudicate_distance: float = DEFAULT_ADJUDICATE_DISTANCE,
) -> DedupResult:
    """Collapse duplicate Concepts across the catalogue, keeping the older id.

    For each Concept, embed its name and take its nearest *other* Concept within the
    adjudication band: merge outright inside the merge distance, else ask the
    adjudicator (when wired) and merge on a confident yes. The lower id survives.
    A merge that would close a `broader-of` cycle is rolled back and reported, never
    fatal. Each merge commits on its own, so an interrupted run keeps its progress.
    """
    merged: list[tuple[int, int]] = []
    reviewed: list[tuple[int, int]] = []
    skipped: list[tuple[int, int]] = []
    concepts = repo.list_concepts()
    gone: set[int] = set()

    for concept in concepts:
        if concept.id in gone:
            continue
        embedding = embedder.embed(concept.name)
        nearest = repo.nearest_concepts(
            embedding, model=model, limit=2, max_distance=adjudicate_distance
        )
        other = next(
            (
                n
                for n in nearest
                if n.concept_id != concept.id and n.concept_id not in gone
            ),
            None,
        )
        if other is None:
            continue

        do_merge = other.distance <= merge_distance
        if not do_merge and adjudicator is not None:
            do_merge = adjudicator(ConceptMention(name=concept.name), other)
        if not do_merge:
            reviewed.append((concept.id, other.concept_id))
            continue

        keep, drop = sorted((concept.id, other.concept_id))
        try:
            did = repo.merge_concepts(keep, drop)
        except psycopg.errors.RaiseException:
            # Repointing a hierarchy edge would close a loop: roll back and skip.
            repo.rollback()
            skipped.append((keep, drop))
            continue
        if did:
            repo.commit()
            merged.append((keep, drop))
            gone.add(drop)
            logger.info("merged concept %s into %s", drop, keep)
        else:
            repo.rollback()

    logger.info(
        "dedup: %d concept(s) scanned, %d merged, %d reviewed-not-merged, "
        "%d skipped (cycle)",
        len(concepts), len(merged), len(reviewed), len(skipped),
    )
    return DedupResult(
        merged=merged,
        reviewed_not_merged=reviewed,
        skipped_cycle=skipped,
        concepts_scanned=len(concepts),
    )
