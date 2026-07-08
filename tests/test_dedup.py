"""De-duplicating the Concept catalogue: merge near-identical hubs (ADR-0014).

Over a real Postgres + pgvector: `Repository.merge_concepts` re-points every
reference (markers, edges, lateral relations + evidence, embeddings) off a merged-
away Concept and deletes it, losing nothing and leaving no dangling ref; and
`dedup.dedup_catalogue` applies the two-tier merge/adjudicate decision across the
catalogue, merging confident duplicates while leaving looser pairs separate. Prior
art: `test_concept_normalization` (the merge/adjudicate band), `test_hierarchy`
(broader-of edges), `test_concept_neighbourhood` (lateral relations).
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok import curation, dedup
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
NOW = datetime(2026, 6, 18, tzinfo=timezone.utc)

# "Alzheimer's disease" and "Alzheimer's" sit in the adjudication band (~0.2 apart):
# close enough to ask the adjudicator, not close enough to auto-merge. "omega-3" and
# "sleep" are orthogonal and must never merge.
VECTORS = {
    "Alzheimer's disease": [1, 0, 0, 0],
    "Alzheimer's": [0.8, 0.6, 0, 0],  # cosine distance ~0.2 from "Alzheimer's disease"
    "omega-3": [0, 1, 0, 0],
    "sleep": [0, 0, 1, 0],
}


def _admit_relation(repo: Repository, *, video_id: str, subject: str, obj: str) -> None:
    seed_processed_video(repo, video_id=video_id, channel_id="UC_a")
    admit_candidate(
        video_id,
        extractor=FakeExtractor(
            Extraction(
                claims=[
                    ExtractedClaim(
                        text=f"{subject} protects_against {obj}.",
                        locator_seconds=10,
                        concepts=[
                            ConceptMention(name=subject),
                            ConceptMention(name=obj),
                        ],
                        triples=[
                            ConceptTriple(
                                subject=ConceptMention(name=subject),
                                predicate="protects_against",
                                object=ConceptMention(name=obj),
                            )
                        ],
                    )
                ]
            )
        ),
        normalizer=ConceptNormalizer(FakeEmbedder(VECTORS), repo, model=EMBED_MODEL),
        repo=repo,
    )
    repo.commit()


def _cid(repo: Repository, name: str) -> int:
    return next(c.id for c in repo.list_concepts() if c.name == name)


def _mint(repo: Repository, name: str) -> int:
    cid = repo.add_concept(name)
    repo.add_embedding("concept", cid, FakeEmbedder(VECTORS).embed(name), model=EMBED_MODEL)
    return cid


def test_merge_concepts_repoints_relations_edges_and_evidence(conn):
    repo = Repository(conn)
    # Two videos assert a relation, one on each spelling of Alzheimer's — after a
    # merge both must rest on the single surviving hub, evidence preserved.
    _admit_relation(repo, video_id="v1", subject="omega-3", obj="Alzheimer's disease")
    _admit_relation(repo, video_id="v2", subject="omega-3", obj="Alzheimer's")

    keep = _cid(repo, "Alzheimer's disease")
    drop = _cid(repo, "Alzheimer's")

    assert repo.merge_concepts(keep, drop) is True
    repo.commit()

    # The dropped Concept is gone; its name no longer resolves.
    assert all(c.name != "Alzheimer's" for c in repo.list_concepts())
    # Both relations now point at the surviving hub — folded onto one directed pair,
    # carrying the evidence from both videos (nothing lost).
    omega = _cid(repo, "omega-3")
    hood = repo.concept_neighbourhood(omega, now=NOW, half_life_days=365)
    protects = [r for r in hood.relations if r.predicate == "protects_against"]
    assert len(protects) == 1
    assert protects[0].dst_concept_id == keep
    assert len(protects[0].evidence) == 2  # both videos' Claims survived the merge


def test_merge_is_a_noop_for_equal_or_missing(conn):
    repo = Repository(conn)
    a = _mint(repo, "sleep")
    repo.commit()
    assert repo.merge_concepts(a, a) is False           # equal ids
    assert repo.merge_concepts(a, 999999) is False       # missing partner
    # The surviving Concept is untouched.
    assert _cid(repo, "sleep") == a


class _ScriptedAdjudicator:
    """Merges exactly the name pairs it is told to; records what it was asked."""

    def __init__(self, merge_pairs: set[frozenset[str]]):
        self._pairs = merge_pairs
        self.calls: list[tuple[str, str]] = []

    def __call__(self, mention, nearest) -> bool:
        self.calls.append((mention.name, nearest.name))
        return frozenset((mention.name, nearest.name)) in self._pairs


def test_dedup_merges_confident_pairs_and_leaves_others(conn):
    repo = Repository(conn)
    for name in ("Alzheimer's disease", "Alzheimer's", "omega-3", "sleep"):
        _mint(repo, name)
    repo.commit()

    # The adjudicator agrees only that the two Alzheimer's spellings are the same.
    adjudicator = _ScriptedAdjudicator(
        {frozenset({"Alzheimer's disease", "Alzheimer's"})}
    )
    result = dedup.dedup_catalogue(
        embedder=FakeEmbedder(VECTORS),
        repo=repo,
        model=EMBED_MODEL,
        adjudicator=adjudicator,
        # Force the band so the near pair is adjudicated, not auto-merged.
        merge_distance=0.001,
        adjudicate_distance=0.30,
    )

    # Exactly one merge — the two Alzheimer's hubs collapse to one.
    assert len(result.merged) == 1
    names = {c.name for c in repo.list_concepts()}
    assert names == {"Alzheimer's disease", "omega-3", "sleep"}
    # omega-3 and sleep are orthogonal to everything, never merged.
    assert result.concepts_scanned == 4
