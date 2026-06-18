"""HierarchyProposer adapter over the ChatModel seam (ADR-0013, ADR-0012).

Proposes the broader `broader-of` parents a Concept should roll up under — the LLM
half of the hierarchy assist (ADR-0013). Mirrors `ChatConceptProposer`: given a
Concept name and a short list of nearby existing Concepts (the caller's pgvector
cluster), it returns which of those are *broader*, drawing only from the nearby set
so a proposal always names a Concept that already exists. The owner confirms before
roll-up sees the edge (ADR-0004); the model never confirms.

The model returns strict JSON; a sloppy or unparseable response degrades to an
empty list, so a bad answer simply yields no suggestions rather than breaking the
page. The provider call goes through an injected `ChatModel`, so the adapter is
provider-neutral; the model is configurable via HIERARCHY_PROPOSAL_MODEL.
"""

from __future__ import annotations

import json

from ..ports import ChatModel

_MAX_TOKENS = 256

_SYSTEM = (
    "You organize a personal health & longevity knowledge graph into a taxonomy. "
    "Every node is a Concept — a supplement, body system, mechanism, condition, "
    "intervention, or biomarker. Given one Concept and a list of NEARBY existing "
    "Concepts, pick which nearby Concepts are genuinely BROADER than it — the ones "
    "it should roll up under (e.g. 'Brain' is broader than 'Brain metabolism'; "
    "'genetics' and 'lipid metabolism' are both broader than 'APOE4').\n\n"
    "Rules:\n"
    "- Choose ONLY from the nearby list; never invent a Concept.\n"
    "- Choose only genuinely broader parents, not siblings, narrower Concepts, or "
    "merely-related ones. A Concept may have several broader parents, or none.\n"
    "- Return the parents' canonical names exactly as given; no prose, no "
    "explanations.\n\n"
    "Respond with ONLY a JSON object of this exact shape, no prose:\n"
    '{"parents":[str]}'
)


class ChatHierarchyProposer:
    """Proposes broader parents for a Concept via an injected `ChatModel`."""

    def __init__(self, chat: ChatModel):
        self._chat = chat

    def propose(self, concept_name: str, nearby: list[str]) -> list[str]:
        if not nearby:
            return []
        raw = self._chat.complete(
            system=_SYSTEM,
            user=render_concept(concept_name, nearby),
            max_tokens=_MAX_TOKENS,
        )
        return parse_parents(raw, nearby)


def render_concept(concept_name: str, nearby: list[str]) -> str:
    """Render the Concept and its nearby cluster as a prompt block."""
    return (
        f"CONCEPT: {concept_name}\n"
        f"NEARBY CONCEPTS: {', '.join(nearby)}\n\n"
        "Which nearby Concepts are broader than this one?"
    )


def parse_parents(raw: str, nearby: list[str]) -> list[str]:
    """Parse the model's JSON into broader-parent names, tolerating code fences.

    An unparseable response, a missing list, or a non-list value all yield ``[]`` —
    the safe degrade. Parents are constrained to the `nearby` set case-insensitively
    (the model is told to choose only from it), so a hallucinated name is dropped
    and the surviving names are returned in their canonical `nearby` spelling.
    """
    try:
        data = json.loads(_unfence(raw))
    except (json.JSONDecodeError, ValueError):
        return []
    parents = data.get("parents") if isinstance(data, dict) else None
    if not isinstance(parents, list):
        return []
    allowed = {n.lower(): n for n in nearby}
    out: list[str] = []
    seen: set[str] = set()
    for p in parents:
        if not isinstance(p, str):
            continue
        key = p.strip().lower()
        if key in allowed and key not in seen:
            seen.add(key)
            out.append(allowed[key])
    return out


def _unfence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()
