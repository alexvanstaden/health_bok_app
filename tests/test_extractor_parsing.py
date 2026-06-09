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
