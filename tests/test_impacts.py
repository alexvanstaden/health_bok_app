"""The Impact engine: bidirectional change detection against a real Postgres (issue #18).

Slice 13 detects when newly-arrived evidence bears on the owner's choices — or a
new choice meets the existing library — and surfaces it as a stance-typed Impact
with a reviewable inbox and an audit trail. This drives the `impacts` service — the
same code the HTTP API and the worker wrap — over a real Postgres + pgvector,
starting from a genuinely admitted Candidate (the Slice-8 path) so the Concept
overlap candidates are generated over the Concepts extraction actually built.
Candidate generation is real; only the `StanceJudge` is faked, so a test pins the
stance and asserts what overlap surfaced.

Covers the acceptance criteria: a newly-admitted Claim/Protocol overlapping an
anchor raises a stance-typed Impact and an `unrelated` judgement raises nothing
(forward); recording a Decision/Goal scans the Body of Knowledge (reverse); the same
finding never raises twice (dedup); the inbox is filterable by stance and anchor;
each Impact walks `new → reviewed → actioned | dismissed` without re-nagging, with
bulk-dismiss; actioning records the resulting Decision link; and a superseded
supporting Claim raises an Impact against its Decision (ADR-0005).
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok import impacts, personal, review
from health_bok.repository import Repository
from health_bok.worker import drain
from tests.fakes import (
    FakeContentSource,
    FakeExtractor,
    FakeStanceJudge,
    FakeTranscriber,
)
from tests.seed import seed_processed_video
from tests.test_admission import (
    RAPAMYCIN_CLAIM,
    make_extraction,
    normalizer,
)

VIDEO_ID = "vid_impacts"
AT = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)


def _admit(repo: Repository) -> None:
    """Admit a video so the BoK has Concepts/Claims/Protocols to detect against.

    Drained without a judge, so admission alone raises no Impacts — the personal
    layer the tests add afterwards is what detection then fires against.
    """
    seed_processed_video(repo, video_id=VIDEO_ID)
    review.approve_candidate(VIDEO_ID, repo=repo)
    drain(
        content_source=FakeContentSource(),
        transcriber=FakeTranscriber(),
        extractor=FakeExtractor(make_extraction()),
        normalizer=normalizer(repo),
        repo=repo,
    )


def _drain_with_judge(repo: Repository, judge: FakeStanceJudge) -> None:
    """Admit a seeded+approved video *with* the forward Impact pass wired."""
    drain(
        content_source=FakeContentSource(),
        transcriber=FakeTranscriber(),
        extractor=FakeExtractor(make_extraction()),
        normalizer=normalizer(repo),
        repo=repo,
        judge=judge,
    )


def _rapamycin_claim_id(repo: Repository) -> int:
    return next(c.id for c in repo.list_claims() if c.text == RAPAMYCIN_CLAIM)


def _decision(repo: Repository, *, action="Take rapamycin", concepts=("rapamycin",)) -> int:
    return personal.record_decision(
        action=action,
        dose="6mg",
        timing=None,
        frequency="weekly",
        duration=None,
        started_at=AT,
        ended_at=None,
        note=None,
        concepts=list(concepts),
        implements_protocol_id=None,
        normalizer=normalizer(repo),
        repo=repo,
    )


def _goal(repo: Repository, *, title, concepts) -> int:
    return personal.record_goal(
        title=title,
        detail=None,
        concepts=list(concepts),
        normalizer=normalizer(repo),
        repo=repo,
    )


# -- Forward: new evidence vs existing anchors ------------------------------


def test_admitting_evidence_raises_stance_impacts_and_unrelated_raises_nothing(conn):
    repo = Repository(conn)
    # Two Goals exist *before* the evidence arrives, minting their Concepts; the
    # admitted Claims/Protocols then merge onto the same Concepts (Slice 8/11).
    slow_aging = _goal(repo, title="Slow aging", concepts=["rapamycin"])
    build_muscle = _goal(repo, title="Build muscle", concepts=["creatine monohydrate"])

    seed_processed_video(repo, video_id=VIDEO_ID)
    review.approve_candidate(VIDEO_ID, repo=repo)
    # The judge reinforces the rapamycin Goal but finds creatine unrelated — overlap
    # alone must not raise it.
    judge = FakeStanceJudge(
        stances={"Slow aging": "reinforces", "Build muscle": "unrelated"}
    )
    _drain_with_judge(repo, judge)

    inbox = impacts.inbox(repo=repo)
    assert len(inbox) == 1
    imp = inbox[0]
    assert imp.stance == "reinforces"
    assert imp.anchor_type == "goal" and imp.anchor_id == slow_aging
    assert imp.source_type == "claim" and imp.source_label == RAPAMYCIN_CLAIM
    assert imp.state == "new"

    # The unrelated pair *was* generated and judged (real Concept overlap), but the
    # judge's verdict — not the overlap — kept it out of the inbox.
    judged = {(k.text, a.id) for k, a in judge.calls}
    assert any(a_id == build_muscle for _, a_id in judged)
    assert not any(i.anchor_id == build_muscle for i in inbox)


def test_a_marker_reading_is_an_anchor(conn):
    repo = Repository(conn)
    # A Marker the owner tracks exists before the evidence; the new Claim is judged
    # against the latest reading.
    personal.record_marker(
        concept="rapamycin",
        value=2.5,
        unit="ng/mL",
        reference_low=None,
        reference_high=2.0,
        measured_at=AT,
        normalizer=normalizer(repo),
        repo=repo,
    )
    seed_processed_video(repo, video_id=VIDEO_ID)
    review.approve_candidate(VIDEO_ID, repo=repo)
    _drain_with_judge(repo, FakeStanceJudge(default="refines"))

    inbox = impacts.inbox(repo=repo)
    imp = next(i for i in inbox if i.anchor_type == "marker")
    assert imp.stance == "refines"
    assert "rapamycin" in imp.anchor_label  # the reading's label
    assert imp.source_label == RAPAMYCIN_CLAIM


# -- Reverse: a new anchor vs the existing Body of Knowledge -----------------


def test_recording_a_decision_scans_the_body_of_knowledge(conn):
    repo = Repository(conn)
    _admit(repo)

    decision_id = _decision(repo)
    judge = FakeStanceJudge(default="contradicts")
    raised = impacts.detect_for_new_anchor("decision", decision_id, judge=judge, repo=repo)

    assert len(raised) == 1
    imp = repo.get_impact(raised[0])
    # The Impact's source is the existing rapamycin Claim, its anchor the new Decision.
    assert imp.source_type == "claim" and imp.source_label == RAPAMYCIN_CLAIM
    assert imp.anchor_type == "decision" and imp.anchor_id == decision_id
    assert imp.stance == "contradicts"
    # The existing rapamycin Claim was actually weighed (Concept overlap, reversed).
    assert any(k.text == RAPAMYCIN_CLAIM for k, _ in judge.calls)


# -- Dedup ------------------------------------------------------------------


def test_the_same_finding_is_not_raised_twice(conn):
    repo = Repository(conn)
    _admit(repo)
    decision_id = _decision(repo)
    judge = FakeStanceJudge(default="reinforces")

    first = impacts.detect_for_new_anchor("decision", decision_id, judge=judge, repo=repo)
    second = impacts.detect_for_new_anchor("decision", decision_id, judge=judge, repo=repo)

    assert first  # a finding the first time
    assert second == []  # deduped the second time — overlapping evidence never nags twice
    assert len(impacts.inbox(repo=repo)) == len(first)


# -- Inbox filtering --------------------------------------------------------


def test_inbox_is_filterable_by_stance_and_by_anchor(conn):
    repo = Repository(conn)
    _admit(repo)
    decision_id = _decision(repo)
    goal_id = _goal(repo, title="Build muscle", concepts=["creatine monohydrate"])

    impacts.detect_for_new_anchor(
        "decision", decision_id, judge=FakeStanceJudge(default="reinforces"), repo=repo
    )
    impacts.detect_for_new_anchor(
        "goal", goal_id, judge=FakeStanceJudge(default="opportunity"), repo=repo
    )

    assert len(impacts.inbox(repo=repo)) == 2

    # By stance.
    reinforces = impacts.inbox(stance="reinforces", repo=repo)
    assert {i.anchor_type for i in reinforces} == {"decision"}
    opportunities = impacts.inbox(stance="opportunity", repo=repo)
    assert {i.anchor_type for i in opportunities} == {"goal"}

    # By anchor — and the Goal's Impact is sourced from the creatine Protocol.
    by_goal = impacts.inbox(anchor_type="goal", anchor_id=goal_id, repo=repo)
    assert len(by_goal) == 1
    assert by_goal[0].stance == "opportunity"
    assert by_goal[0].source_type == "protocol"


# -- Lifecycle & no re-nag --------------------------------------------------


def test_impact_lifecycle_resolves_and_does_not_renag(conn):
    repo = Repository(conn)
    _admit(repo)
    decision_id = _decision(repo)
    judge = FakeStanceJudge(default="contradicts")
    impact_id = impacts.detect_for_new_anchor(
        "decision", decision_id, judge=judge, repo=repo
    )[0]

    # new -> reviewed: still in the inbox, just acknowledged.
    assert impacts.review_impact(impact_id, repo=repo) is True
    assert [i.state for i in impacts.inbox(repo=repo)] == ["reviewed"]

    # reviewed -> dismissed: gone from the inbox, but kept for the audit trail.
    assert impacts.dismiss_impact(impact_id, repo=repo) is True
    assert impacts.inbox(repo=repo) == []
    assert [i.id for i in impacts.inbox(state="dismissed", repo=repo)] == [impact_id]

    # Re-running detection does NOT re-nag — the resolved row dedups the finding.
    assert impacts.detect_for_new_anchor(
        "decision", decision_id, judge=judge, repo=repo
    ) == []
    assert impacts.inbox(repo=repo) == []


def test_bulk_dismiss_clears_a_burst(conn):
    repo = Repository(conn)
    _admit(repo)
    decision_id = _decision(repo)
    goal_id = _goal(repo, title="Build muscle", concepts=["creatine monohydrate"])
    impacts.detect_for_new_anchor(
        "decision", decision_id, judge=FakeStanceJudge(default="reinforces"), repo=repo
    )
    impacts.detect_for_new_anchor(
        "goal", goal_id, judge=FakeStanceJudge(default="opportunity"), repo=repo
    )

    ids = [i.id for i in impacts.inbox(repo=repo)]
    assert len(ids) == 2
    assert impacts.bulk_dismiss(ids, repo=repo) == 2
    assert impacts.inbox(repo=repo) == []


# -- Actioning records the link ---------------------------------------------


def test_actioning_an_impact_records_the_resulting_decision(conn):
    repo = Repository(conn)
    _admit(repo)
    # An unmet Goal — the prime target for an `opportunity` Impact (CONTEXT.md).
    goal_id = _goal(repo, title="Slow aging", concepts=["rapamycin"])
    impact_id = impacts.detect_for_new_anchor(
        "goal", goal_id, judge=FakeStanceJudge(default="opportunity"), repo=repo
    )[0]

    # The owner creates a Decision in response and actions the Impact.
    decision_id = _decision(repo, action="Adopt rapamycin")
    assert impacts.action_impact(impact_id, decision_id=decision_id, repo=repo) is True

    # The link is recorded and the Impact has left the inbox.
    actioned = impacts.inbox(state="actioned", repo=repo)
    assert [i.id for i in actioned] == [impact_id]
    assert actioned[0].actioned_decision_id == decision_id
    assert impacts.inbox(repo=repo) == []


# -- Supersede (ADR-0005) ---------------------------------------------------


def test_superseding_a_supporting_claim_raises_impact_against_the_decision(conn):
    repo = Repository(conn)
    _admit(repo)
    decision_id = _decision(repo)
    claim_id = _rapamycin_claim_id(repo)
    # Confirm the rapamycin Claim supports the Decision (claim -> decision supports).
    assert personal.link_decision(
        decision_id, target_type="claim", target_id=claim_id, repo=repo
    ) is True

    # Re-extraction can no longer match the Claim: rather than silently break the
    # link, the evidence change is surfaced as an Impact on the Decision.
    raised = impacts.supersede_impacts(claim_id, repo=repo)
    assert len(raised) == 1
    imp = repo.get_impact(raised[0])
    assert imp.anchor_type == "decision" and imp.anchor_id == decision_id
    assert imp.source_type == "claim" and imp.source_id == claim_id
    assert imp.stance == "contradicts"
    assert imp.detail and "supersed" in imp.detail.lower()
