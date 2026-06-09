"""Grounded natural-language query against a real Postgres (issue #17, ADR-0011).

Slice 12 turns a free-text question into a grounded, cited answer over the owner's
own library. This drives the `query` service — the same code the HTTP API wraps —
over a real Postgres + pgvector, starting from a genuinely admitted Candidate (the
Slice-8 path) so the Concepts a question retrieves through are the ones extraction
actually built. Retrieval is real; only the `QueryAnswerer` is faked.

Question embeddings are placed in the *same* one-hot space as the seeded Concepts
(via the `FakeEmbedder`), so a question lands exactly on a Concept (cosine distance
0) or orthogonal to all of them (distance 1) — letting the test assert coverage vs
honest abstention deterministically over real pgvector, no network.

Covers, per the acceptance criteria: a question with coverage returns a synthesized
answer citing only admitted Claims (each clickable through to Source + locator); a
question without coverage abstains with the canonical message and never calls the
answerer; retrieval surfaces personal-layer context (Markers/Decisions); a
hallucinated citation is dropped; and the answerer's own abstention is honored.
"""

from __future__ import annotations

from datetime import datetime, timezone

from health_bok import personal, query, review
from health_bok.repository import Repository
from tests.fakes import FakeExtractor, FakeQueryAnswerer
from tests.seed import seed_processed_video
from tests.test_admission import (
    EMBED_MODEL,
    RAPAMYCIN_CLAIM,
    drain_daily,
    make_extraction,
    normalizer,
)

VIDEO_ID = "vid_query"
AT = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)

# Question embeddings in the same one-hot space the admission Concepts use
# (tests.test_admission.CONCEPT_VECTORS). A question equals a Concept's vector
# (distance 0, retrieved) or is orthogonal to all of them (distance 1, excluded).
RAPAMYCIN_Q = "How does rapamycin affect lifespan?"
UNRELATED_Q = "Should I try cold plunges?"
QUESTION_VECTORS = {
    RAPAMYCIN_Q: [0, 0, 1, 0, 0],  # == "rapamycin"
    UNRELATED_Q: [0, 0, 0, 0, 0, 1],  # orthogonal to every seeded Concept
}


def _embedder():
    # Reuse the admission FakeEmbedder so question and Concept share one vector
    # space; an unmapped question would still hash to a far, non-zero vector.
    from tests.fakes import FakeEmbedder

    return FakeEmbedder(QUESTION_VECTORS)


def _admit(repo: Repository) -> None:
    """Admit a video so the BoK has Concepts/Claims/Protocols to retrieve and cite."""
    seed_processed_video(repo, video_id=VIDEO_ID)
    review.approve_candidate(VIDEO_ID, repo=repo)
    drain_daily(FakeExtractor(make_extraction()), repo)


def _answer(repo: Repository, question: str, answerer: FakeQueryAnswerer):
    return query.answer_question(
        question, embedder=_embedder(), answerer=answerer, repo=repo, model=EMBED_MODEL
    )


def _admitted_claim_ids(repo: Repository) -> set[int]:
    return {c.id for c in repo.list_claims()}


def test_answer_is_synthesized_and_cites_only_admitted_claims(conn):
    repo = Repository(conn)
    _admit(repo)

    answerer = FakeQueryAnswerer()  # default: synthesize over + cite every retrieved Claim
    answer = _answer(repo, RAPAMYCIN_Q, answerer)

    # A synthesized answer, not just a list of hits, and not an abstention.
    assert not answer.abstained
    assert answer.text and answer.text != query.ABSTENTION
    assert answer.citations  # always cited

    # Every citation is a real admitted Claim — nothing from general knowledge.
    admitted = _admitted_claim_ids(repo)
    assert {c.claim_id for c in answer.citations} <= admitted

    # The rapamycin Claim is cited, clickable through to its Source + locator
    # (locator_seconds=300 in make_extraction), with its scope qualifier intact.
    rapamycin = next(c for c in answer.citations if c.text == RAPAMYCIN_CLAIM)
    assert rapamycin.deep_link.endswith("&t=300s")
    assert "in mice" in rapamycin.text  # scope qualifier preserved

    # The answerer was actually handed the rapamycin Claim as evidence.
    _, evidence = answerer.calls[-1]
    assert RAPAMYCIN_CLAIM in {c.text for c in evidence.claims}


def test_abstains_when_no_admitted_evidence_covers_the_question(conn):
    repo = Repository(conn)
    _admit(repo)

    answerer = FakeQueryAnswerer()
    answer = _answer(repo, UNRELATED_Q, answerer)

    assert answer.abstained
    assert answer.text == query.ABSTENTION
    assert answer.citations == []
    # Honest abstention is structural: the answerer is never even called, so it
    # cannot confabulate over evidence that does not exist.
    assert answerer.calls == []


def test_retrieval_surfaces_personal_layer_context(conn):
    repo = Repository(conn)
    _admit(repo)

    # A Marker and a Decision that both concern "rapamycin" — normalized onto the
    # same canonical Concept the BoK already minted (Slice 8/11), so a rapamycin
    # question retrieves them alongside the rapamycin Claim.
    personal.record_marker(
        concept="rapamycin",
        value=2.5,
        unit="ng/mL",
        reference_low=None,
        reference_high=2.0,
        measured_at=AT,
        normalizer=normalizer(repo),
        repo=repo,
    )
    personal.record_decision(
        action="Take rapamycin",
        dose="6mg",
        timing=None,
        frequency="weekly",
        duration=None,
        started_at=AT,
        ended_at=None,
        note="trial",
        concepts=["rapamycin"],
        implements_protocol_id=None,
        normalizer=normalizer(repo),
        repo=repo,
    )

    answerer = FakeQueryAnswerer()
    answer = _answer(repo, RAPAMYCIN_Q, answerer)

    assert not answer.abstained
    _, evidence = answerer.calls[-1]
    # Retrieval spans the personal layer, not just the Body of Knowledge.
    assert any(d.action == "Take rapamycin" for d in evidence.decisions)
    marker = next(m for m in evidence.markers if m.concept == "rapamycin")
    assert marker.value == 2.5
    assert marker.out_of_range is True  # 2.5 > reference_high 2.0, derived not stored


def test_hallucinated_citation_id_is_dropped(conn):
    repo = Repository(conn)
    _admit(repo)

    # The answerer cites every retrieved Claim plus a bogus id that was never
    # retrieved; the grounding guard drops the bogus one and keeps the real ones.
    answerer = FakeQueryAnswerer(extra_claim_ids=[999_999])
    answer = _answer(repo, RAPAMYCIN_Q, answerer)

    assert not answer.abstained
    assert answer.citations
    assert 999_999 not in {c.claim_id for c in answer.citations}
    assert {c.claim_id for c in answer.citations} <= _admitted_claim_ids(repo)


def test_answerer_abstention_is_honored(conn):
    repo = Repository(conn)
    _admit(repo)

    # Even with retrievable evidence, an answerer that abstains yields the
    # canonical abstention — the model gets the final say on whether the evidence
    # actually answers the question.
    answer = _answer(repo, RAPAMYCIN_Q, FakeQueryAnswerer(abstain=True))

    assert answer.abstained
    assert answer.text == query.ABSTENTION
    assert answer.citations == []


def test_answer_with_no_citations_is_treated_as_abstention(conn):
    repo = Repository(conn)
    _admit(repo)

    # Cite-or-abstain has no third state: a non-abstaining answer that cites
    # nothing retrievable is itself treated as an abstention.
    answer = _answer(repo, RAPAMYCIN_Q, FakeQueryAnswerer(cite_ids=[]))

    assert answer.abstained
    assert answer.text == query.ABSTENTION
