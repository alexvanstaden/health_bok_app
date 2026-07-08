"""The post-admission auto-hierarchy step (ADR-0014): new Concepts find their parents.

Drives the worker with a `HierarchyProposer` + `Embedder` wired and asserts that a
freshly-admitted Concept close to an existing broader one is *auto-confirmed* under
it (visible to roll-up immediately), while a Concept with no nearby parent is left
alone. Exercises the two-tier gate over a real Postgres + pgvector, no Claude API.
"""

from __future__ import annotations

from health_bok import curation, review
from health_bok.concepts import ConceptNormalizer
from health_bok.models import ConceptMention, ExtractedClaim, Extraction
from health_bok.repository import Repository
from health_bok.worker import drain
from tests.fakes import (
    FakeContentSource,
    FakeEmbedder,
    FakeExtractor,
    FakeHierarchyProposer,
    FakeTranscriber,
)
from tests.seed import seed_processed_video

EMBED_MODEL = "fake-embed"
VECTORS = {
    "brain": [1, 1, 0, 0],
    "brain metabolism": [1, 0, 0, 0],  # ~0.29 from brain -> within the auto band
    "ketones": [0, 0, 1, 0],           # far from everything -> no parent proposed
}


def _mint(repo: Repository, name: str) -> int:
    cid = repo.add_concept(name)
    repo.add_embedding("concept", cid, FakeEmbedder(VECTORS).embed(name), model=EMBED_MODEL)
    return cid


def _cid(repo: Repository, name: str) -> int:
    return next(c.id for c in repo.list_concepts() if c.name == name)


def test_worker_auto_confirms_close_parents_for_new_concepts(conn):
    repo = Repository(conn)
    # An existing broad Concept the newly-admitted one can roll up under.
    brain = _mint(repo, "brain")
    repo.commit()

    seed_processed_video(repo, video_id="v1")
    review.approve_candidate("v1", repo=repo)

    extraction = Extraction(
        claims=[
            ExtractedClaim(
                text="Brain metabolism relies on ketones.",
                locator_seconds=30,
                concepts=[
                    ConceptMention(name="brain metabolism"),
                    ConceptMention(name="ketones"),
                ],
            )
        ]
    )
    proposer = FakeHierarchyProposer(["brain"])
    handled = drain(
        content_source=FakeContentSource(),
        transcriber=FakeTranscriber(),
        extractor=FakeExtractor(extraction),
        normalizer=ConceptNormalizer(FakeEmbedder(VECTORS), repo, model=EMBED_MODEL),
        repo=repo,
        hierarchy_proposer=proposer,
        embedder=FakeEmbedder(VECTORS),
        embedding_model=EMBED_MODEL,
    )
    assert handled == 1

    # The new Concept was auto-confirmed under brain (distance ~0.29 < 0.5), so it
    # rolls up immediately without any manual curation.
    bmet = _cid(repo, "brain metabolism")
    assert [p.name for p in repo.broader_parents(bmet, confirmed_only=True)] == ["brain"]
    assert bmet in repo.descendant_concept_ids(brain)

    # "ketones" has no nearby existing Concept, so nothing is proposed for it.
    assert repo.broader_parents(_cid(repo, "ketones")) == []


def test_worker_leaves_a_far_parent_unconfirmed_for_review(conn, monkeypatch):
    # A parent the LLM proposes but that sits beyond the auto-confirm cutoff is proposed
    # *unconfirmed* — it waits in the review queue rather than auto-applying. Pin the
    # cutoff to 0.45 so the mechanism is tested regardless of the production constant
    # (which now meets the 0.6 suggestion ceiling, leaving no queue band by default).
    monkeypatch.setattr(curation, "BROADER_AUTOCONFIRM_DISTANCE", 0.45)
    repo = Repository(conn)
    # 'cognition' [1,0,0,1] is ~0.59 from this broad parent -> beyond the pinned 0.45
    # cutoff but inside the 0.6 suggestion band, so it is queued, not confirmed.
    far_vectors = {"neuroscience": [1, 1, 1, 0], "cognition": [1, 0, 0, 1]}

    neuro = repo.add_concept("neuroscience")
    repo.add_embedding(
        "concept", neuro, FakeEmbedder(far_vectors).embed("neuroscience"), model=EMBED_MODEL
    )
    repo.commit()

    seed_processed_video(repo, video_id="v2")
    review.approve_candidate("v2", repo=repo)

    extraction = Extraction(
        claims=[
            ExtractedClaim(
                text="Cognition is studied widely.",
                locator_seconds=10,
                concepts=[ConceptMention(name="cognition")],
            )
        ]
    )
    drain(
        content_source=FakeContentSource(),
        transcriber=FakeTranscriber(),
        extractor=FakeExtractor(extraction),
        normalizer=ConceptNormalizer(FakeEmbedder(far_vectors), repo, model=EMBED_MODEL),
        repo=repo,
        hierarchy_proposer=FakeHierarchyProposer(["neuroscience"]),
        embedder=FakeEmbedder(far_vectors),
        embedding_model=EMBED_MODEL,
    )

    cog = _cid(repo, "cognition")
    # Proposed but unconfirmed: present in the queue, invisible to roll-up.
    assert repo.broader_parents(cog, confirmed_only=True) == []
    assert [p.name for p in repo.broader_parents(cog)] == ["neuroscience"]
    assert cog not in repo.descendant_concept_ids(neuro)
