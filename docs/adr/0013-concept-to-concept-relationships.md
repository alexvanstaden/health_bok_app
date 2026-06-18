# Concepts connect to each other: claim-grounded lateral relationships and a curated taxonomy

Until now Concepts were isolated hubs (ADR-0008): Claims, Protocols, Goals, Markers, and
Decisions *reference* a Concept, but no edge ever ran Concept→Concept. Relatedness was only
ever *computed* — co-reference through a shared hub plus pgvector proximity (ADR-0002) — and
never surfaced as a thing in its own right. The owner cannot ask "what is APOE4 connected to,
and how", cannot roll near-duplicate hubs (Brain, Brain metabolism, Brain waste…) up under a
broader one, and is never *alerted* when newly-arrived knowledge connects two Concepts they
care about. Connecting Concepts — and noticing when those connections change — is the point of
the system, so it must become first-class.

This pins how. Two genuinely different links fall out, with different provenance and different
lifecycles, and conflating them was the trap.

## Decision

Concepts gain **two distinct families** of Concept→Concept link.

### Lateral relationships — claim-grounded, derived, self-healing

- A **lateral relationship** is an evidence-bearing link ("APOE4 `risk_factor_for`
  Alzheimer's"). Its **truth comes only from the owner's Claims** — never the model's general
  knowledge (ADR-0011). No Claim references both Concepts ⇒ no edge; delete/supersede the last
  evidencing Claim ⇒ the edge vanishes. A lateral edge is exactly as falsifiable and citable as
  the Claims beneath it.
- It is therefore a **materialized projection of Claims, not a curated object** — auto-derived
  at admit time and recomputed on supersede/delete (ADR-0005). Zero edge-curation; the graph
  cannot drift from the Claims.
- Stored in a typed **`concept_relations`** table (`src_concept_id, predicate, dst_concept_id`,
  directed), with an **evidence link back to the Claims** that assert it — not in the
  polymorphic `edges` table, whose `kind` CHECK would balloon under the predicate set
  (see ADR-0008 amendment).
- The Extractor (ADR-0010) is extended to emit, per Claim, directed
  **(subject Concept, predicate, object Concept)** triples — not just today's flat mention list.
  Precision-first: when unsure of a specific predicate it falls back to the signless
  `associated_with`, so co-reference still yields an edge, upgraded to a precise label when a
  clearer Claim arrives.

### Predicate vocabulary — signed names, polarity as data

- A **lean (~10), grouped, extensible** vocabulary of **signed predicate names**
  (`protects_against ⟷ risk_factor_for`, `increases ⟷ decreases`, `treats ⟷ worsens`),
  plus signless `biomarker_of` / `measured_by` / `mechanism_of`, plus `associated_with` (the
  confidence fallback). Signed names keep the graph legible.
- **Contradiction is derived, not merged** (ADR-0002): a small **opposite-pairs lookup** (data,
  not code), **plus one rule** — `no_effect_on` contradicts *any* signed predicate on the same
  pair. This rescues the most valuable debunking case ("X helps" vs "X does nothing"). Signless
  predicates never contradict.
- The vocabulary grows only when a real Claim needs a predicate that isn't there.

### Hierarchy — a curated taxonomy over the same Concepts

- **Roll-up is a taxonomic `broader-of` edge among Concepts**, not claim-grounded — no creator
  asserts "Brain waste is part of Brain". A high-level node like **Brain is itself a Concept**
  (it can carry children, parents, and its own lateral edges); no new entity type.
- It is a **DAG, multi-parent, acyclic** (a cycle guard rejects edges that would close a loop):
  APOE4 legitimately rolls up under genetics *and* lipid metabolism *and* Alzheimer's.
- Because it is judgement, not evidence, it is the **one link the owner curates**: the system
  **proposes** parents (embedding clustering + LLM, reusing the Concept-suggester pattern of
  issue #39 and the approval gate of ADR-0004), and a proposed `broader-of` stays a *suggestion*
  — invisible to roll-up — until the owner confirms.
- `broader-of` lives in the existing `edges` table (one new `kind`); it is a structural edge, so
  it fits the polymorphic table that lateral predicates do not.

### Viewing — roll-up neighbourhood

- Selecting a Concept shows its sub-Concepts **and every lateral edge in its whole subtree**,
  surfaced at the selected Concept, **attributed** ("via Brain metabolism, `impaired_by`"),
  **deduped** across DAG diamonds, and **ranked by strength**.

### Strength — distinct creators, owner-tiered, recency-decayed

- A relationship's strength = **Σ over distinct creators of (owner trust-tier × recency-decay)**.
  Distinct creators (`claim → video → creator`) resist echo chambers — one prolific creator
  counts once; an owner-set **trust-tier on the creator** supplies "quality" honestly for a
  personal system; recency decays stale consensus.
- Degrades gracefully: before any creator is tiered, every creator is tier 1 and strength is
  plain distinct-creator count. Strength drives roll-up ranking, Tier-2 notability, and
  contradiction weighting ("5 creators say helps, 1 says no effect").

### Alerting — one Impact inbox, two tiers

- Reuses the **Impact** entity and its `new → reviewed → actioned | dismissed` lifecycle — not a
  second inbox.
- **Tier-1 (push)**: a new/changed lateral edge that touches a Concept referenced by the owner's
  Goals/Decisions **or anything in that Concept's hierarchy subtree** raises an Impact.
- **Tier-2 (pull)**: all other notable structural changes (gated by a strength threshold) go to a
  browsable feed, not the inbox.
- Stances = the existing four **plus `new_link`** (a sign-neutral connection appeared) **and
  `eroded`** (a relationship a Decision relied on lost its last evidence) — events the
  evidence-vs-Decision vocabulary cannot express. Relationship stances are **derived
  structurally from the graph diff** where possible (new edge, newly-contested pair, evidence
  count hitting zero), so most need no LLM pass.
- **Scope-widening** — recording a Goal/Decision, or confirming a `broader-of` edge that pulls a
  subtree under a tracked Concept — fires **one summary Tier-1** for the pre-existing backlog
  ("12 existing connections now under Brain — review?"), then stays quiet; only edges arriving
  afterwards push.

## Considered Options

- **Relationships from the model's general knowledge** — rejected: it would let edges exist with
  no Claim behind them, breaking the strict-grounding contract (ADR-0011) that defines the app.
- **One unified edge family** (`part_of` as just another claim-grounded predicate) — rejected:
  the hierarchy would stay perpetually sparse, because creators rarely assert taxonomic claims.
- **A separate `sign` dimension** on signless relationship-type predicates (cleaner 3-way
  contradiction, smaller vocab) — considered, but signed names won for graph legibility; the
  `no_effect_on` rule recovers the 3-way case the pair model otherwise misses.
- **Contradiction-groups** (mutually-exclusive predicate domains) — considered; simple
  opposite-pairs + the one `no_effect_on` rule is enough at this scale.
- **Strict single-parent tree** — rejected: forces a false single home; health Concepts belong
  under several broader ones.
- **Lateral predicates in `edges.kind`** — rejected: the predicate set is far larger and more
  volatile than the structural edge kinds; a dedicated typed table keeps the polymorphic CHECK
  small (the same reasoning ADR-0008 used to keep ownership out of `edges`).
- **A separate relationship inbox** — rejected: two lifecycles and two "don't re-nag"
  mechanisms; the Impact inbox already does this.
- **Fully automatic or fully manual hierarchy** — rejected: auto silently corrupts every
  roll-up beneath a wrong parent; manual leaves Concepts orphaned as they outgrow curation.
  Propose-and-confirm splits the difference.
- **Strength by raw claim count** — rejected: one channel's back-catalogue manufactures false
  consensus, the echo-chamber failure a health system must resist. **LLM-rated source quality** —
  deferred: no quality metadata exists yet and an LLM grading medical evidence is fraught; layer
  it on once owner-tiered creator strength is in.

## Consequences

- **Amends ADR-0002**: relatedness is no longer *only* computed — lateral relationships are now
  *materialized* (still derived from Claims, still never a destructive merge; the materialization
  is an always-true index over the Claims).
- **Extends ADR-0008**: adds the `concept_relations` table + Claim-evidence links, the
  `broader-of` edge `kind`, and a trust-tier column on `creators`. pgvector normalization
  (`concepts.py`) is unchanged and is reused by the hierarchy proposer.
- **Extends ADR-0010**: extraction now also emits directed predicate triples, under the same
  precision-first / `associated_with`-fallback contract.
- **Ties into ADR-0005**: supersede/delete recomputes affected lateral edges; an edge losing its
  last evidence raises an `eroded` Impact rather than silently disappearing under a Decision.
- **Reuses ADR-0012's `ChatModel` seam** for both the predicate-triple extraction and the
  hierarchy parent-proposer; the embedder stays OpenAI-only (ADR-0008).
- **Tuning knobs left to implementation, not design**: the Tier-2 strength threshold, the
  recency-decay half-life, the roll-up view limit, and the trust-tier scale.
