"""The Claude ConceptProposer's JSON parsing & prompt rendering (issue #39) — no network.

`parse_concepts` turns the model's JSON into a list of candidate Concept terms. It
must tolerate code fences, trim whitespace, drop blank/non-string entries, and —
crucially — collapse anything it doesn't recognize (an unparseable response, a
missing list, a non-list value) to an empty list, the safe degrade, so a sloppy
answer simply proposes no new Concepts rather than failing the Goal page.
`render_goal` carries the Goal's title and (optional) detail into the prompt.
"""

from __future__ import annotations

from health_bok.adapters.concept_proposer import parse_concepts, render_goal


def test_parses_fenced_json_and_trims_terms():
    raw = '```json\n{"concepts": ["  berberine ", "VO2 max"]}\n```'
    assert parse_concepts(raw) == ["berberine", "VO2 max"]


def test_drops_blank_and_non_string_entries():
    raw = '{"concepts": ["apoB", "", "   ", 7, null, "zone 2 cardio"]}'
    assert parse_concepts(raw) == ["apoB", "zone 2 cardio"]


def test_missing_or_unparseable_yields_empty_list():
    assert parse_concepts('{"concepts": "not a list"}') == []  # wrong type
    assert parse_concepts('{"other": ["x"]}') == []  # no concepts key
    assert parse_concepts("not json at all") == []
    assert parse_concepts('{"concepts": []}') == []  # an explicit "nothing new"


def test_render_goal_carries_title_and_optional_detail():
    with_detail = render_goal("Improve insulin sensitivity", "Lower fasting glucose.")
    assert "GOAL: Improve insulin sensitivity" in with_detail
    assert "DETAIL: Lower fasting glucose." in with_detail

    # No detail (or blank) → no DETAIL line, just the title.
    bare = render_goal("Improve insulin sensitivity", None)
    assert "GOAL: Improve insulin sensitivity" in bare
    assert "DETAIL:" not in bare
    assert "DETAIL:" not in render_goal("Train for a marathon", "   ")
