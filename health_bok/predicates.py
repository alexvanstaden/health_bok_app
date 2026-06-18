"""The lateral-relationship predicate vocabulary and its polarity (ADR-0013).

A lateral relationship's `predicate` names *how* two Concepts relate ("APOE4
`risk_factor_for` Alzheimer's"). The vocabulary is deliberately **lean, grouped,
signed, and extensible** (ADR-0013): signed names keep the graph legible, and the
sign is carried *as data here* — a small opposite-pairs lookup — rather than a
separate column, so contradiction is *derived*, never merged (ADR-0002).

Three groups:

  * **signed** — directional value judgements that come in opposite pairs
    (`protects_against ⟷ risk_factor_for`, `increases ⟷ decreases`,
    `treats ⟷ worsens`). Two opposite signed predicates on the *same ordered pair*
    contradict.
  * **`no_effect_on`** — the debunking predicate. It contradicts *any* signed
    predicate on the same pair, rescuing the most common refutation ("the trial
    showed nothing") that an opposite-pairs model alone would miss.
  * **signless** — relationship *types* with no valence (`biomarker_of`,
    `measured_by`, `mechanism_of`). These never contradict.

`associated_with` is the precision-first fallback (ADR-0010): when the Extractor
is unsure of a specific predicate, a real connection is still captured as a
generic, signless link rather than lost. The vocabulary grows only when a real
Claim needs a predicate that isn't here; this module is the single place to add
one (plus the `concept_relations` CHECK in `schema.sql`).
"""

from __future__ import annotations

from itertools import combinations
from typing import Iterable

# The confidence fallback: a genuine but untyped connection (ADR-0010). Signless.
ASSOCIATED_WITH = "associated_with"

# The debunking predicate: "X has no effect on Y". Contradicts any *signed*
# predicate on the same ordered pair (ADR-0013), but is not itself signed.
NO_EFFECT_ON = "no_effect_on"

# Signed opposite pairs — the valence carried as data (ADR-0013). Each maps to its
# polar opposite; two opposite predicates on the same ordered (src, dst) contradict.
_OPPOSITES: dict[str, str] = {
    "protects_against": "risk_factor_for",
    "risk_factor_for": "protects_against",
    "increases": "decreases",
    "decreases": "increases",
    "treats": "worsens",
    "worsens": "treats",
}

# Signless relationship *types* — a connection with no valence, so never a
# contradiction (the contested view stays meaningful, user story 13).
_SIGNLESS = frozenset({"biomarker_of", "measured_by", "mechanism_of", ASSOCIATED_WITH})

SIGNED = frozenset(_OPPOSITES)

# The whole admissible vocabulary — the `concept_relations.predicate` CHECK mirrors
# this exact set (schema.sql). `no_effect_on` is admissible but neither signed nor
# (for contradiction purposes) signless: it has its own rule below.
VOCABULARY = SIGNED | _SIGNLESS | {NO_EFFECT_ON}


def normalize_predicate(raw: str | None) -> str:
    """Map a model-proposed predicate onto the vocabulary, falling back precision-first.

    An empty, unknown, or unclear predicate becomes `associated_with` (ADR-0010):
    a real connection is never dropped just because its precise label was unclear,
    and a sloppy model can never inject an out-of-vocabulary predicate the
    `concept_relations` CHECK would reject anyway.
    """
    if not raw:
        return ASSOCIATED_WITH
    candidate = raw.strip().lower().replace(" ", "_").replace("-", "_")
    return candidate if candidate in VOCABULARY else ASSOCIATED_WITH


def contradicts(predicate_a: str, predicate_b: str) -> bool:
    """Whether two predicates on the *same ordered (src, dst) pair* disagree (ADR-0013).

    Derived, never merged (ADR-0002): two signed predicates contradict when they
    are polar opposites, and `no_effect_on` contradicts *any* signed predicate.
    Signless predicates — and a predicate against itself — never contradict, so the
    contested view stays meaningful.
    """
    if predicate_a == NO_EFFECT_ON:
        return predicate_b in SIGNED
    if predicate_b == NO_EFFECT_ON:
        return predicate_a in SIGNED
    return _OPPOSITES.get(predicate_a) == predicate_b and predicate_b in SIGNED


def tensions(predicates: Iterable[str]) -> list[tuple[str, str]]:
    """The contradicting predicate pairs among `predicates` — the "tension" itself.

    Given every predicate asserted on *one ordered (src, dst) pair*, return each
    unordered combination that `contradicts` (an opposite signed pair, or
    `no_effect_on` against any signed predicate), as a sorted, deduped tuple. An
    empty result means the pair is concordant; a non-empty one names exactly which
    predicates are in tension — derived, never merged (ADR-0002). Pure, so the
    polarity logic is unit-testable without a database (ADR-0013 testing decisions).
    """
    distinct = sorted(set(predicates))
    return [(a, b) for a, b in combinations(distinct, 2) if contradicts(a, b)]
