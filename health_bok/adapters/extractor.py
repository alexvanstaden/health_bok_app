"""Extractor adapter over the ChatModel seam (ADR-0010, ADR-0012).

Turns one Transcript into the Body-of-Knowledge layer — Claims and Protocols with
proposed Concept mentions — under the precision-first contract (ADR-0010):

  * extract only the substantive, load-bearing assertions a creator argues,
  * preserve scope qualifiers verbatim ("in mice", "at 5g/day") — ADR-0002,
  * ground every assertion to a timestamp (seconds), or omit it,
  * emit a Protocol only when it is *structured* (action + at least one of
    dose/timing/frequency/duration); otherwise leave it a Claim.

The model is asked for strict JSON keyed to the `Extraction` shape; the locator is
a seconds offset into the Transcript, so the admit step can deep-link back to the
moment (`watch?v=ID&t=NNNs`). The provider call goes through an injected
`ChatModel`, so the adapter is provider-neutral — the orchestrator only ever sees
the `Extractor` port and the factory picks OpenAI or Anthropic.
"""

from __future__ import annotations

import json

from ..models import (
    ConceptMention,
    ExtractedClaim,
    ExtractedProtocol,
    Extraction,
    FetchedTranscript,
)
from ..ports import ChatModel

_MAX_TOKENS = 4096

_SYSTEM = (
    "You extract structured knowledge from health & longevity video transcripts "
    "for a personal knowledge graph. Be PRECISION-FIRST: capture only the "
    "substantive, load-bearing assertions the creator actually argues — never "
    "every remark, anecdote, or sponsor read.\n\n"
    "Rules:\n"
    "- Preserve scope qualifiers verbatim (e.g. 'in deficient individuals', "
    "'in mice', 'at 5g/day'). Never strip or generalize them.\n"
    "- Ground every item to the transcript: set `locator_seconds` to the integer "
    "second offset (from the [Ns] markers) where it is stated. If you cannot "
    "ground it, OMIT it — do not guess.\n"
    "- A `claim` is a single falsifiable assertion. Set `type` to one of "
    "'mechanism', 'principle', or 'finding'.\n"
    "- A `protocol` is a parameterized recommendation. Only emit one when it is "
    "STRUCTURED: an `action` plus at least one of `dose`, `timing`, `frequency`, "
    "`duration`. Vague advice with none of those is a claim, not a protocol.\n"
    "- For each item, list the Concept mentions it references (supplements, body "
    "systems, mechanisms, conditions, interventions) as short canonical names.\n\n"
    "Respond with ONLY a JSON object of this exact shape, no prose:\n"
    '{"claims":[{"text":str,"locator_seconds":int,"type":str,'
    '"concepts":[str]}],'
    '"protocols":[{"action":str,"dose":str|null,"timing":str|null,'
    '"frequency":str|null,"duration":str|null,"locator_seconds":int,'
    '"concepts":[str]}]}'
)


class ChatExtractor:
    """Extracts Claims and Protocols from a Transcript via an injected `ChatModel`."""

    def __init__(self, chat: ChatModel):
        self._chat = chat

    def extract(self, transcript: FetchedTranscript) -> Extraction:
        prov = transcript.provenance
        raw = self._chat.complete(
            system=_SYSTEM,
            user=(
                f"Video: {prov.title}\n"
                f"Channel: {prov.channel_name}\n\n"
                f"Transcript (each line prefixed with its start second):\n"
                f"{_timestamped(transcript)}"
            ),
            max_tokens=_MAX_TOKENS,
        )
        return parse_extraction(raw)


def _timestamped(transcript: FetchedTranscript) -> str:
    """Render the Transcript with `[Ns]` markers so the model can ground items."""
    return "\n".join(
        f"[{int(seg.start)}s] {seg.text}" for seg in transcript.segments
    )


def parse_extraction(raw: str) -> Extraction:
    """Parse the model's JSON into an `Extraction`, tolerating code fences.

    Ungrounded items (no integer locator) are dropped here too, so a sloppy model
    response can never inject a locator-less Claim past the contract; the admit
    step drops them again as a backstop.
    """
    data = json.loads(_unfence(raw))
    claims = [
        ExtractedClaim(
            text=c["text"],
            locator_seconds=_as_seconds(c.get("locator_seconds")),
            type=c.get("type", "finding"),
            concepts=_mentions(c.get("concepts")),
        )
        for c in data.get("claims", [])
        if c.get("text")
    ]
    protocols = [
        ExtractedProtocol(
            action=p["action"],
            locator_seconds=_as_seconds(p.get("locator_seconds")),
            dose=p.get("dose"),
            timing=p.get("timing"),
            frequency=p.get("frequency"),
            duration=p.get("duration"),
            concepts=_mentions(p.get("concepts")),
        )
        for p in data.get("protocols", [])
        if p.get("action")
    ]
    return Extraction(claims=claims, protocols=protocols)


def _unfence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _as_seconds(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mentions(names) -> list[ConceptMention]:
    return [ConceptMention(name=n) for n in (names or []) if n]
