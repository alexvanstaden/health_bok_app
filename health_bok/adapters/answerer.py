"""QueryAnswerer adapter over the ChatModel seam (ADR-0011, ADR-0012).

Turns the owner's question plus the retrieved evidence into a grounded, cited
answer, under the cite-or-abstain contract (ADR-0011):

  * answer *only* from the supplied evidence — the owner's Claims, Protocols, and
    personal layer — never the model's own general medical knowledge,
  * cite the specific Claims the answer rests on by their `[claim N]` id,
  * abstain when the evidence does not actually answer the question, rather than
    confabulate.

The evidence is rendered with each Claim tagged by id so the model can cite it;
the model returns strict JSON keyed to the `GroundedAnswer` shape. The query
service is the backstop — it resolves cited ids against the retrieved evidence and
enforces cite-or-abstain — so a sloppy response can never smuggle in an ungrounded
citation. The provider call goes through an injected `ChatModel`, so the adapter is
provider-neutral; the orchestrator only ever sees the `QueryAnswerer` port. Mirrors
the `ChatExtractor`/`ChatSummarizer` adapter shape; the model is configurable
(default the configured provider's) via QUERY_MODEL.
"""

from __future__ import annotations

import json

from ..models import EvidenceMarker, GroundedAnswer, RetrievedEvidence
from ..ports import ChatModel

_MAX_TOKENS = 1024

_SYSTEM = (
    "You answer the owner's health & longevity questions STRICTLY from their own "
    "curated library — the evidence provided in the message, and nothing else. The "
    "evidence is the owner's Body of Knowledge (Claims and Protocols drawn from "
    "creators they follow) and their personal layer (Goals, Markers, Decisions).\n\n"
    "Rules:\n"
    "- Ground every statement in the supplied evidence. NEVER use your own general "
    "medical or scientific knowledge, and never blend it in.\n"
    "- Cite the specific Claims your answer rests on by their id (the number in "
    "`[claim N]`). Put every id you used in `claim_ids`.\n"
    "- Preserve scope qualifiers from the Claims verbatim (e.g. 'in mice', 'in "
    "deficient individuals'); never strip or generalize them.\n"
    "- You may use Protocols and the personal layer (the owner's Markers, "
    "Decisions, Goals) for context to make the answer actionable, but the citations "
    "are Claims.\n"
    "- If the evidence does not actually answer the question, ABSTAIN: set "
    '"abstain" to true and leave "answer" empty. Do not guess.\n'
    "- Write a synthesized prose answer, not a list of the Claims.\n\n"
    "Respond with ONLY a JSON object of this exact shape, no prose:\n"
    '{"answer": str, "claim_ids": [int], "abstain": bool}'
)


class ChatQueryAnswerer:
    """Answers a grounded, cited question over retrieved evidence via a `ChatModel`."""

    def __init__(self, chat: ChatModel):
        self._chat = chat

    def answer(self, question: str, evidence: RetrievedEvidence) -> GroundedAnswer:
        raw = self._chat.complete(
            system=_SYSTEM,
            user=(
                f"Question: {question}\n\n"
                f"Evidence from the owner's library:\n{render_evidence(evidence)}"
            ),
            max_tokens=_MAX_TOKENS,
        )
        return parse_answer(raw)


def render_evidence(evidence: RetrievedEvidence) -> str:
    """Render the retrieved evidence as a prompt block, tagging Claims by id.

    Each Claim is labelled `[claim N]` so the model can cite it; Protocols and the
    personal layer are labelled by section so the model can use them as context
    without mistaking them for citation targets.
    """
    sections: list[str] = []

    if evidence.claims:
        lines = [
            f"[claim {c.id}] ({c.type}) {c.text}"
            + (f" — concepts: {', '.join(c.concepts)}" if c.concepts else "")
            for c in evidence.claims
        ]
        sections.append("CLAIMS (cite these by id):\n" + "\n".join(lines))

    if evidence.protocols:
        lines = [f"- {_protocol_line(p)}" for p in evidence.protocols]
        sections.append("PROTOCOLS (recommendations — context only):\n" + "\n".join(lines))

    if evidence.goals:
        lines = [
            f"- {g.title}" + (f": {g.detail}" if g.detail else "") for g in evidence.goals
        ]
        sections.append("YOUR GOALS:\n" + "\n".join(lines))

    if evidence.markers:
        lines = [f"- {_marker_line(m)}" for m in evidence.markers]
        sections.append("YOUR LATEST MARKER READINGS:\n" + "\n".join(lines))

    if evidence.decisions:
        lines = [f"- {_decision_line(d)}" for d in evidence.decisions]
        sections.append("YOUR DECISIONS (what you currently do):\n" + "\n".join(lines))

    return "\n\n".join(sections) if sections else "(no evidence)"


def parse_answer(raw: str) -> GroundedAnswer:
    """Parse the model's JSON into a `GroundedAnswer`, tolerating code fences.

    An explicit abstention, an empty answer, or an unparseable response all yield
    an abstaining `GroundedAnswer` — never a fabricated one. Non-integer citation
    ids are dropped here; the query service drops any that were not retrieved.
    """
    try:
        data = json.loads(_unfence(raw))
    except (json.JSONDecodeError, ValueError):
        return GroundedAnswer(text="", cited_claim_ids=[], abstained=True)
    answer = (data.get("answer") or "").strip()
    if data.get("abstain") or not answer:
        return GroundedAnswer(text="", cited_claim_ids=[], abstained=True)
    return GroundedAnswer(
        text=answer,
        cited_claim_ids=_as_ids(data.get("claim_ids")),
        abstained=False,
    )


def _protocol_line(p) -> str:
    params = ", ".join(
        f"{label} {value}"
        for label, value in (
            ("dose", p.dose),
            ("timing", p.timing),
            ("frequency", p.frequency),
            ("duration", p.duration),
        )
        if value
    )
    return p.action + (f" ({params})" if params else "")


def _marker_line(m: EvidenceMarker) -> str:
    flag = " — OUT OF RANGE" if m.out_of_range else ""
    return f"{m.concept}: {m.value} {m.unit} on {m.measured_at.date()}{flag}"


def _decision_line(d) -> str:
    params = ", ".join(
        v for v in (d.dose, d.timing, d.frequency, d.duration) if v
    )
    return d.action + (f" ({params})" if params else "")


def _unfence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _as_ids(values) -> list[int]:
    ids: list[int] = []
    for value in values or []:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            continue
    return ids
