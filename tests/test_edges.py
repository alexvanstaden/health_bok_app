"""The polymorphic edges table's integrity guarantees (ADR-0008).

`edges` can't use real foreign keys for its polymorphic endpoints, so referential
integrity is enforced by a fail-loud trigger and idempotency by a unique
constraint. These assert both against a real Postgres: a dangling endpoint is
rejected, and re-asserting the same edge is a no-op rather than a duplicate.
"""

from __future__ import annotations

import psycopg
import pytest

from health_bok.repository import Repository
from tests.seed import seed_processed_video

VIDEO_ID = "vid_edges"


def _claim_and_concept(repo: Repository) -> tuple[int, int]:
    seed_processed_video(repo, video_id=VIDEO_ID)
    claim_id = repo.add_claim(
        VIDEO_ID, text="A grounded claim.", type="finding", locator_seconds=10
    )
    concept_id = repo.add_concept("apoB")
    repo.commit()
    return claim_id, concept_id


def test_edge_to_existing_endpoints_is_accepted_and_idempotent(conn):
    repo = Repository(conn)
    claim_id, concept_id = _claim_and_concept(repo)

    repo.add_edge("claim", claim_id, "concept", concept_id, "references")
    repo.add_edge("claim", claim_id, "concept", concept_id, "references")  # re-assert
    repo.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM edges")
        assert cur.fetchone()[0] == 1  # the unique constraint deduped the re-assert


def test_dangling_destination_is_rejected(conn):
    repo = Repository(conn)
    claim_id, _ = _claim_and_concept(repo)

    with pytest.raises(psycopg.errors.RaiseException):
        repo.add_edge("claim", claim_id, "concept", 999_999, "references")
    conn.rollback()


def test_dangling_source_is_rejected(conn):
    repo = Repository(conn)
    _, concept_id = _claim_and_concept(repo)

    with pytest.raises(psycopg.errors.RaiseException):
        repo.add_edge("claim", 999_999, "concept", concept_id, "references")
    conn.rollback()


def test_unknown_node_type_is_rejected(conn):
    repo = Repository(conn)
    _, concept_id = _claim_and_concept(repo)

    with pytest.raises(psycopg.errors.RaiseException):
        repo.add_edge("nonsense", 1, "concept", concept_id, "references")
    conn.rollback()
