"""Relationship-aware alerting on the one Impact inbox (ADR-0013, slice 4).

Over a real Postgres: a new/changed relationship touching a tracked Goal/Decision
(or its subtree) raises a Tier-1 push Impact with a structurally-derived stance
(`new_link`/`contradicts`/`eroded`); an untracked-but-strong change reaches the
Tier-2 feed, gated by Strength; widening scope yields a single summary Impact, not a
burst; and re-runs never re-nag. Prior art: `test_impacts`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok import impacts
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

VECTORS = {
    "omega-3": [1, 0, 0, 0, 0, 0],
    "Alzheimer's": [0, 1, 0, 0, 0, 0],
    "magnesium": [0, 0, 1, 0, 0, 0],
    "sleep quality": [0, 0, 0, 1, 0, 0],
    "Brain metabolism": [0, 0, 0, 0, 1, 0],
    "ketones": [0, 0, 0, 0, 0, 1],
    "Brain": [0, 0, 0, 0, 1, 1],
}


def _admit_relation(repo, *, video_id, channel_id, subject, predicate, obj,
                    published=NOW):
    seed_processed_video(repo, video_id=video_id, channel_id=channel_id,
                         channel_name=channel_id, published_at=published)
    admit_candidate(
        video_id,
        extractor=FakeExtractor(Extraction(claims=[
            ExtractedClaim(
                text=f"{subject} {predicate} {obj}.",
                locator_seconds=10,
                concepts=[ConceptMention(name=subject), ConceptMention(name=obj)],
                triples=[ConceptTriple(
                    subject=ConceptMention(name=subject),
                    predicate=predicate,
                    object=ConceptMention(name=obj),
                )],
            )
        ])),
        normalizer=ConceptNormalizer(FakeEmbedder(VECTORS), repo, model=EMBED_MODEL),
        repo=repo,
    )
    repo.commit()


def _cid(repo, name):
    return next(c.id for c in repo.list_concepts() if c.name == name)


def _goal_tracking(repo, concept_id, title="a goal"):
    gid = repo.add_goal(title=title)
    repo.add_edge("goal", gid, "concept", concept_id, "references")
    repo.commit()
    return gid


def _decision_tracking(repo, concept_id, action="an intervention"):
    did = repo.add_decision(action=action, dose=None, timing=None, frequency=None,
                            duration=None, started_at=NOW, ended_at=None, note=None)
    repo.add_edge("decision", did, "concept", concept_id, "references")
    repo.commit()
    return did


def test_tier1_new_link_on_a_tracked_goal_and_no_renag(conn):
    repo = Repository(conn)
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")
    _goal_tracking(repo, _cid(repo, "Alzheimer's"))

    raised = impacts.detect_relationship_impacts_for_video("v1", repo=repo, now=NOW)
    assert len(raised) == 1
    [impact] = impacts.inbox(repo=repo)
    assert impact.stance == "new_link"
    assert impact.anchor_type == "goal"
    assert impact.tier == 1
    assert "omega-3 protects_against Alzheimer's" in impact.source_label

    # Re-running raises nothing — the inbox never re-nags.
    assert impacts.detect_relationship_impacts_for_video("v1", repo=repo, now=NOW) == []
    assert len(impacts.inbox(repo=repo)) == 1


def test_tier1_contradicts_when_pair_becomes_contested(conn):
    repo = Repository(conn)
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")
    _goal_tracking(repo, _cid(repo, "Alzheimer's"))
    impacts.detect_relationship_impacts_for_video("v1", repo=repo, now=NOW)

    # A debunking finding contests the pair -> a contradicts Impact.
    _admit_relation(repo, video_id="v2", channel_id="UC_b",
                    subject="omega-3", predicate="no_effect_on", obj="Alzheimer's")
    impacts.detect_relationship_impacts_for_video("v2", repo=repo, now=NOW)

    stances = {i.stance for i in impacts.inbox(repo=repo)}
    assert "contradicts" in stances


def test_tier1_fires_via_broader_of_subtree(conn):
    repo = Repository(conn)
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="Brain metabolism", predicate="associated_with", obj="ketones")
    brain = repo.add_concept("Brain")
    repo.add_embedding("concept", brain, FakeEmbedder(VECTORS).embed("Brain"),
                       model=EMBED_MODEL)
    repo.commit()
    bmet = _cid(repo, "Brain metabolism")
    repo.propose_broader_of(brain, bmet)
    repo.confirm_broader_of(brain, bmet)
    repo.commit()

    # The Goal tracks "Brain"; the development lives on its descendant "Brain
    # metabolism" — tracking the parent still catches it (user story 31).
    _goal_tracking(repo, brain, title="brain health")
    raised = impacts.detect_relationship_impacts_for_video("v1", repo=repo, now=NOW)
    assert len(raised) == 1
    assert impacts.inbox(repo=repo)[0].anchor_type == "goal"


def test_tier2_feed_is_gated_by_strength(conn):
    repo = Repository(conn)
    # Two distinct creators assert the same relationship; nobody tracks it.
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="magnesium", predicate="increases", obj="sleep quality")
    _admit_relation(repo, video_id="v2", channel_id="UC_b",
                    subject="magnesium", predicate="increases", obj="sleep quality")

    # Below threshold: nothing reaches even the feed.
    assert impacts.detect_relationship_impacts_for_video(
        "v2", repo=repo, now=NOW, strength_threshold=5.0
    ) == []
    assert impacts.tier2_feed(repo=repo) == []

    # Strength 2.0 (two creators, fresh) clears a 1.5 threshold -> Tier-2 feed, not
    # the push inbox.
    raised = impacts.detect_relationship_impacts_for_video(
        "v2", repo=repo, now=NOW, strength_threshold=1.5
    )
    assert len(raised) == 1
    feed = impacts.tier2_feed(repo=repo)
    assert len(feed) == 1 and feed[0].tier == 2 and feed[0].anchor_type == "concept"
    assert impacts.inbox(repo=repo) == []  # never floods the inbox


def test_scope_widening_raises_a_single_summary_not_a_burst(conn):
    repo = Repository(conn)
    # Three pre-existing relationships all touch Alzheimer's.
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")
    _admit_relation(repo, video_id="v2", channel_id="UC_b",
                    subject="magnesium", predicate="associated_with", obj="Alzheimer's")
    _admit_relation(repo, video_id="v3", channel_id="UC_c",
                    subject="ketones", predicate="associated_with", obj="Alzheimer's")

    goal = _goal_tracking(repo, _cid(repo, "Alzheimer's"))
    raised = impacts.detect_scope_widening("goal", goal, repo=repo)
    # One summary for the backlog, not one Impact per relationship.
    assert len(raised) == 1
    [summary] = impacts.inbox(repo=repo)
    assert summary.stance == "new_link"
    assert summary.source_type == "concept"
    assert "3 existing connection" in summary.detail


def test_confirming_broader_of_widens_scope_with_a_single_summary(conn):
    repo = Repository(conn)
    # Two pre-existing relationships sit under "Brain metabolism", which is not yet
    # connected to anything the owner tracks.
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="Brain metabolism", predicate="associated_with",
                    obj="ketones")
    _admit_relation(repo, video_id="v2", channel_id="UC_b",
                    subject="omega-3", predicate="increases", obj="Brain metabolism")

    # A Goal tracks the broader "Brain"; nothing is under it yet.
    brain = repo.add_concept("Brain")
    repo.add_embedding("concept", brain, FakeEmbedder(VECTORS).embed("Brain"),
                       model=EMBED_MODEL)
    repo.commit()
    bmet = _cid(repo, "Brain metabolism")
    _goal_tracking(repo, brain, title="brain health")

    # Confirming Brain broader-of Brain metabolism pulls the subtree into scope.
    repo.propose_broader_of(brain, bmet)
    repo.confirm_broader_of(brain, bmet)
    repo.commit()
    raised = impacts.detect_scope_widening_for_broader_of(brain, bmet, repo=repo)

    # Exactly one summary for the backlog now under Brain — not one per relationship.
    assert len(raised) == 1
    [summary] = impacts.inbox(repo=repo)
    assert summary.stance == "new_link"
    assert summary.anchor_type == "goal"
    assert summary.source_type == "concept"
    assert "2 existing connection" in summary.detail
    assert "under Brain" in summary.detail

    # Re-confirming never re-nags.
    assert impacts.detect_scope_widening_for_broader_of(brain, bmet, repo=repo) == []
    assert len(impacts.inbox(repo=repo)) == 1

    # Only edges arriving *afterwards* push individually: a fresh relationship on the
    # now-in-scope subtree reaches the inbox via the per-video pass.
    _admit_relation(repo, video_id="v3", channel_id="UC_c",
                    subject="Brain metabolism", predicate="associated_with",
                    obj="sleep quality")
    later = impacts.detect_relationship_impacts_for_video("v3", repo=repo, now=NOW)
    assert len(later) == 1
    assert len(impacts.inbox(repo=repo)) == 2


def test_eroded_impact_when_a_tracked_relationship_loses_its_last_evidence(conn):
    repo = Repository(conn)
    _admit_relation(repo, video_id="v1", channel_id="UC_a",
                    subject="omega-3", predicate="protects_against", obj="Alzheimer's")
    _decision_tracking(repo, _cid(repo, "Alzheimer's"), action="take omega-3")
    [claim] = repo.admitted_claims("v1")

    existed, raised = impacts.delete_claim_with_alerts(claim.id, repo=repo)
    assert existed is True
    assert len(raised) == 1
    [impact] = impacts.inbox(repo=repo)
    assert impact.stance == "eroded"
    assert impact.anchor_type == "decision"
    # The relationship is gone; the Impact still renders from its detail.
    assert repo.list_concept_relations() == []
    assert "lost its last evidence" in impact.detail
