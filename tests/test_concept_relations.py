"""Lateral relationships: claim-grounded, derived, self-healing (ADR-0013, slice 1).

Drives `admit_candidate` with a fake `Extractor` returning directed Concept→Concept
triples and a `FakeEmbedder`, over a real Postgres + pgvector, and asserts external
behaviour at the highest seam (ADR-0013 testing decisions):

  * a Claim's triples become `concept_relations` rows, evidenced by that Claim,
  * derivation is idempotent — re-admitting the same video re-asserts, never dups,
  * a triple endpoint and a `references` mention of the same thing collapse onto
    one Concept (shared normalization),
  * deleting the last evidencing Claim self-heals the relationship away (ADR-0005),
  * re-extraction supersede re-points evidence onto the new Claim, drops a
    relationship whose triple vanished, and keeps one another Claim still asserts
    (ADR-0005 / ADR-0013, issue #47).
"""

from __future__ import annotations

from health_bok.admit import admit_candidate, supersede_claims
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


def _admit(repo: Repository, extraction: Extraction, *, video_id: str = VIDEO_ID):
    return admit_candidate(
        video_id,
        extractor=FakeExtractor(extraction),
        normalizer=_normalizer(repo),
        repo=repo,
    )


def _supersede(repo: Repository, extraction: Extraction, *, video_id: str = VIDEO_ID):
    return supersede_claims(
        video_id,
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


def _plain_claim_extraction() -> Extraction:
    """A re-extraction that still produces a grounded Claim but asserts no triple."""
    return Extraction(
        claims=[
            ExtractedClaim(
                text="APOE4 is a gene worth knowing about.",
                locator_seconds=42,
                type="finding",
                concepts=[ConceptMention(name="APOE4")],
            )
        ]
    )


def test_supersede_re_points_evidence_to_the_superseding_claim(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)
    _admit(repo, _extraction_with_triple())
    repo.commit()

    [old_claim] = repo.admitted_claims(VIDEO_ID)
    [old_rel] = repo.list_concept_relations()

    # Re-extraction asserts the *same* relationship: it survives, but its evidence is
    # re-pointed onto the superseding Claim — the prior Claim is gone.
    result = _supersede(repo, _extraction_with_triple())
    repo.commit()

    assert (result.claims_superseded, result.claims_admitted, result.relations_removed) == (
        1, 1, 0,
    )
    [new_claim] = repo.admitted_claims(VIDEO_ID)
    assert new_claim.id != old_claim.id
    [rel] = repo.list_concept_relations()
    assert rel.id == old_rel.id  # the same relationship row, not a churned duplicate
    assert rel.evidence_claim_ids == [new_claim.id]  # evidence re-pointed, no orphans


def test_supersede_drops_a_relationship_whose_triple_vanished(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)
    _admit(repo, _extraction_with_triple())
    repo.commit()
    assert len(repo.list_concept_relations()) == 1

    # The re-extraction no longer asserts the connection: the relationship loses its
    # last evidencing Claim and is removed entirely.
    result = _supersede(repo, _plain_claim_extraction())
    repo.commit()

    assert result.relations_removed == 1
    assert repo.list_concept_relations() == []
    # The new (triple-less) Claim is admitted; the superseded one is gone.
    assert len(repo.admitted_claims(VIDEO_ID)) == 1


def test_supersede_keeps_a_relationship_another_claim_still_asserts(conn):
    repo = Repository(conn)
    other_video = "vid_relations_other"
    seed_processed_video(repo, video_id=VIDEO_ID)
    seed_processed_video(repo, video_id=other_video)
    # Two Sources independently assert APOE4 risk_factor_for Alzheimer's.
    _admit(repo, _extraction_with_triple())
    _admit(repo, _extraction_with_triple(), video_id=other_video)
    repo.commit()

    [rel] = repo.list_concept_relations()
    assert len(rel.evidence_claim_ids) == 2
    [other_claim] = repo.admitted_claims(other_video)

    # Supersede only VIDEO_ID, dropping its triple: the relationship survives on the
    # other Source's Claim — only the stale evidence link is removed.
    result = _supersede(repo, _plain_claim_extraction())
    repo.commit()

    assert result.relations_removed == 0
    [survivor] = repo.list_concept_relations()
    assert survivor.id == rel.id
    assert survivor.evidence_claim_ids == [other_claim.id]


def test_supersede_is_idempotent(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)
    _admit(repo, _extraction_with_triple())
    repo.commit()

    _supersede(repo, _extraction_with_triple())
    repo.commit()
    _supersede(repo, _extraction_with_triple())
    repo.commit()

    relations = repo.list_concept_relations()
    assert len(relations) == 1
    # No orphaned evidence links: every evidence id points at a still-present Claim.
    claim_ids = {c.id for c in repo.admitted_claims(VIDEO_ID)}
    assert set(relations[0].evidence_claim_ids) == claim_ids


def test_supersede_never_clobbers_a_protected_claim(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)
    _admit(repo, _extraction_with_triple())
    repo.commit()

    # The owner hand-corrects the Claim, protecting it (ADR-0010).
    [claim] = repo.admitted_claims(VIDEO_ID)
    assert repo.update_claim(
        claim.id, text="APOE4 strongly raises Alzheimer's risk.", type="finding",
        locator_seconds=claim.locator_seconds,
    )
    repo.commit()

    # A later re-extraction that drops the triple must not touch the protected Claim,
    # so the relationship it evidences stands.
    result = _supersede(repo, _plain_claim_extraction())
    repo.commit()

    assert result.claims_superseded == 0  # the protected Claim is out of the span
    [rel] = repo.list_concept_relations()
    # The relationship still rests on the untouched protected Claim.
    assert rel.evidence_claim_ids == [claim.id]
    assert claim.id in {c.id for c in repo.admitted_claims(VIDEO_ID)}
