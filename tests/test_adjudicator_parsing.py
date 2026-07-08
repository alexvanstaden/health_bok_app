"""The concept-merge Adjudicator's parsing + conservative degrade (ADR-0014).

Pure parsing over the ChatModel seam, no network: an explicit ``{"same": true}``
merges; everything else — a false, a missing/non-boolean field, unparseable prose,
or a raising model — keeps the Concepts separate (the safe default). Mirrors
`test_hierarchy_proposer_parsing`.
"""

from __future__ import annotations

from health_bok.adapters.adjudicator import ChatAdjudicator, parse_same
from health_bok.models import ConceptMention
from health_bok.repository import NearestConcept


class _FakeChat:
    def __init__(self, reply: str | None = None, *, error: Exception | None = None):
        self._reply = reply
        self._error = error

    def complete(self, *, system: str, user: str, max_tokens: int) -> str:
        if self._error is not None:
            raise self._error
        assert self._reply is not None
        return self._reply


def _adjudicate(chat: _FakeChat) -> bool:
    return ChatAdjudicator(chat)(
        ConceptMention(name="Alzheimer's"),
        NearestConcept(concept_id=1, name="Alzheimer's disease", distance=0.2),
    )


def test_explicit_same_true_merges():
    assert _adjudicate(_FakeChat('{"same": true}')) is True
    assert _adjudicate(_FakeChat('```json\n{"same": true}\n```')) is True


def test_anything_else_keeps_separate():
    assert _adjudicate(_FakeChat('{"same": false}')) is False
    assert _adjudicate(_FakeChat('{"same": "yes"}')) is False  # non-boolean
    assert _adjudicate(_FakeChat("{}")) is False               # missing field
    assert _adjudicate(_FakeChat("not json at all")) is False  # unparseable


def test_model_error_degrades_to_keep_separate():
    assert _adjudicate(_FakeChat(error=RuntimeError("model down"))) is False


def test_parse_same_is_strict_about_the_boolean():
    assert parse_same('{"same": true}') is True
    assert parse_same('{"same": 1}') is False
    assert parse_same('{"same": null}') is False
    assert parse_same("[]") is False
