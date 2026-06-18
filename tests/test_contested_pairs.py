"""Contradiction detection on a Concept pair, over real Postgres (issue #48, ADR-0013).

Drives `admit_candidate` so the owner's Claims materialize into `concept_relations`
rows, then asserts `Repository.contested_pair` reports the right verdict at the
repository seam: opposing signed predicates and the helps-vs-no-effect debunking
case come back contested with the clashing predicates named, while agreeing and
signless predicates come back concordant. Contradiction is *derived*, never merged
(ADR-0002): both predicates stay as evidenced relationships; the pair is flagged.
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

EMBED_MODEL = "fake-embed"

# One-hot orthogonal vectors so each distinct mention is unmistakably its own
# Concept (same text -> same vector -> the same hub is reused across videos).
CONCEPT_VECTORS = {
    "omega-3": [1, 0, 0, 0, 0],
    "Alzheimer's": [0, 1, 0, 0, 0],
    "apoB": [0, 0, 1, 0, 0],
}


def _normalizer(repo: Repository) -> ConceptNormalizer:
    return ConceptNormalizer(FakeEmbedder(CONCEPT_VECTORS), repo, model=EMBED_MODEL)


def _triple_extraction(
    predicate: str, *, subject: str = "omega-3", obj: str = "Alzheimer's"
) -> Extraction:
    return Extraction(
        claims=[
            ExtractedClaim(
                text=f"{subject} {predicate} {obj}.",
                locator_seconds=60,
                type="finding",
                concepts=[ConceptMention(name=subject), ConceptMention(name=obj)],
                triples=[
                    ConceptTriple(
                        subject=ConceptMention(name=subject),
                        predicate=predicate,
                        object=ConceptMention(name=obj),
                    )
                ],
            )
        ]
    )


def _admit_triple(repo: Repository, video_id: str, predicate: str, **endpoints) -> None:
    seed_processed_video(repo, video_id=video_id)
    admit_candidate(
        video_id,
        extractor=FakeExtractor(_triple_extraction(predicate, **endpoints)),
        normalizer=_normalizer(repo),
        repo=repo,
    )
    repo.commit()


def _concept_id(repo: Repository, name: str) -> int:
    [concept] = [c for c in repo.list_concepts() if c.name == name]
    return concept.id


def test_opposing_signed_predicates_make_a_pair_contested(conn):
    repo = Repository(conn)
    # Two Sources disagree on the same ordered pair: one says omega-3 protects, the
    # other says it is a risk factor.
    _admit_triple(repo, "vid_protects", "protects_against")
    _admit_triple(repo, "vid_risk", "risk_factor_for")

    src = _concept_id(repo, "omega-3")
    dst = _concept_id(repo, "Alzheimer's")
    pair = repo.contested_pair(src, dst)

    assert pair is not None
    assert pair.contested is True
    assert pair.predicates == ["protects_against", "risk_factor_for"]
    assert pair.tensions == [("protects_against", "risk_factor_for")]
    # Nothing was merged — both predicates still stand as evidenced relationships.
    assert len(repo.list_concept_relations()) == 2


def test_no_effect_on_contests_a_signed_predicate(conn):
    repo = Repository(conn)
    # The debunking case: "omega-3 treats Alzheimer's" vs "omega-3 has no effect".
    _admit_triple(repo, "vid_treats", "treats")
    _admit_triple(repo, "vid_no_effect", "no_effect_on")

    src = _concept_id(repo, "omega-3")
    dst = _concept_id(repo, "Alzheimer's")
    pair = repo.contested_pair(src, dst)

    assert pair.contested is True
    assert pair.tensions == [("no_effect_on", "treats")]


def test_agreeing_predicates_are_concordant(conn):
    repo = Repository(conn)
    # Two Sources independently assert the *same* signed predicate — consensus, not
    # contradiction.
    _admit_triple(repo, "vid_a", "risk_factor_for")
    _admit_triple(repo, "vid_b", "risk_factor_for")

    src = _concept_id(repo, "omega-3")
    dst = _concept_id(repo, "Alzheimer's")
    pair = repo.contested_pair(src, dst)

    assert pair.contested is False
    assert pair.predicates == ["risk_factor_for"]
    assert pair.tensions == []


def test_signless_predicates_never_contest(conn):
    repo = Repository(conn)
    # Valence-free relationship *types* on the same pair cannot disagree, so the
    # contested view stays meaningful (user story 13).
    _admit_triple(repo, "vid_biomarker", "biomarker_of", subject="apoB")
    _admit_triple(repo, "vid_mechanism", "mechanism_of", subject="apoB")

    src = _concept_id(repo, "apoB")
    dst = _concept_id(repo, "Alzheimer's")
    pair = repo.contested_pair(src, dst)

    assert pair.contested is False
    assert pair.predicates == ["biomarker_of", "mechanism_of"]
    assert pair.tensions == []


def test_unrelated_pair_has_no_verdict(conn):
    repo = Repository(conn)
    _admit_triple(repo, "vid_only", "risk_factor_for")

    src = _concept_id(repo, "omega-3")
    dst = _concept_id(repo, "Alzheimer's")
    # The contradiction rule is directional: the reverse direction carries no
    # relationship, so there is nothing to contest.
    assert repo.contested_pair(dst, src) is None
