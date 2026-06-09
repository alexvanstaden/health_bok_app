"""Concept normalization: resolve a proposed mention to a Concept (ADR-0008).

A *Concept* is a normalized, deduplicated hub node (CONTEXT.md): "apoB",
"zone 2 cardio", "rapamycin". The Extractor proposes Concept *mentions* off raw
transcript wording; before they can anchor Claims and Protocols they must be
collapsed onto one canonical Concept each, or the graph fills with near-duplicate
hubs and relatedness-by-shared-Concept stops working.

Normalization is embedding-driven and **conservative** (ADR-0008, ADR-0010):

  * embed the mention,
  * find the nearest existing Concept by cosine distance over pgvector,
  * if it is *clearly* the same Concept (distance ≤ the merge threshold) → reuse it,
  * if it is a near-match (within the adjudication band) → ask the optional LLM
    adjudicator, which only merges when confident,
  * otherwise → mint a new Concept rather than over-merge.

The unsure case creates a new Concept on purpose: a spurious *new* Concept is
cheap to merge later, whereas a wrong *merge* silently corrupts the graph. The
adjudicator is an injected callable (the Claude API in production); when absent —
as in tests — the near-match band conservatively defaults to "new", so
normalization is fully exercised over a real pgvector with controlled vectors and
no network.
"""

from __future__ import annotations

import logging
from typing import Callable

from .models import ConceptMention
from .repository import NearestConcept, Repository

logger = logging.getLogger("health_bok.concepts")

# Cosine distance (pgvector `<=>`, range 0–2). At or below this, two mentions are
# treated as the *same* Concept and merged outright. Deliberately tight so only
# unmistakable matches auto-merge.
DEFAULT_MERGE_DISTANCE = 0.15
# Up to this distance a match is "near" — plausibly the same Concept, but unsure;
# the adjudicator decides, defaulting to a new Concept. Beyond it, always new.
DEFAULT_ADJUDICATE_DISTANCE = 0.30

# Decides a near-match: given the mention and the nearest Concept, return True to
# merge. Conservative by contract — only merge when confident.
Adjudicator = Callable[[ConceptMention, NearestConcept], bool]


class ConceptNormalizer:
    """Resolves Concept mentions to canonical Concept ids over a real pgvector.

    Stateful only in that it writes through the `Repository` (new Concepts and
    their embeddings); resolution within one admit run sees Concepts minted
    earlier in the same run, so two mentions of the same thing in one video
    collapse onto a single Concept.
    """

    def __init__(
        self,
        embedder,
        repo: Repository,
        *,
        model: str,
        merge_distance: float = DEFAULT_MERGE_DISTANCE,
        adjudicate_distance: float = DEFAULT_ADJUDICATE_DISTANCE,
        adjudicator: Adjudicator | None = None,
    ):
        self._embedder = embedder
        self._repo = repo
        self._model = model
        self._merge_distance = merge_distance
        self._adjudicate_distance = adjudicate_distance
        self._adjudicate = adjudicator

    def resolve(self, mention: ConceptMention) -> int:
        """Return the Concept id for `mention`, merging onto or minting a Concept."""
        embedding = self._embedder.embed(mention.name)
        nearest = self._repo.nearest_concept(embedding, model=self._model)

        if nearest is not None and nearest.distance <= self._merge_distance:
            logger.debug(
                "merge %r -> concept %s (d=%.4f)",
                mention.name, nearest.concept_id, nearest.distance,
            )
            return nearest.concept_id

        if (
            nearest is not None
            and nearest.distance <= self._adjudicate_distance
            and self._adjudicate is not None
            and self._adjudicate(mention, nearest)
        ):
            logger.debug(
                "adjudicated merge %r -> concept %s (d=%.4f)",
                mention.name, nearest.concept_id, nearest.distance,
            )
            return nearest.concept_id

        concept_id = self._repo.add_concept(mention.name, kind=mention.kind)
        self._repo.add_embedding("concept", concept_id, embedding, model=self._model)
        logger.debug("new concept %r -> %s", mention.name, concept_id)
        return concept_id
