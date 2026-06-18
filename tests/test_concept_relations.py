"""Lateral relationships: claim-grounded, derived, self-healing (ADR-0013, slice 1).

Drives `admit_candidate` with a fake `Extractor` returning directed Concept→Concept
triples and a `FakeEmbedder`, over a real Postgres + pgvector, and asserts external
behaviour at the highest seam (ADR-0013 testing decisions):

  * a Claim's triples become `concept_relations` rows, evidenced by that Claim,
  * derivation is idempotent — re-admitting the same video re-asserts, never dups,
  * a triple endpoint and a `references` mention of the same thing collapse onto
    one Concept (shared normalization),
  * deleting the last evidencing Claim self-heals the relationship away (ADR-0005).
"""

from __future__ import annotations

from health_bok.admit import admit_candidate
from health_bok.concepts import ConceptNormalizer
from health_bok.models import (
    ConceptMention,
    ConceptTriple,
    ExtractedClaim,
    Extraction,
)
from health_bok.repository import Repository
from tests.fakes import FakeEmbedder, FakeExtractor
from tests.seed import seed_processed_video

VIDEO_ID = "vid_relations"
EMBED_MODEL = "fake-embed"

# One-hot orthogonal vectors so each distinct mention is unmistakably its own
# Concept; same text -> same vector -> the triple endpoint reuses the Concept the
# `concepts` mention minted (shared normalization).
CONCEPT_VECTORS = {
    "APOE4": [1, 0, 0, 0, 0],
    "Alzheimer's": [0, 1, 0, 0, 0],
    "omega-3": [0, 0, 1, 0, 0],
}


def _normalizer(repo: Repository) -> ConceptNormalizer:
    return ConceptNormalizer(FakeEmbedder(CONCEPT_VECTORS), repo, model=EMBED_MODEL)


def _admit(repo: Repository, extraction: Extraction):
    return admit_candidate(
        VIDEO_ID,
        extractor=FakeExtractor(extraction),
        normalizer=_normalizer(repo),
        repo=repo,
    )


def _extraction_with_triple(predicate: str = "risk_factor_for") -> Extraction:
    return Extraction(
        claims=[
            ExtractedClaim(
                text="APOE4 raises Alzheimer's risk.",
                locator_seconds=60,
                type="finding",
                concepts=[ConceptMention(name="APOE4"), ConceptMention(name="Alzheimer's")],
                triples=[
                    ConceptTriple(
                        subject=ConceptMention(name="APOE4"),
                        predicate=predicate,
                        object=ConceptMention(name="Alzheimer's"),
                    )
                ],
            )
        ]
    )


def test_triples_become_evidenced_relations(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)

    _admit(repo, _extraction_with_triple())
    repo.commit()

    relations = repo.list_concept_relations()
    assert len(relations) == 1
    rel = relations[0]
    assert (rel.src_name, rel.predicate, rel.dst_name) == (
        "APOE4", "risk_factor_for", "Alzheimer's",
    )
    # The relationship is claim-grounded: it points back at the Claim that asserts it.
    [claim] = repo.admitted_claims(VIDEO_ID)
    assert rel.evidence_claim_ids == [claim.id]

    # The triple endpoints reused the Concepts the `references` mentions minted —
    # no near-duplicate hubs (shared normalization).
    names = {c.name for c in repo.list_concepts()}
    assert names == {"APOE4", "Alzheimer's"}


def test_re_admission_is_idempotent(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)

    _admit(repo, _extraction_with_triple())
    repo.commit()
    # Re-running extraction over the same video must re-assert, not duplicate, the
    # relationship (UNIQUE on src/predicate/dst; evidence PK collapses the Claim).
    _admit(repo, _extraction_with_triple())
    repo.commit()

    relations = repo.list_concept_relations()
    assert len(relations) == 1


def test_self_loop_triple_is_dropped(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)

    _admit(
        repo,
        Extraction(
            claims=[
                ExtractedClaim(
                    text="APOE4 associates with APOE4 — a useless self-loop.",
                    locator_seconds=10,
                    concepts=[ConceptMention(name="APOE4")],
                    triples=[
                        ConceptTriple(
                            subject=ConceptMention(name="APOE4"),
                            predicate="associated_with",
                            object=ConceptMention(name="APOE4"),
                        )
                    ],
                )
            ]
        ),
    )
    repo.commit()
    assert repo.list_concept_relations() == []


def test_deleting_last_evidencing_claim_self_heals_the_relation(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)
    _admit(repo, _extraction_with_triple())
    repo.commit()

    [claim] = repo.admitted_claims(VIDEO_ID)
    assert len(repo.list_concept_relations()) == 1

    # Delete the only Claim evidencing the relationship -> it loses its last
    # evidence and is removed, not left asserting a connection no Claim supports.
    assert repo.delete_claim(claim.id) is True
    repo.commit()
    assert repo.list_concept_relations() == []
