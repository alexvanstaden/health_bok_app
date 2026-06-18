"""The owner-curated `broader-of` taxonomy: propose/confirm + roll-up (ADR-0013).

Over a real Postgres: a proposed `broader-of` edge stays invisible to roll-up until
confirmed; confirming pulls a descendant's relationships up to the broader Concept,
attributed to where they live; the cycle guard rejects an edge that would close a
loop; and the LLM suggester proposes only legal broader parents. Prior art:
`test_personal_layer` attach/detach, `test_edges` cycle/trigger seam.
"""

from __future__ import annotations

from datetime import datetime, timezone

import psycopg
import pytest

from health_bok import curation
from health_bok.admit import admit_candidate
from health_bok.concepts import ConceptNormalizer
from health_bok.models import (
    ConceptMention,
    ConceptTriple,
    ExtractedClaim,
    Extraction,
)
from health_bok.repository import Repository
from tests.fakes import FakeEmbedder, FakeExtractor, FakeHierarchyProposer
from tests.seed import seed_processed_video

EMBED_MODEL = "fake-embed"
NOW = datetime(2026, 6, 18, tzinfo=timezone.utc)

VECTORS = {
    "Brain metabolism": [1, 0, 0, 0],
    "ketones": [0, 0, 1, 0],
    "Brain": [1, 1, 0, 0],
    "lipid metabolism": [0, 1, 0, 0],
}


def _normalizer(repo):
    return ConceptNormalizer(FakeEmbedder(VECTORS), repo, model=EMBED_MODEL)


def _seed_descendant_relation(repo: Repository) -> None:
    """Admit 'Brain metabolism associated_with ketones' — a relation on a descendant."""
    seed_processed_video(repo, video_id="v1", channel_id="UC_a")
    admit_candidate(
        "v1",
        extractor=FakeExtractor(
            Extraction(
                claims=[
                    ExtractedClaim(
                        text="Brain metabolism is tied to ketones.",
                        locator_seconds=10,
                        concepts=[
                            ConceptMention(name="Brain metabolism"),
                            ConceptMention(name="ketones"),
                        ],
                        triples=[
                            ConceptTriple(
                                subject=ConceptMention(name="Brain metabolism"),
                                predicate="associated_with",
                                object=ConceptMention(name="ketones"),
                            )
                        ],
                    )
                ]
            )
        ),
        normalizer=_normalizer(repo),
        repo=repo,
    )
    repo.commit()


def _cid(repo: Repository, name: str) -> int:
    return next(c.id for c in repo.list_concepts() if c.name == name)


def _mint(repo: Repository, name: str) -> int:
    cid = repo.add_concept(name)
    repo.add_embedding("concept", cid, FakeEmbedder(VECTORS).embed(name), model=EMBED_MODEL)
    return cid


def test_proposed_edge_is_invisible_until_confirmed_then_rolls_up(conn):
    repo = Repository(conn)
    _seed_descendant_relation(repo)
    brain = _mint(repo, "Brain")
    repo.commit()
    bmet = _cid(repo, "Brain metabolism")

    # Propose Brain broader-of Brain metabolism — a suggestion, invisible to roll-up.
    assert curation.propose_broader_of(brain, bmet, repo=repo) is True
    hood = repo.concept_neighbourhood(brain, now=NOW)
    assert hood.sub_concepts == []          # proposed parent doesn't pull children
    assert hood.relations == []             # nor the descendant's relationships

    # Confirm it — now the subtree rolls up.
    assert curation.confirm_broader_of(brain, bmet, repo=repo) is True
    hood = repo.concept_neighbourhood(brain, now=NOW)
    assert [c.name for c in hood.sub_concepts] == ["Brain metabolism"]
    # The Brain-metabolism→ketones relationship surfaces at Brain, attributed to the
    # descendant it actually lives on ("via Brain metabolism").
    [rel] = hood.relations
    assert (rel.src_name, rel.predicate, rel.dst_name) == (
        "Brain metabolism", "associated_with", "ketones",
    )
    assert rel.via_concept_name == "Brain metabolism"


def test_propose_confirm_reject_lifecycle(conn):
    repo = Repository(conn)
    brain = _mint(repo, "Brain")
    bmet = _mint(repo, "Brain metabolism")
    repo.commit()

    assert curation.propose_broader_of(brain, bmet, repo=repo) is True
    assert repo.list_broader_of(confirmed=False) == [(brain, bmet, False)]
    assert repo.list_broader_of(confirmed=True) == []

    assert curation.confirm_broader_of(brain, bmet, repo=repo) is True
    assert repo.list_broader_of(confirmed=True) == [(brain, bmet, True)]

    assert curation.reject_broader_of(brain, bmet, repo=repo) is True
    assert repo.list_broader_of() == []

    # Confirming/rejecting an absent edge is a no-op False; proposing onto a missing
    # Concept is False, not a crash.
    assert curation.confirm_broader_of(brain, bmet, repo=repo) is False
    assert curation.propose_broader_of(brain, 999999, repo=repo) is False


def test_cycle_guard_rejects_a_loop_and_a_self_loop(conn):
    repo = Repository(conn)
    a = _mint(repo, "Brain")
    b = _mint(repo, "Brain metabolism")
    repo.commit()

    curation.propose_broader_of(a, b, repo=repo)
    curation.confirm_broader_of(a, b, repo=repo)

    # b broader-of a would close a loop (a already reaches b) -> the trigger rejects.
    with pytest.raises(psycopg.errors.RaiseException):
        repo.propose_broader_of(b, a)
    conn.rollback()

    # A Concept cannot be broader of itself.
    with pytest.raises(psycopg.errors.RaiseException):
        repo.propose_broader_of(a, a)
    conn.rollback()


def test_suggester_proposes_only_legal_broader_parents(conn):
    repo = Repository(conn)
    bmet = _mint(repo, "Brain metabolism")
    brain = _mint(repo, "Brain")
    _mint(repo, "lipid metabolism")  # nearby but the LLM won't call it a parent
    repo.commit()

    proposer = FakeHierarchyProposer(["Brain"])
    suggestions = curation.suggest_broader_of(
        bmet, proposer=proposer, embedder=FakeEmbedder(VECTORS), repo=repo,
        model=EMBED_MODEL,
    )
    assert [c.name for c in suggestions] == ["Brain"]

    # Once confirmed, the suggester stops re-proposing an existing parent.
    curation.propose_broader_of(brain, bmet, repo=repo)
    curation.confirm_broader_of(brain, bmet, repo=repo)
    assert curation.suggest_broader_of(
        bmet, proposer=FakeHierarchyProposer(["Brain"]), embedder=FakeEmbedder(VECTORS),
        repo=repo, model=EMBED_MODEL,
    ) == []


def test_suggester_degrades_to_empty_on_llm_failure(conn):
    repo = Repository(conn)
    bmet = _mint(repo, "Brain metabolism")
    _mint(repo, "Brain")
    repo.commit()
    boom = FakeHierarchyProposer(error=RuntimeError("model down"))
    assert curation.suggest_broader_of(
        bmet, proposer=boom, embedder=FakeEmbedder(VECTORS), repo=repo, model=EMBED_MODEL
    ) == []
