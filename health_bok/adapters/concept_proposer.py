"""ConceptProposer adapter over the ChatModel seam (issue #39, ADR-0012).

Proposes the Concept *terms* a Goal concerns from its title + detail — the LLM half
of the "when should a new Concept be added?" assist. It only proposes short
canonical names; the caller checks each against the existing catalogue (the same
conservative `ConceptNormalizer` logic) and surfaces only the genuinely new ones,
which the owner confirms before anything is minted (ADR-0004). The model never
mints a Concept and never decides what already exists.

The contract:

  * return short canonical Concept names (supplements, body systems, mechanisms,
    conditions, interventions, markers) — the same shape the Extractor emits,
  * propose what the Goal is *about*, not generic life advice,
  * never prose, never a sentence — just the terms.

The model returns strict JSON; a sloppy or unparseable response degrades to an
empty list, so a bad answer simply yields no new-Concept suggestions rather than
failing the Goal page. The provider call goes through an injected `ChatModel`, so
the adapter is provider-neutral; the orchestrator only ever sees the
`ConceptProposer` port. Mirrors the `ChatExtractor`/`ChatStanceJudge` adapter
shape; the model is configurable (default the configured provider's) via
CONCEPT_PROPOSAL_MODEL.
"""

from __future__ import annotations

import json

from ..ports import ChatModel

_MAX_TOKENS = 256

_SYSTEM = (
    "You name the Concepts a personal health & longevity Goal concerns, for a "
    "knowledge graph. A Concept is a normalized hub — a supplement, body system, "
    "mechanism, condition, intervention, biomarker (e.g. 'apoB', 'zone 2 cardio', "
    "'rapamycin', 'VO2 max'). Given the owner's Goal (title + optional detail), "
    "propose the short canonical Concept names the Goal is about.\n\n"
    "Rules:\n"
    "- Propose what the Goal concerns, not generic advice ('be healthy') or the "
    "owner's action — just the Concepts.\n"
    "- Use short canonical names, lowercase unless a proper acronym; no sentences, "
    "no explanations.\n"
    "- Prefer precision over breadth: a handful of on-target Concepts, not a "
    "scattershot. An empty list is fine if the Goal names no clear Concept.\n\n"
    "Respond with ONLY a JSON object of this exact shape, no prose:\n"
    '{"concepts":[str]}'
)


class ChatConceptProposer:
    """Proposes candidate Concept terms for a Goal via an injected `ChatModel`."""

    def __init__(self, chat: ChatModel):
        self._chat = chat

    def propose(self, title: str, detail: str | None) -> list[str]:
        raw = self._chat.complete(
            system=_SYSTEM,
            user=render_goal(title, detail),
            max_tokens=_MAX_TOKENS,
        )
        return parse_concepts(raw)


def render_goal(title: str, detail: str | None) -> str:
    """Render the Goal as a prompt block — the same title + detail the owner wrote."""
    block = f"GOAL: {title}"
    if detail and detail.strip():
        block += f"\nDETAIL: {detail.strip()}"
    return block + "\n\nWhich Concepts does this Goal concern?"


def parse_concepts(raw: str) -> list[str]:
    """Parse the model's JSON into a list of candidate terms, tolerating code fences.

    An unparseable response, a missing list, or a non-list value all yield ``[]`` —
    the safe degrade — so a sloppy answer simply proposes no new Concepts. Blank
    entries are dropped and surrounding whitespace trimmed; de-duplication and the
    existing-Concept check are the caller's job.
    """
    try:
        data = json.loads(_unfence(raw))
    except (json.JSONDecodeError, ValueError):
        return []
    concepts = data.get("concepts") if isinstance(data, dict) else None
    if not isinstance(concepts, list):
        return []
    return [c.strip() for c in concepts if isinstance(c, str) and c.strip()]


def _unfence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()
