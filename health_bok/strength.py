"""Relationship Strength: distinct creators, owner-tiered, recency-decayed (ADR-0013).

A lateral relationship's **Strength** is how well-supported it is — what ranks the
neighbourhood view, gates Tier-2 notability, and weighs a contradiction ("5 creators
say helps, 1 says no effect"). It is deliberately *not* a raw Claim count: one
prolific channel repeating itself must not manufacture false consensus (the echo-
chamber failure a health system has to resist, ADR-0013). So:

    Strength = Σ over *distinct creators* of (owner trust-tier × recency-decay)

  * **distinct creators** — a creator who asserts a relationship across ten videos
    counts once, weighted by their single most-recent contribution.
  * **owner trust-tier** — a per-creator integer the owner sets (default 1), so
    trusted sources carry more weight. Untiered ⇒ tier 1 ⇒ Strength is a plain
    distinct-creator count, useful from day one (user story 29).
  * **recency-decay** — exponential decay on the contribution's age, so stale
    consensus gradually yields to newer evidence (user story 28).

Pure and side-effect-free, so it is unit-tested in isolation and reused by the
repository's neighbourhood query without dragging the store into the maths.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Recency-decay half-life: a contribution this many days old counts half as much.
# A tuning knob left to implementation (ADR-0013), not a fixed design decision.
DEFAULT_HALF_LIFE_DAYS = 365.0


@dataclass(frozen=True)
class EvidenceContribution:
    """One Claim's contribution to a relationship's Strength (ADR-0013).

    Attributed to its `creator_id` (resolved Claim → video → creator) so distinct
    creators can be counted; `trust_tier` is the owner's weight on that creator and
    `dated` is when the evidence was published, for recency-decay.
    """

    creator_id: int
    trust_tier: int
    dated: datetime


def recency_decay(age_days: float, half_life_days: float) -> float:
    """Exponential recency-decay in [0, 1]: `0.5 ** (age / half_life)` (ADR-0013).

    Today's evidence weighs ~1.0; evidence one half-life old weighs 0.5. A
    negative age (a future timestamp, e.g. clock skew) is clamped to 0, so it never
    *amplifies* beyond the present.
    """
    return 0.5 ** (max(age_days, 0.0) / half_life_days)


def relation_strength(
    contributions: list[EvidenceContribution],
    *,
    now: datetime,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> float:
    """Σ over distinct creators of (trust-tier × recency-decay) (ADR-0013).

    Each distinct creator contributes a single term, weighted by their most-recent
    contribution — so a creator's back-catalogue cannot inflate Strength, and a
    creator who said it recently outweighs one who said it long ago.
    """
    latest = _latest_per_creator(contributions)
    total = 0.0
    for trust_tier, dated in latest.values():
        age_days = (now - dated).total_seconds() / 86400.0
        total += trust_tier * recency_decay(age_days, half_life_days)
    return total


def distinct_creator_count(contributions: list[EvidenceContribution]) -> int:
    """How many distinct creators evidence a relationship (ADR-0013)."""
    return len({c.creator_id for c in contributions})


def _latest_per_creator(
    contributions: list[EvidenceContribution],
) -> dict[int, tuple[int, datetime]]:
    """Most-recent (trust_tier, date) per creator — collapses a back-catalogue to one."""
    latest: dict[int, tuple[int, datetime]] = {}
    for c in contributions:
        current = latest.get(c.creator_id)
        if current is None or c.dated > current[1]:
            latest[c.creator_id] = (c.trust_tier, c.dated)
    return latest
