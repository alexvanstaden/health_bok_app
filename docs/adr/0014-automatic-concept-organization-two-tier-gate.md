# Concepts organize themselves: a two-tier confidence gate for hierarchy, de-duplication, and goal-matching

ADR-0013 gave Concepts a curated `broader-of` taxonomy and a claim-grounded lateral graph, but
left the organizing work manual: the system *proposes* a broader parent and the owner *confirms*
every edge (the ADR-0004 gate), goal↔concept links are hand-added, and the normalizer is
deliberately conservative (unsure ⇒ mint a new Concept, ADR-0008). In practice the taxonomy
stayed almost empty — of ~1,250 Concepts, ~85% had no `broader-of` edge at all; the one batch
`hierarchy propose` run was never repeated, so new Concepts arrived as orphans. The catalogue
also fragmented: "Alzheimer's disease", "Alzheimer's", "Alzheimer's disease pathology"… sat as
separate hubs because near-duplicates land in the normalizer's adjudication band (0.15–0.30
cosine) and, with no adjudicator wired in the worker, always defaulted to a new hub. And the
neighbourhood view rolled up only *downward*, so clicking any Concept showed its own descendants
but never its siblings — a leaf looked isolated even inside a rich family.

The owner wants the connective tissue to maintain itself — automatically, using embeddings and
the LLM — without giving up the ADR-0004/0010 safety that a wrong guess never silently corrupts
the graph.

## Decision

Relax "the owner confirms every edge" to a **two-tier confidence gate**, applied uniformly to the
three organizing moves. Each move runs automatically after admission (and as a one-off backfill
over the existing catalogue); the *only* question is whether a given link is confident enough to
apply outright or should wait for one click.

- **High-confidence ⇒ auto-apply.** A link both the embedding says is close *and* the LLM agrees
  on is applied without the owner.
- **Uncertain ⇒ queue.** A plausible-but-looser link is recorded as a *proposal*, invisible to
  roll-up, surfaced in a review queue for one-click confirm/reject.
- **In-place correction is the safety net** (ADR-0010): an auto-applied link the owner disagrees
  with is fixed where they see it, not prevented by an up-front gate.

### The three moves

1. **Hierarchy (`broader-of`).** After a video is admitted, each Concept it touched is run through
   the existing embedding-cluster + `HierarchyProposer` suggester (`curation.suggest_broader_of`,
   now carrying the cosine distance). A proposed parent within `BROADER_AUTOCONFIRM_DISTANCE`
   (0.35) is proposed **and confirmed**; a looser one is left proposed for review. The same logic
   backfills the existing catalogue via `health-bok hierarchy auto`. Confirming an already-acyclic
   proposal can never create a cycle (the guard considers proposed edges too), so no new guard is
   needed.

2. **De-duplication.** The LLM `Adjudicator` (the seam `ConceptNormalizer` already exposes) is
   wired into the worker, so near-duplicate mentions in the adjudication band merge at admit time
   instead of minting a new hub. A one-off `health-bok concepts dedup` collapses existing
   duplicates: for each Concept it takes the nearest other within the band and merges on a
   tight-distance match or a confident adjudication, keeping the canonical hub and re-pointing
   every reference through `Repository.merge_concepts` (markers, edges, lateral relations +
   evidence, embeddings, impacts) so nothing is lost. Merging stays conservative — a wrong merge
   corrupts the graph, a spurious duplicate is cheap to merge later.

3. **Goal-matching.** After admission, each Goal's text is embedded and matched against the
   Concepts the video touched; a match within `GOAL_AUTOATTACH_DISTANCE` (0.4, tighter than the
   on-screen suggestion cutoff of 0.6) is attached automatically via the same `references` edge
   the manual attach asserts. The 0.4–0.6 band still surfaces as an owner-confirmed suggestion on
   the Goal page.

### Neighbourhood shows the family

The roll-up neighbourhood is re-rooted **one level up**: a Concept's neighbourhood now spans its
parents' subtrees (`Repository.family_concept_ids`), so clicking any Concept surfaces its parents,
siblings, and their subtrees — the whole family — attributed to where each relationship lives. A
Concept with no confirmed parent falls back to its own subtree (unchanged for roots).

## Consequences

- **Amends ADR-0013 / ADR-0004** for these three moves only: the taxonomy and goal links are no
  longer owner-confirmed on *every* edge — high-confidence links auto-apply, uncertain ones keep
  the confirm gate through a new review queue (`GET /api/broader-of/proposals`, the `/hierarchy`
  page). The lateral graph is unchanged (still purely claim-derived).
- **Extends ADR-0010**: correction remains in-place; auto-applied links are editable, not gated.
- **Reuses** the ADR-0012 ChatModel seam (the new `Adjudicator` adapter), ADR-0008 pgvector and
  the `references`/`broader-of` edges, and the worker's failure-isolated post-admission chain
  (ADR-0005) — each new step rolls back on its own without touching the durable admission.
- **New failure mode — a wrong auto-link.** Bounded by conservative thresholds and the fact that
  everything auto-applied is reversible in place; the thresholds
  (`BROADER_AUTOCONFIRM_DISTANCE`, `GOAL_AUTOATTACH_DISTANCE`, the dedup bands) are starting
  values to tune against real data after the first backfill.
- **`merge_concepts` is destructive** (it deletes the merged-away hub after re-pointing). It runs
  in one transaction and skips a merge that would close a `broader-of` cycle, so a bad merge rolls
  back whole rather than leaving a half-merged graph.
