"""The Claude Extractor's JSON parsing (ADR-0010) — pure, no network.

`parse_extraction` turns the model's JSON into an `Extraction`. It must tolerate
code fences, carry scope qualifiers through untouched, keep the dose/timing
structure that distinguishes a Protocol from a Claim, and drop any item the model
failed to ground — a backstop on the precision-first contract.
"""

from __future__ import annotations

from health_bok.adapters.extractor import parse_extraction


def test_parses_fenced_json_with_claims_and_protocols():
    raw = """```json
    {
      "claims": [
        {"text": "Rapamycin extends lifespan in mice.",
         "locator_seconds": 300, "type": "finding",
         "concepts": ["rapamycin", "lifespan"]}
      ],
      "protocols": [
        {"action": "Take creatine monohydrate", "dose": "5g",
         "timing": "morning", "frequency": "daily", "duration": null,
         "locator_seconds": 420, "concepts": ["creatine monohydrate"]}
      ]
    }
    ```"""
    extraction = parse_extraction(raw)

    assert len(extraction.claims) == 1
    claim = extraction.claims[0]
    assert claim.text == "Rapamycin extends lifespan in mice."  # qualifier preserved
    assert claim.locator_seconds == 300
    assert [c.name for c in claim.concepts] == ["rapamycin", "lifespan"]

    assert len(extraction.protocols) == 1
    protocol = extraction.protocols[0]
    assert protocol.is_structured
    assert protocol.dose == "5g" and protocol.duration is None


def test_drops_ungrounded_items():
    raw = (
        '{"claims": [{"text": "No locator here.", "type": "finding"}], '
        '"protocols": []}'
    )
    extraction = parse_extraction(raw)
    # The Claim survives parsing but is marked ungrounded -> the admit step drops it.
    assert extraction.claims[0].is_grounded is False


def test_parses_directed_triples_and_normalizes_predicate():
    raw = """{
      "claims": [
        {"text": "APOE4 raises Alzheimer's risk.", "locator_seconds": 60,
         "type": "finding", "concepts": ["APOE4", "Alzheimer's"],
         "triples": [{"subject": "APOE4", "predicate": "Risk Factor For",
                      "object": "Alzheimer's"}]}
      ],
      "protocols": []
    }"""
    [claim] = parse_extraction(raw).claims
    [triple] = claim.triples
    assert triple.subject.name == "APOE4"
    assert triple.object.name == "Alzheimer's"
    # "Risk Factor For" is normalized onto the canonical signed predicate.
    assert triple.predicate == "risk_factor_for"


def test_unclear_or_unknown_predicate_falls_back_to_associated_with():
    raw = """{
      "claims": [
        {"text": "Magnesium is somehow tied to sleep.", "locator_seconds": 10,
         "type": "finding", "concepts": ["magnesium", "sleep"],
         "triples": [
            {"subject": "magnesium", "predicate": "vaguely_relates_to", "object": "sleep"},
            {"subject": "magnesium", "object": "deep sleep"}
         ]}
      ],
      "protocols": []
    }"""
    [claim] = parse_extraction(raw).claims
    # The out-of-vocabulary predicate AND the missing predicate both fall back —
    # a real connection is never lost just because its label was unclear.
    assert [t.predicate for t in claim.triples] == ["associated_with", "associated_with"]


def test_drops_triple_missing_an_endpoint():
    raw = """{
      "claims": [
        {"text": "Half a triple.", "locator_seconds": 5, "type": "finding",
         "concepts": ["zinc"],
         "triples": [{"subject": "zinc", "predicate": "increases"}]}
      ],
      "protocols": []
    }"""
    [claim] = parse_extraction(raw).claims
    assert claim.triples == []  # a relationship needs both ends
