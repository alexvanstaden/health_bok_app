"""Claude adapter for the StanceJudge port (issue #18).

Weighs one newly-arrived piece of knowledge (a Claim or Protocol) against one of
the owner's anchors (a Decision, Goal, or Marker) and returns the Stance — the LLM
pass that keeps the Impact inbox honest (CONTEXT.md "Stance"): Concept overlap made
them candidates, but only a judged stance becomes an Impact, so the owner sees
genuine change rather than everything merely-related.

The contract:

  * return exactly one of `reinforces | contradicts | refines | opportunity`, or
    `unrelated` to discard the pair,
  * `opportunity` is reserved for an anchor the knowledge opens a *new* option for
    (classically an unmet Goal with no serving Decision),
  * judge only this one pair — never invent anchors or knowledge.

The model returns strict JSON; the `impacts` engine is the backstop — it discards
any out-of-vocabulary stance — so a sloppy response can never mint an Impact the
table's CHECK would reject. The SDK is imported lazily, so importing the package
needs no anthropic install; the orchestrator only ever sees the `StanceJudge` port.
Mirrors the `ClaudeQueryAnswerer` adapter shape; the model is configurable (default
the same Claude model as the rest of the pipeline) via STANCE_MODEL.
"""

from __future__ import annotations

import json

from ..impacts import STANCES, UNRELATED
from ..models import ImpactAnchor, ImpactKnowledge

_MAX_TOKENS = 256

_ANCHOR_NOUN = {
    "decision": "a Decision the owner has made (something they currently do)",
    "goal": "a Goal the owner holds (something they want to achieve or a risk to address)",
    "marker": "a Marker reading the owner recorded (a lab value, with range)",
}

_SYSTEM = (
    "You are the change-detection judge for the owner's personal health & longevity "
    "knowledge graph. You are given ONE piece of newly-relevant knowledge (a Claim "
    "or Protocol drawn from a creator the owner follows) and ONE of the owner's own "
    "anchors (a Decision, Goal, or Marker). They already share a Concept. Decide how "
    "the knowledge bears on that anchor.\n\n"
    "Return exactly one stance:\n"
    "- reinforces: the knowledge supports / strengthens the anchor.\n"
    "- contradicts: the knowledge argues against the anchor or undercuts its basis.\n"
    "- refines: the knowledge qualifies, narrows, or adjusts the anchor (dose, "
    "timing, population, scope) without simply backing or opposing it.\n"
    "- opportunity: the knowledge opens a NEW option the owner is not yet acting on "
    "(typically a Goal with no Decision serving it yet).\n"
    "- unrelated: despite the shared Concept, the knowledge does not actually bear on "
    "this anchor. Prefer this over forcing a weak connection — the owner must not be "
    "flooded with merely-related noise.\n\n"
    "Respect scope qualifiers (e.g. 'in mice', 'in deficient individuals'): a Claim "
    "scoped away from the owner's situation is usually unrelated or at most refines.\n\n"
    "Respond with ONLY a JSON object of this exact shape, no prose:\n"
    '{"stance": str, "rationale": str}'
)


class ClaudeStanceJudge:
    """Judges the Stance of one knowledge↔anchor pair via the Claude API."""

    def __init__(self, api_key: str, model: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def judge(self, knowledge: ImpactKnowledge, anchor: ImpactAnchor) -> str:
        message = self._client.messages.create(
            model=self._model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM,
            messages=[
                {"role": "user", "content": render_pair(knowledge, anchor)},
            ],
        )
        raw = "".join(b.text for b in message.content if b.type == "text").strip()
        return parse_stance(raw)


def render_pair(knowledge: ImpactKnowledge, anchor: ImpactAnchor) -> str:
    """Render the pair as a prompt block, naming each side and its shared Concepts."""
    shared = sorted(set(knowledge.concepts) & set(anchor.concepts))
    overlap = ", ".join(shared) if shared else "(a Concept)"
    anchor_noun = _ANCHOR_NOUN.get(anchor.type, f"a {anchor.type}")
    return (
        f"NEW KNOWLEDGE ({knowledge.type}): {knowledge.text}\n"
        f"  concepts: {', '.join(knowledge.concepts) or '(none)'}\n\n"
        f"OWNER'S ANCHOR — {anchor_noun}: {anchor.text}\n"
        f"  concepts: {', '.join(anchor.concepts) or '(none)'}\n\n"
        f"They share: {overlap}.\n"
        "How does the new knowledge bear on the owner's anchor?"
    )


def parse_stance(raw: str) -> str:
    """Parse the model's JSON into a Stance, defaulting to `unrelated`.

    An unparseable response, a missing stance, or any out-of-vocabulary value all
    yield `unrelated` — the safe discard — so a sloppy judgement never mints an
    Impact (the engine and the table CHECK are the further backstops).
    """
    try:
        data = json.loads(_unfence(raw))
    except (json.JSONDecodeError, ValueError):
        return UNRELATED
    stance = str(data.get("stance", "")).strip().lower()
    return stance if stance in STANCES else UNRELATED


def _unfence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()
