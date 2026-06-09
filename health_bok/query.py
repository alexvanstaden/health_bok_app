"""Grounded natural-language query over the owner's library (issue #17, ADR-0011).

The primary way the owner *explores* the Body of Knowledge now that a visual graph
is out of v1 scope (ADR-0009). A free-text question becomes a grounded, cited
answer in three steps, all read-only (no transaction to own):

  1. **retrieve** — embed the question through the same `Embedder` the admit
     pipeline uses, find the nearest Concepts via pgvector within a distance
     cutoff, and traverse `references` edges to the Claims, Protocols, Goals,
     Markers, and Decisions that touch them (ADR-0008). Retrieval spans both the
     Body of Knowledge and the personal layer, so an answer can be actionable.
  2. **answer** — hand that evidence to the `QueryAnswerer` port, which synthesizes
     prose grounded *only* in it and names the Claims it cites.
  3. **ground** — resolve those cited ids back to full Citations against the
     retrieved evidence, enforcing cite-or-abstain here rather than trusting the
     model: a hallucinated citation id can never resolve, and a non-abstaining
     answer that cites nothing is itself treated as an abstention.

Abstention is honest and structural, not a model whim: when no Concept lands
within the cutoff, or no admitted Claim covers the question, the service returns
the canonical "nothing in your library covers this" and never calls the model — it
*can't* confabulate over evidence it doesn't have. Like the rest of the domain it
depends only on the ports and the repository, so it runs in tests with a fake
`QueryAnswerer` over a real Postgres retrieval (PRD #1 testing decisions).
"""

from __future__ import annotations

import logging

from .models import Answer, Citation, RetrievedEvidence
from .ports import Embedder, QueryAnswerer
from .repository import Repository

logger = logging.getLogger("health_bok.query")

# How many nearest Concepts a question retrieves through (the breadth of the
# Concept-overlap traversal). Tunable via QUERY_CONCEPT_LIMIT.
DEFAULT_CONCEPT_LIMIT = 8
# Cosine distance (pgvector `<=>`, range 0–2) beyond which a Concept is *not*
# considered to cover the question. The honesty knob: a question semantically far
# from everything in the library retrieves no Concept and the answer abstains.
# Tunable via QUERY_MAX_DISTANCE.
DEFAULT_MAX_DISTANCE = 0.6
# Per-category cap on retrieved evidence, so the answerer's context stays bounded
# on a large library. Tunable via QUERY_EVIDENCE_LIMIT.
DEFAULT_EVIDENCE_LIMIT = 20

# The one canonical abstention (ADR-0011): "not in your library" is a first-class
# answer, never a confabulation.
ABSTENTION = "Nothing in your library covers this."


def answer_question(
    question: str,
    *,
    embedder: Embedder,
    answerer: QueryAnswerer,
    repo: Repository,
    model: str,
    concept_limit: int = DEFAULT_CONCEPT_LIMIT,
    max_distance: float = DEFAULT_MAX_DISTANCE,
    evidence_limit: int = DEFAULT_EVIDENCE_LIMIT,
) -> Answer:
    """Answer a free-text question, grounded in and cited to the owner's library.

    Returns an abstaining `Answer` — without ever calling the `QueryAnswerer` —
    when the question is empty, when no Concept lands within `max_distance`, or
    when no admitted Claim covers it. Otherwise returns the synthesized answer with
    its Citations resolved from the retrieved evidence; an answer the model marks
    as abstaining, or that cites no retrievable Claim, also abstains (ADR-0011).
    """
    text = question.strip()
    if not text:
        return _abstain()

    embedding = embedder.embed(text)
    concepts = repo.nearest_concepts(
        embedding, model=model, limit=concept_limit, max_distance=max_distance
    )
    if not concepts:
        logger.info("query abstains (no Concept within %.3f): %.60s", max_distance, text)
        return _abstain()

    evidence = repo.retrieve_evidence(
        [c.concept_id for c in concepts], limit=evidence_limit
    )
    if not evidence.has_citable_evidence:
        logger.info("query abstains (no admitted Claim covers it): %.60s", text)
        return _abstain()

    grounded = answerer.answer(text, evidence)
    citations = _resolve_citations(grounded.cited_claim_ids, evidence)
    if grounded.abstained or not citations:
        logger.info("query abstains (answerer): %.60s", text)
        return _abstain()

    logger.info("query answered with %d citation(s): %.60s", len(citations), text)
    return Answer(text=grounded.text.strip(), citations=citations, abstained=False)


def _abstain() -> Answer:
    return Answer(text=ABSTENTION, citations=[], abstained=True)


def _resolve_citations(
    claim_ids: list[int], evidence: RetrievedEvidence
) -> list[Citation]:
    """Resolve the answerer's cited ids to Citations, keeping only retrieved Claims.

    The grounding guard: a cited id that was not in the retrieved evidence is
    silently dropped, so a hallucinated citation can never reach the owner, and the
    Citation's deep-link is always the real one from the store (ADR-0011). Order is
    the answerer's; duplicates collapse.
    """
    by_id = {c.id: c for c in evidence.claims}
    citations: list[Citation] = []
    seen: set[int] = set()
    for claim_id in claim_ids:
        claim = by_id.get(claim_id)
        if claim is None or claim_id in seen:
            continue
        seen.add(claim_id)
        citations.append(
            Citation(
                claim_id=claim.id,
                text=claim.text,
                type=claim.type,
                deep_link=claim.deep_link,
                source_title=claim.source_title,
            )
        )
    return citations
