"""Backfill lateral Relationships across the existing library (issue #64).

Drives `reprocess.reprocess_relationships` with a fake `Extractor` returning
directed Concept→Concept triples and a `FakeEmbedder`, over a real Postgres +
pgvector. It seeds the *pre-triple* world — videos admitted with Claims but no
relationships — and asserts the run retroactively establishes `concept_relations`
with evidence links through the existing supersede path (ADR-0005/0013), while:

  * a video without an archived Transcript is reported and skipped, never re-fetched
    (the function takes no ContentSource/Transcriber at all — YouTube/Whisper can't
    run by construction);
  * owner-protected Claims survive; only non-protected prior Claims are superseded;
  * the run is idempotent and resumable — a second run is a no-op (the Extractor is
    never re-paid) and re-asserts the same relationships with no orphaned evidence.

Prior art: `test_admission`, `test_concept_relations`.
"""

from __future__ import annotations

from health_bok.concepts import ConceptNormalizer
from health_bok.models import (
    ConceptMention,
    ConceptTriple,
    ExtractedClaim,
    Extraction,
)
from health_bok.reprocess import reprocess_relationships
from health_bok.repository import Repository
from tests.fakes import FakeEmbedder, FakeExtractor
from tests.seed import seed_processed_video

EMBED_MODEL = "fake-embed"

# One-hot orthogonal vectors so each distinct mention is unmistakably its own
# Concept — deterministic, no accidental merges.
CONCEPT_VECTORS = {
    "APOE4": [1, 0, 0, 0, 0],
    "Alzheimer's": [0, 1, 0, 0, 0],
}


def _normalizer(repo: Repository) -> ConceptNormalizer:
    return ConceptNormalizer(FakeEmbedder(CONCEPT_VECTORS), repo, model=EMBED_MODEL)


def _triple_extraction() -> Extraction:
    """A triple-aware extraction — what shipped *after* the existing library was admitted."""
    return Extraction(
        claims=[
            ExtractedClaim(
                text="APOE4 raises Alzheimer's risk.",
                locator_seconds=60,
                type="finding",
                concepts=[
                    ConceptMention(name="APOE4"),
                    ConceptMention(name="Alzheimer's"),
                ],
                triples=[
                    ConceptTriple(
                        subject=ConceptMention(name="APOE4"),
                        predicate="risk_factor_for",
                        object=ConceptMention(name="Alzheimer's"),
                    )
                ],
            )
        ]
    )


def _admit_pre_triple_video(repo: Repository, video_id: str, *, text: str) -> int:
    """Seed an *already-admitted* video with a Claim but no relationships.

    This is the pre-existing library state issue #64 fixes: a Claim admitted before
    triple-aware extraction shipped, so it derived no `concept_relations`. Returns
    the seeded Claim's id.
    """
    seed_processed_video(repo, video_id=video_id, title=f"Video {video_id}")
    claim_id = repo.add_claim(
        video_id, text=text, type="finding", locator_seconds=10
    )
    repo.set_admission(video_id, "admitted")
    repo.commit()
    return claim_id


def test_reprocess_establishes_relationships_across_the_library(conn):
    repo = Repository(conn)
    _admit_pre_triple_video(repo, "vid_a", text="A pre-triple claim about APOE4.")
    _admit_pre_triple_video(repo, "vid_b", text="Another pre-triple claim.")

    # An admitted video with no archived Transcript must be skipped, never re-fetched.
    repo.set_admission("vid_no_transcript", "admitted")
    repo.commit()

    # The existing library is a pile of islands: Claims, but no relationships yet.
    assert repo.list_concept_relations() == []

    extractor = FakeExtractor(_triple_extraction())
    result = reprocess_relationships(
        extractor=extractor, normalizer=_normalizer(repo), repo=repo
    )

    # Both transcript-bearing videos were re-extracted; the transcript-less one skipped.
    assert sorted(result.reprocessed) == ["vid_a", "vid_b"]
    assert result.skipped_no_transcript == ["vid_no_transcript"]
    assert sorted(extractor.extracted) == ["vid_a", "vid_b"]  # never extracted the skipped one

    # The relationship now exists, evidenced by the superseding Claim from *each* video.
    relations = repo.list_concept_relations()
    assert len(relations) == 1
    rel = relations[0]
    assert (rel.src_name, rel.predicate, rel.dst_name) == (
        "APOE4", "risk_factor_for", "Alzheimer's",
    )
    new_claim_ids = {c.id for v in ("vid_a", "vid_b") for c in repo.admitted_claims(v)}
    assert set(rel.evidence_claim_ids) == new_claim_ids

    # The pre-triple Claims were superseded — only the fresh extraction's Claims remain.
    for video_id in ("vid_a", "vid_b"):
        texts = {c.text for c in repo.admitted_claims(video_id)}
        assert texts == {"APOE4 raises Alzheimer's risk."}


def test_owner_protected_claims_are_preserved(conn):
    repo = Repository(conn)
    claim_id = _admit_pre_triple_video(
        repo, "vid_a", text="A hand-corrected claim the owner edited."
    )
    # The owner edited this Claim — every in-place edit marks it protected (ADR-0010).
    repo.update_claim(
        claim_id, text="A hand-corrected claim the owner edited.",
        type="finding", locator_seconds=10,
    )
    repo.commit()

    reprocess_relationships(
        extractor=FakeExtractor(_triple_extraction()),
        normalizer=_normalizer(repo), repo=repo,
    )

    # The protected Claim survives alongside the superseding extraction's Claim.
    texts = {c.text for c in repo.admitted_claims("vid_a")}
    assert "A hand-corrected claim the owner edited." in texts
    assert "APOE4 raises Alzheimer's risk." in texts


def test_second_run_is_a_no_op(conn):
    repo = Repository(conn)
    _admit_pre_triple_video(repo, "vid_a", text="A pre-triple claim.")

    first = FakeExtractor(_triple_extraction())
    reprocess_relationships(extractor=first, normalizer=_normalizer(repo), repo=repo)
    relations_after_first = repo.list_concept_relations()
    assert len(relations_after_first) == 1

    # Resume bookkeeping makes a second full run skip the already-reprocessed video:
    # the Extractor is never re-paid, and the relationships are re-asserted unchanged.
    second = FakeExtractor(_triple_extraction())
    result = reprocess_relationships(
        extractor=second, normalizer=_normalizer(repo), repo=repo
    )
    assert second.extracted == []  # no re-extraction — a true no-op
    assert result.reprocessed == []
    assert result.already_done == ["vid_a"]

    relations_after_second = repo.list_concept_relations()
    assert len(relations_after_second) == 1
    # Same relationship, same single evidence link — no orphans, no duplicates.
    assert relations_after_second[0].id == relations_after_first[0].id
    assert (
        relations_after_second[0].evidence_claim_ids
        == relations_after_first[0].evidence_claim_ids
    )
