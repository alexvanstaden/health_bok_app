"""Admission: turn an approved Candidate into admitted Body-of-Knowledge (ADR-0010).

This is the heart of the "Approve → Extract → See Claims" vertical. Given a video
the owner has approved, it:

  1. reads the archived Transcript back (the worker has already acquired one
     transcribe-if-needed for a backfill Candidate, issue #15; a daily Candidate
     always had one),
  2. runs the `Extractor` over it,
  3. persists each Claim and Protocol with provenance and a locator deep-link,
     applying the extraction contract at the boundary (ADR-0010):
       * **grounded or dropped** — an assertion with no locator is discarded,
       * **structured or demoted** — a "protocol" lacking dose/timing/frequency/
         duration is vague advice and is stored as a Claim, not a Protocol,
       * scope qualifiers ride along verbatim in the Claim text (ADR-0002),
  4. normalizes each proposed Concept mention onto a canonical Concept and links
     it with a `references` edge (ADR-0008),
  5. **auto-admits** — there is no second review gate (ADR-0010).

Like the daily job it depends only on the ports and the repository, so it runs in
tests with a fake `Extractor`/`Embedder` against a real Postgres + pgvector. It
does not commit: the worker owns the transaction, so a mid-admit failure rolls the
whole admission back and the Candidate is driven to `failed`, retryable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .concepts import ConceptNormalizer
from .models import ConceptMention, ConceptTriple, ExtractedProtocol
from .ports import Extractor
from .repository import Repository

logger = logging.getLogger("health_bok.admit")


class AdmissionError(RuntimeError):
    """Admission could not proceed — e.g. the video has no archived Transcript."""


@dataclass(frozen=True)
class AdmissionResult:
    """What an admission produced — for logging and for tests to assert on."""

    video_id: str
    claims_admitted: int
    protocols_admitted: int
    concepts_touched: int


def admit_candidate(
    video_id: str,
    *,
    extractor: Extractor,
    normalizer: ConceptNormalizer,
    repo: Repository,
) -> AdmissionResult:
    """Extract, normalize, and admit an approved Candidate's Claims & Protocols."""
    transcript = repo.load_fetched_transcript(video_id)
    if transcript is None:
        raise AdmissionError(
            f"cannot admit {video_id!r}: no archived Transcript "
            "(the worker acquires one transcribe-if-needed before admitting)"
        )

    extraction = extractor.extract(transcript)
    concepts_touched: set[int] = set()
    claims_admitted = 0
    protocols_admitted = 0

    for claim in extraction.claims:
        if not claim.is_grounded:  # grounded-or-dropped (ADR-0010)
            logger.info("dropping ungroundable claim: %.60s", claim.text)
            continue
        claim_id = repo.add_claim(
            video_id,
            text=claim.text,
            type=claim.type,
            locator_seconds=claim.locator_seconds,
        )
        _link_concepts(
            "claim", claim_id, claim.concepts, normalizer, repo, concepts_touched
        )
        _derive_relations(claim_id, claim.triples, normalizer, repo, concepts_touched)
        claims_admitted += 1

    for protocol in extraction.protocols:
        if not protocol.is_grounded:  # grounded-or-dropped (ADR-0010)
            logger.info("dropping ungroundable protocol: %.60s", protocol.action)
            continue
        if protocol.is_structured:
            protocol_id = repo.add_protocol(
                video_id,
                action=protocol.action,
                dose=protocol.dose,
                timing=protocol.timing,
                frequency=protocol.frequency,
                duration=protocol.duration,
                locator_seconds=protocol.locator_seconds,
            )
            _link_concepts(
                "protocol", protocol_id, protocol.concepts, normalizer, repo,
                concepts_touched,
            )
            protocols_admitted += 1
        else:
            # Vague advice is not a Protocol — it stays a Claim (ADR-0010).
            claim_id = repo.add_claim(
                video_id,
                text=_demoted_text(protocol),
                type="principle",
                locator_seconds=protocol.locator_seconds,
            )
            _link_concepts(
                "claim", claim_id, protocol.concepts, normalizer, repo, concepts_touched
            )
            claims_admitted += 1

    repo.set_admission(video_id, "admitted")
    logger.info(
        "admitted %s: %d claim(s), %d protocol(s), %d concept(s)",
        video_id, claims_admitted, protocols_admitted, len(concepts_touched),
    )
    return AdmissionResult(
        video_id=video_id,
        claims_admitted=claims_admitted,
        protocols_admitted=protocols_admitted,
        concepts_touched=len(concepts_touched),
    )


def _link_concepts(
    src_type: str,
    src_id: int,
    mentions: list[ConceptMention],
    normalizer: ConceptNormalizer,
    repo: Repository,
    touched: set[int],
) -> None:
    """Normalize each mention and assert a `references` edge to its Concept."""
    for mention in mentions:
        concept_id = normalizer.resolve(mention)
        repo.add_edge(src_type, src_id, "concept", concept_id, "references")
        touched.add(concept_id)


def _derive_relations(
    claim_id: int,
    triples: list[ConceptTriple],
    normalizer: ConceptNormalizer,
    repo: Repository,
    touched: set[int],
) -> None:
    """Project a Claim's directed triples into claim-grounded lateral relationships.

    The materialization step (ADR-0013): each (subject, predicate, object) triple
    becomes a `concept_relations` row evidenced by this Claim, its endpoints
    normalized onto canonical Concepts by the *same* `ConceptNormalizer` the flat
    mention list uses — so a triple endpoint and a `references` mention of the same
    thing collapse onto one Concept. A triple whose endpoints normalize to the same
    Concept is dropped (a Concept never relates to itself).
    """
    for triple in triples:
        src_id = normalizer.resolve(triple.subject)
        dst_id = normalizer.resolve(triple.object)
        touched.add(src_id)
        touched.add(dst_id)
        if src_id == dst_id:
            logger.info("dropping self-relation on concept %s", src_id)
            continue
        repo.add_concept_relation(src_id, triple.predicate, dst_id, claim_id=claim_id)


def _demoted_text(protocol: ExtractedProtocol) -> str:
    """Claim text for an unstructured protocol — its action verbatim."""
    return protocol.action
