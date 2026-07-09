"""Backfill the owner-curated `broader-of` taxonomy for the existing catalogue (issue #65).

The `broader-of` hierarchy is owner-curated (ADR-0013/0004): the system *proposes*
broader parents and the owner *confirms*. For the pre-existing catalogue no proposals
were ever generated, so roll-up is empty. There is also no confirm UI yet (deferred),
so curation here happens through a **file round-trip** rather than the screen — three
one-off steps the `health-bok hierarchy` CLI drives:

  * **propose** — run the existing `HierarchyProposer` (`curation.suggest_broader_of`)
    over every existing Concept and persist each result as an *unconfirmed*
    `broader-of` proposal. `suggest_broader_of` already excludes self, existing
    parents, and descendants, so every persisted edge is legal; the DB cycle-guard is
    the last line of defence (two proposals that would close a loop within one run are
    reported and skipped, never fatal). Nothing is minted and nothing is confirmed.
  * **export** — write the current unconfirmed proposals to a CSV the owner edits:
    one row per proposed edge carrying the narrower/broader names + ids and an empty
    `decision` column (plus `repick_broader_id`) for the owner to fill.
  * **apply** — read the edited CSV and enact each decision: `confirm` the marked
    edges (visible to roll-up), `reject` others, and `repick` an edge onto a different
    broader Concept. A row that would close a cycle is reported and skipped (the
    cycle-guard honoured), not crashed on. No edge is ever auto-confirmed — confirmation
    comes solely from the owner's edited CSV.

Each step is idempotent: re-proposing an existing pair is a no-op (the edge's
confirmation state is untouched), and applying the same CSV twice leaves the taxonomy
unchanged. Like the rest of the domain this depends only on the repository (and, for
propose, the proposer/embedder ports), so it is driven in tests against a real Postgres.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from typing import IO

import psycopg

from . import curation
from .ports import Embedder, HierarchyProposer
from .repository import Repository

logger = logging.getLogger("health_bok.hierarchy_backfill")

# The CSV the owner round-trips: identity columns the export fills, plus the two the
# owner edits. `decision` is one of confirm / reject / repick (blank = leave as-is);
# `repick_broader_id` names the new broader Concept when `decision` is `repick`.
CSV_FIELDS = [
    "narrower_id",
    "narrower_name",
    "broader_id",
    "broader_name",
    "decision",
    "repick_broader_id",
]


@dataclass(frozen=True)
class ProposeResult:
    """What a propose-all run did — for logging and test assertions."""

    proposed: list[tuple[int, int]] = field(default_factory=list)
    # The subset of `proposed` a two-tier `auto` run confirmed outright because the
    # parent was close enough (ADR-0014). Empty on a plain propose-only run.
    auto_confirmed: list[tuple[int, int]] = field(default_factory=list)
    skipped_cycle: list[tuple[int, int]] = field(default_factory=list)
    concepts_scanned: int = 0


@dataclass(frozen=True)
class ApplyResult:
    """What an apply run did — confirmations, rejections, repicks, and what it skipped."""

    confirmed: list[tuple[int, int]] = field(default_factory=list)
    rejected: list[tuple[int, int]] = field(default_factory=list)
    repicked: list[tuple[int, int, int]] = field(default_factory=list)
    skipped_cycle: list[tuple[int, int]] = field(default_factory=list)
    skipped_missing: list[tuple[int, int]] = field(default_factory=list)
    unchanged: int = 0


def propose_all(
    *,
    proposer: HierarchyProposer,
    embedder: Embedder,
    repo: Repository,
    model: str,
    auto_confirm_distance: float | None = None,
) -> ProposeResult:
    """Propose broader parents across every existing Concept, persisting them unconfirmed.

    Runs the per-Concept suggester (`curation.suggest_broader_of`, which excludes self,
    existing parents, and descendants) over the whole catalogue and records each
    suggestion as an unconfirmed `broader-of` proposal. Two suggestions that would close
    a loop within one run are caught by the DB cycle-guard, rolled back, and reported —
    never fatal. Idempotent: an already-proposed pair is not re-suggested (it is an
    existing parent) and re-asserting one leaves its confirmation state untouched.

    With `auto_confirm_distance` set (the `hierarchy auto` path, ADR-0014), a proposal
    whose parent sits within that cosine distance is *confirmed outright* — the
    two-tier gate's high-confidence tier, so roll-up organizes automatically — while
    a looser proposal is left unconfirmed for the review queue (or the export/apply
    CSV). Confirming an already-acyclic proposal can never close a cycle (the guard
    considers proposed edges too), so no extra guard is needed here. Left `None`
    (the default), nothing is confirmed — the original propose-only behaviour.
    """
    proposed: list[tuple[int, int]] = []
    auto_confirmed: list[tuple[int, int]] = []
    skipped_cycle: list[tuple[int, int]] = []
    concepts = repo.list_concepts()

    for concept in concepts:
        suggestions = curation.suggest_broader_of(
            concept.id, proposer=proposer, embedder=embedder, repo=repo, model=model
        )
        for parent in suggestions:
            try:
                curation.propose_broader_of(parent.id, concept.id, repo=repo)
            except psycopg.errors.RaiseException:
                # The cycle-guard rejected an edge that would close a loop (user
                # story 17): roll the failed proposal back and keep going.
                repo.rollback()
                logger.warning(
                    "skipping cycle-closing proposal %s broader-of %s",
                    parent.id, concept.id,
                )
                skipped_cycle.append((parent.id, concept.id))
                continue
            proposed.append((parent.id, concept.id))
            if (
                auto_confirm_distance is not None
                and parent.distance <= auto_confirm_distance
            ):
                curation.confirm_broader_of(parent.id, concept.id, repo=repo)
                auto_confirmed.append((parent.id, concept.id))

    logger.info(
        "hierarchy propose: %d concept(s) scanned, %d proposed, %d auto-confirmed, "
        "%d skipped (cycle)",
        len(concepts), len(proposed), len(auto_confirmed), len(skipped_cycle),
    )
    return ProposeResult(
        proposed=proposed,
        auto_confirmed=auto_confirmed,
        skipped_cycle=skipped_cycle,
        concepts_scanned=len(concepts),
    )


def export_proposals(repo: Repository, *, out: IO[str]) -> int:
    """Write the current *unconfirmed* proposals to `out` as the owner-editable CSV.

    One row per proposed (not-yet-confirmed) edge, carrying both Concepts' names + ids
    and empty `decision` / `repick_broader_id` columns. Confirmed edges are already
    decided, so they are not re-exported. Returns the number of rows written.
    """
    names = {c.id: c.name for c in repo.list_concepts()}
    rows = []
    for broader_id, narrower_id, _confirmed in repo.list_broader_of(confirmed=False):
        rows.append(
            {
                "narrower_id": narrower_id,
                "narrower_name": names.get(narrower_id, ""),
                "broader_id": broader_id,
                "broader_name": names.get(broader_id, ""),
                "decision": "",
                "repick_broader_id": "",
            }
        )
    # Stable, human-friendly order: group a narrower Concept's proposed parents together.
    rows.sort(key=lambda r: (r["narrower_name"], r["broader_name"], r["broader_id"]))

    writer = csv.DictWriter(out, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    logger.info("hierarchy export: wrote %d proposal(s)", len(rows))
    return len(rows)


def apply_decisions(repo: Repository, *, source: IO[str]) -> ApplyResult:
    """Apply an edited CSV: confirm, reject, and repick `broader-of` edges (issue #65).

    Reads the owner's decisions and enacts each row in its own transaction so one bad
    row never aborts the rest. A row whose Concept is gone is reported as skipped; a row
    that would close a cycle (a repick onto a descendant, say) is rolled back and
    reported, honouring the cycle-guard. A blank decision leaves the proposal as-is.
    Nothing is confirmed except by an explicit `confirm`/`repick` here. Idempotent:
    applying the same CSV twice leaves the taxonomy unchanged.
    """
    confirmed: list[tuple[int, int]] = []
    rejected: list[tuple[int, int]] = []
    repicked: list[tuple[int, int, int]] = []
    skipped_cycle: list[tuple[int, int]] = []
    skipped_missing: list[tuple[int, int]] = []
    unchanged = 0

    for raw in csv.DictReader(source):
        narrower_id = _as_int(raw.get("narrower_id"))
        broader_id = _as_int(raw.get("broader_id"))
        decision = (raw.get("decision") or "").strip().lower()
        if narrower_id is None or broader_id is None:
            continue  # not a data row (e.g. a stray blank line)
        if decision in ("", "skip", "leave"):
            unchanged += 1
            continue

        if decision == "confirm":
            if repo.confirm_broader_of(broader_id, narrower_id):
                repo.commit()
                confirmed.append((broader_id, narrower_id))
            else:
                repo.rollback()
                skipped_missing.append((broader_id, narrower_id))
        elif decision == "reject":
            if repo.reject_broader_of(broader_id, narrower_id):
                repo.commit()
                rejected.append((broader_id, narrower_id))
            else:
                # Already gone (e.g. a second apply of the same CSV): a stable no-op.
                repo.rollback()
                unchanged += 1
        elif decision == "repick":
            new_broader = _as_int(raw.get("repick_broader_id"))
            if new_broader is None or repo.get_concept(new_broader) is None:
                skipped_missing.append((broader_id, narrower_id))
                continue
            try:
                # Move the edge onto the new broader Concept atomically: drop the old
                # proposal, then propose + confirm the new parent.
                repo.reject_broader_of(broader_id, narrower_id)
                repo.propose_broader_of(new_broader, narrower_id)
                repo.confirm_broader_of(new_broader, narrower_id)
                repo.commit()
                repicked.append((broader_id, narrower_id, new_broader))
            except psycopg.errors.RaiseException:
                repo.rollback()
                logger.warning(
                    "skipping cycle-closing repick %s broader-of %s",
                    new_broader, narrower_id,
                )
                skipped_cycle.append((new_broader, narrower_id))
        else:
            logger.warning("unrecognized decision %r — leaving %s broader-of %s as-is",
                           decision, broader_id, narrower_id)
            unchanged += 1

    logger.info(
        "hierarchy apply: %d confirmed, %d rejected, %d repicked, %d skipped (cycle), "
        "%d skipped (missing), %d unchanged",
        len(confirmed), len(rejected), len(repicked), len(skipped_cycle),
        len(skipped_missing), unchanged,
    )
    return ApplyResult(
        confirmed=confirmed,
        rejected=rejected,
        repicked=repicked,
        skipped_cycle=skipped_cycle,
        skipped_missing=skipped_missing,
        unchanged=unchanged,
    )


def _as_int(value: str | None) -> int | None:
    """Parse a CSV cell to int, tolerating blanks and surrounding whitespace."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None
