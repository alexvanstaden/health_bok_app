"""The Claude StanceJudge's JSON parsing & prompt rendering (issue #18) — no network.

`parse_stance` turns the model's JSON into a Stance. It must tolerate code fences,
normalize case, and — crucially — collapse anything it doesn't recognize (an
unparseable response, a missing stance, or an out-of-vocabulary value) to
`unrelated`, the safe discard, so a sloppy judgement can never mint an Impact the
table's CHECK would reject. `render_pair` names the shared Concepts so the model can
see the overlap it is judging.
"""

from __future__ import annotations

from health_bok.adapters.stance import parse_stance, render_pair
from health_bok.impacts import STANCES, UNRELATED
from health_bok.models import ImpactAnchor, ImpactKnowledge


def test_parses_fenced_json_and_normalizes_case():
    raw = '```json\n{"stance": "Reinforces", "rationale": "supports the decision"}\n```'
    assert parse_stance(raw) == "reinforces"


def test_every_valid_stance_round_trips():
    for stance in STANCES:
        assert parse_stance(f'{{"stance": "{stance}"}}') == stance


def test_unknown_or_unparseable_stance_becomes_unrelated():
    assert parse_stance('{"stance": "tangential"}') == UNRELATED  # out of vocabulary
    assert parse_stance('{"rationale": "no stance key"}') == UNRELATED
    assert parse_stance("not json at all") == UNRELATED
    assert parse_stance('{"stance": "unrelated"}') == UNRELATED


def test_render_pair_names_the_shared_concepts():
    knowledge = ImpactKnowledge(
        type="claim", id=1, text="Rapamycin extends lifespan in mice.",
        concepts=["rapamycin", "lifespan"],
    )
    anchor = ImpactAnchor(
        type="goal", id=2, text="Slow aging", concepts=["rapamycin"]
    )
    rendered = render_pair(knowledge, anchor)
    assert "Rapamycin extends lifespan in mice." in rendered
    assert "Slow aging" in rendered
    assert "They share: rapamycin." in rendered  # the overlap that made them candidates
