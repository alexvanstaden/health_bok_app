"""The predicate vocabulary's polarity and contradiction logic (ADR-0013) — pure.

Contradiction is *derived* from a tiny opposite-pairs lookup plus the one
`no_effect_on` rule, never merged (ADR-0002). These assert that logic standalone,
the way the PRD's testing decisions ask (the JSON/data contracts tested without a
database).
"""

from __future__ import annotations

from health_bok.predicates import contradicts, normalize_predicate


def test_opposite_signed_pairs_contradict():
    assert contradicts("protects_against", "risk_factor_for")
    assert contradicts("risk_factor_for", "protects_against")
    assert contradicts("increases", "decreases")
    assert contradicts("treats", "worsens")


def test_no_effect_on_contradicts_any_signed_predicate():
    # The debunking case: "X does nothing" contests "X helps" AND "X harms".
    assert contradicts("no_effect_on", "protects_against")
    assert contradicts("no_effect_on", "risk_factor_for")
    assert contradicts("treats", "no_effect_on")


def test_signless_predicates_never_contradict():
    # A relationship *type* with no valence cannot be a contradiction, so the
    # contested view stays meaningful (user story 13).
    assert not contradicts("biomarker_of", "measured_by")
    assert not contradicts("biomarker_of", "biomarker_of")
    assert not contradicts("associated_with", "protects_against")
    assert not contradicts("no_effect_on", "biomarker_of")


def test_same_predicate_is_not_a_contradiction():
    assert not contradicts("protects_against", "protects_against")
    assert not contradicts("no_effect_on", "no_effect_on")


def test_normalize_predicate_falls_back_precision_first():
    assert normalize_predicate("Risk Factor For") == "risk_factor_for"
    assert normalize_predicate("protects-against") == "protects_against"
    assert normalize_predicate("totally made up") == "associated_with"
    assert normalize_predicate(None) == "associated_with"
    assert normalize_predicate("") == "associated_with"
