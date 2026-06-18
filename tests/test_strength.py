"""Relationship Strength: distinct creators, tiers, recency-decay (ADR-0013) — pure.

Strength must resist the echo chamber: one prolific creator repeating itself counts
once. These assert that maths standalone, before the neighbourhood query wires it to
real evidence.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from health_bok.strength import (
    EvidenceContribution,
    distinct_creator_count,
    relation_strength,
)

NOW = datetime(2026, 6, 18, tzinfo=timezone.utc)


def _contrib(creator_id, *, tier=1, days_old=0):
    return EvidenceContribution(
        creator_id=creator_id, trust_tier=tier, dated=NOW - timedelta(days=days_old)
    )


def test_one_creators_back_catalogue_counts_once():
    # Three Claims, all from creator 1, today -> Strength 1.0, one distinct creator.
    contribs = [_contrib(1), _contrib(1), _contrib(1)]
    assert distinct_creator_count(contribs) == 1
    assert relation_strength(contribs, now=NOW, half_life_days=365) == 1.0


def test_distinct_creators_add_up():
    contribs = [_contrib(1), _contrib(2), _contrib(3)]
    assert distinct_creator_count(contribs) == 3
    assert relation_strength(contribs, now=NOW, half_life_days=365) == 3.0


def test_trust_tier_weights_a_creator():
    # A tier-3 creator counts triple a tier-1 creator.
    contribs = [_contrib(1, tier=3), _contrib(2, tier=1)]
    assert relation_strength(contribs, now=NOW, half_life_days=365) == 4.0


def test_recency_decays_old_evidence():
    # One half-life old -> weighs half.
    [strength] = [relation_strength([_contrib(1, days_old=365)], now=NOW, half_life_days=365)]
    assert abs(strength - 0.5) < 1e-9


def test_per_creator_most_recent_contribution_wins_for_decay():
    # Creator 1 said it long ago AND recently; the recent one drives their weight.
    contribs = [_contrib(1, days_old=365), _contrib(1, days_old=0)]
    assert relation_strength(contribs, now=NOW, half_life_days=365) == 1.0
