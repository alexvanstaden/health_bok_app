"""Auto-attaching a just-admitted video's Concepts to matching Goals (ADR-0014).

Drives the worker with an `Embedder` wired and asserts a Goal auto-gains a `references`
edge to a Concept the video touched whose text it closely matches, while a far Concept
and an already-attached one are left alone. Real Postgres + pgvector, no network.
"""

from __future__ import annotations

from health_bok import personal, review
from health_bok.concepts import ConceptNormalizer
from health_bok.models import ConceptMention, ExtractedClaim, Extraction
from health_bok.repository import Repository
from health_bok.worker import drain
from tests.fakes import (
    FakeContentSource,
    FakeEmbedder,
    FakeExtractor,
    FakeTranscriber,
)
from tests.seed import seed_processed_video

EMBED_MODEL = "fake-embed"
VECTORS = {
    "brain health": [1, 0, 0, 0],        # the Goal's text
    "brain metabolism": [0.9, 0.1, 0, 0],  # ~0.006 from the Goal -> auto-attached
    "sleep": [0, 0, 1, 0],               # orthogonal -> never attached
}


def _drain(repo: Repository, extraction: Extraction) -> int:
    return drain(
        content_source=FakeContentSource(),
        transcriber=FakeTranscriber(),
        extractor=FakeExtractor(extraction),
        normalizer=ConceptNormalizer(FakeEmbedder(VECTORS), repo, model=EMBED_MODEL),
        repo=repo,
        embedder=FakeEmbedder(VECTORS),
        embedding_model=EMBED_MODEL,
    )


def _cid(repo: Repository, name: str) -> int:
    return next(c.id for c in repo.list_concepts() if c.name == name)


def test_worker_auto_attaches_close_concepts_to_a_goal(conn):
    repo = Repository(conn)
    goal_id = repo.add_goal(title="brain health")
    repo.commit()

    seed_processed_video(repo, video_id="v1")
    review.approve_candidate("v1", repo=repo)

    _drain(
        repo,
        Extraction(
            claims=[
                ExtractedClaim(
                    text="Brain metabolism matters; so does sleep.",
                    locator_seconds=15,
                    concepts=[
                        ConceptMention(name="brain metabolism"),
                        ConceptMention(name="sleep"),
                    ],
                )
            ]
        ),
    )

    linked = set(repo.concept_ids_for("goal", goal_id))
    # The close Concept the video touched is now linked to the Goal; the orthogonal
    # one is not.
    assert _cid(repo, "brain metabolism") in linked
    assert _cid(repo, "sleep") not in linked


def test_auto_attach_is_idempotent_and_skips_already_linked(conn):
    # An already-attached Concept is not duplicated, and a second run is a no-op.
    repo = Repository(conn)
    goal_id = repo.add_goal(title="brain health")
    repo.commit()

    seed_processed_video(repo, video_id="v1")
    review.approve_candidate("v1", repo=repo)
    extraction = Extraction(
        claims=[
            ExtractedClaim(
                text="Brain metabolism matters.",
                locator_seconds=15,
                concepts=[ConceptMention(name="brain metabolism")],
            )
        ]
    )
    _drain(repo, extraction)

    before = repo.concept_ids_for("goal", goal_id)
    assert before == [_cid(repo, "brain metabolism")]

    # Running the matcher again attaches nothing new (the edge already exists).
    again = personal.auto_attach_goal_concepts_for_video(
        "v1", embedder=FakeEmbedder(VECTORS), repo=repo, model=EMBED_MODEL
    )
    assert again == []
    assert repo.concept_ids_for("goal", goal_id) == before


def test_catalogue_backfill_attaches_close_concepts_regardless_of_video(conn):
    # The rerunnable backfill matches every Goal against the *whole* catalogue, not
    # just one video's Concepts — a close Concept no admitted video linked still
    # attaches, an orthogonal one never does, and a second run is a no-op.
    repo = Repository(conn)
    goal_id = repo.add_goal(title="brain health")
    close = repo.add_concept("brain metabolism")
    repo.add_embedding("concept", close, FakeEmbedder(VECTORS).embed("brain metabolism"), model=EMBED_MODEL)
    far = repo.add_concept("sleep")
    repo.add_embedding("concept", far, FakeEmbedder(VECTORS).embed("sleep"), model=EMBED_MODEL)
    repo.commit()

    result = personal.auto_attach_goal_concepts(
        embedder=FakeEmbedder(VECTORS), repo=repo, model=EMBED_MODEL
    )
    assert result.goals_scanned == 1
    assert result.attached == [(goal_id, close)]
    assert repo.concept_ids_for("goal", goal_id) == [close]

    # Idempotent: re-running attaches nothing and reports an empty pass.
    rerun = personal.auto_attach_goal_concepts(
        embedder=FakeEmbedder(VECTORS), repo=repo, model=EMBED_MODEL
    )
    assert rerun.attached == []
    assert repo.concept_ids_for("goal", goal_id) == [close]
