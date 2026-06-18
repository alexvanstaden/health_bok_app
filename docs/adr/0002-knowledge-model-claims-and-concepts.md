# Knowledge model: claim-level granularity, claims never merged, connected via Concepts

The atomic unit of the Body of Knowledge is the individual **Claim** — a single falsifiable
assertion with its own provenance — not the video or its summary. The core queries
(cross-creator contradictions, what supports a Decision, what new content challenges it) are
impossible at coarser granularity.

Claims are **never destructively merged**: merging is lossy and irreversible and silently
flattens nuance (e.g. a "in deficient individuals" qualifier) that may later matter for a
Decision. Instead, relatedness / agreement / contradiction are *computed* by traversing a
shared, normalized **Concept** layer — which, unlike Claims, *may* be merged, since
normalization is its whole purpose.

The impersonal Body of Knowledge (Claims, Protocols) is kept structurally separate from the
personal layer (Decisions, Goals, Status). The personal "why" lives only on Decisions; a
Protocol is what a creator recommends, a Decision is the owner adopting it.

**Amended by ADR-0013**: lateral Concept↔Concept relatedness is now *materialized* (a typed
`concept_relations` projection), not only computed at query time. It is still derived from
Claims and still never a destructive merge — the materialization is an always-true index over
the Claims, which vanishes when its evidence does.
