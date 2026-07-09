"""Browse & edit the Body of Knowledge against a real Postgres (issue #14).

Slice 9 makes the admitted evidence layer browsable and editable: filterable
lists, detail views that resolve connections by traversing `edges` (no visual
graph, ADR-0009), and in-place edit / delete with edit-protection (ADR-0005,
ADR-0010). This drives those repository reads and `curation` writes — the same
code the HTTP API wraps — over a real Postgres + pgvector, starting from a
genuinely admitted Candidate (the slice-8 path), so the graph it browses is the
one extraction actually builds.
"""

from __future__ import annotations

import psycopg
import pytest

from health_bok import curation, personal, review
from health_bok.repository import Repository
from tests.fakes import FakeExtractor
from tests.seed import seed_processed_video
from tests.test_admission import (
    RAPAMYCIN_CLAIM,
    drain_daily,
    make_extraction,
    normalizer,
)

VIDEO_ID = "vid_bok"
SOURCE_TITLE = "Zone 2 Cardio Explained"  # the seeded video's title
ZONE2_CLAIM = "Zone 2 cardio improves mitochondrial density in healthy adults."


def _admit(repo: Repository) -> None:
    """Seed a processed daily Candidate and admit it via the real slice-8 path."""
    seed_processed_video(repo, video_id=VIDEO_ID, title=SOURCE_TITLE)
    review.approve_candidate(VIDEO_ID, repo=repo)
    drain_daily(FakeExtractor(make_extraction()), repo)


def _claims_by_text(repo: Repository) -> dict:
    return {c.text: c for c in repo.list_claims()}


def _concepts_by_name(repo: Repository) -> dict:
    return {c.name: c for c in repo.list_concepts()}


def _edges_touching(conn, node_type: str, node_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM edges WHERE (src_type = %s AND src_id = %s) "
            "OR (dst_type = %s AND dst_id = %s)",
            (node_type, node_id, node_type, node_id),
        )
        return cur.fetchone()[0]


def test_browse_lists_admitted_entities_with_connections(conn):
    repo = Repository(conn)
    _admit(repo)

    # --- Claims: every admitted Claim, with provenance + locator + Concepts. ----
    claims = repo.list_claims()
    assert len(claims) == 3  # 2 grounded claims + 1 demoted unstructured protocol
    by_text = {c.text: c for c in claims}
    rapamycin = by_text[RAPAMYCIN_CLAIM]
    assert rapamycin.source_title == SOURCE_TITLE
    assert rapamycin.source_video_id == VIDEO_ID
    assert rapamycin.deep_link == "https://www.youtube.com/watch?v=vid_bok&t=300s"
    assert [c.name for c in rapamycin.concepts] == ["rapamycin"]
    assert all(c.protected is False for c in claims)  # nothing edited yet

    # --- Protocols: the one structured Protocol, with its Concept. --------------
    protocols = repo.list_protocols()
    assert len(protocols) == 1
    creatine = protocols[0]
    assert creatine.action == "Take creatine monohydrate"
    assert creatine.dose == "5g"
    assert [c.name for c in creatine.concepts] == ["creatine monohydrate"]
    assert creatine.protected is False

    # --- Concepts: the normalized hubs, each with a reference count. ------------
    concepts = repo.list_concepts()
    assert {c.name for c in concepts} == {
        "zone 2 cardio",
        "mitochondrial density",
        "rapamycin",
        "creatine monohydrate",
        "sleep",
    }
    assert _concepts_by_name(repo)["rapamycin"].reference_count == 1

    # --- Filters narrow the lists (AC: filterable list views). ------------------
    principles = repo.list_claims(type="principle")
    assert [c.text for c in principles] == ["Prioritize sleep"]  # the demoted advice
    rapamycin_concept = _concepts_by_name(repo)["rapamycin"]
    only_rapamycin = repo.list_claims(concept_id=rapamycin_concept.id)
    assert [c.text for c in only_rapamycin] == [RAPAMYCIN_CLAIM]
    creatine_concept = _concepts_by_name(repo)["creatine monohydrate"]
    assert [p.action for p in repo.list_protocols(concept_id=creatine_concept.id)] == [
        "Take creatine monohydrate"
    ]


def test_protocols_filter_by_goal_via_concept_overlap(conn):
    """Filtering Protocols by a Goal returns those whose Concepts overlap the Goal's
    attached Concepts — discovery-oriented, not limited to adopted Protocols (#84).
    """
    repo = Repository(conn)
    _admit(repo)
    concepts = _concepts_by_name(repo)

    # The seeded BoK has one Protocol — "Take creatine monohydrate" — referencing the
    # Concept "creatine monohydrate". A Goal attached to that Concept overlaps it.
    creatine_goal = personal.record_goal(
        title="Build strength",
        detail=None,
        concepts=["creatine monohydrate"],
        normalizer=normalizer(repo),
        repo=repo,
    )
    assert [p.action for p in repo.list_protocols(goal_id=creatine_goal)] == [
        "Take creatine monohydrate"
    ]

    # A Goal attached only to a Concept no Protocol references overlaps nothing.
    rapamycin_goal = personal.record_goal(
        title="Slow ageing",
        detail=None,
        concepts=["rapamycin"],
        normalizer=normalizer(repo),
        repo=repo,
    )
    assert repo.list_protocols(goal_id=rapamycin_goal) == []

    # A Goal with no attached Concepts overlaps nothing: an empty list, by design —
    # the Web App turns this into a "attach Concepts to this Goal" hint (#84).
    bare_goal = personal.record_goal(
        title="Feel better",
        detail=None,
        concepts=[],
        normalizer=normalizer(repo),
        repo=repo,
    )
    assert repo.get_goal(bare_goal).concepts == []
    assert repo.list_protocols(goal_id=bare_goal) == []

    # The Concept filter is unaffected and still narrows to the referencing Concept.
    assert [p.action for p in repo.list_protocols(concept_id=concepts["creatine monohydrate"].id)] == [
        "Take creatine monohydrate"
    ]


def test_detail_views_traverse_connections_both_ways(conn):
    repo = Repository(conn)
    _admit(repo)

    rapamycin_claim = _claims_by_text(repo)[RAPAMYCIN_CLAIM]
    protocol = repo.list_protocols()[0]

    # Slice-8 extraction doesn't yet mint `claim -> protocol supports` edges; the
    # BoK browser only *traverses* them (ADR-0008). Wire one directly so the
    # bidirectional Claim<->Protocol navigation is exercised end to end.
    repo.add_edge("claim", rapamycin_claim.id, "protocol", protocol.id, "supports")
    repo.commit()

    # --- Claim detail: Source, locator, referenced Concepts, supported Protocols.
    detail = repo.get_claim(rapamycin_claim.id)
    assert detail.source_video_id == VIDEO_ID
    assert detail.deep_link.endswith("&t=300s")
    assert [c.name for c in detail.concepts] == ["rapamycin"]
    assert [p.action for p in detail.supports] == ["Take creatine monohydrate"]

    # --- Protocol detail: justifying Claims and referenced Concepts. ------------
    pdetail = repo.get_protocol(protocol.id)
    assert [c.text for c in pdetail.justified_by] == [RAPAMYCIN_CLAIM]
    assert [c.name for c in pdetail.concepts] == ["creatine monohydrate"]

    # --- Concept detail: everything that references it. ------------------------
    concepts = _concepts_by_name(repo)
    rapamycin_concept = repo.get_concept(concepts["rapamycin"].id)
    assert [c.text for c in rapamycin_concept.claims] == [RAPAMYCIN_CLAIM]
    assert rapamycin_concept.protocols == []  # no Protocol references rapamycin
    creatine_concept = repo.get_concept(concepts["creatine monohydrate"].id)
    assert [p.action for p in creatine_concept.protocols] == ["Take creatine monohydrate"]

    assert repo.get_claim(999_999) is None  # a missing entity is a clean None


def test_protocol_detail_groups_claims_per_referenced_concept(conn):
    """A Protocol's detail carries, per referenced Concept, the admitted Claims that
    also reference it (issue #85) — the *why* behind the recommendation — with the
    Protocol's direct-justification Claims deduped out so evidence isn't double-counted.
    """
    repo = Repository(conn)
    _admit(repo)

    protocol = repo.list_protocols()[0]  # "Take creatine monohydrate" -> creatine
    claims = _claims_by_text(repo)
    zone2 = claims[ZONE2_CLAIM]
    rapamycin = claims[RAPAMYCIN_CLAIM]
    creatine = _concepts_by_name(repo)["creatine monohydrate"]

    # No Claim references the creatine Concept yet, so the group still renders — it
    # is just empty (the "Concept with no related Claims" state, issue #85).
    [group] = repo.get_protocol(protocol.id).concept_claims
    assert (group.id, group.name) == (creatine.id, "creatine monohydrate")
    assert group.claims == []

    # Two Claims now reference the creatine Concept; one of them (rapamycin) also
    # directly justifies the Protocol, so it must be deduped out of the group.
    repo.add_edge("claim", zone2.id, "concept", creatine.id, "references")
    repo.add_edge("claim", rapamycin.id, "concept", creatine.id, "references")
    repo.add_edge("claim", rapamycin.id, "protocol", protocol.id, "supports")
    repo.commit()

    pdetail = repo.get_protocol(protocol.id)
    assert [c.text for c in pdetail.justified_by] == [RAPAMYCIN_CLAIM]  # direct evidence
    [group] = pdetail.concept_claims
    assert group.name == "creatine monohydrate"
    # zone2 is grouped under the Concept; rapamycin is omitted (already direct evidence).
    assert [c.text for c in group.claims] == [ZONE2_CLAIM]


def test_edit_persists_and_marks_protected(conn):
    repo = Repository(conn)
    _admit(repo)

    claim = _claims_by_text(repo)[ZONE2_CLAIM]
    assert claim.protected is False  # raw extractor output

    # An owner edit persists and is recorded as a protected version (ADR-0010).
    edited = curation.edit_claim(
        claim.id,
        text="Zone 2 cardio raises mitochondrial density (corrected).",
        type="mechanism",
        locator_seconds=125,
        repo=repo,
    )
    assert edited is True

    reread = repo.get_claim(claim.id)
    assert reread.text == "Zone 2 cardio raises mitochondrial density (corrected)."
    assert reread.type == "mechanism"
    assert reread.locator_seconds == 125
    assert reread.protected is True
    # The flag is the discriminator a later re-extraction supersede (ADR-0005)
    # reads to leave hand-corrected Claims alone: only the edited one is protected.
    assert {c.text: c.protected for c in repo.list_claims()}[RAPAMYCIN_CLAIM] is False

    # Protocols edit the same way, and stay structurally valid.
    protocol = repo.list_protocols()[0]
    assert curation.edit_protocol(
        protocol.id,
        action="Take creatine monohydrate",
        dose="3-5g",
        timing="any time",
        frequency="daily",
        duration=None,
        locator_seconds=420,
        repo=repo,
    ) is True
    preread = repo.get_protocol(protocol.id)
    assert preread.dose == "3-5g"
    assert preread.protected is True

    # Editing a vanished entity is a clean no-op, not a crash.
    assert curation.edit_claim(
        999_999, text="x", type="finding", locator_seconds=0, repo=repo
    ) is False


def test_edit_cannot_strip_a_protocol_of_all_structure(conn):
    """The structure CHECK survives editing — vague advice never sneaks in (ADR-0010)."""
    repo = Repository(conn)
    _admit(repo)
    protocol = repo.list_protocols()[0]

    with pytest.raises(psycopg.errors.CheckViolation):
        repo.update_protocol(
            protocol.id,
            action="Take creatine monohydrate",
            dose=None,
            timing=None,
            frequency=None,
            duration=None,
            locator_seconds=420,
        )
    conn.rollback()


def test_delete_removes_entity_and_its_dangling_edges(conn):
    repo = Repository(conn)
    _admit(repo)

    zone2 = _claims_by_text(repo)[ZONE2_CLAIM]
    protocol = repo.list_protocols()[0]
    # A supports edge so deleting the Protocol must also clear an *inbound* edge.
    repo.add_edge("claim", zone2.id, "protocol", protocol.id, "supports")
    repo.commit()
    assert _edges_touching(conn, "protocol", protocol.id) == 2  # 1 references + 1 supports

    # --- Delete the Protocol: it's gone, and no edge still points at it. --------
    assert curation.delete_protocol(protocol.id, repo=repo) is True
    assert repo.get_protocol(protocol.id) is None
    assert repo.list_protocols() == []
    assert _edges_touching(conn, "protocol", protocol.id) == 0  # dangling edges handled
    # The Claim that supported it survives; only the edge was removed.
    assert repo.get_claim(zone2.id) is not None

    # --- Delete a Claim: it's gone, its Concept edges with it, Concepts remain. --
    assert _edges_touching(conn, "claim", zone2.id) == 2  # its 2 `references` edges
    assert curation.delete_claim(zone2.id, repo=repo) is True
    assert repo.get_claim(zone2.id) is None
    assert _edges_touching(conn, "claim", zone2.id) == 0
    assert "zone 2 cardio" in {c.name for c in repo.list_concepts()}  # hub untouched

    # Deleting a vanished entity is a clean no-op.
    assert curation.delete_claim(zone2.id, repo=repo) is False
