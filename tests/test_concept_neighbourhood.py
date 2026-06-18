"""The roll-up neighbourhood: Strength-ranked, contested-aware relationships (ADR-0013).

Seeds Concepts, Claims from several Creators at different dates, and lateral
relationships over a real Postgres + pgvector, then asserts `concept_neighbourhood`
ranks by Strength, counts *distinct* Creators (a prolific Creator counts once), and
flags a contested pair — including the helps-vs-no-effect debunking case — while a
signless relationship is never contested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
PUBLISHED = datetime(2026, 1, 1, tzinfo=timezone.utc)

CONCEPT_VECTORS = {
    "omega-3": [1, 0, 0, 0, 0],
    "Alzheimer's": [0, 1, 0, 0, 0],
    "apoB": [0, 0, 1, 0, 0],
}


def _admit_relation(
    repo: Repository,
    *,
    video_id: str,
    channel_id: str,
    subject: str,
    predicate: str,
    obj: str,
    published: datetime = PUBLISHED,
):
    seed_processed_video(
        repo, video_id=video_id, channel_id=channel_id, channel_name=channel_id,
        published_at=published,
    )
    admit_candidate(
        video_id,
        extractor=FakeExtractor(
            Extraction(
                claims=[
                    ExtractedClaim(
                        text=f"{subject} {predicate} {obj}.",
                        locator_seconds=10,
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
        ),
        normalizer=ConceptNormalizer(
            FakeEmbedder(CONCEPT_VECTORS), repo, model=EMBED_MODEL
        ),
        repo=repo,
    )
    repo.commit()


def _concept_id(repo: Repository, name: str) -> int:
    return next(c.id for c in repo.list_concepts() if c.name == name)


def test_neighbourhood_ranks_by_strength_and_flags_contested(conn):
    repo = Repository(conn)
    # Two creators say omega-3 protects against Alzheimer's; one says no effect.
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")
    _admit_relation(repo, video_id="v2", channel_id="UC_b",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")
    _admit_relation(repo, video_id="v3", channel_id="UC_c",
                    subject="omega-3", predicate="no_effect_on", obj="Alzheimer's")
    # A weaker, uncontested signless relationship from a single creator.
    _admit_relation(repo, video_id="v4", channel_id="UC_a",
                    subject="omega-3", predicate="biomarker_of", obj="apoB")

    omega3 = _concept_id(repo, "omega-3")
    hood = repo.concept_neighbourhood(omega3, now=NOW, half_life_days=365)

    assert hood is not None
    assert hood.concept_name == "omega-3"
    by_pred = {r.predicate: r for r in hood.relations}

    # protects_against: 2 distinct creators -> strongest; contested by no_effect_on.
    protects = by_pred["protects_against"]
    assert protects.creator_count == 2
    assert protects.contested is True

    # no_effect_on: 1 creator, contested (the debunking case).
    no_effect = by_pred["no_effect_on"]
    assert no_effect.creator_count == 1
    assert no_effect.contested is True

    # biomarker_of: signless -> never contested, weakest.
    biomarker = by_pred["biomarker_of"]
    assert biomarker.contested is False

    # Ranked best-supported first: protects_against (2 creators) outranks the rest.
    assert hood.relations[0].predicate == "protects_against"
    assert protects.strength > no_effect.strength


def test_trust_tier_lifts_a_creator_above_the_count(conn):
    repo = Repository(conn)
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")
    # One trusted creator (tier 3) should outweigh a single tier-1 creator.
    repo.set_creator_trust_tier(repo.creator_id("UC_a"), 3)
    repo.commit()

    omega3 = _concept_id(repo, "omega-3")
    hood = repo.concept_neighbourhood(omega3, now=NOW, half_life_days=365)
    [rel] = hood.relations
    assert rel.creator_count == 1
    # tier 3 (x recency-decay) lifts one trusted creator well above a single
    # untiered creator's ~0.7 — the trust-tier carries "quality" into Strength.
    assert rel.strength > 2.0


def test_untiered_strength_is_a_plain_distinct_creator_count(conn):
    # Issue #49 AC: with no tiers set, Strength == the distinct-Creator count, and
    # one prolific Creator's whole back-catalogue counts *once* (the echo-chamber
    # guard, ADR-0013). Seed three Creators, all today, all default tier 1 — but
    # Creator UC_a evidences the same relationship across THREE videos.
    repo = Repository(conn)
    for vid in ("v1", "v2", "v3"):
        _admit_relation(repo, video_id=vid, channel_id="UC_a",
                        subject="omega-3", predicate="protects_against",
                        obj="Alzheimer's", published=NOW)
    _admit_relation(repo, video_id="v4", channel_id="UC_b",
                    subject="omega-3", predicate="protects_against",
                    obj="Alzheimer's", published=NOW)
    _admit_relation(repo, video_id="v5", channel_id="UC_c",
                    subject="omega-3", predicate="protects_against",
                    obj="Alzheimer's", published=NOW)

    omega3 = _concept_id(repo, "omega-3")
    [rel] = repo.concept_neighbourhood(omega3, now=NOW, half_life_days=365).relations

    # Five evidencing Claims, but only three distinct Creators...
    assert rel.creator_count == 3
    # ...so Strength is exactly that count (every Creator tier 1, every Claim today,
    # so recency-decay is 1.0): UC_a's three videos do not manufacture consensus.
    assert abs(rel.strength - 3.0) < 1e-9


def test_strength_sums_distinct_creators_by_tier_and_recency(conn):
    # Issue #49 AC: seed evidencing Claims from several Creators at different tiers
    # and dates and assert the exact Strength. With half-life 365d and NOW the
    # reference instant, contributions land on round numbers:
    #   UC_a  tier 1, today          -> 1 * 0.5**(0/365)   = 1.0
    #   UC_b  tier 2, one half-life   -> 2 * 0.5**(365/365) = 1.0
    #   UC_c  tier 1, prolific:                              = 1.0
    #         a stale video a half-life back AND a fresh one today; the most-recent
    #         contribution drives the Creator's single term -> 1 * 1.0 = 1.0
    # Total = 3.0 over three distinct Creators (UC_c's two videos count once).
    a_year_ago = NOW - timedelta(days=365)
    repo = Repository(conn)
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="omega-3", predicate="protects_against",
                    obj="Alzheimer's", published=NOW)
    _admit_relation(repo, video_id="v2", channel_id="UC_b",
                    subject="omega-3", predicate="protects_against",
                    obj="Alzheimer's", published=a_year_ago)
    _admit_relation(repo, video_id="v3", channel_id="UC_c",
                    subject="omega-3", predicate="protects_against",
                    obj="Alzheimer's", published=a_year_ago)
    _admit_relation(repo, video_id="v4", channel_id="UC_c",
                    subject="omega-3", predicate="protects_against",
                    obj="Alzheimer's", published=NOW)
    repo.set_creator_trust_tier(repo.creator_id("UC_b"), 2)
    repo.commit()

    omega3 = _concept_id(repo, "omega-3")
    [rel] = repo.concept_neighbourhood(omega3, now=NOW, half_life_days=365).relations

    assert rel.creator_count == 3
    assert abs(rel.strength - 3.0) < 1e-9


def test_relations_carry_evidencing_claims_with_source_and_locator(conn):
    # Issue #51 AC: every relationship in the neighbourhood links through to the
    # Claims that evidence it — each a Citation clickable to its Source + locator
    # deep-link, the *same* shape NL Query cites (consistent picture, ADR-0011).
    repo = Repository(conn)
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")
    _admit_relation(repo, video_id="v2", channel_id="UC_b",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")

    omega3 = _concept_id(repo, "omega-3")
    [rel] = repo.concept_neighbourhood(omega3, now=NOW, half_life_days=365).relations

    # Two distinct creators evidence it, so two Citations — one per evidencing Claim.
    assert [c.claim_id for c in rel.evidence] == rel.evidence_claim_ids
    assert len(rel.evidence) == 2

    cite = rel.evidence[0]
    assert cite.text == "omega-3 protects_against Alzheimer's."
    assert cite.source_title == "Zone 2 Cardio Explained"
    # The locator deep-link jumps straight to the moment the Claim was asserted.
    assert cite.deep_link == "https://www.youtube.com/watch?v=v1&t=10s"


def test_unknown_concept_has_no_neighbourhood(conn):
    repo = Repository(conn)
    assert repo.concept_neighbourhood(999999, now=NOW) is None
