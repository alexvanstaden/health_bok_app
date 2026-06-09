"""The Approve → Extract → See Claims vertical, end to end (issue #13).

Drives the whole Part-2 admission path with the new ports faked and a real
ephemeral Postgres + pgvector: an approved daily Candidate is enqueued, the worker
drains it, and its extracted Claims and Protocols land in the Body of Knowledge
with provenance, locators, normalized Concepts, and edges — auto-admitted, no
second gate (ADR-0008, ADR-0009, ADR-0010). Asserts only on what gets persisted
and on the Candidate's visible lifecycle.
"""

from __future__ import annotations

from health_bok import review
from health_bok.concepts import ConceptNormalizer
from health_bok.models import (
    ConceptMention,
    ExtractedClaim,
    ExtractedProtocol,
    Extraction,
)
from health_bok.repository import Repository
from health_bok.worker import drain
from tests.fakes import FakeEmbedder, FakeExtractor
from tests.seed import seed_processed_video

VIDEO_ID = "vid_zone2"
EMBED_MODEL = "fake-embed"

# One-hot, mutually orthogonal vectors so each distinct mention is unmistakably a
# distinct Concept (cosine distance 1) — deterministic, no accidental merges.
CONCEPT_VECTORS = {
    "zone 2 cardio": [1, 0, 0, 0, 0],
    "mitochondrial density": [0, 1, 0, 0, 0],
    "rapamycin": [0, 0, 1, 0, 0],
    "creatine monohydrate": [0, 0, 0, 1, 0],
    "sleep": [0, 0, 0, 0, 1],
}

RAPAMYCIN_CLAIM = "Rapamycin extends lifespan in mice."  # scope qualifier: "in mice"


def make_extraction() -> Extraction:
    return Extraction(
        claims=[
            ExtractedClaim(
                text="Zone 2 cardio improves mitochondrial density in healthy adults.",
                locator_seconds=120,
                type="finding",
                concepts=[
                    ConceptMention(name="zone 2 cardio"),
                    ConceptMention(name="mitochondrial density"),
                ],
            ),
            ExtractedClaim(
                text=RAPAMYCIN_CLAIM,
                locator_seconds=300,
                type="finding",
                concepts=[ConceptMention(name="rapamycin")],
            ),
            # Ungroundable — no locator — must be dropped, not smoothed over.
            ExtractedClaim(text="Someone vaguely mentioned NAD once.", locator_seconds=None),
        ],
        protocols=[
            # Structured: action + dose/timing/frequency -> a real Protocol.
            ExtractedProtocol(
                action="Take creatine monohydrate",
                dose="5g",
                timing="morning",
                frequency="daily",
                locator_seconds=420,
                concepts=[ConceptMention(name="creatine monohydrate")],
            ),
            # Unstructured: no dose/timing/frequency/duration -> demoted to a Claim.
            ExtractedProtocol(
                action="Prioritize sleep",
                locator_seconds=480,
                concepts=[ConceptMention(name="sleep")],
            ),
        ],
    )


def normalizer(repo: Repository) -> ConceptNormalizer:
    return ConceptNormalizer(
        FakeEmbedder(CONCEPT_VECTORS), repo, model=EMBED_MODEL
    )


def test_approve_then_admit_builds_the_body_of_knowledge(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)

    # --- Approve enqueues a job and returns immediately (no admission yet). ----
    assert review.approve_candidate(VIDEO_ID, repo=repo) is True
    assert repo.admission_state(VIDEO_ID) == "approved"
    assert _job_states(conn) == ["queued"]
    assert repo.admitted_claims(VIDEO_ID) == []  # nothing admitted before the worker

    # --- The worker drains the job: approved -> processing -> admitted. --------
    extractor = FakeExtractor(make_extraction())
    handled = drain(extractor=extractor, normalizer=normalizer(repo), repo=repo)
    assert handled == 1
    assert extractor.extracted == [VIDEO_ID]
    assert repo.admission_state(VIDEO_ID) == "admitted"
    assert _job_states(conn) == ["done"]

    # --- Claims: grounded ones kept, ungroundable dropped, qualifier preserved.
    claims = repo.admitted_claims(VIDEO_ID)
    texts = {c.text for c in claims}
    assert RAPAMYCIN_CLAIM in texts  # scope qualifier "in mice" preserved verbatim
    assert "Someone vaguely mentioned NAD once." not in texts  # ungroundable dropped
    # 2 grounded claims + 1 demoted unstructured "protocol" = 3 Claims.
    assert "Prioritize sleep" in texts
    assert len(claims) == 3

    # --- Every Claim carries provenance + a locator deep-link (ADR-0010). -----
    by_text = {c.text: c for c in claims}
    assert by_text[RAPAMYCIN_CLAIM].deep_link == (
        "https://www.youtube.com/watch?v=vid_zone2&t=300s"
    )
    assert by_text["Prioritize sleep"].type == "principle"  # demoted advice

    # --- Protocols: only the structured one exists (ADR-0010). ----------------
    protocols = repo.admitted_protocols(VIDEO_ID)
    assert len(protocols) == 1
    assert protocols[0].action == "Take creatine monohydrate"
    assert protocols[0].dose == "5g"
    assert protocols[0].deep_link == "https://www.youtube.com/watch?v=vid_zone2&t=420s"

    # --- Concepts normalized; references edges wired to Claims & the Protocol. -
    assert _concept_names(conn) == {
        "zone 2 cardio",
        "mitochondrial density",
        "rapamycin",
        "creatine monohydrate",
        "sleep",
    }
    # 5 concept references: 2 (zone2 claim) + 1 (rapamycin) + 1 (sleep claim) +
    # 1 (creatine protocol). All `references`, claim/protocol -> concept.
    assert _edge_count(conn) == 5
    assert {c for claim in claims for c in claim.concepts} == {
        "zone 2 cardio", "mitochondrial density", "rapamycin", "sleep"
    }
    assert protocols[0].concepts == ["creatine monohydrate"]

    # --- An admitted Candidate leaves the daily review queue. ------------------
    assert VIDEO_ID not in {c.video_id for c in repo.list_daily_candidates()}


def test_failed_extraction_marks_candidate_failed_and_is_retryable(conn):
    repo = Repository(conn)
    seed_processed_video(repo, video_id=VIDEO_ID)
    review.approve_candidate(VIDEO_ID, repo=repo)

    # A raising Extractor drives the Candidate to `failed`, nothing half-admitted.
    boom = FakeExtractor(error=RuntimeError("model unavailable"))
    drain(extractor=boom, normalizer=normalizer(repo), repo=repo)
    assert repo.admission_state(VIDEO_ID) == "failed"
    assert _job_states(conn) == ["failed"]
    assert repo.admitted_claims(VIDEO_ID) == []
    # A failed Candidate is still visible in the review queue, so it can be retried.
    failed = {c.video_id: c.state for c in repo.list_daily_candidates()}
    assert failed.get(VIDEO_ID) == "failed"

    # Retry re-enqueues; a working Extractor now admits it.
    assert review.retry_candidate(VIDEO_ID, repo=repo) is True
    assert repo.admission_state(VIDEO_ID) == "approved"
    drain(extractor=FakeExtractor(make_extraction()), normalizer=normalizer(repo), repo=repo)
    assert repo.admission_state(VIDEO_ID) == "admitted"
    assert len(repo.admitted_claims(VIDEO_ID)) == 3


def _job_states(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM jobs ORDER BY id")
        return [r[0] for r in cur.fetchall()]


def _concept_names(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM concepts")
        return {r[0] for r in cur.fetchall()}


def _edge_count(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM edges")
        return cur.fetchone()[0]
