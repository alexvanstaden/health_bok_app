"""Concept-merge Adjudicator over the ChatModel seam (ADR-0008, ADR-0012).

The optional LLM tie-breaker `ConceptNormalizer` consults inside its adjudication
band (0.15–0.30 cosine): the embedding says "plausibly the same Concept, but
unsure", and this decides. Conservative by contract — it merges only when the two
names clearly denote the *same* thing (a synonym, abbreviation, trivial rewording,
or singular/plural), never a narrower-into-broader or a merely-related pair. A
sloppy or unparseable answer, or a provider error, degrades to ``False`` (keep them
separate), because a wrong merge silently corrupts the graph while a spurious new
Concept is cheap to merge later (ADR-0010). The provider call goes through an
injected `ChatModel`, so the adapter is provider-neutral (ADR-0012).
"""

from __future__ import annotations

import json
import logging

from ..models import ConceptMention
from ..ports import ChatModel
from ..repository import NearestConcept

logger = logging.getLogger("health_bok.adapters.adjudicator")

_MAX_TOKENS = 64

_SYSTEM = (
    "You deduplicate a personal health & longevity knowledge graph. Each node is a "
    "Concept — a supplement, body system, mechanism, condition, intervention, or "
    "biomarker. Given TWO Concept names, decide whether they denote the SAME Concept "
    "and should be merged into one hub.\n\n"
    "Merge ONLY when they clearly refer to the same thing: a synonym, an "
    "abbreviation, a trivial rewording, or singular/plural (e.g. 'Alzheimer's "
    "disease' and 'Alzheimer's'; 'apoB' and 'apolipoprotein B'). Do NOT merge a "
    "narrower Concept into a broader one, a part into a whole, or two merely-related "
    "Concepts (e.g. 'vitamin D' vs 'vitamin D3'; 'Alzheimer's disease' vs "
    "'Alzheimer's prevention' — different). When unsure, do NOT merge.\n\n"
    "Respond with ONLY a JSON object of this exact shape, no prose:\n"
    '{"same":bool}'
)


class ChatAdjudicator:
    """Decides whether a mention is the same Concept as its nearest match.

    Matches the `Adjudicator` callable contract in `concepts.py`
    (``(ConceptMention, NearestConcept) -> bool``): the normalizer only calls it for
    a near-match in the adjudication band, and treats a ``True`` as "merge".
    """

    def __init__(self, chat: ChatModel):
        self._chat = chat

    def __call__(self, mention: ConceptMention, nearest: NearestConcept) -> bool:
        try:
            raw = self._chat.complete(
                system=_SYSTEM,
                user=render_pair(mention.name, nearest.name),
                max_tokens=_MAX_TOKENS,
            )
        except Exception:
            logger.exception("adjudicator model call failed for %r", mention.name)
            return False
        return parse_same(raw)


def render_pair(a: str, b: str) -> str:
    """Render the two Concept names as the adjudication prompt block."""
    return f"CONCEPT A: {a}\nCONCEPT B: {b}\n\nAre A and B the same Concept?"


def parse_same(raw: str) -> bool:
    """Parse the model's JSON into a merge/keep-separate decision, tolerating fences.

    Anything but an explicit ``{"same": true}`` — an unparseable body, a missing or
    non-boolean field, a truthy-but-not-``True`` value — yields ``False``, the safe
    conservative default (keep the Concepts separate).
    """
    try:
        data = json.loads(_unfence(raw))
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("same") is True


def _unfence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()
