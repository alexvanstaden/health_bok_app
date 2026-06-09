"""Concept normalization over a real pgvector (ADR-0008).

The `ConceptNormalizer` must merge a mention onto an existing Concept when they
are clearly the same, and mint a new Concept when unsure — never over-merge. This
drives it with a `FakeEmbedder` emitting controlled vectors at chosen cosine
distances, against a real pgvector nearest-neighbour search, and asserts the
merge-vs-new decision falls at the threshold.
"""

from __future__ import annotations

from health_bok.concepts import ConceptNormalizer
from health_bok.models import ConceptMention
from health_bok.repository import Repository
from tests.fakes import FakeEmbedder

EMBED_MODEL = "fake-embed"

# Cosine distance from [1, 0]:
#   "apolipoprotein B" : [1, 0]      -> the seed Concept
#   "apoB"             : [0.999,0.01]-> ~0.00 distance: clearly the same -> merge
#   "near-but-unsure"  : [0.8, 0.6]  -> 0.20 distance: inside the unsure band -> new
#   "zone 2 cardio"    : [0, 1]      -> 1.00 distance: clearly different    -> new
VECTORS = {
    "apolipoprotein B": [1.0, 0.0],
    "apoB": [0.999, 0.01],
    "near-but-unsure": [0.8, 0.6],
    "zone 2 cardio": [0.0, 1.0],
}


def make_normalizer(repo: Repository) -> ConceptNormalizer:
    return ConceptNormalizer(FakeEmbedder(VECTORS), repo, model=EMBED_MODEL)


def test_clear_match_merges_and_distinct_mention_creates_new(conn):
    repo = Repository(conn)
    normalizer = make_normalizer(repo)

    seed = normalizer.resolve(ConceptMention(name="apolipoprotein B"))
    merged = normalizer.resolve(ConceptMention(name="apoB"))
    distinct = normalizer.resolve(ConceptMention(name="zone 2 cardio"))
    repo.commit()

    # A near-identical mention merges onto the seed Concept...
    assert merged == seed
    # ...while an orthogonal one mints its own.
    assert distinct != seed
    assert _concept_count(conn) == 2


def test_unsure_near_match_creates_new_rather_than_over_merging(conn):
    """In the adjudication band with no adjudicator, default to a new Concept."""
    repo = Repository(conn)
    normalizer = make_normalizer(repo)

    seed = normalizer.resolve(ConceptMention(name="apolipoprotein B"))
    unsure = normalizer.resolve(ConceptMention(name="near-but-unsure"))
    repo.commit()

    assert unsure != seed  # conservative: unsure -> new, never over-merge
    assert _concept_count(conn) == 2


def test_adjudicator_may_merge_a_near_match(conn):
    """A confident adjudicator collapses an unsure near-match onto the Concept."""
    repo = Repository(conn)
    normalizer = ConceptNormalizer(
        FakeEmbedder(VECTORS), repo, model=EMBED_MODEL,
        adjudicator=lambda mention, nearest: True,
    )

    seed = normalizer.resolve(ConceptMention(name="apolipoprotein B"))
    near = normalizer.resolve(ConceptMention(name="near-but-unsure"))
    repo.commit()

    assert near == seed
    assert _concept_count(conn) == 1


def _concept_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM concepts")
        return cur.fetchone()[0]
