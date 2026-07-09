"""Manual Concept merge from /concepts: owner-driven curation (issue #86).

Over a real Postgres + pgvector. Concepts are the one entity that MAY be
merged/normalized (CONTEXT.md "Concept"); de-duplication does it automatically
(ADR-0014), this does it by hand. `curation.merge_concepts` reuses the de-dup
repository write once per merged-away hub, in one transaction, so 3+ Concepts
collapse atomically and nothing is lost: markers, lateral Relationships (with
evidence), and the like all re-point to the survivor, which may be renamed in the
same step. A merge that would close a `broader-of` cycle fails whole. Prior art:
`test_dedup` (the merge write + two-tier band), `test_hierarchy` (the cycle guard).
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from health_bok import curation
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

# Orthogonal vectors so nothing auto-merges at admission (distance 1.0, well past the
# 0.15 merge band): the owner merges these by hand, on purpose.
VECTORS = {
    "omega-3": [1, 0, 0, 0],
    "vitamin D": [0, 1, 0, 0],
    "cholecalciferol": [0, 0, 1, 0],
    "vitamin D3": [0, 0, 0, 1],
}


def _admit_relation(repo: Repository, *, video_id: str, subject: str, obj: str) -> None:
    """Admit one video asserting `subject protects_against obj`, minting both hubs."""
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
    return repo.add_concept(name)


def test_rename_concept_updates_name_and_reports_missing(conn):
    repo = Repository(conn)
    cid = _mint(repo, "vitamin D")
    repo.commit()

    assert repo.rename_concept(cid, "Vitamin D (25-OH)") is True
    repo.commit()
    assert repo.get_concept(cid).name == "Vitamin D (25-OH)"

    assert repo.rename_concept(999999, "nope") is False


def test_manual_merge_folds_multiple_concepts_onto_survivor(conn):
    repo = Repository(conn)
    # omega-3 relates to three distinct hubs, one per video (evidence to preserve).
    _admit_relation(repo, video_id="v1", subject="omega-3", obj="vitamin D")
    _admit_relation(repo, video_id="v2", subject="omega-3", obj="cholecalciferol")
    _admit_relation(repo, video_id="v3", subject="omega-3", obj="vitamin D3")

    survivor = _cid(repo, "vitamin D")
    drop1 = _cid(repo, "cholecalciferol")
    drop2 = _cid(repo, "vitamin D3")

    # A dated Marker reading on each merged-away hub must move to the survivor.
    repo.add_marker_reading(
        concept_id=drop1, value=30, unit="ng/mL", reference_low=30,
        reference_high=100, measured_at=NOW,
    )
    repo.add_marker_reading(
        concept_id=drop2, value=42, unit="ng/mL", reference_low=30,
        reference_high=100, measured_at=NOW,
    )
    repo.commit()

    result = curation.merge_concepts(
        survivor, [drop1, drop2], repo=repo
    )
    assert result is not None
    assert result.survivor_id == survivor
    assert set(result.merged_away) == {drop1, drop2}
    assert result.renamed is False

    # The merged-away hubs are gone; the survivor remains.
    names = {c.name for c in repo.list_concepts()}
    assert "cholecalciferol" not in names and "vitamin D3" not in names
    assert "vitamin D" in names

    # All three lateral relations fold onto the one surviving pair, evidence intact.
    omega = _cid(repo, "omega-3")
    hood = repo.concept_neighbourhood(omega, now=NOW, half_life_days=365)
    protects = [r for r in hood.relations if r.predicate == "protects_against"]
    assert len(protects) == 1
    assert protects[0].dst_concept_id == survivor
    assert len(protects[0].evidence) == 3  # one Claim per video, nothing lost

    # Both Marker readings now hang off the survivor (its history runs two deep).
    assert len(repo.marker_history(survivor)) == 2


def test_merge_applies_rename_in_same_step(conn):
    repo = Repository(conn)
    survivor = _mint(repo, "vitamin D")
    drop = _mint(repo, "cholecalciferol")
    repo.commit()

    result = curation.merge_concepts(
        survivor, [drop], new_name="Vitamin D (calcifediol)", repo=repo
    )
    assert result is not None and result.renamed is True
    assert repo.get_concept(survivor).name == "Vitamin D (calcifediol)"
    assert all(c.name != "cholecalciferol" for c in repo.list_concepts())


def test_merge_rejects_a_broader_of_cycle_whole(conn):
    repo = Repository(conn)
    a = _mint(repo, "Brain")
    b = _mint(repo, "Brain metabolism")
    c = _mint(repo, "Cognition")
    repo.commit()

    # Build C broader-of A broader-of B  (C → A → B), all confirmed.
    curation.propose_broader_of(a, b, repo=repo)
    curation.confirm_broader_of(a, b, repo=repo)
    curation.propose_broader_of(c, a, repo=repo)
    curation.confirm_broader_of(c, a, repo=repo)

    # Merging B into C would repoint "A broader-of B" to "A broader-of C", closing
    # the loop C → A → C. The DB cycle-guard rejects it; the merge must fail whole.
    with pytest.raises(psycopg.errors.RaiseException):
        curation.merge_concepts(c, [b], repo=repo)

    # No partial state: every Concept still exists and the hierarchy is unchanged.
    names = {x.name for x in repo.list_concepts()}
    assert names == {"Brain", "Brain metabolism", "Cognition"}
    assert repo.list_broader_of(confirmed=True) == sorted([(a, b, True), (c, a, True)])


def test_merge_is_a_noop_for_missing_or_too_few(conn):
    repo = Repository(conn)
    survivor = _mint(repo, "vitamin D")
    repo.commit()

    # No distinct drop to fold on -> nothing happens.
    assert curation.merge_concepts(survivor, [survivor], repo=repo) is None
    assert curation.merge_concepts(survivor, [], repo=repo) is None
    # A missing drop -> whole merge is a no-op, survivor untouched.
    assert curation.merge_concepts(survivor, [999999], repo=repo) is None
    assert repo.get_concept(survivor).name == "vitamin D"
