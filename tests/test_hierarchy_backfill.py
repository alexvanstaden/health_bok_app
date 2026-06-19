"""Backfill the owner-curated `broader-of` taxonomy via a CSV round-trip (issue #65).

Over a real Postgres + pgvector: the propose step runs the suggester across the whole
catalogue and persists *unconfirmed* proposals (excluding self/existing-parent/
descendant, and skipping a cycle-closing pair rather than crashing); the export step
writes those proposals to a CSV; the apply step enacts an edited CSV — confirming,
rejecting, and repicking edges — honouring the cycle-guard and idempotent on a second
apply. No edge is ever auto-confirmed. Prior art: `test_hierarchy` (propose/confirm,
cycle guard), `test_reprocess` (one-off backfill shape).
"""

from __future__ import annotations

import csv
import io

from health_bok import curation, hierarchy_backfill
from health_bok.repository import Repository
from tests.fakes import FakeEmbedder

EMBED_MODEL = "fake-embed"

# Two independent parent→child pairs, each child within roll-up distance of its parent
# and orthogonal to the other pair, so the suggester's nearby cluster is unambiguous.
VECTORS = {
    "Brain": [1, 1, 0, 0],
    "Brain metabolism": [1, 0, 0, 0],
    "genetics": [0, 1, 1, 0],
    "APOE4": [0, 0, 1, 0],
}


class _TaxonomyProposer:
    """A HierarchyProposer that proposes a fixed parent per Concept name.

    Mirrors the real adapter by returning only parents present in the `nearby` cluster
    the caller supplies, so an out-of-reach parent is never proposed. Lets one proposer
    drive a propose-all pass across the whole catalogue deterministically.
    """

    def __init__(self, parents_by_concept: dict[str, list[str]]):
        self._by = parents_by_concept

    def propose(self, concept_name: str, nearby: list[str]) -> list[str]:
        return [p for p in self._by.get(concept_name, []) if p in nearby]


def _embedder() -> FakeEmbedder:
    return FakeEmbedder(VECTORS)


def _mint(repo: Repository, name: str) -> int:
    cid = repo.add_concept(name)
    repo.add_embedding("concept", cid, _embedder().embed(name), model=EMBED_MODEL)
    return cid


def _seed_catalogue(repo: Repository) -> dict[str, int]:
    ids = {name: _mint(repo, name) for name in VECTORS}
    repo.commit()
    return ids


def _propose_all(repo: Repository, parents: dict[str, list[str]]):
    return hierarchy_backfill.propose_all(
        proposer=_TaxonomyProposer(parents),
        embedder=_embedder(),
        repo=repo,
        model=EMBED_MODEL,
    )


# The two legal parent proposals the taxonomy yields across the catalogue.
TAXONOMY = {"Brain metabolism": ["Brain"], "APOE4": ["genetics"]}


def _edit_and_apply(repo: Repository, decisions: dict[tuple[int, int], tuple]):
    """Export proposals, stamp owner decisions onto matching rows, then apply.

    `decisions` maps (broader_id, narrower_id) -> (decision[, repick_broader_id]).
    Returns the ApplyResult. Exercises the real export→edit→apply file round-trip.
    """
    buf = io.StringIO()
    hierarchy_backfill.export_proposals(repo, out=buf)
    buf.seek(0)
    rows = list(csv.DictReader(buf))
    for row in rows:
        key = (int(row["broader_id"]), int(row["narrower_id"]))
        if key in decisions:
            decision = decisions[key]
            row["decision"] = decision[0]
            row["repick_broader_id"] = str(decision[1]) if len(decision) > 1 else ""

    edited = io.StringIO()
    writer = csv.DictWriter(edited, fieldnames=hierarchy_backfill.CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    edited.seek(0)
    return hierarchy_backfill.apply_decisions(repo, source=edited)


def test_propose_persists_unconfirmed_and_excludes_existing_parents(conn):
    repo = Repository(conn)
    ids = _seed_catalogue(repo)

    # Brain is already a confirmed parent of Brain metabolism — propose-all must not
    # re-offer it, and must leave its confirmation state untouched.
    curation.propose_broader_of(ids["Brain"], ids["Brain metabolism"], repo=repo)
    curation.confirm_broader_of(ids["Brain"], ids["Brain metabolism"], repo=repo)

    result = _propose_all(repo, TAXONOMY)

    # Only the new, legal proposal was persisted — and as *unconfirmed* (no edge is
    # ever auto-confirmed).
    assert result.proposed == [(ids["genetics"], ids["APOE4"])]
    assert repo.list_broader_of(confirmed=False) == [
        (ids["genetics"], ids["APOE4"], False)
    ]
    # The pre-existing confirmed edge is preserved, not reset by re-running propose.
    assert repo.list_broader_of(confirmed=True) == [
        (ids["Brain"], ids["Brain metabolism"], True)
    ]

    # Re-running is a no-op for already-proposed pairs — no duplicates, nothing reset.
    before = repo.list_broader_of()
    _propose_all(repo, TAXONOMY)
    assert repo.list_broader_of() == before


def test_propose_skips_a_cycle_closing_proposal(conn):
    """Two reciprocal proposals in one pass: the cycle-guard skips the loop, not crash."""
    repo = Repository(conn)
    # X and Y sit close enough to be each other's nearby cluster.
    x = repo.add_concept("X")
    repo.add_embedding("concept", x, FakeEmbedder().embed("seed"), model=EMBED_MODEL)
    y = repo.add_concept("Y")
    repo.add_embedding("concept", y, FakeEmbedder().embed("seed"), model=EMBED_MODEL)
    repo.commit()

    result = _propose_all(repo, {"X": ["Y"], "Y": ["X"]})

    # The first proposal lands; its reciprocal would close a loop and is reported.
    assert result.proposed == [(y, x)]
    assert result.skipped_cycle == [(x, y)]
    assert repo.list_broader_of() == [(y, x, False)]


def test_export_round_trips_proposals_to_csv(conn):
    repo = Repository(conn)
    ids = _seed_catalogue(repo)
    _propose_all(repo, TAXONOMY)

    buf = io.StringIO()
    written = hierarchy_backfill.export_proposals(repo, out=buf)
    buf.seek(0)
    rows = list(csv.DictReader(buf))

    assert written == 2
    # Both proposals appear with names + ids and empty, owner-editable decision columns.
    assert {(r["narrower_name"], r["broader_name"]) for r in rows} == {
        ("APOE4", "genetics"),
        ("Brain metabolism", "Brain"),
    }
    for row in rows:
        assert row["decision"] == ""
        assert row["repick_broader_id"] == ""
        assert int(row["broader_id"]) == ids[row["broader_name"]]
        assert int(row["narrower_id"]) == ids[row["narrower_name"]]


def test_apply_confirms_and_rejects_and_is_idempotent(conn):
    repo = Repository(conn)
    ids = _seed_catalogue(repo)
    _propose_all(repo, TAXONOMY)

    # Owner confirms Brain⊃Brain metabolism and rejects genetics⊃APOE4.
    result = _edit_and_apply(
        repo,
        {
            (ids["Brain"], ids["Brain metabolism"]): ("confirm",),
            (ids["genetics"], ids["APOE4"]): ("reject",),
        },
    )
    assert result.confirmed == [(ids["Brain"], ids["Brain metabolism"])]
    assert result.rejected == [(ids["genetics"], ids["APOE4"])]

    # The confirmed edge is now visible to roll-up; the rejected one is gone entirely.
    assert ids["Brain metabolism"] in repo.descendant_concept_ids(ids["Brain"])
    assert repo.list_broader_of() == [(ids["Brain"], ids["Brain metabolism"], True)]

    # Applying the same CSV again leaves the taxonomy unchanged (idempotent).
    before = repo.list_broader_of()
    _edit_and_apply(
        repo,
        {
            (ids["Brain"], ids["Brain metabolism"]): ("confirm",),
            (ids["genetics"], ids["APOE4"]): ("reject",),
        },
    )
    assert repo.list_broader_of() == before


def test_apply_repick_moves_the_edge_to_a_different_broader(conn):
    repo = Repository(conn)
    ids = _seed_catalogue(repo)
    _propose_all(repo, TAXONOMY)

    # The owner judges 'genetics' wrong for APOE4 and repicks 'Brain' instead.
    result = _edit_and_apply(
        repo,
        {(ids["genetics"], ids["APOE4"]): ("repick", ids["Brain"])},
    )
    assert result.repicked == [(ids["genetics"], ids["APOE4"], ids["Brain"])]

    # The original proposal is gone; the repicked parent is confirmed and rolls up.
    parents = repo.broader_parents(ids["APOE4"], confirmed_only=True)
    assert [p.name for p in parents] == ["Brain"]
    assert (ids["genetics"], ids["APOE4"], False) not in repo.list_broader_of()


def test_apply_skips_a_cycle_closing_row(conn):
    repo = Repository(conn)
    ids = _seed_catalogue(repo)
    # A confirmed Brain ⊃ Brain metabolism, plus a proposed genetics ⊃ Brain.
    curation.propose_broader_of(ids["Brain"], ids["Brain metabolism"], repo=repo)
    curation.confirm_broader_of(ids["Brain"], ids["Brain metabolism"], repo=repo)
    curation.propose_broader_of(ids["genetics"], ids["Brain"], repo=repo)

    # Repick genetics⊃Brain onto Brain metabolism⊃Brain — which would close a loop
    # (Brain already reaches Brain metabolism). It must be reported and skipped.
    result = _edit_and_apply(
        repo,
        {(ids["genetics"], ids["Brain"]): ("repick", ids["Brain metabolism"])},
    )
    assert result.skipped_cycle == [(ids["Brain metabolism"], ids["Brain"])]
    assert result.repicked == []

    # The cycle-closing edge never landed, and the rolled-back original survives intact.
    assert (ids["Brain metabolism"], ids["Brain"], True) not in repo.list_broader_of()
    assert (ids["genetics"], ids["Brain"], False) in repo.list_broader_of()
    assert repo.list_broader_of(confirmed=True) == [
        (ids["Brain"], ids["Brain metabolism"], True)
    ]
