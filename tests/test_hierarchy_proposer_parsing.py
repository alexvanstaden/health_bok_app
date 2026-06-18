"""The HierarchyProposer's JSON parsing (ADR-0013) — pure, no network.

`parse_parents` turns the model's JSON into broader-parent names. It must tolerate
code fences, constrain parents to the nearby set (a hallucinated name is dropped),
return canonical spellings, and degrade an unparseable answer to no suggestions.
Mirrors `test_concept_proposer_parsing`.
"""

from __future__ import annotations

from health_bok.adapters.hierarchy_proposer import parse_parents

NEARBY = ["Brain", "Brain metabolism", "lipid metabolism", "genetics"]


def test_parses_fenced_json_and_constrains_to_nearby():
    raw = """```json
    {"parents": ["Brain", "genetics"]}
    ```"""
    assert parse_parents(raw, NEARBY) == ["Brain", "genetics"]


def test_drops_hallucinated_names_not_in_the_nearby_set():
    raw = '{"parents": ["Brain", "quantum healing", "lipid metabolism"]}'
    # "quantum healing" was never offered -> dropped; the real ones survive.
    assert parse_parents(raw, NEARBY) == ["Brain", "lipid metabolism"]


def test_returns_canonical_spelling_case_insensitively():
    raw = '{"parents": ["brain", "GENETICS"]}'
    assert parse_parents(raw, NEARBY) == ["Brain", "genetics"]


def test_unparseable_or_empty_degrades_to_no_suggestions():
    assert parse_parents("not json", NEARBY) == []
    assert parse_parents('{"parents": "Brain"}', NEARBY) == []
    assert parse_parents('{"nope": []}', NEARBY) == []
